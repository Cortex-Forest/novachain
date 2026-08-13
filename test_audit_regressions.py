# -*- coding: utf-8 -*-
"""审核回归测试：覆盖 P0/P1 修复点。"""
import asyncio
import json
import time

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from core.transaction import Tx
from core.vm import deploy_address
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9972)
    kw.setdefault("rpc", 8324)
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


def _apply(node, tx):
    assert node.validate_tx(tx), "validate failed: " + tx.data[:80]
    node.apply_tx(tx)


# ---------------------------------------------------------------------------
# P0-1: 部署奖励必须由 creator 签名认领
# ---------------------------------------------------------------------------
async def test_deploy_requires_signed_creator():
    node = _node()
    _fund_eco(node)
    app = web.Application()
    setup_routes(app, node)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        w = QuantumWallet()
        bytecode = "let x = 1;\nreturn x;"

        res = await client.post("/api/deploy", json={"bytecode": bytecode, "creator": w.address})
        assert res.status == 400
        assert "签名" in (await res.json())["error"]

        addr = deploy_address(bytecode)
        sig = w.sign("deploy:{0}:{1}".format(addr, bytecode))
        res = await client.post("/api/deploy", json={
            "bytecode": bytecode, "creator": w.address,
            "sender_public_key": w.public_key_hex(), "signature": sig,
        })
        body = await res.json()
        assert res.status == 200
        assert body["reward"] == node.economy.deploy_reward()
        assert node.store.contract_code[addr] == [0x01, 1, 0x02, 0, 0x06, 0, 0x05]

        # 同一地址只能领一次部署奖励
        bytecode2 = "let y = 2;\nreturn y;"
        addr2 = deploy_address(bytecode2)
        sig2 = w.sign("deploy:{0}:{1}".format(addr2, bytecode2))
        res = await client.post("/api/deploy", json={
            "bytecode": bytecode2, "creator": w.address,
            "sender_public_key": w.public_key_hex(), "signature": sig2,
        })
        assert res.status == 400
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# P1-4: 计算市场过期/重复提交不产生额外发放
# ---------------------------------------------------------------------------
def test_compute_expired_task_no_double_payout():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for a in (creator, w1, w2):
        _fund(node, a.address)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=10.0, spec="task", expires_in=300))
    tid = next(iter(node.store.compute_tasks))
    _apply(node, _signed_tx(w1, "nova:compute:accept", task_id=tid))
    node.store.compute_tasks[tid]["expires_at"] = time.time() - 1  # 已过期但状态仍 open

    t1 = _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash="11" * 32)
    assert node.validate_tx(t1)
    node.apply_tx(t1)
    assert node.store.compute_tasks[tid]["status"] == "expired"

    # 过期后第二个提交被拒绝
    t2 = _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash="11" * 32)
    assert not node.validate_tx(t2)

    # 无人获得赏金；creator 已全额退回（扣除手续费）
    assert node.balances[w1.address] < 100000.0
    assert node.balances[w2.address] == 100000.0
    assert node.balances[creator.address] > 99999.0


def test_compute_market_complete_idempotent():
    node = _node()
    store = node.store
    for a in ("a", "b", "c", "d"):
        store.balances[a] = 100.0
    cm = node.compute_market
    cm.publish("a", "spec", 10.0, 3600, "t1")
    cm.accept("b", "t1")
    cm.accept("c", "t1")
    r0 = cm.submit("b", "t1", "11" * 32)
    assert r0["status"] == "open"
    r1 = cm.submit("c", "t1", "11" * 32)
    assert r1["status"] == "completed"
    before = store.balances["b"] + store.balances["c"]
    r2 = cm.submit("c", "t1", "22" * 32)
    assert r2["reward"] == 0.0
    assert store.balances["b"] + store.balances["c"] == before


