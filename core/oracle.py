# -*- coding: utf-8 -*-
"""Nova 链预言机：VRF 随机数 / 多源价格聚合 / AI 生成结果验证。

设计（对应需求）：
- VRF：自建 ECVRF（P-256，RFC 9381 风格，纯 Python 实现），节点持有私钥、
  公开公钥；随机数上链前不可预测（私钥保密），上链后任何人可用公钥验证明文
  结果（proof: gamma/c/s）。盲盒、抽奖、AI 结果验证统一走此模块。
- 价格预言机：多数据源聚合（chainlink/pyth/binance/kucoin/gate），取中位数并
  剔除偏离中位数 >5% 的源；聚合价格每 5 分钟发布一次；供 DEX 与预售合约使用。
- AI 生成结果验证：AI 内容哈希经预言机节点验证通过后标记 verified，合约才允许
  上架销售；验证节点获得小额 NOVA 奖励（生态基金支付）。
- 节点经济：预言机节点质押 500 NOVA；价格偏离聚合价 >10% 拒绝、>25% 视为作恶
  罚没全部质押；VRF 结果与已存结果冲突同样罚没。

实现方式与存储/算力模块一致：signed tx（sender == receiver，data 为 JSON
{op, ...}），经区块广播后在所有节点确定性重放。
"""
import hashlib
import json
import math
import os
import re
import time

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
PUBKEY_RE = re.compile(r"^0x[0-9a-fA-F]{128}$")
CID_RE = re.compile(r"^(?:0x[0-9a-fA-F]{64}|bafy[a-z2-7]{46,58})$")

ORACLE_STAKE = 500.0            # 预言机节点质押（NOVA）
ORACLE_MAX_NODES = 21           # 节点上限
PRICE_MIN_INTERVAL = 300        # 聚合价格发布最短间隔（秒）= 5 分钟
PRICE_MAX_DEV_REJECT = 0.10     # 单源价格偏离聚合价 >10% 拒绝
PRICE_MAX_DEV_SLASH = 0.25      # 偏离 >25% 视为作恶，罚没
PRICE_SOURCES = ("chainlink", "pyth", "binance", "kucoin", "gate")
FEEDS = ("USDT/USD", "ETH/USD", "NOVA/USD")
DERIVED_FEEDS = {"USDT/ETH": ("USDT/USD", "ETH/USD"),
                 "NOVA/USDT": ("NOVA/USD", "USDT/USD")}
PRICE_MIN = 1e-9
PRICE_MAX = 1e12
VRF_FEE = 0.0                   # VRF 请求免费（仅 Gas）
AI_VERIFY_REWARD = 0.1          # 每次 AI 验证节点奖励（NOVA）
AI_VERIFY_DAILY_CAP = 5.0       # 节点单日验证奖励上限（NOVA）
AI_VERIFY_TTL = 7 * 86400       # 验证结果有效期（7 天）
ORACLE_UNBOND = 7 * 86400       # 退出节点质押冷却期

# ---------------------------------------------------------------------------
# ECVRF-P256（自建，确定性、可验证）
# ---------------------------------------------------------------------------
_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_A = 0xffffffff00000001000000000000000000000000fffffffffffffffffffffffc
_B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
_G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
      0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)
_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
_INF = None


def _fmod(v):
    return v % _P


def _inv(x):
    return pow(x % _P, _P - 2, _P)


def _neg(p):
    return None if p is _INF else (p[0], _fmod(-p[1]))


def _add(p1, p2):
    if p1 is _INF:
        return p2
    if p2 is _INF:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return _INF
        lam = _fmod((3 * x1 * x1 + _A) * _inv(2 * y1))
    else:
        lam = _fmod((y2 - y1) * _inv(x2 - x1))
    x3 = _fmod(lam * lam - x1 - x2)
    y3 = _fmod(lam * (x1 - x3) - y1)
    return (x3, y3)


def _mul(k, p):
    k = k % _N
    r = _INF
    while k > 0:
        if k & 1:
            r = _add(r, p)
        p = _add(p, p)
        k >>= 1
    return r


def _point_from_hex(pub_hex):
    h = pub_hex[2:] if pub_hex.startswith("0x") else pub_hex
    return (int(h[:64], 16), int(h[64:128], 16))


def _point_to_hex(p):
    return "0x" + f"{p[0]:064x}{p[1]:064x}"


