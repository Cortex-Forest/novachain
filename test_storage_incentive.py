# -*- coding: utf-8 -*-
"""存储激励合约测试：自动注册 / 挑战证明 / 奖励结算 / 作弊罚没 / 状态健康度 /
配额管理 / 心跳监控 / 热门保护 / 退出迁移 / RPC 接口。"""
import asyncio
import hashlib
import json
import time

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from core.transaction import Tx
from core.storage_network import day_index
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9961)
    kw.setdefault("rpc", 8313)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


def _fund_eco(node, amt=1000000.0):
    node.balances[node.economy.ECOSYSTEM_FUND] = amt


def _signed_tx(w, op, amount=0.0, **kw):
    data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _apply(node, tx):
    assert node.validate_tx(tx), "validate failed: " + tx.data[:80]
    node.apply_tx(tx)


def _cid(n=0):
    return "0x" + ("aabbccdd" + f"{n:056x}")


def _content(n=0):
    return bytes([n % 251 for _ in range(2048)])


def _commit(content):
    return hashlib.sha256(content[:1024]).hexdigest()


def _fragment(content):
    return content[:1024].hex()


def _stake(node, wallet, amount=1000.0):
    """质押交易：data 为裸字符串 nova:stake（与现有测试约定一致）。"""
    _fund(node, wallet.address)
    ts = int(time.time())
    tx = Tx(wallet.address, wallet.address, amount, [], "nova:stake",
            wallet.public_key_hex(), "", timestamp=ts)
    tx.signature = wallet.sign(tx.signing_data())
    assert node.validate_tx(tx), "质押交易校验失败"
    node.apply_tx(tx)


def _register_supernode(node, wallet, stake=1000.0):
    """质押即自动注册为存储节点（无需额外配置）。"""
    _stake(node, wallet, amount=stake)
    assert wallet.address in node.store.inc_nodes


def _register_file(node, creator, cid, size_gb=1.0, content=None):
    content = content or _content(1)
    _apply(node, _signed_tx(creator, "nova:storage:inc:file", cid=cid, size_gb=size_gb,
                            fragment_commit=_commit(content), title="测试文件", content_type="music"))
    assert cid in node.store.inc_files


# ---------------------------------------------------------------------------
# 1. 超级节点自动注册 + 文件登记 + 认领 + 挑战证明 + 奖励结算
# ---------------------------------------------------------------------------

def test_inc_auto_register_and_supernode_stake():
    node = _node()
    w = QuantumWallet()
    _register_supernode(node, w, stake=500)
    n = node.store.inc_nodes[w.address]
    assert n["is_supernode"] is True
    assert n["quota_gb"] >= 10.0

    # 旧存储网络注册也会自动进入激励系统
    old = QuantumWallet()
    _fund(node, old.address)
    _apply(node, _signed_tx(old, "nova:storage:register", capacity_gb=50))
    assert old.address in node.store.inc_nodes
    assert node.store.inc_nodes[old.address]["quota_gb"] == 50.0


def test_inc_prove_and_settle_reward():
    node = _node()
    creator, n1, n2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    _register_supernode(node, n1)
    _register_supernode(node, n2)
    _fund(node, creator.address)
    _fund_eco(node)

    # 创作者登记 3 个文件（每个 1GB，前 1KB 片段承诺上链）
    contents = {_cid(i): _content(i) for i in range(3)}
    for cid, content in contents.items():
        _register_file(node, creator, cid, size_gb=1.0, content=content)

    # 节点认领 3 个文件（声称存储）
    for cid in contents:
        _apply(node, _signed_tx(n1, "nova:storage:inc:claim", cid=cid))
    assert len(node.store.inc_nodes[n1.address]["assigned"]) == 3

    # 获取当日挑战（确定性选择 3 个文件）
    day = day_index()
    ch = node.storage_incentive.current_challenge(n1.address, day)
    assert ch["found"] and len(ch["files"]) == 3
    assert set(ch["files"]) == set(contents)

    # 提交正确片段 → 验证通过，记录证明时间戳
    eco_before = node.balances[node.economy.ECOSYSTEM_FUND]
    bal_before = node.balances[n1.address]
    _apply(node, _signed_tx(n1, "nova:storage:inc:prove", day=day,
                            files=ch["files"], fragments=[_fragment(contents[c]) for c in ch["files"]]))
    n = node.store.inc_nodes[n1.address]
    assert n["last_proof_epoch"] == day
    assert n["last_proof_at"] > 0
    assert n["fail_count"] == 0

    # 同一周期重复证明被拒绝
    assert not node.validate_tx(_signed_tx(n1, "nova:storage:inc:prove", day=day,
                                           files=ch["files"],
                                           fragments=[_fragment(contents[c]) for c in ch["files"]]))

    # 结算：每 GB 每月 1 NOVA → 3GB 日奖励 0.1，从生态基金扣除
    result = node.storage_incentive.settle_epoch(day)
    assert result["rewards_paid"] == pytest.approx(0.1)
    assert result["nodes_paid"] == 1
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(eco_before - 0.1)
    assert node.balances[n1.address] == pytest.approx(bal_before + 0.1)
    assert node.store.inc_nodes[n1.address]["revenue"] == pytest.approx(0.1)
    assert node.store.inc_nodes[n1.address]["month_revenue"] == pytest.approx(0.1)

    # 未证明的节点（n2 无文件）不受影响；重复结算幂等
    again = node.storage_incentive.settle_epoch(day)
    assert again["skipped"] == "already"


