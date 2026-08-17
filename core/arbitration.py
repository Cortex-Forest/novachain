# -*- coding: utf-8 -*-
"""链上社区仲裁合约（Arbitration）：仲裁员注册与质押、案件仲裁流程、
激励与惩罚、防串通与利益回避（四大提示词合并为一个确定性状态机模块）。

与 Nova 链既有约定一致：
- 全部操作以 signed tx 上链（sender == receiver，data 为 JSON {op, ...}），
  经区块/DAG 广播后在所有节点确定性重放；
- 资格/金额/时间类硬约束全部在 validate_op 阶段强制（链上硬约束）；
- 链上可验证随机数（VRF）由「前次 VRF 种子 + 案件 ID + 盐」的哈希链派生，
  上链前不可预测，抽取结果可被任何节点复算验证；
- 所有仲裁员 / 候选人 / 案件 / 通知状态保存在 store.arb_* 字段，
  随状态快照复制到全节点。

覆盖需求：
1. 仲裁员注册与质押（提示词 1）：500 NOVA 质押、地址注册时长 >= 30 天、
   历史无罚没、信誉分 >= 70（读信誉系统合约）、7 天社区投票
   （1 NOVA = 1 票，赞成 > 反对 x 1.5 且总票数 > 100）、90 天任期、
   任期结束前 7 天可申请连任（需重新社区投票）、提前 7 天声明退出 +
   7 天冷静期返还质押、有未完成案件不可退出。
2. 案件仲裁流程（提示词 2）：10 NOVA 投诉保证金、自动冻结卖家保证金、
   VRF 随机抽取 3 名仲裁员（排除利益关联）、72 小时投票窗口、
   超时自动扣 1 NOVA 并重新抽取替代、3:0 立即执行 / 2:1 按多数执行、
   支持买家自动赔付 + 投诉保证金退还、支持卖家分账、
   7 天内 50 NOVA 二次仲裁（7 名仲裁员、最终结果、推翻时一次仲裁员
   扣 10 NOVA + 信誉分 -5）。
3. 激励与惩罚（提示词 3）：按时投票 +2 NOVA、与最终多数一致 +1 信誉分、
   连续 10 次正确 +10 NOVA；超时 -1 NOVA / -2 信誉分、被二次仲裁推翻
   -10 NOVA / -5 信誉分、受贿/明显偏袒罚没全部质押并永久取消资格、
   与当事人串通罚没质押并赔偿受害者；信誉分满分 100 初始 50，
   < 30 暂停资格需重新质押激活、归零永久取消；报酬 60% 生态基金 /
   40% 投诉保证金；收益统计接口。
4. 防串通与利益回避（提示词 4）：30 天内直接转账/推荐关系自动排除、
   主动声明利益冲突 +1 信誉分并重新抽取、VRF 抽取在投诉后 1 小时内
   自动完成、抽取结果公开但当事人对仲裁员匿名（仅编号）、同组仲裁员
   30 天内被抽取超过 3 次 / 投票一致率 > 90% 标记可疑并进入 7 天观察期、
   匿名投票（编号投票、结束后公开投票人）、恶意投诉检测（30 天内
   3 次全败诉 -> 保证金提至 50 NOVA；连续 5 次 -> 限制密文交易 30 天）。
"""
import hashlib
import json
import time

# ---------------------------------------------------------------------------
# 合约参数（全部为链上常量，确定性执行）
# ---------------------------------------------------------------------------
ARB_STAKE = 500.0                # 仲裁员质押：500 NOVA
ARB_ACCOUNT_AGE_DAYS = 30        # 地址注册时长门槛：30 天（自首次链上交易）
ARB_REP_MIN_APPLY = 70.0         # 申请信誉分门槛：>= 70（读信誉系统合约）
ARB_REP_MAX = 100.0              # 信誉分上限
ARB_REP_INIT = 50.0              # 信誉分初始值（新仲裁员下限）
ARB_REP_SUSPEND = 30.0           # 信誉分低于 30：暂停仲裁资格
ARB_VOTE_PERIOD_DAYS = 7         # 社区投票期：7 天
ARB_PASS_RATIO = 1.5             # 通过条件：赞成 > 反对 x 1.5
ARB_MIN_VOTES = 100              # 通过条件：总票数 > 100
ARB_TERM_DAYS = 90               # 初始任期：90 天
ARB_RENEW_WINDOW_DAYS = 7        # 任期结束前 7 天可申请连任
ARB_EXIT_NOTICE_DAYS = 7         # 提前 7 天声明退出
ARB_EXIT_COOLDOWN_DAYS = 7       # 退出后 7 天冷静期返还质押
ARB_COMPLAINT_DEPOSIT = 10.0     # 投诉保证金：10 NOVA
ARB_MALICIOUS_DEPOSIT = 50.0     # 恶意投诉者保证金：50 NOVA
ARB_SECOND_DEPOSIT = 50.0        # 二次仲裁保证金：50 NOVA
ARB_SELLER_FREEZE_MULT = 2.0     # 卖家保证金冻结倍数（2 倍投诉保证金）
ARB_PANEL_SIZE = 3               # 首次仲裁庭人数
ARB_SECOND_PANEL_SIZE = 7        # 二次仲裁庭人数
ARB_VOTE_WINDOW = 72 * 3600      # 仲裁员投票窗口：72 小时
ARB_DRAW_WINDOW = 3600           # 投诉发起后 1 小时内完成抽取（自动）
ARB_VOTE_REWARD = 2.0            # 按时投票奖励：+2 NOVA
ARB_MAJORITY_REP = 1.0           # 与最终多数一致：+1 信誉分
ARB_STREAK_LEN = 10              # 连续正确次数阈值
ARB_STREAK_REWARD = 10.0         # 连续 10 次正确：额外 +10 NOVA
ARB_TIMEOUT_PENALTY = 1.0        # 超时未投票：-1 NOVA
ARB_TIMEOUT_REP = 2.0            # 超时未投票：信誉分 -2
ARB_OVERTURN_PENALTY = 10.0      # 被二次仲裁推翻：-10 NOVA
ARB_OVERTURN_REP = 5.0           # 被二次仲裁推翻：信誉分 -5
ARB_REWARD_ECO_RATIO = 0.6       # 报酬来源：生态基金 60%
ARB_REWARD_DEPOSIT_RATIO = 0.4   # 报酬来源：投诉保证金 40%
ARB_APPEAL_WINDOW_DAYS = 7       # 二次仲裁发起窗口：7 天
ARB_CASE_VOTE_MAX_DAYS = 7       # 案件投票总时限（超时按现票/卖家处理）
ARB_SUSPECT_PANEL_REPEAT = 3     # 同一组仲裁员 30 天内被抽超 3 次 -> 可疑
ARB_SUSPECT_WINDOW_DAYS = 30     # 串通检测窗口：30 天
ARB_SUSPECT_VOTE_AGREE = 0.90    # 投票一致率 > 90% -> 可疑
ARB_OBSERVE_DAYS = 7             # 可疑仲裁员 7 天观察期
ARB_MALICIOUS_LOSS_LIMIT = 3     # 30 天内败诉超 3 次 -> 恶意投诉名单
ARB_MALICIOUS_WINDOW_DAYS = 30   # 恶意投诉统计窗口：30 天
ARB_MALICIOUS_LOCK_DAYS = 30     # 连续恶意投诉 5 次：限制密文交易 30 天
ARB_MALICIOUS_LOCK_COUNT = 5     # 连续恶意投诉阈值
ARB_CHARGE_DEPOSIT = 2.0         # 检举仲裁员保证金：2 NOVA
ARB_CHARGE_CONFIRM_MIN = 2       # 审计修复 H-4：确认可疑需 ≥2 名独立检举人，防单人 2 NOVA 免费罚没
ARB_CHARGE_MIN_POWER = 100.0     # 审计修复 H-4：检举人最低权益（质押+余额），防 Sybil 刷确认
ARB_REASON_MAX = 2000            # 投诉理由长度上限
ARB_EVIDENCE_MAX = 300           # 证据 CID 长度上限
ARB_TRADE_ID_MAX = 128           # 交易 ID 长度上限
ARB_NOTIF_LIMIT = 50             # 每地址通知保留条数
ARB_EVENT_LIMIT = 200            # 公开案件公示保留条数
ARB_SELLER_WIN_RATIO = 0.4       # 支持卖家：投诉保证金剩余 40% 赔偿卖家
ARB_SECOND_FAIL_ECO_RATIO = 0.5  # 二次仲裁未推翻：保证金 50% 入生态基金


def _amt(v):
    return round(float(v), 8)


def _h(raw: str) -> str:
    return hashlib.sha3_256(raw.encode()).hexdigest()


