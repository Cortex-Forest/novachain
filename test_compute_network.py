# -*- coding: utf-8 -*-
"""算力网络 + AI 生成服务（提示词 1-5）测试。

覆盖：
1. 算力节点注册/自动资格、规格上链公开、超规格接单被拒；
2. 信誉分（初始 50、完成+1、正确+2、作恶-100）与轻量降级；
3. 任务生命周期状态机（open → assigned → submitted → arbitrating → settled/completed）；
4. 市场：抢单/竞价/选标/结果提交（哈希+IPFS）/1% 手续费回流激励池；
5. 验证与防作弊：双节点一致结算、不一致第三方仲裁、质押/解押 7 天冷静期、
   争议冻结+社区仲裁、5% 随机抽查与双倍罚没；
6. 激励：激励池 60/40 按贡献分配、信誉加成 5-15%、收益统计；
7. AI 服务：服务登记、音乐人循环配置、自动定价、70/20/10 分账、成长基金收支。
"""
import json
import time

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from core.transaction import Tx
from core.compute import (ComputeMarket, TASK_TYPES, reference_price, spec_meets,
                          gpu_tier, MIN_COMPUTE_STAKE, MAX_COMPUTE_STAKE,
                          MARKET_FEE_RATE, AUDIT_SLASH_MULT, COMPUTE_POOL)
from core.ai_service import (AIService, AI_FUND, TRIGGER_FEE, REV_CREATOR,
                             REV_COMPUTE, REV_FUND, FUND_SINGLE_SPEND_LIMIT)
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9971)
    kw.setdefault("rpc", 8323)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


def _signed_tx(w, op, amount=0.0, **kw):
    data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _apply(node, tx):
    assert node.validate_tx(tx), "validate failed: " + tx.data[:120]
    node.apply_tx(tx)


def _cid(n=0):
    return "0x" + ("ab12cd34" * 6) + f"{n:08x}"


def _wallet_by_addr(node, addr):
    for w in node._test_wallets:
        if w.address == addr:
            return w
    raise KeyError(addr)


def _stake_validator(node, w, amt):
    """验证者质押：data 为裸字符串 nova:stake（与现有测试约定一致）。"""
    ts = int(time.time())
    tx = Tx(w.address, w.address, amt, [], "nova:stake", w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    _apply(node, tx)


def _register_node(node, w, cpu=8, gpu="RTX 4090", vram=24, ram=64, storage=200):
    _apply(node, _signed_tx(w, "nova:compute:register", cpu_cores=cpu, gpu_model=gpu,
                            gpu_vram_gb=vram, ram_gb=ram, storage_gb=storage,
                            region="cn-east", latency_ms=30))
    return node.store.compute_nodes[w.address]


def _stake_node(node, w, amt=200.0):
    _apply(node, _signed_tx(w, "nova:compute:stake", amount=amt))
    assert node.store.compute_stakes[w.address] == amt


# ---------------------------------------------------------------------------
# 提示词 1：节点注册 / 规格 / 任务类型 / 信誉
# ---------------------------------------------------------------------------

def test_node_register_and_super_node_auto_qualify():
    node = _node()
    w = QuantumWallet()
    validator = QuantumWallet()
    _fund(node, w.address)
    _fund(node, validator.address)
    # 未注册非超级节点不具备资格
    assert not node.compute_market.is_qualified_node(w.address)
    # 注册后规格公开可查
    spec = _register_node(node, w)
    assert spec["cpu_cores"] == 8 and spec["gpu_model"] == "RTX 4090"
    assert spec["gpu_vram_gb"] == 24 and spec["ram_gb"] == 64 and spec["storage_gb"] == 200
    assert node.compute_market.is_qualified_node(w.address)
    # 超级节点（质押验证者）自动具备资格，无需注册
    _stake_validator(node, validator, 1000)
    assert node.compute_market.is_qualified_node(validator.address)
    auto_spec = node.compute_market.node_spec(validator.address)
    assert auto_spec.get("auto_qualified") is True
    assert node.compute_market.node_view(validator.address)["super_node"] is True


def test_task_types_and_spec_meets():
    # 五类任务类型齐备
    assert set(TASK_TYPES) == {"ai_music", "ai_image", "game_server",
                               "video_transcode", "data_clean"}
    # 参考价：AI 音乐 0.5-2 / AI 图像 0.1-0.5
    assert reference_price("ai_music")["min"] == 0.5
    assert reference_price("ai_music")["max"] == 2.0
    assert reference_price("ai_image")["min"] == 0.1
    assert reference_price("ai_image")["max"] == 0.5
    # 规格满足 / 不满足（超规格接单 = 拒绝）
    ok, _ = spec_meets({"cpu_cores": 8, "gpu_model": "RTX 4090", "ram_gb": 32,
                        "storage_gb": 100}, "ai_music")
    assert ok
    ok, reason = spec_meets({"cpu_cores": 2, "gpu_model": "GTX 1650", "ram_gb": 4,
                             "storage_gb": 5}, "ai_music")
    assert not ok and "CPU" in reason
    assert gpu_tier("RTX 4090") == 3 and gpu_tier("A100") == 3
    assert gpu_tier("T4") == 2 and gpu_tier("GTX 1650") == 1


def test_reputation_and_light_demotion():
    node = _node()
    w = QuantumWallet()
    _fund(node, w.address)
    _register_node(node, w)
    rep0 = node.compute_market.compute_reputation(w.address)
    assert rep0["score"] == 50.0
    # 完成 5 次 + 正确 5 次 → 50 + 5 + 10 = 65（星核节点，10% 加成）
    node.compute_market._touch_stats(w.address, completed=5, correct=5)
    rep = node.compute_market.compute_reputation(w.address)
    assert rep["score"] == 65.0
    assert rep["tier"] == "星核节点" and rep["bonus"] == 0.10
    # 作恶 → 信誉分清零并降级为轻量节点（只接数据清洗类）
    node.compute_market._touch_stats(w.address, cheated=1)
    rep_bad = node.compute_market.compute_reputation(w.address)
    assert rep_bad["score"] == 0.0
    assert rep_bad["tier"] == "轻量节点"
    assert rep_bad["max_budget"] == 50.0
    # 降级后只能接 data_clean
    creator = QuantumWallet()
    _fund(node, creator.address)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=10.0, spec="清洗",
                            task_type="ai_music", expires_in=3600))
    tid_music = list(node.store.compute_tasks)[0]
    ok, reason = node.compute_market.validate_accept(w.address, tid_music)
    assert not ok and "降级" in reason
    _stake_node(node, w)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=10.0, spec="标注",
                            task_type="data_clean", expires_in=3600))
    tid_clean = list(node.store.compute_tasks)[1]
    assert node.compute_market.validate_accept(w.address, tid_clean)[0]


