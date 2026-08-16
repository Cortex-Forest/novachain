# -*- coding: utf-8 -*-
"""链上存储激励合约（Storage Incentive）——超级节点存储挖矿、挑战证明、奖惩与自动恢复。

覆盖《存储激励》《链上存储状态合约》《存储节点监控与自动恢复》三份需求：

1. 超级节点自动注册为存储节点（质押即注册，无需额外配置）。
2. 节点每 24 小时向链上提交一次“存储证明”：
   - 链上确定性选择节点声称存储的最多 3 个文件；
   - 节点必须返回每个文件的前 1KB 片段（fragment）；
   - 合约比对片段哈希（sha256）与创作者提交文件时登记的 fragment_commit，
     全部通过则记录证明时间戳（last_proof_at / last_proof_epoch）。
3. 合约自动计算奖励：每 GB 每月 1 NOVA，每日结算一次，从生态基金扣除。
4. 作弊惩罚：连续 3 次证明失败（含当日未证明）罚没节点质押的 10%，罚没资金进入生态基金。
5. 存储状态：每个文件按“在线存储节点数”计算健康度
   🟢 3+ 节点 / 🟡 1-2 节点 / 🔴 0 节点；提供 RPC 查询。
6. 监控与自动恢复：5 分钟心跳扫描（30 分钟超时判离线）、濒危文件自动重新分配、
   配额管理（默认 10GB，质押扩容）、收益统计、退出前 7 天声明与数据迁移。

所有逻辑均为确定性链上规则：挑战选择、奖励结算、惩罚判定不依赖链外输入，
任何节点执行同一状态机都能得到相同结果。
"""
import hashlib
import time

from core.storage_network import CID_RE, HEX64_RE, day_index, MAX_CAPACITY_GB

INCENTIVE_DAY = 86400                  # 激励周期：1 天
CHALLENGE_FILES = 3                    # 每次挑战抽查的文件数（最多）
FRAGMENT_SIZE = 1024                   # 证明片段大小：文件前 1KB
REWARD_PER_GB_PER_MONTH = 1.0          # 每 GB 每月奖励 1 NOVA
DAYS_PER_MONTH = 30.0
SLASH_AFTER_FAILURES = 3               # 连续失败次数阈值
SLASH_RATIO = 0.10                     # 罚没比例：质押的 10%
MIN_STAKE_FOR_NODE = 100.0             # 超级节点自动注册的最低质押
DEFAULT_QUOTA_GB = 10.0                # 默认存储配额
QUOTA_PER_STAKE_GB = 0.1               # 每质押 1 NOVA 增加 0.1GB 配额（10GB/100NOVA）
HEARTBEAT_INTERVAL = 300               # 心跳检查周期：5 分钟
HEARTBEAT_TIMEOUT = 1800               # 心跳超时：30 分钟判离线
HOT_CACHE_SIZE = 100                   # 超级节点本地热文件缓存条数
CACHE_TTL = 7 * 86400                  # 缓存过期时间：7 天
HOT_TOP_N = 1000                       # 每日热门文件保护数量
HOT_REPLICAS = 3                       # 热门文件保底副本数
HOT_PIN_FEE = 2.0                      # 热门文件固定付费（生态基金 → 节点）
MIN_HEALTHY_ONLINE = 2                 # 文件健康在线节点数下限（濒危判定）
REASSIGN_FEE = 1.0                     # 濒危文件接管付费（生态基金 → 节点）
MAX_INC_REPLICAS = 10                  # 单个文件最大存储副本数
EXIT_NOTICE_DAYS = 7                   # 退出提前声明天数
MIN_INC_SIZE_GB = 0.001
MAX_INC_SIZE_GB = 1024.0
EVENT_LIMIT = 500                      # 链上事件保留上限


def month_key(ts: float = None) -> str:
    return time.strftime("%Y-%m", time.localtime(ts if ts is not None else time.time()))


