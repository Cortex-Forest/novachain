# -*- coding: utf-8 -*-
"""预言机模块测试：ECVRF 可验证随机数 / 多源价格聚合 / 节点质押与罚没 / AI 结果验证。"""
import hashlib
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.oracle import Oracle, vrf_keygen, vrf_prove, vrf_verify, vrf_output
from core.transaction import Tx
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


def _register(node, n=3):
    ws = []
    for _ in range(n):
        w = QuantumWallet()
        _fund(node, w.address)
        priv, pub = vrf_keygen()
        _apply(node, _signed_tx(w, "nova:oracle:node:register", amount=500, pubkey=pub))
        ws.append((w, priv, pub))
    return ws


# ---------------------------------------------------------------------------
# 1. ECVRF：可验证随机数
# ---------------------------------------------------------------------------
def test_vrf_prove_verify():
    priv, pub = vrf_keygen()
    alpha = "nova:vrf:abc123"
    proof = vrf_prove(priv, alpha)
    assert vrf_verify(pub, alpha, proof)
    assert not vrf_verify(pub, alpha + "x", proof)      # 不同输入验证失败
    other, opub = vrf_keygen()
    assert not vrf_verify(opub, alpha, proof)           # 错误公钥验证失败
    assert vrf_output(alpha, proof["gamma"]) == vrf_output(alpha, proof["gamma"])
    assert len(vrf_output(alpha, proof["gamma"])) == 64


def test_vrf_request_fulfill_flow():
    node = _node()
    node_w, priv, pub = _register(node, 1)[0]
    user = QuantumWallet()
    _fund(node, user.address)
    _apply(node, _signed_tx(user, "nova:oracle:vrf:request", hint="盲盒抽奖"))
    rid = next(reversed(node.store.oracle_requests))
    req = node.store.oracle_requests[rid]
    assert req["status"] == "pending"
    alpha = node.oracle._vrf_alpha(rid)
    proof = vrf_prove(priv, alpha)
    _apply(node, _signed_tx(node_w, "nova:oracle:vrf:fulfill", request_id=rid, proof=proof))
    req = node.store.oracle_requests[rid]
    assert req["status"] == "fulfilled"
    assert req["random"] == vrf_output(alpha, proof["gamma"])
    # 错误证明被拒绝
    _apply(node, _signed_tx(user, "nova:oracle:vrf:request"))
    rid2 = next(reversed(node.store.oracle_requests))
    bad = _signed_tx(node_w, "nova:oracle:vrf:fulfill", request_id=rid2,
                     proof=vrf_prove(priv, alpha + "x"))
    assert not node.validate_tx(bad)


# ---------------------------------------------------------------------------
# 2. 多源价格聚合（中位数 + 偏离剔除）
# ---------------------------------------------------------------------------
def test_price_aggregation_median():
    node = _node()
    ws = _register(node, 3)
    a, b, c = [w for w, _, _ in ws]
    _apply(node, _signed_tx(a, "nova:oracle:price:update", feed="USDT/USD", source="chainlink", price=100.0))
    _apply(node, _signed_tx(b, "nova:oracle:price:update", feed="USDT/USD", source="pyth", price=101.0))
    agg = node.store.oracle_feeds["USDT/USD"]
    assert agg["price"] == pytest.approx(100.5)          # 2 源取均值
    # 5 分钟更新频率：第 3 源到达不立即重新发布
    _apply(node, _signed_tx(c, "nova:oracle:price:update", feed="USDT/USD", source="binance", price=99.0))
    assert node.store.oracle_feeds["USDT/USD"]["price"] == pytest.approx(100.5)
    assert len(node.store.oracle_price_sources["USDT/USD"]) == 3
    # 超过 5 分钟间隔后重新发布 -> 3 源中位数
    node.store.oracle_feeds["USDT/USD"]["ts"] = 0
    _apply(node, _signed_tx(c, "nova:oracle:price:update", feed="USDT/USD", source="binance", price=99.0))
    assert node.store.oracle_feeds["USDT/USD"]["price"] == pytest.approx(100.0)
    assert node.oracle.price("USDT/USD")["price"] == pytest.approx(100.0)
    # 偏离 >10% 拒绝
    bad = _signed_tx(a, "nova:oracle:price:update", feed="USDT/USD", source="gate", price=115.0)
    assert not node.validate_tx(bad)
    # 偏离 >25% 由举报罚没（模拟历史坏价入库后聚合价移开）
    node.store.oracle_price_sources["ETH/USD"] = {
        "chainlink": {"price": 2000.0, "updated_at": time.time(), "node": a.address, "active": True},
        "pyth": {"price": 2010.0, "updated_at": time.time(), "node": b.address, "active": True},
        "binance": {"price": 2600.0, "updated_at": time.time(), "node": c.address, "active": True},
    }
    node.oracle._commit_feed("ETH/USD")
    _apply(node, _signed_tx(a, "nova:oracle:report", target=c.address, feed="ETH/USD"))
    assert node.store.oracle_nodes[c.address]["status"] == "slashed"
    assert node.store.oracle_slashed == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# 3. 节点质押 / 退出
# ---------------------------------------------------------------------------
def test_oracle_node_register_stake_exit():
    node = _node()
    w = QuantumWallet()
    _fund(node, w.address)
    _apply(node, _signed_tx(w, "nova:oracle:node:register", amount=500,
                            pubkey="0x" + "ab" * 64))
    assert node.store.oracle_nodes[w.address]["stake"] == 500.0
    # 质押不足被拒
    w2 = QuantumWallet()
    _fund(node, w2.address)
    bad = _signed_tx(w2, "nova:oracle:node:register", amount=100, pubkey="0x" + "cd" * 64)
    assert not node.validate_tx(bad)
    # 退出冷却后可取回
    _apply(node, _signed_tx(w, "nova:oracle:node:exit"))
    assert node.oracle.exit_claimable(w.address) == 0.0
    node.store.oracle_nodes[w.address]["exiting"] = time.time() - 1
    bal0 = node.balances[w.address]
    _apply(node, _signed_tx(w, "nova:oracle:node:claim"))
    assert node.balances[w.address] == pytest.approx(bal0 + 500.0)


# ---------------------------------------------------------------------------
# 4. AI 生成结果验证 + 节点奖励
# ---------------------------------------------------------------------------
def test_ai_verification_reward():
    node = _node()
    node.balances[node.economy.ECOSYSTEM_FUND] = 100000.0
    node_w, _, _ = _register(node, 1)[0]
    creator = QuantumWallet()
    _fund(node, creator.address)
    h = hashlib.sha3_256(b"ai generated music").hexdigest()
    _apply(node, _signed_tx(creator, "nova:oracle:ai:submit", content_hash=h, meta={"kind": "music"}))
    assert node.oracle.ai_verification(h)["status"] == "pending"
    bal0 = node.balances[node_w.address]
    _apply(node, _signed_tx(node_w, "nova:oracle:ai:verify", content_hash=h, verdict=True))
    assert node.oracle.ai_verification(h)["status"] == "verified"
    assert node.balances[node_w.address] == pytest.approx(bal0 + 0.1)
    # 未验证的哈希查询返回 unknown
    assert node.oracle.ai_verification("0x" + "00" * 32) is None
