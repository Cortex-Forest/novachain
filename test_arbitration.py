# -*- coding: utf-8 -*-
"""链上社区仲裁合约测试：注册与质押 / 社区投票 / 案件流程 / VRF 抽取 /
激励与惩罚 / 防串通与利益回避 / 二次仲裁 / 恶意投诉 / RPC 接口。"""
import json
import time

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from core.transaction import Tx
from network.rpc import setup_routes
from nova_node import NovaNode

DAY = 86400


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9962)
    kw.setdefault("rpc", 8314)
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
    assert node.validate_tx(tx), "validate failed: " + tx.data[:100]
    node.apply_tx(tx)
    node._record_tx(tx)
    return tx


def _age_tx(node, addr, ts):
    """插入一条历史交易，用于地址注册时长（首次链上交易时间）。"""
    key = "hist_" + addr[-10:] + "_" + str(len(node.store.tx_history))
    node.store.tx_history[key] = {
        "sender": addr, "receiver": addr, "amount": 0.1,
        "gas": 0.0, "data": "{}", "ts": ts, "confirmed_at": ts,
    }


def _boost_rep(node, addr):
    """把地址的链上信誉分抬到 80（声誉系统由各组件合成）。"""
    node.store.light_checkins[addr] = 270          # 20 分
    node.store.stakes[addr] = 1000                 # 20 分
    for i in range(3):                              # 部署 3 个合约：15 分
        node.store.contract_creator[f"0x{'c%d' % i * 20}"] = addr
    for i in range(2):                              # 推荐 2 人：10 分
        node.store.referrals[f"0x{'r%d' % i * 20}"] = addr
    node.store.text_reputation[addr] = 500         # 文本创作：15 分
    rep = node.socialfi.reputation(addr)["score"]
    assert rep >= 70, f"信誉分不足: {rep}"


def _apply_candidate(node, wallet, backdate_days=8):
    """申请成为候选仲裁员（质押 500 + 资格校验 + 进入候选池）。"""
    _fund(node, wallet.address)
    _age_tx(node, wallet.address, time.time() - 40 * DAY)
    _boost_rep(node, wallet.address)
    _apply(node, _signed_tx(wallet, "nova:arb:apply", amount=500.0))
    cand = node.store.arb_candidates[wallet.address]
    cand["applied_at"] = time.time() - backdate_days * DAY  # 模拟投票期已过
    return wallet


def _seed_yes_votes(node, cand_addr, yes=160, no=5):
    """直接为候选池注入赞成/反对票（真实投票另有专门测试）。"""
    cand = node.store.arb_candidates[cand_addr]
    cand["votes"]["yes"] = float(yes)
    cand["votes"]["no"] = float(no)
    cand["votes"]["total"] = float(yes + no)
    cand["voted"]["0xseed_voter"] = "yes"


def _elect(node, wallet, backdate_days=8):
    """申请 + 社区投票通过，成为在职仲裁员（返回钱包）。"""
    wallet = _apply_candidate(node, wallet, backdate_days)
    _reg(wallet)
    _seed_yes_votes(node, wallet.address)
    node.arbitration.maintain()
    ar = node.store.arb_arbitrators.get(wallet.address)
    assert ar and ar["status"] == "active", "候选投票未通过"
    return wallet


def _elect_many(node, n):
    """批量选举 n 名仲裁员（返回钱包列表）。"""
    return [_elect(node, QuantumWallet()) for _ in range(n)]


def _open_case(node, buyer, seller, trade="T-1"):
    """发起投诉：保证金随恶意投诉档位自动确定 + 冻结卖家 2 倍保证金。

    调用方需先给卖家充值足够余额（冻结 = 保证金 x 2）。"""
    deposit = node.arbitration._deposit_for(buyer.address)
    _apply(node, _signed_tx(buyer, "nova:arb:complain", amount=deposit,
                            seller=seller.address, trade_id=trade,
                            reason="商品与描述不符", evidence="0xabcd"))
    cid = max(node.store.arb_cases, key=lambda k: int(k.split("_")[1]))
    return node.store.arb_cases[cid]


def _draw(node, cid):
    w = QuantumWallet()
    _fund(node, w.address)
    _apply(node, _signed_tx(w, "nova:arb:draw", case_id=cid))


