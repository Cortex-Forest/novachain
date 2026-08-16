# -*- coding: utf-8 -*-
"""AI 生成服务接入层（提示词 3）——AI 音乐人自动创作与收益分账。

覆盖：
1. 主流 AI 服务接入：Suno API（AI音乐）/ OpenAI API（文本/图像）/ Stable Diffusion API（图像）/
   自定义模型；链上登记服务元数据（模型、端点指纹），API Key 不上链。
2. AI 音乐人自动创作循环：
   - 定时触发（每日/每周，时间可配置，链上配置 nova:ai:muso:config）；
   - 离线圈子（scripts/ai_musician_loop.py）到点后调用 Suno API 生成歌曲、
     上传 IPFS（CID 上链）、创建内容合约作品（nova:ai:work:create）；
   - 自动设定售价：基于历史销量与热度（suggest_price，链上确定性计算）。
3. 收益自动分账（写死在合约中）：
   - 创作者（人类/AI）70% / 算力提供节点 20% / AI 成长基金 10%；
   - 算力份额优先按生成任务的执行节点分配，无执行节点时进入算力池。
4. AI 成长基金：
   - 基金地址由合约控制（0x_ai_growth_fund），任何人可查询余额与收支记录；
   - 基金用途：购买更多算力、训练更好模型（nova:ai:fund:spend，仅基金监护人可操作）。
5. 前端展示：AI 音乐人专区（作品列表/试听/购买）、AI 状态面板（今日生成/累计销量/基金余额）、
   一键触发 AI 创作（社区成员付费触发，nova:ai:trigger）。
"""
import time

from core.compute import reference_price, COMPUTE_POOL

AI_SERVICE_TYPES = ("suno", "openai", "stable_diffusion", "custom")
SERVICE_TASK_TYPES = {"suno": "ai_music", "openai": "ai_text",
                      "stable_diffusion": "ai_image", "custom": "custom"}
AI_FUND = "0x_ai_growth_fund"         # 基金地址由合约控制
REV_CREATOR = 0.70                    # 创作者 70%
REV_COMPUTE = 0.20                    # 算力提供节点 20%
REV_FUND = 0.10                       # AI 成长基金 10%
TRIGGER_FEE = 2.0                     # 社区一键触发 AI 创作费用
FUND_SINGLE_SPEND_LIMIT = 20.0       # 单监护人单笔/单日支出上限（H-04，超过须双监护人审批）
FUND_APPROVALS_REQUIRED = 2        # 大额支出所需审批监护人数量
FUND_PENDING_EXPIRE = 7 * 86400    # 待审批支出 7 天未达成自动作废
AI_WORK_PRICE_MIN = 0.1
AI_WORK_PRICE_MAX = 50.0
SVC_NAME_MAX = 64
SVC_MODEL_MAX = 64
SVC_ENDPOINT_HASH_MAX = 128
WORK_TITLE_MAX = 128
WORK_META_MAX = 512
LEDGER_LIMIT = 500


