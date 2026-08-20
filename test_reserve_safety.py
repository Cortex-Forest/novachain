# -*- coding: utf-8 -*-
"""v0.10 储备金与经济安全网测试：
① 储备金自动回购 / 价格托底 / 质押冻结 / 逆风补偿
② 全网动态手续费档位 / 忠诚者徽章 / 逆风期奖励翻倍
③ 最低节点保障 / 紧急招募 / 种子基金
④ 事故赔付 / 紧急冻结 / 链上公告
⑤ 自动补血 / 减支 / 重新起航
"""
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from nova_node import NovaNode


class FakeOracle:
    """模拟预言机：固定 NOVA/USDT 价格。"""
    def __init__(self, price):
        self._p = price

    def price(self, feed):
        return self._p if feed == "NOVA/USDT" else None


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9978)
    kw.setdefault("rpc", 8330)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


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


def _stake_tx(w, data, amt):
    ts = int(time.time())
    tx = Tx(w.address, w.address, amt, [], data, w.public_key_hex(), "", timestamp=ts)
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


def _price_history(node, price, days=7):
    """构造最近 7 天均匀价格历史（均线 = price）。"""
    now = time.time()
    hist = []
    for i in range(days):
        day = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
        hist.append([day, float(price)])
    node.store.price_history["NOVA/USDT"] = hist


# ---------------------------------------------------------------------------
# 0. 资金池初始值注入
# ---------------------------------------------------------------------------
def test_seed_funds():
    node = _node()
    node.reserve.seed_funds()
    assert node.balances[node.economy.RESERVE] == pytest.approx(node.economy.RESERVE_INITIAL)
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(node.economy.ECOSYSTEM_FUND_INITIAL)
    assert node.balances[node.economy.VALIDATOR_POOL] == pytest.approx(node.economy.VALIDATOR_POOL_INITIAL)
    # 幂等：再次调用不覆盖
    node.balances[node.economy.RESERVE] = 500.0
    node.reserve.seed_funds()
    assert node.balances[node.economy.RESERVE] == 500.0


# ---------------------------------------------------------------------------
# ① 储备金自动回购 / 质押冻结 / 逆风补偿
# ---------------------------------------------------------------------------
def test_buyback_tiers():
    node = _node()
    node.reserve.seed_funds()
    # 跌破 30%：价格 0.7 / 均线 1.0（每档独立天，避免单日上限叠加）
    node.reserve.oracle = FakeOracle(0.7)
    _price_history(node, 1.0)
    rec = node.reserve.buyback_check()
    assert rec and rec["ratio"] == pytest.approx(0.01) and "30%" in rec["reason"]
    assert node.balances[node.economy.BUYBACK_DEAD] > 0
    # 跌破 50%：价格 0.5
    node.store.buyback_log.clear()
    node.reserve.oracle = FakeOracle(0.5)
    rec = node.reserve.buyback_check()
    assert rec and rec["ratio"] == pytest.approx(0.02)
    # 跌破 70%：紧急 5%
    node.store.buyback_log.clear()
    node.reserve.oracle = FakeOracle(0.29)
    rec = node.reserve.buyback_check()
    assert rec and rec["ratio"] == pytest.approx(0.05)
    # 单日上限约束：任一天回购总额不超过（初始储备金 1%，随储备金递减只会更低）
    daily = sum(float(e["amount"]) for e in node.store.buyback_log
                if e["day"] == time.strftime("%Y-%m-%d", time.gmtime()))
    assert daily <= node.economy.RESERVE_INITIAL * node.economy.BUYBACK_DAILY_CAP_RATIO + 1e-6
    # 同日第二档被单日上限拦截（返回 None）
    node.reserve.oracle = FakeOracle(0.3)
    assert node.reserve.buyback_check() is None


def test_buyback_paused_by_gov():
    node = _node()
    node.reserve.seed_funds()
    node.reserve.oracle = FakeOracle(0.5)
    _price_history(node, 1.0)
    node.store.gov_params["reserve.buyback_paused"] = True
    assert node.reserve.buyback_check() is None


def test_stake_freeze_on_crash():
    node = _node()
    node.reserve.oracle = FakeOracle(0.4)   # 暴跌 60%
    _price_history(node, 1.0)
    node.reserve._update_stake_freeze()
    assert node.store.stake_freeze_until > time.time()
    # 冻结期内 unstake 拒绝
    w = QuantumWallet()
    _fund(node, w.address)
    node.store.stakes[w.address] = 100.0
    assert not node.validate_tx(_stake_tx(w, "nova:unstake", 50.0))
    # 价格回升（均线 90%）自动解除
    node.reserve.oracle = FakeOracle(0.9)
    node.reserve._update_stake_freeze()
    assert node.store.stake_freeze_until <= time.time()


