# -*- coding: utf-8 -*-
"""链上治理测试：提案流程 / 投票权与委托 / 时间锁 / 参数执行 / 基金多签 / 升级绝对多数。"""
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.economy import Economy
from core.transaction import Tx
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9964)
    kw.setdefault("rpc", 8316)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    kw.setdefault("genesis", None)
    return NovaNode(**kw)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


def _signed_tx(w, op, amount=0.0, data=None, **kw):
    payload = {"op": op}
    if data:
        payload.update(data)
    if amount:
        payload["amount"] = amount
    payload.update(kw)
    data = json.dumps(payload, ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _apply(node, tx):
    assert node.validate_tx(tx), "validate failed: " + tx.data[:100]
    node.apply_tx(tx)


def _propose(node, w, **kw):
    _apply(node, _signed_tx(w, "nova:gov:propose", **kw))
    return next(reversed(node.store.gov_proposals))


def _to_voting(node, pid):
    p = node.governance.proposal(pid)
    p["discussion_end"] = time.time() - 1
    node.governance.tick()
    assert p["status"] == "voting"
    return p


def _resolve(node, pid):
    p = node.governance.proposal(pid)
    p["vote_end"] = time.time() - 1
    node.governance.tick()
    return p


# ---------------------------------------------------------------------------
# 1. 参数提案全流程（公示 -> 投票 -> 时间锁 -> 执行）
# ---------------------------------------------------------------------------
def test_param_proposal_flow():
    node = _node()
    proposer = QuantumWallet()
    _fund(node, proposer.address, 200000)
    old = Economy.FIXED_GAS
    try:
        pid = _propose(node, proposer, ptype="param", title="降低交易手续费",
                       description="将固定 Gas 调整为 5e-7", target="economy",
                       key="FIXED_GAS", value=0.0000005)
        p = node.governance.proposal(pid)
        assert p["status"] == "discussion" and p["proposer_ok"]
        _to_voting(node, pid)
        _apply(node, _signed_tx(proposer, "nova:gov:vote", proposal_id=pid, support=True))
        _resolve(node, pid)
        assert p["status"] == "passed"
        assert p["for_votes"] >= node.governance.circulating_supply() * 0.1
        # 时间锁 48h 未到不能执行
        bad = _signed_tx(proposer, "nova:gov:execute", proposal_id=pid)
        assert not node.validate_tx(bad)
        p["timelock_end"] = time.time() - 1
        _apply(node, _signed_tx(proposer, "nova:gov:execute", proposal_id=pid))
        assert p["status"] == "executed"
        assert node.economy.FIXED_GAS == pytest.approx(0.0000005)
    finally:
        Economy.FIXED_GAS = old


# ---------------------------------------------------------------------------
# 2. 社区联署发起 + 委托投票权
# ---------------------------------------------------------------------------
def test_endorsement_and_delegation():
    node = _node()
    proposer = QuantumWallet()
    _fund(node, proposer.address, 500)   # 权益不足 1000
    pid = _propose(node, proposer, ptype="arb", title="仲裁员数量", key="panel_size", value=5)
    p = node.governance.proposal(pid)
    assert not p["proposer_ok"]
    for _ in range(100):
        w = QuantumWallet()
        _fund(node, w.address, 100)
        _apply(node, _signed_tx(w, "nova:gov:endorse", proposal_id=pid))
    assert len(node.store.gov_endorsements[pid]) == 100
    _to_voting(node, pid)
    # 委托：a 的 5000 票委托给 b
    a = QuantumWallet()
    _fund(node, a.address, 5000)
    b = QuantumWallet()
    _fund(node, b.address, 3000)
    _apply(node, _signed_tx(a, "nova:gov:delegate", to=b.address))
    assert node.governance.voting_power(b.address) == pytest.approx(8000.0)


# ---------------------------------------------------------------------------
# 3. 协议升级：2/3 绝对多数
# ---------------------------------------------------------------------------
def test_upgrade_requires_supermajority():
    node = _node()
    proposer = QuantumWallet()
    _fund(node, proposer.address, 40000)
    v1 = QuantumWallet()
    _fund(node, v1.address, 40000)
    v2 = QuantumWallet()
    _fund(node, v2.address, 40000)
    pid = _propose(node, proposer, ptype="upgrade", title="硬分叉 v2",
                   upgrade_height=100000, content="consensus upgrade")
    p = _to_voting(node, pid)
    _apply(node, _signed_tx(proposer, "nova:gov:vote", proposal_id=pid, support=True))
    _apply(node, _signed_tx(v1, "nova:gov:vote", proposal_id=pid, support=False))
    _apply(node, _signed_tx(v2, "nova:gov:vote", proposal_id=pid, support=False))
    _resolve(node, pid)
    assert p["status"] == "rejected"      # 赞成 1/3 < 2/3
    # 全票通过
    pid2 = _propose(node, proposer, ptype="upgrade", title="硬分叉 v3",
                    upgrade_height=200000, content="consensus upgrade 2")
    p2 = _to_voting(node, pid2)
    _apply(node, _signed_tx(proposer, "nova:gov:vote", proposal_id=pid2, support=True))
    _apply(node, _signed_tx(v1, "nova:gov:vote", proposal_id=pid2, support=True))
    _apply(node, _signed_tx(v2, "nova:gov:vote", proposal_id=pid2, support=True))
    _resolve(node, pid2)
    assert p2["status"] == "passed"
    p2["timelock_end"] = time.time() - 1
    _apply(node, _signed_tx(proposer, "nova:gov:execute", proposal_id=pid2))
    assert node.store.gov_params.get("upgrade.height") == 200000


# ---------------------------------------------------------------------------
# 4. 基金支出：需 3/5 桥节点多签确认
# ---------------------------------------------------------------------------
def test_fund_proposal_multisig():
    node = _node()
    node.balances[node.economy.ECOSYSTEM_FUND] = 100000.0
    nodes = []
    for _ in range(3):
        w = QuantumWallet()
        _fund(node, w.address, 100000)
        _apply(node, _signed_tx(w, "nova:bridge:node:register", amount=1000))
        nodes.append(w)
    proposer = QuantumWallet()
    _fund(node, proposer.address, 500000)
    recipient = QuantumWallet()
    pid = _propose(node, proposer, data={"amount": 1000}, ptype="fund",
                   title="生态基金支出", recipient=recipient.address)
    p = _to_voting(node, pid)
    _apply(node, _signed_tx(proposer, "nova:gov:vote", proposal_id=pid, support=True))
    _resolve(node, pid)
    assert p["status"] == "passed"
    p["timelock_end"] = time.time() - 1
    # 未多签确认不能执行
    bad = _signed_tx(proposer, "nova:gov:execute", proposal_id=pid)
    assert not node.validate_tx(bad)
    for w in nodes:
        _apply(node, _signed_tx(w, "nova:gov:confirm", proposal_id=pid))
    _apply(node, _signed_tx(proposer, "nova:gov:execute", proposal_id=pid))
    assert node.balances[recipient.address] == pytest.approx(1000.0)
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(100000.0 - 1000.0)


# ---------------------------------------------------------------------------
# 5. 治理控制 DEX 暂停 / 跨链桥额度
# ---------------------------------------------------------------------------
def test_gov_controls_dex_and_bridge():
    node = _node()
    node.balances[node.economy.ECOSYSTEM_FUND] = 1000000.0
    proposer = QuantumWallet()
    _fund(node, proposer.address, 300000)
    # DEX 暂停
    pid = _propose(node, proposer, ptype="param", title="暂停 DEX", target="dex",
                   key="paused", value=1)
    p = _to_voting(node, pid)
    _apply(node, _signed_tx(proposer, "nova:gov:vote", proposal_id=pid, support=True))
    _resolve(node, pid)
    p["timelock_end"] = time.time() - 1
    _apply(node, _signed_tx(proposer, "nova:gov:execute", proposal_id=pid))
    assert node.store.dex_paused is True
    # 跨链桥每日额度上限调整
    pid2 = _propose(node, proposer, ptype="param", title="提高桥额度", target="bridge",
                    key="daily_limit_usd", value=2000000)
    p2 = _to_voting(node, pid2)
    _apply(node, _signed_tx(proposer, "nova:gov:vote", proposal_id=pid2, support=True))
    _resolve(node, pid2)
    p2["timelock_end"] = time.time() - 1
    _apply(node, _signed_tx(proposer, "nova:gov:execute", proposal_id=pid2))
    assert node.bridge._daily_limit_usd() == pytest.approx(2000000.0)


# ---------------------------------------------------------------------------
# 审计回归 F-06：委托链不放大投票权（一币一票）
# ---------------------------------------------------------------------------
def test_gov_delegation_chain_no_amplification():
    node = _node()
    a = QuantumWallet()
    _fund(node, a.address, 1000)
    b = QuantumWallet()
    _fund(node, b.address, 1000)
    c = QuantumWallet()
    _fund(node, c.address, 1000)
    _apply(node, _signed_tx(a, "nova:gov:delegate", to=b.address))
    _apply(node, _signed_tx(b, "nova:gov:delegate", to=c.address))
    # 已委托地址不再保留权力，全部转移给最终受托方（c 自身 1000 + b 委托 1000 + a 委托 1000 = 3000）
    assert node.governance.voting_power(a.address) == 0.0
    assert node.governance.voting_power(b.address) == 0.0
    assert node.governance.voting_power(c.address) == pytest.approx(3000.0)
    total = (node.governance.voting_power(a.address)
             + node.governance.voting_power(b.address)
             + node.governance.voting_power(c.address))
    assert total == pytest.approx(3000.0)   # 总票权 = 总供应（1000x3），无放大
    # 委托环：不放大也不死循环（d 500 -> a -> b -> c）
    d = QuantumWallet()
    _fund(node, d.address, 500)
    _apply(node, _signed_tx(d, "nova:gov:delegate", to=a.address))
    assert node.governance.voting_power(c.address) == pytest.approx(3500.0)
    assert node.governance.voting_power(a.address) == 0.0