def _panel_map(node, cid):
    case = node.store.arb_cases[cid]
    return {num: addr for num, addr in case["panel"].items()}


def _vote(node, case_id, addr, number, side, stage=1):
    _apply(node, _signed_tx(_REG[addr], "nova:arb:vote", case_id=case_id,
                            number=str(number), side=side, stage=stage))


_REG = {}


def _reg(wallet):
    _REG[wallet.address] = wallet
    return wallet


# ---------------------------------------------------------------------------
# 1. 申请条件自动校验 + 质押锁定
# ---------------------------------------------------------------------------
def test_apply_conditions_and_stake_lock():
    node = _node()
    _fund_eco(node)
    w = QuantumWallet()
    _fund(node, w.address)

    # 注册时长不足 30 天 -> 拒绝
    _age_tx(node, w.address, time.time() - 5 * DAY)
    _boost_rep(node, w.address)
    assert not node.validate_tx(_signed_tx(w, "nova:arb:apply", amount=500.0))

    # 补齐 30 天注册时长 -> 通过，质押锁定在仲裁合约
    _age_tx(node, w.address, time.time() - 40 * DAY)
    tx = _signed_tx(w, "nova:arb:apply", amount=500.0)
    assert node.validate_tx(tx)
    _apply(node, tx)
    assert w.address in node.store.arb_candidates
    assert node.store.arb_candidates[w.address]["status"] == "voting"
    assert node.store.arb_pools.get(f"cand_{w.address}") == 500.0
    # 重复申请 / 质押金额错误 -> 拒绝
    assert not node.validate_tx(_signed_tx(w, "nova:arb:apply", amount=500.0))
    w2 = QuantumWallet()
    _fund(node, w2.address)
    _age_tx(node, w2.address, time.time() - 40 * DAY)
    _boost_rep(node, w2.address)
    assert not node.validate_tx(_signed_tx(w2, "nova:arb:apply", amount=400.0))


def test_apply_low_rep_and_banned():
    node = _node()
    w = QuantumWallet()
    _fund(node, w.address)
    _age_tx(node, w.address, time.time() - 40 * DAY)
    # 信誉分不足 70 -> 拒绝
    assert not node.validate_tx(_signed_tx(w, "nova:arb:apply", amount=500.0))
    # 被永久取消资格 -> 拒绝
    _boost_rep(node, w.address)
    node.store.arb_banned.add(w.address)
    assert not node.validate_tx(_signed_tx(w, "nova:arb:apply", amount=500.0))


# ---------------------------------------------------------------------------
# 2. 社区投票（1 NOVA = 1 票）
# ---------------------------------------------------------------------------
def test_community_vote_real_voters():
    node = _node()
    _fund_eco(node)
    cand_w = _apply_candidate(node, QuantumWallet(), backdate_days=0)
    cand = cand_w.address
    voters = []
    for _ in range(120):
        v = QuantumWallet()
        _fund(node, v.address, 1.0)
        voters.append(v)
    for v in voters:
        _apply(node, _signed_tx(v, "nova:arb:candidate_vote", candidate=cand, side="yes"))
    c = node.store.arb_candidates[cand]
    assert c["votes"]["yes"] == 120.0
    # 双投拒绝；无余额者拒绝
    assert not node.validate_tx(_signed_tx(voters[0], "nova:arb:candidate_vote",
                                           candidate=cand, side="yes"))
    poor = QuantumWallet()
    _fund(node, poor.address, 0.5)
    assert not node.validate_tx(_signed_tx(poor, "nova:arb:candidate_vote",
                                           candidate=cand, side="yes"))
    # 投票期结束自动统计：赞成 120 > 0（120 > 100）-> 通过
    node.store.arb_candidates[cand]["applied_at"] = time.time() - 8 * DAY
    node.arbitration.maintain()
    ar = node.store.arb_arbitrators[cand]
    assert ar["status"] == "active"
    assert ar["stake"] == 500.0
    assert ar["term_end"] - ar["term_start"] == 90 * DAY