def test_headwind_compensation():
    node = _node()
    node.store.headwind_pool = 100.0
    node.store.stake_freeze_until = time.time() + 3600
    node.store.stakes["v1"] = 500.0   # 合格
    node.store.stakes["v2"] = 50.0    # 不合格（<100）
    node.reserve.headwind_compensate()
    assert node.balances.get("v1", 0.0) == pytest.approx(1.0)
    assert node.balances.get("v2", 0.0) == 0.0
    # 防重复：同日只发一次
    node.reserve.headwind_compensate()
    assert node.balances.get("v1", 0.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# ② 全网动态手续费档位 / 忠诚者徽章 / 逆风奖励翻倍
# ---------------------------------------------------------------------------
def test_daily_volume_tier_gas():
    node = _node()
    eco = node.economy
    prev = eco._prev_day_key()
    a = QuantumWallet(); b = QuantumWallet()
    _fund(node, a.address); _fund(node, b.address)
    tx = _transfer(a, b.address, 1.0)
    # >100 万：1x
    node.store.daily_tx_count[prev] = 50_000_000
    assert eco.daily_volume_mult() == 1.0
    assert node.gas_for(tx) == pytest.approx(0.000001)
    # 10万-100万：10x
    node.store.daily_tx_count[prev] = 500_000
    assert eco.daily_volume_mult() == 10.0
    assert node.gas_for(tx) == pytest.approx(0.000001 * 10)
    # <10万：100x（逆风期）
    node.store.daily_tx_count[prev] = 5_000
    assert eco.daily_volume_mult() == 100.0
    assert node.gas_for(tx) == pytest.approx(0.000001 * 100)
    # 大额/高频倍率叠乘：逆风 100x + 大额 100x = 10000x
    large = _transfer(a, b.address, 200000.0)
    assert node.gas_for(large) == pytest.approx(0.000001 * 100 * 100)


def test_loyalty_badge_consecutive():
    node = _node()
    eco = node.economy
    node.store.daily_tx_count[eco._prev_day_key()] = 5_000   # 逆风期
    addr = "0x" + "ab" * 20
    today = time.strftime("%Y-%m-%d", time.gmtime())
    base = time.mktime(time.strptime(today, "%Y-%m-%d"))
    dates = set(time.strftime("%Y-%m-%d", time.gmtime(base - i * 86400)) for i in range(30))
    node.store.light_checkin_dates[addr] = dates
    node.store.light_checkins[addr] = 30
    assert node._consecutive_checkin(addr, today, 30)
    # 非连续：中间缺一天
    dates.discard(time.strftime("%Y-%m-%d", time.gmtime(base - 5 * 86400)))
    assert not node._consecutive_checkin(addr, today, 30)


def test_headwind_reward_double():
    node = _node()
    eco = node.economy
    node.store.daily_tx_count[eco._prev_day_key()] = 5_000   # 逆风期
    node.balances[eco.ECOSYSTEM_FUND] = 3_000_000            # 高于安全线，避免减支干扰
    assert eco.in_headwind()
    assert eco.deploy_reward() == pytest.approx(eco.INIT_DEPLOY_REWARD * 2)
    assert eco.call_reward() == pytest.approx(eco.INIT_CALL_REWARD * 2)
    # 逆风结束恢复
    node.store.daily_tx_count[eco._prev_day_key()] = 50_000_000
    assert not eco.in_headwind()
    assert eco.deploy_reward() == pytest.approx(eco.INIT_DEPLOY_REWARD)


# ---------------------------------------------------------------------------
# ③ 最低节点保障 / 紧急招募 / 种子基金
# ---------------------------------------------------------------------------
def test_node_recovery_recruiting():
    node = _node()
    eco = node.economy
    # 无质押（bootstrap）：不算招募
    assert not eco.node_recovery_active()
    # 质押 1 节点（<50）：紧急招募
    node.store.stakes["a"] = 100.0
    assert eco.node_recovery_active()
    assert eco.min_stake() == pytest.approx(50.0)
    normal_reward = eco.INIT_REWARD / (2 ** min(int((time.time() - eco.GENESIS_TIME) // eco.HALVING), eco.MAX_HALVINGS))
    assert eco.block_reward() == pytest.approx(normal_reward * 2)
    # 恢复到 50 节点：招募结束
    for i in range(49):
        node.store.stakes[f"n{i}"] = 100.0
    assert not eco.node_recovery_active()
    assert eco.min_stake() == pytest.approx(100.0)
    assert eco.block_reward() == pytest.approx(normal_reward)


def test_recruit_min_stake_validate():
    node = _node()
    node.store.stakes["a"] = 100.0    # 触发招募（<50）
    w = QuantumWallet()
    _fund(node, w.address)
    # 招募期质押 60（原门槛 100 会拒绝）可通过
    assert node.validate_tx(_stake_tx(w, "nova:stake", 60.0))


def test_seed_fund_and_subsidy():
    node = _node()
    node.reserve.seed_funds()
    node.reserve.seed_fund_init()
    assert node.store.seed_fund > 0
    # 逆风期种子节点补贴
    node.store.daily_tx_count[node.economy._prev_day_key()] = 5_000
    node.store.miner_registry["m1"] = time.time() - 100
    node.reserve.seed_subsidy()
    assert node.balances.get("m1", 0.0) == pytest.approx(node.economy.SEED_SUBSIDY_MONTHLY)


# ---------------------------------------------------------------------------
# ④ 事故赔付 / 紧急冻结 / 链上公告
# ---------------------------------------------------------------------------
def test_payout_fund_and_confirm():
    node = _node()
    node.reserve.seed_funds()
    node.reserve.payout_fund_init()
    assert node.store.payout_fund > 0
    victim = QuantumWallet()
    _fund(node, victim.address)
    po = node.reserve.execute_payout("px-1", victim.address, 100.0, "合约漏洞导致资产损失")
    assert po["amount"] == pytest.approx(80.0)   # 80%
    assert po["status"] == "pending"
    # 受害者签链上确认书后到账（初始 _fund 100000 + 赔付 80 - gas）
    _apply(node, _signed_tx(victim, "nova:payout:accept", payout_id="px-1"))
    assert po["status"] == "confirmed"
    assert node.balances[victim.address] == pytest.approx(100000.0 + 80.0, abs=1e-3)


def test_emergency_freeze_blocks_tx():
    node = _node()
    node.reserve.freeze_target("0x" + "c" * 40, 48)
    assert node.reserve.is_frozen("0x" + "c" * 40)
    w = QuantumWallet()
    _fund(node, w.address)
    node.contracts["0x" + "c" * 40] = "code"
    ts = int(time.time())
    tx = Tx(w.address, "0x" + "c" * 40, 0, [], "call", w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    assert not node.validate_tx(tx)   # 冻结目标交易被拒
    # 模块冻结（op 名）
    node.reserve.freeze_target("nova:bridge:deposit", 1)
    assert not node.validate_tx(_signed_tx(w, "nova:bridge:deposit", amount=1.0))


def test_notice_post_public():
    node = _node()
    n = node.reserve.notice_post("安全事件", "预言机价格源异常", "价格短暂失真",
                                 "已暂停回购并排查", "48 小时内修复", "0xteam")
    assert n and node.store.notices.get(n["id"])["reason"] == "预言机价格源异常"


def test_gov_freeze_payout_proposal_types():
    node = _node()
    w = QuantumWallet()
    _fund(node, w.address, 200000)
    # freeze 提案可发起
    freeze = _signed_tx(w, "nova:gov:propose", ptype="freeze", title="紧急冻结",
                        target="0x" + "d" * 40, hours=48)
    assert node.validate_tx(freeze)
    # payout 提案可发起
    payout = _signed_tx(w, "nova:gov:propose", ptype="payout", title="事故赔付",
                        victim=w.address, loss=100.0)
    assert node.validate_tx(payout)


# ---------------------------------------------------------------------------
# ⑤ 自动补血 / 减支 / 重新起航
# ---------------------------------------------------------------------------
def test_refill_check():
    node = _node()
    node.reserve.seed_funds()
    eco = node.economy
    node.balances[eco.ECOSYSTEM_FUND] = 100_000.0   # 低于安全线
    r0 = node.balances[eco.RESERVE]
    node.reserve.refill_check()
    assert node.balances[eco.ECOSYSTEM_FUND] > 100_000.0
    assert node.balances[eco.RESERVE] < r0
    # 储备金低于安全线：补血暂停
    node.balances[eco.RESERVE] = 100_000.0
    node.balances[eco.ECOSYSTEM_FUND] = 100_000.0
    node.reserve.refill_check()
    assert node.balances[eco.ECOSYSTEM_FUND] == pytest.approx(100_000.0)


def test_austerity_rewards_floor():
    node = _node()
    eco = node.economy
    node.balances[eco.ECOSYSTEM_FUND] = 100_000.0   # < 安全线
    assert eco.austerity_mode()
    assert eco.deploy_reward() == pytest.approx(eco.MIN_DEPLOY_REWARD)
    assert eco.referral_reward() == pytest.approx(eco.MIN_REFERRAL_REWARD)
    assert eco.call_reward() == pytest.approx(eco.MIN_CALL_REWARD)
    assert eco.light_verify_reward() == 0.0
    # 恢复
    node.balances[eco.ECOSYSTEM_FUND] = 3_000_000.0
    assert not eco.austerity_mode()
    assert eco.light_verify_reward() > 0.0


def test_sail_nft_sale():
    node = _node()
    node.reserve.seed_funds()
    eco = node.economy
    node.balances[eco.ECOSYSTEM_FUND] = 10_000.0
    node.balances[eco.VALIDATOR_POOL] = 10_000.0
    assert eco.sail_active()
    w = QuantumWallet()
    _fund(node, w.address, 1000)
    eco0 = node.balances[eco.ECOSYSTEM_FUND]
    nft = node.reserve.sail_mint(w.address, 100.0)
    assert nft and node.store.sail_sold == 1
    assert node.balances[eco.ECOSYSTEM_FUND] == pytest.approx(eco0 + 100.0)
    # 金额不符拒绝
    assert node.reserve.sail_mint(w.address, 50.0) is None


def test_rpc_handlers_exist():
    node = _node()
    for name in ("rpc_reserve_status", "rpc_node_guard", "rpc_reserve_payouts",
                 "rpc_reserve_freeze", "rpc_reserve_notices", "rpc_reserve_sail",
                 "rpc_reserve_sail_buy", "rpc_loyalty"):
        assert callable(getattr(node, name, None)), name
