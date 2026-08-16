# -*- coding: utf-8 -*-
"""DID 与声誉系统测试：哈希绑定 / 创作者认证 / 声誉分与隐私。"""
import hashlib
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9965)
    kw.setdefault("rpc", 8317)
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


def _apply(node, tx):
    assert node.validate_tx(tx), "validate failed: " + tx.data[:100]
    node.apply_tx(tx)


# ---------------------------------------------------------------------------
# 1. 身份绑定：只存哈希，可撤销，原始数据不上链
# ---------------------------------------------------------------------------
def test_bind_unbind_hash_only():
    node = _node()
    user = QuantumWallet()
    _fund(node, user.address)
    email = "creator@nova.chain"
    h = hashlib.sha3_256(email.encode()).hexdigest()
    _apply(node, _signed_tx(user, "nova:did:bind", kind="email", hash=h, visible=True))
    prof = node.did.profile(user.address, user.address)
    assert "email" in prof["bindings"]
    assert email not in json.dumps(node.store.did_profiles, ensure_ascii=False)
    # 撤销
    _apply(node, _signed_tx(user, "nova:did:unbind", kind="email"))
    assert "email" not in node.did.profile(user.address, user.address)["bindings"]
    # 非法哈希被拒
    bad = _signed_tx(user, "nova:did:bind", kind="email", hash="not-a-hash")
    assert not node.validate_tx(bad)


# ---------------------------------------------------------------------------
# 2. 创作者认证：作品集 + 社区投票 -> 不可转让徽章
# ---------------------------------------------------------------------------
def test_creator_certification():
    node = _node()
    creator = QuantumWallet()
    _fund(node, creator.address)
    contract = "0x" + "aa" * 20
    node.store.contract_creator[contract] = creator.address
    _apply(node, _signed_tx(creator, "nova:did:apply",
                            portfolio=[contract], statement="我的链上作品集"))
    assert node.store.did_applications[creator.address]["status"] == "pending"
    voters = []
    for _ in range(10):
        w = QuantumWallet()
        _fund(node, w.address, 200)
        voters.append(w)
        _apply(node, _signed_tx(w, "nova:did:vote", applicant=creator.address, support=True))
    assert "nova:did:creator" in node.store.did_badges.get(creator.address, [])
    assert creator.address in node.store.soulbound.get("nova:did:creator", [])
    # 已认证不可重复申请
    bad = _signed_tx(creator, "nova:did:apply", portfolio=[contract])
    assert not node.validate_tx(bad)


# ---------------------------------------------------------------------------
# 3. 声誉分：初始 50，四维加权，详情仅本人可见
# ---------------------------------------------------------------------------
def test_reputation_score_and_privacy():
    node = _node()
    user = QuantumWallet()
    _fund(node, user.address, 1.0)              # 近乎无资产 -> 初始约 50 分
    _apply(node, _signed_tx(user, "nova:did:update"))
    r = node.did.reputation(user.address)
    assert 50.0 <= r["score"] < 51.0
    _apply(node, _signed_tx(user, "nova:did:bind", kind="x",
                            hash=hashlib.sha3_256(b"x").hexdigest()))
    r2 = node.did.reputation(user.address)
    assert r2["score"] > 50.0
    # 他人视角：无明细
    other = QuantumWallet()
    pub = node.did.reputation(user.address, viewer=other.address)
    assert "components" not in pub and "score" in pub
    # 本人视角：有明细
    priv = node.did.reputation(user.address, viewer=user.address)
    assert "components" in priv
    # 低声誉惩罚：被仲裁封禁
    node.store.arb_banned.add(user.address)
    node.did.recompute(user.address)
    assert node.did.reputation(user.address)["score"] < 50.0
