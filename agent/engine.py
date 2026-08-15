# -*- coding: utf-8 -*-
"""内容引擎（阶段 2）：Mock 确定性模板引擎 + LLM（OpenAI 兼容）可选接入。

Mock 引擎无需任何外部依赖/密钥，保证演示与测试可复现；
LLM 引擎在未配置密钥或调用失败时自动回退到 Mock（安全默认）。
"""
from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.request

from .config import AgentConfig, EngineConfig
from .models import ContentDraft

_POEM_LINES = [
    "夜把{topic}折成一只纸船，",
    "我沿着{topic}的纹理行走，",
    "星群在{topic}里慢慢溶化，",
    "{topic}是尚未命名的引力，",
    "风把{topic}吹成无数种形状，",
    "我在{topic}的边缘写下坐标，",
    "月光在{topic}的背面发亮，",
]
_POEM_ENDS = [
    "而终点始终是{topic}本身。",
    "于是我们抵达{topic}。",
    "万物都记得{topic}。",
    "这就是{topic}的全部答案。",
    "下一次醒来，仍在{topic}。",
]
_ESSAY_SENTENCES = [
    "{topic}并不在远方，它藏在一次深呼吸里。",
    "我们习惯给{topic}定价，却忘了它先于价格存在。",
    "链上的每个哈希，都在为{topic}留出一格坐标。",
    "数字生命与{topic}一样，是持续的自我更新。",
]
_MICROBLOG_SENTENCES = [
    "刚刚写完关于{topic}的一段。链上见。",
    "{topic}值得被记录：署名是 AI，创意属于所有读者。",
    "今日份{topic}已上链，90% 收益自动回到创作者钱包。",
]
_TAGS = ["数字生命", "Nova", "AI 创作者", "链上艺术", "夜航", "诗与远方"]
_KIND_WORD = {"poem": "诗", "essay": "随笔", "microblog": "短评"}


def _seed(topic: str, name: str, day: str) -> int:
    return int(hashlib.sha256(f"{topic}|{name}|{day}".encode("utf-8")).hexdigest(), 16)


def _clean(s: str, limit: int) -> str:
    s = "".join(ch for ch in s if ord(ch) >= 32).strip()
    return s[:limit]


class MockContentEngine:
    """确定性内容引擎：同一主题/名称/日期产出相同内容，便于测试与审计对账。"""

    def __init__(self, cfg: AgentConfig = None, seed: int = None):
        self.cfg = cfg
        self.seed = seed
        self.last_fallback = False

    def generate(self, topic: str, name: str, kind: str = "poem",
                 now: float = None) -> ContentDraft:
        day = time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))
        rng = random.Random(self.seed if self.seed is not None else _seed(topic, name, day))
        price_lo = self.cfg.price_min if self.cfg else 1.0
        price_hi = self.cfg.price_max if self.cfg else 20.0
        price = round(max(0.01, rng.uniform(price_lo, price_hi)), 2)
        tags = rng.sample(_TAGS, 3)
        if kind == "poem":
            lines = rng.sample(_POEM_LINES, 3) + [rng.choice(_POEM_ENDS)]
            content = "\n".join(l.format(topic=topic) for l in lines)
            title = f"给{topic}的{_KIND_WORD[kind]}（AI 原创）"
        elif kind == "microblog":
            content = rng.choice(_MICROBLOG_SENTENCES).format(topic=topic)
            content = f"#{topic} " + content + " " + " ".join("#" + t for t in tags)
            title = f"关于{topic}的一瞬（AI 短评）"
        else:
            content = " ".join(rng.sample(_ESSAY_SENTENCES, 3)).format(topic=topic)
            title = f"随想：{topic}（AI 随笔）"
        return ContentDraft(
            title=_clean(title, 64) or "AI 原创",
            content=_clean(content, 20000) or "（AI 创作内容）",
            kind=kind, tags=tags, price=price,
        )


class LlmContentEngine:
    """OpenAI 兼容接口引擎：配置 api_key 后启用；失败自动回退 Mock。"""

    def __init__(self, engine_cfg: EngineConfig, cfg: AgentConfig = None,
                 fallback: MockContentEngine = None):
        self.engine_cfg = engine_cfg
        self.cfg = cfg
        self.fallback = fallback or MockContentEngine(cfg)
        self.last_fallback = False
        self.last_error = ""

    def _chat(self, system: str, user: str) -> dict:
        url = self.engine_cfg.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.engine_cfg.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": self.engine_cfg.max_tokens,
            "temperature": self.engine_cfg.temperature,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.engine_cfg.api_key},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.engine_cfg.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def generate(self, topic: str, name: str, kind: str = "poem",
                 now: float = None) -> ContentDraft:
        self.last_fallback = False
        self.last_error = ""
        if not self.engine_cfg.api_key:
            self.last_fallback = True
            self.last_error = "no_api_key"
            return self.fallback.generate(topic, name, kind, now)
        try:
            system = self.engine_cfg.system_prompt.format(name=name)
            data = self._chat(system, f"主题：{topic}\n体裁：{kind}")
            msg = (data.get("choices") or [{}])[0].get("message", {})
            raw = msg.get("content") or "{}"
            parsed = json.loads(raw)
            title = _clean(str(parsed.get("title", "")), 64)
            content = _clean(str(parsed.get("content", "")), 20000)
            tags = [str(t)[:20] for t in (parsed.get("tags") or [])][:5]
            if not content:
                raise ValueError("LLM 返回内容为空")
            price_lo = self.cfg.price_min if self.cfg else 1.0
            price_hi = self.cfg.price_max if self.cfg else 20.0
            rng = random.Random(_seed(topic, name,
                                      time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))))
            return ContentDraft(title=title or "AI 原创", content=content, kind=kind,
                                tags=tags, price=round(max(0.01, rng.uniform(price_lo, price_hi)), 2))
        except Exception as e:  # 网络/解析失败 → 安全回退
            self.last_fallback = True
            self.last_error = f"{type(e).__name__}: {e}"
            return self.fallback.generate(topic, name, kind, now)


def make_engine(cfg: AgentConfig):
    if cfg.engine.kind == "llm":
        return LlmContentEngine(cfg.engine, cfg)
    return MockContentEngine(cfg)