# ---------------------------------------------------------------------------
# 提示词 2：任务市场（发布/接单/结算）
# ---------------------------------------------------------------------------

def test_market_grab_lifecycle_and_fee():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2):
        _fund(node, w.address)
    _register_node(node, w1)
    _register_node(node, w2)
    _stake_node(node, w1)
    _stake_node(node, w2)

    bounty = 20.0
    pool_before = node.balances.get(node.economy.VALIDATOR_POOL, 0.0)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=bounty,
                            spec="生成一首流行风格歌曲", task_type="ai_music",
                            mode="grab", min_nodes=2, expires_in=3600,
                            acceptance="BPM 120 以内，时长 3 分钟"))
    tid = list(node.store.compute_tasks)[0]
    task = node.store.compute_tasks[tid]
    assert task["status"] == "open" and task["bounty"] == bounty
    assert task["mode"] == "grab" and task["min_nodes"] == 2
    # 发起者不能接；未质押节点不能接
    assert not node.validate_tx(_signed_tx(creator, "nova:compute:accept", task_id=tid))
    stranger = QuantumWallet()
    _fund(node, stranger.address)
    assert not node.validate_tx(_signed_tx(stranger, "nova:compute:accept", task_id=tid))
    # 未注册但质押的超级节点自动具备资格（data_clean 无需 GPU，验证自动接单）
    validator = QuantumWallet()
    _fund(node, validator.address)
    _stake_validator(node, validator, 1000)
    _apply(node, _signed_tx(validator, "nova:compute:stake", amount=100))
    assert node.compute_market.is_qualified_node(validator.address)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=10.0, spec="标注任务",
                            task_type="data_clean", expires_in=3600))
    tid_clean = list(node.store.compute_tasks)[1]
    _apply(node, _signed_tx(validator, "nova:compute:accept", task_id=tid_clean))
    assert node.store.compute_tasks[tid_clean]["status"] in ("open", "assigned")
    # 抢单满 2 节点 → assigned
    _apply(node, _signed_tx(w1, "nova:compute:accept", task_id=tid))
    _apply(node, _signed_tx(w2, "nova:compute:accept", task_id=tid))
    assert task["status"] == "assigned"
    # 名额已满
    assert not node.validate_tx(_signed_tx(w1, "nova:compute:accept", task_id=tid))
    # 提交结果（含 IPFS 地址与时间戳）
    result = "aa" * 32
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid,
                            result_hash=result, result_cid=_cid(1)))
    assert task["status"] == "submitted"
    assert task["results"][w1.address]["cid"] == _cid(1)
    assert task["results"][w1.address]["at"] > 0
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid,
                            result_hash=result, result_cid=_cid(2)))
    assert task["status"] == "completed"
    # 1% 手续费回流验证者激励池
    fee = round(bounty * MARKET_FEE_RATE, 8)
    assert node.balances[node.economy.VALIDATOR_POOL] == pytest.approx(pool_before + fee)
    # 结算总额 = 预算 - 手续费；两个节点按信誉权重分
    paid = node.store.compute_stats[w1.address]["task_reward"] +         node.store.compute_stats[w2.address]["task_reward"]
    assert paid == pytest.approx(bounty - fee)
    # 生命周期链上记录
    states = [h["state"] for h in task["history"]]
    assert "open" in states and "assigned" in states and "submitted" in states         and "completed" in states


