# -*- coding: utf-8 -*-
import asyncio
import json
import time

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from core.transaction import Tx
from core.storage_network import StorageNetwork
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


# ---------------------------------------------------------------------------
# 去中心化存储网络
# ---------------------------------------------------------------------------

def test_storage_register_pin_claim_proof_reward():
    node = _node()
    provider = QuantumWallet()
    creator = QuantumWallet()
    _fund(node, provider.address)
    _fund(node, creator.address)
    _fund_eco(node)
    eco_before = node.balances[node.economy.ECOSYSTEM_FUND]

    # 注册存储提供者
    _apply(node, _signed_tx(provider, "nova:storage:register", capacity_gb=500))
    assert node.store.storage_providers[provider.address]["capacity_gb"] == 500.0

    # 固定文件：生态基金注入固定奖励池
    cid = _cid()
    _apply(node, _signed_tx(creator, "nova:storage:pin", cid=cid, size_gb=10, duration_days=30))
    claim = node.store.storage_claims[cid]
    assert claim["owner"] == creator.address
    pool = round(10 * 30 * node.economy.STORAGE_REWARD_PER_GB_PER_DAY, 8)
    assert claim["reward_pool"] == pool
    assert node.balances[node.economy.ECOSYSTEM_FUND] == round(eco_before - pool, 8)

    # 认领（提交哈希链密封）
    chain = StorageNetwork.make_chain("secret")
    _apply(node, _signed_tx(provider, "nova:storage:claim", cid=cid, seal=chain[-1]))
    assert provider.address in claim["providers"]

    # 存储证明：揭示下一个前像，获得存储挖矿奖励
    bal_before = node.balances[provider.address]
    _apply(node, _signed_tx(provider, "nova:storage:proof", cid=cid, reveal=chain[-2]))
    assert node.balances[provider.address] == pytest.approx(bal_before + node.economy.STORAGE_PROOF_REWARD)
    assert claim["reward_pool"] == round(pool - node.economy.STORAGE_PROOF_REWARD, 8)
    assert node.store.storage_rewards[provider.address] == node.economy.STORAGE_PROOF_REWARD

    # 同一证明周期内重复提交被拒绝
    assert not node.validate_tx(_signed_tx(provider, "nova:storage:proof", cid=cid, reveal=chain[-3]))
    # 错误的前像被拒绝
    assert not node.validate_tx(_signed_tx(provider, "nova:storage:proof", cid=cid, reveal="0" * 64))
    # 未注册的地址不能认领/证明
    stranger = QuantumWallet()
    _fund(node, stranger.address)
    assert not node.validate_tx(_signed_tx(stranger, "nova:storage:claim", cid=cid, seal="1" * 64))
    assert not node.validate_tx(_signed_tx(stranger, "nova:storage:proof", cid=cid, reveal=chain[-3]))
    # 同一 CID 不能重复固定
    assert not node.validate_tx(_signed_tx(creator, "nova:storage:pin", cid=cid, size_gb=1, duration_days=1))


def test_storage_max_replicas():
    node = _node()
    creator = QuantumWallet()
    _fund(node, creator.address)
    _fund_eco(node)
    cid = _cid(1)
    _apply(node, _signed_tx(creator, "nova:storage:pin", cid=cid, size_gb=1, duration_days=1))
    for i in range(10):
        w = QuantumWallet()
        _fund(node, w.address)
        _apply(node, _signed_tx(w, "nova:storage:register", capacity_gb=100))
        _apply(node, _signed_tx(w, "nova:storage:claim", cid=cid, seal=f"{i:064x}"))
    assert len(node.store.storage_claims[cid]["providers"]) == 10
    extra = QuantumWallet()
    _fund(node, extra.address)
    _apply(node, _signed_tx(extra, "nova:storage:register", capacity_gb=100))
    assert not node.validate_tx(_signed_tx(extra, "nova:storage:claim", cid=cid, seal="f" * 64))


