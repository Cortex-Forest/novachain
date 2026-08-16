# -*- coding: utf-8 -*-
"""Nova 链去中心化身份（DID）与声誉系统。

设计（对应需求）：
- DID 注册：可选绑定 邮箱/Telegram/X/IPFS头像 的哈希（原始数据永不上链）；
  绑定由 Nova 私钥签名交易确认；可随时撤销。
- 创作者认证：提交作品集（本人部署的合约地址列表），社区投票通过后获得不可转让
  的"认证创作者"徽章（soulbound NFT）。
- 声誉分（满分 100，初始 50）：创作质量 30% + 社区贡献 25% + 资产稳定 25% +
  身份完整 20%（四维在 50 分基数上各贡献一半权重，合计满分 100）。
- 声誉用途：>80 享 20% 手续费折扣/预售优先/更高空投权重；<30 限制密文发布、
  提高投诉保证金（由其它模块读取 tier 决策）。
- 隐私：只存哈希；每个绑定可设可见性；声誉详情仅本人可见，公开显示总分。

与其它模块一致：signed tx（sender == receiver，data 为 JSON {op, ...}）。
"""
import json
import math
import re
import time

HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
CID_RE = re.compile(r"^(?:0x[0-9a-fA-F]{64}|bafy[a-z2-7]{46,58})$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
KINDS = ("email", "telegram", "x", "avatar")
PORTFOLIO_MAX = 20
APPLY_VOTES_MIN = 10            # 认证投票最低票数
APPLY_SUPPORT_RATIO = 0.5       # 赞成比例
VOTER_MIN_POWER = 100.0         # 投票者最低权益（防女巫）
BADGE_CREATOR = "nova:did:creator"
DID_BIND_FEE = 0.0


def _amt(v):
    return round(float(v), 8)


class DID:
    def __init__(self, store, economy):
        self.store = store
        self.economy = economy

    # ------------------------------------------------------------------
    # 声誉分计算（确定性，仅依赖链上状态）
    # ------------------------------------------------------------------
    def recompute(self, addr):
        st = self.store
        quality = 0.0
        sold = sum(1 for a in st.text_assets.values()
                   if a.get("author") == addr and a.get("sold", 0) > 0)
        quality += min(sold * 1.5, 6.0)
        held = sum(t.get("holders", {}).get(addr, 0) for t in st.fan_tokens.values())
        quality += min(held / 200.0, 3.0)
        likes = sum(len(p.get("likes", [])) for p in st.graph_posts.values()
                    if p.get("addr") == addr)
        quality += min(likes * 0.1, 3.0)
        ai_works = sum(1 for w in st.ai_works.values()
                       if w.get("creator") == addr or w.get("owner") == addr)
        quality += min(ai_works * 0.5, 3.0)

        community = 0.0
        community += min(st.light_checkins.get(addr, 0) / 270.0 * 8.0, 8.0)
        refs = sum(1 for r in st.referrals.values() if r == addr)
        community += min(refs * 0.5, 2.0)
        arb_ok = sum(1 for c in st.arb_cases.values()
                     if c.get("settled") and addr in c.get("panel", [])
                     and c.get("winner") == addr)
        community += min(arb_ok * 0.5, 2.5)

        asset = 0.0
        staked = float(st.stakes.get(addr, 0.0))
        asset += min(staked / 10000.0 * 6.0, 6.0)
        bal = float(st.balances.get(addr, 0.0))
        asset += min(bal / 10000.0 * 4.0, 4.0)
        txs = sum(1 for t in st.tx_history.values()
                  if t.get("sender") == addr or t.get("receiver") == addr)
        asset += min(txs / 50.0, 2.5)

        identity = 0.0
        prof = st.did_profiles.get(addr)
        bindings = len(prof.get("bindings", {})) if prof else 0
        identity += min(bindings * 2.0, 8.0)
        identity += 2.0 if addr in st.did_badges and BADGE_CREATOR in st.did_badges.get(addr, []) else 0.0

        penalty = 0.0
        if addr in st.jailed:
            penalty += 10.0
        if addr in st.arb_banned:
            penalty += 15.0
        if addr in st.arb_malicious:
            penalty += 8.0
        if addr in st.oracle_nodes and st.oracle_nodes[addr].get("status") == "slashed":
            penalty += 10.0
        if addr in st.bridge_nodes and st.bridge_nodes[addr].get("status") == "slashed":
            penalty += 10.0

        score = round(max(0.0, min(100.0, 50.0 + quality + community + asset + identity - penalty)), 2)
        tier = "high" if score > 80 else ("mid" if score >= 50 else "low")
        grade = "S" if score > 80 else ("A" if score >= 50 else "C")
        details = {
            "quality": _amt(quality), "community": _amt(community),
            "asset": _amt(asset), "identity": _amt(identity), "penalty": _amt(penalty),
        }
        entry = {
            "addr": addr, "score": score, "tier": tier, "grade": grade,
            "base": 50.0, "components": details, "updated_at": time.time(),
        }
        st.did_reputation[addr] = entry
        return entry

    def reputation(self, addr, viewer=None):
        """声誉查询：viewer 非本人时只返回公开信息（总分/等级/用途）。"""
        entry = self.store.did_reputation.get(addr) or self.recompute(addr)
        public = {
            "addr": addr, "score": entry["score"], "tier": entry["tier"],
            "grade": entry["grade"],
            "perks": self.perks(addr),
        }
        if viewer == addr:
            public["base"] = entry.get("base", 50.0)
            public["components"] = entry.get("components", {})
            public["updated_at"] = entry.get("updated_at")
        return public

    def perks(self, addr):
        score = self.store.did_reputation.get(addr, {}).get("score")
        if score is None:
            score = self.recompute(addr)["score"]
        if score > 80:
            return {"level": "high",
                    "benefits": ["交易手续费降低 20%", "优先参与预售", "更高空投权重"]}
        if score >= 50:
            return {"level": "mid", "benefits": ["标准权限"]}
        return {"level": "low",
                "benefits": ["限制密文交易发布量", "提高投诉保证金"]}

    def profile(self, addr, viewer=None):
        prof = self.store.did_profiles.get(addr)
        if not prof:
            return None
        out = {"addr": addr, "bindings": {}, "created_at": prof.get("created_at")}
        for kind, b in prof.get("bindings", {}).items():
            if viewer == addr or b.get("visible", True):
                out["bindings"][kind] = {"hash": b["hash"][:16] + "...",
                                         "visible": b.get("visible", True),
                                         "bound_at": b.get("bound_at")}
        out["creator_badge"] = addr in self.store.did_badges and BADGE_CREATOR in self.store.did_badges.get(addr, [])
        out["reputation"] = self.reputation(addr, viewer)
        return out

    def summary(self):
        return {
            "profiles": len(self.store.did_profiles),
            "applications": len(self.store.did_applications),
            "creator_badges": sum(1 for v in self.store.did_badges.values() if BADGE_CREATOR in v),
            "avg_score": _amt(sum(r.get("score", 0.0) for r in self.store.did_reputation.values())
                              / max(len(self.store.did_reputation), 1)),
            "events": len(self.store.did_events),
        }

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _record(self, tx, op, target, msg, extra=None):
        self.store.did_event_seq += 1
        ev = {"seq": self.store.did_event_seq, "op": op, "addr": tx.sender,
              "target": target, "msg": msg, "ts": time.time()}
        if extra:
            ev.update(extra)
        self.store.did_events[tx.txid] = ev

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------
    OPS = {
        "nova:did:bind": "_bind",
        "nova:did:unbind": "_bind",
        "nova:did:apply": "_apply",
        "nova:did:vote": "_vote",
        "nova:did:update": "_update",
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

    # ---------------- 绑定 / 撤销 ----------------
    def _bind_validate(self, d, tx):
        op = d.get("op")
        kind = d.get("kind", "")
        if kind not in KINDS:
            return False
        if op == "nova:did:bind":
            if tx.amount != 0:
                return False
            h = d.get("hash", "")
            if kind == "avatar":
                if not (CID_RE.match(h) or HEX64_RE.match(h)):
                    return False
            elif not HEX64_RE.match(h):
                return False
            prof = self.store.did_profiles.setdefault(tx.sender, {
                "addr": tx.sender, "bindings": {}, "created_at": time.time()})
            return kind not in prof["bindings"]
        if op == "nova:did:unbind":
            if tx.amount != 0:
                return False
            prof = self.store.did_profiles.get(tx.sender)
            return prof is not None and kind in prof.get("bindings", {})
        return False

    def _bind_apply(self, tx, d):
        op = d["op"]
        kind = d["kind"]
        prof = self.store.did_profiles.setdefault(tx.sender, {
            "addr": tx.sender, "bindings": {}, "created_at": time.time()})
        if op == "nova:did:bind":
            prof["bindings"][kind] = {
                "hash": d["hash"].lower(), "visible": bool(d.get("visible", True)),
                "bound_at": time.time(),
            }
            self._record(tx, op, tx.sender, f"DID 绑定 {kind}（仅存哈希）")
        else:
            del prof["bindings"][kind]
            self._record(tx, op, tx.sender, f"DID 撤销绑定 {kind}")
        self.recompute(tx.sender)

    # ---------------- 创作者认证 ----------------
    def _apply_validate(self, d, tx):
        if tx.amount != 0:
            return False
        if tx.sender in self.store.did_applications:
            return False  # 已有进行中的申请
        if self.store.did_badges.get(tx.sender) and BADGE_CREATOR in self.store.did_badges[tx.sender]:
            return False  # 已认证
        portfolio = d.get("portfolio", [])
        if not isinstance(portfolio, list) or not portfolio or len(portfolio) > PORTFOLIO_MAX:
            return False
        for addr in portfolio:
            if not ADDRESS_RE.match(addr):
                return False
        # 至少一个作品集合约是本人部署的（链上真实作品证明）
        owned = any(self.store.contract_creator.get(a) == tx.sender for a in portfolio)
        return owned or any(a.get("author") == tx.sender for a in self.store.text_assets.values())

    def _apply_apply(self, tx, d):
        self.store.did_application_seq += 1
        aid = f"did-{self.store.did_application_seq:06d}"
        self.store.did_applications[tx.sender] = {
            "application_id": aid, "applicant": tx.sender,
            "portfolio": d.get("portfolio", []), "statement": d.get("statement", ""),
            "votes": {"for": 0, "against": 0}, "voters": {},
            "status": "pending", "created_at": time.time(),
        }
        self._record(tx, d["op"], aid, "创作者认证申请已提交")

    def _vote_validate(self, d, tx):
        if tx.amount != 0:
            return False
        app = self.store.did_applications.get(d.get("applicant", ""))
        if not app or app.get("status") != "pending":
            return False
        if tx.sender == app["applicant"] or tx.sender in app.get("voters", {}):
            return False
        power = float(self.store.balances.get(tx.sender, 0.0)) \
            + float(self.store.stakes.get(tx.sender, 0.0))
        if power < VOTER_MIN_POWER:
            return False
        return isinstance(d.get("support"), bool)

    def _vote_apply(self, tx, d):
        app = self.store.did_applications[d["applicant"]]
        key = "for" if d["support"] else "against"
        app["votes"][key] += 1
        app["voters"][tx.sender] = {"support": d["support"], "ts": time.time()}
        self._record(tx, d["op"], app["application_id"],
                     f"认证投票：{'赞成' if d['support'] else '反对'}（{app['votes']['for']} 赞 / {app['votes']['against']} 反）")
        self._resolve_application(app, tx)

    def _resolve_application(self, app, tx):
        total = app["votes"]["for"] + app["votes"]["against"]
        if total < APPLY_VOTES_MIN:
            return
        if app["votes"]["for"] > total * APPLY_SUPPORT_RATIO:
            app["status"] = "approved"
            self.store.did_badges.setdefault(app["applicant"], []).append(BADGE_CREATOR)
            # 不可转让徽章：同步登记到 soulbound
            self.store.soulbound.setdefault(BADGE_CREATOR, []).append(app["applicant"])
            self.recompute(app["applicant"])
            self._record(tx, "nova:did:approve", app["applicant"],
                         f"创作者认证通过，授予不可转让徽章（{app['votes']['for']} 赞成）")
        else:
            app["status"] = "rejected"
            self._record(tx, "nova:did:reject", app["applicant"], "创作者认证未通过")

    # ---------------- 声誉更新 ----------------
    def _update_validate(self, d, tx):
        return tx.amount == 0

    def _update_apply(self, tx, d):
        self.recompute(tx.sender)
        self._record(tx, d["op"], tx.sender, "声誉分已重新计算")

    def maintain(self):
        # 结算到期未达最低票数的申请（避免长期悬挂）
        n = 0
        for app in self.store.did_applications.values():
            if app.get("status") != "pending":
                continue
            if time.time() - app.get("created_at", 0) > 14 * 86400 \
                    and app["votes"]["for"] + app["votes"]["against"] < APPLY_VOTES_MIN:
                app["status"] = "expired"
                n += 1
        return n