def _h2p(alpha):
    """确定性 hash-to-curve（try-and-increment，P-256）。"""
    seed = hashlib.sha3_256(("nova:vrf:h2p:" + alpha).encode()).digest()
    for i in range(1024):
        x = int.from_bytes(hashlib.sha3_256(seed + bytes([i])).digest(), "big") % _P
        rhs = _fmod(pow(x, 3, _P) + _A * x + _B)
        y = pow(rhs, (_P + 1) // 4, _P)
        if _fmod(y * y) == rhs:
            return (x, y)
    raise ValueError("hash-to-point failed")


def _hnum(*parts):
    raw = "|".join(str(p) for p in parts)
    return int(hashlib.sha3_256(("nova:vrf:" + raw).encode()).hexdigest(), 16)


def vrf_keygen():
    x = int.from_bytes(os.urandom(32), "big") % (_N - 1) + 1
    return f"{x:064x}", _point_to_hex(_mul(x, _G))


def vrf_prove(priv_hex, alpha):
    """生成 VRF 证明：返回 {gamma, c, s}。"""
    x = int(priv_hex, 16)
    hp = _h2p(alpha)
    gamma = _mul(x, hp)
    k = _hnum(x, alpha, "nonce") % _N
    u = _mul(k, _G)
    v = _mul(k, hp)
    c = _hnum(_point_to_hex(u), _point_to_hex(v)) % _N
    s = (k + c * x) % _N
    return {"gamma": _point_to_hex(gamma), "c": f"{c:064x}", "s": f"{s:064x}"}


def vrf_verify(pub_hex, alpha, proof):
    """验证 VRF 证明（任何节点/任何人都可执行）。"""
    try:
        y = _point_from_hex(pub_hex)
        g = _point_from_hex(proof["gamma"])
        c = int(proof["c"], 16)
        s = int(proof["s"], 16)
        hp = _h2p(alpha)
        u = _add(_mul(s, _G), _neg(_mul(c, y)))
        v = _add(_mul(s, hp), _neg(_mul(c, g)))
        return _hnum(_point_to_hex(u), _point_to_hex(v)) % _N == c
    except Exception:
        return False


def vrf_output(alpha, gamma_hex):
    return hashlib.sha3_256(("nova:vrf:out:" + alpha + ":" + gamma_hex).encode()).hexdigest()


def _amt(v):
    return round(float(v), 8)


def _day(ts=None):
    # UTC 自然日（审计：统一 UTC，避免跨时区节点奖励窗口不一致）
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


class Oracle:
    """预言机状态全部保存在 self.store 上，随状态快照持久化与全网同步。"""

    def __init__(self, store, economy):
        self.store = store
        self.economy = economy

    # ------------------------------------------------------------------
    # 查询接口（RPC / SDK 使用，不改变状态）
    # ------------------------------------------------------------------
    def price(self, feed):
        """查询聚合价格；派生对（USDT/ETH、NOVA/USDT）自动换算。"""
        if feed in DERIVED_FEEDS:
            a, b = DERIVED_FEEDS[feed]
            pa = self.price(a)
            pb = self.price(b)
            if not pa or not pb:
                return None
            return _amt(pa["price"] / pb["price"])
        agg = self.store.oracle_feeds.get(feed)
        return agg

    def node(self, addr):
        return self.store.oracle_nodes.get(addr)

    def vrf_result(self, request_id):
        return self.store.oracle_requests.get(request_id)

    def ai_verification(self, content_hash):
        return self.store.oracle_ai_verifications.get(content_hash)

    def summary(self):
        return {
            "nodes": len(self.store.oracle_nodes),
            "total_stake": _amt(sum(n.get("stake", 0.0) for n in self.store.oracle_nodes.values())),
            "requests": len(self.store.oracle_requests),
            "feeds": {f: (dict(agg) if agg else None) for f, agg in self.store.oracle_feeds.items()},
            "ai_verified": sum(1 for v in self.store.oracle_ai_verifications.values() if v.get("status") == "verified"),
            "events": len(self.store.oracle_events),
            "slashed": _amt(self.store.oracle_slashed),
        }

    # ------------------------------------------------------------------
    # 事件记录
    # ------------------------------------------------------------------
    def _record(self, tx, op, target, msg, extra=None):
        self.store.oracle_event_seq += 1
        ev = {"seq": self.store.oracle_event_seq, "op": op, "addr": tx.sender,
              "target": target, "msg": msg, "ts": time.time()}
        if extra:
            ev.update(extra)
        self.store.oracle_events[tx.txid] = ev

    # ------------------------------------------------------------------
    # 价格聚合：多数据源 -> 中位数（剔除偏离 >5% 的源）
    # ------------------------------------------------------------------
    def aggregate(self, feed):
        """对 feed 的已上报源价格做中位数聚合（确定性）。"""
        sources = self.store.oracle_price_sources.get(feed, {})
        prices = {src: float(v["price"]) for src, v in sources.items()
                  if v.get("active", True)}
        if len(prices) < 2:
            return None
        # 审计 F-03：聚合至少需要 2 个独立节点维护的源（防单节点女巫多源）
        nodes = {v.get("node") for v in sources.values()
                 if v.get("active", True) and v.get("node")}
        if len(nodes) < 2:
            return None
        ordered = sorted(prices.values())
        median = ordered[len(ordered) // 2] if len(ordered) % 2 else \
            (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2.0
        valid = {src: p for src, p in prices.items()
                 if abs(p - median) / median <= 0.05}
        if len(valid) < 2:
            return None
        vals = sorted(valid.values())
        agg = (vals[len(vals) // 2] if len(vals) % 2
               else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2.0)
        return _amt(agg)

    def _commit_feed(self, feed):
        agg = self.aggregate(feed)
        if agg is None:
            return
        now = time.time()
        old = self.store.oracle_feeds.get(feed)
        if old and now - old.get("ts", 0) < PRICE_MIN_INTERVAL:
            return  # 每 5 分钟最多发布一次聚合价
        self.store.oracle_feeds[feed] = {
            "feed": feed, "price": agg, "ts": now,
            "sources": {src: _amt(p["price"]) for src, p in
                        self.store.oracle_price_sources.get(feed, {}).items()},
            "method": "median",
        }

    def _slash(self, addr, reason, tx):
        """作恶罚没：全额质押进入生态基金。"""
        node = self.store.oracle_nodes.get(addr)
        if not node:
            return False
        stake = float(node.get("stake", 0.0))
        if stake > 0:
            self.store.balances[self.economy.ECOSYSTEM_FUND] = \
                self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) + stake
            self.store.oracle_slashed = _amt(self.store.oracle_slashed + stake)
        node["status"] = "slashed"
        node["stake"] = 0.0
        node["slash_reason"] = reason
        node["slashed_at"] = time.time()
        self._record(tx, "nova:oracle:node:slash", addr, f"节点作恶罚没：{reason}")
        return True

    def _ai_reward(self, node_addr, tx):
        """验证节点小额 NOVA 奖励（生态基金支付，单日上限）。"""
        rwd = AI_VERIFY_REWARD
        key = f"{_day()}|{node_addr}"
        spent = float(self.store.oracle_ai_verifications.get("__reward_day", {}).get(key, 0.0))
        if spent + rwd > AI_VERIFY_DAILY_CAP:
            return 0.0
        if self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) < rwd:
            return 0.0
        self.store.balances[self.economy.ECOSYSTEM_FUND] -= rwd
        self.store.balances[node_addr] = self.store.balances.get(node_addr, 0) + rwd
        days = self.store.oracle_ai_verifications.setdefault("__reward_day", {})
        days[key] = _amt(spent + rwd)
        return rwd

    # ------------------------------------------------------------------
    # 校验与执行（统一入口）
    # ------------------------------------------------------------------
    OPS = {
        "nova:oracle:node:register": "_node",
        "nova:oracle:node:exit": "_node",
        "nova:oracle:node:claim": "_node",
        "nova:oracle:vrf:request": "_vrf",
        "nova:oracle:vrf:fulfill": "_vrf",
        "nova:oracle:price:update": "_price",
        "nova:oracle:report": "_report",
        "nova:oracle:ai:submit": "_ai",
        "nova:oracle:ai:verify": "_ai",
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

    # ---------------- 节点注册 / 退出 ----------------
    def _node_validate(self, d, tx):
        op = d.get("op")
        if op == "nova:oracle:node:register":
            pub = d.get("pubkey", "")
            return (tx.amount >= ORACLE_STAKE and tx.sender not in self.store.oracle_nodes
                    and len(self.store.oracle_nodes) < ORACLE_MAX_NODES
                    and bool(PUBKEY_RE.match(pub)))
        if op == "nova:oracle:node:exit":
            node = self.store.oracle_nodes.get(tx.sender)
            return (tx.amount == 0 and node is not None
                    and node.get("status") == "active"
                    and not self.store.oracle_nodes.get(tx.sender, {}).get("exiting"))
        if op == "nova:oracle:node:claim":
            return tx.amount == 0 and self.exit_claimable(tx.sender) > 0
        return False

    def _node_apply(self, tx, d):
        if d["op"] == "nova:oracle:node:register":
            self.store.oracle_nodes[tx.sender] = {
                "addr": tx.sender, "pubkey": d["pubkey"], "stake": tx.amount,
                "status": "active", "registered_at": time.time(),
                "fulfills": 0, "updates": 0, "ai_verified": 0,
            }
            self._record(tx, d["op"], tx.sender,
                         f"预言机节点注册，质押 {_amt(tx.amount)} NOVA")
        elif d["op"] == "nova:oracle:node:exit":
            self.store.oracle_nodes[tx.sender]["exiting"] = time.time() + ORACLE_UNBOND
            self._record(tx, d["op"], tx.sender, "预言机节点退出，进入 7 天冷却期")
        elif d["op"] == "nova:oracle:node:claim":
            amt = self.claim_exit(tx.sender, tx)

    def exit_claimable(self, addr):
        node = self.store.oracle_nodes.get(addr)
        if not node or node.get("status") != "active" or not node.get("exiting"):
            return 0.0
        if time.time() < node["exiting"]:
            return 0.0
        return float(node.get("stake", 0.0))

    def claim_exit(self, addr, tx):
        amt = self.exit_claimable(addr)
        if amt <= 0:
            return 0.0
        self.store.balances[addr] = self.store.balances.get(addr, 0) + amt
        node = self.store.oracle_nodes[addr]
        node["stake"] = 0.0
        node["status"] = "exited"
        self._record(tx, "nova:oracle:node:claim", addr, f"退出质押返还 {_amt(amt)} NOVA")
        return amt

    # ---------------- VRF 随机数 ----------------
    def _vrf_validate(self, d, tx):
        op = d.get("op")
        if op == "nova:oracle:vrf:request":
            if tx.amount != 0:
                return False
            active = sum(1 for r in self.store.oracle_requests.values()
                         if r.get("requester") == tx.sender and r.get("status") == "pending")
            return active < 3
        if op == "nova:oracle:vrf:fulfill":
            node = self.store.oracle_nodes.get(tx.sender)
            rid = d.get("request_id", "")
            req = self.store.oracle_requests.get(rid)
            if tx.amount != 0 or not node or node.get("status") != "active":
                return False
            if not req or req.get("status") != "pending":
                return False
            proof = d.get("proof")
            if not isinstance(proof, dict):
                return False
            alpha = self._vrf_alpha(rid)
            if not vrf_verify(node["pubkey"], alpha, proof):
                return False
            # 防双答案：同一节点对同一请求重复 fulfill 由 pending 状态拦截
            return True
        return False

    @staticmethod
    def _vrf_alpha(request_id):
        return "nova:vrf:" + request_id

    def _vrf_apply(self, tx, d):
        if d["op"] == "nova:oracle:vrf:request":
            self.store.oracle_request_seq += 1
            rid = f"{self.store.oracle_request_seq:016x}"
            self.store.oracle_requests[rid] = {
                "request_id": rid, "requester": tx.sender, "status": "pending",
                "created_at": time.time(), "hint": d.get("hint", ""),
            }
            self._record(tx, d["op"], rid, "VRF 随机数请求已受理")
        elif d["op"] == "nova:oracle:vrf:fulfill":
            rid = d["request_id"]
            req = self.store.oracle_requests[rid]
            alpha = self._vrf_alpha(rid)
            proof = d["proof"]
            node = self.store.oracle_nodes[tx.sender]
            rand = vrf_output(alpha, proof["gamma"])
            req.update({
                "status": "fulfilled", "random": rand,
                "node": tx.sender, "proof": proof, "alpha": alpha,
                "fulfilled_at": time.time(),
            })
            node["fulfills"] += 1
            self._record(tx, d["op"], rid, "VRF 随机数已上链（可验证）",
                         {"random": rand[:16] + "...", "node": tx.sender})

    # ---------------- 价格更新 ----------------
    def _price_validate(self, d, tx):
        node = self.store.oracle_nodes.get(tx.sender)
        if tx.amount != 0 or not node or node.get("status") != "active":
            return False
        feed = d.get("feed", "")
        source = d.get("source", "")
        price = d.get("price")
        if feed not in FEEDS or source not in PRICE_SOURCES:
            return False
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            return False
        if not math.isfinite(price) or not (PRICE_MIN <= price <= PRICE_MAX):
            return False
        # 审计 F-03：源归属绑定 + 单节点单源约束（防女巫多源操纵聚合价）
        sources = self.store.oracle_price_sources.get(feed, {})
        for src, v in sources.items():
            if not v.get("active", True):
                continue
            owner = v.get("node")
            if src == source:
                if owner and owner != tx.sender:
                    return False          # 源已被其他节点绑定，不得接管
            elif owner == tx.sender:
                return False              # 同一节点在同一 feed 只能维护一个源
        agg = self.store.oracle_feeds.get(feed)
        if agg:
            dev = abs(price - agg["price"]) / agg["price"]
            # 偏离聚合价 >10% 拒绝（含 >25% 的疑似作恶，由举报流程罚没，见 report）
            if dev > PRICE_MAX_DEV_REJECT:
                return False
        return True

    def _price_apply(self, tx, d):
        feed = d["feed"]
        source = d["source"]
        sources = self.store.oracle_price_sources.setdefault(feed, {})
        sources[source] = {"price": float(d["price"]), "updated_at": time.time(),
                           "node": tx.sender, "active": True}
        node = self.store.oracle_nodes[tx.sender]
        node["updates"] += 1
        self._commit_feed(feed)
        self._record(tx, d["op"], feed,
                     f"{source} 上报 {feed} = {d['price']}，多源聚合已更新")

    # ---------------- 作恶举报与罚没 ----------------
    def _report_validate(self, d, tx):
        node = self.store.oracle_nodes.get(tx.sender)
        if tx.amount != 0 or not node or node.get("status") != "active":
            return False
        target = d.get("target", "")
        feed = d.get("feed", "")
        tnode = self.store.oracle_nodes.get(target)
        if not tnode or tnode.get("status") != "active" or feed not in FEEDS:
            return False
        if target == tx.sender:
            return False
        sources = self.store.oracle_price_sources.get(feed, {})
        for src, v in sources.items():
            if v.get("node") == target and v.get("active", True):
                agg = self.store.oracle_feeds.get(feed)
                if not agg:
                    return False
                dev = abs(float(v["price"]) - agg["price"]) / agg["price"]
                return dev > PRICE_MAX_DEV_SLASH
        return False

    def _report_apply(self, tx, d):
        target = d["target"]
        feed = d["feed"]
        agg = self.store.oracle_feeds.get(feed, {})
        for src, v in self.store.oracle_price_sources.get(feed, {}).items():
            if v.get("node") == target and v.get("active", True):
                dev = abs(float(v["price"]) - agg.get("price", 1.0)) / max(agg.get("price", 1.0), 1e-12)
                if dev > PRICE_MAX_DEV_SLASH:
                    v["active"] = False
                    self._slash(target, f"价格严重偏离 {feed}（{dev*100:.1f}%）", tx)
                    return

    # ---------------- AI 生成结果验证 ----------------
    def _ai_validate(self, d, tx):
        op = d.get("op")
        content_hash = d.get("content_hash", "")
        if not (HEX64_RE.match(content_hash) or CID_RE.match(content_hash)):
            return False
        if op == "nova:oracle:ai:submit":
            if tx.amount != 0:
                return False
            cur = self.store.oracle_ai_verifications.get(content_hash)
            return cur is None or cur.get("status") == "rejected"
        if op == "nova:oracle:ai:verify":
            node = self.store.oracle_nodes.get(tx.sender)
            if tx.amount != 0 or not node or node.get("status") != "active":
                return False
            cur = self.store.oracle_ai_verifications.get(content_hash)
            if not cur or cur.get("status") not in ("pending", "rejected"):
                return False
            return isinstance(d.get("verdict"), bool)
        return False

    def _ai_apply(self, tx, d):
        content_hash = d["content_hash"]
        if d["op"] == "nova:oracle:ai:submit":
            self.store.oracle_ai_verifications[content_hash] = {
                "content_hash": content_hash, "status": "pending",
                "creator": tx.sender, "meta": d.get("meta", {}),
                "submitted_at": time.time(), "verified_by": None,
                "verdict": None, "verified_at": None,
            }
            self._record(tx, d["op"], content_hash, "AI 生成结果已提交待验证")
        elif d["op"] == "nova:oracle:ai:verify":
            cur = self.store.oracle_ai_verifications[content_hash]
            cur["status"] = "verified" if d["verdict"] else "rejected"
            cur["verified_by"] = tx.sender
            cur["verdict"] = d["verdict"]
            cur["verified_at"] = time.time()
            node = self.store.oracle_nodes[tx.sender]
            node["ai_verified"] += 1
            rwd = self._ai_reward(tx.sender, tx) if d["verdict"] else 0.0
            self._record(tx, d["op"], content_hash,
                         f"AI 结果{'验证通过' if d['verdict'] else '未通过'}",
                         {"reward": rwd, "node": tx.sender})

    def maintain(self):
        """过期 AI 验证结果标记过期（保留记录，状态改为 expired）。"""
        n = 0
        now = time.time()
        for v in self.store.oracle_ai_verifications.values():
            if isinstance(v, dict) and v.get("status") == "verified" and v.get("verified_at"):
                if now - v["verified_at"] > AI_VERIFY_TTL:
                    v["status"] = "expired"
                    n += 1
        return n