def test_storage_premium_order_payout_and_refund():
    node = _node()
    creator = QuantumWallet()
    p1, p2 = QuantumWallet(), QuantumWallet()
    for w in (creator, p1, p2):
        _fund(node, w.address)
    _fund_eco(node)

    cid = _cid(2)
    _apply(node, _signed_tx(creator, "nova:storage:pin", cid=cid, size_gb=5, duration_days=365))
    chains = [StorageNetwork.make_chain("s1"), StorageNetwork.make_chain("s2")]
    for w, ch in ((p1, chains[0]), (p2, chains[1])):
        _apply(node, _signed_tx(w, "nova:storage:register", capacity_gb=200))
        _apply(node, _signed_tx(w, "nova:storage:claim", cid=cid, seal=ch[-1]))

    # 创作者购买高级存储：100 NOVA 托管，2 副本
    order_amt = 100.0
    bal_c = node.balances[creator.address]
    _apply(node, _signed_tx(creator, "nova:storage:order", amount=order_amt,
                            cid=cid, replicas=2, duration_days=30))
    order_id = list(node.store.storage_orders)[0]
    order = node.store.storage_orders[order_id]
    assert order["amount"] == order_amt
    assert node.balances[creator.address] == pytest.approx(bal_c - order_amt - node.economy.FIXED_GAS)

    # 两个提供者证明后各得 50 NOVA（另加存储挖矿奖励）
    b1 = node.balances[p1.address]
    _apply(node, _signed_tx(p1, "nova:storage:proof", cid=cid, reveal=chains[0][-2]))
    assert node.balances[p1.address] == pytest.approx(b1 + node.economy.STORAGE_PROOF_REWARD + 50.0)
    b2 = node.balances[p2.address]
    _apply(node, _signed_tx(p2, "nova:storage:proof", cid=cid, reveal=chains[1][-2]))
    assert node.balances[p2.address] == pytest.approx(b2 + node.economy.STORAGE_PROOF_REWARD + 50.0)
    assert order["paid_amount"] == order_amt
    assert len(order["paid"]) == 2

    # 到期未发放的托管金退回创作者
    _apply(node, _signed_tx(creator, "nova:storage:order", amount=30.0,
                            cid=cid, replicas=3, duration_days=30))
    oid2 = [k for k, v in node.store.storage_orders.items() if v["amount"] == 30.0][0]
    node.store.storage_orders[oid2]["expires_at"] = time.time() - 1
    bal = node.balances[creator.address]
    assert node.storage_net.settle_expired() == 1
    assert node.store.storage_orders[oid2]["status"] == "expired"
    assert node.balances[creator.address] == pytest.approx(bal + 30.0)


# ---------------------------------------------------------------------------
# 去中心化计算网络
# ---------------------------------------------------------------------------

def test_compute_dual_node_verification():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2):
        _fund(node, w.address)

    bounty = 20.0
    bal_c = node.balances[creator.address]
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=bounty,
                            spec="AI 生成一首流行风格歌曲", expires_in=3600))
    assert node.balances[creator.address] == pytest.approx(bal_c - bounty - node.economy.FIXED_GAS)
    task_id = list(node.store.compute_tasks)[0]
    task = node.store.compute_tasks[task_id]
    assert task["status"] == "open" and task["bounty"] == bounty

    # 发起者不能接自己的任务；未接单不能提交
    assert not node.validate_tx(_signed_tx(creator, "nova:compute:accept", task_id=task_id))
    assert not node.validate_tx(_signed_tx(w1, "nova:compute:submit", task_id=task_id, result_hash="a" * 64))

    _apply(node, _signed_tx(w1, "nova:compute:accept", task_id=task_id))
    _apply(node, _signed_tx(w2, "nova:compute:accept", task_id=task_id))
    assert set(task["accepted"]) == {w1.address, w2.address}
    assert not node.validate_tx(_signed_tx(w1, "nova:compute:accept", task_id=task_id))  # 重复接单

    # 单节点结果不一致：任务保持 open，无人获赏
    bal1 = node.balances[w1.address]
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=task_id, result_hash="a" * 64))
    assert task["status"] == "open"
    assert node.balances[w1.address] == pytest.approx(bal1 - node.economy.FIXED_GAS)

    # 双节点结果一致：验证通过，各得一半悬赏
    b1, b2 = node.balances[w1.address], node.balances[w2.address]
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=task_id, result_hash="a" * 64))
    assert task["status"] == "completed"
    assert node.balances[w1.address] == pytest.approx(b1 + 10.0)
    assert node.balances[w2.address] == pytest.approx(b2 + 10.0)

    # 完成后不能再提交
    assert not node.validate_tx(_signed_tx(w1, "nova:compute:submit", task_id=task_id, result_hash="b" * 64))