class AIService:
    """AI 生成服务接入层：服务登记 / AI 音乐人 / 分账 / 成长基金。"""

    def __init__(self, store, economy, compute_market, socialfi):
        self.store = store
        self.economy = economy
        self.compute = compute_market
        self.socialfi = socialfi
        self._init_state()

    def _init_state(self):
        if not self.store.ai_muso:
            self.store.ai_muso = {
                "enabled": False, "schedule": "daily", "hour": 0, "weekday": 0,
                "last_run": 0.0, "last_run_day": "", "due": False,
                "today_count": 0, "total_generated": 0, "total_sales": 0,
                "total_revenue": 0.0, "created_at": time.time(),
            }
        self.store.ai_fund_guardians = set(self.store.ai_fund_guardians or [])

    # ======================================================================
    # AI 服务登记（提示词 3.1）
    # ======================================================================
    def validate_svc_register(self, d, addr) -> tuple:
        svc_type = d.get("service_type", "")
        if svc_type not in AI_SERVICE_TYPES:
            return False, "不支持的 AI 服务类型"
        name = d.get("name", "")
        if not (isinstance(name, str) and 0 < len(name.strip()) <= SVC_NAME_MAX):
            return False, "服务名称无效"
        model = d.get("model", "")
        if not (isinstance(model, str) and 0 < len(model.strip()) <= SVC_MODEL_MAX):
            return False, "模型名称无效"
        eh = d.get("endpoint_hash", "")
        if not (isinstance(eh, str) and 0 < len(eh) <= SVC_ENDPOINT_HASH_MAX):
            return False, "端点指纹无效"
        return True, "ok"

    def svc_register(self, addr, svc_id, d) -> dict:
        self.store.ai_services[svc_id] = {
            "id": svc_id, "owner": addr, "service_type": d["service_type"],
            "name": d["name"].strip(), "model": d["model"].strip(),
            "endpoint_hash": d["endpoint_hash"], "status": "active",
            "created_at": time.time(),
        }
        return self.store.ai_services[svc_id]

    def validate_svc_config(self, d, addr) -> tuple:
        svc = self.store.ai_services.get(d.get("svc_id", ""))
        if not svc or svc["owner"] != addr:
            return False, "服务不存在或非所有者"
        if d.get("action") not in ("pause", "resume"):
            return False, "动作无效"
        return True, "ok"

    def svc_config(self, addr, d):
        svc = self.store.ai_services[d["svc_id"]]
        svc["status"] = "paused" if d["action"] == "pause" else "active"
        svc["updated_at"] = time.time()
        return svc

    # ======================================================================
    # AI 音乐人循环配置（提示词 3.2）
    # ======================================================================
    def validate_muso_config(self, d, addr) -> tuple:
        if not self.socialfi.ai_identity(addr):
            return False, "仅 AI 创作者可配置音乐人循环"
        enabled = d.get("enabled")
        if not isinstance(enabled, bool):
            return False, "enabled 无效"
        schedule = d.get("schedule", "daily")
        if schedule not in ("daily", "weekly"):
            return False, "schedule 需为 daily/weekly"
        hour = d.get("hour", 0)
        if not (isinstance(hour, (int, float)) and not isinstance(hour, bool) and 0 <= hour <= 23):
            return False, "hour 无效"
        weekday = d.get("weekday", 0)
        if not (isinstance(weekday, (int, float)) and not isinstance(weekday, bool) and 0 <= weekday <= 6):
            return False, "weekday 无效"
        budget = d.get("budget", 0)
        if not (isinstance(budget, (int, float)) and not isinstance(budget, bool) and 0 <= budget <= 10000):
            return False, "budget 无效"
        return True, "ok"

    def muso_config(self, addr, d):
        m = self.store.ai_muso
        m["enabled"] = bool(d["enabled"])
        m["schedule"] = d.get("schedule", "daily")
        m["hour"] = int(d.get("hour", 0))
        m["weekday"] = int(d.get("weekday", 0))
        m["budget"] = float(d.get("budget", 0))
        m["updated_at"] = time.time()
        return m

    def muso_is_due(self, now: float = None) -> bool:
        """链上判定到点：由维护循环标记 due，离线圈子读取后执行生成。"""
        m = self.store.ai_muso
        return bool(m.get("enabled") and m.get("due"))

    def muso_take_due(self, addr: str) -> bool:
        """离线圈子确认执行（标记该轮已消费），并滚动 last_run。"""
        if not self.muso_is_due():
            return False
        m = self.store.ai_muso
        m["due"] = False
        m["last_run"] = time.time()
        m["last_run_day"] = time.strftime("%Y-%m-%d")
        return True

    def suggest_price(self, task_type: str = "ai_music", sales: int = 0,
                      age_days: float = 0.0) -> float:
        """自动定价：基于参考价区间 + 历史销量/热度（确定性链上计算）。"""
        ref = reference_price(task_type)
        base = (ref["min"] + ref["max"]) / 2 if ref.get("found") else 1.0
        if sales >= 10:
            base *= 1.5
        elif sales >= 5:
            base *= 1.3
        elif sales >= 1:
            base *= 1.15
        if age_days >= 14 and sales == 0:
            base *= 0.8
        return round(max(AI_WORK_PRICE_MIN, min(base, AI_WORK_PRICE_MAX)), 4)

    # ======================================================================
    # 作品上架 / 购买分账（提示词 3.2 / 3.3）
    # ======================================================================
    def validate_work_create(self, d, addr) -> tuple:
        if not self.socialfi.ai_identity(addr):
            return False, "仅 AI 创作者可上架作品"
        title = d.get("title", "")
        if not (isinstance(title, str) and 0 < len(title.strip()) <= WORK_TITLE_MAX):
            return False, "作品标题无效"
        cid = d.get("cid", "")
        if not (isinstance(cid, str) and 0 < len(cid) <= 128):
            return False, "内容地址（IPFS CID）无效"
        price = d.get("price")
        if price is not None and not (isinstance(price, (int, float)) and not isinstance(price, bool)
                                      and AI_WORK_PRICE_MIN <= price <= AI_WORK_PRICE_MAX):
            return False, "售价无效"
        meta = d.get("meta", "")
        if not (isinstance(meta, str) and len(meta) <= WORK_META_MAX):
            return False, "元数据无效"
        task_id = d.get("task_id", "")
        if task_id and task_id not in self.store.compute_tasks:
            return False, "关联算力任务不存在"
        trigger_id = d.get("trigger_id", "")
        if trigger_id and trigger_id not in self.store.ai_triggers:
            return False, "触发记录不存在"
        return True, "ok"

    def work_create(self, addr, wid, d):
        sales = 0
        for w in self.store.ai_works.values():
            if w["artist"] == addr:
                sales += int(w.get("sales", 0))
        price = d.get("price")
        if price is None:
            price = self.suggest_price(d.get("task_type", "ai_music"), sales)
        work = {
            "id": wid, "title": d["title"].strip(), "artist": addr,
            "cid": d["cid"], "price": round(float(price), 4),
            "task_id": d.get("task_id", ""), "task_type": d.get("task_type", "ai_music"),
            "trigger_id": d.get("trigger_id", ""),
            "sales": 0, "revenue": 0.0, "compute_paid": 0.0,
            "meta": d.get("meta", ""), "created_at": time.time(),
        }
        self.store.ai_works[wid] = work
        if d.get("trigger_id"):
            tr = self.store.ai_triggers[d["trigger_id"]]
            tr["status"] = "done"
            tr["work_id"] = wid
        m = self.store.ai_muso
        m["today_count"] = int(m.get("today_count", 0)) + 1
        m["total_generated"] = int(m.get("total_generated", 0)) + 1
        m["last_run"] = time.time()
        m["last_run_day"] = time.strftime("%Y-%m-%d")
        self._fund_ledger("income", "work_publish", d.get("trigger_id") or wid,
                          addr, 0.0, "作品上架「" + work["title"] + "」售价 " + str(work["price"]) + " NOVA")
        return work

    def validate_work_buy(self, d, addr, amount) -> tuple:
        work = self.store.ai_works.get(d.get("wid", ""))
        if not work:
            return False, "作品不存在"
        if addr == work["artist"]:
            return False, "不能购买自己的作品"
        if not (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                and abs(amount - work["price"]) < 1e-6):
            return False, "金额需与售价一致"
        return True, "ok"

    def work_buy(self, buyer, wid, amount):
        work = self.store.ai_works[wid]
        creator_share = round(amount * REV_CREATOR, 8)
        compute_share = round(amount * REV_COMPUTE, 8)
        fund_share = round(amount * REV_FUND, 8)
        self.store.balances[work["artist"]] = self.store.balances.get(work["artist"], 0.0) + creator_share
        # 算力 20%：优先按生成任务的执行节点信誉权重分配
        task = self.store.compute_tasks.get(work.get("task_id", ""))
        workers = task.get("paid_workers", []) if task else []
        if workers:
            weights = {w: 1.0 + self.compute.rep_bonus(w) for w in workers}
            total_w = sum(weights.values())
            for w in workers:
                amt = round(compute_share * weights[w] / total_w, 8)
                self.store.balances[w] = self.store.balances.get(w, 0.0) + amt
                self.compute._touch_stats(w, task_reward=amt)
        else:
            self.store.balances[COMPUTE_POOL] = (
                self.store.balances.get(COMPUTE_POOL, 0.0) + compute_share)
        self.store.balances[AI_FUND] = self.store.balances.get(AI_FUND, 0.0) + fund_share
        self._fund_ledger("income", "work_sale", wid, buyer, amount,
                          "购买「" + work["title"] + "」分账：70%创作者/20%算力/10%基金")
        work["sales"] = int(work.get("sales", 0)) + 1
        work["revenue"] = round(float(work.get("revenue", 0.0)) + amount, 8)
        work["compute_paid"] = round(float(work.get("compute_paid", 0.0)) + compute_share, 8)
        m = self.store.ai_muso
        m["total_sales"] = int(m.get("total_sales", 0)) + 1
        m["total_revenue"] = round(float(m.get("total_revenue", 0.0)) + amount, 8)
        return {"ok": True, "creator": creator_share, "compute": compute_share,
                "fund": fund_share}

    # ======================================================================
    # 一键触发（提示词 3.5）
    # ======================================================================
    def validate_trigger(self, d, addr, amount) -> tuple:
        svc_type = d.get("service_type", "suno")
        if svc_type not in AI_SERVICE_TYPES:
            return False, "不支持的 AI 服务类型"
        if not (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                and abs(amount - TRIGGER_FEE) < 1e-6):
            return False, "触发费用需为 " + str(TRIGGER_FEE) + " NOVA"
        return True, "ok"

    def trigger(self, addr, txid, d, amount):
        self.store.balances[AI_FUND] = self.store.balances.get(AI_FUND, 0.0) + amount
        self.store.ai_triggers[txid] = {
            "id": txid, "by": addr, "amount": round(float(amount), 8),
            "service_type": d.get("service_type", "suno"),
            "status": "pending", "created_at": time.time(),
        }
        self._fund_ledger("income", "trigger", txid, addr, amount,
                          "社区付费触发 AI 创作（" + d.get("service_type", "suno") + "）")
        return self.store.ai_triggers[txid]

    # ======================================================================
    # AI 成长基金（提示词 3.4）
    # ======================================================================
    def validate_fund_guard(self, d, addr) -> tuple:
        target = d.get("addr", "")
        if not (isinstance(target, str) and target.startswith("0x") and 32 <= len(target) <= 42):
            return False, "目标地址无效"
        if addr not in (self.store.ai_fund_guardians or set()) and not self.socialfi.ai_identity(addr):
            return False, "仅 AI 创作者或现有基金监护人可授权"
        return True, "ok"

    def fund_guard(self, addr, d):
        self.store.ai_fund_guardians.add(d["addr"])
        return sorted(self.store.ai_fund_guardians)

    def validate_fund_spend(self, d, addr, amount) -> tuple:
        if addr not in (self.store.ai_fund_guardians or set()):
            return False, "仅基金监护人可支出"
        recipient = d.get("recipient", "")
        if not (isinstance(recipient, str) and recipient.startswith("0x")):
            return False, "收款地址无效"
        purpose = d.get("purpose", "")
        if not (isinstance(purpose, str) and 0 < len(purpose) <= 128):
            return False, "用途说明无效"
        if not (isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0):
            return False, "金额无效"
        if self.store.balances.get(AI_FUND, 0.0) < amount:
            return False, "基金余额不足"
        if amount <= FUND_SINGLE_SPEND_LIMIT:
            # 小额支出受单监护人单日上限约束（H-04：单监护人不能全量提走）
            day = time.strftime("%Y-%m-%d", time.localtime())
            spent = float(self.store.ai_fund_spend_day.get(day + "|" + addr, 0.0))
            if spent + float(amount) > FUND_SINGLE_SPEND_LIMIT:
                return False, "超过单监护人单日支出上限（" + str(FUND_SINGLE_SPEND_LIMIT) + " NOVA）"
        return True, "ok"

    def fund_spend(self, addr, d, amount):
        amount = float(amount)
        if amount > FUND_SINGLE_SPEND_LIMIT:
            # 大额支出：创建待审批记录（发起人视为第一票）
            seq = self.store.ai_fund_pending_seq + 1
            self.store.ai_fund_pending_seq = seq
            pid = "spend_" + str(seq)
            self.store.ai_fund_pending[pid] = {
                "id": pid, "amount": round(amount, 8), "recipient": d["recipient"],
                "purpose": d["purpose"], "by": addr, "approvals": [addr],
                "created_at": time.time(),
            }
            self._fund_ledger("pending", "fund_spend_pending", pid, addr, amount,
                              "大额基金支出待审批：" + d["purpose"])
            return {"ok": True, "pending": pid, "amount": amount, "status": "pending"}
        # 小额支出：即时转账 + 记录单日额度
        self.store.balances[AI_FUND] = self.store.balances.get(AI_FUND, 0.0) - amount
        self.store.balances[d["recipient"]] = self.store.balances.get(d["recipient"], 0.0) + amount
        day = time.strftime("%Y-%m-%d", time.localtime())
        key = day + "|" + addr
        self.store.ai_fund_spend_day[key] = float(self.store.ai_fund_spend_day.get(key, 0.0)) + amount
        self._fund_ledger("expense", "fund_spend", d["recipient"], addr, amount,
                          "基金支出：" + d["purpose"])
        return {"ok": True, "recipient": d["recipient"], "amount": amount}

    def validate_fund_approve(self, d, addr) -> tuple:
        pid = d.get("pid", "")
        pend = self.store.ai_fund_pending.get(pid)
        if not pend:
            return False, "待审批支出不存在或已处理"
        if addr not in (self.store.ai_fund_guardians or set()):
            return False, "仅基金监护人可审批"
        if addr in pend["approvals"]:
            return False, "该监护人已审批"
        if time.time() - float(pend.get("created_at", 0)) > FUND_PENDING_EXPIRE:
            return False, "待审批支出已过期"
        return True, "ok"

    def fund_approve(self, addr, d):
        pend = self.store.ai_fund_pending[d["pid"]]
        pend["approvals"].append(addr)
        self._fund_ledger("approval", "fund_approve", pend["id"], addr, 0.0,
                          "审批大额支出：" + pend["purpose"])
        if len(pend["approvals"]) < FUND_APPROVALS_REQUIRED:
            return {"ok": True, "pending": pend["id"], "status": "waiting",
                    "approvals": list(pend["approvals"])}
        # 审批达成：执行转账
        self.store.balances[AI_FUND] = self.store.balances.get(AI_FUND, 0.0) - float(pend["amount"])
        self.store.balances[pend["recipient"]] = (
            self.store.balances.get(pend["recipient"], 0.0) + float(pend["amount"]))
        pid = pend["id"]
        self._fund_ledger("expense", "fund_spend", pend["recipient"], pid, float(pend["amount"]),
                          "基金支出（审批通过）：" + pend["purpose"])
        del self.store.ai_fund_pending[pid]
        return {"ok": True, "recipient": pend["recipient"], "amount": float(pend["amount"]),
                "status": "executed"}

    def _fund_ledger(self, kind: str, event: str, ref: str, addr: str, amount: float, memo: str):
        seq = self.store.ai_fund_seq + 1
        self.store.ai_fund_seq = seq
        eid = "fund_" + str(seq)
        self.store.ai_fund_ledger[eid] = {
            "id": eid, "kind": kind, "event": event, "ref": ref, "addr": addr,
            "amount": round(float(amount), 8), "memo": memo, "at": time.time(),
        }
        if len(self.store.ai_fund_ledger) > LEDGER_LIMIT:
            for k in list(self.store.ai_fund_ledger)[:len(self.store.ai_fund_ledger) - LEDGER_LIMIT]:
                del self.store.ai_fund_ledger[k]

    def fund_view(self, limit: int = 50) -> dict:
        income = sum(float(e["amount"]) for e in self.store.ai_fund_ledger.values()
                     if e["kind"] == "income")
        expense = sum(float(e["amount"]) for e in self.store.ai_fund_ledger.values()
                      if e["kind"] == "expense")
        return {
            "balance": self.store.balances.get(AI_FUND, 0.0),
            "income_total": round(income, 8),
            "expense_total": round(expense, 8),
            "guardians": sorted(self.store.ai_fund_guardians or set()),
            "single_spend_limit": FUND_SINGLE_SPEND_LIMIT,
            "approvals_required": FUND_APPROVALS_REQUIRED,
            "pending": sorted(self.store.ai_fund_pending.values(),
                              key=lambda e: e.get("created_at", 0), reverse=True)[:20],
            "ledger": sorted(self.store.ai_fund_ledger.values(),
                             key=lambda e: e.get("at", 0), reverse=True)[:limit],
        }

    # ======================================================================
    # 维护：每日统计滚动 + 定时触发标记（提示词 3.2）
    # ======================================================================
    def maintain(self, now: float = None):
        now = time.time() if now is None else now
        m = self.store.ai_muso
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if m.get("last_run_day") != today:
            m["today_count"] = 0
        if m.get("enabled"):
            lt = time.localtime(now)
            due = False
            if m["schedule"] == "daily" and lt.tm_hour >= int(m.get("hour", 0)):
                due = True
            elif m["schedule"] == "weekly" and lt.tm_wday == int(m.get("weekday", 0))                     and lt.tm_hour >= int(m.get("hour", 0)):
                due = True
            if m.get("last_run_day") != today and due:
                m["due"] = True
        # H-04：清理过期待审批与陈旧单日统计
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        for k in list(self.store.ai_fund_spend_day):
            if not k.startswith(today + "|"):
                del self.store.ai_fund_spend_day[k]
        for pid in list(self.store.ai_fund_pending):
            p = self.store.ai_fund_pending[pid]
            if now - float(p.get("created_at", 0)) > FUND_PENDING_EXPIRE:
                self._fund_ledger("expired", "fund_spend_expired", pid, p.get("by", ""),
                                  float(p.get("amount", 0.0)), "待审批支出已过期作废")
                del self.store.ai_fund_pending[pid]
        return self.store.ai_muso

    def overview(self) -> dict:
        m = self.store.ai_muso
        return {
            "services": len(self.store.ai_services),
            "works": len(self.store.ai_works),
            "triggers_pending": sum(1 for t in self.store.ai_triggers.values()
                                    if t.get("status") == "pending"),
            "today_generated": m.get("today_count", 0),
            "total_generated": m.get("total_generated", 0),
            "total_sales": m.get("total_sales", 0),
            "total_revenue": round(float(m.get("total_revenue", 0.0)), 8),
            "muso": {k: m.get(k) for k in ("enabled", "schedule", "hour", "weekday",
                                           "budget", "due", "last_run")},
            "fund": self.fund_view(10),
            "split": {"creator": REV_CREATOR, "compute": REV_COMPUTE, "fund": REV_FUND},
            "trigger_fee": TRIGGER_FEE,
        }