def test_community_vote_fail_refund():
    node = _node()
    _fund_eco(node)
    cand_w = _apply_candidate(node, QuantumWallet(), backdate_days=0)
    cand = cand_w.address
    # 赞成不满足 > 反对 x 1.5 且总票数 > 100 -> 未通过
    _seed_yes_votes(node, cand, yes=60, no=50)
    node.store.arb_candidates[cand]["applied_at"] = time.time() - 8 * DAY
    node.arbitration.maintain()
    assert node.store.arb_candidates[cand]["status"] == "failed"
    # 质押进入 7 天冷静期，到期可领取
    assert cand in node.store.arb_stake_pending
    assert node.store.arb_stake_pending[cand][0] == 500.0
    node.store.arb_stake_pending[cand][1] = time.time() - 1
    before = node.balances[cand]
    _apply(node, _signed_tx(cand_w, "nova:arb:claim_stake"))
    assert node.balances[cand] >= before + 500.0 - 0.000002


# ---------------------------------------------------------------------------
# 3. 案件仲裁流程：投诉 / VRF 抽取 / 投票 / 自动执行
# ---------------------------------------------------------------------------
def test_complain_freezes_seller_funds():
    node = _node()
    _fund_eco(node)
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address)
    _fund(node, seller.address, 100.0)
    before_seller = node.balances[seller.address]
    case = _open_case(node, buyer, seller)
    assert case["status"] == "pending_draw"
    assert case["deposit"] == 10.0
    assert case["seller_frozen"] == 20.0
    assert node.balances[seller.address] == before_seller - 20.0
    # 卖家余额不足 -> 拒绝
    poor_seller = QuantumWallet()
    _fund(node, poor_seller.address, 5.0)
    assert not node.validate_tx(_signed_tx(buyer, "nova:arb:complain", amount=10.0,
                                           seller=poor_seller.address, trade_id="T-X",
                                           reason="测试"))
    # 保证金金额错误 -> 拒绝
    assert not node.validate_tx(_signed_tx(buyer, "nova:arb:complain", amount=5.0,
                                           seller=seller.address, trade_id="T-Y",
                                           reason="测试"))


def test_vrf_draw_excludes_conflicted():
    node = _node()
    _fund_eco(node)
    wallets = _elect_many(node, 6)
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address)
    _fund(node, seller.address)
    conflicted = wallets[0].address
    _age_tx(node, conflicted, time.time() - 10 * DAY)
    node.store.tx_history["tx_transfer"] = {
        "sender": conflicted, "receiver": buyer.address, "amount": 5.0,
        "gas": 0.0, "data": "{}", "ts": time.time() - 3 * DAY, "confirmed_at": 0,
    }
    related = wallets[1].address
    node.store.referrals[buyer.address] = related
    case = _open_case(node, buyer, seller)
    _draw(node, case["id"])
    case = node.store.arb_cases[case["id"]]
    assert case["status"] == "voting"
    panel = list(case["panel"].values())
    assert conflicted not in panel
    assert related not in panel
    assert len(panel) == 3
    # 被抽中仲裁员收到链上通知
    for a in panel:
        kinds = [n["kind"] for n in node.arbitration.notifications(a)]
        assert "arb_drawn" in kinds
    # 抽取结果公开但当事人匿名：公共视角无地址映射
    pub = node.arbitration.case_public(case["id"])
    for row in pub["panel"]:
        assert "addr" not in row


def test_vote_buyer_wins_auto_payout():
    node = _node()
    _fund_eco(node)
    w1, w2, w3 = _elect_many(node, 3)
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address)
    _fund(node, seller.address)
    case = _open_case(node, buyer, seller)
    _draw(node, case["id"])
    cid = case["id"]
    panel = _panel_map(node, cid)
    buyer_bal = node.balances[buyer.address]
    _vote(node, cid, panel["1"], "1", "buyer")
    _vote(node, cid, panel["2"], "2", "buyer")
    _vote(node, cid, panel["3"], "3", "seller")
    case = node.store.arb_cases[cid]
    # 2:1 -> 支持买家，立即执行：冻结保证金赔付 + 保证金退还
    assert case["status"] == "decided"
    assert case["result"] == "buyer"
    assert case["revealed"] is True
    assert node.balances[buyer.address] > buyer_bal + 20.0
    # 仲裁员激励：按时投票 +2 NOVA、与多数一致 +1 信誉分
    # （仲裁员到匿名编号的映射由 VRF 随机分配，按实际投票方断言）
    for num, addr in panel.items():
        ar = node.store.arb_arbitrators[addr]
        assert ar["revenue"] >= 2.0
        if case["votes"][num] == case["result"]:
            assert ar["rep"] > 80.0
        else:
            assert ar["rep"] == 80.0
            assert ar["streak"] == 0
    # 7 天内可发起二次仲裁
    assert case["appeal_deadline"] - time.time() > 6 * DAY


