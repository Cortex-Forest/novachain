# -*- coding: utf-8 -*-
"""调度器（阶段 2）：AgentRuntime 把感知/决策/执行/护栏串成一次 tick 循环。

特性：
- tick()：单次生命周期（感知 → 决策 → 护栏 → 执行 → 审计）
- run_loop()：按间隔持续运转（可配 stop_event / max_ticks）
- 审计日志：JSONL 追加写（决策、txid、成本、预算余额、结果）
- 状态持久化：本地镜像（跨天重置、当日次数、累计收支）
- 状态服务：可选 aiohttp 端点暴露 status / 审计尾部
"""
from __future__ import annotations

import json
import os
import time

from aiohttp import web

from core.crypto import QuantumWallet

from .config import AgentConfig
from .engine import make_engine
from .executor import Executor
from .gateway import ChainGateway
from .guardrail import Guardrail
from .models import AgentState, AuditEntry
from .perception import Perceiver
from .planner import Planner


def _day(now: float = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


class AgentRuntime:
    def __init__(self, cfg: AgentConfig, chain: ChainGateway, wallet=None,
                 engine=None, perceiver=None, planner=None,
                 guardrail=None, executor=None, state=None):
        self.cfg = cfg
        self.chain = chain
        if wallet is None:
            if cfg.ai_key_hex:
                wallet = QuantumWallet(private_key_bytes=bytes.fromhex(cfg.ai_key_hex))
            else:
                wallet = QuantumWallet()
        self.wallet = wallet
        self.engine = engine or make_engine(cfg)
        self.perceiver = perceiver or Perceiver(cfg)
        self.planner = planner or Planner(cfg)
        self.guardrail = guardrail or Guardrail(cfg)
        self.executor = executor or Executor(cfg)
        self.state = state or self._load_state()
        self.audit = []

    # ---------- 持久化 ----------
    def _load_state(self) -> AgentState:
        if self.cfg.state_file and os.path.exists(self.cfg.state_file):
            try:
                with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                    return AgentState.from_dict(json.load(f))
            except Exception:
                pass
        return AgentState()

    def _save_state(self):
        if not self.cfg.state_file:
            return
        with open(self.cfg.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state.to_dict(), f, ensure_ascii=False, indent=2)

    def _append_audit(self, entry: AuditEntry):
        self.audit.append(entry)
        if self.cfg.audit_file:
            with open(self.cfg.audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    # ---------- 外部控制 ----------
    def inject_prompt(self, text: str):
        """注入创作指令：下个 tick 优先按该主题创作。"""
        self.state.pending_prompts.append(str(text).strip())

    def emergency_pause(self):
        self.state.local_paused = True
        self._save_state()

    def emergency_resume(self):
        self.state.local_paused = False
        self._save_state()

    # ---------- 主循环 ----------
    def tick(self, now: float = None) -> AuditEntry:
        now = time.time() if now is None else now
        self.state.last_tick = now
        self.state.tick_count += 1
        tick_no = self.state.tick_count
        self.state.roll_day(_day(now))

        signals = self.perceiver.observe(self.chain, self.wallet.address, self.state)
        for s in signals:
            if s.kind == "income":
                self.state.total_income += float(s.data.get("amount", 0.0) or 0.0)
                self.state.last_income_ts = max(self.state.last_income_ts, s.ts)
        if any(s.kind == "prompt" for s in signals) and self.state.pending_prompts:
            self.state.pending_prompts.pop(0)

        decision = self.planner.plan(signals, self.state, self.chain,
                                     self.wallet.address, self.engine)
        entry = AuditEntry(ts=now, tick=tick_no, status="idle")
        entry.decision = decision.to_dict()
        if decision.action == "idle":
            entry.error = decision.reason
        else:
            ok, reason = self.guardrail.check(decision, self.state, self.chain,
                                              self.wallet.address, now)
            if not ok:
                entry.status = "blocked"
                entry.error = reason
            else:
                try:
                    result = self.executor.execute(decision, self.wallet, self.chain)
                    entry.status = "ok"
                    entry.txid = result.get("txid", "")
                    entry.cost = float(result.get("cost", 0.0) or 0.0)
                    self.state.last_action_at = now
                    self.state.actions_today += 1
                    self.state.total_published += 1
                    self.state.total_spent += entry.cost
                except Exception as e:
                    entry.status = "error"
                    entry.error = f"{type(e).__name__}: {e}"

        budget = self.chain.ai_budget_state(self.wallet.address) or {}
        entry.budget_remaining = float(budget.get("remaining", 0.0) or 0.0)
        self._append_audit(entry)
        self._save_state()
        return entry

    def run_loop(self, interval: float = None, max_ticks: int = None,
                 stop_event=None) -> int:
        interval = interval if interval is not None else max(self.cfg.min_interval, 1.0)
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            if stop_event is not None and stop_event.is_set():
                break
            self.tick()
            ticks += 1
            time.sleep(interval)
        return ticks

    # ---------- 状态视图 ----------
    def status(self) -> dict:
        budget = self.chain.ai_budget_state(self.wallet.address) or {}
        identity = self.chain.ai_identity(self.wallet.address) or {}
        return {
            "name": self.cfg.name,
            "addr": self.wallet.address,
            "autonomy_level": self.cfg.autonomy_level,
            "chain_status": identity.get("status", "unknown"),
            "local_paused": self.state.local_paused,
            "budget": budget,
            "balance": self.chain.balance(self.wallet.address),
            "tick_count": self.state.tick_count,
            "actions_today": self.state.actions_today,
            "total_published": self.state.total_published,
            "total_spent": round(self.state.total_spent, 8),
            "total_income": round(self.state.total_income, 8),
            "engine": self.cfg.engine.kind,
            "gateway": self.cfg.gateway.kind,
        }

    def tail_audit(self, n: int = 20) -> list:
        return [e.to_dict() for e in self.audit[-n:]]

    # ---------- 状态服务（可选） ----------
    async def serve_status(self, host: str = "127.0.0.1", port: int = 0):
        app = web.Application()
        app.router.add_get("/status", self._http_status)
        app.router.add_get("/audit", self._http_audit)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        return site

    async def _http_status(self, req):
        return web.json_response(self.status())

    async def _http_audit(self, req):
        try:
            n = int(req.query.get("n", 20))
        except (TypeError, ValueError):
            n = 20
        return web.json_response({"tail": self.tail_audit(n)})
