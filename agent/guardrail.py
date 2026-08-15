# -*- coding: utf-8 -*-
"""安全护栏（阶段 2）：链上约束的本地镜像 + 运行策略约束。

链上日预算/暂停由节点强制执行（防私钥泄露兜底）；
本护栏在提交前再做一层运行策略校验：动作白名单、本地紧急暂停、
预算/余额复核、动作冷却、每日次数上限。
"""
from __future__ import annotations

from .config import AgentConfig


class Guardrail:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def check(self, decision, state, chain, addr: str, now: float):
        if decision.action == "idle":
            return True, "ok"
        if decision.action not in self.cfg.action_whitelist:
            return False, "动作不在白名单"
        identity = chain.ai_identity(addr)
        if not identity:
            return False, "链上无 AI 身份"
        if identity.get("status") != "active":
            return False, "链上状态非 active"
        if state.local_paused:
            return False, "本地紧急暂停"
        budget = chain.ai_budget_state(addr) or {}
        if float(budget.get("remaining", 0.0) or 0.0) + 1e-9 < decision.cost:
            return False, "链上日预算不足"
        if chain.balance(addr) + 1e-9 < decision.cost:
            return False, "钱包余额不足"
        if now - state.last_action_at < self.cfg.min_interval:
            return False, "动作冷却中"
        if state.actions_today >= self.cfg.max_actions_per_day:
            return False, "当日次数达上限"
        return True, "ok"
