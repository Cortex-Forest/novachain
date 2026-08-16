# -*- coding: utf-8 -*-
"""Nova 链创作者订阅与会员系统。

设计（对应需求）：
- 订阅模式：按月订阅（30 天）、永久会员（一次支付永久）、分档订阅（创作者设置
  多个档位，不同价格对应不同权益）。
- 自动续费：订阅时可开启，到期后由任意 keeper 提交 renew 交易确定性续费；
  余额不足自动取消订阅；用户可随时手动取消（到期后不再续）。
- 订阅权益：专属内容解锁（has_access 供内容模块/前端校验）、订阅者专属徽章
  （不可转让 soulbound NFT）、新作品上线前 24 小时优先购买权、私密社区访问权。
- 收益分配：订阅收入 90% 归创作者、10% 归生态基金；Gas 费由节点统一回流激励池。

与其它模块一致：signed tx（sender == receiver，data 为 JSON {op, ...}）。
"""
import json
import math
import re
import time

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TIER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
TIER_MAX = 8
TIER_MIN_PRICE = 0.01
TIER_MAX_PRICE = 1_000_000.0
MONTH_DAYS = 30
CREATOR_SHARE = 0.9
ECOSYSTEM_SHARE = 0.1
EARLY_ACCESS_HOURS = 24
SUB_BADGE_PREFIX = "nova:sub:"


def _amt(v):
    return round(float(v), 8)


