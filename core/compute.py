# -*- coding: utf-8 -*-
"""去中心化算力网络核心（Compute Network / 算力网络核心架构）。

覆盖五份需求：
1. 算力网络核心架构
   - 算力节点注册：声明 CPU 核心数 / GPU 型号 / 显存 / 内存 / 可用存储，规格写入链上公开可查；
     超级节点（验证者 / 矿工 / 存储节点）自动具备算力提供资格，无需额外注册。
   - 任务类型：ai_music（高GPU）/ ai_image（中GPU）/ game_server（低GPU高内存）/
     video_transcode（中CPU低GPU）/ data_clean（低CPU高内存），各带需求规格与参考价。
   - 任务生命周期状态机：open → bidding/assigned → submitted → arbitrating →
     settled → completed；另有 expired / failed / disputed，每个阶段均有链上状态记录。
   - 节点信誉分：初始 50、满分 100，由完成率 / 结果正确性 / 响应速度 / 投诉与作恶综合计算；
     决定接单优先级、质押要求与最大任务金额；低于阈值自动降级为轻量任务提供者。
   - 调度策略：信誉高、延迟低、报价合理的节点优先；支持一任务多节点冗余执行。
2. 算力任务市场
   - 发布任务（类型 / 需求规格 / 预算 / 截止时间 / 验收标准），预算全额质押进托管；
   - 抢单模式（先到先得、固定价格）与竞价模式（节点报价、发起者挑选）；
   - 结果提交：结果哈希 + 结果存储地址（IPFS），合约记录提交时间戳；
   - 双节点冗余验证：哈希一致通过，不一致引入第三节点仲裁；验证通过自动从托管支付，
     失败扣信誉并退回发起者；1% 手续费回流验证者激励池；
   - 争议处理：收到结果后 24 小时内可提出异议，进入社区仲裁，仲裁期间预算冻结。
3. 算力验证与防作弊
   - 接单前必须质押与任务预算等额的 NOVA；提交虚假结果 → 罚没质押 + 信誉分清零；
   - 串通检测：两节点结果一致但被证明错误 → 双双罚没；
   - 随机抽查：确定性选取约 5% 已完成任务由第三方节点重跑，发现错误罚双倍质押；
   - 算力证明：接单时提交 GPU 型号 / 显存等规格，超规格接单视为作恶被拒。
4. 算力节点激励与经济模型
   - 收益 = 任务报酬（大头）+ 出块奖励（验证者激励池，存储 40% / 算力 60%）+ 信誉加成 5-15%；
   - 质押 100-10000 NOVA，解质押 7 天冷静期，作恶罚没；
   - 参考价：AI音乐生成 0.5-2 NOVA/首、AI图像 0.1-0.5 NOVA/张，实际价格由市场竞价决定；
   - 收益统计接口：node_view / node_income / overview。

兼容性：legacy 任务（发布时不带 task_type 的旧式任务）保持原有「双节点结果一致平分悬赏」
语义；其余任务走完整生命周期状态机。
"""
import hashlib
import re
import time

RESULT_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# IPFS CIDv0 / CIDv1 / 链上十六进制引用
IPFS_RE = re.compile(r"^(Qm[1-9A-HJ-NP-Za-km-z]{44}|bafy[A-Za-z0-9]{50,}|0x[0-9a-fA-F]{40,})$")
MAX_WORKERS = 8
MAX_SPEC_LEN = 4096
MIN_EXPIRES = 300                     # 任务最短有效期：5 分钟
MAX_EXPIRES = 90 * 86400              # 任务最长有效期：90 天

# ---------------------------------------------------------------------------
# 任务类型：需求规格 + 参考价（提示词 1 / 5）
# ---------------------------------------------------------------------------
TASK_TYPES = {
    "ai_music":        {"name": "AI音乐生成",    "gpu": "high", "min_cpu": 4, "min_ram_gb": 16, "min_storage_gb": 20, "price_min": 0.5,  "price_max": 2.0},
    "ai_image":        {"name": "AI图像生成",    "gpu": "mid",  "min_cpu": 4, "min_ram_gb": 8,  "min_storage_gb": 10, "price_min": 0.1,  "price_max": 0.5},
    "game_server":     {"name": "游戏服务器托管", "gpu": "low",  "min_cpu": 4, "min_ram_gb": 32, "min_storage_gb": 50, "price_min": 0.1,  "price_max": 1.0},
    "video_transcode": {"name": "视频转码",      "gpu": "low",  "min_cpu": 8, "min_ram_gb": 8,  "min_storage_gb": 50, "price_min": 0.05, "price_max": 0.5},
    "data_clean":      {"name": "数据清洗/标注", "gpu": "low",  "min_cpu": 2, "min_ram_gb": 16, "min_storage_gb": 10, "price_min": 0.01, "price_max": 0.1},
}
GPU_TIERS = {"none": 0, "low": 1, "mid": 2, "high": 3}
_GPU_HIGH = ("a100", "a800", "h100", "h200", "b100", "b200", "rtx4090", "4090", "l40", "l40s", "a6000", "mi300")
_GPU_MID = ("a40", "a5000", "rtx4080", "4080", "rtx4070", "4070", "rtx3090", "3090", "rtx3080", "3080",
            "v100", "t4", "l4", "a10", "a16", "mi250")
_GPU_LOW = ("rtx3060", "3060", "rtx2060", "2060", "gtx", "p40", "p100", "k80", "mx")

DEFAULT_SPEC = {"cpu_cores": 8, "gpu_model": "auto", "gpu_vram_gb": 8.0,
                "ram_gb": 16.0, "storage_gb": 100.0, "region": "auto", "latency_ms": 50.0}
MAX_CPU = 1024
MAX_RAM_GB = 4096.0
MAX_STORAGE_GB = 1048576.0
MAX_GPU_VRAM_GB = 512.0
MAX_LATENCY_MS = 60000.0
SPEC_LEN_MAX = 64

