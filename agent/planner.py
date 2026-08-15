# -*- coding: utf-8 -*-
"""决策循环（阶段 2）：选题 → 调内容引擎 → 估算成本 → 输出动作。

预算感知：任何动作都必须能被当日剩余预算 + 钱包余额覆盖，否则转入 idle。
当前自主度 L1：仅自动创作售卖（publish_text），为后续 L2/L3 预留扩展点。
"""
from __future__ import annotations

from .config import AgentConfig
from .models import AgentDecision

_KINDS = ("poem", "essay", "microblog")


class Planner:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def plan(self, signals, state, chain, addr: str, engine) -> AgentDecision:
        kinds = {s.kind: s for s in signals}
        if kinds.get("paused"):
            return AgentDecision("idle", "链上已暂停，等待 owner 恢复",
                                 params={"reason": "paused"})
        if kinds.get("budget_low"):
            return AgentDecision("idle", "当日预算不足以覆盖最小动作，等待跨天重置",
                                 params={"reason": "budget_low"})
        if state.actions_today >= self.cfg.max_actions_per_day:
            return AgentDecision("idle", "当日动作次数已达上限",
                                 params={"reason": "max_actions"})

        prompt_sig = kinds.get("prompt")
        if prompt_sig:
            topic = str(prompt_sig.data.get("text", "")).strip()[:24] or "灵感"
        else:
            topic = self.cfg.topics[state.actions_today % len(self.cfg.topics)]

        kind = _KINDS[state.actions_today % len(_KINDS)]
        draft = engine.generate(topic, self.cfg.name, kind=kind)
        deposit = chain.text_deposit_required(self.cfg.tier, addr)
        gas = chain.gas_of(addr)
        cost = round(deposit + gas, 8)

        budget = chain.ai_budget_state(addr) or {}
        remaining = float(budget.get("remaining", 0.0) or 0.0)
        if remaining + 1e-9 < cost:
            return AgentDecision("idle", "剩余预算不足以覆盖本次发布",
                                 params={"reason": "budget_short", "cost": cost,
                                         "remaining": remaining})
        if chain.balance(addr) + 1e-9 < cost:
            return AgentDecision("idle", "钱包余额不足以覆盖本次发布",
                                 params={"reason": "balance_short", "cost": cost})

        return AgentDecision(
            "publish_text",
            f"主题「{topic}」→ 生成{kind} → 自动签名发布（成本 {cost} NOVA）",
            draft=draft, cost=cost,
            params={"topic": topic, "kind": kind, "deposit": deposit, "gas": gas},
        )