def test_vote_timeout_penalty_and_substitute():
    node = _node()
    _fund_eco(node)
    extra = _elect_many(node, 1)         # 替代来源
    w1, w2, w3 = _elect_many(node, 3)
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address)
    _fund(node, seller.address)
    case = _open_case(node, buyer, seller)
    _draw(node, case["id"])
    cid = case["id"]
    panel = _panel_map(node, cid)
    _vote(node, cid, panel["1"], "1", "buyer")
    _vote(node, cid, panel["2"], "2", "buyer")
    # 3 号超时：把 deadline 拨到过去
    case = node.store.arb_cases[cid]
    case["panel_meta"][panel["3"]]["deadline"] = time.time() - 1
    stake_before = node.store.arb_arbitrators[panel["3"]]["stake"]
    rep_before = node.store.arb_arbitrators[panel["3"]]["rep"]
    node.arbitration.maintain()
    case = node.store.arb_cases[cid]
    # 超时惩罚：-1 NOVA、信誉分 -2
    assert node.store.arb_arbitrators[panel["3"]]["stake"] == stake_before - 1.0
    assert node.store.arb_arbitrators[panel["3"]]["rep"] == rep_before - 2.0
    # 重新抽取替代仲裁员（匿名编号不变）
    assert "3" in case["panel"]
    repl = case["panel"]["3"]
    assert repl != panel["3"]
    assert repl not in (buyer.address, seller.address)
    _vote(node, cid, repl, "3", "seller")
    case = node.store.arb_cases[cid]
    assert case["status"] == "decided"
    assert case["result"] == "buyer"  # 2:1 支持买家（替代仲裁员加入后多数仍为买家）


# ---------------------------------------------------------------------------
# 4. 二次仲裁：推翻时一次仲裁员扣 10 NOVA + 信誉分 -5
# ---------------------------------------------------------------------------
def test_second_arbitration_overturns():
    node = _node()
    _fund_eco(node)
    first_w = _elect_many(node, 3)
    second_w = _elect_many(node, 7)  # 二次仲裁 7 名（排除一次仲裁员）
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address)
    _fund(node, seller.address)
    case = _open_case(node, buyer, seller)
    _draw(node, case["id"])
    cid = case["id"]
    panel = _panel_map(node, cid)
    _vote(node, cid, panel["1"], "1", "seller")
    _vote(node, cid, panel["2"], "2", "seller")
    _vote(node, cid, panel["3"], "3", "buyer")
    case = node.store.arb_cases[cid]
    assert case["result"] == "seller"
    # 买家 7 天内发起二次仲裁（50 NOVA）
    stake_before = {addr: node.store.arb_arbitrators[addr]["stake"] for addr in panel.values()}
    rep_before = {addr: node.store.arb_arbitrators[addr]["rep"] for addr in panel.values()}
    _apply(node, _signed_tx(buyer, "nova:arb:second", amount=50.0, case_id=cid))
    case = node.store.arb_cases[cid]
    assert case["status"] == "second_voting"
    sec = case["second"]
    assert len(sec["panel"]) == 7
    assert not set(panel.values()) & set(sec["panel"].values())  # 二次仲裁排除已抽中的首轮仲裁员
    # 二次仲裁 5:2 支持买家（推翻）
    for num, addr in sec["panel"].items():
        side = "buyer" if int(num) <= 5 else "seller"
        _vote(node, cid, addr, num, side, stage=2)
    case = node.store.arb_cases[cid]
    assert case["status"] == "settled"
    assert sec["result"] == "buyer"
    # 一次仲裁员全部被扣 10 NOVA + 信誉分 -5
    for addr in panel.values():  # 仅实际参与首轮裁决的仲裁员被推翻惩罚
        assert node.store.arb_arbitrators[addr]["stake"] == stake_before[addr] - 10.0
        assert node.store.arb_arbitrators[addr]["rep"] == rep_before[addr] - 5.0
        kinds = [n["kind"] for n in node.arbitration.notifications(addr)]
        assert "arb_overturned" in kinds
    # 上诉人（买家）拿回 50 保证金
    assert node.balances[buyer.address] > 0


