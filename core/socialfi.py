# -*- coding: utf-8 -*-
"""SocialFi 层：粉丝经济 x 链上互动 x 金融化（10 类玩法）。

与 Nova 链既有模块打通：
- 内容类玩法（粉丝代币头像、策展封面、社交动态）可携带 CID，链自动将其
  固定到存储网络（storage_network），真正占用链的存储能力；
- 推荐引擎在链上确定性计算，并输出任务规格，可一键发布为算力市场的
  计算任务（compute market），占用链的算力能力；
- 声誉系统实时计算 0-100 信誉分，高信誉（>=80）享受 50% 交易费折扣。

实现方式与存储/算力模块一致：全部为 signed tx（sender == receiver，
data 为 JSON {op, ...}），经区块/DAG 广播后在所有节点确定性重放。
"""
import hashlib
import json
import math
import re
import time

from core.crypto import (TEXT_CRYPTO_OK, text_ecies_decrypt, text_ecies_wrap_to,
                          text_gen_p256_keypair, text_p256_pub_from_priv)

CID_RE = re.compile(r"^(?:0x[0-9a-fA-F]{64}|bafy[a-z2-7]{46,58})$")
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

NAME_MAX = 64
DESC_MAX = 512
CONTENT_MAX = 2000
ITEMS_MAX = 50
OPTIONS_MAX = 8
TIERS_MAX = 8
BLIND_MAX_DRAWS = 100

FAN_MIN_PRICE = 0.01
FAN_MAX_SUPPLY = 100_000_000
CURATE_CURATOR_SHARE = 0.9          # 策展分润：90% 归策展人
MARKET_FEE_RATIO = 0.02             # 预测市场平台费：2% 入生态基金
BOND_MIN_PRINCIPAL = 10.0
BOND_MAX_RATE = 0.5
BOND_MIN_TERM_DAYS = 30
BOND_MAX_TERM_DAYS = 3650
FRAC_MIN_SUPPLY = 2
FRAC_MAX_SUPPLY = 100_000
FRAC_MIN_PRICE = 0.001
# 文本创作合约（公开/加密发布）参数
TEXT_TITLE_MAX = 64
TEXT_IDENT_MAX = 32
TEXT_BODY_MAX = 20000
TEXT_CIPHER_MAX = 8000
TEXT_MIN_PRICE = 0.01
TEXT_DEPOSIT_BASIC = 10.0          # 基础档保证金（新创作者单篇）
TEXT_DEPOSIT_ADVANCED = 100.0      # 进阶档保证金（已验证创作者系列）
TEXT_DEPOSIT_PRO = 1000.0          # 专业档保证金（机构/头部创作者）
TEXT_AUTHOR_SHARE = 0.9            # 版税自动分账：90% 归作者
TEXT_RELEASE_DAYS = 7              # 下架后无投诉，7 天自动退回保证金
TEXT_DISPUTE_QUORUM = 3            # 首次仲裁庭人数
TEXT_DISPUTE_MAX = 7               # 二次仲裁最大人数
TEXT_DISPUTE_TIMEOUT_DAYS = 14     # 仲裁超时自动按卖家处理
AI_NAME_MAX = 64                 # AI 创作者名称长度上限
AI_META_MAX = 512                # AI 元数据（模型指纹等）长度上限
AI_MIN_BUDGET = 0.1              # 日预算下限（NOVA/日）
AI_MAX_BUDGET = 10000.0          # 日预算上限（NOVA/日）
TEXT_ESCROW = "0x_text_escrow"     # 文本交易合约保证金池
REPUTATION_TIERS = ((90, "星核", "S"), (70, "星环", "A"), (40, "星芒", "B"), (0, "星尘", "C"))
PIN_SIZE_GB = 0.001                 # 内容自动固定时的默认最小体积（0.001 GB）
PIN_DURATION_DAYS = 30


def _amt(v):
    return round(float(v), 8)


def _h(raw: str) -> str:
    return hashlib.sha3_256(raw.encode()).hexdigest()