# ---------------------------------------------------------------------------
# 信誉分（提示词 1 / 4）
# ---------------------------------------------------------------------------
REP_INIT = 50.0
REP_MAX = 100.0
REP_COMPLETE = 1.0
REP_CORRECT = 2.0
REP_WRONG = -10.0                     # 仲裁/抽查判定结果错误
REP_COMPLAIN = -10.0                  # 被投诉
REP_CHEAT = -100.0                    # 作恶：信誉分清零
# (下限, 等级名, 信誉加成, 质押系数, 最大任务金额)
REP_TIERS = (
    (80.0, "恒星节点", 0.15, 1.0, 10000.0),
    (60.0, "星核节点", 0.10, 0.8, 2000.0),
    (40.0, "星云节点", 0.05, 0.6, 500.0),
    (0.0,  "轻量节点", 0.00, 0.3, 50.0),
)
LIGHT_TIER_THRESHOLD = 40.0           # 低于此阈值自动降级为轻量任务提供者

# ---------------------------------------------------------------------------
# 质押 / 手续费 / 抽查 / 争议（提示词 2 / 4 / 5）
# ---------------------------------------------------------------------------
MIN_COMPUTE_STAKE = 100.0
MAX_COMPUTE_STAKE = 10000.0
UNBOND_COMPUTE = 7 * 86400            # 解质押 7 天冷静期
MARKET_FEE_RATE = 0.01                # 每笔任务成交收 1% 手续费 → 验证者激励池
AUDIT_RATE = 0.05                     # 随机抽查比例 5%
AUDIT_SLASH_MULT = 2.0                # 抽查发现错误 → 罚没双倍质押
AUDIT_REWARD = 0.5                    # 审计节点奖励（生态基金支付）
DISPUTE_WINDOW = 24 * 3600            # 收到结果后 24 小时内可异议
DISPUTE_QUORUM = 3                    # 社区仲裁投票阈值
COMPUTE_POOL = "0x_compute_pool"      # 算力贡献份额暂存地址（无明确执行节点的分账）
EVENT_LIMIT = 500

# 生命周期状态（每阶段链上记录）
LIFECYCLE = ("open", "bidding", "assigned", "submitted", "arbitrating",
             "settled", "completed", "expired", "failed", "disputed")


def gpu_tier(model: str) -> int:
    """把节点声明的 GPU 型号映射为档位：high=3 / mid=2 / low=1 / none=0。"""
    m = (model or "").lower().replace(" ", "")
    if not m or m == "auto":
        return 0
    for token in _GPU_HIGH:
        if token in m:
            return 3
    for token in _GPU_MID:
        if token in m:
            return 2
    for token in _GPU_LOW:
        if token in m:
            return 1
    return 0


def reference_price(task_type: str) -> dict:
    """算力价格指导：参考价区间，实际价格由市场竞价决定。"""
    t = TASK_TYPES.get(task_type)
    if not t:
        return {"found": False}
    return {"found": True, "task_type": task_type, "name": t["name"],
            "min": t["price_min"], "max": t["price_max"]}


def spec_meets(spec: dict, task_type: str) -> tuple:
    """校验节点算力规格是否满足任务需求；超规格接单视为作恶（校验拒绝）。"""
    t = TASK_TYPES.get(task_type)
    if not t:
        return False, "未知任务类型"
    cpu = float(spec.get("cpu_cores", 0))
    ram = float(spec.get("ram_gb", 0))
    storage = float(spec.get("storage_gb", 0))
    if cpu < t["min_cpu"]:
        return False, "CPU 核心不足：需要 " + str(t["min_cpu"]) + "，实际 " + str(int(cpu))
    if ram < t["min_ram_gb"]:
        return False, "内存不足：需要 " + str(t["min_ram_gb"]) + "GB，实际 " + str(ram) + "GB"
    if storage < t["min_storage_gb"]:
        return False, "存储不足：需要 " + str(t["min_storage_gb"]) + "GB，实际 " + str(storage) + "GB"
    need = GPU_TIERS[t["gpu"]]
    if need >= 2 and gpu_tier(spec.get("gpu_model", "")) < need:
        return False, "GPU 档位不足：需要 " + t["gpu"] + "（" + str(spec.get("gpu_model", "无")) + "）"
    return True, "ok"