# ---------------------------------------------------------------------------
# 5. 激励与惩罚 / 信誉分管理
# ---------------------------------------------------------------------------
def test_rep_suspend_and_reactivate():
    node = _node()
    _fund_eco(node)
    w = _elect_many(node, 1)[0]
    a = w.address
    ar = node.store.arb_arbitrators[a]
    assert ar["rep"] == 80.0
    # 信誉分降至 < 30 -> 暂停资格
    ar["rep"] = 31.0
    node.arbitration._rep_delta(a, -2.0, "测试")
    assert node.store.arb_arbitrators[a]["status"] == "suspended"
    # 暂停期间不可被抽取
    assert a not in node.arbitration._eligible_pool({"buyer": "0x1", "seller": "0x2",
                                                     "excluded": [], "panel": {}})
    # 重新质押激活：信誉分回到 50
    _fund(node, a)
    _apply(node, _signed_tx(w, "nova:arb:reactivate", amount=500.0))
    ar = node.store.arb_arbitrators[a]
    assert ar["status"] == "active"
    assert ar["rep"] == 50.0


def test_rep_zero_banned():
    node = _node()
    _fund_eco(node)
    w = _elect_many(node, 1)[0]
    ar = node.store.arb_arbitrators[w.address]
    ar["rep"] = 3.0
    node.arbitration._rep_delta(w.address, -3.0, "测试归零")
    assert node.store.arb_arbitrators[w.address]["status"] == "banned"
    assert w.address in node.store.arb_banned


# ---------------------------------------------------------------------------
# 6. 防串通：利益回避声明 / 同组重复抽取 / 检举罚没
# ---------------------------------------------------------------------------
def test_decline_conflict_rep_plus_one():
    node = _node()
    _fund_eco(node)
    wallets = _elect_many(node, 4)
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address)
    _fund(node, seller.address)
    case = _open_case(node, buyer, seller)
    _draw(node, case["id"])
    cid = case["id"]
    panel = _panel_map(node, cid)
    decliner = panel["1"]
    rep_before = node.store.arb_arbitrators[decliner]["rep"]
    _apply(node, _signed_tx(_REG[decliner], "nova:arb:decline", case_id=cid))
    case = node.store.arb_cases[cid]
    # 信誉分 +1，替代仲裁员已重新抽取
    assert node.store.arb_arbitrators[decliner]["rep"] == rep_before + 1.0
    assert "1" in case["panel"]
    assert case["panel"]["1"] != decliner
    assert decliner in case["excluded"]


def test_same_panel_repeat_marks_suspicious():
    node = _node()
    _fund_eco(node)
    trio = _elect_many(node, 3)  # 只有 3 名仲裁员，每案必然同组
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address)
    _fund(node, seller.address)
    for i in range(4):
        case = _open_case(node, buyer, seller, trade=f"T-R{i}")
        _draw(node, case["id"])
        cid = case["id"]
        panel = _panel_map(node, cid)
        for num, addr in panel.items():
            _vote(node, cid, addr, num, "buyer")
    node.arbitration.maintain()
    # 同组 30 天被抽 4 次（> 3）-> 标记可疑，进入观察期
    for w in trio:
        assert w.address in node.store.arb_suspicious
        assert node.store.arb_arbitrators[w.address]["status"] == "observing"