def test_compute_expiry_refund():
    node = _node()
    creator = QuantumWallet()
    _fund(node, creator.address)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=50.0,
                            spec="视频转码", expires_in=3600))
    task_id = list(node.store.compute_tasks)[0]
    node.store.compute_tasks[task_id]["expires_at"] = time.time() - 1
    bal = node.balances[creator.address]
    assert node.compute_market.expire_all() == 1
    assert node.store.compute_tasks[task_id]["status"] == "expired"
    assert node.balances[creator.address] == pytest.approx(bal + 50.0)


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------

def test_storage_compute_state_persistence():
    from core.storage import StateStore
    node = _node()
    creator, provider = QuantumWallet(), QuantumWallet()
    _fund(node, provider.address)
    _fund(node, creator.address)
    _fund_eco(node)
    cid = _cid(3)
    _apply(node, _signed_tx(provider, "nova:storage:register", capacity_gb=100))
    _apply(node, _signed_tx(creator, "nova:storage:pin", cid=cid, size_gb=2, duration_days=10))
    chain = StorageNetwork.make_chain("persist")
    _apply(node, _signed_tx(provider, "nova:storage:claim", cid=cid, seal=chain[-1]))
    _apply(node, _signed_tx(provider, "nova:storage:proof", cid=cid, reveal=chain[-2]))
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=5.0,
                            spec="图片超分", expires_in=3600))

    s2 = StateStore(genesis_file="genesis.json")
    s2.from_dict(node.store.to_dict())
    assert s2.storage_providers == node.store.storage_providers
    assert s2.storage_claims == node.store.storage_claims
    assert s2.storage_seals == node.store.storage_seals
    assert s2.storage_orders == node.store.storage_orders
    assert s2.storage_rewards == node.store.storage_rewards
    assert s2.compute_tasks == node.store.compute_tasks


# ---------------------------------------------------------------------------
# RPC 全流程
# ---------------------------------------------------------------------------

async def _make_client(node):
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    return TestClient(TestServer(app))


async def _rpc_send(client, w, op, amount=0.0, **kw):
    data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    resp = await client.post("/api/send", json=tx.to_dict())
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def test_storage_rpc_flow():
    node = _node()
    creator, provider = QuantumWallet(), QuantumWallet()
    _fund(node, provider.address)
    _fund(node, creator.address)
    _fund_eco(node)
    client = await _make_client(node)
    await client.start_server()
    try:
        cid = _cid(4)
        chain = StorageNetwork.make_chain("rpc")
        await _rpc_send(client, provider, "nova:storage:register", capacity_gb=500)
        await _rpc_send(client, creator, "nova:storage:pin", cid=cid, size_gb=3, duration_days=7)
        await _rpc_send(client, provider, "nova:storage:claim", cid=cid, seal=chain[-1])
        await _rpc_send(client, provider, "nova:storage:proof", cid=cid, reveal=chain[-2])

        resp = await client.get("/api/storage/pins")
        assert cid in (await resp.json())["pins"]
        resp = await client.get("/api/storage/providers")
        data = await resp.json()
        assert provider.address in data["providers"]
        resp = await client.get("/api/storage/orders")
        assert (await resp.json())["orders"] == {}
        resp = await client.get("/api/status")
        data = await resp.json()
        assert data["storage_providers"] == 1 and data["pins"] == 1
    finally:
        await client.close()