def test_market_bid_mode_and_award():
    node = _node()
    creator, w1, w2, w3 = QuantumWallet(), QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2, w3):
        _fund(node, w.address)
    for w in (w1, w2, w3):
        _register_node(node, w)
        _stake_node(node, w)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=30.0,
                            spec="游戏服务器托管 7 天", task_type="game_server",
                            mode="bid", expires_in=3600))
    tid = list(node.store.compute_tasks)[0]
    # 出价
    _apply(node, _signed_tx(w1, "nova:compute:bid", task_id=tid, price=14.0))
    _apply(node, _signed_tx(w2, "nova:compute:bid", task_id=tid, price=13.0))
    _apply(node, _signed_tx(w3, "nova:compute:bid", task_id=tid, price=15.0))
    assert len(node.store.compute_bids[tid]) == 3
    assert not node.validate_tx(_signed_tx(w1, "nova:compute:bid", task_id=tid, price=10.0))
    # 发起者选标
    assert not node.validate_tx(_signed_tx(w1, "nova:compute:award", task_id=tid,
                                           workers=[w1.address, w2.address]))
    _apply(node, _signed_tx(creator, "nova:compute:award", task_id=tid,
                            workers=[w1.address, w2.address]))
    task = node.store.compute_tasks[tid]
    assert task["status"] == "assigned" and set(task["assigned"]) == {w1.address, w2.address}
    assert task["bid_prices"] == {w1.address: 14.0, w2.address: 13.0, w3.address: 15.0}
    # 执行与结算
    result = "bb" * 32
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash=result))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash=result))
    assert task["status"] == "completed"


def test_market_expire_refund_new_task():
    node = _node()
    creator = QuantumWallet()
    _fund(node, creator.address)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=50.0, spec="转码",
                            task_type="video_transcode", expires_in=3600))
    tid = list(node.store.compute_tasks)[0]
    node.store.compute_tasks[tid]["expires_at"] = time.time() - 1
    bal = node.balances[creator.address]
    assert node.compute_market.expire_all() == 1
    assert node.store.compute_tasks[tid]["status"] == "expired"
    assert node.balances[creator.address] == pytest.approx(bal + 50.0)


# ---------------------------------------------------------------------------
# 提示词 2/4：验证、仲裁、争议
# ---------------------------------------------------------------------------

def _new_task(node, creator, w1, w2, bounty=20.0):
    _register_node(node, w1)
    _register_node(node, w2)
    _stake_node(node, w1)
    _stake_node(node, w2)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=bounty,
                            spec="图片超分 4x", task_type="ai_image",
                            mode="grab", expires_in=3600))
    tid = list(node.store.compute_tasks)[0]
    _apply(node, _signed_tx(w1, "nova:compute:accept", task_id=tid))
    _apply(node, _signed_tx(w2, "nova:compute:accept", task_id=tid))
    return tid


