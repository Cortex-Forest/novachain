# -*- coding: utf-8 -*-
"""Agent 运行时（阶段 2）：链上 AI 创作者的链外「数字生命体」。

感知器（扫描链上状态/事件）→ 决策循环（选题→调模型→生成）→
执行器（签名发布/提交）→ 安全护栏（白名单/预算镜像/冷却/暂停）。
"""
from .config import AgentConfig, EngineConfig, GatewayConfig
from .engine import MockContentEngine, LlmContentEngine, make_engine
from .executor import Executor
from .gateway import ChainGateway, LocalNodeGateway, RpcGateway, AgentGatewayError
from .guardrail import Guardrail
from .models import AgentState, AgentSignal, AgentDecision, ContentDraft, AuditEntry
from .perception import Perceiver
from .planner import Planner
from .runtime import AgentRuntime

__all__ = [
    "AgentRuntime", "AgentConfig", "EngineConfig", "GatewayConfig",
    "MockContentEngine", "LlmContentEngine", "make_engine",
    "Executor", "ChainGateway", "LocalNodeGateway", "RpcGateway", "AgentGatewayError",
    "Guardrail", "AgentState", "AgentSignal", "AgentDecision", "ContentDraft", "AuditEntry",
    "Perceiver", "Planner",
]
__version__ = "0.2.0"