def test_charge_bribe_slashing_and_ban():
    node = _node()
    _fund_eco(node)
    target_w = _elect_many(node, 1)[0]
    target = target_w.address
    # 目标已因异常投票被标记可疑，但尚未被独立检举人确认（审计 H-4：未确认不可罚没）
    node.store.arb_suspicious[target] = {"reason": "测试", "marked_at": time.time(),
                                         "observe_until": time.time() + 7 * DAY,
                                         "confirmed": False, "chargers": []}
    node.store.arb_arbitrators[target]["status"] = "observing"
    charger1 = QuantumWallet()
    _fund(node, charger1.address)
    # 单个检举人 → 仅进入观察期，不罚没、不封禁、押金不退还
    _apply(node, _signed_tx(charger1, "nova:arb:charge", amount=2.0,
                            target=target, kind="bribe", evidence="0xbeef"))
    ar = node.store.arb_arbitrators[target]
    assert ar["stake"] > 0.0
    assert ar["status"] == "observing"
    assert target not in node.store.arb_banned
    assert node.store.arb_suspicious[target]["confirmed"] is False
    assert node.balances[charger1.address] < 100000.0 - 1.0  # 押金未退还（扣除 2 NOVA + gas）

    # 第二名独立检举人确认后 → 检举成立：罚没全部质押 + 永久取消资格 + 押金退还
    charger2 = QuantumWallet()
    _fund(node, charger2.address)
    _apply(node, _signed_tx(charger2, "nova:arb:charge", amount=2.0,
                            target=target, kind="bribe", evidence="0xcafe"))
    ar = node.store.arb_arbitrators[target]
    assert ar["stake"] == 0.0
    assert ar["status"] == "banned"
    assert target in node.store.arb_banned
    assert node.balances[charger2.address] > 100000.0 - 3.0  # 成立后退还押金


# ---------------------------------------------------------------------------
# 7. 恶意投诉检测
# ---------------------------------------------------------------------------
def test_malicious_complainant_deposit_and_cipher_lock():
    node = _node()
    _fund_eco(node)
    _elect_many(node, 6)  # 需要仲裁员投票；6 名保证各案面板轮换、不触发同组重复标记
    buyer = QuantumWallet()
    _fund(node, buyer.address)
    for i in range(4):
        seller = QuantumWallet()
        _fund(node, seller.address)
        case = _open_case(node, buyer, seller, trade=f"T-M{i}")
        _draw(node, case["id"])
        cid = case["id"]
        panel = _panel_map(node, cid)
        for num, addr in panel.items():
            _vote(node, cid, addr, num, "seller")
        case = node.store.arb_cases[cid]
        case["status"] = "settled"
        case["decided_at"] = time.time()
    node.arbitration.maintain()
    m = node.store.arb_malicious[buyer.address]
    assert m["loss_count"] == 4
    # 恶意投诉者保证金提高至 50 NOVA
    assert node.arbitration._deposit_for(buyer.address) == 50.0
    # 再败诉 2 次（累计 6 次）-> 连续恶意投诉 5 次，限制密文交易 30 天
    for i in range(2):
        seller = QuantumWallet()
        _fund(node, seller.address)
        case = _open_case(node, buyer, seller, trade=f"T-M5-{i}")
        _draw(node, case["id"])
        cid = case["id"]
        panel = _panel_map(node, cid)
        for num, addr in panel.items():
            _vote(node, cid, addr, num, "seller")
        case = node.store.arb_cases[cid]
        case["status"] = "settled"
        case["decided_at"] = time.time()
    node.arbitration.maintain()
    m = node.store.arb_malicious[buyer.address]
    assert m["lock_until"] > time.time()
    # 密文交易被链上拦截（普通公开文本不受影响）
    assert node.arbitration.cipher_locked(buyer.address, {"op": "nova:text:create",
                                                          "visibility": "sealed"})
    assert not node.arbitration.cipher_locked(buyer.address, {"op": "nova:text:create",
                                                              "visibility": "public"})
    # 锁定期间不可发起新投诉
    assert not node.validate_tx(_signed_tx(buyer, "nova:arb:complain", amount=50.0,
                                           seller=QuantumWallet().address, trade_id="T-LOCK",
                                           reason="测试"))