# ---------------------------------------------------------------------------
# 2. 作弊惩罚：片段错误计失败；连续 3 次罚没质押 10% → 生态基金
# ---------------------------------------------------------------------------

def test_inc_wrong_fragment_counts_failure():
    node = _node()
    creator, n1 = QuantumWallet(), QuantumWallet()
    _register_supernode(node, n1)
    _fund(node, creator.address)
    _fund_eco(node)
    cid = _cid(10)
    _register_file(node, creator, cid, content=_content(10))
    _apply(node, _signed_tx(n1, "nova:storage:inc:claim", cid=cid))

    day = day_index()
    ch = node.storage_incentive.current_challenge(n1.address, day)
    wrong = bytes([0] * 1024)
    _apply(node, _signed_tx(n1, "nova:storage:inc:prove", day=day,
                            files=ch["files"], fragments=[wrong.hex()]))
    assert node.store.inc_nodes[n1.address]["fail_count"] == 1
    assert node.store.inc_nodes[n1.address]["last_proof_epoch"] != day
    # 同一周期内多次失败尝试只计一次；结算也不再重复累计
    _apply(node, _signed_tx(n1, "nova:storage:inc:prove", day=day,
                            files=ch["files"], fragments=[wrong.hex()]))
    assert node.store.inc_nodes[n1.address]["fail_count"] == 1
    node.storage_incentive.settle_epoch(day)
    assert node.store.inc_nodes[n1.address]["fail_count"] == 1


def test_inc_slash_after_three_failures():
    node = _node()
    creator, n1 = QuantumWallet(), QuantumWallet()
    _register_supernode(node, n1, stake=1000.0)
    _fund(node, creator.address)
    _fund_eco(node)
    cid = _cid(11)
    _register_file(node, creator, cid)
    _apply(node, _signed_tx(n1, "nova:storage:inc:claim", cid=cid))

    eco_before = node.balances[node.economy.ECOSYSTEM_FUND]
    assert node.store.stakes[n1.address] == pytest.approx(1000.0)

    # 已有 2 次连续失败，再结算一个未证明周期 → 连续 3 次 → 罚没 10%
    node.store.inc_nodes[n1.address]["fail_count"] = 2
    result = node.storage_incentive.settle_epoch(12345)
    assert result["slashed"] == pytest.approx(100.0)
    assert node.store.stakes[n1.address] == pytest.approx(900.0)
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(eco_before + 100.0)
    assert node.store.inc_slashed == pytest.approx(100.0)
    assert any(e["type"] == "node_slash" for e in node.store.inc_events.values())


# ---------------------------------------------------------------------------
# 3. 存储状态健康度：🟢 3+ / 🟡 1-2 / 🔴 0
# ---------------------------------------------------------------------------

def test_inc_file_status_health():
    node = _node()
    creator = QuantumWallet()
    _fund(node, creator.address)
    _fund_eco(node)
    cid = _cid(20)
    _register_file(node, creator, cid)

    st = node.storage_incentive.file_status(cid)
    assert st["health"] == "red" and st["online"] == 0

    nodes = []
    for i in range(4):
        w = QuantumWallet()
        _register_supernode(node, w)
        _apply(node, _signed_tx(w, "nova:storage:inc:claim", cid=cid))
        nodes.append(w)
    st = node.storage_incentive.file_status(cid)
    assert st["health"] == "green" and st["online"] == 4

    # 下线 2 个 → 2 在线 → 🟡
    node.store.inc_nodes[nodes[0].address]["online"] = False
    node.store.inc_nodes[nodes[1].address]["online"] = False
    st = node.storage_incentive.file_status(cid)
    assert st["health"] == "yellow" and st["online"] == 2

    # 全部离线 → 🔴
    for w in nodes:
        node.store.inc_nodes[w.address]["online"] = False
    assert node.storage_incentive.file_status(cid)["health"] == "red"