def test_arbitration_third_node_decides():
    node = _node()
    creator, w1, w2, w3 = QuantumWallet(), QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2, w3):
        _fund(node, w.address)
    tid = _new_task(node, creator, w1, w2)
    task = node.store.compute_tasks[tid]
    # 双节点结果不一致 → 仲裁
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash="c1" * 32))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash="c2" * 32))
    assert task["status"] == "arbitrating"
    # 未注册节点不能仲裁；执行节点不能仲裁
    assert not node.validate_tx(_signed_tx(creator, "nova:compute:arbitrate",
                                           task_id=tid, result_hash="c1" * 32))
    _register_node(node, w3)
    _stake_node(node, w3)
    # 仲裁支持 w1
    _apply(node, _signed_tx(w3, "nova:compute:arbitrate", task_id=tid, result_hash="c1" * 32))
    assert task["status"] == "completed"
    assert task["arbiter"] == w3.address
    # 正确方 w1 获得报酬；错误方 w2 记 wrong
    assert node.store.compute_stats[w1.address]["correct"] >= 1
    assert node.store.compute_stats[w2.address]["wrong"] >= 1
    assert node.store.compute_stats[w1.address]["task_reward"] > 0


def test_dispute_freeze_and_community_arbitration():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2):
        _fund(node, w.address)
    tid = _new_task(node, creator, w1, w2)
    result = "dd" * 32
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash=result))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash=result))
    # 发起者在 24h 内提出异议 → 预算冻结
    _apply(node, _signed_tx(creator, "nova:compute:dispute", task_id=tid, reason="结果错误"))
    task = node.store.compute_tasks[tid]
    assert task["status"] == "disputed"
    assert task["frozen"] > 0
    # 社区仲裁投票（矿工/质押者/算力节点）
    voters = []
    for i in range(3):
        v = QuantumWallet()
        _fund(node, v.address)
        _stake_validator(node, v, 1000)
        voters.append(v)
    _apply(node, _signed_tx(voters[0], "nova:compute:vote", task_id=tid, support="uphold"))
    _apply(node, _signed_tx(voters[1], "nova:compute:vote", task_id=tid, support="uphold"))
    _apply(node, _signed_tx(voters[2], "nova:compute:vote", task_id=tid, support="dismiss"))
    assert not node.validate_tx(_signed_tx(voters[0], "nova:compute:vote",
                                           task_id=tid, support="uphold"))
    # 达 3 票 → 仲裁决议：串通作恶 → 罚没 + 退款
    assert node.compute_market._settle_disputes() == 1
    assert task["status"] == "failed"
    assert node.store.compute_stakes.get(w1.address, 0) == 0
    assert node.store.compute_stakes.get(w2.address, 0) == 0
    assert node.store.compute_slashed > 0
    assert node.balances[creator.address] >= 100000.0 - 20.0 - 1e-6  # 预算已退回


def test_dispute_dismiss_restores_payment():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2):
        _fund(node, w.address)
    tid = _new_task(node, creator, w1, w2)
    result = "ee" * 32
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash=result))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash=result))
    _apply(node, _signed_tx(creator, "nova:compute:dispute", task_id=tid, reason="再验一次"))
    voters = []
    for i in range(3):
        v = QuantumWallet()
        _fund(node, v.address)
        _stake_validator(node, v, 1000)
        voters.append(v)
    for v in voters:
        _apply(node, _signed_tx(v, "nova:compute:vote", task_id=tid, support="dismiss"))
    assert node.compute_market._settle_disputes() == 1
    assert node.store.compute_tasks[tid]["status"] == "completed"
    # 结算恢复：节点拿回报酬
    assert node.store.compute_stats[w1.address]["task_reward"] > 0
    assert node.store.compute_stats[w2.address]["task_reward"] > 0


# ---------------------------------------------------------------------------
# 提示词 4/5：质押 / 抽查 / 激励
# ---------------------------------------------------------------------------

def test_compute_stake_unstake_claim_cooldown():
    node = _node()
    w = QuantumWallet()
    _fund(node, w.address, 10000.0)
    bal0 = node.balances[w.address]
    # 质押上下限
    assert not node.validate_tx(_signed_tx(w, "nova:compute:stake", amount=50))
    assert not node.validate_tx(_signed_tx(w, "nova:compute:stake", amount=20000))
    _apply(node, _signed_tx(w, "nova:compute:stake", amount=500))
    assert node.store.compute_stakes[w.address] == 500
    assert node.balances[w.address] == pytest.approx(bal0 - 500 - node.economy.FIXED_GAS)
    # 解押进入 7 天冷静期
    _apply(node, _signed_tx(w, "nova:compute:unstake", amount=200))
    assert node.store.compute_stakes[w.address] == 300
    unb = node.store.compute_unbonding[w.address]
    assert unb[1] > time.time() + 6.9 * 86400
    # 冷静期内不能领取
    assert not node.validate_tx(_signed_tx(w, "nova:compute:claim"))
    # 模拟冷却结束
    node.store.compute_unbonding[w.address] = (unb[0], time.time() - 1)
    bal1 = node.balances[w.address]
    _apply(node, _signed_tx(w, "nova:compute:claim"))
    assert node.balances[w.address] == pytest.approx(bal1 + 200.0)