# ---------------------------------------------------------------------------
# 8. 退出与任期
# ---------------------------------------------------------------------------
def test_exit_notice_cooldown_and_blocked_with_open_case():
    node = _node()
    _fund_eco(node)
    w1, w2, w3 = _elect_many(node, 3)
    # 无未完成案件时可申请退出
    _apply(node, _signed_tx(w1, "nova:arb:exit"))
    ar = node.store.arb_arbitrators[w1.address]
    assert ar["status"] == "leaving"
    assert ar["exit_ready_at"] - time.time() > 6 * DAY
    # 冷静期未到不可领取
    assert not node.validate_tx(_signed_tx(w1, "nova:arb:claim_stake"))
    # 有未完成案件时不可退出
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address)
    _fund(node, seller.address)
    case = _open_case(node, buyer, seller)
    _draw(node, case["id"])
    for addr in _panel_map(node, case["id"]).values():
        assert not node.validate_tx(_signed_tx(_REG[addr], "nova:arb:exit"))
    # 退出声明期满 -> 质押进入冷静期，到期自动返还
    node.store.arb_arbitrators[w1.address]["exit_ready_at"] = time.time() - 1
    before = node.balances[w1.address]
    node.arbitration.maintain()
    assert node.store.arb_arbitrators[w1.address]["status"] == "retired"
    node.store.arb_stake_pending[w1.address][1] = time.time() - 1  # 模拟 7 天冷静期结束
    node.arbitration.maintain()  # 冷静期到期自动返还
    assert node.balances[w1.address] >= before + 500.0


def test_renew_requires_new_community_vote():
    node = _node()
    _fund_eco(node)
    w = _elect_many(node, 1)[0]
    a = w.address
    ar = node.store.arb_arbitrators[a]
    ar["term_end"] = time.time() + 3 * DAY  # 任期结束前 7 天内
    # 申请连任
    _apply(node, _signed_tx(w, "nova:arb:renew"))
    assert a in node.store.arb_candidates
    assert node.store.arb_candidates[a]["kind"] == "renew"
    assert node.store.arb_arbitrators[a]["status"] == "renewing"
    # 连任需重新社区投票：未通过 -> 任期满自动退休
    _seed_yes_votes(node, a, yes=50, no=50)
    node.store.arb_candidates[a]["applied_at"] = time.time() - 8 * DAY
    node.arbitration.maintain()
    assert node.store.arb_arbitrators[a]["status"] == "retired"
    # 任期外不可申请连任
    w2 = _elect_many(node, 1)[0]
    assert not node.validate_tx(_signed_tx(w2, "nova:arb:renew"))
    # 连任通过：任期延长
    w3 = _elect_many(node, 1)[0]
    node.store.arb_arbitrators[w3.address]["term_end"] = time.time() + 3 * DAY
    _apply(node, _signed_tx(w3, "nova:arb:renew"))
    _seed_yes_votes(node, w3.address, yes=150, no=0)
    node.store.arb_candidates[w3.address]["applied_at"] = time.time() - 8 * DAY
    node.arbitration.maintain()
    assert node.store.arb_arbitrators[w3.address]["status"] == "active"
    assert node.store.arb_arbitrators[w3.address]["term_end"] - time.time() > 90 * DAY


# ---------------------------------------------------------------------------
# 9. RPC 接口
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rpc_arbitration_endpoints():
    node = _node()
    _fund_eco(node)
    wallets = _elect_many(node, 3)
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address)
    _fund(node, seller.address)
    case = _open_case(node, buyer, seller)
    _draw(node, case["id"])
    cid = case["id"]
    panel = _panel_map(node, cid)
    for num, addr in panel.items():
        _vote(node, cid, addr, num, "buyer")

    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/api/arb/summary")
        d = await r.json()
        assert d["arbitrators"] >= 3
        assert d["cases"] >= 1
        r = await client.get("/api/arb/arbitrators")
        d = await r.json()
        assert d["total"] >= 3
        assert "rep" in d["arbitrators"][0]
        r = await client.get("/api/arb/cases")
        d = await r.json()
        assert d["total"] >= 1
        r = await client.get(f"/api/arb/cases/{cid}?viewer={buyer.address}")
        d = await r.json()
        assert d["id"] == cid
        assert d["buyer"] == buyer.address
        r = await client.get(f"/api/arb/user/{buyer.address}")
        d = await r.json()
        assert d["complaints"]
        r = await client.get(f"/api/arb/panel/{wallets[0].address}")
        d = await r.json()
        assert d["found"]
        assert "term_remaining_days" in d
        r = await client.get(f"/api/arb/notifications/{wallets[0].address}")
        d = await r.json()
        assert d["notifications"]
        r = await client.post("/api/arb/notifications/read", json={"addr": wallets[0].address})
        d = await r.json()
        assert d["marked"] >= 1
