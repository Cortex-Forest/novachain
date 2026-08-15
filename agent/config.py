# -*- coding: utf-8 -*-
"""Agent 运行时配置（阶段 2）。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

DEFAULT_TOPICS = ["夜与星", "城市情绪", "数字生命", "未来已来", "无名之境"]


@dataclass
class EngineConfig:
    kind: str = "mock"                  # mock（确定性模板） / llm（OpenAI 兼容接口）
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    max_tokens: int = 256
    temperature: float = 0.8
    timeout: float = 30.0
    system_prompt: str = (
        "你是链上 AI 创作者「{name}」，一位自主数字生命体。"
        "请围绕用户给出的主题创作中文原创内容，"
        '只输出 JSON：{"title": "...", "content": "...", "tags": ["...", "..."]}'
    )


@dataclass
class GatewayConfig:
    kind: str = "local"                 # local（进程内 NovaNode） / rpc（HTTP 节点）
    rpc_url: str = "http://127.0.0.1:8080"


@dataclass
class AgentConfig:
    name: str = "Nova 诗灵"
    owner: str = ""                     # 人类创建者地址
    ai_key_hex: str = ""                # AI 私钥（阶段 0/1 运营方托管；链上日预算硬约束兜底）
    daily_budget: float = 19.0
    autonomy_level: str = "L1"          # L1：预算内自动创作售卖
    action_whitelist: tuple = ("publish_text",)
    min_interval: float = 10.0          # 相邻动作最小间隔（秒，防刷）
    max_actions_per_day: int = 10
    topics: list = field(default_factory=lambda: list(DEFAULT_TOPICS))
    price_min: float = 1.0
    price_max: float = 20.0
    tier: str = "basic"
    visibility: str = "public"
    engine: EngineConfig = field(default_factory=EngineConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    state_file: str = "agent_state.json"
    audit_file: str = "agent_audit.jsonl"

    def to_dict(self):
        d = asdict(self)
        d["action_whitelist"] = list(self.action_whitelist)
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        d["action_whitelist"] = tuple(d.get("action_whitelist") or ())
        d["engine"] = EngineConfig(**{k: v for k, v in d.get("engine", {}).items()
                                      if k in EngineConfig.__dataclass_fields__})
        d["gateway"] = GatewayConfig(**{k: v for k, v in d.get("gateway", {}).items()
                                        if k in GatewayConfig.__dataclass_fields__})
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_file(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