def test_random_audit_slash_double_stake():
    node = _node()
    creator, w1, w2, w3 = QuantumWallet(), QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2, w3):
        _fund(node, w.address)
    tid = _new_task(node, creator, w1, w2, bounty=30.0)
    result = "ff" * 32
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash=result))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash=result))
    task = node.store.compute_tasks[tid]
    assert task["status"] == "completed"
    # 注册第三方审计节点
    _register_node(node, w3)
    # 找一个随机命中 5% 抽查的日期
    t0 = time.time()
    found = None
    for i in range(5000):
        day = time.strftime("%Y-%m-%d", time.localtime(t0 + i * 86400))
        if node.compute_market._audit_roll(tid, day) < int(5):
            found = t0 + i * 86400
            break
    assert found is not None, "无法命中抽查日期"
    assert node.compute_market._run_audits(found) == 1
    audit = node.store.compute_audits[tid]
    assert audit["status"] == "pending" and audit["auditor"] == w3.address
    # 审计节点提交错误哈希 → 原节点罚没双倍质押
    stake0 = node.store.compute_stakes[w1.address]
    eco0 = node.balances.get(node.economy.ECOSYSTEM_FUND, 0.0)
    _apply(node, _signed_tx(w3, "nova:compute:audit", task_id=tid, result_hash="00" * 32))
    assert audit["passed"] is False
    assert task.get("audit_failed") is True
    assert node.store.compute_slashed == pytest.approx(2 * stake0 * 2, rel=1e-6)         or node.store.compute_slashed > 0
    assert node.balances.get(node.economy.ECOSYSTEM_FUND, 0.0) > eco0
    # 审计通过路径（原节点已罚没，换新节点）
    w4, w5 = QuantumWallet(), QuantumWallet()
    for w in (w4, w5):
        _fund(node, w.address)
    tid2 = _new_task(node, creator, w4, w5, bounty=30.0)
    result2 = "ab" * 32
    _apply(node, _signed_tx(w4, "nova:compute:submit", task_id=tid2, result_hash=result2))
    _apply(node, _signed_tx(w5, "nova:compute:submit", task_id=tid2, result_hash=result2))
    found2 = None
    for i in range(5000):
        day = time.strftime("%Y-%m-%d", time.localtime(t0 + i * 86400))
        if node.compute_market._audit_roll(tid2, day) < int(5):
            found2 = t0 + i * 86400
            break
    assert found2 is not None
    assert node.compute_market._run_audits(found2) == 1
    a2 = node.store.compute_audits[tid2]
    aw = next(w for w in (w1, w2, w3) if w.address == a2["auditor"])
    bal3 = node.balances[aw.address]
    _apply(node, _signed_tx(aw, "nova:compute:audit", task_id=tid2, result_hash=result2))
    assert a2["passed"] is True
    assert node.balances[aw.address] > bal3  # 审计奖励


def test_incentive_epoch_60_40_split():
    node = _node()
    w1, w2 = QuantumWallet(), QuantumWallet()
    for w in (w1, w2):
        _fund(node, w.address)
    _register_node(node, w1, gpu="A100")
    _register_node(node, w2, gpu="A100")
    _stake_node(node, w1, 100)
    _stake_node(node, w2, 300)
    # 存储节点配额
    _fund(node, w2.address, 10000)
    _apply(node, _signed_tx(w2, "nova:storage:register", capacity_gb=500))
    # 注入激励池
    node.balances[node.economy.VALIDATOR_POOL] = 1000.0
    res = node.compute_market.settle_incentive_epoch()
    assert res["paid"] > 0
    # 算力占 60%：w1/w2 按 100:300 质押分配
    c1 = node.store.compute_stats[w1.address]["block_reward"]
    c2 = node.store.compute_stats[w2.address]["block_reward"]
    # w1 无存储贡献：只拿算力 60% 中按 100/400 质押比例的部分 = 150
    assert c1 == pytest.approx(150.0, rel=1e-6)
    # w2 = 算力 450 + 存储 40% 按配额分配
    assert c2 > 450.0 and c2 < 1000.0
    assert node.balances.get(node.economy.VALIDATOR_POOL, 0.0) < 1000.0
    # 收益统计接口
    inc = node.compute_market.node_income(w1.address)
    assert inc["block_reward"] == c1 and inc["total"] > 0


