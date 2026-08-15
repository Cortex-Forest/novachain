# -*- coding: utf-8 -*-
"""Agent 运行时数据结构（阶段 2）。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict


@dataclass
class ContentDraft:
    """内容引擎产出的草稿。"""
    title: str = ""
    content: str = ""
    kind: str = "poem"
    tags: list = field(default_factory=list)
    price: float = 0.0

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AgentSignal:
    """感知器输出的一条信号。"""
    kind: str = "idle"          # paused / budget_low / income / prompt / idle
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self):
        return asdict(self)


@dataclass
class AgentDecision:
    """决策循环的产物：一次拟执行的动作（或 idle 空转）。"""
    action: str = "idle"        # publish_text / idle（后续可扩展）
    reason: str = ""
    draft: ContentDraft = None
    cost: float = 0.0
    params: dict = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        d["draft"] = self.draft.to_dict() if self.draft else None
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["draft"] = ContentDraft.from_dict(d["draft"]) if d.get("draft") else None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AuditEntry:
    """审计日志条目（JSONL 一行）。"""
    ts: float
    tick: int
    status: str                 # ok / blocked / idle / error
    decision: dict = field(default_factory=dict)
    txid: str = ""
    error: str = ""
    cost: float = 0.0
    budget_remaining: float = 0.0

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AgentState:
    """运行时本地状态（可持久化，与链上状态互为镜像）。"""
    created_at: float = field(default_factory=time.time)
    last_tick: float = 0.0
    last_action_at: float = 0.0
    tick_count: int = 0
    actions_today: int = 0
    day: str = ""
    total_published: int = 0
    total_spent: float = 0.0
    total_income: float = 0.0
    last_income_ts: float = 0.0
    local_paused: bool = False
    pending_prompts: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s

    def roll_day(self, day: str):
        """跨天重置当日计数。"""
        if day and day != self.day:
            self.day = day
            self.actions_today = 0
