# -*- coding: utf-8 -*-
"""Agent 运行时（阶段 2）测试。

覆盖：Mock 引擎确定性与约束 / LLM 引擎无密钥回退 / 感知信号 /
决策预算感知 / 护栏（白名单、冷却、本地暂停）/ 端到端自动发布 /
链上暂停联动 / 审计 JSONL / 状态持久化 / RPC 网关提交。
"""
import json
import os
import tempfile
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import (AgentConfig, AgentRuntime, LocalNodeGateway, RpcGateway,
                   MockContentEngine, LlmContentEngine, Guardrail,
                   AgentState)
from core.crypto import QuantumWallet
from core.transaction import Tx
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9977)
    kw.setdefault("rpc", 8455)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


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


def _register(node, ai, owner, budget=60.0, name="测试 AI"):
    _apply(node, _signed_tx(ai, "nova:ai:register", name=name,
                            owner=owner.address, daily_budget=budget,
                            meta="model:test;runtime:agent-v2"))


def _make_runtime(node, ai, owner, budget=60.0, tmp=None, **cfgkw):
    _register(node, ai, owner, budget)
    cfg = AgentConfig(
        name="测试 AI", owner=owner.address, ai_key_hex=ai.private_key_hex(),
        daily_budget=budget, min_interval=0.0, max_actions_per_day=20,
        state_file=os.path.join(tmp, "state.json") if tmp else None,
        audit_file=os.path.join(tmp, "audit.jsonl") if tmp else None,
        **cfgkw)
    return AgentRuntime(cfg, LocalNodeGateway(node))


# ----------------------------------------------------------------------
# 1. 内容引擎
# ----------------------------------------------------------------------
def test_mock_engine_deterministic_and_bounded():
    cfg = AgentConfig(price_min=1.0, price_max=20.0)
    eng = MockContentEngine(cfg)
    d1 = eng.generate("夜与星", "Nova 诗灵", "poem")
    d2 = eng.generate("夜与星", "Nova 诗灵", "poem")
    d3 = eng.generate("城市情绪", "Nova 诗灵", "poem")
    assert d1.to_dict() == d2.to_dict()
    assert d1.title != d3.title
    assert 0 < len(d1.title) <= 64
    assert 0 < len(d1.content) <= 20000
    assert 0.01 <= d1.price <= 20.0


def test_llm_engine_falls_back_without_key():
    cfg = AgentConfig()
    eng = LlmContentEngine(cfg.engine, cfg)
    d = eng.generate("夜与星", "Nova 诗灵", "poem")
    assert eng.last_fallback is True
    assert eng.last_error == "no_api_key"
    assert d.title and d.content


# ----------------------------------------------------------------------
# 2. 感知器
# ----------------------------------------------------------------------
def test_perception_signals():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    node.balances[owner.address] = 1000.0
    node.balances[ai.address] = 1000.0
    _register(node, ai, owner, budget=60.0)
    state = AgentState()
    from agent import Perceiver
    signals = Perceiver(AgentConfig()).observe(LocalNodeGateway(node), ai.address, state)
    kinds = {s.kind for s in signals}
    assert "idle" in kinds and "paused" not in kinds and "budget_low" not in kinds

    # 暂停 → paused 信号
    _apply(node, _signed_tx(owner, "nova:ai:config", action="pause", target=ai.address))
    signals = Perceiver(AgentConfig()).observe(LocalNodeGateway(node), ai.address, state)
    assert any(s.kind == "paused" for s in signals)

    # prompt 注入 → prompt 信号
    state.pending_prompts.append("时间是一面镜子")
    signals = Perceiver(AgentConfig()).observe(LocalNodeGateway(node), ai.address, state)
    assert any(s.kind == "prompt" for s in signals)


# ----------------------------------------------------------------------
# 3. 决策器（预算感知）
# ----------------------------------------------------------------------
def test_planner_budget_aware():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    node.balances[owner.address] = 1000.0
    node.balances[ai.address] = 1000.0
    _register(node, ai, owner, budget=1.0)   # 预算远低于保证金 10
    from agent import Perceiver, Planner
    gw = LocalNodeGateway(node)
    cfg = AgentConfig(ai_key_hex=ai.private_key_hex(), daily_budget=1.0)
    state = AgentState()
    signals = Perceiver(cfg).observe(gw, ai.address, state)
    decision = Planner(cfg).plan(signals, state, gw, ai.address, MockContentEngine(cfg))
    assert decision.action == "idle"
    assert decision.params.get("reason") == "budget_low"


# ----------------------------------------------------------------------
# 4. 护栏
# ----------------------------------------------------------------------
def test_guardrail_blocks_non_whitelist_cooldown_and_local_pause():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    node.balances[owner.address] = 1000.0
    node.balances[ai.address] = 1000.0
    _register(node, ai, owner, budget=60.0)
    gw = LocalNodeGateway(node)
    cfg = AgentConfig(ai_key_hex=ai.private_key_hex(), daily_budget=60.0,
                      min_interval=100.0, action_whitelist=("publish_text",))
    guard = Guardrail(cfg)
    state = AgentState()
    from agent import AgentDecision, ContentDraft
    decision = AgentDecision("publish_text", "x", draft=ContentDraft("t", "c", price=5.0),
                             cost=10.0)
    # 冷却中
    state.last_action_at = time.time()
    ok, reason = guard.check(decision, state, gw, ai.address, time.time())
    assert not ok and "冷却" in reason
    # 白名单外动作
    state.last_action_at = 0.0
    ok, reason = guard.check(AgentDecision("hack", "x", cost=1.0), state, gw, ai.address, time.time())
    assert not ok and "白名单" in reason
    # 本地紧急暂停
    state.local_paused = True
    ok, reason = guard.check(decision, state, gw, ai.address, time.time())
    assert not ok and "暂停" in reason