class StorageIncentive:
    """存储激励合约状态机。

    状态存放于 StateStore.inc_* 系列字段，全部随区块状态快照复制到全节点。
    """

    def __init__(self, store, economy):
        self.store = store
        self.economy = economy

    # ======================================================================
    # 内部工具
    # ======================================================================
    @staticmethod
    def _fragment_commit(data: bytes) -> str:
        """计算文件前 1KB 片段的 sha256（hex，64 位）。"""
        return hashlib.sha256(data[:FRAGMENT_SIZE]).hexdigest()

    @staticmethod
    def _hash_key(*parts) -> int:
        return int.from_bytes(hashlib.sha3_256(("|".join(str(p) for p in parts)).encode()).digest(), "big")

    def _emit(self, event_type: str, creator: str, cid: str, title: str, message: str) -> str:
        """追加链上事件（创作者通知、作弊惩罚等）。"""
        seq = self.store.inc_event_seq + 1
        self.store.inc_event_seq = seq
        eid = f"inc_{seq}"
        self.store.inc_events[eid] = {
            "id": eid, "type": event_type, "creator": creator, "cid": cid,
            "title": title, "message": message, "at": time.time(), "read": False,
        }
        if len(self.store.inc_events) > EVENT_LIMIT:
            for k in list(self.store.inc_events)[:len(self.store.inc_events) - EVENT_LIMIT]:
                del self.store.inc_events[k]
        return eid

    # ======================================================================
    # 注册（超级节点自动注册）
    # ======================================================================
    def auto_register(self, addr: str, capacity_gb: float = None) -> bool:
        """超级节点自动注册为存储节点。

        任何地址质押达到 MIN_STAKE_FOR_NODE，或在旧存储网络注册提供者，
        都会自动进入激励系统，无需额外配置。重复调用幂等。
        """
        if addr in self.store.inc_nodes:
            return False
        stake = self.store.stakes.get(addr, 0.0)
        capacity = float(capacity_gb) if capacity_gb else DEFAULT_QUOTA_GB
        now = time.time()
        self.store.inc_nodes[addr] = {
            "registered_at": now,
            "is_supernode": stake >= MIN_STAKE_FOR_NODE,
            "stake": stake,
            "quota_gb": max(DEFAULT_QUOTA_GB, capacity if capacity_gb else DEFAULT_QUOTA_GB),
            "assigned_gb": 0.0,
            "last_heartbeat": now,
            "online": True,
            "fail_count": 0,
            "fail_epoch": 0,
            "success_count": 0,
            "last_proof_epoch": 0,
            "last_proof_at": 0.0,
            "exit_at": 0.0,
            "revenue": 0.0,
            "month_revenue": 0.0,
            "revenue_month": month_key(),
            "assigned": [],          # 声称存储的文件 CID 列表
        }
        self._emit("node_register", addr, "", "", f"存储节点已注册（配额 {self.store.inc_nodes[addr]['quota_gb']}GB）")
        return True

    def node_quota(self, addr: str) -> float:
        """节点当前配额 = 基础配额 + 质押加成。"""
        node = self.store.inc_nodes.get(addr)
        if not node:
            return 0.0
        stake = self.store.stakes.get(addr, 0.0)
        return round(node.get("quota_gb", DEFAULT_QUOTA_GB) + stake * QUOTA_PER_STAKE_GB, 8)

    def can_assign(self, addr: str, size_gb: float) -> bool:
        node = self.store.inc_nodes.get(addr)
        if not node or node.get("exit_at"):
            return False
        if node.get("assigned_gb", 0.0) + size_gb > self.node_quota(addr) + 1e-9:
            return False
        return True

    # ======================================================================
    # 文件注册（创作者上传时登记片段承诺）
    # ======================================================================
    def file_register(self, creator: str, cid: str, size_gb: float,
                      fragment_commit: str, title: str = "", content_type: str = "music") -> dict:
        now = time.time()
        self.store.inc_files[cid] = {
            "owner": creator,
            "cid": cid,
            "prev_cid": "",
            "title": title or cid[:10],
            "content_type": content_type,     # music / nft_image / ciphertext / metadata / preview
            "size_gb": float(size_gb),
            "fragment_commit": fragment_commit.lower(),
            "created_at": now,
            "replicas": [],                   # 存储该文件的节点列表
            "online": 0,                      # 在线节点数（扫描后刷新）
            "health": "red",                  # green / yellow / red
            "notified_red": False,
            "hot": False,
            "access_today": 0,
        }
        self._emit("file_register", creator, cid, self.store.inc_files[cid]["title"],
                   f"文件已登记，等待存储节点认领")
        return self.store.inc_files[cid]

    def file_reupload(self, creator: str, old_cid: str, new_cid: str,
                      size_gb: float, fragment_commit: str, title: str = "") -> bool:
        """创作者一键重新上传：替换 IPFS 哈希并更新片段承诺，保留原副本节点。"""
        f = self.store.inc_files.get(old_cid)
        if not f or f["owner"] != creator or new_cid == old_cid:
            return False
        if new_cid in self.store.inc_files:
            return False
        f["prev_cid"] = old_cid
        f["cid"] = new_cid
        f["size_gb"] = float(size_gb)
        f["fragment_commit"] = fragment_commit.lower()
        f["title"] = title or f["title"]
        f["notified_red"] = False
        self.store.inc_files[new_cid] = f
        del self.store.inc_files[old_cid]
        self._emit("file_reupload", creator, new_cid, f["title"], "文件已重新上传并替换 IPFS 哈希")
        return True

    def file_status(self, cid: str) -> dict:
        """计算文件存储状态：🟢 3+ / 🟡 1-2 / 🔴 0（按在线节点数）。"""
        f = self.store.inc_files.get(cid)
        if not f:
            return {"cid": cid, "found": False}
        online = [a for a in f.get("replicas", [])
                  if self.store.inc_nodes.get(a, {}).get("online") and not self.store.inc_nodes.get(a, {}).get("exit_at")]
        n = len(online)
        health = "green" if n >= 3 else ("yellow" if n >= 1 else "red")
        f["online"] = n
        f["health"] = health
        return {
            "cid": cid, "found": True, "owner": f["owner"], "title": f["title"],
            "size_gb": f["size_gb"], "health": health,
            "online": n, "replicas": len(f.get("replicas", [])),
            "nodes": online, "created_at": f["created_at"], "hot": f.get("hot", False),
        }

    def notify_if_red(self, cid: str):
        """文件状态变为 🔴 时自动通知创作者（链上事件）。"""
        f = self.store.inc_files.get(cid)
        if not f:
            return
        st = self.file_status(cid)
        if st["health"] == "red" and not f["notified_red"]:
            f["notified_red"] = True
            self._emit("file_red", f["owner"], cid, f["title"],
                       f"您的文件《{f['title']}》存储状态异常（0 个在线节点），请重新上传")
        elif st["health"] != "red" and f["notified_red"]:
            f["notified_red"] = False

    # ======================================================================
    # 认领（节点声称存储）
    # ======================================================================
    def claim(self, addr: str, cid: str) -> bool:
        f = self.store.inc_files.get(cid)
        node = self.store.inc_nodes.get(addr)
        if not f or not node:
            return False
        if addr in f["replicas"] or len(f["replicas"]) >= MAX_INC_REPLICAS:
            return False
        if not self.can_assign(addr, f["size_gb"]):
            return False
        f["replicas"].append(addr)
        node["assigned"].append(cid)
        node["assigned_gb"] = round(node["assigned_gb"] + f["size_gb"], 8)
        self.notify_if_red(cid)
        return True

    # ======================================================================
    # 挑战与证明
    # ======================================================================
    def current_challenge(self, addr: str, day: int = None) -> dict:
        """确定性生成节点当日挑战：从节点声称存储的文件中抽取（最多 3 个）。

        选择算法：以 (day, addr, 已应答次数) 为种子做 sha3-256，对排序后的
        文件列表做确定性洗牌，取前 CHALLENGE_FILES 个。链上与节点端一致。
        """
        day = day_index() if day is None else int(day)
        node = self.store.inc_nodes.get(addr)
        if not node:
            return {"found": False, "reason": "未注册"}
        files = sorted(node.get("assigned", []))
        if not files:
            return {"found": False, "reason": "无已认领文件", "day": day, "files": []}
        seed = self._hash_key("nova:challenge", day, addr, node.get("challenge_seq", 0))
        n = len(files)
        order = sorted(range(n), key=lambda i: (self._hash_key("nova:challenge:shuffle", day, addr, i, seed)))
        chosen = [files[i] for i in order[:min(CHALLENGE_FILES, n)]]
        return {
            "found": True, "day": day, "addr": addr,
            "files": chosen, "fragment_size": FRAGMENT_SIZE, "nonce": node.get("challenge_seq", 0),
        }

    def verify_proof(self, addr: str, day: int, files: list, fragments: list) -> dict:
        """验证存储证明：比对每个文件前 1KB 片段的 sha256。

        返回 {ok, reason, reward_base_gb}。全部片段匹配才算成功；
        任何失败都会累计 fail_count（连续 3 次触发罚没）。
        """
        ch = self.current_challenge(addr, day)
        if not ch.get("found"):
            return {"ok": False, "reason": ch.get("reason", "无挑战")}
        if list(files) != ch["files"] or len(fragments) != len(files):
            return {"ok": False, "reason": "挑战文件不匹配"}
        node = self.store.inc_nodes.get(addr)
        if not node:
            return {"ok": False, "reason": "未注册"}
        if node.get("last_proof_epoch") == day:
            return {"ok": False, "reason": "本周期已证明"}
        bad = []
        for cid, frag_hex in zip(files, fragments):
            f = self.store.inc_files.get(cid)
            if not f:
                bad.append(cid)
                continue
            try:
                frag = bytes.fromhex(frag_hex)
            except ValueError:
                bad.append(cid)
                continue
            if hashlib.sha256(frag).hexdigest() != f["fragment_commit"]:
                bad.append(cid)
        if bad:
            # 失败尝试计入连续失败次数（当日只计一次，连续 3 次触发罚没）
            if node.get("fail_epoch") != day:
                node["fail_count"] = node.get("fail_count", 0) + 1
                node["fail_epoch"] = day
            node["last_proof_at"] = time.time()
            self._emit("node_proof_fail", addr, "", "",
                       f"存储证明片段校验失败（{len(bad)} 个文件）：{bad}")
            return {"ok": False, "reason": f"片段校验失败: {bad}", "failed": bad}
        node["last_proof_epoch"] = day
        node["last_proof_at"] = time.time()
        node["last_heartbeat"] = time.time()
        node["online"] = True
        node["fail_epoch"] = 0
        node["challenge_seq"] = node.get("challenge_seq", 0) + 1
        return {"ok": True, "reason": "ok", "assigned_gb": node.get("assigned_gb", 0.0)}

    # ======================================================================
    # 心跳与离线扫描
    # ======================================================================
    def heartbeat(self, addr: str) -> bool:
        node = self.store.inc_nodes.get(addr)
        if not node:
            return False
        node["last_heartbeat"] = time.time()
        node["online"] = True
        return True

    def scan_offline(self, now: float = None):
        """每 5 分钟扫描：心跳超时 30 分钟 → 离线；离线节点文件标记濒危并通知创作者。"""
        now = time.time() if now is None else now
        changed = 0
        for addr, node in list(self.store.inc_nodes.items()):
            was_online = node.get("online", False)
            if node.get("exit_at"):
                node["online"] = False
                continue
            if now - node.get("last_heartbeat", now) > HEARTBEAT_TIMEOUT:
                node["online"] = False
                if was_online:
                    changed += 1
                    self._emit("node_offline", addr, "", "",
                               f"存储节点心跳超时，已标记离线（>30 分钟）")
        if changed:
            for cid in self.store.inc_files:
                self.notify_if_red(cid)
        return changed

    # ======================================================================
    # 奖励结算与惩罚
    # ======================================================================
    def daily_reward(self, node) -> float:
        """每 GB 每月 1 NOVA → 每日 = size_gb * 1 / 30。"""
        return round(node.get("assigned_gb", 0.0) * REWARD_PER_GB_PER_MONTH / DAYS_PER_MONTH, 8)

    def settle_epoch(self, day: int = None) -> dict:
        """每 24 小时结算一次：

        - 当日成功证明（last_proof_epoch == day）的节点：发放奖励（生态基金扣除）；
        - 未证明的节点：fail_count + 1；
        - 连续 3 次失败：罚没质押 10% → 生态基金，重置失败计数；
        - 刷新月度收益统计。
        """
        day = day_index() if day is None else int(day)
        if day in self.store.inc_settled_epochs:
            return {"settled": True, "skipped": "already"}
        self.store.inc_settled_epochs.add(day)
        result = {"day": day, "rewards_paid": 0.0, "nodes_paid": 0, "slashed": 0.0, "slashed_nodes": 0}
        eco = self.economy.ECOSYSTEM_FUND
        cur_month = month_key()
        for addr, node in list(self.store.inc_nodes.items()):
            if node.get("exit_at") or not node.get("assigned_gb"):
                continue
            if node.get("revenue_month") != cur_month:
                node["revenue_month"] = cur_month
                node["month_revenue"] = 0.0
            if node.get("last_proof_epoch") == day:
                node["fail_count"] = 0
                node["fail_epoch"] = 0
                node["success_count"] = node.get("success_count", 0) + 1
                reward = self.daily_reward(node)
                if reward > 0 and self.store.balances.get(eco, 0) >= reward:
                    self.store.balances[eco] = round(self.store.balances[eco] - reward, 8)
                    self.store.balances[addr] = self.store.balances.get(addr, 0) + reward
                    node["revenue"] = round(node["revenue"] + reward, 8)
                    node["month_revenue"] = round(node["month_revenue"] + reward, 8)
                    self.store.inc_rewards[addr] = self.store.inc_rewards.get(addr, 0.0) + reward
                    result["rewards_paid"] = round(result["rewards_paid"] + reward, 8)
                    result["nodes_paid"] += 1
                    self._emit("node_reward", addr, "", "",
                               f"当日存储奖励 +{reward} NOVA")
            else:
                # 当日已因失败尝试计数过则不再重复累计（失败按“天”计）
                if node.get("fail_epoch") != day:
                    node["fail_count"] = node.get("fail_count", 0) + 1
                node["fail_epoch"] = day
                if node["fail_count"] >= SLASH_AFTER_FAILURES:
                    slashed = self._slash(addr)
                    result["slashed"] = round(result["slashed"] + slashed, 8)
                    if slashed > 0:
                        result["slashed_nodes"] += 1
        return result

    def _slash(self, addr: str) -> float:
        """罚没节点质押的 10%，罚没资金进入生态基金。"""
        node = self.store.inc_nodes.get(addr)
        staked = self.store.stakes.get(addr, 0.0)
        amount = round(staked * SLASH_RATIO, 8) if staked > 0 else 0.0
        if amount > 0:
            self.store.stakes[addr] = round(staked - amount, 8)
            if self.store.stakes[addr] <= 0:
                del self.store.stakes[addr]
            self.store.balances[self.economy.ECOSYSTEM_FUND] = \
                self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) + amount
            self.store.inc_slashed = round(self.store.inc_slashed + amount, 8)
        node["fail_count"] = 0
        node["stake"] = self.store.stakes.get(addr, 0.0)
        self._emit("node_slash", addr, "", "",
                   f"连续 {SLASH_AFTER_FAILURES} 次证明失败，罚没质押 10%（{amount} NOVA）进入生态基金")
        return amount

    # ======================================================================
    # 热门文件保护计划（提示词 2）
    # ======================================================================
    def record_access(self, cid: str):
        """记录文件链上访问量（每日滚动）。"""
        f = self.store.inc_files.get(cid)
        if not f:
            return False
        day = day_index()
        bucket = self.store.inc_access_counts.setdefault(day, {})
        bucket[cid] = bucket.get(cid, 0) + 1
        f["access_today"] = bucket[cid]
        return True

    def protect_hot_files(self, day: int = None) -> dict:
        """每天统计链上访问量前 HOT_TOP_N 的文件，从生态基金扣款付费固定。

        目标：热门文件至少 HOT_REPLICAS 个在线节点存储；冷门文件由创作者自行负责。
        """
        day = day_index() - 1 if day is None else int(day)
        counts = self.store.inc_access_counts.get(day, {})
        hot = sorted(counts.items(), key=lambda kv: -kv[1])[:HOT_TOP_N]
        eco = self.economy.ECOSYSTEM_FUND
        result = {"day": day, "protected": 0, "spent": 0.0}
        for cid, cnt in hot:
            f = self.store.inc_files.get(cid)
            if not f:
                continue
            f["hot"] = True
            online = [a for a in f["replicas"] if self.store.inc_nodes.get(a, {}).get("online")]
            missing = HOT_REPLICAS - len(online)
            if missing <= 0:
                continue
            candidates = [a for a, nd in self.store.inc_nodes.items()
                          if nd.get("online") and a not in f["replicas"]
                          and self.can_assign(a, f["size_gb"])]
            for addr in candidates[:missing]:
                fee = HOT_PIN_FEE
                if self.store.balances.get(eco, 0) < fee:
                    break
                self.store.balances[eco] = round(self.store.balances[eco] - fee, 8)
                self.store.balances[addr] = self.store.balances.get(addr, 0) + fee
                self.store.inc_rewards[addr] = self.store.inc_rewards.get(addr, 0.0) + fee
                self.claim(addr, cid)
                result["spent"] = round(result["spent"] + fee, 8)
                result["protected"] += 1
        # 清理过期访问计数（保留最近 3 天）
        for old in [k for k in self.store.inc_access_counts if k < day - 2]:
            del self.store.inc_access_counts[old]
        return result

    # ======================================================================
    # 濒危文件自动重新分配（提示词 5）
    # ======================================================================
    def endangered_files(self) -> list:
        out = []
        for cid, f in self.store.inc_files.items():
            online = [a for a in f["replicas"] if self.store.inc_nodes.get(a, {}).get("online")]
            if len(online) < MIN_HEALTHY_ONLINE:
                out.append((cid, f, online))
        return out

    def reassign_endangered(self) -> dict:
        """文件标记濒危后，自动从生态基金扣款让健康节点接管存储。"""
        eco = self.economy.ECOSYSTEM_FUND
        result = {"endangered": 0, "reassigned": 0, "spent": 0.0}
        for cid, f, online in self.endangered_files():
            result["endangered"] += 1
            self.notify_if_red(cid)
            missing = MIN_HEALTHY_ONLINE - len(online)
            if missing <= 0:
                continue
            candidates = [a for a, nd in self.store.inc_nodes.items()
                          if nd.get("online") and a not in f["replicas"]
                          and self.can_assign(a, f["size_gb"])]
            for addr in candidates[:missing]:
                fee = REASSIGN_FEE
                if self.store.balances.get(eco, 0) < fee:
                    break
                self.store.balances[eco] = round(self.store.balances[eco] - fee, 8)
                self.store.balances[addr] = self.store.balances.get(addr, 0) + fee
                self.store.inc_rewards[addr] = self.store.inc_rewards.get(addr, 0.0) + fee
                self.claim(addr, cid)
                result["spent"] = round(result["spent"] + fee, 8)
                result["reassigned"] += 1
                self._emit("file_reassign", f["owner"], cid, f["title"],
                           f"文件濒危，已付费让节点接管存储")
        return result

    # ======================================================================
    # 配额管理（提示词 5）
    # ======================================================================
    def upgrade_quota(self, addr: str, amount: float) -> float:
        """节点质押更多 NOVA 升级配额：质押增加 amount，配额增加 amount * QUOTA_PER_STAKE_GB。"""
        node = self.store.inc_nodes.get(addr)
        if not node or amount <= 0:
            return 0.0
        self.store.balances[addr] = self.store.balances.get(addr, 0) - amount
        self.store.stakes[addr] = self.store.stakes.get(addr, 0.0) + amount
        node["stake"] = self.store.stakes[addr]
        added = round(amount * QUOTA_PER_STAKE_GB, 8)
        node["quota_gb"] = round(node["quota_gb"] + added, 8)
        self._emit("node_upgrade", addr, "", "", f"质押升级，存储配额 +{added}GB")
        return added

    def node_stats(self, addr: str) -> dict:
        """节点收益统计：本月收益、存储量、健康度。"""
        node = self.store.inc_nodes.get(addr)
        if not node:
            return {"found": False}
        total = node.get("success_count", 0) + node.get("fail_count", 0)
        health = round(node.get("success_count", 0) * 100.0 / total, 1) if total else 100.0
        return {
            "found": True, "addr": addr,
            "revenue": node.get("revenue", 0.0),
            "month_revenue": node.get("month_revenue", 0.0),
            "revenue_month": node.get("revenue_month", month_key()),
            "stored_gb": node.get("assigned_gb", 0.0),
            "health_pct": health,
            "quota_gb": self.node_quota(addr),
            "online": node.get("online", False),
            "fail_count": node.get("fail_count", 0),
            "success_count": node.get("success_count", 0),
            "last_proof_at": node.get("last_proof_at", 0.0),
            "last_proof_epoch": node.get("last_proof_epoch", 0),
            "exit_at": node.get("exit_at", 0.0),
        }

    # ======================================================================
    # 退出与数据迁移（提示词 5）
    # ======================================================================
    def exit_notice(self, addr: str) -> bool:
        node = self.store.inc_nodes.get(addr)
        if not node or node.get("exit_at"):
            return False
        node["exit_at"] = time.time() + EXIT_NOTICE_DAYS * 86400
        node["online"] = False
        self._emit("node_exit_notice", addr, "", "",
                   f"节点声明退出，{EXIT_NOTICE_DAYS} 天后迁移数据并释放质押")
        return True

    def finalize_exits(self, now: float = None) -> dict:
        """迁移退出节点的文件到健康节点，完成后释放退出节点质押（进入解押队列）。"""
        now = time.time() if now is None else now
        result = {"finalized": 0, "migrated": 0}
        for addr, node in list(self.store.inc_nodes.items()):
            if not node.get("exit_at") or now < node["exit_at"]:
                continue
            for cid in list(node.get("assigned", [])):
                f = self.store.inc_files.get(cid)
                if not f:
                    continue
                if addr in f["replicas"]:
                    f["replicas"].remove(addr)
                # 迁移：付费让健康节点接管
                missing = MIN_HEALTHY_ONLINE - len([a for a in f["replicas"]
                                                    if self.store.inc_nodes.get(a, {}).get("online")])
                for cand in [a for a, nd in self.store.inc_nodes.items()
                             if nd.get("online") and a != addr and a not in f["replicas"]
                             and self.can_assign(a, f["size_gb"])][:max(missing, 0)]:
                    fee = REASSIGN_FEE
                    if self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) >= fee:
                        self.store.balances[self.economy.ECOSYSTEM_FUND] -= fee
                        self.store.balances[cand] = self.store.balances.get(cand, 0) + fee
                        self.claim(cand, cid)
                        result["migrated"] += 1
                self.notify_if_red(cid)
            # 释放质押：质押转入解押队列（7 天冷却，与普通解押一致）
            staked = self.store.stakes.pop(addr, 0.0)
            if staked > 0:
                old = self.store.unbonding.get(addr, (0.0, 0.0))[0]
                self.store.unbonding[addr] = (old + staked, now + self.economy.UNBOND)
            del self.store.inc_nodes[addr]
            result["finalized"] += 1
            self._emit("node_exit_done", addr, "", "", "节点退出完成，数据已迁移，质押已释放")
        return result

    # ======================================================================
    # 汇总
    # ======================================================================
    def summary(self) -> dict:
        return {
            "nodes": len(self.store.inc_nodes),
            "files": len(self.store.inc_files),
            "green": sum(1 for cid in self.store.inc_files if self.file_status(cid)["health"] == "green"),
            "yellow": sum(1 for cid in self.store.inc_files if self.file_status(cid)["health"] == "yellow"),
            "red": sum(1 for cid in self.store.inc_files if self.file_status(cid)["health"] == "red"),
            "rewards_paid": round(sum(self.store.inc_rewards.values()), 8),
            "slashed": self.store.inc_slashed,
            "ecosystem_fund": self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0),
            "events": len(self.store.inc_events),
        }

