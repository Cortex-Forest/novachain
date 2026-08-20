# -*- coding: utf-8 -*-
"""无感反 FOMO 机制（v0.9）。

设计意图（防大户暴力拉升后砸盘、保护散户不被 FOMO 情绪裹挟）：
- 99% 普通用户感知不到：只对极端投机行为触发，规则事先公开、自动执行、无人工干预。
- 触发条件（只针对极端）：
    * 单地址 24 小时内买入超过 10 万 NOVA
    * 单地址 7 天内累计买入超过 50 万 NOVA
- 触发动作：进入 24 小时冷却，冷却期内只限制「买入」类交易；
  卖出、转账、质押、签到、部署合约及全部娱乐功能不受影响。
- 透明化：触发前零提示（不打扰）；触发后前端经 /api/fomo/status 展示一句温和提示。
  不标记用户、不公开名单、不影响信誉分。
- 自动解除：冷却期固定 24 小时，结束自动恢复、无任何手续；多次触发不累加、不翻倍。
- 确定性：买入记录按交易时间戳（链上确定性）滚动统计，跨节点收敛一致；
  状态随快照持久化同步。

「买入」口径 = 花钱换资产的消费类操作（signed tx，sender == receiver，data 为 {op,...}）：
fan:buy / rev:invest / market:bet / blind:open / curate:buy / bond:buy /
frac:buy / text:buy / ai:work:buy。
"""
import json
import time

WINDOW_24H = 24 * 3600
WINDOW_7D = 7 * 86400
BUY_24H_LIMIT = 100_000.0       # 24h 窗口触发阈值（NOVA）
BUY_7D_LIMIT = 500_000.0        # 7 天窗口触发阈值（NOVA）
COOLDOWN = 24 * 3600            # 冷却期固定 24h（不累加、不翻倍）

# 触发后钱包展示的温和提示文案
COOLDOWN_MESSAGE = (
    "您近期的买入量较大，为保护市场稳定，已自动暂停您的买入功能 24 小时。\n"
    "您仍可正常出售、转账和使用所有 Nova 娱乐功能。"
)

BUY_OPS = (
    "nova:fan:buy", "nova:rev:invest", "nova:market:bet", "nova:blind:open",
    "nova:curate:buy", "nova:bond:buy", "nova:frac:buy", "nova:text:buy",
    "nova:ai:work:buy",
)


def parse_op(tx):
    try:
        d = json.loads(tx.data)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


class AntiFOMO:
    """反 FOMO 冷却状态机：买入记录 + 触发 + 校验 + 查询。"""

    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    @staticmethod
    def is_buy_tx(tx) -> bool:
        d = parse_op(tx)
        if not d:
            return False
        return d.get("op") in BUY_OPS and tx.sender == tx.receiver

    def _window_sum(self, addr, now, window) -> float:
        """按交易时间戳滚动求和：now-window ~ now 内的买入金额。"""
        cutoff = now - window
        total = 0.0
        for ts, amt in self.store.fomo_buys.get(addr, []):
            if ts >= cutoff and ts <= now:
                total += float(amt)
        return total

    def cooldown_until(self, addr) -> float:
        return float(self.store.fomo_cooldown.get(addr, 0.0))

    def cooldown_left(self, addr, now=None) -> float:
        now = now if now is not None else time.time()
        return max(0.0, self.cooldown_until(addr) - now)

    def in_cooldown(self, addr, now=None) -> bool:
        return self.cooldown_left(addr, now) > 0

    # ------------------------------------------------------------------
    # 记录与触发（apply 阶段调用，纯确定性：以最新买入交易时间戳为参照）
    # ------------------------------------------------------------------
    def record_buy(self, tx):
        addr = tx.sender
        rec = self.store.fomo_buys.setdefault(addr, [])
        rec.append([float(tx.timestamp), float(tx.amount)])
        rec.sort(key=lambda r: r[0])
        # 以最新一笔买入的交易时间戳为确定性参照（跨节点一致）
        now = rec[-1][0]
        cutoff = now - WINDOW_7D
        self.store.fomo_buys[addr] = [r for r in rec if r[0] >= cutoff]
        # 已在冷却：不延长（冷却期固定 24h，多次触发不累加不翻倍）
        if self.cooldown_until(addr) > now:
            return
        if self._window_sum(addr, now, WINDOW_24H) > BUY_24H_LIMIT or \
                self._window_sum(addr, now, WINDOW_7D) > BUY_7D_LIMIT:
            self.store.fomo_cooldown[addr] = now + COOLDOWN

    # ------------------------------------------------------------------
    # 校验（validate 阶段调用，纯确定性：交易时间戳 >= 冷却截止才放行）
    # ------------------------------------------------------------------
    def validate_buy(self, tx) -> bool:
        if not self.is_buy_tx(tx):
            return True
        # 冷却期内（该笔交易时间戳早于冷却截止）拒绝买入；
        # 冷却期结束自动恢复，无任何额外手续。
        return float(tx.timestamp) >= self.cooldown_until(tx.sender)

    # ------------------------------------------------------------------
    # 查询（RPC，墙钟仅用于展示剩余时间）
    # ------------------------------------------------------------------
    def status(self, addr, now=None):
        now = now if now is not None else time.time()
        left = self.cooldown_left(addr, now)
        return {
            "addr": addr,
            "cooldown": left > 0,
            "remaining": round(left, 1),
            "message": COOLDOWN_MESSAGE if left > 0 else "",
            "buy_24h": round(self._window_sum(addr, now, WINDOW_24H), 4),
            "buy_7d": round(self._window_sum(addr, now, WINDOW_7D), 4),
            "limits": {"buy_24h": BUY_24H_LIMIT, "buy_7d": BUY_7D_LIMIT, "cooldown_hours": 24},
        }