# ----------------------------------------------------------------------
# 5. 端到端：自动发布
# ----------------------------------------------------------------------
def test_runtime_publishes_valid_tx():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    node.balances[owner.address] = 1000.0
    node.balances[ai.address] = 1000.0
    rt = _make_runtime(node, ai, owner, budget=60.0)
    entry = rt.tick()
    assert entry.status == "ok"
    assert entry.txid and entry.txid in node.dag
    assert entry.cost > 0
    assert node.store.text_assets, "链上应存在文本资产"
    asset = next(iter(node.store.text_assets.values()))
    assert asset["author"] == ai.address
    assert rt.state.total_published == 1
    st = node.socialfi.ai_budget_state(ai.address)
    assert st["spent"] >= 10.0


def test_runtime_dry_run_does_not_apply():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    node.balances[owner.address] = 1000.0
    node.balances[ai.address] = 1000.0
    rt = _make_runtime(node, ai, owner, budget=60.0)
    from agent import AgentDecision, ContentDraft
    from agent.executor import Executor
    decision = AgentDecision("publish_text", "dry", draft=ContentDraft("预演", "内容", price=3.0),
                             cost=10.0)
    result = Executor(rt.cfg).execute(decision, ai, LocalNodeGateway(node), dry_run=True)
    assert result["dry_run"] and result["accepted"]
    assert not node.store.text_assets, "dry-run 不应上链"


# ----------------------------------------------------------------------
# 6. 链上暂停联动
# ----------------------------------------------------------------------
def test_chain_pause_halts_runtime():
    node = _node()
    owner = QuantumWallet()
    ai = QuantumWallet()
    node.balances[owner.address] = 1000.0
    node.balances[ai.address] = 1000.0
    rt = _make_runtime(node, ai, owner, budget=60.0)
    assert rt.tick().status == "ok"
    _apply(node, _signed_tx(owner, "nova:ai:config", action="pause", target=ai.address))
    entry = rt.tick()
    assert entry.status == "idle"
    assert "暂停" in entry.error
    _apply(node, _signed_tx(owner, "nova:ai:config", action="resume", target=ai.address))
    assert rt.tick().status == "ok"


# ----------------------------------------------------------------------
# 7. 审计与状态持久化
# ----------------------------------------------------------------------
def test_audit_jsonl_written():
    with tempfile.TemporaryDirectory() as tmp:
        node = _node()
        owner = QuantumWallet()
        ai = QuantumWallet()
        node.balances[owner.address] = 1000.0
        node.balances[ai.address] = 1000.0
        rt = _make_runtime(node, ai, owner, budget=60.0, tmp=tmp)
        rt.tick()
        rt.tick()
        with open(os.path.join(tmp, "audit.jsonl"), "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        assert len(lines) == 2
        assert lines[0]["status"] == "ok"
        assert lines[0]["txid"] and lines[0]["cost"] > 0
        assert "budget_remaining" in lines[0]
        assert rt.tail_audit(1)[0]["tick"] == 2


def test_state_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        node = _node()
        owner = QuantumWallet()
        ai = QuantumWallet()
        node.balances[owner.address] = 1000.0
        node.balances[ai.address] = 1000.0
        rt = _make_runtime(node, ai, owner, budget=60.0, tmp=tmp)
        rt.tick()
        saved = os.path.join(tmp, "state.json")
        assert os.path.exists(saved)
        with open(saved, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["total_published"] == 1
        # 重建运行时恢复状态
        cfg = AgentConfig(ai_key_hex=ai.private_key_hex(), state_file=saved, audit_file=None)
        rt2 = AgentRuntime(cfg, LocalNodeGateway(node))
        assert rt2.state.total_published == 1


# ----------------------------------------------------------------------
# 8. RPC 网关
# ----------------------------------------------------------------------
def test_rpc_gateway_submit():
    """RPC 网关：HTTP 服务跑在后台线程，主线程用同步 urllib 提交。"""
    import asyncio
    import threading
    node = _node()
    app = web.Application()
    setup_routes(app, node)
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    try:
        owner = QuantumWallet()
        ai = QuantumWallet()
        node.balances[owner.address] = 1000.0
        node.balances[ai.address] = 1000.0
        _register(node, ai, owner, budget=60.0)
        gw = RpcGateway(f"http://127.0.0.1:{port}")
        assert gw.ai_identity(ai.address)["name"] == "测试 AI"
        assert gw.balance(ai.address) == pytest.approx(1000.0, abs=1e-3)
        assert gw.text_deposit_required("basic") == 10.0
        rt = AgentRuntime(AgentConfig(ai_key_hex=ai.private_key_hex(),
                                      min_interval=0.0, state_file=None, audit_file=None), gw)
        entry = rt.tick()
        assert entry.status == "ok"
        assert entry.txid in node.dag
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(timeout=5)
