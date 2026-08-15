# -*- coding: utf-8 -*-
"""AI 创作者（阶段 0 PoC）测试。

覆盖：身份注册校验 / 日预算硬约束 / 文本自动发布与 90:10 自动分账 /
日窗口跨天重置 / owner 暂停恢复 / 快照持久化 / RPC 视图。
"""
import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from core.transaction import Tx
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9988)
    kw.setdefault("rpc", 8466)
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


def _register_ai(node, ai, owner, budget=19.0, name="Nova 诗灵"):
    _apply(node, _signed_tx(ai, "nova:ai:register", name=name,
                            owner=owner.address, daily_budget=budget,
                            meta="model:novapoet-v1"))


def _publish(node, ai, title="AI 作品", content="内容", price=5.0, deposit=None):
    if deposit is None:
        deposit = node.socialfi.text_deposit_required("basic", ai.address)
    return _signed_tx(ai, "nova:text:create", amount=deposit, title=title,
                      content=content, price=price, tier="basic", visibility="public")


def test_ai_register_budget_enforcement():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    _fund(node, owner.address)
    _fund(node, ai.address)
    _register_ai(node, ai, owner, budget=19.0)

    identity = node.socialfi.ai_identity(ai.address)
    assert identity["name"] == "Nova 诗灵"
    assert identity["owner"] == owner.address
    assert identity["daily_budget"] == 19.0
    assert identity["status"] == "active"

    # 第一次发布（保证金 10）通过，窗口已用 10
    _apply(node, _publish(node, ai))
    st = node.socialfi.ai_budget_state(ai.address)
    assert st["spent"] == pytest.approx(10.0)
    assert st["remaining"] == pytest.approx(9.0)

    # 同日第二次发布（+10 > 19）被链上拒绝 —— 预算为链上硬约束
    assert not node.validate_tx(_publish(node, ai, title="第二篇"))
    # 小额度转账（剩余 9 内）放行
    tx = Tx(ai.address, owner.address, 5.0, [], "", ai.public_key_hex(), "", timestamp=int(time.time()))
    tx.signature = ai.sign(tx.signing_data())
    _apply(node, tx)
    assert node.socialfi.ai_budget_state(ai.address)["spent"] == pytest.approx(15.0)
    # 再转 5 就超预算
    tx2 = Tx(ai.address, owner.address, 5.0, [], "", ai.public_key_hex(), "", timestamp=int(time.time()))
    tx2.signature = ai.sign(tx2.signing_data())
    assert not node.validate_tx(tx2)


def test_ai_text_publish_buy_split_90_10():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    fan = QuantumWallet()
    _fund(node, owner.address)
    _fund(node, ai.address)
    _fund(node, fan.address)
    _fund_eco(node)
    _register_ai(node, ai, owner, budget=50.0)

    _apply(node, _publish(node, ai, title="AI 的散文", content="正文", price=8.0))
    tid = next(iter(node.store.text_assets))
    a = node.store.text_assets[tid]
    assert a["author"] == ai.address
    assert a["status"] == "listed"

    ai_bal0 = node.balances[ai.address]
    eco0 = node.balances[node.economy.ECOSYSTEM_FUND]
    _apply(node, _signed_tx(fan, "nova:text:buy", amount=8.0, text_id=tid))
    assert fan.address in a["buyers"]
    # 90% 归 AI 创作者，10% 归生态基金
    assert node.balances[ai.address] == pytest.approx(ai_bal0 + 7.2)
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(eco0 + 0.8)


def test_ai_daily_window_reset():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    _fund(node, owner.address)
    _fund(node, ai.address)
    _register_ai(node, ai, owner, budget=19.0)

    _apply(node, _publish(node, ai))
    assert node.socialfi.ai_budget_state(ai.address)["spent"] == pytest.approx(10.0)
    assert not node.validate_tx(_publish(node, ai, title="超预算"))

    # 模拟跨天：旧窗口过期，支出自动清零
    node.store.ai_daily_spend[ai.address] = {"date": "2000-01-01", "spent": 0.0}
    st = node.socialfi.ai_budget_state(ai.address)
    assert st["spent"] == pytest.approx(0.0)
    _apply(node, _publish(node, ai, title="次日的作品"))
    assert node.socialfi.ai_budget_state(ai.address)["spent"] == pytest.approx(10.0)


