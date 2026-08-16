# -*- coding: utf-8 -*-
"""Nova 链上治理：提案 / 投票 / 委托 / 时间锁。

设计（对应需求）：
- 治理范围：经济参数（手续费率/出块奖励/质押门槛/减半周期）、基金支出、协议升级、
  仲裁参数。
- 投票权：1 NOVA = 1 票；余额 + 质押 + 锁仓均可投票（质押者权益不稀释）；可委托。
- 提案流程：持有 >=1000 NOVA 或社区联署 100 人 -> 公示期 3 天 -> 投票期 7 天 ->
  通过条件：赞成 > 反对 且 投票率 >= 流通量 10% -> 时间锁 48 小时 -> 执行。
- 提案类型：参数调整（自动执行）、基金支出（需 3/5 桥节点多签确认）、协议升级
  （2/3 绝对多数）。
- 状态迁移由 tick() 按时间戳确定性推进（任何治理操作或每日维护触发）。

与其它模块一致：signed tx（sender == receiver，data 为 JSON {op, ...}）。
"""
import json
import math
import re
import time

HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

MIN_PROPOSER_POWER = 1000.0     # 发起提案最低持有/质押/锁仓权益
MIN_ENDORSEMENTS = 100          # 社区联署人数
DISCUSSION_DAYS = 3             # 公示期
VOTING_DAYS = 7                 # 投票期
TIMELOCK_HOURS = 48             # 时间锁
QUORUM_RATIO = 0.10             # 投票率达到流通量 10%
UPGRADE_SUPERMAJORITY = 2 / 3   # 协议升级 2/3 绝对多数
TYPES = ("param", "fund", "upgrade", "arb")
PARAM_TARGETS = ("economy", "dex", "bridge", "arbitration")
ECONOMY_PARAMS = ("FIXED_GAS", "INIT_REWARD", "MIN_STAKE", "MAX_STAKE", "HALVING",
                  "MAX_TOTAL_STAKE", "MAX_UNBONDING_RATIO")


def _amt(v):
    return round(float(v), 8)


