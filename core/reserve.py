# -*- coding: utf-8 -*-
"""储备金与经济安全网引擎（v0.10）。

集中承载五大保障机制，全部为链上确定性逻辑、自动执行、无人工干预：
1. 储备金自动回购与价格托底（跌破 7 日均线 30%/50%/70% -> 划拨 1%/2%/5% 回购销毁）
2. 质押保护期（暴跌 >=50% -> 30 天解质押冻结）+ 逆风补偿池（每节点每天 1 NOVA）
3. 最低节点保障 / 紧急招募 / 种子节点运维基金 / 网络重建模式
4. 事故赔付基金（储备 2%）/ 紧急冻结（合约暂停 48h）/ 链上公告
5. 储备金自动补血 / 减支触发 / 重新起航纪念 NFT

状态全部保存在 store 上，随快照持久化同步；RPC 只读查询。
"""
import json
import time

MA7_DAYS = 7


def _day_key(ts=None):
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))
    except Exception:
        return str(int(ts if ts is not None else time.time()) // 86400)


class ReserveEngine:
    def __init__(self, store, economy, oracle=None):
        self.store = store
        self.economy = economy
        self.oracle = oracle

    # ------------------------------------------------------------------
    # 资金池初始值（幂等，仅空账户注入）
    # ------------------------------------------------------------------
    def seed_funds(self):
        b = self.store.balances
        if b.get(self.economy.RESERVE, 0.0) <= 0:
            b[self.economy.RESERVE] = self.economy.RESERVE_INITIAL
        if b.get(self.economy.ECOSYSTEM_FUND, 0.0) <= 0:
            b[self.economy.ECOSYSTEM_FUND] = self.economy.ECOSYSTEM_FUND_INITIAL
        if b.get(self.economy.VALIDATOR_POOL, 0.0) <= 0:
            b[self.economy.VALIDATOR_POOL] = self.economy.VALIDATOR_POOL_INITIAL

    # ------------------------------------------------------------------
    # 事件记录（公开可查）
    # ------------------------------------------------------------------
    def _event(self, op, msg, extra=None):
        self.store.reserve_event_seq += 1
        ev = {"seq": self.store.reserve_event_seq, "op": op,
              "ts": time.time(), "msg": msg}
        if extra:
            ev.update(extra)
        self.store.reserve_events[ev["seq"]] = ev
        return ev

    # ------------------------------------------------------------------
    # ① 价格历史 / 7 日均线 / 回购托底
    # ------------------------------------------------------------------
    def current_price(self):
        if self.oracle is None:
            return None
        try:
            return self.oracle.price("NOVA/USDT")
        except Exception:
            return None

    def record_daily_price(self):
        """按 UTC 自然日采样一次 NOVA/USDT 价，保留 7 天（供均线）。"""
        p = self.current_price()
        if not p:
            return
        day = _day_key()
        hist = self.store.price_history.setdefault("NOVA/USDT", [])
        # 同一天只保留最新采样
        hist = [h for h in hist if h[0] != day]
        hist.append([day, float(p)])
        cutoff = time.time() - (MA7_DAYS + 1) * 86400
        self.store.price_history["NOVA/USDT"] = \
            [h for h in hist if self._day_ts(h[0]) >= cutoff]

    @staticmethod
    def _day_ts(day):
        try:
            return time.mktime(time.strptime(day, "%Y-%m-%d"))
        except Exception:
            return 0.0

    def ma7(self):
        hist = self.store.price_history.get("NOVA/USDT", [])
        if len(hist) < 2:
            return None
        return sum(h[1] for h in hist) / len(hist)

    def buyback_check(self):
        """每日维护：跌破 7 日均线触发回购（受单日/单周上限与治理暂停约束）。"""
        if self.store.gov_params.get("reserve.buyback_paused"):
            return None
        p = self.current_price()
        ma = self.ma7()
        if not p or not ma:
            return None
        dev = (p - ma) / ma           # 偏离度（负 = 跌破）
        ratio = 0.0
        reason = ""
        if dev <= -self.economy.BUYBACK_MA7_70:
            ratio, reason = 0.05, "跌破 7 日均线 70%（紧急回购）"
        elif dev <= -self.economy.BUYBACK_MA7_50:
            ratio, reason = 0.02, "跌破 7 日均线 50%（回购翻倍）"
        elif dev <= -self.economy.BUYBACK_MA7_30:
            ratio, reason = 0.01, "跌破 7 日均线 30%"
        if ratio <= 0:
            return None
        reserve = self.store.balances.get(self.economy.RESERVE, 0.0)
        if reserve <= 0:
            return None
        today = _day_key()
        week = _day_key(time.time() - 6 * 86400)
        # 单日 / 单周已回购
        daily = sum(float(e.get("amount", 0.0)) for e in self.store.buyback_log
                    if e.get("day") == today)
        weekly = sum(float(e.get("amount", 0.0)) for e in self.store.buyback_log
                     if e.get("day") >= week)
        daily_cap = reserve * self.economy.BUYBACK_DAILY_CAP_RATIO
        weekly_cap = reserve * self.economy.BUYBACK_WEEKLY_CAP_RATIO
        amt = min(reserve * ratio, daily_cap - daily, weekly_cap - weekly)
        if amt <= 0:
            return None
        self.store.balances[self.economy.RESERVE] = reserve - amt
        self.store.balances[self.economy.BUYBACK_DEAD] = \
            self.store.balances.get(self.economy.BUYBACK_DEAD, 0.0) + amt
        rec = {"day": today, "price": round(p, 8), "ma7": round(ma, 8),
               "deviation": round(dev, 6), "ratio": ratio, "reason": reason,
               "amount": round(amt, 4), "ts": time.time()}
        self.store.buyback_log.append(rec)
        self._event("reserve:buyback", reason, rec)
        return rec

    # ------------------------------------------------------------------
    # ② 质押冻结（暴跌 >=50% -> 30 天）+ 逆风补偿池
    # ------------------------------------------------------------------
    def _deviation(self):
        p = self.current_price()
        ma = self.ma7()
        if not p or not ma:
            return None
        return (p - ma) / ma

    def _update_stake_freeze(self):
        dev = self._deviation()
        if dev is None:
            return
        now = time.time()
        until = float(self.store.stake_freeze_until or 0.0)
        if until > now:
            # 冻结中：价格回升至均线 80%（偏离 > -20%）自动解除
            if dev > -0.20:
                self.store.stake_freeze_until = 0.0
                self._event("reserve:stake_freeze", "价格回升，质押冻结解除")
        elif dev <= -0.50:
            # 未冻结且暴跌 >=50%：冻结 30 天
            self.store.stake_freeze_until = now + self.economy.STAKE_FREEZE_DAYS * 86400
            self._event("reserve:stake_freeze",
                        f"价格暴跌 {abs(dev) * 100:.0f}%，启动 {self.economy.STAKE_FREEZE_DAYS} 天解质押冻结")
            self._seed_headwind_pool()

    def stake_frozen(self, ts) -> bool:
        until = float(self.store.stake_freeze_until or 0.0)
        return until > ts

    def _seed_headwind_pool(self):
        if self.store.headwind_pool > 0:
            return
        eco = self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0)
        amt = eco * self.economy.HEADWIND_COMPENSATION_POOL_RATIO
        if amt > 0:
            self.store.balances[self.economy.ECOSYSTEM_FUND] = eco - amt
            self.store.headwind_pool = amt
            self._event("reserve:headwind_pool", f"生态基金划拨 {round(amt, 4)} NOVA 建立逆风补偿池")

    def headwind_compensate(self):
        """逆风补偿：冻结期内坚持运行且未解质押的验证者，每天 +1 NOVA（不跑的人拿不到）。"""
        pool = float(self.store.headwind_pool)
        if pool <= 0:
            return 0
        today = _day_key()
        paid = 0.0
        for addr in list(self.store.stakes.keys()):
            if float(self.store.stakes.get(addr, 0.0)) < self.economy.MIN_STAKE:
                continue
            if self.store.headwind_comp_paid.get(addr) == today:
                continue
            amt = min(self.economy.HEADWIND_COMPENSATION_DAY, pool - paid)
            if amt <= 0:
                break
            self.store.balances[addr] = self.store.balances.get(addr, 0.0) + amt
            self.store.headwind_pool = round(self.store.headwind_pool - amt, 8)
            self.store.headwind_comp_paid[addr] = today
            paid += amt
        if paid > 0:
            self._event("reserve:headwind_comp", f"逆风补偿发放 {round(paid, 4)} NOVA")
        return paid

    # ------------------------------------------------------------------
    # ③ 节点保障：活跃监控 / 种子基金 / 网络重建
    # ------------------------------------------------------------------
    def node_guard(self):
        """每 5 分钟：记录活跃节点数（链上公开）；重建模式下暂停新交易确认。"""
        n = self.economy.active_nodes()
        self.store.node_count_history.append([time.time(), n])
        # 保留最近 90 天记录
        cutoff = time.time() - 90 * 86400
        self.store.node_count_history = \
            [h for h in self.store.node_count_history if h[0] >= cutoff]
        if n < self.economy.CRITICAL_ACTIVE_NODES:
            # 网络重建：暂停新交易确认 1 小时（由 nova_node.validate 拦截）
            if self.store.rebuild_until < time.time():
                self.store.rebuild_until = time.time() + 3600
                self._event("reserve:rebuild",
                            f"活跃节点 {n} < {self.economy.CRITICAL_ACTIVE_NODES}，进入网络重建模式")
        else:
            self.store.rebuild_until = 0.0
        return n

    def rebuild_paused(self, now=None) -> bool:
        until = float(self.store.rebuild_until or 0.0)
        return until > (now if now is not None else time.time())

    def seed_nodes(self):
        """种子节点：全网最早注册的 SEED_NODE_QUOTA 位矿工。"""
        order = sorted(self.store.miner_registry.items(), key=lambda kv: kv[1])[:self.economy.SEED_NODE_QUOTA]
        return [a for a, _ in order]

    def seed_fund_init(self):
        if self.store.seed_fund > 0:
            return
        reserve = self.store.balances.get(self.economy.RESERVE, 0.0)
        amt = reserve * self.economy.SEED_FUND_RATIO
        if amt > 0:
            self.store.balances[self.economy.RESERVE] = reserve - amt
            self.store.seed_fund = amt
            self._event("reserve:seed_fund", f"储备金划拨 {round(amt, 4)} NOVA 建立种子节点运维基金")

    def seed_subsidy(self):
        """逆风期种子节点每月 100 NOVA 运维补贴（自动发放，无需审批）。"""
        if not self.economy.in_headwind() or float(self.store.seed_fund) <= 0:
            return 0
        month = _day_key()[:7]
        paid = 0.0
        for addr in self.seed_nodes():
            if self.store.seed_subsidy_paid.get(addr) == month:
                continue
            amt = min(self.economy.SEED_SUBSIDY_MONTHLY, float(self.store.seed_fund) - paid)
            if amt <= 0:
                break
            self.store.balances[addr] = self.store.balances.get(addr, 0.0) + amt
            self.store.seed_fund = round(self.store.seed_fund - amt, 8)
            self.store.seed_subsidy_paid[addr] = month
            paid += amt
        if paid > 0:
            self._event("reserve:seed_subsidy", f"逆风期种子节点补贴发放 {round(paid, 4)} NOVA")
        return paid

    def rebuild_cost(self):
        """网络重建：储备金支付现存活跃节点重启成本（每节点 10 NOVA）。"""
        if not self.economy.critical_node_recovery():
            return 0
        reserve = self.store.balances.get(self.economy.RESERVE, 0.0)
        n = self.economy.active_nodes()
        amt = min(n * 10.0, reserve * 0.01)
        if amt <= 0:
            return 0
        self.store.balances[self.economy.RESERVE] = reserve - amt
        self._event("reserve:rebuild_cost", f"网络重建：储备金支付重启成本 {round(amt, 4)} NOVA")
        return amt

    # ------------------------------------------------------------------
    # ④ 事故赔付 / 紧急冻结 / 链上公告（治理联动）
    # ------------------------------------------------------------------
    def payout_fund_init(self):
        if self.store.payout_fund > 0:
            return
        reserve = self.store.balances.get(self.economy.RESERVE, 0.0)
        amt = reserve * self.economy.PAYOUT_FUND_RATIO
        if amt > 0:
            self.store.balances[self.economy.RESERVE] = reserve - amt
            self.store.payout_fund = amt
            self._event("reserve:payout_fund", f"储备金划拨 {round(amt, 4)} NOVA 建立事故赔付基金")

    def execute_payout(self, payout_id, victim, loss, reason):
        """赔付：实际损失 x PAYOUT_RATIO（默认 80%），入受害者待领取，签确认书后到账。"""
        amt = round(float(loss) * self.economy.PAYOUT_RATIO, 8)
        fund = float(self.store.payout_fund)
        if fund < amt:
            amt = fund
        if amt <= 0:
            return None
        self.store.payout_fund = round(fund - amt, 8)
        self.store.payouts[payout_id] = {
            "payout_id": payout_id, "victim": victim, "loss": float(loss),
            "amount": amt, "reason": reason, "status": "pending",
            "created_at": time.time(),
        }
        self._event("reserve:payout", f"赔付 {victim[:12]}... {amt} NOVA（损失 {loss}，比例 {self.economy.PAYOUT_RATIO}）")
        return self.store.payouts[payout_id]

    def confirm_payout(self, payout_id, addr, ts):
        """用户签署链上确认书：赔付到账。"""
        po = self.store.payouts.get(payout_id)
        if not po or po.get("status") != "pending" or po.get("victim") != addr:
            return False
        po["status"] = "confirmed"
        po["confirmed_at"] = ts
        self.store.balances[addr] = self.store.balances.get(addr, 0.0) + float(po["amount"])
        self._event("reserve:payout:confirm", f"{addr[:12]}... 签署赔付确认书，到账 {po['amount']} NOVA")
        return True

    def freeze_target(self, target, hours=None):
        """紧急冻结：治理通过后冻结目标合约/模块 48 小时。"""
        h = hours or self.economy.FREEZE_HOURS
        self.store.freeze_targets[target] = time.time() + h * 3600
        self._event("reserve:freeze", f"紧急冻结 {target}（{h} 小时）")

    def is_frozen(self, target, now=None) -> bool:
        until = float(self.store.freeze_targets.get(target, 0.0))
        return until > (now if now is not None else time.time())

    def notice_post(self, title, reason, impact, actions, timeline, author):
        self.store.notice_seq += 1
        nid = f"notice-{self.store.notice_seq:06d}"
        self.store.notices[nid] = {
            "id": nid, "title": title, "reason": reason, "impact": impact,
            "actions": actions, "timeline": timeline, "author": author,
            "ts": time.time(),
        }
        self._event("reserve:notice", f"链上公告：{title}")
        return self.store.notices[nid]

    # ------------------------------------------------------------------
    # ⑤ 自动补血 / 减支 / 重新起航
    # ------------------------------------------------------------------
    def refill_check(self):
        """每日：生态基金/验证者池低于安全线时从储备金补血（储备金低于安全线则暂停补血）。"""
        b = self.store.balances
        eco = b.get(self.economy.ECOSYSTEM_FUND, 0.0)
        val = b.get(self.economy.VALIDATOR_POOL, 0.0)
        reserve = b.get(self.economy.RESERVE, 0.0)
        log = []
        if reserve < self.economy.RESERVE_SAFE_LINE:
            self._event("reserve:refill", "储备金低于安全线，补血暂停（仅维持减支）")
            return log
        if eco < self.economy.ECO_SAFE_LINE:
            amt = reserve * self.economy.REFILL_ECO_RATIO
            b[self.economy.RESERVE] = b.get(self.economy.RESERVE, 0.0) - amt
            b[self.economy.ECOSYSTEM_FUND] = eco + amt
            log.append(("eco", round(amt, 4)))
            self._event("reserve:refill", f"生态基金低于安全线，储备金补血 {round(amt, 4)} NOVA（20%）")
            reserve = b.get(self.economy.RESERVE, 0.0)
        if reserve < self.economy.RESERVE_SAFE_LINE:
            return log
        if val < self.economy.VALIDATOR_SAFE_LINE:
            amt = reserve * self.economy.REFILL_VALIDATOR_RATIO
            b[self.economy.RESERVE] = b.get(self.economy.RESERVE, 0.0) - amt
            b[self.economy.VALIDATOR_POOL] = val + amt
            log.append(("validator", round(amt, 4)))
            self._event("reserve:refill", f"验证者池低于安全线，储备金补血 {round(amt, 4)} NOVA（30%）")
        return log

    def sail_mint(self, addr, amount):
        """重新起航纪念 NFT 销售：限量 1000，收入 100% 进生态基金。"""
        if not self.economy.sail_active():
            return None
        if self.store.sail_sold >= self.economy.SAIL_NFT_TOTAL:
            return None
        if float(amount) != self.economy.SAIL_NFT_PRICE:
            return None
        if self.store.balances.get(addr, 0.0) < float(amount) + self.economy.FIXED_GAS:
            return None
        self.store.balances[addr] = self.store.balances.get(addr, 0.0) - float(amount)
        self.store.balances[self.economy.ECOSYSTEM_FUND] = \
            self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0) + float(amount)
        self.store.sail_sold += 1
        sid = f"sail-{self.store.sail_sold:04d}"
        self.store.sail_nfts[sid] = {"id": sid, "holder": addr, "ts": time.time()}
        self._event("reserve:sail", f"重新起航纪念 NFT {sid} 售出，收入 {float(amount)} NOVA 进入生态基金")
        return self.store.sail_nfts[sid]

    # ------------------------------------------------------------------
    # 每日维护入口（nova_node._run_daily_maintenance 调用）
    # ------------------------------------------------------------------
    def maintain(self):
        self.seed_funds()
        self.seed_fund_init()
        self.payout_fund_init()
        self.record_daily_price()
        self.buyback_check()
        self._update_stake_freeze()
        self.headwind_compensate()
        self.seed_subsidy()
        self.refill_check()