def test_inc_quota_and_upgrade():
    node = _node()
    creator, n1 = QuantumWallet(), QuantumWallet()
    _fund(node, creator.address)
    _fund(node, n1.address)
    _fund_eco(node)
    # 通过旧提供者注册进入激励系统：配额 = 声明容量 10GB，无质押加成
    _apply(node, _signed_tx(n1, "nova:storage:register", capacity_gb=10))
    assert node.store.inc_nodes[n1.address]["quota_gb"] == 10.0

    cids = [_cid(30 + i) for i in range(3)]
    for cid in cids:
        _register_file(node, creator, cid, size_gb=6.0)
    # 第一个 6GB 可认领；第二个超配额被拒
    _apply(node, _signed_tx(n1, "nova:storage:inc:claim", cid=cids[0]))
    assert not node.validate_tx(_signed_tx(n1, "nova:storage:inc:claim", cid=cids[1]))

    # 质押 100 NOVA 升级配额 +10GB → 可再认领
    _apply(node, _signed_tx(n1, "nova:storage:inc:upgrade", amount=100.0))
    assert node.store.inc_nodes[n1.address]["quota_gb"] == pytest.approx(20.0)
    _apply(node, _signed_tx(n1, "nova:storage:inc:claim", cid=cids[1]))
    assert node.store.inc_nodes[n1.address]["assigned_gb"] == pytest.approx(12.0)
    # 20GB 配额：6+6+6=18 可认领
    _apply(node, _signed_tx(n1, "nova:storage:inc:claim", cid=cids[2]))
    assert node.store.inc_nodes[n1.address]["assigned_gb"] == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# 5. 心跳监控：30 分钟超时判离线 → 濒危 → 自动重新分配
# ---------------------------------------------------------------------------

def test_inc_heartbeat_offline_reassign():
    node = _node()
    creator, n1, n2, n3 = QuantumWallet(), QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (n1, n2, n3):
        _register_supernode(node, w)
    _fund(node, creator.address)
    _fund_eco(node)
    cid = _cid(40)
    _register_file(node, creator, cid)
    for w in (n1, n2, n3):
        _apply(node, _signed_tx(w, "nova:storage:inc:claim", cid=cid))
    assert node.storage_incentive.file_status(cid)["health"] == "green"

    # 心跳超时 30 分钟 → 全部离线 → 🔴
    node.storage_incentive.scan_offline(time.time() + 1900)
    assert node.store.inc_nodes[n1.address]["online"] is False
    assert node.store.inc_nodes[n2.address]["online"] is False
    assert node.storage_incentive.file_status(cid)["health"] == "red"
    assert any(e["type"] == "file_red" and e["cid"] == cid for e in node.store.inc_events.values())

    # 节点离线期间心跳恢复 → 🟡
    _apply(node, _signed_tx(n1, "nova:storage:inc:heartbeat"))
    assert node.store.inc_nodes[n1.address]["online"] is True
    assert node.storage_incentive.file_status(cid)["health"] == "yellow"

    # 所有节点再次离线 → 濒危 → 自动重新分配（生态基金付费让健康节点接管）
    for w in (n1, n2, n3):
        node.store.inc_nodes[w.address]["online"] = False
    assert node.storage_incentive.file_status(cid)["health"] == "red"

    n4 = QuantumWallet()
    _register_supernode(node, n4)
    eco_before = node.balances[node.economy.ECOSYSTEM_FUND]
    result = node.storage_incentive.reassign_endangered()
    assert result["reassigned"] >= 1
    assert node.store.inc_nodes[n4.address] is not None
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(eco_before - result["spent"])
    assert node.storage_incentive.file_status(cid)["online"] >= 1


# ---------------------------------------------------------------------------
# 6. 热门文件保护：每日访问量前 N，生态基金付费固定 ≥3 副本
# ---------------------------------------------------------------------------

def test_inc_hot_file_protection():
    node = _node()
    creator = QuantumWallet()
    _fund(node, creator.address)
    _fund_eco(node)
    cid = _cid(50)
    _register_file(node, creator, cid, size_gb=1.0)

    nodes = [QuantumWallet() for _ in range(4)]
    for w in nodes:
        _register_supernode(node, w)
    # 记录昨日访问量
    yesterday = day_index() - 1
    node.store.inc_access_counts.setdefault(yesterday, {})[cid] = 100
    _apply(node, _signed_tx(nodes[0], "nova:storage:inc:access", cid=cid))
    assert node.store.inc_files[cid]["access_today"] == 1

    eco_before = node.balances[node.economy.ECOSYSTEM_FUND]
    result = node.storage_incentive.protect_hot_files(yesterday)
    assert result["protected"] == 3            # 补足到 3 个副本
    assert result["spent"] == pytest.approx(6.0)  # 3 × 2 NOVA
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(eco_before - 6.0)
    assert node.store.inc_files[cid]["hot"] is True
    assert node.storage_incentive.file_status(cid)["health"] == "green"