class Governance:
    def __init__(self, store, economy, bridge=None, dex=None):
        self.store = store
        self.economy = economy
        self.bridge = bridge
        self.dex = dex

    # ------------------------------------------------------------------
    # 投票权计算
    # ------------------------------------------------------------------
    def _locked_of(self, addr):
        lock = self.store.locked_balances.get(addr)
        return float(lock["amount"]) if lock else 0.0

    def voting_power(self, addr, _seen=None):
        """余额 + 质押 + 锁仓 + 所有委托给本地址的投票权（防循环）。"""
        _seen = _seen or set()
        if addr in _seen:
            return 0.0
        _seen.add(addr)
        power = (float(self.store.balances.get(addr, 0.0))
                 + float(self.store.stakes.get(addr, 0.0))
                 + self._locked_of(addr))
        for delegator, delegate in self.store.gov_delegations.items():
            if delegate == addr:
                power += self.voting_power(delegator, _seen)
        return power

    def circulating_supply(self):
        return sum(float(v) for v in self.store.balances.values()) or 1.0

    # ------------------------------------------------------------------
    # 提案状态流转（确定性，按时间戳推进）
    # ------------------------------------------------------------------
    def tick(self):
        now = time.time()
        moved = 0
        for p in self.store.gov_proposals.values():
            st = p.get("status")
            if st == "discussion" and now >= p.get("discussion_end", 0):
                if (p.get("proposer_ok") or len(self.store.gov_endorsements.get(p["id"], [])) >= MIN_ENDORSEMENTS):
                    p["status"] = "voting"
                    p["vote_start"] = now
                    p["vote_end"] = now + VOTING_DAYS * 86400
                else:
                    p["status"] = "rejected"
                    p["reject_reason"] = "公示期结束，联署不足 100 人且发起人权益不足 1000 NOVA"
                moved += 1
            elif st == "voting" and now >= p.get("vote_end", 0):
                self._resolve(p)
                moved += 1
        return moved

    def _resolve(self, p):
        total_votes = float(p.get("for_votes", 0.0)) + float(p.get("against_votes", 0.0))
        quorum = self.circulating_supply() * QUORUM_RATIO
        p["total_votes"] = _amt(total_votes)
        p["quorum"] = _amt(quorum)
        if p["ptype"] == "upgrade":
            ok = total_votes >= quorum and float(p.get("for_votes", 0.0)) >= total_votes * UPGRADE_SUPERMAJORITY
        else:
            ok = total_votes >= quorum and float(p.get("for_votes", 0.0)) > float(p.get("against_votes", 0.0))
        p["status"] = "passed" if ok else "rejected"
        p["resolved_at"] = time.time()
        if ok:
            p["timelock_end"] = time.time() + TIMELOCK_HOURS * 3600

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def proposal(self, pid):
        return self.store.gov_proposals.get(pid)

    def list_proposals(self, status=None, limit=100):
        items = sorted(self.store.gov_proposals.values(),
                       key=lambda p: p.get("created_at", 0), reverse=True)
        if status:
            items = [p for p in items if p.get("status") == status]
        return items[:limit]

    def summary(self):
        self.tick()
        return {
            "proposals": len(self.store.gov_proposals),
            "active": sum(1 for p in self.store.gov_proposals.values() if p.get("status") in ("discussion", "voting")),
            "passed": sum(1 for p in self.store.gov_proposals.values() if p.get("status") == "passed"),
            "executed": sum(1 for p in self.store.gov_proposals.values() if p.get("status") == "executed"),
            "delegations": len(self.store.gov_delegations),
            "circulating": _amt(self.circulating_supply()),
            "quorum": _amt(self.circulating_supply() * QUORUM_RATIO),
            "events": len(self.store.gov_events),
        }

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _record(self, tx, op, target, msg, extra=None):
        self.store.gov_event_seq += 1
        ev = {"seq": self.store.gov_event_seq, "op": op, "addr": tx.sender,
              "target": target, "msg": msg, "ts": time.time()}
        if extra:
            ev.update(extra)
        self.store.gov_events[tx.txid] = ev

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------
    OPS = {
        "nova:gov:propose": "_propose",
        "nova:gov:endorse": "_propose",
        "nova:gov:vote": "_vote",
        "nova:gov:delegate": "_vote",
        "nova:gov:confirm": "_confirm",
        "nova:gov:execute": "_execute",
        "nova:gov:cancel": "_execute",
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
        self.tick()
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

    # ---------------- 提案 / 联署 ----------------
    def _propose_validate(self, d, tx):
        op = d.get("op")
        if op == "nova:gov:propose":
            if tx.amount != 0:
                return False
            ptype = d.get("ptype", "")
            title = d.get("title", "")
            if ptype not in TYPES or not (1 <= len(title) <= 120):
                return False
            if ptype == "param":
                target = d.get("target", "")
                key = d.get("key", "")
                value = d.get("value")
                if target not in PARAM_TARGETS or not key:
                    return False
                if target == "economy" and key not in ECONOMY_PARAMS:
                    return False
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False
                if not math.isfinite(value) or value < 0:
                    return False
            elif ptype == "fund":
                recipient = d.get("recipient", "")
                amount = d.get("amount")
                if not ADDRESS_RE.match(recipient) or not isinstance(amount, (int, float)) \
                        or isinstance(amount, bool) or not math.isfinite(amount) or amount <= 0:
                    return False
            elif ptype == "upgrade":
                if not isinstance(d.get("upgrade_height"), (int, float)) or isinstance(d.get("upgrade_height"), bool):
                    return False
                if d.get("upgrade_height", 0) <= 0 or not (1 <= len(str(d.get("content", ""))) <= 2000):
                    return False
            elif ptype == "arb":
                key = d.get("key", "")
                value = d.get("value")
                if not key or not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False
            return True
        if op == "nova:gov:endorse":
            if tx.amount != 0:
                return False
            p = self.store.gov_proposals.get(d.get("proposal_id", ""))
            if not p or p.get("status") != "discussion":
                return False
            return tx.sender not in self.store.gov_endorsements.get(p["id"], []) \
                and tx.sender != p["proposer"]
        return False

    def _propose_apply(self, tx, d):
        op = d["op"]
        if op == "nova:gov:propose":
            self.store.gov_proposal_seq += 1
            pid = f"p-{self.store.gov_proposal_seq:06d}"
            power = self.voting_power(tx.sender)
            p = {
                "id": pid, "proposer": tx.sender, "title": d["title"],
                "description": d.get("description", ""), "ptype": d["ptype"],
                "status": "discussion", "created_at": time.time(),
                "discussion_end": time.time() + DISCUSSION_DAYS * 86400,
                "proposer_ok": power >= MIN_PROPOSER_POWER,
                "proposer_power": _amt(power),
                "for_votes": 0.0, "against_votes": 0.0, "voters": {},
                "multisig": [],
            }
            if d["ptype"] == "param":
                p.update({"target": d["target"], "key": d["key"], "value": d["value"]})
            elif d["ptype"] == "fund":
                p.update({"recipient": d["recipient"], "amount": d["amount"]})
            elif d["ptype"] == "upgrade":
                p.update({"upgrade_height": d["upgrade_height"], "content": d["content"]})
            else:
                p.update({"arb_key": d["key"], "arb_value": d["value"]})
            self.store.gov_proposals[pid] = p
            self._record(tx, op, pid, f"发起 {d['ptype']} 提案：{d['title']}",
                         {"proposer_ok": p["proposer_ok"]})
        elif op == "nova:gov:endorse":
            p = self.store.gov_proposals[d["proposal_id"]]
            self.store.gov_endorsements.setdefault(p["id"], []).append(tx.sender)
            self._record(tx, op, p["id"],
                         f"社区联署（{len(self.store.gov_endorsements[p['id']])}/{MIN_ENDORSEMENTS}）")

    # ---------------- 投票 / 委托 ----------------
    def _vote_validate(self, d, tx):
        op = d.get("op")
        if op == "nova:gov:vote":
            if tx.amount != 0:
                return False
            p = self.store.gov_proposals.get(d.get("proposal_id", ""))
            if not p or p.get("status") != "voting":
                return False
            if tx.sender in p.get("voters", {}):
                return False
            return isinstance(d.get("support"), bool)
        if op == "nova:gov:delegate":
            if tx.amount != 0:
                return False
            to = d.get("to", "")
            return ADDRESS_RE.match(to) and to != tx.sender
        return False

    def _vote_apply(self, tx, d):
        op = d["op"]
        if op == "nova:gov:vote":
            p = self.store.gov_proposals[d["proposal_id"]]
            power = self.voting_power(tx.sender)
            if power <= 0:
                return
            if d["support"]:
                p["for_votes"] = _amt(p["for_votes"] + power)
            else:
                p["against_votes"] = _amt(p["against_votes"] + power)
            p["voters"][tx.sender] = {"support": d["support"], "power": _amt(power)}
            self._record(tx, op, p["id"], f"{'赞成' if d['support'] else '反对'}（{_amt(power)} 票）")
        elif op == "nova:gov:delegate":
            self.store.gov_delegations[tx.sender] = d["to"]
            self._record(tx, op, d["to"], f"投票权委托给 {d['to'][:12]}...")

    # ---------------- 基金支出多签确认 ----------------
    def _confirm_validate(self, d, tx):
        if tx.amount != 0:
            return False
        if self.bridge is None or not self.bridge._is_node(tx.sender):
            return False
        p = self.store.gov_proposals.get(d.get("proposal_id", ""))
        if not p or p.get("ptype") != "fund" or p.get("status") != "passed":
            return False
        return tx.sender not in p.get("multisig", [])

    def _confirm_apply(self, tx, d):
        p = self.store.gov_proposals[d["proposal_id"]]
        p.setdefault("multisig", []).append(tx.sender)
        self._record(tx, d["op"], p["id"],
                     f"基金支出多签确认（{len(p['multisig'])}/3）")

    # ---------------- 执行 / 取消 ----------------
    def _execute_validate(self, d, tx):
        op = d.get("op")
        if op == "nova:gov:execute":
            if tx.amount != 0:
                return False
            p = self.store.gov_proposals.get(d.get("proposal_id", ""))
            if not p or p.get("status") != "passed":
                return False
            if time.time() < p.get("timelock_end", 0):
                return False  # 时间锁 48 小时未到
            if p.get("ptype") == "fund" and len(p.get("multisig", [])) < 3:
                return False  # 基金支出需多签确认
            return True
        if op == "nova:gov:cancel":
            if tx.amount != 0:
                return False
            p = self.store.gov_proposals.get(d.get("proposal_id", ""))
            if not p or p.get("status") != "discussion":
                return False
            return tx.sender == p["proposer"]
        return False

    def _execute_apply(self, tx, d):
        op = d["op"]
        if op == "nova:gov:cancel":
            p = self.store.gov_proposals[d["proposal_id"]]
            p["status"] = "cancelled"
            self._record(tx, op, p["id"], "提案已取消")
            return
        p = self.store.gov_proposals[d["proposal_id"]]
        result = self._apply_effects(p, tx)
        p["status"] = "executed"
        p["executed_at"] = time.time()
        p["executed_by"] = tx.sender
        self._record(tx, op, p["id"], f"提案已执行：{result}", {"result": result})

    def _apply_effects(self, p, tx):
        ptype = p["ptype"]
        if ptype == "param":
            target, key, value = p["target"], p["key"], p["value"]
            if target == "economy" and key in ECONOMY_PARAMS:
                setattr(self.economy.__class__, key, float(value))
                return f"经济参数 {key} 已调整为 {value}"
            if target == "dex" and self.dex is not None:
                if key == "paused":
                    self.dex.set_paused(bool(value))
                    return f"DEX 暂停开关已设为 {bool(value)}"
                if key == "farm_apr":
                    pair = p.get("pair_id", "")
                    if pair and self.dex.set_farm_apr(pair, float(value)):
                        return f"DEX {pair} 挖矿 APR 已调整为 {value}"
            if target == "bridge" and key == "daily_limit_usd":
                self.store.gov_params["bridge.daily_limit_usd"] = float(value)
                return f"跨链桥每日额度已调整为 {value} USD"
            if target == "arbitration":
                self.store.gov_params[f"arb.{key}"] = value
                return f"仲裁参数 {key} 已调整为 {value}"
            return f"参数 {target}.{key} 已记录（无执行器）"
        if ptype == "fund":
            recipient, amount = p["recipient"], float(p["amount"])
            fund = self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0)
            pay = min(amount, fund)
            if pay > 0:
                self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(fund - pay)
                self.store.balances[recipient] = self.store.balances.get(recipient, 0.0) + pay
            return f"生态基金支出 {_amt(pay)} NOVA -> {recipient[:12]}..."
        if ptype == "upgrade":
            self.store.gov_params["upgrade.height"] = p["upgrade_height"]
            self.store.gov_params["upgrade.content"] = p["content"]
            return f"协议升级已登记：高度 {p['upgrade_height']}"
        if ptype == "arb":
            return f"仲裁参数已登记：{p.get('arb_key')}={p.get('arb_value')}"
        return "noop"

    def maintain(self):
        return self.tick()