async def test_compute_rpc_flow():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2):
        _fund(node, w.address)
    client = await _make_client(node)
    await client.start_server()
    try:
        # 通过 /api/compute/publish 处理器发布任务（处理器内部构造 data）
        spec = "AIGC 生成歌曲"
        data = json.dumps({"op": "nova:compute:publish", "spec": spec, "expires_in": 3600.0})
        ts = int(time.time())
        tx = Tx(creator.address, creator.address, 8.0, [], data,
                creator.public_key_hex(), "", timestamp=ts)
        body = {"addr": creator.address, "spec": spec, "bounty": 8.0, "expires_in": 3600,
                "timestamp": ts, "sender_public_key": creator.public_key_hex(),
                "signature": creator.sign(tx.signing_data())}
        resp = await client.post("/api/compute/publish", json=body)
        assert resp.status == 200, await resp.text()
        task_id = (await resp.json())["task_id"]
        assert task_id in node.store.compute_tasks

        # 两个节点接单
        for w in (w1, w2):
            data = json.dumps({"op": "nova:compute:accept", "task_id": task_id})
            ts = int(time.time())
            tx = Tx(w.address, w.address, 0.0, [], data, w.public_key_hex(), "", timestamp=ts)
            body = {"addr": w.address, "task_id": task_id, "timestamp": ts,
                    "sender_public_key": w.public_key_hex(),
                    "signature": w.sign(tx.signing_data())}
            resp = await client.post("/api/compute/accept", json=body)
            assert resp.status == 200, await resp.text()

        # 提交相同结果哈希 → 完成
        result = "abcd1234" * 8
        for w in (w1, w2):
            data = json.dumps({"op": "nova:compute:submit", "task_id": task_id, "result_hash": result})
            ts = int(time.time())
            tx = Tx(w.address, w.address, 0.0, [], data, w.public_key_hex(), "", timestamp=ts)
            body = {"addr": w.address, "task_id": task_id, "result_hash": result, "timestamp": ts,
                    "sender_public_key": w.public_key_hex(),
                    "signature": w.sign(tx.signing_data())}
            resp = await client.post("/api/compute/submit", json=body)
            assert resp.status == 200, await resp.text()

        resp = await client.get("/api/compute/tasks")
        data = await resp.json()
        assert data["tasks"][task_id]["status"] == "completed"
        assert node.balances[w1.address] == pytest.approx(100000.0 - 2 * node.economy.FIXED_GAS + 4.0)
        assert node.balances[w2.address] == pytest.approx(100000.0 - 2 * node.economy.FIXED_GAS + 4.0)
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# 审计回归 F-02：pin 每地址上限 + 禁止自认领 + 模块级基金守卫
# ---------------------------------------------------------------------------
def test_storage_pin_limits_and_self_claim():
    node = _node()
    attacker = QuantumWallet()
    _fund(node, attacker.address)
    _fund_eco(node, 20000.0)
    _apply(node, _signed_tx(attacker, "nova:storage:register", capacity_gb=1024))
    cid = _cid(50)
    _apply(node, _signed_tx(attacker, "nova:storage:pin", cid=cid, size_gb=10, duration_days=30))
    # 自认领被拒绝
    assert not node.validate_tx(_signed_tx(attacker, "nova:storage:claim", cid=cid, seal="1" * 64))
    # 每地址承诺总额上限：0.3 + 3737.6 <= 5000 通过，再 pin 3737.6 超限拒绝
    _apply(node, _signed_tx(attacker, "nova:storage:pin", cid=_cid(51), size_gb=1024, duration_days=3650))
    over = _signed_tx(attacker, "nova:storage:pin", cid=_cid(52), size_gb=1024, duration_days=3650)
    assert not node.validate_tx(over)
    # 模块级纵深防御：基金不足时 pin 拒绝且不改变基金余额（不产生负余额）
    node2 = _node()
    node2.balances[node2.economy.ECOSYSTEM_FUND] = 0.0   # 显式置空基金验证守卫
    assert node2.storage_net.pin("0x" + "1" * 40, "0x" + "2" * 64, 1024.0, 3650) == 0.0
    assert node2.balances.get(node2.economy.ECOSYSTEM_FUND, 0.0) == 0.0