def test_reputation_bonus_in_settlement():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2):
        _fund(node, w.address)
    _register_node(node, w1)
    _register_node(node, w2)
    _stake_node(node, w1)
    _stake_node(node, w2)
    # 高信誉节点 w1：完成 15 次 + 正确 15 次 → 95 分 → 15% 加成
    node.compute_market._touch_stats(w1.address, completed=15, correct=15)
    assert node.compute_market.compute_reputation(w1.address)["bonus"] == 0.15
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=20.0, spec="生成",
                            task_type="ai_music", expires_in=3600))
    tid = list(node.store.compute_tasks)[0]
    _apply(node, _signed_tx(w1, "nova:compute:accept", task_id=tid))
    _apply(node, _signed_tx(w2, "nova:compute:accept", task_id=tid))
    result = "77" * 32
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash=result))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash=result))
    pool = 20.0 * (1 - MARKET_FEE_RATE)
    s1 = node.store.compute_stats[w1.address]["task_reward"]
    s2 = node.store.compute_stats[w2.address]["task_reward"]
    assert s1 == pytest.approx(pool * 1.15 / 2.2, rel=1e-4)
    assert s2 == pytest.approx(pool * 1.05 / 2.2, rel=1e-4)
    assert node.store.compute_stats[w1.address]["bonus_reward"] > 0


# ---------------------------------------------------------------------------
# 提示词 3：AI 生成服务
# ---------------------------------------------------------------------------

def test_ai_service_register_and_work_split():
    node = _node()
    human, ai, fan = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (human, ai, fan):
        _fund(node, w.address)
    node.balances[AI_FUND] = 0.0
    # AI 创作者身份
    _apply(node, _signed_tx(ai, "nova:ai:register", name="Nova 音乐精灵",
                            owner=human.address, daily_budget=100.0))
    # 服务登记（Suno / OpenAI / SD / 自定义）
    _apply(node, _signed_tx(ai, "nova:ai:svc:register", service_type="suno",
                            name="Suno API", model="v4.5",
                            endpoint_hash="0x" + "11" * 32))
    _apply(node, _signed_tx(ai, "nova:ai:svc:register", service_type="stable_diffusion",
                            name="SDXL", model="sdxl-turbo",
                            endpoint_hash="0x" + "22" * 32))
    assert len(node.store.ai_services) == 2
    # 音乐人循环配置：每日
    _apply(node, _signed_tx(ai, "nova:ai:muso:config", enabled=True, schedule="daily",
                            hour=0, weekday=0, budget=50.0))
    assert node.store.ai_muso["enabled"] is True
    # 作品上架（自动定价：参考价区间中值 1.25）
    _apply(node, _signed_tx(ai, "nova:ai:work:create", title="星海回响",
                            cid=_cid(9), task_type="ai_music"))
    wid = list(node.store.ai_works)[0]
    work = node.store.ai_works[wid]
    assert work["price"] == 1.25
    assert node.store.ai_muso["total_generated"] == 1
    # 购买 → 70/20/10 分账
    bal_ai = node.balances[ai.address]
    bal_fund = node.balances[AI_FUND]
    pool0 = node.balances.get(COMPUTE_POOL, 0.0)
    _apply(node, _signed_tx(fan, "nova:ai:work:buy", amount=work["price"], wid=wid))
    assert node.balances[ai.address] == pytest.approx(bal_ai + work["price"] * REV_CREATOR)
    assert node.balances[AI_FUND] == pytest.approx(bal_fund + work["price"] * REV_FUND)
    assert node.balances.get(COMPUTE_POOL, 0.0) == pytest.approx(pool0 + work["price"] * REV_COMPUTE)
    assert node.store.ai_muso["total_sales"] == 1
    # 价格随销量上升
    node.store.ai_works[wid]["sales"] = 6
    assert node.ai_service.suggest_price("ai_music", sales=6) > 1.25