class Subscription:
    def __init__(self, store, economy):
        self.store = store
        self.economy = economy

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def creator(self, addr):
        return self.store.sub_creators.get(addr)

    def subscription(self, user, creator):
        return self.store.sub_subscriptions.get(f"{user}|{creator}")

    def is_active(self, user, creator):
        """是否享有专属内容访问权（有效月付 + 永久 + 未到期）。"""
        sub = self.subscription(user, creator)
        if not sub or sub.get("status") != "active":
            return False
        if sub.get("period") == "lifetime":
            return True
        return time.time() < sub.get("expires_at", 0)

    def has_early_access(self, user, creator):
        """新作品上线前 24 小时优先购买权。"""
        return self.is_active(user, creator)

    def summary(self):
        return {
            "creators": len(self.store.sub_creators),
            "subscriptions": len(self.store.sub_subscriptions),
            "active": sum(1 for s in self.store.sub_subscriptions.values()
                          if s.get("status") == "active" and
                          (s.get("period") == "lifetime" or time.time() < s.get("expires_at", 0))),
            "events": len(self.store.sub_events),
        }

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _record(self, tx, op, target, msg, extra=None):
        self.store.sub_event_seq += 1
        ev = {"seq": self.store.sub_event_seq, "op": op, "addr": tx.sender,
              "target": target, "msg": msg, "ts": time.time()}
        if extra:
            ev.update(extra)
        self.store.sub_events[tx.txid] = ev

    def _badge(self, user, creator):
        badge = SUB_BADGE_PREFIX + creator.lower()
        self.store.soulbound.setdefault(badge, [])
        if user not in self.store.soulbound[badge]:
            self.store.soulbound[badge].append(user)

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------
    OPS = {
        "nova:sub:create": "_create",
        "nova:sub:subscribe": "_subscribe",
        "nova:sub:renew": "_renew",
        "nova:sub:cancel": "_cancel",
        "nova:sub:update": "_create",
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

    # ---------------- 档位设置 ----------------
    def _create_validate(self, d, tx):
        op = d.get("op")
        if tx.amount != 0:
            return False
        tiers = d.get("tiers")
        if not isinstance(tiers, list) or not tiers or len(tiers) > TIER_MAX:
            return False
        seen = set()
        for t in tiers:
            if not isinstance(t, dict):
                return False
            tid = t.get("id", "")
            if not TIER_ID_RE.match(tid) or tid in seen:
                return False
            seen.add(tid)
            price = t.get("price")
            if not isinstance(price, (int, float)) or isinstance(price, bool):
                return False
            if not math.isfinite(price) or not (TIER_MIN_PRICE <= price <= TIER_MAX_PRICE):
                return False
            if t.get("period") not in ("monthly", "lifetime"):
                return False
            if t.get("benefits") is not None and not isinstance(t.get("benefits"), list):
                return False
        return True

    def _create_apply(self, tx, d):
        op = d["op"]
        tiers = {}
        for t in d["tiers"]:
            tiers[t["id"]] = {
                "id": t["id"], "name": t.get("name", t["id"]),
                "price": float(t["price"]), "period": t["period"],
                "benefits": t.get("benefits", []), "active": True,
            }
        if op == "nova:sub:update" and tx.sender in self.store.sub_creators:
            cur = self.store.sub_creators[tx.sender]
            cur["tiers"] = tiers
            cur["updated_at"] = time.time()
            msg = "订阅档位已更新"
        else:
            self.store.sub_creators[tx.sender] = {
                "addr": tx.sender, "tiers": tiers, "created_at": time.time(),
                "subscribers": 0, "revenue": 0.0, "updated_at": time.time(),
            }
            msg = "订阅档位已创建"
        self._record(tx, op, tx.sender, msg)

    # ---------------- 订阅 ----------------
    def _subscribe_validate(self, d, tx):
        creator = d.get("creator", "")
        tier_id = d.get("tier_id", "")
        if not ADDRESS_RE.match(creator) or creator == tx.sender:
            return False
        tiers = self.store.sub_creators.get(creator, {}).get("tiers", {})
        tier = tiers.get(tier_id)
        if not tier or not tier.get("active"):
            return False
        if tx.amount != tier["price"]:
            return False
        if self.store.balances.get(tx.sender, 0.0) < tx.amount + 1e-9:
            return False
        if self.subscription(tx.sender, creator) is not None:
            return False  # 已有订阅，续费走 renew
        return True

    def _subscribe_apply(self, tx, d):
        creator = d["creator"]
        tier = self.store.sub_creators[creator]["tiers"][d["tier_id"]]
        price = tx.amount
        creator_amt = _amt(price * CREATOR_SHARE)
        eco_amt = _amt(price * ECOSYSTEM_SHARE)
        # 90/10 分账（tx.amount 已在入口扣除）
        self.store.balances[creator] = self.store.balances.get(creator, 0.0) + creator_amt
        self.store.balances[self.economy.ECOSYSTEM_FUND] = \
            self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0) + eco_amt
        now = time.time()
        sub = {
            "user": tx.sender, "creator": creator, "tier_id": tier["id"],
            "tier_name": tier["name"], "period": tier["period"], "price": price,
            "auto_renew": bool(d.get("auto_renew", False)),
            "started_at": now,
            "expires_at": now + MONTH_DAYS * 86400 if tier["period"] == "monthly" else 0.0,
            "status": "active", "renewals": 0,
        }
        self.store.sub_subscriptions[f"{tx.sender}|{creator}"] = sub
        self.store.sub_creators[creator]["subscribers"] += 1
        self.store.sub_creators[creator]["revenue"] = _amt(
            self.store.sub_creators[creator]["revenue"] + creator_amt)
        self._badge(tx.sender, creator)
        self._record(tx, d["op"], creator,
                     f"订阅 {tier['name']}（{tier['period']}，{_amt(price)} NOVA），"
                     f"创作者 {_amt(creator_amt)} / 生态基金 {_amt(eco_amt)}")

    # ---------------- 自动续费 ----------------
    def _renew_validate(self, d, tx):
        if tx.amount != 0:
            return False
        creator = d.get("creator", "")
        user = d.get("user", tx.sender)
        if not ADDRESS_RE.match(creator):
            return False
        sub = self.subscription(user, creator)
        if not sub or sub.get("status") != "active" or sub.get("period") != "monthly":
            return False
        if not sub.get("auto_renew"):
            return False
        if time.time() < sub.get("expires_at", 0):
            return False  # 未到期无需续
        return True

    def _renew_apply(self, tx, d):
        creator = d["creator"]
        user = d.get("user", tx.sender)
        sub = self.store.sub_subscriptions[f"{user}|{creator}"]
        price = sub["price"]
        balance = self.store.balances.get(user, 0.0)
        if balance < price + 1e-9:
            # 余额不足自动取消订阅
            sub["status"] = "cancelled"
            sub["cancel_reason"] = "余额不足，自动取消"
            self.store.sub_creators[creator]["subscribers"] = max(
                0, self.store.sub_creators[creator]["subscribers"] - 1)
            self._record(tx, d["op"], creator, f"订阅者 {user[:10]}... 余额不足，订阅自动取消")
            return
        self.store.balances[user] = _amt(balance - price)
        creator_amt = _amt(price * CREATOR_SHARE)
        eco_amt = _amt(price * ECOSYSTEM_SHARE)
        self.store.balances[creator] = self.store.balances.get(creator, 0.0) + creator_amt
        self.store.balances[self.economy.ECOSYSTEM_FUND] = \
            self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0) + eco_amt
        sub["expires_at"] = time.time() + MONTH_DAYS * 86400
        sub["renewals"] += 1
        self.store.sub_creators[creator]["revenue"] = _amt(
            self.store.sub_creators[creator]["revenue"] + creator_amt)
        self._record(tx, d["op"], creator,
                     f"自动续费成功（第 {sub['renewals']} 次），"
                     f"创作者 {_amt(creator_amt)} / 生态基金 {_amt(eco_amt)}")

    # ---------------- 取消 ----------------
    def _cancel_validate(self, d, tx):
        if tx.amount != 0:
            return False
        creator = d.get("creator", "")
        if not ADDRESS_RE.match(creator):
            return False
        sub = self.subscription(tx.sender, creator)
        if not sub or sub.get("status") != "active":
            return False
        if sub.get("period") == "lifetime":
            return False  # 永久会员不可取消（仍享有权益）
        return True

    def _cancel_apply(self, tx, d):
        creator = d["creator"]
        sub = self.store.sub_subscriptions[f"{tx.sender}|{creator}"]
        sub["auto_renew"] = False
        sub["cancelled_at"] = time.time()
        # 到期即失效；取消后仍享用到期日
        self._record(tx, d["op"], creator, "已取消自动续费，订阅将在当前周期到期后结束")

    def maintain(self):
        """被动到期处理：月付到期且未续费 -> 状态过期。"""
        n = 0
        now = time.time()
        for key, sub in self.store.sub_subscriptions.items():
            if sub.get("status") != "active" or sub.get("period") != "monthly":
                continue
            if sub.get("expires_at", 0) < now and not sub.get("auto_renew"):
                sub["status"] = "expired"
                c = self.store.sub_creators.get(sub["creator"])
                if c:
                    c["subscribers"] = max(0, c["subscribers"] - 1)
                n += 1
        return n