def test_ai_pause_resume_owner_only():
    node = _node()
    owner = QuantumWallet()
    stranger = QuantumWallet()
    ai = QuantumWallet()
    _fund(node, owner.address)
    _fund(node, stranger.address)
    _fund(node, ai.address)
    _register_ai(node, ai, owner, budget=100.0)

    # 仅 owner 可配置；陌生人被拒
    assert not node.validate_tx(_signed_tx(stranger, "nova:ai:config", action="pause", target=ai.address))
    _apply(node, _signed_tx(owner, "nova:ai:config", action="pause", target=ai.address))
    assert node.socialfi.ai_identity(ai.address)["status"] == "paused"

    # 暂停期间即使预算充足也拒绝一切支出
    assert not node.validate_tx(_publish(node, ai))
    tx = Tx(ai.address, owner.address, 1.0, [], "", ai.public_key_hex(), "", timestamp=int(time.time()))
    tx.signature = ai.sign(tx.signing_data())
    assert not node.validate_tx(tx)

    # owner 恢复后能力回归
    _apply(node, _signed_tx(owner, "nova:ai:config", action="resume", target=ai.address))
    assert node.socialfi.ai_identity(ai.address)["status"] == "active"
    _apply(node, _publish(node, ai))

    # owner 调整预算后按新预算执行
    _apply(node, _signed_tx(owner, "nova:ai:config", action="budget", daily_budget=5.0, target=ai.address))
    assert node.socialfi.ai_identity(ai.address)["daily_budget"] == 5.0
    assert not node.validate_tx(_publish(node, ai, title="超出新预算"))  # 已用 10 > 5


def test_ai_register_validation():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    ai2 = QuantumWallet()
    _fund(node, owner.address)
    _fund(node, ai.address)
    _fund(node, ai2.address)

    # 参数不合法：预算过小/过大、缺名称、金额非 0、owner 非法
    for kw in (dict(name="x", owner=owner.address, daily_budget=0.001),
               dict(name="x", owner=owner.address, daily_budget=999999),
               dict(owner=owner.address, daily_budget=10),
               dict(name="x", owner="bad-owner", daily_budget=10)):
        assert not node.validate_tx(_signed_tx(ai, "nova:ai:register", **kw)), kw
    assert not node.validate_tx(_signed_tx(ai, "nova:ai:register", name="x",
                                           owner=owner.address, daily_budget=10, amount=1))
    _apply(node, _signed_tx(ai, "nova:ai:register", name="合法注册",
                            owner=owner.address, daily_budget=10.0))
    # 同地址不能重复注册
    assert not node.validate_tx(_signed_tx(ai, "nova:ai:register", name="重复",
                                           owner=owner.address, daily_budget=10.0))
    # 未注册地址不能 config
    assert not node.validate_tx(_signed_tx(ai2, "nova:ai:config", action="pause"))
    # 不存在的 action 被拒
    assert not node.validate_tx(_signed_tx(owner, "nova:ai:config", action="hack"))


def test_ai_state_snapshot_roundtrip():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    _fund(node, owner.address)
    _fund(node, ai.address)
    _register_ai(node, ai, owner, budget=19.0)
    _apply(node, _publish(node, ai))

    snap = node.store.to_dict()
    assert snap["ai_creators"][ai.address]["name"] == "Nova 诗灵"
    assert snap["ai_daily_spend"][ai.address]["spent"] == pytest.approx(10.0)

    fresh = _node()
    fresh.store.from_dict(snap)
    assert fresh.store.ai_creators[ai.address]["daily_budget"] == 19.0
    assert fresh.socialfi.ai_budget_state(ai.address)["spent"] == pytest.approx(10.0)
    # 恢复后预算约束依然生效
    assert not fresh.validate_tx(_publish(fresh, ai, title="超预算"))


@pytest.mark.asyncio
async def test_ai_rpc_view_list():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    _fund(node, owner.address)
    _fund(node, ai.address)
    _register_ai(node, ai, owner, budget=19.0)
    _apply(node, _publish(node, ai))

    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/ai/" + ai.address)
        body = await resp.json()
        assert resp.status == 200
        assert body["name"] == "Nova 诗灵"
        assert body["budget"]["spent"] == pytest.approx(10.0)
        assert body["budget"]["remaining"] == pytest.approx(9.0)
        assert any(e["op"] == "nova:ai:register" for e in body["recent_ops"])

        resp = await client.get("/api/ai/" + "0x" + "0" * 40)
        assert resp.status == 404

        resp = await client.get("/api/ai")
        listing = await resp.json()
        assert listing["count"] == 1
        assert listing["creators"][0]["addr"] == ai.address
    finally:
        await client.close()