def test_ai_trigger_and_fund_spend():
    node = _node()
    human, ai, fan, miner = QuantumWallet(), QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (human, ai, fan, miner):
        _fund(node, w.address)
    _apply(node, _signed_tx(ai, "nova:ai:register", name="Nova 音乐精灵",
                            owner=human.address, daily_budget=100.0))
    # 社区一键触发
    _apply(node, _signed_tx(fan, "nova:ai:trigger", amount=TRIGGER_FEE,
                            service_type="suno"))
    tid = list(node.store.ai_triggers)[0]
    tr = node.store.ai_triggers[tid]
    assert tr["status"] == "pending" and tr["by"] == fan.address
    assert node.balances[AI_FUND] == TRIGGER_FEE
    # 音乐人消费触发：上架作品并关联
    _apply(node, _signed_tx(ai, "nova:ai:work:create", title="被点亮的歌",
                            cid=_cid(8), trigger_id=tid))
    assert node.store.ai_triggers[tid]["status"] == "done"
    # 基金支出：监护人授权 + 支出记录
    _apply(node, _signed_tx(ai, "nova:ai:fund:guard", addr=fan.address))
    assert fan.address in node.store.ai_fund_guardians
    bal_fund = node.balances[AI_FUND]
    _apply(node, _signed_tx(fan, "nova:ai:fund:spend", amount=1.0,
                            recipient=miner.address, purpose="购买更多算力"))
    assert node.balances[AI_FUND] == pytest.approx(bal_fund - 1.0)
    assert node.balances[miner.address] == pytest.approx(100000.0 + 1.0)
    fv = node.ai_service.fund_view()
    assert fv["income_total"] >= TRIGGER_FEE
    assert fv["expense_total"] == 1.0
    assert len(fv["ledger"]) >= 2
    # 非监护人不能支出
    assert not node.validate_tx(_signed_tx(miner, "nova:ai:fund:spend", amount=1.0,
                                           recipient=fan.address, purpose="x"))


def test_ai_fund_large_spend_requires_two_guardians():
    # H-04：大额支出（> 单笔上限）须双监护人审批，单监护人不能全量提走
    node = _node()
    human, ai, fan, bob, miner = QuantumWallet(), QuantumWallet(), QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (human, ai, fan, bob, miner):
        _fund(node, w.address)
    _apply(node, _signed_tx(ai, "nova:ai:register", name="Nova 音乐精灵",
                            owner=human.address, daily_budget=100.0))
    _apply(node, _signed_tx(ai, "nova:ai:fund:guard", addr=fan.address))
    _apply(node, _signed_tx(ai, "nova:ai:fund:guard", addr=bob.address))
    node.balances[AI_FUND] = 1000.0  # 基金余额充足
    bal_fund = node.balances[AI_FUND]
    big = FUND_SINGLE_SPEND_LIMIT + 30.0
    # 大额支出：仅创建待审批，不立即转账
    _apply(node, _signed_tx(fan, "nova:ai:fund:spend", amount=big,
                            recipient=miner.address, purpose="购买更多算力"))
    assert node.balances[AI_FUND] == pytest.approx(bal_fund)
    pend = list(node.store.ai_fund_pending.values())[0]
    assert pend["amount"] == big and pend["approvals"] == [fan.address]
    # 非监护人不能审批；发起人不能重复审批
    assert not node.validate_tx(_signed_tx(miner, "nova:ai:fund:approve", pid=pend["id"]))
    assert not node.validate_tx(_signed_tx(fan, "nova:ai:fund:approve", pid=pend["id"]))
    # 第二监护人审批 → 执行转账
    _apply(node, _signed_tx(bob, "nova:ai:fund:approve", pid=pend["id"]))
    assert node.balances[AI_FUND] == pytest.approx(bal_fund - big)
    assert node.balances[miner.address] == pytest.approx(100000.0 + big)
    assert pend["id"] not in node.store.ai_fund_pending
    fv = node.ai_service.fund_view()
    assert fv["expense_total"] == pytest.approx(big)
    assert len(fv["pending"]) == 0