def _ai_day(ts=None) -> str:
    """AI 预算窗口按自然日划分（UTC，审计 M-13：避免跨时区节点判定不一致）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


class SocialFi:
    """所有玩法状态保存在 self.store 上，随状态快照持久化与全网同步。"""

    def __init__(self, store, economy, storage_net):
        self.store = store
        self.economy = economy
        self.storage_net = storage_net

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------
    def _pin_content(self, addr, cid, size_gb, duration_days) -> bool:
        """将内容固定到存储网络（占用链的存储能力）。"""
        if not cid or cid in self.store.storage_claims:
            return False
        if not (isinstance(size_gb, (int, float)) and not isinstance(size_gb, bool)
                and size_gb >= PIN_SIZE_GB):
            size_gb = PIN_SIZE_GB
        if not (isinstance(duration_days, (int, float)) and not isinstance(duration_days, bool)
                and duration_days >= 1):
            duration_days = PIN_DURATION_DAYS
        # 审计 M-7：应用每地址固定数量上限（与 nova:storage:pin 一致），
        # 防止通过发帖/策展/文本发布携带唯一 CID 无限抽干生态基金
        from core.storage_network import MAX_PINS_PER_ADDR
        mine = [c for c in self.store.storage_claims.values() if c.get("owner") == addr]
        if len(mine) >= MAX_PINS_PER_ADDR:
            return False
        reward = self.storage_net.pin_reward(size_gb, duration_days)
        if self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) < reward:
            return False
        self.storage_net.pin(addr, cid, size_gb, duration_days)
        return True

    def _valid_cid_extra(self, d) -> bool:
        cid = d.get("cid", "")
        if not cid:
            return True
        if not CID_RE.match(cid) or cid in self.store.storage_claims:
            return False
        size = d.get("size_gb", PIN_SIZE_GB)
        days = d.get("duration_days", PIN_DURATION_DAYS)
        if not (isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0):
            return False
        if not (isinstance(days, (int, float)) and not isinstance(days, bool) and days >= 1):
            return False
        return self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) >= self.storage_net.pin_reward(size, days)

    def _record(self, tx, op, obj_id, summary=""):
        self.store.socialfi_events[tx.txid] = {
            "op": op, "id": obj_id, "addr": tx.sender, "ts": time.time(),
            "summary": summary or obj_id,
        }

    # ------------------------------------------------------------------
    # 1. 粉丝代币发行平台
    # ------------------------------------------------------------------
    def fan_price_at(self, tid, qty=1) -> float:
        t = self.store.fan_tokens[tid]
        sold = float(t["sold"])
        price = float(t["price"]) * (1 + sold / float(t["supply"]))
        return _amt(price * qty)

    def _fan_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:fan:issue":
            symbol, name = d.get("symbol", ""), d.get("name", "")
            supply, price = d.get("supply", 0), d.get("price", 0)
            if tx.amount != 0 or not SYMBOL_RE.match(symbol):
                return False
            if not (isinstance(name, str) and 0 < len(name) <= NAME_MAX):
                return False
            if not (isinstance(supply, int) and not isinstance(supply, bool) and 1 <= supply <= FAN_MAX_SUPPLY):
                return False
            if not (isinstance(price, (int, float)) and not isinstance(price, bool)
                    and price >= FAN_MIN_PRICE and price <= 1e6):
                return False
            return self._valid_cid_extra(d)
        if op == "nova:fan:buy":
            tid, qty = d.get("tid", ""), d.get("qty", 0)
            if tid not in self.store.fan_tokens:
                return False
            if not (isinstance(qty, int) and not isinstance(qty, bool) and qty >= 1):
                return False
            t = self.store.fan_tokens[tid]
            if addr == t["creator"] or t["sold"] + qty > t["supply"]:
                return False
            cost = self.fan_price_at(tid, qty)
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) \
                and _amt(tx.amount) == cost
        if op == "nova:fan:propose":
            tid, title = d.get("tid", ""), d.get("title", "")
            closes_in = d.get("closes_in", 0)
            if tx.amount != 0 or tid not in self.store.fan_tokens:
                return False
            if self.store.fan_tokens[tid]["holders"].get(addr, 0) < 1:
                return False
            if not (isinstance(title, str) and 0 < len(title) <= NAME_MAX):
                return False
            return isinstance(closes_in, (int, float)) and not isinstance(closes_in, bool) \
                and 300 <= closes_in <= 90 * 86400
        if op == "nova:fan:vote":
            tid, pid, option = d.get("tid", ""), d.get("proposal_id", ""), d.get("option", -1)
            if tx.amount != 0 or tid not in self.store.fan_tokens:
                return False
            t = self.store.fan_tokens[tid]
            voted = t["voted"].get(pid, set())
            if t["holders"].get(addr, 0) < 1 or addr in voted:
                return False
            prop = t["proposals"].get(pid)
            if not prop or time.time() >= prop["closes_at"]:
                return False
            return isinstance(option, int) and not isinstance(option, bool) and 0 <= option < len(prop["options"])
        return False

    def _fan_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:fan:issue":
            symbol, name = d.get("symbol"), d.get("name")
            supply, price = int(d["supply"]), float(d["price"])
            tid = "fan_" + _h(f"{addr}{symbol}{name}{tx.txid}")[:20]
            self.store.fan_tokens[tid] = {
                "id": tid, "creator": addr, "symbol": symbol, "name": name,
                "supply": supply, "sold": 0, "price": price,
                "avatar_cid": d.get("cid", ""), "created_at": time.time(),
                "holders": {}, "proposals": {}, "voted": {},
            }
            self._pin_content(addr, d.get("cid", ""), d.get("size_gb", PIN_SIZE_GB),
                              d.get("duration_days", PIN_DURATION_DAYS))
            self._record(tx, op, tid, f"发行粉丝代币 {symbol} · {name}")
        elif op == "nova:fan:buy":
            tid, qty = d["tid"], int(d["qty"])
            t = self.store.fan_tokens[tid]
            cost = self.fan_price_at(tid, qty)
            t["sold"] += qty
            t["holders"][addr] = t["holders"].get(addr, 0) + qty
            self.store.balances[addr] = self.store.balances.get(addr, 0) - cost
            self.store.balances[t["creator"]] = self.store.balances.get(t["creator"], 0) + cost
            self._record(tx, op, tid, f"买入 {qty} 份 {t['symbol']}")
        elif op == "nova:fan:propose":
            tid, title = d["tid"], d["title"]
            closes_in = float(d["closes_in"])
            t = self.store.fan_tokens[tid]
            pid = "fp_" + _h(f"{tid}{addr}{title}{closes_in}")[:20]
            t["proposals"][pid] = {
                "id": pid, "proposer": addr, "title": title,
                "closes_at": time.time() + closes_in,
                "options": ["支持", "反对"], "votes": [0, 0],
            }
            t["voted"][pid] = set()
            self._record(tx, op, pid, f"发起提案「{title}」")
        elif op == "nova:fan:vote":
            tid, pid, option = d["tid"], d["proposal_id"], int(d["option"])
            t = self.store.fan_tokens[tid]
            t["proposals"][pid]["votes"][option] += t["holders"].get(addr, 0)
            t["voted"].setdefault(pid, set()).add(addr)
            self._record(tx, op, pid, "投票完成")    # ------------------------------------------------------------------
    # 2. 收益共享合约
    # ------------------------------------------------------------------
    def _rev_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:rev:create":
            name = d.get("name", "")
            if tx.amount != 0:
                return False
            if not (isinstance(name, str) and 0 < len(name) <= NAME_MAX):
                return False
            return isinstance(d.get("desc", ""), str) and len(d.get("desc", "")) <= DESC_MAX
        if op == "nova:rev:invest":
            rid, amount = d.get("rid", ""), d.get("amount", 0)
            if rid not in self.store.revenue_shares:
                return False
            r = self.store.revenue_shares[rid]
            if addr == r["creator"]:
                return False
            if not (isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 1):
                return False
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) \
                and _amt(tx.amount) == _amt(amount)
        if op == "nova:rev:royalty":
            rid, amount = d.get("rid", ""), d.get("amount", 0)
            if rid not in self.store.revenue_shares:
                return False
            r = self.store.revenue_shares[rid]
            if addr != r["creator"] or not (isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0):
                return False
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) \
                and _amt(tx.amount) == _amt(amount)
        if op == "nova:rev:claim":
            rid = d.get("rid", "")
            if tx.amount != 0 or rid not in self.store.revenue_shares:
                return False
            r = self.store.revenue_shares[rid]
            return r["investors"].get(addr, 0) > 0 and self.pending_rev_claim(rid, addr) > 0
        return False

    def pending_rev_claim(self, rid, addr) -> float:
        r = self.store.revenue_shares.get(rid)
        if not r or r["pool"] <= 0:
            return 0.0
        total = sum(float(v) for v in r["investors"].values())
        if total <= 0:
            return 0.0
        # 审计修复 H-2：按「累计注入版税 × 份额占比 − 已领取」计算应得，并封顶当前池子。
        # 修复前按「当前池 × 占比」计算且不记录已领取，导致反复领取收敛到整池（抽干其他投资者）。
        frac = float(r["investors"].get(addr, 0)) / total
        pooled = float(r.get("total_pooled", r["pool"]))
        fair = _amt(frac * pooled)
        paid = float(r.get("paid", {}).get(addr, 0.0))
        pending = _amt(fair - paid)
        if pending <= 0:
            return 0.0
        return min(pending, _amt(float(r["pool"])))

    def _rev_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:rev:create":
            name = d["name"]
            rid = "rev_" + _h(f"{addr}{name}{tx.txid}")[:20]
            self.store.revenue_shares[rid] = {
                "id": rid, "creator": addr, "name": name, "desc": d.get("desc", ""),
                "investors": {}, "total_invested": 0.0, "pool": 0.0,
                "total_pooled": 0.0, "paid": {}, "created_at": time.time(),
            }
            self._record(tx, op, rid, f"开设收益共享「{name}」")
        elif op == "nova:rev:invest":
            rid, amount = d["rid"], float(d["amount"])
            r = self.store.revenue_shares[rid]
            r["investors"][addr] = _amt(r["investors"].get(addr, 0) + amount)
            r["total_invested"] = _amt(r["total_invested"] + amount)
            self.store.balances[addr] = self.store.balances.get(addr, 0) - amount
            self.store.balances[r["creator"]] = self.store.balances.get(r["creator"], 0) + amount
            self._record(tx, op, rid, f"投资 {amount} NOVA 支持 {r['creator'][:8]}…")
        elif op == "nova:rev:royalty":
            rid, amount = d["rid"], float(d["amount"])
            r = self.store.revenue_shares[rid]
            r["pool"] = _amt(r["pool"] + amount)
            r["total_pooled"] = _amt(r.get("total_pooled", 0.0) + amount)
            self.store.balances[addr] = self.store.balances.get(addr, 0) - amount
            self._record(tx, op, rid, f"注入版税收益 {amount} NOVA")
        elif op == "nova:rev:claim":
            rid = d["rid"]
            r = self.store.revenue_shares[rid]
            payout = self.pending_rev_claim(rid, addr)
            if payout > 0:
                r["pool"] = _amt(r["pool"] - payout)
                paid_map = r.setdefault("paid", {})
                paid_map[addr] = _amt(paid_map.get(addr, 0.0) + payout)
                self.store.balances[addr] = self.store.balances.get(addr, 0) + payout
                self._record(tx, op, rid, f"领取收益分成 {payout} NOVA")

    # ------------------------------------------------------------------
    # 3. 链上成就系统（灵魂绑定）
    # ------------------------------------------------------------------
    def _ach_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:ach:issue":
            title, badge = d.get("title", ""), d.get("badge", "")
            aid = "ach_" + _h(f"{addr}{title}{tx.txid}")[:20]
            if tx.amount != 0 or aid in self.store.achievements:
                return False
            return (isinstance(title, str) and 0 < len(title) <= NAME_MAX
                    and isinstance(d.get("desc", ""), str) and len(d["desc"]) <= DESC_MAX
                    and isinstance(badge, str) and 0 < len(badge) <= 8)
        if op == "nova:ach:award":
            aid, target = d.get("aid", ""), d.get("target", "")
            if tx.amount != 0 or aid not in self.store.achievements:
                return False
            # 审计 M-5：仅成就创建者（issuer）可颁发（此前任意地址可把已有成就颁给自己刷声誉分）
            if self.store.achievements[aid]["issuer"] != tx.sender:
                return False
            if not ADDRESS_RE.match(target) or target in self.store.soulbound.get(aid, {}):
                return False
            return True
        return False

    def _ach_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:ach:issue":
            title, badge = d["title"], d["badge"]
            aid = "ach_" + _h(f"{addr}{title}{tx.txid}")[:20]
            self.store.achievements[aid] = {
                "id": aid, "issuer": addr, "title": title,
                "desc": d.get("desc", ""), "badge": badge, "created_at": time.time(),
            }
            self.store.soulbound[aid] = {}
            self._record(tx, op, aid, f"创建成就「{title}」")
        elif op == "nova:ach:award":
            aid, target = d["aid"], d["target"]
            self.store.soulbound[aid][target] = time.time()
            self._record(tx, op, aid, f"颁发成就 → {target[:10]}…")

    # ------------------------------------------------------------------
    # 4. 预言机驱动的预测市场
    # ------------------------------------------------------------------
    def _mkt_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:market:create":
            question, options = d.get("question", ""), d.get("options", [])
            closes_in = d.get("closes_in", 0)
            oracle = d.get("oracle", addr)
            if tx.amount != 0:
                return False
            if not (isinstance(question, str) and 0 < len(question) <= 200):
                return False
            if not (isinstance(options, list) and 2 <= len(options) <= OPTIONS_MAX):
                return False
            for o in options:
                if not (isinstance(o, str) and 0 < len(o) <= 64):
                    return False
            if not (isinstance(closes_in, (int, float)) and not isinstance(closes_in, bool)
                    and 300 <= closes_in <= 90 * 86400):
                return False
            return ADDRESS_RE.match(oracle)
        if op == "nova:market:bet":
            mid, option, amount = d.get("mid", ""), d.get("option", -1), d.get("amount", 0)
            if mid not in self.store.markets:
                return False
            m = self.store.markets[mid]
            if m["settled"] or time.time() >= m["closes_at"]:
                return False
            # 审计 M-6：预言机 / 创建者不得自我下注（防内幕套利）
            if addr == m["oracle"] or addr == m["creator"]:
                return False
            if not (isinstance(option, int) and not isinstance(option, bool) and 0 <= option < len(m["options"])):
                return False
            if not (isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 1):
                return False
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) \
                and _amt(tx.amount) == _amt(amount)
        if op == "nova:market:settle":
            mid, outcome = d.get("mid", ""), d.get("outcome", -1)
            if mid not in self.store.markets:
                return False
            m = self.store.markets[mid]
            if tx.amount != 0 or m["settled"] or time.time() < m["closes_at"]:
                return False
            if addr != m["oracle"]:
                return False
            return isinstance(outcome, int) and not isinstance(outcome, bool) and 0 <= outcome < len(m["options"])
        return False

    def _mkt_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:market:create":
            question, options = d["question"], d["options"]
            closes_in = float(d["closes_in"])
            oracle = d.get("oracle", addr)
            mid = "mkt_" + _h(f"{addr}{question}{tx.txid}")[:20]
            self.store.markets[mid] = {
                "id": mid, "creator": addr, "oracle": oracle,
                "question": question, "options": list(options),
                "closes_at": time.time() + closes_in,
                "pool": [0.0] * len(options), "bets": {}, "settled": False,
                "outcome": None, "created_at": time.time(),
            }
            self._record(tx, op, mid, f"开设预测市场「{question[:24]}」")
        elif op == "nova:market:bet":
            mid, option, amount = d["mid"], int(d["option"]), float(d["amount"])
            m = self.store.markets[mid]
            m["pool"][option] = _amt(m["pool"][option] + amount)
            bets = m["bets"].setdefault(addr, {})
            bets[option] = _amt(bets.get(option, 0) + amount)
            self.store.balances[addr] = self.store.balances.get(addr, 0) - amount
            self._record(tx, op, mid, f"押注 {amount} NOVA")
        elif op == "nova:market:settle":
            mid, outcome = d["mid"], int(d["outcome"])
            m = self.store.markets[mid]
            total = float(sum(m["pool"]))
            win_pool = float(m["pool"][outcome])
            m["settled"] = True
            m["outcome"] = outcome
            if win_pool > 0 and total > 0:
                fee = _amt(total * MARKET_FEE_RATIO)
                self.store.balances[self.economy.ECOSYSTEM_FUND] = \
                    self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) + fee
                for addr_bet, opts in m["bets"].items():
                    bet = float(opts.get(outcome, 0))
                    if bet > 0:
                        payout = _amt(bet / win_pool * (total - fee))
                        self.store.balances[addr_bet] = self.store.balances.get(addr_bet, 0) + payout
                        m["payouts"] = m.get("payouts", {})
                        m["payouts"][addr_bet] = _amt(m["payouts"].get(addr_bet, 0) + payout)
            self._record(tx, op, mid, f"结算结果：{m['options'][outcome]}")    # ------------------------------------------------------------------
    # 5. 链上随机抽奖 / 盲盒（commit-reveal 可验证随机）
    # ------------------------------------------------------------------
    def _blind_validate(self, d, tx):
        op = d.get("op")
        if op == "nova:blind:create":
            name, commit, tiers = d.get("name", ""), d.get("commit", ""), d.get("tiers", [])
            price = d.get("price", 0)
            reserve = d.get("reserve", 0)
            if tx.amount != 0:
                return False
            if not (isinstance(name, str) and 0 < len(name) <= NAME_MAX):
                return False
            if not HEX64_RE.match(commit):
                return False
            if not (isinstance(price, (int, float)) and not isinstance(price, bool) and price >= FAN_MIN_PRICE):
                return False
            # 审计修复 H-3：nova 奖励必须由创建者预存储备金（reserve）支付，杜绝凭空铸币。
            if not (isinstance(reserve, (int, float)) and not isinstance(reserve, bool)
                    and math.isfinite(reserve) and reserve >= 0):
                return False
            if reserve > 0 and self.store.balances.get(tx.sender, 0) < reserve - 1e-9:
                return False
            if not (isinstance(tiers, list) and 1 <= len(tiers) <= TIERS_MAX):
                return False
            for t in tiers:
                if not (isinstance(t, dict) and isinstance(t.get("name"), str) and 0 < len(t["name"]) <= NAME_MAX):
                    return False
                if not (isinstance(t.get("weight", 0), (int, float)) and not isinstance(t.get("weight"), bool)
                        and t["weight"] >= 1):
                    return False
                if t.get("reward_type") not in ("nova", "badge"):
                    return False
                if t["reward_type"] == "nova" and not (isinstance(t.get("reward_amount", 0), (int, float))
                                                       and not isinstance(t.get("reward_amount"), bool)
                                                       and t["reward_amount"] >= 0):
                    return False
                if t.get("reward_cid") and not CID_RE.match(t["reward_cid"]):
                    return False
            # 审计修复 H-3：期望奖励（按权重加权）必须 ≤ 售价，防止开盒凭空铸币。
            # EV = Σ(weight × reward) / Σ(weight)
            total_w = sum(int(t["weight"]) for t in tiers)
            ev = sum(float(t.get("reward_amount", 0)) * int(t["weight"])
                     for t in tiers if t["reward_type"] == "nova") / total_w
            if ev > price + 1e-9:
                return False
            return True
        if op == "nova:blind:reveal":
            bid, seed = d.get("bid", ""), d.get("seed", "")
            if tx.amount != 0 or bid not in self.store.blindboxes:
                return False
            if bid in self.store.blind_reveals or not HEX64_RE.match(seed):
                return False
            return _h(seed.lower()) == self.store.blindboxes[bid]["commit"]
        if op == "nova:blind:open":
            bid, draws = d.get("bid", ""), d.get("draws", 0)
            if bid not in self.store.blindboxes or bid not in self.store.blind_reveals:
                return False
            box = self.store.blindboxes[bid]
            # 审计修复 H-3：创建者不得自开（防创建者自开套利铸造）
            if tx.sender == box["creator"]:
                return False
            if not (isinstance(draws, int) and not isinstance(draws, bool) and 1 <= draws <= BLIND_MAX_DRAWS):
                return False
            price = _amt(float(box["price"]) * draws)
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) \
                and _amt(tx.amount) == price
        return False

    @staticmethod
    def blind_draw(box, seed, addr, nonce) -> dict:
        """可验证抽奖：sha3_256(seed || addr || nonce) 映射到加权档位。"""
        rand = int(_h(seed + addr + str(nonce))[:16], 16)
        total_w = sum(int(t["weight"]) for t in box["tiers"])
        pos = rand % total_w
        for t in box["tiers"]:
            pos -= int(t["weight"])
            if pos < 0:
                return t
        return box["tiers"][-1]

    def _blind_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:blind:create":
            name, commit = d["name"], d["commit"].lower()
            price, tiers = float(d["price"]), d["tiers"]
            reserve = float(d.get("reserve", 0.0))
            bid = "box_" + _h(f"{addr}{name}{commit}{tx.txid}")[:20]
            # 审计修复 H-3：创建者预存储备金，开盒奖励从中支付（不再凭空铸造）
            if reserve > 0:
                self.store.balances[addr] = self.store.balances.get(addr, 0) - reserve
            self.store.blindboxes[bid] = {
                "id": bid, "creator": addr, "name": name, "price": price,
                "commit": commit, "tiers": list(tiers), "reserve": reserve,
                "created_at": time.time(), "draws": {},
            }
            self._record(tx, op, bid, f"上架盲盒「{name}」")
        elif op == "nova:blind:reveal":
            bid, seed = d["bid"], d["seed"].lower()
            self.store.blind_reveals[bid] = seed
            self._record(tx, op, bid, "盲盒种子已揭示（可验证随机）")
        elif op == "nova:blind:open":
            bid, draws = d["bid"], int(d["draws"])
            box = self.store.blindboxes[bid]
            seed = self.store.blind_reveals[bid]
            nonce = box["draws"].get(addr, 0)
            cost = _amt(float(box["price"]) * draws)
            self.store.balances[addr] = self.store.balances.get(addr, 0) - cost
            self.store.balances[box["creator"]] = self.store.balances.get(box["creator"], 0) + cost
            won = []
            for i in range(draws):
                tier = self.blind_draw(box, seed, addr, nonce + i)
                if tier["reward_type"] == "nova":
                    rw = float(tier.get("reward_amount", 0))
                    # 审计修复 H-3：奖励从储备金支付（不足则少付/不付），绝不凭空铸造
                    pay = min(rw, float(box.get("reserve", 0.0)))
                    if pay > 0:
                        box["reserve"] = round(float(box.get("reserve", 0.0)) - pay, 8)
                        self.store.balances[addr] = self.store.balances.get(addr, 0) + pay
                        won.append({"tier": tier["name"], "type": "nova", "amount": pay})
                    else:
                        won.append({"tier": tier["name"], "type": "nova",
                                    "amount": 0.0, "out_of_reserve": True})
                else:
                    aid = "ach_" + _h(f"{box['id']}{tier['name']}{addr}{nonce + i}")[:20]
                    self.store.achievements.setdefault(aid, {
                        "id": aid, "issuer": box["creator"], "title": f"盲盒·{tier['name']}",
                        "desc": tier.get("reward_cid", ""), "badge": "🎁", "created_at": time.time(),
                    })
                    self.store.soulbound.setdefault(aid, {})[addr] = time.time()
                    won.append({"tier": tier["name"], "type": "badge", "aid": aid})
            box["draws"][addr] = nonce + draws
            self._record(tx, op, bid, f"开盒 {draws} 次")

    # ------------------------------------------------------------------
    # 6. 去中心化内容策展
    # ------------------------------------------------------------------
    def _cur_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:curate:create":
            title, items, price = d.get("title", ""), d.get("items", []), d.get("price", 0)
            if tx.amount != 0:
                return False
            if not (isinstance(title, str) and 0 < len(title) <= NAME_MAX):
                return False
            if not (isinstance(items, list) and 1 <= len(items) <= ITEMS_MAX):
                return False
            for it in items:
                if not (isinstance(it, str) and 0 < len(it) <= 200):
                    return False
            if not (isinstance(price, (int, float)) and not isinstance(price, bool) and price >= FAN_MIN_PRICE):
                return False
            return self._valid_cid_extra(d)
        if op == "nova:curate:buy":
            cur = d.get("cur_id", "")
            if cur not in self.store.curations:
                return False
            c = self.store.curations[cur]
            if addr == c["curator"] or addr in c["owners"]:
                return False
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) \
                and _amt(tx.amount) == _amt(c["price"])
        return False

    def _cur_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:curate:create":
            title, items, price = d["title"], d["items"], float(d["price"])
            cur = "cur_" + _h(f"{addr}{title}{tx.txid}")[:20]
            self.store.curations[cur] = {
                "id": cur, "curator": addr, "title": title, "items": list(items),
                "price": price, "owners": [addr], "cover_cid": d.get("cid", ""),
                "created_at": time.time(),
            }
            self._pin_content(addr, d.get("cid", ""), d.get("size_gb", PIN_SIZE_GB),
                              d.get("duration_days", PIN_DURATION_DAYS))
            self._record(tx, op, cur, f"创建策展「{title}」")
        elif op == "nova:curate:buy":
            cur = d["cur_id"]
            c = self.store.curations[cur]
            price = float(c["price"])
            self.store.balances[addr] = self.store.balances.get(addr, 0) - price
            curator_share = _amt(price * CURATE_CURATOR_SHARE)
            eco_share = _amt(price - curator_share)
            self.store.balances[c["curator"]] = self.store.balances.get(c["curator"], 0) + curator_share
            self.store.balances[self.economy.ECOSYSTEM_FUND] = \
                self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) + eco_share
            c["owners"].append(addr)
            self._record(tx, op, cur, f"收藏策展「{c['title']}」")    # ------------------------------------------------------------------
    # 7. 社交图谱与推荐引擎
    # ------------------------------------------------------------------
    def _graph_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:graph:post":
            content = d.get("content", "")
            if tx.amount != 0:
                return False
            if not (isinstance(content, str) and 0 < len(content) <= CONTENT_MAX):
                return False
            return self._valid_cid_extra(d)
        if op == "nova:graph:follow":
            target = d.get("target", "")
            if tx.amount != 0:
                return False
            if not ADDRESS_RE.match(target) or target == addr:
                return False
            return target not in self.store.graph_follows.get(addr, [])
        if op == "nova:graph:like":
            pid = d.get("pid", "")
            if tx.amount != 0 or pid not in self.store.graph_posts:
                return False
            return addr not in self.store.graph_posts[pid]["likes"]
        return False

    def _graph_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:graph:post":
            content = d.get("content", "")
            cid = d.get("cid", "")
            pid = "p_" + _h(f"{addr}{content}{tx.txid}")[:20]
            self.store.graph_posts[pid] = {
                "id": pid, "addr": addr, "content": content,
                "cid": cid, "likes": [], "ts": time.time(),
            }
            self._pin_content(addr, cid, d.get("size_gb", PIN_SIZE_GB),
                              d.get("duration_days", PIN_DURATION_DAYS))
            self._record(tx, op, pid, content[:20])
        elif op == "nova:graph:follow":
            target = d["target"]
            self.store.graph_follows.setdefault(addr, []).append(target)
            self._record(tx, op, target, f"关注 {target[:10]}…")
        elif op == "nova:graph:like":
            pid = d["pid"]
            self.store.graph_posts[pid]["likes"].append(addr)
            self._record(tx, op, pid, "点赞")

    def graph_hash(self) -> str:
        """社交图谱状态哈希（可作算力任务规格，验证推荐计算的输入）。"""
        payload = {
            "follows": {k: sorted(v) for k, v in self.store.graph_follows.items()},
            "posts": {k: {"addr": v["addr"], "likes": sorted(v["likes"]), "ts": v["ts"]}
                      for k, v in self.store.graph_posts.items()},
        }
        return _h(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def recommendations(self, addr, limit=6) -> list:
        """确定性链上推荐：好友的好友 + 同好交集 + 高声誉创作者加权。"""
        follows = self.store.graph_follows
        score = {}
        reason = {}
        for f in follows.get(addr, []):
            score[f] = score.get(f, 0) + 1.0
            reason.setdefault(f, "已关注")
            for f2 in follows.get(f, []):
                if f2 != addr:
                    score[f2] = score.get(f2, 0) + 3.0
                    reason.setdefault(f2, "好友的好友")
        liked_pids = [pid for pid, p in self.store.graph_posts.items() if addr in p["likes"]]
        for pid in liked_pids:
            p = self.store.graph_posts[pid]
            for liker in p["likes"]:
                if liker == addr:
                    continue
                score[liker] = score.get(liker, 0) + 2.0
                reason.setdefault(liker, "品味相似")
                for f in follows.get(liker, []):
                    if f != addr:
                        score[f] = score.get(f, 0) + 1.0
                        reason.setdefault(f, "同好关注")
        for tid, t in self.store.fan_tokens.items():
            c = t["creator"]
            if c == addr:
                continue
            score[c] = score.get(c, 0) + 1.0
            reason.setdefault(c, "创作者")
        ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        for cand, s in ranked[:limit]:
            rep = self.reputation(cand)
            out.append({"addr": cand, "score": round(s, 2), "reason": reason.get(cand, "潜在兴趣"),
                        "reputation": rep["score"]})
        return out

    def recommend_task_spec(self, addr) -> str:
        """推荐引擎的算力任务规格：可直接 POST /api/compute/publish。"""
        return f"nova:recommend:{addr}:{self.graph_hash()}"

    # ------------------------------------------------------------------
    # 8. 链上声誉系统
    # ------------------------------------------------------------------
    def reputation(self, addr) -> dict:
        st = self.store
        comp = {}
        staked = float(st.stakes.get(addr, 0))
        comp["质押"] = round(min(staked / 1000.0 * 20, 20), 2)
        comp["签到"] = round(min(st.light_checkins.get(addr, 0) / 270.0 * 20, 20), 2)
        deploys = sum(1 for c in st.contract_creator.values() if c == addr)
        comp["部署合约"] = round(min(deploys * 5, 15), 2)
        refs = sum(1 for r in st.referrals.values() if r == addr)
        comp["推荐"] = round(min(refs * 5, 10), 2)
        earned = sum(1 for aid, targets in st.soulbound.items() if addr in targets)
        comp["成就"] = round(min(earned * 2, 10), 2)
        created_cur = sum(1 for c in st.curations.values() if c["curator"] == addr)
        bought_cur = sum(1 for c in st.curations.values() if addr in c["owners"] and c["curator"] != addr)
        comp["策展"] = round(min((created_cur + bought_cur) * 2, 10), 2)
        held_tokens = sum(t["holders"].get(addr, 0) for t in st.fan_tokens.values())
        comp["粉丝代币"] = round(min(held_tokens / 100.0, 5), 2)
        posts = sum(1 for p in st.graph_posts.values() if p["addr"] == addr)
        likes_got = sum(len(p["likes"]) for p in st.graph_posts.values() if p["addr"] == addr)
        comp["内容"] = round(min(posts * 2 + likes_got * 0.5, 10), 2)
        comp["文本创作"] = round(min(self._text_rep(addr) / 5.0, 15), 2)
        score = round(sum(comp.values()), 2)
        if addr in st.jailed:
            score = max(0.0, round(score - 10, 2))
            comp["惩戒"] = -10.0
        tier_name, tier_grade = "星尘", "C"
        for lo, nm, gr in REPUTATION_TIERS:
            if score >= lo:
                tier_name, tier_grade = nm, gr
                break
        return {
            "addr": addr, "score": min(score, 100.0),
            "components": comp, "tier": tier_name, "grade": tier_grade,
            "fee_multiplier": 0.5 if score >= 80 else 1.0,
        }    # ------------------------------------------------------------------
    # 9. 创作者债券
    # ------------------------------------------------------------------
    def bond_owed(self, bid) -> float:
        b = self.store.bonds[bid]
        years = b["term_days"] / 365.0
        total = 0.0
        for v in b["sold"].values():
            total += float(v) * (1 + float(b["rate"]) * years)
        return _amt(total)

    def _bond_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:bond:issue":
            name, principal, rate, term_days = (d.get("name", ""), d.get("principal", 0),
                                                d.get("rate", 0), d.get("term_days", 0))
            if tx.amount != 0:
                return False
            if not (isinstance(name, str) and 0 < len(name) <= NAME_MAX):
                return False
            if not (isinstance(principal, (int, float)) and not isinstance(principal, bool)
                    and principal >= BOND_MIN_PRINCIPAL):
                return False
            if not (isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0 <= rate <= BOND_MAX_RATE):
                return False
            return isinstance(term_days, (int, float)) and not isinstance(term_days, bool) \
                and BOND_MIN_TERM_DAYS <= term_days <= BOND_MAX_TERM_DAYS
        if op == "nova:bond:buy":
            bid, amount = d.get("bid", ""), d.get("amount", 0)
            if bid not in self.store.bonds:
                return False
            b = self.store.bonds[bid]
            if addr == b["creator"] or b["settled"] or time.time() >= b["matures_at"]:
                return False
            if not (isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 1):
                return False
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) \
                and _amt(tx.amount) == _amt(amount)
        if op == "nova:bond:fund":
            bid, amount = d.get("bid", ""), d.get("amount", 0)
            if bid not in self.store.bonds:
                return False
            b = self.store.bonds[bid]
            if addr != b["creator"] or b["settled"]:
                return False
            if not (isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0):
                return False
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) \
                and _amt(tx.amount) == _amt(amount)
        if op == "nova:bond:redeem":
            bid = d.get("bid", "")
            if tx.amount != 0 or bid not in self.store.bonds:
                return False
            b = self.store.bonds[bid]
            if time.time() < b["matures_at"] or b["settled"]:
                return False
            return b["sold"].get(addr, 0) > 0 and b["pool"] > 0
        return False

    def _bond_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:bond:issue":
            name, principal, rate, term_days = (d["name"], float(d["principal"]),
                                                float(d["rate"]), int(d["term_days"]))
            bid = "bnd_" + _h(f"{addr}{name}{tx.txid}")[:20]
            self.store.bonds[bid] = {
                "id": bid, "creator": addr, "name": name, "principal": principal,
                "rate": rate, "term_days": term_days, "sold": {}, "pool": 0.0,
                "settled": False, "created_at": time.time(),
                "matures_at": time.time() + term_days * 86400,
            }
            self._record(tx, op, bid, f"发行债券「{name}」")
        elif op == "nova:bond:buy":
            bid, amount = d["bid"], float(d["amount"])
            b = self.store.bonds[bid]
            b["sold"][addr] = _amt(b["sold"].get(addr, 0) + amount)
            self.store.balances[addr] = self.store.balances.get(addr, 0) - amount
            self.store.balances[b["creator"]] = self.store.balances.get(b["creator"], 0) + amount
            self._record(tx, op, bid, f"认购债券 {amount} NOVA")
        elif op == "nova:bond:fund":
            bid, amount = d["bid"], float(d["amount"])
            b = self.store.bonds[bid]
            b["pool"] = _amt(b["pool"] + amount)
            self.store.balances[addr] = self.store.balances.get(addr, 0) - amount
            self._record(tx, op, bid, f"注入偿债池 {amount} NOVA")
        elif op == "nova:bond:redeem":
            bid = d["bid"]
            b = self.store.bonds[bid]
            years = b["term_days"] / 365.0
            invested = float(b["sold"].get(addr, 0))
            total_owed = self.bond_owed(bid)
            factor = min(1.0, float(b["pool"]) / total_owed) if total_owed > 0 else 0.0
            payout = _amt(invested * (1 + float(b["rate"]) * years) * factor)
            if payout > 0:
                b["pool"] = _amt(b["pool"] - payout)
                self.store.balances[addr] = self.store.balances.get(addr, 0) + payout
                b["sold"][addr] = 0.0
            if not any(v > 0 for v in b["sold"].values()) or b["pool"] <= 0:
                b["settled"] = True
            self._record(tx, op, bid, f"赎回债券 {payout} NOVA")

    # ------------------------------------------------------------------
    # 10. 碎片化 NFT 市场
    # ------------------------------------------------------------------
    def _frac_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:frac:split":
            name, nft_ref, supply, price_per = (d.get("name", ""), d.get("nft_ref", ""),
                                                d.get("supply", 0), d.get("price_per", 0))
            if tx.amount != 0:
                return False
            if not (isinstance(name, str) and 0 < len(name) <= NAME_MAX):
                return False
            if not (isinstance(nft_ref, str) and 0 < len(nft_ref) <= 200):
                return False
            if not (isinstance(supply, int) and not isinstance(supply, bool)
                    and FRAC_MIN_SUPPLY <= supply <= FRAC_MAX_SUPPLY):
                return False
            return isinstance(price_per, (int, float)) and not isinstance(price_per, bool) \
                and price_per >= FRAC_MIN_PRICE
        if op == "nova:frac:buy":
            fid, qty = d.get("fid", ""), d.get("qty", 0)
            if fid not in self.store.fractions:
                return False
            f = self.store.fractions[fid]
            if addr == f["owner"]:
                return False
            if not (isinstance(qty, int) and not isinstance(qty, bool) and 1 <= qty <= f["owner_hold"]):
                return False
            cost = _amt(qty * float(f["price_per"]))
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) \
                and _amt(tx.amount) == cost
        return False

    def _frac_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:frac:split":
            name, nft_ref, supply, price_per = (d["name"], d["nft_ref"],
                                                int(d["supply"]), float(d["price_per"]))
            fid = "fr_" + _h(f"{addr}{nft_ref}{tx.txid}")[:20]
            self.store.fractions[fid] = {
                "id": fid, "owner": addr, "name": name, "nft_ref": nft_ref,
                "supply": supply, "owner_hold": supply, "price_per": price_per,
                "fractions": {addr: supply}, "created_at": time.time(),
            }
            self._record(tx, op, fid, f"拆分 NFT「{name}」为 {supply} 份")
        elif op == "nova:frac:buy":
            fid, qty = d["fid"], int(d["qty"])
            f = self.store.fractions[fid]
            cost = _amt(qty * float(f["price_per"]))
            f["owner_hold"] -= qty
            f["fractions"][addr] = f["fractions"].get(addr, 0) + qty
            self.store.balances[addr] = self.store.balances.get(addr, 0) - cost
            self.store.balances[f["owner"]] = self.store.balances.get(f["owner"], 0) + cost
            self._record(tx, op, fid, f"购买 {qty} 份碎片")    # ------------------------------------------------------------------
    # 11. 文本创作合约：公开文本 / 加密文本（密文市场）
    # ------------------------------------------------------------------
    def text_contract_pubkey(self) -> str:
        """Nova 文本合约公钥：作者用它锁定正文密钥（AES-256），
        购买后合约用私钥把密钥二次加密给买家。由创世状态确定性派生并持久化。"""
        if not self.store.text_contract_priv:
            # 审计 M-8：改用稳定的协议级种子派生密钥，避免依赖易变的余额状态
            # （此前用「当前全部余额」作 seed，节点状态稍有偏差即派生出不同密钥 → 密文跨节点不一致）
            seed = hashlib.sha3_256(b"nova:text:contract:genesis-v1").digest()
            priv, _pub = text_gen_p256_keypair(b"nova:text:contract:" + seed)
            self.store.text_contract_priv = priv
        return text_p256_pub_from_priv(self.store.text_contract_priv)

    def _text_rep(self, addr) -> float:
        return float(self.store.text_reputation.get(addr, 0.0))

    def _text_bump_rep(self, addr, delta) -> None:
        v = min(100.0, max(0.0, self._text_rep(addr) + delta))
        self.store.text_reputation[addr] = v

    def text_deposit_required(self, tier: str, addr: str = None) -> float:
        """阶梯式保证金：基础 10 / 进阶 100 / 专业 1000 NOVA，
        信誉分 >= 80 时自动下调至 50%（如基础档 10 -> 5 NOVA）。"""
        base = {"basic": TEXT_DEPOSIT_BASIC, "advanced": TEXT_DEPOSIT_ADVANCED,
                "pro": TEXT_DEPOSIT_PRO}.get(tier, TEXT_DEPOSIT_BASIC)
        rep = self._text_rep(addr) if addr else 0.0
        discount = 0.5 * min(1.0, rep / 80.0)
        return _amt(base * (1.0 - discount))

    def _is_text_validator(self, addr) -> bool:
        """社区验证者：超级节点矿工 / 质押 >= 100 NOVA 的验证者 / 高信誉（>=70）用户。"""
        if addr in self.store.miner_registry:
            return True
        if float(self.store.stakes.get(addr, 0)) >= 100.0:
            return True
        return self.reputation(addr)["score"] >= 70.0

    def _valid_text_identifier(self, ident) -> bool:
        if not (isinstance(ident, str) and 0 < len(ident) <= TEXT_IDENT_MAX):
            return False
        if any(ord(c) < 32 for c in ident):
            return False
        return all(a.get("identifier") != ident for a in self.store.text_assets.values())

    def _text_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:text:create":
            title = d.get("title", "")
            visibility = d.get("visibility", "public")
            tier = d.get("tier", "basic")
            price = d.get("price", 0)
            if not (isinstance(title, str) and 0 < len(title.strip()) <= TEXT_TITLE_MAX):
                return False
            if visibility not in ("public", "sealed"):
                return False
            if tier not in ("basic", "advanced", "pro"):
                return False
            if not (isinstance(price, (int, float)) and not isinstance(price, bool)
                    and math.isfinite(price)):
                return False
            if visibility == "public":
                if price < 0 or price > 1e6:
                    return False
                content = d.get("content", "")
                if not (isinstance(content, str) and 0 < len(content.strip()) <= TEXT_BODY_MAX):
                    return False
            else:
                if not TEXT_CRYPTO_OK:
                    return False
                if not (isinstance(price, (int, float)) and not isinstance(price, bool)
                        and TEXT_MIN_PRICE <= price <= 1e6):
                    return False
                self.text_contract_pubkey()   # 确保合约密钥对已就绪（确定性派生）
                kc = d.get("key_cipher")
                if not isinstance(kc, dict) or kc.get("tag") != "nova-text-key-v1":
                    return False
                try:
                    k_hex = text_ecies_decrypt(self.store.text_contract_priv, kc)
                except Exception:
                    return False
                if not (isinstance(k_hex, str) and len(k_hex) == 64
                        and re.fullmatch(r"[0-9a-fA-F]{64}", k_hex)):
                    return False
                cipher_cid = d.get("cipher_cid", "")
                cipher_data = d.get("cipher_data", "")
                if bool(cipher_cid) == bool(cipher_data):
                    return False
                if cipher_cid and not CID_RE.match(cipher_cid):
                    return False
                if cipher_data and not (isinstance(cipher_data, str)
                                        and 0 < len(cipher_data) <= TEXT_CIPHER_MAX):
                    return False
            ident = d.get("identifier", "")
            if ident and not self._valid_text_identifier(ident):
                return False
            if not self._valid_cid_extra(d):
                return False
            req = self.text_deposit_required(tier, addr)
            return (isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool)
                    and _amt(tx.amount) == _amt(req))
        if op == "nova:text:buy":
            tid = d.get("text_id", "")
            if tid not in self.store.text_assets:
                return False
            a = self.store.text_assets[tid]
            if addr == a["author"] or a["status"] in ("unlisted", "destroyed"):
                return False
            if addr in a["buyers"]:
                return False
            if a["visibility"] == "sealed":
                bp = d.get("buyer_pub", "")
                if not (isinstance(bp, str) and re.fullmatch(r"04[0-9a-fA-F]{128}", bp)):
                    return False
            return (isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool)
                    and _amt(tx.amount) == _amt(a["price"]))
        if op == "nova:text:unlist":
            tid = d.get("text_id", "")
            if tx.amount != 0 or tid not in self.store.text_assets:
                return False
            a = self.store.text_assets[tid]
            return addr == a["author"] and a["status"] == "listed" and not a.get("disputed")
        if op == "nova:text:destroy":
            tid = d.get("text_id", "")
            if tx.amount != 0 or tid not in self.store.text_assets:
                return False
            a = self.store.text_assets[tid]
            return addr == a["author"] and a["status"] != "destroyed" and not a.get("disputed")
        if op == "nova:text:release_deposit":
            tid = d.get("text_id", "")
            if tx.amount != 0 or tid not in self.store.text_assets:
                return False
            a = self.store.text_assets[tid]
            if addr != a["author"] or a.get("deposit_released") or a.get("disputed"):
                return False
            if a["status"] not in ("unlisted", "destroyed"):
                return False
            return time.time() >= a.get("releasable_at", 0)
        if op == "nova:text:complain":
            tid = d.get("text_id", "")
            if tx.amount != 0 or tid not in self.store.text_assets:
                return False
            a = self.store.text_assets[tid]
            return (a["visibility"] == "sealed" and addr in a["buyers"]
                    and addr != a["author"] and not a.get("disputed")
                    and a["status"] != "destroyed")
        if op == "nova:text:vote":
            tid = d.get("text_id", "")
            support = d.get("support", "")
            if tx.amount != 0 or tid not in self.store.text_assets:
                return False
            a = self.store.text_assets[tid]
            dis = a.get("dispute")
            if not dis or dis.get("settled") or dis.get("outcome") is not None:
                return False
            if support not in ("buyer", "seller", "abstain"):
                return False
            if addr in dis["voters"] or len(dis["voters"]) >= TEXT_DISPUTE_MAX:
                return False
            return self._is_text_validator(addr)
        return False

    def _text_settle(self, a):
        """仲裁结算：>= 2/3 支持买家 -> 赔付+罚没；>= 2/3 支持卖家 -> 释放；
        平局 -> 二次仲裁（扩大仲裁庭）；超时无结论 -> 按卖家处理。"""
        dis = a.get("dispute")
        if not dis or dis.get("settled"):
            return
        voters = dis["voters"]
        n = len(voters)
        b = sum(1 for v in voters.values() if v == "buyer")
        s = sum(1 for v in voters.values() if v == "seller")
        if n < TEXT_DISPUTE_QUORUM:
            if time.time() - dis["started_at"] > TEXT_DISPUTE_TIMEOUT_DAYS * 86400:
                self._text_execute(a, "seller")
            return
        if not dis.get("escalated"):
            if n >= TEXT_DISPUTE_QUORUM and max(b, s) * 3 >= 2 * n:
                self._text_execute(a, "buyer" if b > s else "seller")
                return
            dis["escalated"] = True          # 平局 -> 二次仲裁
            return
        if n >= TEXT_DISPUTE_MAX:
            self._text_execute(a, "buyer" if b > s else "seller")

    def _text_execute(self, a, winner):
        """执行仲裁结果：从保证金池划款 + 信誉分结算（确定性、自动执行）。"""
        dis = a["dispute"]
        deposit = float(a["deposit"])
        escrow = self.store.balances.get(TEXT_ESCROW, 0)
        if winner == "buyer":
            comp = _amt(deposit * 0.5)
            forfeit = _amt(deposit - comp)
            self.store.balances[TEXT_ESCROW] = _amt(escrow - deposit)
            self.store.balances[dis["complainant"]] = \
                self.store.balances.get(dis["complainant"], 0) + comp
            self.store.balances[self.economy.ECOSYSTEM_FUND] = \
                self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) + forfeit
            self._text_bump_rep(a["author"], -20.0)
            self._text_bump_rep(dis["complainant"], 5.0)
        else:
            self.store.balances[TEXT_ESCROW] = _amt(escrow - deposit)
            self.store.balances[a["author"]] = \
                self.store.balances.get(a["author"], 0) + deposit
        for v, side in dis["voters"].items():
            if side == winner:
                self._text_bump_rep(v, 3.0)
        dis["settled"] = True
        dis["outcome"] = winner
        a["deposit_frozen"] = False
        a["deposit_released"] = True

    def _text_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:text:create":
            title = d["title"].strip()
            visibility = d.get("visibility", "public")
            price = float(d.get("price", 0))
            tier = d.get("tier", "basic")
            deposit = float(tx.amount)
            ident = (d.get("identifier") or "").strip()
            if not ident:
                ident = "t-" + _h(f"{addr}{title}{tx.txid}")[:16]
            tid = "txt_" + _h(f"{addr}{title}{tx.txid}")[:20]
            self.store.balances[addr] = self.store.balances.get(addr, 0) - deposit
            self.store.balances[TEXT_ESCROW] = self.store.balances.get(TEXT_ESCROW, 0) + deposit
            asset = {
                "id": tid, "identifier": ident, "author": addr, "title": title,
                "visibility": visibility, "price": price, "tier": tier,
                "series": bool(d.get("series", False)),
                "exposure_weight": 1.5 if tier == "pro" else 1.0,
                "deposit": deposit, "deposit_frozen": False, "deposit_released": False,
                "status": "listed", "buyers": [], "keys": {},
                "content": "", "cipher_cid": "", "cipher_data": "", "key_cipher": {},
                "dispute": None, "releasable_at": 0, "created_at": time.time(),
                "cid": d.get("cid", ""),
            }
            if visibility == "public":
                asset["content"] = d["content"]
            else:
                asset["cipher_cid"] = d.get("cipher_cid", "")
                asset["cipher_data"] = d.get("cipher_data", "")
                asset["key_cipher"] = d.get("key_cipher") or {}
            self.store.text_assets[tid] = asset
            self._pin_content(addr, d.get("cid", ""), d.get("size_gb", PIN_SIZE_GB),
                              d.get("duration_days", PIN_DURATION_DAYS))
            self._record(tx, op, tid,
                         f"{'加密发布' if visibility == 'sealed' else '发布'}「{title}」")
        elif op == "nova:text:buy":
            tid = d["text_id"]
            a = self.store.text_assets[tid]
            price = float(a["price"])
            self.store.balances[addr] = self.store.balances.get(addr, 0) - price
            author_share = _amt(price * TEXT_AUTHOR_SHARE)
            eco_share = _amt(price - author_share)
            self.store.balances[a["author"]] = self.store.balances.get(a["author"], 0) + author_share
            self.store.balances[self.economy.ECOSYSTEM_FUND] = \
                self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) + eco_share
            a["buyers"].append(addr)
            if a["visibility"] == "sealed":
                k_hex = text_ecies_decrypt(self.store.text_contract_priv, a["key_cipher"])
                seed = (tid + ":" + addr).encode()
                a["keys"][addr] = text_ecies_wrap_to(self.store.text_contract_priv,
                                                     d["buyer_pub"], k_hex, seed)
            self._text_bump_rep(a["author"], 2.0)
            self._text_bump_rep(addr, 1.0)
            self._record(tx, op, tid, f"购买「{a['title']}」{price} NOVA")
        elif op == "nova:text:unlist":
            tid = d["text_id"]
            a = self.store.text_assets[tid]
            a["status"] = "unlisted"
            a["releasable_at"] = time.time() + TEXT_RELEASE_DAYS * 86400
            self._record(tx, op, tid, f"下架「{a['title']}」")
        elif op == "nova:text:destroy":
            tid = d["text_id"]
            a = self.store.text_assets[tid]
            a["status"] = "destroyed"
            a["releasable_at"] = time.time()
            if not a.get("deposit_released"):
                self.store.balances[TEXT_ESCROW] = \
                    _amt(self.store.balances.get(TEXT_ESCROW, 0) - float(a["deposit"]))
                self.store.balances[a["author"]] = \
                    self.store.balances.get(a["author"], 0) + float(a["deposit"])
                a["deposit_released"] = True
            self._record(tx, op, tid, f"销毁密文 NFT「{a['title']}」")
        elif op == "nova:text:release_deposit":
            tid = d["text_id"]
            a = self.store.text_assets[tid]
            self.store.balances[TEXT_ESCROW] = \
                _amt(self.store.balances.get(TEXT_ESCROW, 0) - float(a["deposit"]))
            self.store.balances[a["author"]] = \
                self.store.balances.get(a["author"], 0) + float(a["deposit"])
            a["deposit_released"] = True
            self._record(tx, op, tid, f"退回保证金 {a['deposit']} NOVA")
        elif op == "nova:text:complain":
            tid = d["text_id"]
            a = self.store.text_assets[tid]
            a["dispute"] = {"complainant": addr, "voters": {}, "started_at": time.time(),
                            "escalated": False, "settled": False, "outcome": None}
            a["deposit_frozen"] = True
            self._record(tx, op, tid, f"投诉「{a['title']}」货不对板")
        elif op == "nova:text:vote":
            tid = d["text_id"]
            a = self.store.text_assets[tid]
            a["dispute"]["voters"][addr] = d["support"]
            self._text_settle(a)
            self._record(tx, op, tid, f"仲裁投票（{'支持买家' if d['support'] == 'buyer' else '支持卖家' if d['support'] == 'seller' else '弃权'}）")

    # ------------------------------------------------------------------
    # 11. AI 创作者身份与日预算约束（阶段 0 PoC）
    # ------------------------------------------------------------------
    def _ai_validate(self, d, tx):
        op = d.get("op")
        addr = tx.sender
        if op == "nova:ai:register":
            name = d.get("name", "")
            owner = d.get("owner", "")
            budget = d.get("daily_budget", 0)
            if tx.amount != 0 or addr in self.store.ai_creators:
                return False
            if not (isinstance(name, str) and 0 < len(name.strip()) <= AI_NAME_MAX):
                return False
            if not (isinstance(owner, str) and ADDRESS_RE.match(owner)):
                return False
            if not (isinstance(budget, (int, float)) and not isinstance(budget, bool)
                    and math.isfinite(budget) and AI_MIN_BUDGET <= budget <= AI_MAX_BUDGET):
                return False
            meta = d.get("meta", "")
            return isinstance(meta, str) and len(meta) <= AI_META_MAX
        if op == "nova:ai:config":
            identity = self.store.ai_creators.get(d.get("target", ""))
            if not identity or tx.amount != 0 or tx.sender != identity["owner"]:
                return False
            action = d.get("action", "")
            if action in ("pause", "resume"):
                return True
            if action == "budget":
                budget = d.get("daily_budget", 0)
                return (isinstance(budget, (int, float)) and not isinstance(budget, bool)
                        and math.isfinite(budget) and AI_MIN_BUDGET <= budget <= AI_MAX_BUDGET)
            return False

    def _ai_apply(self, tx, d):
        addr = tx.sender
        op = d.get("op")
        if op == "nova:ai:register":
            identity = {
                "addr": addr,
                "name": d["name"].strip(),
                "owner": d["owner"],
                "daily_budget": float(d["daily_budget"]),
                "meta": d.get("meta", ""),
                "status": "active",
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            self.store.ai_creators[addr] = identity
            self._record(tx, op, addr, f"AI 创作者注册「{identity['name']}」")
        elif op == "nova:ai:config":
            identity = self.store.ai_creators[d["target"]]
            action = d.get("action")
            if action == "pause":
                identity["status"] = "paused"
            elif action == "resume":
                identity["status"] = "active"
            elif action == "budget":
                identity["daily_budget"] = float(d["daily_budget"])
            identity["updated_at"] = time.time()
            self._record(tx, op, d["target"], f"AI 配置更新：{action}")

    # --- 供节点在交易流水线中调用的预算工具（链上强制，非链外建议） ---
    def ai_identity(self, addr):
        return self.store.ai_creators.get(addr)

    def ai_budget_state(self, addr):
        """当日预算窗口：{date, budget, spent, remaining, status}。"""
        identity = self.store.ai_creators.get(addr)
        if not identity:
            return None
        day = _ai_day()
        entry = self.store.ai_daily_spend.get(addr)
        spent = 0.0
        if entry and entry.get("date") == day:
            spent = float(entry.get("spent", 0.0))
        return {
            "date": day,
            "budget": float(identity["daily_budget"]),
            "spent": _amt(spent),
            "remaining": _amt(max(0.0, float(identity["daily_budget"]) - spent)),
            "status": identity.get("status", "active"),
        }

    def ai_can_spend(self, addr, amount) -> bool:
        """AI 地址发起支出前由 validate_tx 调用；非 AI 地址恒为 True。"""
        identity = self.store.ai_creators.get(addr)
        if not identity:
            return True
        if identity.get("status") != "active":
            return False
        if not (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                and math.isfinite(amount) and amount >= 0):
            return False
        st = self.ai_budget_state(addr)
        return st["spent"] + amount <= st["budget"] + 1e-9

    def ai_record_spend(self, addr, amount):
        """apply_tx 时确定性累计当日支出；跨天窗口自动重置。"""
        if not self.store.ai_creators.get(addr):
            return
        day = _ai_day()
        entry = self.store.ai_daily_spend.get(addr)
        if not entry or entry.get("date") != day:
            entry = {"date": day, "spent": 0.0}
            self.store.ai_daily_spend[addr] = entry
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            amt = 0.0
        if not math.isfinite(amt) or amt < 0:
            amt = 0.0
        entry["spent"] = _amt(float(entry.get("spent", 0.0)) + amt)

    # ------------------------------------------------------------------
    # 统一入口：校验 / 应用 / 维护
    # ------------------------------------------------------------------
    OPS = {
        "nova:fan:issue": "_fan", "nova:fan:buy": "_fan", "nova:fan:propose": "_fan",
        "nova:fan:vote": "_fan",
        "nova:rev:create": "_rev", "nova:rev:invest": "_rev", "nova:rev:royalty": "_rev", "nova:rev:claim": "_rev",
        "nova:ach:issue": "_ach", "nova:ach:award": "_ach",
        "nova:market:create": "_mkt", "nova:market:bet": "_mkt", "nova:market:settle": "_mkt",
        "nova:blind:create": "_blind", "nova:blind:reveal": "_blind", "nova:blind:open": "_blind",
        "nova:curate:create": "_cur", "nova:curate:buy": "_cur",
        "nova:graph:post": "_graph", "nova:graph:follow": "_graph", "nova:graph:like": "_graph",
        "nova:bond:issue": "_bond", "nova:bond:buy": "_bond", "nova:bond:fund": "_bond",
        "nova:bond:redeem": "_bond",
        "nova:frac:split": "_frac", "nova:frac:buy": "_frac",
        "nova:text:create": "_text", "nova:text:buy": "_text",
        "nova:text:unlist": "_text", "nova:text:destroy": "_text",
        "nova:text:release_deposit": "_text",
        "nova:text:complain": "_text", "nova:text:vote": "_text",
        "nova:ai:register": "_ai", "nova:ai:config": "_ai",
    }

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
        fn = getattr(self, f"{kind}_apply")
        fn(tx, d)

    @staticmethod
    def _parse_op(tx):
        try:
            d = json.loads(tx.data)
        except Exception:
            return None
        return d if isinstance(d, dict) else None

    def maintain(self):
        """每日维护：文本仲裁超时自动结算（无结论按卖家处理）。"""
        settled = 0
        for a in self.store.text_assets.values():
            if a.get("dispute") and not a["dispute"].get("settled"):
                before = a["dispute"].get("settled", False)
                self._text_settle(a)
                if a["dispute"].get("settled") and not before:
                    settled += 1
        return settled