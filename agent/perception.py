# -*- coding: utf-8 -*-
"""感知器（阶段 2）：扫描链上状态与事件，产出自适应信号。

信号类型：
- paused     链上 AI 身份被 owner 暂停 / 冻结
- budget_low 当日剩余预算已不足以完成一次最小动作
- income     收到内容售卖/保证金退回等链上收入
- prompt     外部注入的创作指令（运行时可 inject_prompt）
- idle       可正常活动（供决策器决定是否出手）
"""
from __future__ import annotations

import time

from .config import AgentConfig
from .models import AgentSignal


class Perceiver:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def observe(self, chain, addr: str, state) -> list:
        signals = []
        identity = chain.ai_identity(addr)
        if not identity:
            return [AgentSignal("idle", {"reason": "no_identity"})]

        if identity.get("status") != "active":
            signals.append(AgentSignal("paused", {"status": identity.get("status")}))

        budget = chain.ai_budget_state(addr) or {}
        remaining = float(budget.get("remaining", 0.0) or 0.0)
        min_cost = chain.text_deposit_required(self.cfg.tier, addr) + chain.gas_of(addr)
        if remaining + 1e-9 < min_cost:
            signals.append(AgentSignal("budget_low", {"remaining": remaining, "min_cost": min_cost}))

        income = 0.0
        for e in chain.recent_events(addr, 30):
            if e.get("income") and float(e.get("ts", 0) or 0) > state.last_income_ts:
                income += float(e.get("amount", 0.0) or 0.0)
        if income > 0:
            signals.append(AgentSignal("income", {"amount": income}))

        if state.pending_prompts:
            signals.append(AgentSignal("prompt", {"text": state.pending_prompts[0]}))

        signals.append(AgentSignal("idle", {"remaining": remaining}))
        return signals