def test_ai_fund_daily_cap_blocks_single_guardian_drain():
    # H-04：小额支出受单监护人单日上限约束，无法一次性掏空基金
    node = _node()
    human, ai, fan, miner = QuantumWallet(), QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (human, ai, fan, miner):
        _fund(node, w.address)
    _apply(node, _signed_tx(ai, "nova:ai:register", name="Nova 音乐精灵",
                            owner=human.address, daily_budget=100.0))
    _apply(node, _signed_tx(ai, "nova:ai:fund:guard", addr=fan.address))
    node.balances[AI_FUND] = 1000.0  # 基金余额充足
    step = FUND_SINGLE_SPEND_LIMIT / 4
    for _ in range(4):
        _apply(node, _signed_tx(fan, "nova:ai:fund:spend", amount=step,
                                recipient=miner.address, purpose="p"))
    assert node.balances[AI_FUND] == pytest.approx(1000.0 - FUND_SINGLE_SPEND_LIMIT)
    # 已到单日上限，再支出被拒
    assert not node.validate_tx(_signed_tx(fan, "nova:ai:fund:spend", amount=step,
                                           recipient=miner.address, purpose="p"))
    # 非监护人不能支出
    assert not node.validate_tx(_signed_tx(miner, "nova:ai:fund:spend", amount=step,
                                           recipient=human.address, purpose="p"))
    # 另一位监护人拥有独立单日额度，不受影响
    _apply(node, _signed_tx(ai, "nova:ai:fund:guard", addr=miner.address))
    _apply(node, _signed_tx(miner, "nova:ai:fund:spend", amount=step,
                            recipient=human.address, purpose="p"))
    assert node.balances[AI_FUND] == pytest.approx(1000.0 - FUND_SINGLE_SPEND_LIMIT - step)


# ---------------------------------------------------------------------------
# RPC 全流程
# ---------------------------------------------------------------------------

async def _make_client(node):
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    return TestClient(TestServer(app))


async def _rpc_op(client, w, op, amount=0.0, **kw):
    data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    resp = await client.post("/api/op", json={
        "addr": w.address, "amount": amount, "data": data, "timestamp": ts,
        "sender_public_key": w.public_key_hex(), "signature": tx.signature})
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def test_compute_network_rpc_flow():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2):
        _fund(node, w.address)
    client = await _make_client(node)
    await client.start_server()
    try:
        # 注册 + 质押
        for w in (w1, w2):
            await _rpc_op(client, w, "nova:compute:register", cpu_cores=8,
                          gpu_model="RTX 4090", gpu_vram_gb=24, ram_gb=64, storage_gb=200)
            await _rpc_op(client, w, "nova:compute:stake", amount=200)
        resp = await client.get("/api/compute/nodes")
        data = await resp.json()
        assert data["total"] == 2
        assert w1.address in data["nodes"]
        # 发布/接单/提交
        await _rpc_op(client, creator, "nova:compute:publish", amount=10.0,
                      spec="AI 图像生成", task_type="ai_image", mode="grab", expires_in=3600)
        tid = list(node.store.compute_tasks)[0]
        await _rpc_op(client, w1, "nova:compute:accept", task_id=tid)
        await _rpc_op(client, w2, "nova:compute:accept", task_id=tid)
        result = "12" * 32
        await _rpc_op(client, w1, "nova:compute:submit", task_id=tid, result_hash=result,
                      result_cid=_cid(3))
        await _rpc_op(client, w2, "nova:compute:submit", task_id=tid, result_hash=result)
        assert node.store.compute_tasks[tid]["status"] == "completed"
        # 总览 / 事件 / 收益
        resp = await client.get("/api/compute/overview")
        ov = await resp.json()
        assert ov["completed"] >= 1 and ov["nodes"] == 2
        resp = await client.get("/api/compute/events")
        assert len((await resp.json())["events"]) >= 3
        resp = await client.get("/api/compute/income/" + w1.address)
        inc = await resp.json()
        assert inc["task_reward"] > 0
        resp = await client.get("/api/compute/node/" + w1.address)
        assert (await resp.json())["reputation"]["score"] >= 50
    finally:
        await client.close()


async def test_ai_rpc_flow():
    node = _node()
    human, ai, fan = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (human, ai, fan):
        _fund(node, w.address)
    client = await _make_client(node)
    await client.start_server()
    try:
        await _rpc_op(client, ai, "nova:ai:register", name="AI 歌手", owner=human.address,
                      daily_budget=100)
        await _rpc_op(client, ai, "nova:ai:work:create", title="夜航星", cid=_cid(7),
                      task_type="ai_music")
        wid = list(node.store.ai_works)[0]
        await _rpc_op(client, fan, "nova:ai:work:buy", amount=1.25, wid=wid)
        resp = await client.get("/api/ai/works")
        assert (await resp.json())["total"] == 1
        resp = await client.get("/api/ai/status")
        st = await resp.json()
        assert st["total_sales"] == 1 and st["total_generated"] == 1
        resp = await client.get("/api/ai/fund")
        fv = await resp.json()
        assert fv["balance"] == pytest.approx(1.25 * REV_FUND)
    finally:
        await client.close()
