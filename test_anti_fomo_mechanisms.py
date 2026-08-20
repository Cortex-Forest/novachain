# -*- coding: utf-8 -*-
"""v0.9 无感机制综合测试：
1. 无感反 FOMO（买入冷却 / 温和提示 / 不影响转账娱乐）
2. 无感动态手续费（大额转账 ×100 / 高频合约调用 ×10 / 费率表）
3. 无感质押过热保护（档位权重 / 暂停新质押 / 分层质押不追减）
4. 无感内容质量守护（曝光档位 / 策展投票 / 仅主页展示）
5. 无感系统负载自适应（负载档位 / 重操作排队 / 灾难扩容激励）
（跨链大额保护见 test_bridge.py）
"""
import asyncio
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from nexlang_compiler import NexLangCompiler
from nova_node import NovaNode
from core.vm import deploy_address


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9975)
    kw.setdefault("rpc", 8327)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


def _fund_eco(node, amt=1000000.0):
    node.balances[node.economy.ECOSYSTEM_FUND] = amt


def _signed_tx(w, op, amount=0.0, **kw):
    payload = {"op": op}
    if amount:
        payload["amount"] = amount
    payload.update(kw)
    data = json.dumps(payload, ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _transfer(w, to, amt):
    ts = int(time.time())
    tx = Tx(w.address, to, amt, [], "", w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _apply(node, tx):
    assert node.validate_tx(tx), "validate failed: " + tx.data[:80]
    node.apply_tx(tx)


def _day():
    return time.strftime("%Y-%m-%d", time.gmtime())


# ---------------------------------------------------------------------------
# 1. 无感反 FOMO
# ---------------------------------------------------------------------------
def test_fomo_24h_and_7d_trigger_unit():
    node = _node()
    w = QuantumWallet()
    now = int(time.time())

    def buy(amount, ts):
        d = json.dumps({"op": "nova:fan:buy", "tid": "t"})
        tx = Tx(w.address, w.address, amount, [], d, w.public_key_hex(), "", timestamp=ts)
        tx.signature = w.sign(tx.signing_data())
        return tx

    # 单笔 5 万：24h 与 7d 窗口均不超阈值
    node.fomo.record_buy(buy(50000, now - 86400))
    assert not node.fomo.in_cooldown(w.address)
    # 24h 内再买 5 万：24h 累计 10 万（不超）、7d 累计 10 万（不超）
    node.fomo.record_buy(buy(50000, now - 86400))
    assert not node.fomo.in_cooldown(w.address)
    # 再买 1 万：24h 累计 11 万 > 10 万 -> 触发 24h 冷却
    node.fomo.record_buy(buy(10000, now))
    assert node.fomo.in_cooldown(w.address)
    left = node.fomo.cooldown_left(w.address)
    assert 0 < left <= 24 * 3600
    # 冷却期内买入（交易时间戳在冷却期内）被拒绝
    assert not node.fomo.validate_buy(buy(1, now))
    # 冷却期内再记录：不延长（冷却期固定 24h）
    node.fomo.record_buy(buy(10000, now))
    assert node.fomo.cooldown_left(w.address) == pytest.approx(left, abs=1.0)


def test_fomo_integration_buy_cooldown_and_gentle_rejection():
    node = _node()
    creator = QuantumWallet()
    buyer = QuantumWallet()
    _fund(node, creator.address)
    _fund(node, buyer.address, 1_000_000)
    _apply(node, _signed_tx(creator, "nova:fan:issue", symbol="FOMO", name="Fomo",
                            supply=100_000_000, price=0.01))
    tid = next(reversed(node.store.fan_tokens))
    # 一次 15 万 NOVA 买入（24h 窗口 >10 万）→ 触发 24h 冷却
    cost = node.socialfi.fan_price_at(tid, 15_000_000)
    _apply(node, _signed_tx(buyer, "nova:fan:buy", tid=tid, qty=15_000_000, amount=cost))
    assert node.fomo.in_cooldown(buyer.address)
    # 冷却期内再次买入被拒
    cost2 = node.socialfi.fan_price_at(tid, 1)
    bad = _signed_tx(buyer, "nova:fan:buy", tid=tid, qty=1, amount=cost2)
    assert not node.validate_tx(bad)
    # 冷却期内普通转账、娱乐（非买入）不受影响
    other = QuantumWallet()
    _fund(node, other.address)
    assert node.validate_tx(_transfer(buyer, other.address, 1.0))
    assert node.validate_tx(_signed_tx(buyer, "nova:graph:post", content="hello"))
    # 温和提示文案
    st = node.fomo.status(buyer.address)
    assert st["cooldown"] is True and "买入功能" in st["message"]
    # 不影响信誉分
    assert "score" in node.socialfi.reputation(buyer.address)


# ---------------------------------------------------------------------------
# 2. 无感动态手续费
# ---------------------------------------------------------------------------
def test_dynamic_fee_large_transfer_x100():
    node = _node()
    a = QuantumWallet()
    b = QuantumWallet()
    _fund(node, a.address)
    _fund(node, b.address)
    small = _transfer(a, b.address, 50000.0)
    large = _transfer(a, b.address, 200000.0)   # > 10 万
    assert node.gas_for(small) == pytest.approx(0.000001)
    assert node.gas_for(large) == pytest.approx(0.000001 * 100)


def test_dynamic_fee_high_freq_contract_call_x10():
    node = _node()
    caller = QuantumWallet()
    _fund(node, caller.address, 1_000_000)
    bytecode = "let a = 1; return a;"
    addr = deploy_address(bytecode)
    node.store.contracts[addr] = bytecode
    node.store.contract_code[addr] = NexLangCompiler().compile(bytecode)
    ts = int(time.time())
    tx = Tx(caller.address, addr, 0, [], "call", caller.public_key_hex(), "", timestamp=ts)
    tx.signature = caller.sign(tx.signing_data())
    # 前 1000 次：1x
    assert node.gas_for(tx) == pytest.approx(0.000001)
    # 第 1001 次起：10x（当日已 1000 次）
    node.store.contract_call_daily[caller.address] = {_day(): 1000}
    assert node.gas_for(tx) == pytest.approx(0.000001 * 10)
    # 娱乐消费 op 永远 1x
    fan = QuantumWallet()
    _fund(node, fan.address)
    op = _signed_tx(fan, "nova:fan:issue", symbol="AAA", name="A", supply=10, price=1.0)
    assert node.gas_for(op) == pytest.approx(0.000001)


def test_dynamic_fee_apply_deduction_matches_validate():
    """审计 M-14：动态手续费在 validate 与 apply 扣费一致。"""
    node = _node()
    a = QuantumWallet()
    b = QuantumWallet()
    node.balances[a.address] = 300000.0
    node.balances[b.address] = 0.0
    tx = _transfer(a, b.address, 200000.0)
    assert node.validate_tx(tx)
    bal_before = node.balances[a.address]
    node.apply_tx(tx)
    assert node.balances[a.address] == pytest.approx(bal_before - (200000.0 + 0.000001 * 100))


# ---------------------------------------------------------------------------
# 3. 无感质押过热保护
# ---------------------------------------------------------------------------
def test_stake_protect_tiers():
    node = _node()
    eco = node.economy
    T = eco.TOTAL_SUPPLY
    # 正常：质押 1/3 * T = 2700 万 -> ratio 0.5 -> warm（新质押 ×0.8）
    node.store.stakes.update({"a": T / 3.0})
    tier, weight, paused = eco.stake_tier()
    assert (tier, weight, paused) == ("warm", 0.8, False)
    # 70% 档：ratio 0.7 -> hot（×0.5）
    node.store.stakes.update({"a": 0.7 * T / 1.7})
    tier, weight, paused = eco.stake_tier()
    assert (tier, weight, paused) == ("hot", 0.5, False)
    # 80%：暂停新质押
    node.store.stakes.update({"a": 0.8 * T / 1.8})
    tier, weight, paused = eco.stake_tier()
    assert (tier, weight, paused) == ("paused", 0.0, True)
    assert eco.circulating() > 0


def test_stake_protect_pause_rejects_new_stake():
    node = _node()
    eco = node.economy
    node.store.stakes.update({"a": 0.8 * eco.TOTAL_SUPPLY / 1.8})
    w = QuantumWallet()
    _fund(node, w.address, 1_000_000)

    def stake_tx(data, amt):
        ts = int(time.time())
        tx = Tx(w.address, w.address, amt, [], data, w.public_key_hex(), "", timestamp=ts)
        tx.signature = w.sign(tx.signing_data())
        return tx

    assert not node.validate_tx(stake_tx("nova:stake", 100.0))   # 暂停新质押
    assert not node.validate_tx(stake_tx("nova:unstake", 1.0))   # 无质押不可解
    # 正常档位下可质押
    node.store.stakes.pop("a", None)
    assert node.validate_tx(stake_tx("nova:stake", 100.0))


def test_stake_protect_layer_weight_no_retroactive():
    node = _node()
    eco = node.economy
    # 老质押（正常期入账）权重 1.0，不追减
    node.store.stakes["old"] = 5000.0
    node.store.stake_layers["old"] = [[time.time() - 999999, 5000.0, 1.0]]
    # 过热期新增层权重 0.5
    node.store.stakes["new"] = 4000.0
    node.store.stake_layers["new"] = [[time.time(), 4000.0, 0.5]]
    assert eco.effective_stake("old") == pytest.approx(5000.0)
    assert eco.effective_stake("new") == pytest.approx(2000.0)
    # 无分层旧状态兼容
    node.store.stakes["legacy"] = 100.0
    assert eco.effective_stake("legacy") == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 4. 无感内容质量守护
# ---------------------------------------------------------------------------
def test_content_exposure_tiers_and_reasons():
    node = _node()
    fresh = QuantumWallet()
    exp = node.socialfi.exposure(fresh.address)
    assert exp["tier"] == "rising" and exp["weight"] == 0.2 and exp["reasons"]
    # 败诉投诉 >=3 -> restricted（仅主页展示）
    now = time.time()
    for i in range(3):
        node.store.arb_cases[f"c{i}"] = {"seller": fresh.address, "buyer": "0x" + "0" * 40,
                                          "result": "buyer", "filed_at": now - 1000}
    exp = node.socialfi.exposure(fresh.address)
    assert exp["tier"] == "restricted" and exp["weight"] == 0.05
    assert "败诉投诉" in exp["reasons"][1]


def test_content_curate_vote_promotes_rising():
    node = _node()
    creator = QuantumWallet()          # 低信誉创作者（rep 0 -> rising）
    _fund(node, creator.address)
    _apply(node, _signed_tx(creator, "nova:graph:post", content="新作品"))
    pid = next(reversed(node.store.graph_posts))
    assert node.socialfi.exposure(creator.address)["tier"] == "rising"
    # 3 位策展人（质押 >=100）投票
    curators = [QuantumWallet() for _ in range(3)]
    for c in curators:
        _fund(node, c.address)
        node.store.stakes[c.address] = 200.0      # 满足策展人资格（质押 >=100）
    for c in curators:
        v = _signed_tx(c, "nova:curate:vote", target=pid)
        assert node.validate_tx(v), "curate vote validate failed"
        node.apply_tx(v)
    assert pid in node.store.curate_passed
    # 上架后进入推荐池
    feed = node.socialfi.content_feed("hot", 100)
    assert any(x["id"] == pid for x in feed)


def test_content_feed_excludes_restricted():
    node = _node()
    low = QuantumWallet()
    _fund(node, low.address)
    _apply(node, _signed_tx(low, "nova:graph:post", content="内容"))
    pid = next(reversed(node.store.graph_posts))
    now = time.time()
    for i in range(3):
        node.store.arb_cases[f"r{i}"] = {"seller": low.address, "buyer": "0x" + "1" * 40,
                                          "result": "buyer", "filed_at": now - 1000}
    assert node.socialfi.exposure(low.address)["tier"] == "restricted"
    feed = node.socialfi.content_feed("normal", 100)
    assert all(x["id"] != pid for x in feed)     # 仅主页展示，不进公共推荐


# ---------------------------------------------------------------------------
# 5. 无感系统负载自适应
# ---------------------------------------------------------------------------
def test_load_tier_and_heavy_queue():
    node = _node()
    eco = node.economy
    node.store.daily_tx_count[_day()] = 15_000_000
    assert eco.load_tier() == 1
    assert node._load_delay() == 60
    node.store.daily_tx_count[_day()] = 60_000_000
    assert eco.load_tier() == 2
    assert node._load_delay() == 300
    # 重操作排队（不拒绝）
    w = QuantumWallet()
    _fund(node, w.address)
    heavy = _signed_tx(w, "nova:fan:issue", symbol="ZZZ", name="Z", supply=10, price=1.0)
    queued, eta, delay = node._submit_heavy(heavy)
    assert queued and delay == 300
    # 普通档位：即时
    node.store.daily_tx_count.pop(_day(), None)
    queued, eta, delay = node._submit_heavy(heavy)
    assert not queued


def test_load_disaster_scale_boost():
    node = _node()
    eco = node.economy
    base = eco.INIT_REWARD / (2 ** min(int((time.time() - eco.GENESIS_TIME) // eco.HALVING), eco.MAX_HALVINGS))
    node.store.daily_tx_count[_day()] = 150_000_000
    assert eco.load_tier() == 3
    assert eco.disaster_load() is True
    assert eco.block_reward() == pytest.approx(base * 2.0)   # 灾难负载出块奖励 ×2（非增发）


def test_rpc_handlers_exist():
    """新增查询接口可被路由注册（方法存在性）。"""
    node = _node()
    for name in ("rpc_fomo_status", "rpc_fees", "rpc_stake_protect",
                 "rpc_content_exposure", "rpc_content_feed", "rpc_load"):
        assert callable(getattr(node, name, None)), name