# ---------------------------------------------------------------------------
# P1-6: 聊天 ack 必须由收件人签名
# ---------------------------------------------------------------------------
async def test_chat_ack_requires_signature():
    node = _node()
    app = web.Application()
    setup_routes(app, node)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        w = QuantumWallet()
        ids = ["ab" * 24]

        res = await client.post("/api/chat/ack", json={"addr": w.address, "ids": ids})
        assert res.status == 400

        sig_msg = "ack:" + w.address + ":" + json.dumps(sorted(set(ids)))
        res = await client.post("/api/chat/ack", json={
            "addr": w.address, "ids": ids,
            "sender_public_key": w.public_key_hex(), "signature": w.sign(sig_msg),
        })
        assert res.status == 200
        assert (await res.json())["removed"] == 0
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# P1-5: 存储模块守卫
# ---------------------------------------------------------------------------
def test_storage_module_guards():
    node = _node()
    sn = node.storage_net
    fake_addr = "0x" + "1" * 40
    fake_cid = "0x" + "a" * 64
    assert sn.claim(fake_addr, fake_cid, "0" * 64) is False
    assert "error" in sn.proof(fake_addr, fake_cid, "0" * 64)
    assert sn.pin(fake_addr, fake_cid, 1.0, 30) >= 0  # 生态基金不足时仍不抛异常


# ---------------------------------------------------------------------------
# P0-2: P2P 换行分帧支持大消息
# ---------------------------------------------------------------------------
async def test_p2p_framing_large_message_roundtrip():
    from network.p2p import _enc
    big = {"type": "state_snapshot", "payload": "x" * 200000}
    data = _enc(big)
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    reader.feed_data(data)
    reader.feed_eof()
    line = await reader.readuntil(b"\n")
    assert json.loads(line.strip()) == big

# ---------------------------------------------------------------------------
# P1-7: SocialFi ID 跨节点确定性（不得依赖墙钟时间）
# ---------------------------------------------------------------------------
def test_socialfi_ids_deterministic_across_nodes():
    w = QuantumWallet()
    payload = {"op": "nova:fan:issue", "symbol": "DET", "name": "Deterministic",
               "supply": 10000, "price": 0.5}
    data = json.dumps(payload, ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, 0, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())

    n1, n2 = _node(), _node()
    _fund(n1, w.address)
    _fund(n2, w.address)
    _apply(n1, tx)
    _apply(n2, tx)
    ids1 = set(n1.store.fan_tokens)
    ids2 = set(n2.store.fan_tokens)
    assert len(ids1) == 1 and ids1 == ids2

    for op, kw in [
        ("nova:rev:create", {"name": "R", "desc": "d"}),
        ("nova:ach:issue", {"title": "T", "badge": "B", "desc": "d"}),
        ("nova:market:create", {"question": "Q", "options": ["a", "b"], "closes_in": 3600}),
        ("nova:curate:create", {"title": "C", "items": ["a", "b"], "price": 1.0}),
        ("nova:bond:issue", {"name": "BD", "principal": 1000, "rate": 0.05, "term_days": 90}),
        ("nova:frac:split", {"nft_ref": "nft_1", "name": "F", "supply": 100, "price_per": 1.0}),
    ]:
        data2 = json.dumps({"op": op, **kw}, ensure_ascii=False)
        tx2 = Tx(w.address, w.address, 0, [], data2, w.public_key_hex(), "", timestamp=ts)
        tx2.signature = w.sign(tx2.signing_data())
        a1, a2 = _node(), _node()
        _fund(a1, w.address)
        _fund(a2, w.address)
        _apply(a1, tx2)
        _apply(a2, tx2)
        dom1 = set()
        for attr in ("revenue_shares", "achievements", "markets", "curations", "bonds", "fractions"):
            if getattr(a1.store, attr):
                dom1 |= set(getattr(a1.store, attr))
        dom2 = set()
        for attr in ("revenue_shares", "achievements", "markets", "curations", "bonds", "fractions"):
            if getattr(a2.store, attr):
                dom2 |= set(getattr(a2.store, attr))
        assert dom1 == dom2 and len(dom1) == 1, (op, dom1, dom2)