class ComputeMarket:
    """算力网络状态机：节点注册 / 信誉 / 市场 / 验证 / 审计 / 激励。"""

    def __init__(self, store, economy):
        self.store = store
        self.economy = economy

    # ======================================================================
    # 节点注册与算力规格（提示词 1）
    # ======================================================================
    def register(self, addr: str, cpu_cores, gpu_model: str, gpu_vram_gb,
                 ram_gb, storage_gb, region: str = "", latency_ms: float = 50.0) -> dict:
        spec = {
            "cpu_cores": int(cpu_cores),
            "gpu_model": str(gpu_model).strip(),
            "gpu_vram_gb": round(float(gpu_vram_gb), 2),
            "ram_gb": round(float(ram_gb), 2),
            "storage_gb": round(float(storage_gb), 2),
            "region": str(region).strip(),
            "latency_ms": round(float(latency_ms), 1),
            "updated_at": time.time(),
        }
        old = self.store.compute_nodes.get(addr, {})
        spec["registered_at"] = old.get("registered_at", time.time())
        self.store.compute_nodes[addr] = spec
        self._ensure_stats(addr)
        self._emit("node_register", addr, "", "",
                   "算力节点已注册/更新规格（" + str(spec["cpu_cores"]) + " 核 / "
                   + (spec["gpu_model"] or "无GPU") + " / " + str(spec["ram_gb"]) + "GB）")
        return spec

    def is_super_node(self, addr: str) -> bool:
        """超级节点自动具备算力提供资格：验证者（质押）、矿工、存储节点。"""
        return (addr in self.store.stakes or addr in self.store.miner_registry
                or addr in self.store.inc_nodes)

    def is_qualified_node(self, addr: str) -> bool:
        return addr in self.store.compute_nodes or self.is_super_node(addr)

    def node_spec(self, addr: str) -> dict:
        """公开可查的算力规格；超级节点未注册时返回默认规格并标记自动资格。"""
        if addr in self.store.compute_nodes:
            return dict(self.store.compute_nodes[addr])
        if self.is_super_node(addr):
            spec = dict(DEFAULT_SPEC)
            spec["auto_qualified"] = True
            return spec
        return {}

    def _ensure_stats(self, addr: str):
        self.store.compute_stats.setdefault(addr, {
            "completed": 0, "correct": 0, "wrong": 0, "complaints": 0, "cheated": 0,
            "task_reward": 0.0, "bonus_reward": 0.0, "block_reward": 0.0, "audit_reward": 0.0,
            "response_sum": 0.0, "responses": 0, "first_seen": time.time(),
        })

    # ======================================================================
    # 信誉分（提示词 1 / 4）
    # ======================================================================
    def compute_reputation(self, addr: str) -> dict:
        st = self.store.compute_stats.get(addr, {})
        score = (REP_INIT
                 + float(st.get("completed", 0)) * REP_COMPLETE
                 + float(st.get("correct", 0)) * REP_CORRECT
                 + float(st.get("wrong", 0)) * REP_WRONG
                 + float(st.get("complaints", 0)) * REP_COMPLAIN
                 + float(st.get("cheated", 0)) * REP_CHEAT)
        score = max(0.0, min(score, REP_MAX))
        tier_name, bonus, stake_factor, max_budget = "轻量节点", 0.0, 0.3, 50.0
        for lo, name, b, sf, mb in REP_TIERS:
            if score >= lo:
                tier_name, bonus, stake_factor, max_budget = name, b, sf, mb
                break
        total = st.get("completed", 0)
        rate = (st.get("correct", 0) / total) if total else 1.0
        resp = st.get("responses", 0)
        avg_resp = (st.get("response_sum", 0.0) / resp) if resp else 0.0
        return {
            "addr": addr, "score": round(score, 2),
            "tier": tier_name, "bonus": bonus, "stake_factor": stake_factor,
            "max_budget": max_budget, "completion_rate": round(rate, 4),
            "avg_response_ms": round(avg_resp, 1),
            "stats": {k: st.get(k, 0) for k in ("completed", "correct", "wrong",
                                                "complaints", "cheated")},
        }

    def rep_bonus(self, addr: str) -> float:
        return self.compute_reputation(addr)["bonus"]

    def _touch_stats(self, addr: str, **kw):
        self._ensure_stats(addr)
        for k, v in kw.items():
            if k in ("task_reward", "bonus_reward", "block_reward", "audit_reward"):
                self.store.compute_stats[addr][k] = round(
                    float(self.store.compute_stats[addr].get(k, 0.0)) + float(v), 8)
            else:
                self.store.compute_stats[addr][k] = (
                    int(self.store.compute_stats[addr].get(k, 0)) + int(v))

    def _record_response(self, addr: str, task: dict):
        if task.get("assigned_at"):
            dt = max(0.0, time.time() - float(task["assigned_at"]))
            st = self.store.compute_stats.setdefault(addr, {})
            st["response_sum"] = float(st.get("response_sum", 0.0)) + dt
            st["responses"] = int(st.get("responses", 0)) + 1

    # ======================================================================
    # 任务市场：发布（提示词 1 / 2）
    # ======================================================================
    def publish(self, creator: str, spec: str, bounty: float, expires_in: float, task_id: str,
                task_type=None, mode="grab", min_nodes=2, acceptance="") -> dict:
        """发布任务：预算全额质押进托管。task_type 为空 = legacy 旧式任务。"""
        self.store.balances[creator] = self.store.balances.get(creator, 0) - float(bounty)
        task = {
            "creator": creator,
            "spec": spec,
            "bounty": round(float(bounty), 8),
            "status": "open",
            "accepted": [],
            "assigned": [],
            "results": {},
            "shares": {},
            "paid_workers": [],
            "created_at": time.time(),
            "expires_at": time.time() + float(expires_in),
        }
        if task_type is not None:
            task.update({
                "task_type": task_type,
                "mode": mode,
                "min_nodes": int(min_nodes),
                "acceptance": acceptance,
                "fee": round(float(bounty) * MARKET_FEE_RATE, 8),
                "history": [{"state": "open", "at": time.time(), "by": creator}],
                "audited": False,
                "audit_pending": False,
            })
        self.store.compute_tasks[task_id] = task
        tn = TASK_TYPES.get(task_type, {}).get("name", "legacy")
        self._emit("task_publish", creator, task_id, spec[:80],
                   "任务发布（" + tn + "，预算 " + str(bounty) + " NOVA 已托管）")
        return task

    def _history(self, task: dict, state: str, by: str, note: str = ""):
        if "history" not in task:
            return
        task["history"].append({"state": state, "at": time.time(), "by": by, "note": note})

    # ----------------------------------------------------------------------
    # 接单：抢单 / 竞价（提示词 2）
    # ----------------------------------------------------------------------
    def validate_accept(self, addr: str, tid: str) -> tuple:
        task = self.store.compute_tasks.get(tid)
        if not task:
            return False, "任务不存在"
        if task.get("task_type") is None:
            if task["status"] != "open" or addr in task["accepted"]:
                return False, "任务不可接或已接"
            if len(task["accepted"]) >= MAX_WORKERS:
                return False, "参与人数已满"
            if addr == task["creator"]:
                return False, "发起者不能接单"
            return True, "ok"
        if task["status"] != "open" or addr == task["creator"] or addr in task["accepted"]:
            return False, "任务不可接"
        if not self.is_qualified_node(addr):
            return False, "未注册算力节点（超级节点自动具备资格）"
        ok, reason = spec_meets(self.node_spec(addr), task["task_type"])
        if not ok:
            return False, "算力不达标：" + reason
        rep = self.compute_reputation(addr)
        if rep["score"] < LIGHT_TIER_THRESHOLD and task["task_type"] != "data_clean":
            return False, "信誉过低（" + str(rep["score"]) + "），已降级为轻量任务提供者"
        if task["bounty"] > rep["max_budget"]:
            return False, "任务预算超过信誉档位上限（" + str(rep["max_budget"]) + " NOVA）"
        need = max(MIN_COMPUTE_STAKE, task["bounty"] * rep["stake_factor"])
        if self.store.compute_stakes.get(addr, 0.0) < need:
            return False, "质押不足：需要 " + str(round(need, 2)) + " NOVA"
        if task["mode"] == "grab" and len(task["assigned"]) >= task.get("min_nodes", 2):
            return False, "抢单名额已满"
        return True, "ok"

    def accept(self, worker: str, task_id: str) -> bool:
        task = self.store.compute_tasks[task_id]
        if worker in task["accepted"]:
            return False
        task["accepted"].append(worker)
        if task.get("task_type") is None:
            return True
        # 新式：抢单模式满员即进入执行；竞价模式等待出价
        if task["mode"] == "grab":
            task["assigned"].append(worker)
            task["assigned_at"] = task.get("assigned_at", time.time())
            if len(task["assigned"]) >= task.get("min_nodes", 2):
                task["status"] = "assigned"
                self._history(task, "assigned", worker, "抢单满员，进入执行")
        else:
            task["status"] = "bidding"
            self._history(task, "bidding", worker, "进入竞价阶段")
        return True

    def validate_bid(self, addr: str, tid: str, price: float) -> tuple:
        task = self.store.compute_tasks.get(tid)
        if not task or task.get("task_type") is None or task.get("mode") != "bid":
            return False, "任务不存在或非竞价模式"
        if task["status"] not in ("open", "bidding") or addr == task["creator"]:
            return False, "任务不可出价"
        if addr in [b["addr"] for b in self.store.compute_bids.get(tid, [])]:
            return False, "已出过价"
        if not (isinstance(price, (int, float)) and not isinstance(price, bool)
                and 0 < price <= 10000):
            return False, "报价无效"
        if not self.is_qualified_node(addr):
            return False, "未注册算力节点"
        ok, reason = spec_meets(self.node_spec(addr), task["task_type"])
        if not ok:
            return False, "算力不达标：" + reason
        if self.store.compute_stakes.get(addr, 0.0) < max(MIN_COMPUTE_STAKE, price):
            return False, "质押不足"
        return True, "ok"

    def bid(self, addr: str, tid: str, price: float) -> dict:
        self.store.compute_bids.setdefault(tid, []).append({
            "addr": addr, "price": round(float(price), 8), "at": time.time(),
        })
        self._emit("task_bid", addr, tid, "", "节点出价 " + str(price) + " NOVA")
        return {"ok": True, "bids": len(self.store.compute_bids[tid])}

    def validate_award(self, creator: str, tid: str, workers: list) -> tuple:
        task = self.store.compute_tasks.get(tid)
        if not task or task.get("mode") != "bid" or task.get("task_type") is None:
            return False, "任务不存在或非竞价模式"
        if task["creator"] != creator or task["status"] not in ("open", "bidding"):
            return False, "仅发起者可在竞价阶段选标"
        if not isinstance(workers, list) or not (2 <= len(workers) <= MAX_WORKERS):
            return False, "需要选择 2-8 个执行节点"
        bids = {b["addr"] for b in self.store.compute_bids.get(tid, [])}
        for w in workers:
            if w not in bids:
                return False, str(w[:12]) + " 未出价"
        return True, "ok"

    def award(self, creator: str, tid: str, workers: list) -> dict:
        task = self.store.compute_tasks[tid]
        bids = {b["addr"]: b["price"] for b in self.store.compute_bids.get(tid, [])}
        task["assigned"] = list(workers)
        task["accepted"] = list(workers)
        task["bid_prices"] = bids
        task["status"] = "assigned"
        task["assigned_at"] = time.time()
        self._history(task, "assigned", creator, "发起者选定执行节点")
        self._emit("task_award", creator, tid, "",
                   "发起者选定节点：" + ", ".join(w[:10] for w in workers))
        return task

    # ----------------------------------------------------------------------
    # 结果提交（提示词 2 / 4）
    # ----------------------------------------------------------------------
    def validate_submit(self, worker: str, tid: str, result_hash: str, result_cid: str = "") -> tuple:
        task = self.store.compute_tasks.get(tid)
        if not task:
            return False, "任务不存在"
        if task.get("task_type") is None:
            if task["status"] != "open" or worker not in task["accepted"]:
                return False, "任务不可提交"
            if worker in task["results"]:
                return False, "已提交过结果"
            return True, "ok"
        if task["status"] not in ("assigned", "submitted", "arbitrating"):
            return False, "任务不在执行/验证阶段"
        if worker not in task["assigned"] or worker in task["results"]:
            return False, "非执行节点或已提交"
        if result_cid and (not isinstance(result_cid, str) or len(result_cid) > 128
                           or not IPFS_RE.match(result_cid)):
            return False, "结果存储地址无效"
        return True, "ok"

    def submit(self, worker: str, task_id: str, result_hash: str, result_cid: str = "") -> dict:
        task = self.store.compute_tasks[task_id]
        rh = result_hash.lower()
        if task.get("task_type") is None:
            # legacy：任意两个不同节点结果一致即完成
            if (task["status"] != "open" or worker not in task["accepted"]
                    or worker in task["results"]):
                return {"status": task.get("status", "unknown"), "reward": 0.0}
            task["results"][worker] = rh
            for other, h in task["results"].items():
                if other != worker and h == rh:
                    return self._complete(task, worker, other)
            return {"status": task["status"], "reward": 0.0}
        # 新式状态机
        if (task["status"] not in ("assigned", "submitted", "arbitrating")
                or worker not in task["assigned"] or worker in task["results"]):
            return {"status": task.get("status", "unknown"), "reward": 0.0}
        task["results"][worker] = {"hash": rh, "cid": result_cid or "", "at": time.time()}
        self._record_response(worker, task)
        self._history(task, "submitted", worker, "结果已提交（含 IPFS 地址）")
        hashes = {w: r["hash"] for w, r in task["results"].items()}
        for w1, h1 in hashes.items():
            for w2, h2 in hashes.items():
                if w1 < w2 and h1 == h2:
                    return self._settle(task, [w1, w2])
        if len(task["results"]) >= 2:
            task["status"] = "arbitrating"
            self._history(task, "arbitrating", worker, "结果不一致，引入第三节点仲裁")
        elif task["status"] != "submitted":
            task["status"] = "submitted"
        return {"status": task["status"], "reward": 0.0}

    def _complete(self, task: dict, w1: str, w2: str) -> dict:
        """legacy 结算：平分悬赏，无手续费。"""
        if task.get("status") == "completed":
            return {"status": "completed", "reward": 0.0, "workers": [w1, w2]}
        task["status"] = "completed"
        each = round(task["bounty"] / 2, 8)
        self.store.balances[w1] = self.store.balances.get(w1, 0) + each
        self.store.balances[w2] = self.store.balances.get(w2, 0) + each
        refund = round(task["bounty"] - each * 2, 8)
        if refund > 0:
            self.store.balances[task["creator"]] = self.store.balances.get(task["creator"], 0) + refund
        return {"status": "completed", "reward": each, "workers": [w1, w2]}

    def _settle(self, task: dict, workers: list) -> dict:
        """新式结算：1% 手续费回流验证者激励池 + 信誉加成（5-15%）。"""
        if task.get("status") in ("settled", "completed", "disputed"):
            return {"status": task["status"], "reward": 0.0, "workers": workers}
        bounty = task["bounty"]
        fee = round(bounty * MARKET_FEE_RATE, 8)
        pool = round(bounty - fee, 8)
        self.store.balances[self.economy.VALIDATOR_POOL] = (
            self.store.balances.get(self.economy.VALIDATOR_POOL, 0.0) + fee)
        weights = {w: 1.0 + self.rep_bonus(w) for w in workers}
        total_w = sum(weights.values())
        base = pool / len(workers)
        for w in workers:
            share = round(pool * weights[w] / total_w, 8)
            self.store.balances[w] = self.store.balances.get(w, 0) + share
            task["shares"][w] = share
            self._touch_stats(w, completed=1, correct=1, task_reward=share)
            if share > base:
                self._touch_stats(w, bonus_reward=share - base)
        task["paid_workers"] = list(workers)
        task["status"] = "settled"
        self._history(task, "settled", ",".join(workers),
                      "验证通过（手续费 " + str(fee) + " NOVA 回流激励池）")
        task["status"] = "completed"
        task["completed_at"] = time.time()
        self._history(task, "completed", ",".join(workers), "结算完成")
        self._emit("task_completed", task["creator"], task.get("spec", "")[:80], "",
                   "任务完成，报酬已结算")
        return {"status": "completed", "reward": pool / len(workers),
                "workers": workers, "fee": fee}

    # ----------------------------------------------------------------------
    # 第三方仲裁（提示词 2 / 4）
    # ----------------------------------------------------------------------
    def validate_arbitrate(self, addr: str, tid: str, result_hash: str) -> tuple:
        task = self.store.compute_tasks.get(tid)
        if not task or task.get("task_type") is None or task["status"] != "arbitrating":
            return False, "任务不在仲裁阶段"
        if not self.is_qualified_node(addr):
            return False, "非算力节点"
        if addr in task["results"] or addr == task["creator"]:
            return False, "仲裁节点不能是执行节点或发起者"
        if task.get("arbiter") is not None and task["arbiter"] != addr:
            return False, "仲裁节点已确定"
        if not RESULT_HASH_RE.match(result_hash):
            return False, "结果哈希无效"
        return True, "ok"

    def arbitrate(self, addr: str, tid: str, result_hash: str) -> dict:
        task = self.store.compute_tasks[tid]
        task["arbiter"] = addr
        task["arbiter_hash"] = result_hash.lower()
        hashes = {w: r["hash"] for w, r in task["results"].items()}
        correct = [w for w, h in hashes.items() if h == result_hash.lower()]
        wrong = [w for w, h in hashes.items() if h != result_hash.lower()]
        self._history(task, "arbitrating", addr, "第三方仲裁完成")
        if not correct:
            # 仲裁与双方均不一致：视为执行节点集体作恶（串通检测）
            for w in wrong:
                self._slash_cheat(task, w, "仲裁结果与双方均不一致，判为作恶")
            task["status"] = "failed"
            self._refund(task, "仲裁失败，预算退回发起者")
            self._history(task, "failed", addr, "仲裁判负，双方罚没")
            return {"status": "failed", "correct": [], "wrong": wrong}
        for w in correct:
            self._touch_stats(w, completed=1, correct=2)
        for w in wrong:
            self._touch_stats(w, wrong=1)
        self._settle(task, correct)
        for w in wrong:
            self._touch_stats(w, complaints=1)
        task["status"] = "completed"
        self._history(task, "completed", addr, "仲裁定标完成")
        self._emit("task_completed", task["creator"], "", "", "仲裁后完成结算")
        return {"status": "completed", "correct": correct, "wrong": wrong}

    def _slash_cheat(self, task: dict, addr: str, note: str):
        """作恶罚没：质押清零 + 信誉作恶扣分（清零）。"""
        staked = self.store.compute_stakes.pop(addr, 0.0)
        if staked > 0:
            self.store.balances[self.economy.ECOSYSTEM_FUND] = (
                self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0) + staked)
            self.store.compute_slashed = round(
                self.store.compute_slashed + staked, 8)
        self._touch_stats(addr, cheated=1, complaints=1)
        self._emit("node_cheat", addr, task.get("spec", "")[:80], "", note)

    def _refund(self, task: dict, reason: str = "任务未完成，预算退回"):
        self.store.balances[task["creator"]] = (
            self.store.balances.get(task["creator"], 0.0) + task["bounty"])
        self._emit("task_refund", task["creator"], task.get("spec", "")[:80], "", reason)

    # ----------------------------------------------------------------------
    # 争议处理（提示词 2）
    # ----------------------------------------------------------------------
    def validate_dispute(self, by: str, tid: str, reason: str) -> tuple:
        task = self.store.compute_tasks.get(tid)
        if not task or task.get("task_type") is None:
            return False, "任务不存在或非新式任务"
        if task["status"] != "completed" or task["creator"] != by:
            return False, "仅发起者可在完成后提出异议"
        if time.time() - float(task.get("completed_at", 0)) > DISPUTE_WINDOW:
            return False, "超过 24 小时异议窗口"
        if not (isinstance(reason, str) and 0 < len(reason.strip()) <= MAX_SPEC_LEN):
            return False, "异议理由无效"
        return True, "ok"

    def dispute(self, by: str, tid: str, reason: str) -> dict:
        task = self.store.compute_tasks[tid]
        # 预算冻结：回拨已结算报酬（记录每名工人实际回拨额 clawed，审计 M-1）
        frozen = 0.0
        clawed = {}
        for w, share in task.get("shares", {}).items():
            claw = min(share, self.store.balances.get(w, 0.0))
            self.store.balances[w] = self.store.balances.get(w, 0.0) - claw
            clawed[w] = round(claw, 8)
            frozen = round(frozen + claw, 8)
        task["frozen"] = frozen
        task["clawed"] = clawed
        task["status"] = "disputed"
        self.store.compute_disputes[tid] = {
            "task_id": tid, "by": by, "reason": reason, "votes": {},
            "created_at": time.time(), "resolved": False,
        }
        self._history(task, "disputed", by, "发起异议，预算冻结")
        self._emit("task_dispute", by, tid, reason[:80], "异议进入社区仲裁")
        return self.store.compute_disputes[tid]

    def validate_vote(self, voter: str, tid: str, support: str) -> tuple:
        d = self.store.compute_disputes.get(tid)
        task = self.store.compute_tasks.get(tid)
        if not d or d.get("resolved"):
            return False, "无进行中的争议"
        if not task or task["status"] != "disputed":
            return False, "任务不在争议状态"
        if support not in ("uphold", "dismiss"):
            return False, "投票选项无效"
        if voter in d["votes"]:
            return False, "已投过票"
        # 审计 M-2：排除利益相关方（任务发起者与已获报酬的工人），防止自投自保/被回拨者投票
        if voter == task.get("creator") or voter in task.get("paid_workers", []):
            return False, "利益相关方不得参与争议投票"
        if not (voter in self.store.stakes or voter in self.store.miner_registry
                or self.is_qualified_node(voter)):
            return False, "仅社区验证者（矿工/质押者/算力节点）可投票"
        return True, "ok"

    def vote(self, voter: str, tid: str, support: str) -> dict:
        d = self.store.compute_disputes[tid]
        d["votes"][voter] = support
        self._emit("dispute_vote", voter, tid, "",
                   "仲裁投票：" + ("支持发起者" if support == "uphold" else "驳回异议"))
        return d

    def _settle_disputes(self, now: float = None):
        now = time.time() if now is None else now
        n = 0
        for tid, d in list(self.store.compute_disputes.items()):
            task = self.store.compute_tasks.get(tid)
            if not task or task["status"] != "disputed" or d.get("resolved"):
                continue
            if len(d["votes"]) < DISPUTE_QUORUM:
                continue
            uphold = sum(1 for v in d["votes"].values() if v == "uphold") >                      sum(1 for v in d["votes"].values() if v == "dismiss")
            d["resolved"] = True
            frozen = float(task.get("frozen", 0.0))
            if uphold:
                # 串通检测：结果一致但被证明错误 → 双双罚没
                for w in task.get("paid_workers", []):
                    self._slash_cheat(task, w, "争议仲裁判定作恶（串通）")
                self.store.balances[task["creator"]] = (
                    self.store.balances.get(task["creator"], 0.0) + frozen)
                task["status"] = "failed"
                self._history(task, "failed", tid, "争议判定发起者胜诉，预算退回")
                self._emit("task_dispute_resolved", task["creator"], tid, "",
                           "争议仲裁：发起者胜诉，节点罚没")
            else:
                # 驳回异议：仅回补争议冻结时实际回拨的金额（clawed），
                # 而非完整 share——防止工人在冻结窗口内转走资金后被重复入账（审计 M-1）
                for w, share in task.get("shares", {}).items():
                    self.store.balances[w] = self.store.balances.get(w, 0.0) + float(task.get("clawed", {}).get(w, 0.0))
                task["status"] = "completed"
                self._history(task, "completed", tid, "争议仲裁驳回，恢复结算")
                self._emit("task_dispute_resolved", task["creator"], tid, "",
                           "争议仲裁：驳回异议，结算恢复")
            n += 1
        return n

    # ----------------------------------------------------------------------
    # 随机抽查（提示词 4）
    # ----------------------------------------------------------------------
    @staticmethod
    def _audit_roll(tid: str, day: str) -> int:
        h = hashlib.sha3_256((tid + "|audit|" + day).encode()).hexdigest()
        return int(h, 16) % 100

    @staticmethod
    def _pick_auditor(tid: str, store) -> str:
        """确定性选择第三方审计节点：注册节点中排除原执行节点。"""
        task = store.compute_tasks.get(tid, {})
        exclude = set(task.get("paid_workers", [])) | {task.get("creator", "")}
        cands = [a for a in store.compute_nodes if a not in exclude]
        if not cands:
            cands = [a for a in store.compute_stats if a not in exclude]
        if not cands:
            return ""
        h = int(hashlib.sha3_256((tid + "|auditor").encode()).hexdigest(), 16)
        return cands[h % len(cands)]

    def _run_audits(self, now: float = None):
        now = time.time() if now is None else now
        # UTC 自然日（审计：统一 UTC，避免跨时区节点抽查窗口不一致）
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        n = 0
        for tid, task in list(self.store.compute_tasks.items()):
            if task.get("task_type") is None or task["status"] != "completed":
                continue
            if task.get("audited") or task.get("audit_pending"):
                continue
            if self._audit_roll(tid, day) >= int(AUDIT_RATE * 100):
                continue
            auditor = self._pick_auditor(tid, self.store)
            if not auditor:
                continue
            task["audit_pending"] = True
            self.store.compute_audits[tid] = {
                "task_id": tid, "selected_at": now, "status": "pending",
                "auditor": auditor, "result_hash": self._verified_hash(task),
                "passed": None,
            }
            n += 1
        return n

    @staticmethod
    def _verified_hash(task: dict) -> str:
        counts = {}
        for r in task.get("results", {}).values():
            h = r["hash"] if isinstance(r, dict) else r
            counts[h] = counts.get(h, 0) + 1
        if not counts:
            return ""
        return max(counts, key=lambda h: (counts[h], h))

    def validate_audit(self, addr: str, tid: str, result_hash: str) -> tuple:
        a = self.store.compute_audits.get(tid)
        task = self.store.compute_tasks.get(tid)
        if not a or a["status"] != "pending":
            return False, "无待执行的抽查"
        if a["auditor"] != addr or not (task and task.get("audit_pending")):
            return False, "非指定审计节点"
        if not RESULT_HASH_RE.match(result_hash):
            return False, "结果哈希无效"
        return True, "ok"

    def audit_submit(self, addr: str, tid: str, result_hash: str) -> dict:
        a = self.store.compute_audits[tid]
        task = self.store.compute_tasks[tid]
        a["status"] = "done"
        a["passed"] = (result_hash.lower() == a["result_hash"])
        a["submitted_at"] = time.time()
        task["audited"] = True
        task["audit_pending"] = False
        if a["passed"]:
            # 审计通过：审计节点获得奖励 + 信誉
            self._touch_stats(addr, completed=1, correct=2)
            if self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0) >= AUDIT_REWARD:
                self.store.balances[self.economy.ECOSYSTEM_FUND] -= AUDIT_REWARD
                self.store.balances[addr] = self.store.balances.get(addr, 0.0) + AUDIT_REWARD
                self._touch_stats(addr, audit_reward=AUDIT_REWARD)
            self._emit("task_audit", addr, tid, "", "随机抽查通过，无惩罚")
        else:
            # 抽查发现错误 → 原节点罚没双倍质押
            for w in task.get("paid_workers", []):
                staked = self.store.compute_stakes.get(w, 0.0)
                penalty = round(staked * AUDIT_SLASH_MULT, 8)
                available = staked + max(0.0, self.store.balances.get(w, 0.0))
                penalty = min(penalty, available)
                take_stake = min(staked, penalty)
                self.store.compute_stakes[w] = round(staked - take_stake, 8)
                rest = round(penalty - take_stake, 8)
                self.store.balances[w] = self.store.balances.get(w, 0.0) - rest
                self.store.balances[self.economy.ECOSYSTEM_FUND] = (
                    self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0) + penalty)
                self.store.compute_slashed = round(self.store.compute_slashed + penalty, 8)
                self._touch_stats(w, cheated=1)
            task["audit_failed"] = True
            self._history(task, "completed", addr, "抽查发现错误，原节点罚没双倍质押")
            self._emit("task_audit", addr, tid, "", "抽查发现错误，原节点罚没双倍质押")
        return a

    # ----------------------------------------------------------------------
    # 质押（提示词 4 / 5）
    # ----------------------------------------------------------------------
    def validate_stake(self, addr: str, amount: float) -> tuple:
        if not (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                and MIN_COMPUTE_STAKE <= amount <= MAX_COMPUTE_STAKE):
            return False, "质押需在 " + str(MIN_COMPUTE_STAKE) + "-" + str(MAX_COMPUTE_STAKE) + " NOVA 之间"
        if self.store.compute_stakes.get(addr, 0.0) + amount > MAX_COMPUTE_STAKE:
            return False, "超过单节点质押上限"
        if self.store.balances.get(addr, 0.0) < amount:
            return False, "余额不足"
        return True, "ok"

    def stake(self, addr: str, amount: float):
        self.store.balances[addr] = self.store.balances.get(addr, 0.0) - float(amount)
        self.store.compute_stakes[addr] = self.store.compute_stakes.get(addr, 0.0) + float(amount)
        self._ensure_stats(addr)
        self._emit("node_stake", addr, "", "", "算力质押 +" + str(amount) + " NOVA")

    def validate_unstake(self, addr: str, amount: float) -> tuple:
        if not (isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0):
            return False, "解押金额无效"
        if amount > self.store.compute_stakes.get(addr, 0.0):
            return False, "超过已质押金额"
        return True, "ok"

    def unstake(self, addr: str, amount: float):
        staked = self.store.compute_stakes.get(addr, 0.0)
        amt = min(float(amount), staked)
        self.store.compute_stakes[addr] = round(staked - amt, 8)
        if self.store.compute_stakes[addr] <= 0:
            del self.store.compute_stakes[addr]
        old = self.store.compute_unbonding.get(addr, (0.0, 0.0))[0]
        self.store.compute_unbonding[addr] = (round(old + amt, 8), time.time() + UNBOND_COMPUTE)
        self._emit("node_unstake", addr, "", "", "申请解押 " + str(amt) + " NOVA（7 天冷静期）")

    def validate_claim(self, addr: str) -> tuple:
        entry = self.store.compute_unbonding.get(addr)
        if not entry or time.time() < entry[1]:
            return False, "冷静期未到或无待领取解押"
        return True, "ok"

    def claim(self, addr: str):
        amt, _ = self.store.compute_unbonding.pop(addr)
        self.store.balances[addr] = self.store.balances.get(addr, 0.0) + amt
        self._emit("node_claim", addr, "", "", "领取解押 " + str(amt) + " NOVA")

    # ----------------------------------------------------------------------
    # 激励与经济（提示词 5）
    # ----------------------------------------------------------------------
    def settle_incentive_epoch(self) -> dict:
        """验证者激励池分配：存储 40% / 算力 60%，按贡献比例（质押/配额）。"""
        pool = self.store.balances.get(self.economy.VALIDATOR_POOL, 0.0)
        if pool <= 0:
            return {"paid": 0.0, "compute_nodes": 0, "storage_nodes": 0}
        compute_share = round(pool * 0.6, 8)
        storage_share = round(pool - compute_share, 8)
        paid = 0.0
        c_nodes = s_nodes = 0
        total_cs = sum(self.store.compute_stakes.values())
        if total_cs > 0:
            for addr, st in self.store.compute_stakes.items():
                amt = round(compute_share * st / total_cs, 8)
                self.store.balances[addr] = self.store.balances.get(addr, 0.0) + amt
                self._touch_stats(addr, block_reward=amt)
                paid = round(paid + amt, 8)
                c_nodes += 1
        total_quota = sum(float(n.get("quota_gb", 0.0) or 0.0)
                          for n in self.store.inc_nodes.values())
        if total_quota > 0:
            for addr, n in self.store.inc_nodes.items():
                quota = float(n.get("quota_gb", 0.0) or 0.0)
                amt = round(storage_share * quota / total_quota, 8)
                self.store.balances[addr] = self.store.balances.get(addr, 0.0) + amt
                self._touch_stats(addr, block_reward=amt)
                paid = round(paid + amt, 8)
                s_nodes += 1
        self.store.balances[self.economy.VALIDATOR_POOL] = round(pool - paid, 8)
        self.store.last_incentive_epoch = time.time()
        self._emit("incentive_epoch", "", "", "",
                   "激励池结算：算力 " + str(c_nodes) + " 节点 / 存储 " + str(s_nodes) + " 节点，共 " + str(paid) + " NOVA")
        return {"paid": paid, "compute_nodes": c_nodes, "storage_nodes": s_nodes}

    def node_income(self, addr: str) -> dict:
        st = self.store.compute_stats.get(addr, {})
        task_r = float(st.get("task_reward", 0.0))
        bonus = float(st.get("bonus_reward", 0.0))
        block = float(st.get("block_reward", 0.0))
        audit = float(st.get("audit_reward", 0.0))
        return {
            "found": addr in self.store.compute_stats or self.is_qualified_node(addr),
            "addr": addr,
            "task_reward": round(task_r, 8),
            "rep_bonus": round(bonus, 8),
            "block_reward": round(block, 8),
            "audit_reward": round(audit, 8),
            "total": round(task_r + bonus + block + audit, 8),
            "stake": self.store.compute_stakes.get(addr, 0.0),
            "unbonding": self.store.compute_unbonding.get(addr, [0.0, 0.0]),
        }

    def node_view(self, addr: str) -> dict:
        spec = self.node_spec(addr)
        if not spec:
            return {"found": False}
        rep = self.compute_reputation(addr)
        return {
            "found": True, "addr": addr, "spec": spec,
            "reputation": rep,
            "stake": self.store.compute_stakes.get(addr, 0.0),
            "unbonding": self.store.compute_unbonding.get(addr, [0.0, 0.0]),
            "income": self.node_income(addr),
            "qualified": self.is_qualified_node(addr),
            "super_node": self.is_super_node(addr),
        }

    def overview(self) -> dict:
        auto = set(self.store.stakes) | set(self.store.miner_registry) | set(self.store.inc_nodes)
        return {
            "nodes": len(self.store.compute_nodes),
            "auto_qualified": sum(1 for a in auto if a not in self.store.compute_nodes),
            "tasks": len(self.store.compute_tasks),
            "open": sum(1 for t in self.store.compute_tasks.values() if t["status"] in ("open", "bidding")),
            "assigned": sum(1 for t in self.store.compute_tasks.values() if t["status"] == "assigned"),
            "completed": sum(1 for t in self.store.compute_tasks.values() if t["status"] in ("completed", "settled")),
            "disputed": sum(1 for t in self.store.compute_tasks.values() if t["status"] == "disputed"),
            "audits_pending": sum(1 for t in self.store.compute_audits.values() if t["status"] == "pending"),
            "audits_failed": sum(1 for t in self.store.compute_audits.values() if t.get("passed") is False),
            "total_staked": round(sum(self.store.compute_stakes.values()), 8),
            "slashed": round(self.store.compute_slashed, 8),
            "fees_to_pool": round(sum(t.get("fee", 0.0) for t in self.store.compute_tasks.values()
                                      if t.get("status") in ("completed", "settled", "disputed", "failed")), 8),
            "validator_pool": self.store.balances.get(self.economy.VALIDATOR_POOL, 0.0),
            "compute_pool": self.store.balances.get(COMPUTE_POOL, 0.0),
            "reference_prices": {k: {"name": v["name"], "min": v["price_min"], "max": v["price_max"]}
                                 for k, v in TASK_TYPES.items()},
        }

    # ----------------------------------------------------------------------
    # 过期与维护
    # ----------------------------------------------------------------------
    def expire(self, task_id: str) -> bool:
        task = self.store.compute_tasks.get(task_id)
        if not task or time.time() <= task["expires_at"]:
            return False
        if task.get("task_type") is None:
            if task["status"] != "open":
                return False
            task["status"] = "expired"
            self.store.balances[task["creator"]] = (
                self.store.balances.get(task["creator"], 0.0) + task["bounty"])
            return True
        if task["status"] in ("completed", "settled", "failed", "expired", "disputed"):
            return False
        task["status"] = "expired"
        self._history(task, "expired", task["creator"], "任务过期，预算退回")
        self._refund(task, "任务过期，预算退回发起者")
        return True

    def expire_all(self) -> int:
        n = 0
        for tid in list(self.store.compute_tasks):
            if self.expire(tid):
                n += 1
        return n

    def maintain(self, now: float = None) -> dict:
        now = time.time() if now is None else now
        expired = self.expire_all()
        disputes = self._settle_disputes(now)
        audits = self._run_audits(now)
        incentive = self.settle_incentive_epoch()
        return {"expired": expired, "disputes_settled": disputes,
                "audits_started": audits, "incentive": incentive}

    # ----------------------------------------------------------------------
    # 事件
    # ----------------------------------------------------------------------
    def _emit(self, event_type: str, addr: str, ref: str, title: str, message: str) -> str:
        seq = self.store.compute_event_seq + 1
        self.store.compute_event_seq = seq
        eid = "cmp_" + str(seq)
        self.store.compute_events[eid] = {
            "id": eid, "type": event_type, "addr": addr, "ref": ref,
            "title": title, "message": message, "at": time.time(),
        }
        if len(self.store.compute_events) > EVENT_LIMIT:
            for k in list(self.store.compute_events)[:len(self.store.compute_events) - EVENT_LIMIT]:
                del self.store.compute_events[k]
        return eid

    def events(self, limit: int = 50) -> list:
        return sorted(self.store.compute_events.values(),
                      key=lambda e: e.get("at", 0), reverse=True)[:limit]