# ---------------------------------------------------------------------------
# 7. 退出迁移：提前 7 天声明 → 数据迁移 → 释放质押
# ---------------------------------------------------------------------------

def test_inc_exit_migration():
    node = _node()
    creator, n1, n2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    _register_supernode(node, n1, stake=500.0)
    _register_supernode(node, n2)
    _fund(node, creator.address)
    _fund_eco(node)
    cid = _cid(60)
    _register_file(node, creator, cid)
    _apply(node, _signed_tx(n1, "nova:storage:inc:claim", cid=cid))

    # 声明退出 → exit_at = now + 7 天
    _apply(node, _signed_tx(n1, "nova:storage:inc:exit"))
    assert node.store.inc_nodes[n1.address]["exit_at"] > time.time()
    assert not node.validate_tx(_signed_tx(n1, "nova:storage:inc:exit"))  # 不能重复声明

    # 模拟 7 天后：迁移文件 → 健康节点接管 → 释放质押到解押队列
    node.store.inc_nodes[n1.address]["exit_at"] = time.time() - 1
    result = node.storage_incentive.finalize_exits()
    assert result["finalized"] == 1
    assert n1.address not in node.store.inc_nodes
    assert n1.address not in node.store.inc_files[cid]["replicas"]
    assert node.store.unbonding.get(n1.address, (0, 0))[0] == pytest.approx(500.0)
    # 接管节点获得付费
    assert node.store.inc_nodes[n2.address]["assigned_gb"] > 0


# ---------------------------------------------------------------------------
# 8. RPC 接口
# ---------------------------------------------------------------------------

async def _make_client(node):
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    return TestClient(TestServer(app))


def _signed_body(w, op, amount=0.0, **kw):
    data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    body = {"addr": w.address, "amount": amount, "timestamp": ts,
             "sender_public_key": w.public_key_hex(), "signature": tx.signature,
             "data": data}
    body.update(kw)
    return body


async def test_inc_rpc_flow():
    node = _node()
    creator, n1 = QuantumWallet(), QuantumWallet()
    _register_supernode(node, n1)
    _fund(node, creator.address)
    _fund_eco(node)
    client = await _make_client(node)
    await client.start_server()
    try:
        cid = _cid(70)
        content = _content(7)
        body = _signed_body(creator, "nova:storage:inc:file", cid=cid, size_gb=1.0,
                            fragment_commit=_commit(content), title="RPC 文件",
                            content_type="music")
        resp = await client.post("/api/storage/inc/file", json=body)
        assert resp.status == 200, await resp.text()

        resp = await client.post("/api/storage/inc/claim",
                                 json=_signed_body(n1, "nova:storage:inc:claim", cid=cid))
        assert resp.status == 200, await resp.text()

        # 查询文件存储状态
        resp = await client.get(f"/api/storage/status/{cid}")
        assert resp.status == 200
        st = await resp.json()
        assert st["health"] == "yellow" and st["online"] == 1

        # 查询全网存储节点
        resp = await client.get("/api/storage/nodes")
        data = await resp.json()
        assert data["total"] == 1 and n1.address in data["nodes"]

        # 获取挑战并提交证明
        resp = await client.get(f"/api/storage/nodes/{n1.address}/challenge")
        ch = await resp.json()
        assert ch["found"] and ch["files"] == [cid]
        day = ch["day"]
        resp = await client.post("/api/storage/prove", json=_signed_body(
            n1, "nova:storage:inc:prove", day=day, files=ch["files"],
            fragments=[_fragment(content)]))
        assert resp.status == 200, await resp.text()
        assert node.store.inc_nodes[n1.address]["last_proof_epoch"] == day

        # 节点收益统计
        resp = await client.get(f"/api/storage/nodes/{n1.address}/revenue")
        rev = await resp.json()
        assert rev["found"] and rev["stored_gb"] == pytest.approx(1.0)
        assert rev["health_pct"] == 100.0

        # 创作者面板：文件状态 + 事件通知
        resp = await client.get(f"/api/storage/creator/{creator.address}")
        panel = await resp.json()
        assert len(panel["files"]) == 1 and panel["files"][0]["health"] == "yellow"
        assert any(e["type"] == "file_register" for e in panel["events"])

        # 汇总
        resp = await client.get("/api/storage/inc/summary")
        s = await resp.json()
        assert s["nodes"] == 1 and s["files"] == 1 and s["yellow"] == 1
    finally:
        await client.close()