class Arbitration:
    """链上社区仲裁合约状态机。

    状态全部保存在 self.store.arb_* 系列字段，随状态快照同步到全节点。
    """

    OPS = {
        "nova:arb:apply": "apply",
        "nova:arb:candidate_vote": "candidate_vote",
        "nova:arb:candidate_settle": "candidate_settle",
        "nova:arb:renew": "renew",
        "nova:arb:reactivate": "reactivate",
        "nova:arb:exit": "exit",
        "nova:arb:claim_stake": "claim_stake",
        "nova:arb:complain": "complain",
        "nova:arb:draw": "draw",
        "nova:arb:vote": "vote",
        "nova:arb:decline": "decline",
        "nova:arb:second": "second",
        "nova:arb:charge": "charge",
    }

    def __init__(self, store, economy, socialfi):
        self.store = store
        self.economy = economy
        self.socialfi = socialfi

    # ======================================================================
    # 内部工具
    # ======================================================================
    @staticmethod
    def _clamp_rep(v):
        return max(0.0, min(ARB_REP_MAX, round(float(v), 2)))

    @staticmethod
    def _vrf(*parts) -> int:
        """链上可验证随机数：对参数串做 SHA3-256，返回大整数。

        种子链（store.arb_vrf_seed）随每次抽取前滚，抽取前不可预测，
        任何节点可用相同输入复算验证。
        """
        raw = "|".join(str(p) for p in parts if str(p) != "")
        return int.from_bytes(hashlib.sha3_256(raw.encode()).digest(), "big")

    def _chain_rep(self, addr) -> float:
        """从信誉系统合约读取 0-100 信誉分（socialfi 声誉引擎）。"""
        try:
            return float(self.socialfi.reputation(addr)["score"])
        except Exception:
            return 0.0

    def _first_tx_ts(self, addr) -> float:
        """地址首次链上交易时间（来自交易历史，即注册时长起点）。"""
        best = None
        for t in self.store.tx_history.values():
            if t.get("sender") == addr:
                ts = float(t.get("ts", 0) or 0)
                if best is None or ts < best:
                    best = ts
        return best or 0.0

    def _is_banned(self, addr) -> bool:
        return addr in self.store.arb_banned

    def _notify(self, addr, kind, title, message, case_id=""):
        """向指定地址追加链上通知（前端弹窗轮询）。"""
        seq = self.store.arb_notif_seq + 1
        self.store.arb_notif_seq = seq
        box = self.store.arb_notifications.setdefault(addr, [])
        box.append({
            "id": f"arbn_{seq}", "kind": kind, "title": title,
            "message": message, "case_id": case_id,
            "at": time.time(), "read": False,
        })
        if len(box) > ARB_NOTIF_LIMIT:
            del box[:len(box) - ARB_NOTIF_LIMIT]

    def _event(self, kind, title, message, case_id=""):
        """追加公开案件公示事件。"""
        seq = self.store.arb_event_seq + 1
        self.store.arb_event_seq = seq
        self.store.arb_events.append({
            "id": f"arbe_{seq}", "kind": kind, "title": title,
            "message": message, "case_id": case_id, "at": time.time(),
        })
        if len(self.store.arb_events) > ARB_EVENT_LIMIT:
            del self.store.arb_events[:len(self.store.arb_events) - ARB_EVENT_LIMIT]

    def _eco_balance(self) -> float:
        return self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0)

    def _pay_arb_reward(self, addr, amount, pool_key) -> float:
        """按 60% 生态基金 / 40% 案件保证金池发放仲裁员报酬。

        返回实际从保证金池扣除的金额（不足部分由生态基金补齐或放弃，
        保证确定性）。"""
        amount = _amt(amount)
        if amount <= 0:
            return 0.0
        eco = self._eco_balance()
        eco_pay = _amt(min(amount * ARB_REWARD_ECO_RATIO, eco))
        pool = float(self.store.arb_pools.get(pool_key, 0.0))
        dep_pay = _amt(min(amount - eco_pay, max(0.0, pool)))
        paid = _amt(eco_pay + dep_pay)
        if paid <= 0:
            return 0.0
        if eco_pay > 0:
            self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(eco - eco_pay)
        if dep_pay > 0:
            self.store.arb_pools[pool_key] = _amt(pool - dep_pay)
        ar = self.store.arb_arbitrators.get(addr)
        if ar:
            ar["revenue"] = _amt(ar.get("revenue", 0.0) + paid)
        self.store.balances[addr] = _amt(self.store.balances.get(addr, 0.0) + paid)
        return dep_pay

    def _slash_stake(self, addr, amount) -> float:
        """从仲裁员质押中扣除罚金（罚没资金进入生态基金），返回实际扣款。"""
        ar = self.store.arb_arbitrators.get(addr)
        if not ar:
            return 0.0
        stake = float(ar.get("stake", 0.0))
        cut = _amt(min(amount, stake))
        ar["stake"] = _amt(stake - cut)
        self.store.arb_slashed = _amt(self.store.arb_slashed + cut)
        if cut > 0:
            self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(self._eco_balance() + cut)
        if ar["stake"] <= 0:
            ar["stake"] = 0.0
            self._ban(addr, "质押清零")
        return cut

    def _rep_delta(self, addr, delta, reason="") -> float:
        """调整仲裁信誉分（含 0 分永久取消资格）。"""
        ar = self.store.arb_arbitrators.get(addr)
        if not ar:
            return 0.0
        new_rep = self._clamp_rep(float(ar.get("rep", ARB_REP_INIT)) + float(delta))
        ar["rep"] = new_rep
        if new_rep <= 0.0:
            self._ban(addr, reason or "信誉分归零")
        elif new_rep < ARB_REP_SUSPEND and ar.get("status") in ("active", "observing"):
            ar["status"] = "suspended"
            self._notify(addr, "arb_suspend", "仲裁资格暂停",
                         f"信誉分低于 {ARB_REP_SUSPEND:.0f}，需重新质押激活", "")
        return new_rep

    def _ban(self, addr, reason="") -> None:
        """永久取消仲裁员资格（保留状态记录供查询）。"""
        self.store.arb_banned.add(addr)
        ar = self.store.arb_arbitrators.get(addr)
        if ar:
            ar["status"] = "banned"
            ar["ban_reason"] = reason
        self._notify(addr, "arb_ban", "永久取消仲裁资格",
                     f"原因：{reason or '违规'}。质押已罚没。", "")

    def _pending_release(self, addr, amount, release_ts) -> None:
        """质押进入冷静期，到期后由 nova:arb:claim_stake 领取。"""
        self.store.arb_stake_pending[addr] = [float(amount), float(release_ts)]

    # ======================================================================
    # 利益回避：转账 / 推荐 / 主动声明
    # ======================================================================
    def _has_direct_transfer(self, addr, party, now) -> bool:
        """近 30 天内 addr 与 party 是否有直接转账记录。"""
        cutoff = now - ARB_SUSPECT_WINDOW_DAYS * 86400
        for t in self.store.tx_history.values():
            ts = float(t.get("ts", 0) or 0)
            if ts < cutoff or float(t.get("amount", 0) or 0) <= 0:
                continue
            s, r = t.get("sender", ""), t.get("receiver", "")
            if s == addr and r == party:
                return True
            if s == party and r == addr:
                return True
        return False

    def _has_referral(self, addr, party) -> bool:
        """addr 与 party 是否存在推荐关系（双向）。"""
        refs = self.store.referrals
        return (refs.get(addr) == party or refs.get(party) == addr
                or (addr in refs and party in refs and refs.get(addr) == refs.get(party)))

    def _conflicted(self, addr, buyer, seller, now) -> bool:
        if addr in (buyer, seller):
            return True
        if self._has_direct_transfer(addr, buyer, now) or self._has_direct_transfer(addr, seller, now):
            return True
        if self._has_referral(addr, buyer) or self._has_referral(addr, seller):
            return True
        return False

    # ======================================================================
    # 随机抽取（VRF）与防串通
    # ======================================================================
    def _combo_count(self, combo) -> int:
        """同一组仲裁员在 30 天内被抽取的次数。"""
        cutoff = time.time() - ARB_SUSPECT_WINDOW_DAYS * 86400
        target = tuple(sorted(combo))
        count = 0
        for c in self.store.arb_cases.values():
            first_panel = c.get("panel")
            second_panel = (c.get("second") or {}).get("panel")
            for p, is_first in ((first_panel, True), (second_panel, False)):
                if not p:
                    continue
                if is_first and float(c.get("drawn_at", 0)) < cutoff:
                    continue
                members = tuple(sorted(v for v in p.values() if isinstance(v, str)))
                if members == target:
                    count += 1
        return count

    def _panel_members(self, case, second=False) -> list:
        if second:
            sec = case.get("second") or {}
            panel = sec.get("panel", {})
        else:
            panel = case.get("panel", {})
        return [v for v in panel.values() if isinstance(v, str)]

    def _eligible_pool(self, case, second=False, extra_exclude=frozenset()) -> list:
        """抽取候选池：在职 + 信誉达标 + 无利益关联 + 未在庭。"""
        now = time.time()
        buyer, seller = case["buyer"], case["seller"]
        taken = set()
        for p in (case.get("panel"), (case.get("second") or {}).get("panel")):
            if p:
                taken.update(v for v in p.values() if isinstance(v, str))
        pool = []
        for addr, ar in self.store.arb_arbitrators.items():
            if ar.get("status") != "active":
                continue
            if addr in self.store.arb_banned:
                continue
            if ar.get("observe_until", 0) and float(ar["observe_until"]) > now:
                continue
            if float(ar.get("rep", ARB_REP_INIT)) < ARB_REP_SUSPEND:
                continue
            if addr in taken or addr in extra_exclude:
                continue
            if addr in (buyer, seller):
                continue
            if addr in case.get("excluded", []):
                continue
            if self._conflicted(addr, buyer, seller, now):
                continue
            pool.append(addr)
        return pool

    def _draw_panel(self, case, size, second=False) -> list:
        """VRF 随机抽取 N 名仲裁员（确定性、可复算、防串通去重）。

        返回选中地址列表；候选不足时返回 []，案件保持待抽取状态。
        """
        pool = self._eligible_pool(case, second=second)
        if len(pool) < size:
            return []
        base = self._vrf(self.store.arb_vrf_seed, case["id"], "2nd" if second else "1st")
        selected = sorted(pool, key=lambda a: self._vrf(str(base), a))[:size]
        # 防串通：避免同一组合在 30 天内重复出现（确定性递进盐重试）
        combo = tuple(sorted(selected))
        if self._combo_count(combo) > 0:
            for salt in range(1, 64):
                alt = sorted(pool, key=lambda a: self._vrf(str(base), str(salt), a))[:size]
                alt_combo = tuple(sorted(alt))
                if self._combo_count(alt_combo) == 0:
                    selected = alt
                    combo = alt_combo
                    break
        # VRF 种子前滚：下一次抽取不可预测
        self.store.arb_vrf_seed = _h("|".join(
            [self.store.arb_vrf_seed, case["id"], str(second)] + sorted(selected)))
        # 记录抽取历史（串通检测）
        for addr in selected:
            ar = self.store.arb_arbitrators.get(addr)
            if ar:
                ar["panel_history"].append(time.time())
                if len(ar["panel_history"]) > 50:
                    del ar["panel_history"][:len(ar["panel_history"]) - 50]
                ar["history"].append({"kind": "drawn", "case_id": case["id"], "at": time.time()})
        return selected

    def _assign_panel(self, case, addrs, second=False) -> None:
        """给案件分配匿名编号（1..N），并通知被抽中仲裁员。"""
        now = time.time()
        if second:
            sec = case["second"]
            panel = sec.setdefault("panel", {})
            meta = sec.setdefault("panel_meta", {})
        else:
            panel = case.setdefault("panel", {})
            meta = case.setdefault("panel_meta", {})
        number = 1
        for addr in addrs:
            while str(number) in panel:
                number += 1
            num = str(number)
            panel[num] = addr
            meta[addr] = {"number": num, "assigned_at": now,
                          "deadline": now + ARB_VOTE_WINDOW, "voted": False,
                          "side": "", "replaced": False, "conflict": False}
            number += 1
            self._notify(addr, "arb_drawn", "您被抽中担任仲裁员",
                         f"案件 {case['id']} 需要您在 72 小时内投票（匿名编号 #{num}）。",
                         case["id"])
        if not second:
            case["drawn_at"] = now
        self._event("draw", f"案件 {case['id']} 完成仲裁员抽取",
                    f"已抽取 {len(addrs)} 名仲裁员（当事人匿名），进入投票阶段。", case["id"])

    # ======================================================================
    # 通用校验/执行入口
    # ======================================================================
    def validate_op(self, tx) -> bool:
        d = self._parse_op(tx)
        if not isinstance(d, dict):
            return False
        op = d.get("op")
        kind = self.OPS.get(op)
        if not kind or tx.sender != tx.receiver:
            return False
        fn = getattr(self, f"{kind}_validate")
        try:
            return bool(fn(d, tx))
        except Exception:
            return False

    def apply_op(self, tx):
        d = self._parse_op(tx)
        kind = self.OPS.get(d.get("op"))
        getattr(self, f"{kind}_apply")(tx, d)

    @staticmethod
    def _parse_op(tx):
        try:
            d = json.loads(tx.data)
        except Exception:
            return None
        return d if isinstance(d, dict) else None

    # ======================================================================
    # 1. 仲裁员注册与质押
    # ======================================================================
    def apply_validate(self, d, tx):
        addr = tx.sender
        if _amt(tx.amount) != _amt(ARB_STAKE):
            return False
        if addr in self.store.arb_arbitrators:
            return False
        cand = self.store.arb_candidates.get(addr)
        if cand and cand.get("status") == "voting":
            return False  # 已有进行中的申请投票
        if self._is_banned(addr):
            return False
        first = self._first_tx_ts(addr)
        if not first or time.time() - first < ARB_ACCOUNT_AGE_DAYS * 86400:
            return False  # 地址注册时长不足 30 天
        if self._chain_rep(addr) < ARB_REP_MIN_APPLY:
            return False  # 信誉分 < 70
        return True

    def apply_apply(self, tx, d):
        addr = tx.sender
        now = time.time()
        self.store.arb_candidates[addr] = {
            "addr": addr, "applied_at": now, "kind": "first",
            "votes": {"yes": 0.0, "no": 0.0}, "voted": {},
            "status": "voting", "settled_at": 0, "renew_from": 0,
        }
        self.store.arb_pools[f"cand_{addr}"] = ARB_STAKE  # 质押锁定在仲裁合约
        self._event("candidate", "新的仲裁员申请",
                    f"{addr[:12]}... 已质押 {ARB_STAKE:.0f} NOVA 进入候选池，等待社区投票。", "")
        self._notify(addr, "arb_candidate", "申请已进入候选池",
                     f"质押 {ARB_STAKE:.0f} NOVA 已锁定，社区投票期 {ARB_VOTE_PERIOD_DAYS} 天。", "")

    def candidate_vote_validate(self, d, tx):
        addr = tx.sender
        cand_addr = str(d.get("candidate", ""))
        side = str(d.get("side", ""))
        if tx.amount != 0 or side not in ("yes", "no"):
            return False
        cand = self.store.arb_candidates.get(cand_addr)
        if not cand or cand.get("status") != "voting":
            return False
        if addr == cand_addr or addr in cand.get("voted", {}):
            return False
        if time.time() - float(cand.get("applied_at", 0)) > ARB_VOTE_PERIOD_DAYS * 86400:
            return False  # 投票期已过，等待自动统计
        if self.store.balances.get(addr, 0.0) < 1.0:
            return False  # 1 NOVA = 1 票，需至少持有 1 NOVA
        return True

    def candidate_vote_apply(self, tx, d):
        addr = tx.sender
        cand_addr = str(d.get("candidate", ""))
        side = str(d.get("side", ""))
        cand = self.store.arb_candidates[cand_addr]
        # 1 NOVA = 1 票：gas 已扣除，用微小补偿保持余额语义（floor 前余额）
        power = float(int(self.store.balances.get(addr, 0.0) + 1e-4))
        cand["votes"][side] = _amt(cand["votes"][side] + power)
        cand["voted"][addr] = side
        cand["votes"]["total"] = _amt(cand["votes"]["yes"] + cand["votes"]["no"])

    def candidate_settle_validate(self, d, tx):
        addr = str(d.get("candidate", ""))
        cand = self.store.arb_candidates.get(addr)
        if not cand or cand.get("status") != "voting":
            return False
        return time.time() - float(cand.get("applied_at", 0)) > ARB_VOTE_PERIOD_DAYS * 86400

    def candidate_settle_apply(self, tx, d):
        self._settle_candidate(str(d.get("candidate", "")))

    def _settle_candidate(self, cand_addr):
        """投票结束自动统计：通过 -> 加入仲裁员池；未通过 -> 质押进入冷静期。"""
        cand = self.store.arb_candidates.get(cand_addr)
        if not cand or cand.get("status") != "voting":
            return
        now = time.time()
        yes, no = float(cand["votes"]["yes"]), float(cand["votes"]["no"])
        total = yes + no
        passed = yes > no * ARB_PASS_RATIO and total > ARB_MIN_VOTES
        cand["status"] = "passed" if passed else "failed"
        cand["settled_at"] = now
        pool_key = f"cand_{cand_addr}"
        stake = float(self.store.arb_pools.get(pool_key, ARB_STAKE))
        self.store.arb_pools.pop(pool_key, None)
        if passed:
            rep = self._clamp_rep(max(ARB_REP_INIT, self._chain_rep(cand_addr)))
            if cand.get("kind") == "renew":
                ar = self.store.arb_arbitrators.get(cand_addr)
                if ar:
                    ar["term_end"] = float(ar.get("term_end", now)) + ARB_TERM_DAYS * 86400
                    ar["status"] = "active"
                    ar["renewed_at"] = now
                    self._notify(cand_addr, "arb_renew", "连任投票通过",
                                 f"任期延长 {ARB_TERM_DAYS} 天，至 "
                                 f"{time.strftime('%Y-%m-%d', time.localtime(ar['term_end']))}。", "")
            else:
                self.store.arb_arbitrators[cand_addr] = {
                    "addr": cand_addr, "stake": stake, "rep": rep,
                    "term_start": now, "term_end": now + ARB_TERM_DAYS * 86400,
                    "cases": 0, "correct": 0, "streak": 0, "revenue": 0.0,
                    "status": "active", "exit_notice_at": 0, "exit_ready_at": 0,
                    "observe_until": 0, "declared_conflicts": 0,
                    "recent_votes": [], "panel_history": [], "history": [],
                }
                self._notify(cand_addr, "arb_passed", "社区投票通过",
                             "您已正式成为仲裁员，任期 90 天。", "")
            self._event("candidate", "仲裁员申请通过",
                        f"{cand_addr[:12]}... 获得 {yes:.0f} 赞成票，成为在职仲裁员。", "")
        else:
            # 未通过：质押 7 天冷静期后返还
            if cand.get("kind") == "renew":
                ar = self.store.arb_arbitrators.get(cand_addr)
                if ar:
                    stake = float(ar.get("stake", 0.0))
                    ar["status"] = "retired"
                    ar["stake"] = 0.0
                    self._notify(cand_addr, "arb_renew_fail", "连任投票未通过",
                                 "仲裁资格已结束，质押进入 7 天冷静期后返还。", "")
            self._pending_release(cand_addr, stake, now + ARB_EXIT_COOLDOWN_DAYS * 86400)
            self._notify(cand_addr, "arb_failed", "社区投票未通过",
                         f"赞成 {yes:.0f} / 反对 {no:.0f}，未达通过条件。质押将进入 7 天冷静期返还。", "")
            self._event("candidate", "仲裁员申请未通过",
                        f"{cand_addr[:12]}... 社区投票未通过，质押进入冷静期。", "")

    def renew_validate(self, d, tx):
        addr = tx.sender
        if tx.amount != 0:
            return False
        ar = self.store.arb_arbitrators.get(addr)
        if not ar or ar.get("status") != "active":
            return False
        cand = self.store.arb_candidates.get(addr)
        if cand and cand.get("status") == "voting":
            return False  # 已有进行中的连任投票
        now = time.time()
        term_end = float(ar.get("term_end", 0))
        if not (0 < term_end - now <= ARB_RENEW_WINDOW_DAYS * 86400):
            return False  # 仅任期结束前 7 天内可申请连任
        return True

    def renew_apply(self, tx, d):
        addr = tx.sender
        ar = self.store.arb_arbitrators[addr]
        now = time.time()
        self.store.arb_candidates[addr] = {
            "addr": addr, "applied_at": now, "kind": "renew",
            "votes": {"yes": 0.0, "no": 0.0}, "voted": {},
            "status": "voting", "settled_at": 0,
            "renew_from": float(ar.get("term_end", now)),
        }
        ar["status"] = "renewing"
        self._notify(addr, "arb_renew_vote", "连任申请已提交",
                     f"需重新社区投票，任期满 {time.strftime('%Y-%m-%d', time.localtime(ar['term_end']))} "
                     f"前未通过将自动退休。", "")

    def reactivate_validate(self, d, tx):
        addr = tx.sender
        if _amt(tx.amount) != _amt(ARB_STAKE):
            return False
        ar = self.store.arb_arbitrators.get(addr)
        if not ar or ar.get("status") != "suspended" or self._is_banned(addr):
            return False
        return True

    def reactivate_apply(self, tx, d):
        addr = tx.sender
        ar = self.store.arb_arbitrators[addr]
        now = time.time()
        ar["stake"] = _amt(float(ar.get("stake", 0.0)) + ARB_STAKE)
        ar["rep"] = ARB_REP_INIT  # 重新质押激活：信誉分回到初始值
        ar["status"] = "active"
        ar["observe_until"] = 0.0
        ar["term_end"] = now + ARB_TERM_DAYS * 86400
        self._notify(addr, "arb_reactivate", "重新质押激活成功",
                     f"已重新质押 {ARB_STAKE:.0f} NOVA，仲裁资格恢复，任期 90 天。", "")

    def exit_validate(self, d, tx):
        addr = tx.sender
        if tx.amount != 0:
            return False
        ar = self.store.arb_arbitrators.get(addr)
        if not ar or ar.get("status") not in ("active", "renewing"):
            return False
        if ar.get("exit_notice_at"):
            return False
        # 有未完成案件时不可退出
        for c in self.store.arb_cases.values():
            if c.get("status") in ("pending_draw", "voting", "second_pending", "second_voting"):
                members = self._panel_members(c) + self._panel_members(c, second=True)
                if addr in members:
                    return False
        return True

    def exit_apply(self, tx, d):
        addr = tx.sender
        ar = self.store.arb_arbitrators[addr]
        now = time.time()
        ar["status"] = "leaving"
        ar["exit_notice_at"] = now
        ar["exit_ready_at"] = now + ARB_EXIT_NOTICE_DAYS * 86400
        self._notify(addr, "arb_exit", "退出申请已登记",
                     "7 天声明期后质押进入 7 天冷静期，合计 14 天后可领取。", "")

    def claim_stake_validate(self, d, tx):
        addr = tx.sender
        if tx.amount != 0:
            return False
        p = self.store.arb_stake_pending.get(addr)
        return bool(p) and time.time() >= float(p[1])

    def claim_stake_apply(self, tx, d):
        addr = tx.sender
        amount, _release = self.store.arb_stake_pending.pop(addr)
        self.store.balances[addr] = _amt(self.store.balances.get(addr, 0.0) + amount)
        self._notify(addr, "arb_claim", "质押已返还",
                     f"{_amt(amount):.0f} NOVA 已退回账户。", "")


    # ======================================================================
    # 2. 案件仲裁流程
    # ======================================================================
    def _deposit_for(self, buyer) -> float:
        """恶意投诉者保证金提高至 50 NOVA。"""
        m = self.store.arb_malicious.get(buyer)
        if m and float(m.get("loss_count", 0)) >= ARB_MALICIOUS_LOSS_LIMIT:
            return ARB_MALICIOUS_DEPOSIT
        return ARB_COMPLAINT_DEPOSIT

    def complain_validate(self, d, tx):
        addr = tx.sender
        seller = str(d.get("seller", ""))
        trade_id = str(d.get("trade_id", ""))
        reason = str(d.get("reason", ""))
        if seller == addr or not (0 < len(reason) <= ARB_REASON_MAX):
            return False
        if not (0 < len(trade_id) <= ARB_TRADE_ID_MAX):
            return False
        ev = str(d.get("evidence", "") or "")
        if len(ev) > ARB_EVIDENCE_MAX:
            return False
        deposit = self._deposit_for(addr)
        if _amt(tx.amount) != _amt(deposit):
            return False
        # 恶意投诉名单锁定期间不可发起投诉
        m = self.store.arb_malicious.get(addr)
        if m and float(m.get("lock_until", 0)) > time.time():
            return False
        # 同一交易不可重复投诉（未结案）
        for c in self.store.arb_cases.values():
            if (c.get("buyer") == addr and c.get("trade_id") == trade_id
                    and c.get("status") in ("pending_draw", "voting", "second_pending", "second_voting")):
                return False
        freeze = _amt(deposit * ARB_SELLER_FREEZE_MULT)
        if self.store.balances.get(seller, 0.0) < freeze:
            return False  # 卖家保证金不足以冻结
        return True

    def complain_apply(self, tx, d):
        buyer = tx.sender
        seller = str(d.get("seller", ""))
        deposit = _amt(tx.amount)
        freeze = _amt(deposit * ARB_SELLER_FREEZE_MULT)
        seq = self.store.arb_case_seq + 1
        self.store.arb_case_seq = seq
        cid = f"arb_{seq}"
        now = time.time()
        case = {
            "id": cid, "stage": 1, "buyer": buyer, "seller": seller,
            "trade_id": str(d.get("trade_id", "")), "reason": str(d.get("reason", "")),
            "evidence": str(d.get("evidence", "") or ""),
            "deposit": deposit, "seller_frozen": freeze,
            "filed_at": now, "status": "pending_draw",
            "drawn_at": 0.0, "panel": {}, "panel_meta": {},
            "votes": {}, "revealed": False, "result": "",
            "decided_at": 0.0, "appeal_deadline": 0.0,
            "second": None, "payouts": {}, "excluded": [], "events": [],
        }
        self.store.arb_cases[cid] = case
        # 保证金池：投诉保证金（40% 报酬来源）
        self.store.arb_pools[f"case_{cid}"] = deposit
        # 合约自动冻结卖家保证金
        self.store.balances[seller] = _amt(self.store.balances.get(seller, 0.0) - freeze)
        self._notify(seller, "arb_complaint", "您收到一笔投诉",
                     f"买家对交易 {case['trade_id'][:32]} 发起投诉，"
                     f"{_amt(freeze):.0f} NOVA 保证金已冻结。", cid)
        self._event("case", "新投诉", f"{buyer[:12]}... 对 {seller[:12]}... 发起投诉。", cid)
        case["events"].append({"kind": "filed", "at": now, "msg": "投诉已发起"})

    def draw_validate(self, d, tx):
        cid = str(d.get("case_id", ""))
        case = self.store.arb_cases.get(cid)
        if tx.amount != 0 or not case or case.get("status") != "pending_draw":
            return False
        return True

    def draw_apply(self, tx, d):
        self._auto_draw(str(d.get("case_id", "")))

    def _auto_draw(self, cid) -> bool:
        """VRF 抽取 3 名仲裁员；候选不足保持待抽取（maintain 自动重试）。"""
        case = self.store.arb_cases.get(cid)
        if not case or case.get("status") != "pending_draw":
            return False
        selected = self._draw_panel(case, ARB_PANEL_SIZE)
        if len(selected) < ARB_PANEL_SIZE:
            return False
        self._assign_panel(case, selected)
        case["status"] = "voting"
        case["events"].append({"kind": "draw", "at": time.time(),
                               "msg": f"已抽取 {len(selected)} 名仲裁员"})
        return True

    def _active_slots(self, case, second=False) -> list:
        if second:
            meta = (case.get("second") or {}).get("panel_meta", {})
        else:
            meta = case.get("panel_meta", {})
        return [m for m in meta.values() if not m.get("replaced")]

    def _all_voted(self, case, second=False) -> bool:
        votes = (case.get("second") or {}).get("votes", {}) if second else case.get("votes", {})
        active = self._active_slots(case, second=second)
        if not active:
            return False
        return all(votes.get(m["number"]) in ("buyer", "seller") for m in active)

    def vote_validate(self, d, tx):
        addr = tx.sender
        cid = str(d.get("case_id", ""))
        number = str(d.get("number", ""))
        side = str(d.get("side", ""))
        case = self.store.arb_cases.get(cid)
        if tx.amount != 0 or side not in ("buyer", "seller") or not case:
            return False
        stage = int(d.get("stage", 1) or 1)
        if stage == 2:
            if case.get("status") != "second_voting" or not case.get("second"):
                return False
            panel, meta, votes = case["second"]["panel"], case["second"]["panel_meta"], case["second"]["votes"]
        else:
            if case.get("status") != "voting":
                return False
            panel, meta, votes = case["panel"], case["panel_meta"], case["votes"]
        if number not in panel or panel[number] != addr:
            return False  # 编号必须与本人一致（匿名投票）
        m = meta.get(addr)
        if not m or m.get("replaced") or votes.get(number) in ("buyer", "seller"):
            return False
        if time.time() > float(m.get("deadline", 0)):
            return False  # 已超时，等待重新抽取替代
        return True

    def vote_apply(self, tx, d):
        addr = tx.sender
        cid = str(d.get("case_id", ""))
        number = str(d.get("number", ""))
        side = str(d.get("side", ""))
        stage = int(d.get("stage", 1) or 1)
        case = self.store.arb_cases[cid]
        if stage == 2:
            sec = case["second"]
            sec["votes"][number] = side
            sec["panel_meta"][addr]["voted"] = True
            sec["panel_meta"][addr]["side"] = side
            now = time.time()
            self._pay_arb_reward(addr, ARB_VOTE_REWARD, f"case_{cid}")
            case["events"].append({"kind": "vote2", "at": now,
                                   "msg": f"#{number} 已完成投票"})
            if self._all_voted(case, second=True):
                self._execute_second(case)
        else:
            case["votes"][number] = side
            case["panel_meta"][addr]["voted"] = True
            case["panel_meta"][addr]["side"] = side
            ar = self.store.arb_arbitrators.get(addr)
            if ar:
                ar["cases"] = int(ar.get("cases", 0)) + 1
                ar["recent_votes"].append(side)
                if len(ar["recent_votes"]) > 30:
                    del ar["recent_votes"][:len(ar["recent_votes"]) - 30]
                ar["history"].append({"kind": "vote", "case_id": cid, "side": side,
                                      "at": time.time()})
            now = time.time()
            self._pay_arb_reward(addr, ARB_VOTE_REWARD, f"case_{cid}")
            case["events"].append({"kind": "vote", "at": now,
                                   "msg": f"#{number} 已完成投票"})
            if self._all_voted(case):
                self._execute_case(case)

    def decline_validate(self, d, tx):
        addr = tx.sender
        cid = str(d.get("case_id", ""))
        case = self.store.arb_cases.get(cid)
        if tx.amount != 0 or not case:
            return False
        if case.get("status") not in ("voting", "second_voting"):
            return False
        stage = 2 if case.get("status") == "second_voting" else 1
        meta = (case.get("second") or {}).get("panel_meta", {}) if stage == 2 else case.get("panel_meta", {})
        m = meta.get(addr)
        return bool(m and not m.get("replaced"))

    def decline_apply(self, tx, d):
        addr = tx.sender
        cid = str(d.get("case_id", ""))
        case = self.store.arb_cases[cid]
        stage = 2 if case.get("status") == "second_voting" else 1
        if stage == 2:
            sec = case["second"]
            meta = sec["panel_meta"]
            panel = sec["panel"]
        else:
            meta = case["panel_meta"]
            panel = case["panel"]
        m = meta[addr]
        m["replaced"] = True
        m["conflict"] = True
        num = m["number"]
        panel.pop(num, None)
        ar = self.store.arb_arbitrators.get(addr)
        if ar:
            ar["declared_conflicts"] = int(ar.get("declared_conflicts", 0)) + 1
            self._rep_delta(addr, 1.0, "主动声明利益冲突")  # +1 信誉分
            ar["history"].append({"kind": "decline", "case_id": cid, "at": time.time()})
        self._notify(addr, "arb_declined", "利益冲突已声明",
                     f"您已退出案件 {cid}，信誉分 +1，系统将重新抽取替代仲裁员。", cid)
        case["excluded"].append(addr)
        # 重新抽取替代仲裁员
        if stage == 2 and case.get("second"):
            pool = self._eligible_pool(case, second=True, extra_exclude={addr})
        else:
            pool = self._eligible_pool(case, extra_exclude={addr})
        if pool:
            repl = sorted(pool, key=lambda a: self._vrf(self.store.arb_vrf_seed, cid, "repl", a))[0]
            now = time.time()
            panel[num] = repl
            meta[repl] = {"number": num, "assigned_at": now,
                          "deadline": now + ARB_VOTE_WINDOW, "voted": False,
                          "side": "", "replaced": False, "conflict": False}
            self._notify(repl, "arb_drawn", "您被抽中担任替代仲裁员",
                         f"案件 {cid} 需要您在 72 小时内投票（匿名编号 #{num}）。", cid)
            ar2 = self.store.arb_arbitrators.get(repl)
            if ar2:
                ar2["panel_history"].append(time.time())
                ar2["history"].append({"kind": "drawn", "case_id": cid, "at": time.time()})
        case["events"].append({"kind": "decline", "at": time.time(),
                               "msg": "仲裁员声明利益冲突，已重新抽取"})

    def second_validate(self, d, tx):
        addr = tx.sender
        cid = str(d.get("case_id", ""))
        case = self.store.arb_cases.get(cid)
        if not case or case.get("stage") != 1 or case.get("second"):
            return False
        if case.get("status") != "decided" or addr not in (case["buyer"], case["seller"]):
            return False
        if time.time() - float(case.get("decided_at", 0)) > ARB_APPEAL_WINDOW_DAYS * 86400:
            return False  # 7 天内可发起二次仲裁
        return _amt(tx.amount) == _amt(ARB_SECOND_DEPOSIT)

    def second_apply(self, tx, d):
        addr = tx.sender
        cid = str(d.get("case_id", ""))
        case = self.store.arb_cases[cid]
        now = time.time()
        case["second"] = {
            "appellant": addr, "deposit": ARB_SECOND_DEPOSIT, "filed_at": now,
            "panel": {}, "panel_meta": {}, "votes": {}, "result": "",
            "decided_at": 0.0,
        }
        self.store.arb_pools[f"case_{cid}"] = _amt(
            float(self.store.arb_pools.get(f"case_{cid}", 0.0)) + ARB_SECOND_DEPOSIT)
        case["status"] = "second_pending"
        case["events"].append({"kind": "second", "at": now,
                               "msg": f"{addr[:12]}... 发起二次仲裁"})
        self._notify(case["buyer"] if addr != case["buyer"] else case["seller"],
                     "arb_second", "对方发起二次仲裁",
                     f"案件 {cid} 进入二次仲裁，将抽取 7 名仲裁员。", cid)
        self._auto_draw_second(cid)

    def _auto_draw_second(self, cid) -> bool:
        case = self.store.arb_cases.get(cid)
        if not case or case.get("status") != "second_pending":
            return False
        selected = self._draw_panel(case, ARB_SECOND_PANEL_SIZE, second=True)
        if len(selected) < ARB_SECOND_PANEL_SIZE:
            return False
        self._assign_panel(case, selected, second=True)
        case["status"] = "second_voting"
        case["events"].append({"kind": "draw2", "at": time.time(),
                               "msg": f"二次仲裁已抽取 {len(selected)} 名仲裁员"})
        return True

    # ======================================================================
    # 3. 裁决执行与二次仲裁
    # ======================================================================
    def _tally(self, votes) -> str:
        b = sum(1 for v in votes.values() if v == "buyer")
        s = sum(1 for v in votes.values() if v == "seller")
        if b == s:
            return "seller"  # 平票默认卖家（保守）
        return "buyer" if b > s else "seller"

    def _execute_case(self, case):
        """首次裁决：3:0 或 2:1 自动执行。"""
        now = time.time()
        winner = self._tally(case["votes"])
        case["result"] = winner
        case["decided_at"] = now
        case["appeal_deadline"] = now + ARB_APPEAL_WINDOW_DAYS * 86400
        case["status"] = "decided"
        case["revealed"] = True  # 投票结束，公开投票人
        # 激励：与多数一致 +1 信誉分、连续正确奖励
        self._settle_first_incentives(case)
        # 自动执行赔付
        self._payout(case, winner)
        self._event("case", f"案件 {case['id']} 已裁决",
                    f"支持{'买家' if winner == 'buyer' else '卖家'}，赔付已自动执行。", case["id"])
        self._notify(case["buyer"], "arb_result", "您的投诉已有裁决结果",
                     f"案件 {case['id']} 最终支持{'买家' if winner == 'buyer' else '卖家'}。"
                     f"7 天内可发起二次仲裁。", case["id"])
        self._notify(case["seller"], "arb_result", "您的案件已有裁决结果",
                     f"案件 {case['id']} 最终支持{'买家' if winner == 'buyer' else '卖家'}。"
                     f"7 天内可发起二次仲裁。", case["id"])
        case["events"].append({"kind": "decided", "at": now,
                               "msg": f"首次裁决：支持{'买家' if winner == 'buyer' else '卖家'}"})

    def _settle_first_incentives(self, case):
        """首次裁决激励：与最终多数一致 +1 信誉分；连续 10 次正确 +10 NOVA。"""
        winner = case["result"]
        for num, addr in case["panel"].items():
            m = case["panel_meta"].get(addr)
            if not m or m.get("replaced") or not m.get("voted"):
                continue
            ar = self.store.arb_arbitrators.get(addr)
            if not ar:
                continue
            if m.get("side") == winner:
                ar["correct"] = int(ar.get("correct", 0)) + 1
                ar["streak"] = int(ar.get("streak", 0)) + 1
                self._rep_delta(addr, ARB_MAJORITY_REP, "")
                if ar["streak"] >= ARB_STREAK_LEN:
                    ar["streak"] = 0
                    self._pay_arb_reward(addr, ARB_STREAK_REWARD, f"case_{case['id']}")
                    ar["history"].append({"kind": "streak", "case_id": case["id"],
                                          "at": time.time()})
            else:
                ar["streak"] = 0

    def _payout(self, case, winner):
        """自动执行赔付。

        支持买家：卖家冻结保证金全额赔付买家 + 投诉保证金（扣报酬后）退还；
        支持卖家：投诉保证金剩余 40% 赔偿卖家、60% 进入生态基金，冻结保证金退回。
        """
        deposit_pool = float(self.store.arb_pools.get(f"case_{case['id']}", 0.0))
        frozen = float(case.get("seller_frozen", 0.0))
        seller = case["seller"]
        buyer = case["buyer"]
        pay = {}
        if winner == "buyer":
            # 卖家冻结保证金 -> 买家
            self.store.balances[buyer] = _amt(self.store.balances.get(buyer, 0.0) + frozen)
            pay["to_buyer_frozen"] = frozen
            # 投诉保证金剩余 -> 退还买家
            remain = _amt(deposit_pool)
            if remain > 0:
                self.store.balances[buyer] = _amt(self.store.balances.get(buyer, 0.0) + remain)
                self.store.arb_pools.pop(f"case_{case['id']}", None)
                pay["to_buyer_deposit"] = remain
        else:
            # 冻结保证金退回卖家
            self.store.balances[seller] = _amt(self.store.balances.get(seller, 0.0) + frozen)
            pay["to_seller_frozen"] = frozen
            # 投诉保证金剩余：40% 赔偿卖家，60% 进入生态基金
            remain = _amt(deposit_pool)
            if remain > 0:
                seller_share = _amt(remain * ARB_SELLER_WIN_RATIO)
                eco_share = _amt(remain - seller_share)
                self.store.balances[seller] = _amt(self.store.balances.get(seller, 0.0) + seller_share)
                self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(self._eco_balance() + eco_share)
                self.store.arb_pools.pop(f"case_{case['id']}", None)
                pay["to_seller_share"] = seller_share
                pay["to_eco"] = eco_share
        case["payouts"][f"first_{winner}"] = pay

    def _execute_second(self, case):
        """二次仲裁：最终结果；推翻时一次仲裁员扣 10 NOVA + 信誉分 -5。"""
        sec = case["second"]
        now = time.time()
        winner2 = self._tally(sec["votes"])
        sec["result"] = winner2
        sec["decided_at"] = now
        overturned = winner2 != case.get("result")
        # 二次仲裁员激励
        for num, addr in sec["panel"].items():
            m = sec["panel_meta"].get(addr)
            if not m or m.get("replaced") or not m.get("voted"):
                continue
            ar = self.store.arb_arbitrators.get(addr)
            if not ar:
                continue
            ar["cases"] = int(ar.get("cases", 0)) + 1
            ar["recent_votes"].append(m.get("side"))
            if len(ar["recent_votes"]) > 30:
                del ar["recent_votes"][:len(ar["recent_votes"]) - 30]
            if m.get("side") == winner2:
                ar["correct"] = int(ar.get("correct", 0)) + 1
                ar["streak"] = int(ar.get("streak", 0)) + 1
                self._rep_delta(addr, ARB_MAJORITY_REP, "")
                if ar["streak"] >= ARB_STREAK_LEN:
                    ar["streak"] = 0
                    self._pay_arb_reward(addr, ARB_STREAK_REWARD, f"case_{case['id']}")
            else:
                ar["streak"] = 0
            ar["history"].append({"kind": "vote2", "case_id": case["id"],
                                  "side": m.get("side"), "at": now})
        # 二次仲裁保证金处理
        pool_key = f"case_{case['id']}"
        pool = float(self.store.arb_pools.get(pool_key, 0.0))
        if overturned:
            # 推翻：上诉人拿回保证金
            self.store.balances[sec["appellant"]] = _amt(
                self.store.balances.get(sec["appellant"], 0.0) + ARB_SECOND_DEPOSIT)
            self.store.arb_pools[pool_key] = _amt(max(0.0, pool - ARB_SECOND_DEPOSIT))
        else:
            # 未推翻：50% 入生态基金，50% 留在保证金池
            eco_add = _amt(min(ARB_SECOND_DEPOSIT, pool) * ARB_SECOND_FAIL_ECO_RATIO)
            self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(self._eco_balance() + eco_add)
            self.store.arb_pools[pool_key] = _amt(max(0.0, pool - eco_add))
        if overturned:
            # 一次仲裁员惩罚：-10 NOVA + 信誉分 -5
            for addr in self._panel_members(case):
                self._slash_stake(addr, ARB_OVERTURN_PENALTY)
                self._rep_delta(addr, -ARB_OVERTURN_REP, "二次仲裁推翻")
                ar = self.store.arb_arbitrators.get(addr)
                if ar:
                    ar["streak"] = 0
                    ar["history"].append({"kind": "overturned", "case_id": case["id"], "at": now})
                self._notify(addr, "arb_overturned", "裁决被二次仲裁推翻",
                             f"案件 {case['id']}：扣 {ARB_OVERTURN_PENALTY:.0f} NOVA，"
                             f"信誉分 -{ARB_OVERTURN_REP:.0f}。", case["id"])
            # 回滚首次赔付：按二次结果重新执行
            self._revert_and_repay(case, winner2)
        else:
            self._finalize_second_pool(case)
        case["status"] = "settled"
        case["events"].append({"kind": "second_decided", "at": now,
                               "msg": f"二次仲裁：支持{'买家' if winner2 == 'buyer' else '卖家'}（最终结果）"})
        self._event("case", f"案件 {case['id']} 二次仲裁结束",
                    f"最终支持{'买家' if winner2 == 'buyer' else '卖家'}。", case["id"])
        self._notify(case["buyer"], "arb_result", "二次仲裁结果已出",
                     f"案件 {case['id']} 最终结果：支持{'买家' if winner2 == 'buyer' else '卖家'}。", case["id"])
        self._notify(case["seller"], "arb_result", "二次仲裁结果已出",
                     f"案件 {case['id']} 最终结果：支持{'买家' if winner2 == 'buyer' else '卖家'}。", case["id"])

    def _revert_and_repay(self, case, winner2):
        """推翻一次裁决：回滚首次赔付并按二次结果重新执行。

        优先从首次受益方追回（余额不足部分由生态基金兜底）。
        审计 M-9：追回的资金（claw）必须按二次结果重新入账——
        此前被追回后从未入账，导致每次翻案都造成资金凭空消失。"""
        buyer, seller = case["buyer"], case["seller"]
        first_winner = case.get("result", "")
        received = 0.0
        for pay in case["payouts"].values():
            received += float(pay.get("to_buyer_frozen", 0.0))
            received += float(pay.get("to_buyer_deposit", 0.0))
            received += float(pay.get("to_seller_frozen", 0.0))
            received += float(pay.get("to_seller_share", 0.0))
        beneficiary = buyer if first_winner == "buyer" else seller
        claw = _amt(min(received, max(0.0, self.store.balances.get(beneficiary, 0.0))))
        if claw > 0:
            self.store.balances[beneficiary] = _amt(self.store.balances.get(beneficiary, 0.0) - claw)
            shortfall = _amt(received - claw)
            # 生态基金兜底追缴不足部分
            if shortfall > 0:
                eco_cover = _amt(min(shortfall, self._eco_balance()))
                if eco_cover > 0:
                    self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(self._eco_balance() - eco_cover)
                claw = _amt(claw + eco_cover)
            pool_key = f"case_{case['id']}"
            pool = float(self.store.arb_pools.get(pool_key, 0.0))
            frozen = float(case.get("seller_frozen", 0.0))
            # 追回资金（claw）按二次结果重新分配，保证链上账务守恒
            if winner2 == "buyer":
                # 首次卖家胜：追回的（frozen + 卖家分得的投诉保证金）归还买家
                self.store.balances[buyer] = _amt(self.store.balances.get(buyer, 0.0) + frozen)
                refund = _amt(max(0.0, claw - frozen))
                if refund > 0:
                    self.store.balances[buyer] = _amt(self.store.balances.get(buyer, 0.0) + refund)
                # 二次保证金池剩余归买家（原逻辑）
                remain = _amt(pool)
                if remain > 0:
                    self.store.balances[buyer] = _amt(self.store.balances.get(buyer, 0.0) + remain)
                    self.store.arb_pools.pop(pool_key, None)
                case["payouts"]["second_buyer"] = {"to_buyer_frozen": frozen,
                                                   "to_buyer_claw_refund": refund,
                                                   "to_buyer_pool": remain}
            else:
                # 首次买家胜：追回的（frozen + 买家拿回的投诉保证金）按卖家胜诉规则 40/60 分配
                self.store.balances[seller] = _amt(self.store.balances.get(seller, 0.0) + frozen)
                refund = _amt(max(0.0, claw - frozen))
                if refund > 0:
                    seller_share = _amt(refund * ARB_SELLER_WIN_RATIO)
                    eco_share = _amt(refund - seller_share)
                    self.store.balances[seller] = _amt(self.store.balances.get(seller, 0.0) + seller_share)
                    self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(self._eco_balance() + eco_share)
                # 二次保证金池剩余同样按 40/60 分配（原逻辑）
                remain = _amt(pool)
                if remain > 0:
                    seller_share = _amt(remain * ARB_SELLER_WIN_RATIO)
                    eco_share = _amt(remain - seller_share)
                    self.store.balances[seller] = _amt(self.store.balances.get(seller, 0.0) + seller_share)
                    self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(self._eco_balance() + eco_share)
                    self.store.arb_pools.pop(pool_key, None)
                case["payouts"]["second_seller"] = {"to_seller_frozen": frozen,
                                                    "to_seller_claw_share": refund,
                                                    "to_seller_pool_share": remain}
        else:
            # 首次未执行赔付（理论上不会发生），直接按二次结果走标准赔付
            self._payout(case, winner2)

    def _finalize_second_pool(self, case):
        """二次仲裁未推翻：保证金池剩余发放给二次仲裁员报酬后归生态基金。"""
        pool_key = f"case_{case['id']}"
        pool = float(self.store.arb_pools.get(pool_key, 0.0))
        if pool > 0:
            self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(self._eco_balance() + pool)
            self.store.arb_pools.pop(pool_key, None)

    # ======================================================================
    # 4. 检举（受贿/串通）与恶意投诉
    # ======================================================================
    def charge_validate(self, d, tx):
        addr = tx.sender
        target = str(d.get("target", ""))
        kind = str(d.get("kind", ""))
        cid = str(d.get("case_id", "") or "")
        if _amt(tx.amount) != _amt(ARB_CHARGE_DEPOSIT):
            return False
        if kind not in ("bribe", "collude"):
            return False
        if target == addr or target not in self.store.arb_arbitrators:
            return False
        # 审计 H-4：检举人须为有质押/资产的社区成员，防 Sybil 刷独立检举人确认
        power = float(self.store.stakes.get(addr, 0.0)) + float(self.store.balances.get(addr, 0.0))
        if power < ARB_CHARGE_MIN_POWER:
            return False
        if kind == "collude":
            case = self.store.arb_cases.get(cid)
            if not case or target not in self._panel_members(case):
                return False
        ev = str(d.get("evidence", "") or "")
        return len(ev) <= ARB_EVIDENCE_MAX

    def _charge_slash(self, addr, target, kind, cid) -> bool:
        """确认可疑后的检举成立：罚没质押 + 封禁 + 退还检举押金。返回 True 表示已处理。"""
        ar = self.store.arb_arbitrators.get(target)
        if not ar:
            return False
        if kind == "bribe":
            # 受贿/明显偏袒：罚没全部质押，永久取消资格
            stake = float(ar.get("stake", 0.0))
            if stake > 0:
                self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(self._eco_balance() + stake)
                self.store.arb_slashed = _amt(self.store.arb_slashed + stake)
                ar["stake"] = 0.0
            self._rep_delta(target, -ARB_REP_MAX, "收受贿赂/明显偏袒")
            self._ban(target, "收受贿赂/明显偏袒")
            self.store.arb_suspicious.pop(target, None)
            self.store.balances[addr] = _amt(self.store.balances.get(addr, 0.0) + ARB_CHARGE_DEPOSIT)
            self._event("charge", "检举成立：罚没仲裁员质押",
                        f"{target[:12]}... 因受贿/偏袒被罚没全部质押并永久取消资格。", cid)
            return True
        if kind == "collude":
            case = self.store.arb_cases.get(cid)
            sec = case and case.get("second")
            overturn = bool(sec and sec.get("result") and case.get("result")
                            and sec["result"] != case["result"])
            if not overturn:
                return False
            # 与当事人串通：罚没质押，赔偿受害者损失
            stake = float(ar.get("stake", 0.0))
            victim = case["seller"] if ar.get("recent_votes", []) else case["buyer"]
            for a2 in case.get("panel", {}).values():
                if a2 == target and case["panel_meta"].get(a2, {}).get("side"):
                    voted_side = case["panel_meta"][a2]["side"]
                    victim = case["seller"] if voted_side == "buyer" else case["buyer"]
                    break
            if stake > 0:
                half = _amt(stake / 2.0)
                self.store.balances[victim] = _amt(self.store.balances.get(victim, 0.0) + half)
                self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(self._eco_balance() + stake - half)
                self.store.arb_slashed = _amt(self.store.arb_slashed + stake)
                ar["stake"] = 0.0
            self._rep_delta(target, -ARB_REP_MAX, "与当事人串通")
            self._ban(target, "与当事人串通")
            self.store.arb_suspicious.pop(target, None)
            self.store.balances[addr] = _amt(self.store.balances.get(addr, 0.0) + ARB_CHARGE_DEPOSIT)
            self._event("charge", "检举成立：串通罚没",
                        f"{target[:12]}... 因与当事人串通被罚没质押并赔偿受害者。", cid)
            return True
        return False

    def charge_apply(self, tx, d):
        addr = tx.sender
        target = str(d.get("target", ""))
        kind = str(d.get("kind", ""))
        cid = str(d.get("case_id", "") or "")
        evidence = str(d.get("evidence", "") or "")
        ar = self.store.arb_arbitrators[target]
        now = time.time()
        # 审计修复 H-4：仅「已确认」可疑（≥2 名独立检举人）才可触发罚没；
        # 单人单次检举只能将目标置入观察期，杜绝「2 NOVA 免费罚没他人 500 质押」。
        sus = self.store.arb_suspicious.get(target)
        if sus and sus.get("confirmed"):
            if self._charge_slash(addr, target, kind, cid):
                return
        # 证据不足：目标进入 7 天观察期，信誉分 -5；
        # 累计 ≥2 名独立检举人后确认可疑（confirmed=True），且本次检举立即生效（审计 H-4）。
        ar["status"] = "observing"
        ar["observe_until"] = now + ARB_OBSERVE_DAYS * 86400
        self._rep_delta(target, -5.0, "检举观察")
        prev = sus or {}
        chargers = list(prev.get("chargers", []))
        if addr not in chargers:
            chargers.append(addr)
        confirmed = bool(prev.get("confirmed")) or len(chargers) >= ARB_CHARGE_CONFIRM_MIN
        self.store.arb_suspicious[target] = {
            "reason": f"{kind} 检举（证据：{evidence[:40] or '无'}）",
            "marked_at": prev.get("marked_at", now),
            "observe_until": now + ARB_OBSERVE_DAYS * 86400,
            "confirmed": confirmed,
            "chargers": chargers,
        }
        if confirmed:
            # 本次检举恰好达到确认阈值 → 立即视为检举成立
            if self._charge_slash(addr, target, kind, cid):
                return
        self._notify(target, "arb_suspect", "已被标记可疑",
                     "进入 7 天观察期，期间暂停抽取。请配合调查。", cid)
        self._event("charge", "仲裁员进入观察期",
                    f"{target[:12]}... 因检举进入 7 天观察期。", cid)

    # ======================================================================
    # 每日维护：自动统计/抽取/超时/串通检测/恶意投诉/任期与退出
    # ======================================================================
    def maintain(self) -> dict:
        """自动执行链上规则（节点每日维护 + 手动可调）。

        返回统计字典供日志/测试断言。"""
        stats = {"candidates_settled": 0, "cases_drawn": 0, "timeouts": 0,
                 "substitutes": 0, "suspicious": 0, "malicious": 0,
                 "retired": 0, "stakes_released": 0}
        now = time.time()
        # 1. 候选人投票自动统计
        for cand_addr in list(self.store.arb_candidates):
            cand = self.store.arb_candidates.get(cand_addr)
            if cand and cand.get("status") == "voting" and                     now - float(cand.get("applied_at", 0)) > ARB_VOTE_PERIOD_DAYS * 86400:
                self._settle_candidate(cand_addr)
                stats["candidates_settled"] += 1
        # 2. 案件自动抽取（投诉发起后 1 小时内完成）
        for cid, case in list(self.store.arb_cases.items()):
            if case.get("status") == "pending_draw" and                     now - float(case.get("filed_at", 0)) >= ARB_DRAW_WINDOW:
                if self._auto_draw(cid):
                    stats["cases_drawn"] += 1
        # 3. 二次仲裁抽取
        for cid, case in list(self.store.arb_cases.items()):
            if case.get("status") == "second_pending":
                if self._auto_draw_second(cid):
                    stats["cases_drawn"] += 1
        # 4. 投票超时：扣 1 NOVA + 信誉分 -2，重新抽取替代
        for cid, case in list(self.store.arb_cases.items()):
            if case.get("status") in ("voting", "second_voting"):
                second = case.get("status") == "second_voting"
                meta = (case.get("second") or {}).get("panel_meta", {}) if second else case.get("panel_meta", {})
                votes = (case.get("second") or {}).get("votes", {}) if second else case.get("votes", {})
                for addr, m in list(meta.items()):
                    if m.get("replaced") or votes.get(m["number"]) in ("buyer", "seller"):
                        continue
                    if now > float(m.get("deadline", 0)):
                        self._timeout_arbitrator(case, addr, m, second)
                        stats["timeouts"] += 1
                        stats["substitutes"] += 1
                # 总时限兜底：超过 7 天按当前票数/卖家处理
                started = case.get("drawn_at", 0) if not second else (case.get("second") or {}).get("filed_at", 0)
                if started and now - float(started) > ARB_CASE_VOTE_MAX_DAYS * 86400:
                    if second:
                        if not (case.get("second") or {}).get("result"):
                            self._execute_second(case)
                    elif not case.get("result"):
                        self._execute_case(case)
        # 5. 串通检测：同组重复抽取 / 投票一致率
        suspicious = self._detect_collusion()
        stats["suspicious"] = len(suspicious)
        # 6. 恶意投诉检测
        stats["malicious"] = len(self._detect_malicious())
        # 7. 退出/任期到期处理
        for addr, ar in list(self.store.arb_arbitrators.items()):
            status = ar.get("status")
            if status == "leaving" and float(ar.get("exit_ready_at", 0)) <= now:
                ar["status"] = "retired"
                self._pending_release(addr, float(ar.get("stake", 0.0)),
                                      now + ARB_EXIT_COOLDOWN_DAYS * 86400)
                ar["stake"] = 0.0
                self._notify(addr, "arb_exit_ready", "质押进入冷静期",
                             "退出声明期满，质押 7 天冷静期后自动返还。", "")
                stats["retired"] += 1
            elif status in ("active", "renewing") and float(ar.get("term_end", 0)) <= now:
                # 任期结束未连任：自动退休，质押进入冷静期
                ar["status"] = "retired"
                self._pending_release(addr, float(ar.get("stake", 0.0)),
                                      now + ARB_EXIT_COOLDOWN_DAYS * 86400)
                ar["stake"] = 0.0
                self._notify(addr, "arb_term_end", "任期已结束",
                             "未在任期内完成连任投票，仲裁资格自动结束。质押进入冷静期。", "")
                stats["retired"] += 1
            elif status in ("active", "renewing"):
                term_end = float(ar.get("term_end", 0))
                if 0 < term_end - now <= ARB_RENEW_WINDOW_DAYS * 86400 and                         addr not in self.store.arb_candidates and not ar.get("_renew_reminded"):
                    ar["_renew_reminded"] = True
                    self._notify(addr, "arb_term", "任期即将到期",
                                 f"任期将于 {time.strftime('%Y-%m-%d', time.localtime(term_end))} 结束，"
                                 "请在 7 天内申请连任。", "")
            # 观察期结束恢复
            if status == "observing" and float(ar.get("observe_until", 0)) <= now:
                ar["status"] = "active" if float(ar.get("rep", 0)) >= ARB_REP_SUSPEND else "suspended"
                ar["observe_until"] = 0.0
                self.store.arb_suspicious.pop(addr, None)
                self._notify(addr, "arb_observe_end", "观察期结束",
                             "可疑标记已解除，恢复仲裁资格。", "")
        # 8. 冷静期质押到期自动返还
        for addr, p in list(self.store.arb_stake_pending.items()):
            if now >= float(p[1]):
                amount = float(p[0])
                self.store.balances[addr] = _amt(self.store.balances.get(addr, 0.0) + amount)
                del self.store.arb_stake_pending[addr]
                self._notify(addr, "arb_claim", "质押已返还",
                             f"{_amt(amount):.0f} NOVA 已退回账户。", "")
                stats["stakes_released"] += 1
        return stats

    def _timeout_arbitrator(self, case, addr, m, second=False):
        """超时未投票：-1 NOVA / 信誉分 -2，并重新抽取替代仲裁员。"""
        self._slash_stake(addr, ARB_TIMEOUT_PENALTY)
        self._rep_delta(addr, -ARB_TIMEOUT_REP, "超时未投票")
        ar = self.store.arb_arbitrators.get(addr)
        if ar:
            ar["history"].append({"kind": "timeout", "case_id": case["id"], "at": time.time()})
        self._notify(addr, "arb_timeout", "投票超时",
                     f"案件 {case['id']} 未在 72 小时内投票：扣 {ARB_TIMEOUT_PENALTY:.0f} NOVA，"
                     f"信誉分 -{ARB_TIMEOUT_REP:.0f}。", case["id"])
        m["replaced"] = True
        m["side"] = ""
        num = m["number"]
        if second:
            sec = case["second"]
            panel = sec["panel"]
            meta = sec["panel_meta"]
        else:
            panel = case["panel"]
            meta = case["panel_meta"]
        panel.pop(num, None)
        meta.pop(addr, None)
        pool = self._eligible_pool(case, second=second, extra_exclude={addr})
        if pool:
            repl = sorted(pool, key=lambda a: self._vrf(self.store.arb_vrf_seed, case["id"], "repl", a))[0]
            now = time.time()
            panel[num] = repl
            meta[repl] = {"number": num, "assigned_at": now,
                                    "deadline": now + ARB_VOTE_WINDOW, "voted": False,
                                    "side": "", "replaced": False, "conflict": False}
            self._notify(repl, "arb_drawn", "您被抽中担任替代仲裁员",
                         f"案件 {case['id']} 需要您在 72 小时内投票（匿名编号 #{num}）。", case["id"])
            ar2 = self.store.arb_arbitrators.get(repl)
            if ar2:
                ar2["panel_history"].append(now)
                ar2["history"].append({"kind": "drawn", "case_id": case["id"], "at": now})
        case["events"].append({"kind": "timeout", "at": time.time(),
                               "msg": f"#{num} 超时未投票，已重新抽取替代"})

    def _detect_collusion(self) -> list:
        """串通检测：同组 30 天被抽超 3 次 / 投票一致率 > 90% -> 标记可疑。"""
        now = time.time()
        cutoff = now - ARB_SUSPECT_WINDOW_DAYS * 86400
        marked = []
        # 同一组仲裁员重复抽取
        combo_count = {}
        for c in self.store.arb_cases.values():
            if float(c.get("drawn_at", 0)) < cutoff:
                continue
            members = self._panel_members(c)
            if len(members) >= 2:
                key = tuple(sorted(members))
                combo_count[key] = combo_count.get(key, 0) + 1
        for combo, cnt in combo_count.items():
            if cnt > ARB_SUSPECT_PANEL_REPEAT:
                for addr in combo:
                    if self._mark_suspicious(addr, f"同组仲裁员 30 天内被抽 {cnt} 次"):
                        marked.append(addr)
        # 投票一致率 > 90%（共同裁决 >= 10 案）
        pair_agree = {}
        pair_total = {}
        for c in self.store.arb_cases.values():
            if c.get("status") != "settled" or not c.get("revealed"):
                continue
            meta = c.get("panel_meta", {})
            for a1, m1 in meta.items():
                for a2, m2 in meta.items():
                    if a1 >= a2 or not m1.get("voted") or not m2.get("voted"):
                        continue
                    key = tuple(sorted([a1, a2]))
                    pair_total[key] = pair_total.get(key, 0) + 1
                    if m1.get("side") == m2.get("side"):
                        pair_agree[key] = pair_agree.get(key, 0) + 1
        for key, total in pair_total.items():
            if total >= 10 and pair_agree.get(key, 0) / total > ARB_SUSPECT_VOTE_AGREE:
                for addr in key:
                    if self._mark_suspicious(addr, f"与同庭仲裁员投票一致率过高（{pair_agree.get(key, 0)}/{total}）"):
                        marked.append(addr)
        return marked

    def _mark_suspicious(self, addr, reason) -> bool:
        ar = self.store.arb_arbitrators.get(addr)
        if not ar or addr in self.store.arb_suspicious:
            return False
        if ar.get("status") not in ("active", "renewing"):
            return False
        ar["status"] = "observing"
        ar["observe_until"] = time.time() + ARB_OBSERVE_DAYS * 86400
        # 审计 H-4：自动统计标记默认未确认（confirmed=False），
        # 必须由 ≥2 名独立检举人确认后才可被检举罚没，避免统计误报被滥用。
        self.store.arb_suspicious[addr] = {
            "reason": reason, "marked_at": time.time(),
            "observe_until": ar["observe_until"],
            "confirmed": False, "chargers": [],
        }
        self._notify(addr, "arb_suspect", "已被标记可疑",
                     f"{reason}。暂停抽取，进入 7 天观察期。", "")
        return True

    def _detect_malicious(self) -> list:
        """恶意投诉检测：30 天内败诉超 3 次 -> 名单（保证金 50）；连续 5 次 -> 锁密文 30 天。"""
        now = time.time()
        cutoff = now - ARB_MALICIOUS_WINDOW_DAYS * 86400
        lost = {}
        for c in self.store.arb_cases.values():
            if c.get("status") != "settled":
                continue
            if float(c.get("decided_at", 0)) < cutoff:
                continue
            buyer = c.get("buyer", "")
            final = (c.get("second") or {}).get("result") or c.get("result")
            if final == "seller":
                lost[buyer] = lost.get(buyer, 0) + 1
        marked = []
        for buyer, cnt in lost.items():
            m = self.store.arb_malicious.get(buyer)
            if not m:
                m = {"loss_count": 0, "consecutive": 0, "lock_until": 0.0, "marked_at": 0.0}
                self.store.arb_malicious[buyer] = m
            m["loss_count"] = cnt
            if cnt >= ARB_MALICIOUS_LOSS_LIMIT:
                m["marked_at"] = now
                if not m.get("_notified"):
                    m["_notified"] = True
                    self._notify(buyer, "arb_malicious", "被列入恶意投诉名单",
                                 f"30 天内 {cnt} 次投诉全部败诉，投诉保证金提高至 "
                                 f"{ARB_MALICIOUS_DEPOSIT:.0f} NOVA。", "")
                m["consecutive"] = cnt  # 窗口内连续败诉次数（随维护重算，不重复累加）
                if m["consecutive"] >= ARB_MALICIOUS_LOCK_COUNT:
                    m["lock_until"] = now + ARB_MALICIOUS_LOCK_DAYS * 86400
                    self._notify(buyer, "arb_malicious_lock", "密文交易权限受限",
                                 f"连续 {m['consecutive']} 次恶意投诉，密文交易权限被限制 "
                                 f"{ARB_MALICIOUS_LOCK_DAYS} 天。", "")
                marked.append(buyer)
        return marked

    def cipher_locked(self, addr, data) -> bool:
        """恶意投诉锁定期间限制密文交易（nova:text 密文发布/购买）。"""
        m = self.store.arb_malicious.get(addr)
        if not m or float(m.get("lock_until", 0)) <= time.time():
            return False
        if not isinstance(data, dict):
            return False
        op = data.get("op")
        if op == "nova:text:create" and data.get("visibility") == "sealed":
            return True
        if op == "nova:text:buy":
            tid = data.get("text_id", "")
            a = self.store.text_assets.get(tid)
            if a and a.get("visibility") == "sealed":
                return True
        return False


    # ======================================================================
    # 查询接口（RPC 用）
    # ======================================================================
    def summary(self) -> dict:
        return {
            "arbitrators": len(self.store.arb_arbitrators),
            "candidates": len(self.store.arb_candidates),
            "cases": len(self.store.arb_cases),
            "open_cases": sum(1 for c in self.store.arb_cases.values()
                              if c.get("status") in ("pending_draw", "voting",
                                                     "second_pending", "second_voting")),
            "settled_cases": sum(1 for c in self.store.arb_cases.values()
                                 if c.get("status") == "settled"),
            "banned": len(self.store.arb_banned),
            "suspicious": len(self.store.arb_suspicious),
            "malicious": len(self.store.arb_malicious),
            "slashed": self.store.arb_slashed,
            "eco_fund": self._eco_balance(),
            "vrf_seed": self.store.arb_vrf_seed,
        }

    def list_arbitrators(self) -> list:
        out = []
        for addr, ar in self.store.arb_arbitrators.items():
            out.append({
                "addr": addr, "rep": ar.get("rep", 0), "stake": ar.get("stake", 0),
                "cases": ar.get("cases", 0), "correct": ar.get("correct", 0),
                "revenue": ar.get("revenue", 0), "status": ar.get("status", ""),
                "term_start": ar.get("term_start", 0), "term_end": ar.get("term_end", 0),
                "streak": ar.get("streak", 0), "ban_reason": ar.get("ban_reason", ""),
            })
        out.sort(key=lambda x: -float(x["rep"]))
        return out

    def list_candidates(self) -> list:
        out = []
        for addr, c in self.store.arb_candidates.items():
            out.append({
                "addr": addr, "applied_at": c.get("applied_at", 0),
                "kind": c.get("kind", "first"), "votes": c.get("votes", {}),
                "status": c.get("status", ""), "settled_at": c.get("settled_at", 0),
            })
        out.sort(key=lambda x: -float(x["applied_at"]))
        return out

    def case_public(self, case_id, viewer="") -> dict:
        """案件公示：在途案件对当事人匿名（仅编号）；已裁决案件公开。"""
        case = self.store.arb_cases.get(case_id)
        if not case:
            return {}
        revealed = bool(case.get("revealed")) or case.get("status") == "settled"
        is_party = viewer in (case.get("buyer"), case.get("seller"))
        out = {
            "id": case["id"], "stage": case.get("stage", 1),
            "status": case.get("status"), "trade_id": case.get("trade_id"),
            "reason": case.get("reason"), "evidence": case.get("evidence"),
            "deposit": case.get("deposit"), "seller_frozen": case.get("seller_frozen"),
            "filed_at": case.get("filed_at"), "decided_at": case.get("decided_at"),
            "result": case.get("result"), "appeal_deadline": case.get("appeal_deadline"),
            "payouts": case.get("payouts", {}),
            "my_number": "",
        }
        if is_party:
            out["buyer"] = case.get("buyer")
            out["seller"] = case.get("seller")
        else:
            out["buyer"] = case.get("buyer", "")[:12] + "..."
            out["seller"] = case.get("seller", "")[:12] + "..."
        # 仲裁员编号（本人可见自己的匿名编号）
        meta = case.get("panel_meta", {})
        m = meta.get(viewer)
        if m:
            out["my_number"] = m.get("number", "")
        # 投票展示：已公开 -> 具体投票人；未公开 -> 仅编号
        if revealed:
            out["panel"] = [{"number": num, "addr": case.get("panel", {}).get(num, ""),
                             "side": case.get("votes", {}).get(num, ""),
                             "deadline": meta.get(case.get("panel", {}).get(num, ""), {})
                             .get("deadline", 0)}
                            for num in sorted(case.get("panel", {}))]
            sec = case.get("second")
            if sec:
                out["second_panel"] = [{"number": num, "addr": sec.get("panel", {}).get(num, ""),
                                        "side": sec.get("votes", {}).get(num, ""),
                                        "deadline": sec.get("panel_meta", {})
                                        .get(sec.get("panel", {}).get(num, ""), {}).get("deadline", 0)}
                                       for num in sorted(sec.get("panel", {}))]
                out["second_result"] = sec.get("result", "")
                out["second_decided_at"] = sec.get("decided_at", 0)
                out["second_appellant"] = sec.get("appellant", "")
        else:
            out["panel"] = [{"number": num, "side": case.get("votes", {}).get(num, ""),
                             "deadline": meta.get(case.get("panel", {}).get(num, ""), {})
                             .get("deadline", 0)}
                            for num in sorted(case.get("panel", {}))]
        out["events"] = case.get("events", [])[-20:]
        return out

    def list_cases(self, viewer="") -> list:
        out = []
        for cid in self.store.arb_cases:
            pub = self.case_public(cid, viewer)
            if pub:
                out.append({k: pub.get(k) for k in (
                    "id", "status", "stage", "trade_id", "result", "filed_at",
                    "decided_at", "deposit", "buyer", "seller")})
        out.sort(key=lambda x: -float(x.get("filed_at", 0)))
        return out

    def user_panel(self, addr) -> dict:
        my_cases = [self.case_public(cid, addr) for cid, c in self.store.arb_cases.items()
                    if c.get("buyer") == addr]
        my_cases.sort(key=lambda x: -float(x.get("filed_at", 0)))
        m = self.store.arb_malicious.get(addr, {})
        return {
            "addr": addr,
            "deposit": self._deposit_for(addr),
            "malicious": m,
            "complaints": my_cases,
            "is_arbitrator": addr in self.store.arb_arbitrators,
            "is_candidate": addr in self.store.arb_candidates,
            "banned": self._is_banned(addr),
        }

    def arbitrator_panel(self, addr) -> dict:
        ar = self.store.arb_arbitrators.get(addr)
        if not ar:
            return {"found": False, "addr": addr}
        pending = []
        for cid, c in self.store.arb_cases.items():
            if c.get("status") in ("voting", "second_voting"):
                meta = c.get("panel_meta", {})
                sec_meta = (c.get("second") or {}).get("panel_meta", {})
                m = meta.get(addr) or sec_meta.get(addr)
                if m and not m.get("replaced"):
                    stage = 2 if addr in sec_meta else 1
                    pending.append({
                        "case_id": cid, "number": m.get("number", ""), "stage": stage,
                        "trade_id": c.get("trade_id"), "reason": c.get("reason"),
                        "evidence": c.get("evidence"), "deadline": m.get("deadline", 0),
                        "side": m.get("side", ""), "voted": m.get("voted", False),
                        "filed_at": c.get("filed_at"),
                    })
        pending.sort(key=lambda x: -float(x.get("filed_at", 0)))
        term_end = float(ar.get("term_end", 0))
        return {
            "found": True, "addr": addr,
            "status": ar.get("status", ""), "rep": ar.get("rep", 0),
            "stake": ar.get("stake", 0), "cases": ar.get("cases", 0),
            "correct": ar.get("correct", 0), "revenue": ar.get("revenue", 0),
            "streak": ar.get("streak", 0), "term_start": ar.get("term_start", 0),
            "term_end": term_end,
            "term_remaining_days": max(0.0, (term_end - time.time()) / 86400),
            "accuracy": round(ar.get("correct", 0) / ar.get("cases", 0) * 100, 1)
            if ar.get("cases", 0) else 0.0,
            "observe_until": ar.get("observe_until", 0),
            "declared_conflicts": ar.get("declared_conflicts", 0),
            "history": ar.get("history", [])[-20:],
            "pending": pending,
            "banned": self._is_banned(addr),
        }

    def notifications(self, addr) -> list:
        box = self.store.arb_notifications.get(addr, [])
        return sorted(box, key=lambda n: -float(n.get("at", 0)))

    def mark_read(self, addr, ids=None) -> int:
        box = self.store.arb_notifications.get(addr, [])
        idset = set(ids or [])
        n = 0
        for item in box:
            if (not idset and not item.get("read")) or item.get("id") in idset:
                item["read"] = True
                n += 1
        return n
