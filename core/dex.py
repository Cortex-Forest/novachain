# -*- coding: utf-8 -*-
"""Nova 链去中心化交易所（DEX）：AMM 恒定乘积 + LP 代币 + 流动性挖矿。

设计（对应需求）：
- 交易对：NOVA/USDT（nUSDT）、NOVA/nETH；恒定乘积 x*y=k。
- 手续费 0.3%：0.25% 留给流动性提供者（池内留存），0.05% 回购销毁（通缩）。
- 滑点保护：交易必须指定 min_out（默认最大滑点 5%），超限自动取消；
  split_quote 提供大额分拆建议，减少价格冲击。
- 流动性挖矿：LP 代币可质押进 farm 池获得 NOVA 奖励；初期 APR 20-50% 由治理调整。
- 安全：紧急暂停开关（由治理模块执行 set_paused）；初始流动性由预售资金提供
  （node 启动时 bootstrap，幂等）。

与其它模块一致：signed tx（sender == receiver，data 为 JSON {op, ...}）。
"""
import json
import math
import re
import time

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

FEE_RATE = 0.003            # 交易总手续费 0.3%
LP_FEE_RATE = 0.0025        # 0.25% 归流动性提供者
BUYBACK_RATE = 0.0005       # 0.05% 回购销毁（通缩）
MINIMUM_LIQUIDITY = 1000    # 首次注入锁定的最小流动性（shares）
MIN_SHARES = 1e-9
MAX_SLIPPAGE = 0.05         # 默认最大滑点 5%
FARM_APR_DEFAULT = 0.35     # 挖矿年化默认 35%（20-50% 区间由治理调整）
FARM_APR_MIN = 0.20
FARM_APR_MAX = 0.50
FARM_REWARD_DAY = 0         # 奖励日账本（预留：按日结算）
MAX_RESERVE = 1e12
PAIR_ID_RE = re.compile(r"^NOVA/[A-Z0-9]{2,10}$")
# 交易对显示名 -> 链上包装资产（DEX 交易的是桥接包装资产）
TOKEN_WRAP = {"USDT": "nUSDT", "nUSDT": "nUSDT", "ETH": "nETH", "nETH": "nETH"}


def _amt(v):
    return round(float(v), 8)


class Dex:
    def __init__(self, store, economy, bridge=None):
        self.store = store
        self.economy = economy
        self.bridge = bridge

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def pair(self, pair_id):
        return self.store.dex_pairs.get(pair_id)

    def _reserve_holder(self, pair_id):
        return f"0x_dex:{pair_id}"

    def _token1_balance(self, pair_id, holder):
        p = self.store.dex_pairs[pair_id]
        a = self.store.bridge_assets.get(p["token1"])
        if not a:
            return 0.0
        return float(a.get("balances", {}).get(holder, 0.0))

    def _lp_of(self, pair_id, addr):
        return float(self.store.dex_lp.get(f"{pair_id}|{addr}", {}).get("shares", 0.0))

    def lp_position(self, addr, pair_id):
        return {"pair": pair_id, "shares": _amt(self._lp_of(pair_id, addr)),
                "total_shares": _amt(self.store.dex_pairs.get(pair_id, {}).get("total_shares", 0.0))}

    def quote(self, pair_id, amount_in, token_in):
        """无滑点校验的报价：返回 {amount_out, price_impact}。"""
        p = self.store.dex_pairs.get(pair_id)
        if not p or p.get("paused"):
            return None
        amount_in = float(amount_in)
        if amount_in <= 0:
            return None
        if token_in == 0:
            reserve_in, reserve_out = p["reserve0"], p["reserve1"]
        elif token_in == 1:
            reserve_in, reserve_out = p["reserve1"], p["reserve0"]
        else:
            return None
        if reserve_in <= 0 or reserve_out <= 0:
            return None
        net = amount_in * (1 - FEE_RATE)
        amount_out = reserve_out * net / (reserve_in + net)
        impact = net / (reserve_in + net)
        return {"amount_out": _amt(amount_out), "price_impact": round(impact, 6),
                "price": _amt(amount_out / amount_in)}

    def split_quote(self, pair_id, amount_in, token_in, chunk=0.2):
        """大额交易分拆建议：每片不超过池子阻力的一定比例，减少价格冲击。"""
        p = self.store.dex_pairs.get(pair_id)
        if not p:
            return None
        reserve_in = p["reserve0"] if token_in == 0 else p["reserve1"]
        if reserve_in <= 0:
            return None
        n = max(1, math.ceil(amount_in / max(reserve_in * chunk, 1e-9)))
        pieces = [amount_in / n] * n
        total_out = 0.0
        for piece in pieces:
            q = self.quote(pair_id, piece, token_in)
            total_out += q["amount_out"] if q else 0.0
        return {"pieces": n, "per_piece": _amt(pieces[0]), "total_out": _amt(total_out),
                "note": "按片执行，每片单独设 min_out 以控制滑点"}

    def farm_pool(self, pair_id):
        return self.store.dex_farm.get(pair_id)

    def farm_user(self, pair_id, addr):
        pool = self.store.dex_farm.get(pair_id)
        if not pool:
            return None
        u = pool["users"].get(addr, {"shares": 0.0, "debt": 0.0, "pending": 0.0})
        return {"pair": pair_id, "staked": _amt(u["shares"]),
                "pending_reward": _amt(u["pending"] + self._farm_earned(pool, addr)),
                "apr": pool.get("apr", FARM_APR_DEFAULT)}

    def summary(self):
        return {
            "pairs": {pid: {"reserve0": p.get("reserve0", 0.0), "reserve1": p.get("reserve1", 0.0),
                            "total_shares": p.get("total_shares", 0.0), "paused": p.get("paused", False),
                            "burned0": p.get("burned0", 0.0), "burned1": p.get("burned1", 0.0)}
                     for pid, p in self.store.dex_pairs.items()},
            "farms": {pid: {"total_staked": pool.get("total_staked", 0.0), "apr": pool.get("apr", FARM_APR_DEFAULT)}
                      for pid, pool in self.store.dex_farm.items()},
            "paused": self.store.dex_paused,
            "events": len(self.store.dex_events),
        }

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _record(self, tx, op, target, msg, extra=None):
        self.store.dex_event_seq += 1
        ev = {"seq": self.store.dex_event_seq, "op": op, "addr": tx.sender,
              "target": target, "msg": msg, "ts": time.time()}
        if extra:
            ev.update(extra)
        self.store.dex_events[tx.txid] = ev

    # ------------------------------------------------------------------
    # 治理控制
    # ------------------------------------------------------------------
    def set_paused(self, paused):
        self.store.dex_paused = bool(paused)

    def set_farm_apr(self, pair_id, apr):
        if not (FARM_APR_MIN <= apr <= FARM_APR_MAX):
            return False
        if pair_id not in self.store.dex_farm:
            return False
        self.store.dex_farm[pair_id]["apr"] = float(apr)
        return True

    def bootstrap(self):
        """预售资金提供初始流动性（幂等）：0x_presale -> NOVA/USDT、NOVA/nETH。"""
        if self.store.dex_pairs or self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) <= 0:
            return False
        presale = self.store.balances.get("0x_presale", 0.0)
        if presale < 10000:
            return False
        # 初始流动性：NOVA 10000 + nUSDT 20000（1 NOVA = 2 USDT 起始锚定）
        nova = 10000.0
        usdt = 20000.0
        self._ensure_asset("nUSDT")
        self._ensure_asset("nETH")
        self.store.balances["0x_presale"] = _amt(presale - nova)
        pair0 = self._create_pair_internal("NOVA/USDT", tx=None)
        pair0["reserve0"] = nova
        pair0["reserve1"] = usdt
        pair0["total_shares"] = math.sqrt(nova * usdt) - MINIMUM_LIQUIDITY
        self.store.bridge_assets["nUSDT"]["balances"][self._reserve_holder("NOVA/USDT")] = usdt
        pair1 = self._create_pair_internal("NOVA/nETH", tx=None)
        neth = 2.0
        pair1["reserve0"] = nova * 0.5
        pair1["reserve1"] = neth
        pair1["total_shares"] = math.sqrt(pair1["reserve0"] * neth) - MINIMUM_LIQUIDITY
        self.store.bridge_assets["nETH"]["balances"][self._reserve_holder("NOVA/nETH")] = neth
        self.store.balances["0x_presale"] = _amt(self.store.balances["0x_presale"] - nova * 0.5)
        return True

    def _ensure_asset(self, symbol):
        if symbol not in self.store.bridge_assets:
            self.store.bridge_assets[symbol] = {"symbol": symbol, "supply": 0.0,
                                                "balances": {}, "created_at": time.time()}

    # ------------------------------------------------------------------
    # 内部账本
    # ------------------------------------------------------------------
    def _create_pair_internal(self, pair_id, tx):
        display = pair_id.split("/")[1]
        p = {"pair_id": pair_id, "token0": "NOVA", "token1": TOKEN_WRAP.get(display, display),
             "reserve0": 0.0, "reserve1": 0.0, "total_shares": 0.0,
             "fee": FEE_RATE, "paused": False, "burned0": 0.0, "burned1": 0.0,
             "created_at": time.time()}
        self.store.dex_pairs[pair_id] = p
        self.store.dex_farm[pair_id] = {"pair_id": pair_id, "apr": FARM_APR_DEFAULT,
                                        "total_staked": 0.0, "acc": 0.0, "last": time.time(),
                                        "users": {}}
        return p

    def _transfer_wrapped(self, symbol, frm, to, amount):
        a = self.store.bridge_assets.get(symbol)
        if not a or amount <= 0:
            return False
        bal = a.setdefault("balances", {})
        frm_bal = float(bal.get(frm, 0.0))
        if frm_bal < amount - 1e-9:
            return False
        bal[frm] = _amt(frm_bal - amount)
        if bal[frm] <= 0:
            bal.pop(frm, None)
        bal[to] = _amt(float(bal.get(to, 0.0)) + amount)
        return True

    def _burn_wrapped(self, symbol, frm, amount):
        a = self.store.bridge_assets.get(symbol)
        if not a:
            return False
        bal = a.setdefault("balances", {})
        frm_bal = float(bal.get(frm, 0.0))
        if frm_bal < amount - 1e-9:
            return False
        bal[frm] = _amt(frm_bal - amount)
        if bal[frm] <= 0:
            bal.pop(frm, None)
        a["supply"] = _amt(a["supply"] - amount)
        return True

    def _mint_lp(self, pair_id, addr, shares):
        key = f"{pair_id}|{addr}"
        entry = self.store.dex_lp.setdefault(key, {"addr": addr, "pair": pair_id, "shares": 0.0})
        entry["shares"] = _amt(entry["shares"] + shares)
        self.store.dex_pairs[pair_id]["total_shares"] = _amt(
            self.store.dex_pairs[pair_id]["total_shares"] + shares)

    def _burn_lp(self, pair_id, addr, shares):
        key = f"{pair_id}|{addr}"
        entry = self.store.dex_lp.get(key)
        if not entry or entry["shares"] < shares - 1e-9:
            return False
        entry["shares"] = _amt(entry["shares"] - shares)
        if entry["shares"] <= 0:
            del self.store.dex_lp[key]
        self.store.dex_pairs[pair_id]["total_shares"] = _amt(
            max(0.0, self.store.dex_pairs[pair_id]["total_shares"] - shares))
        return True

    # ------------------------------------------------------------------
    # 挖矿奖励（按份额累计，确定性）
    # ------------------------------------------------------------------
    def _farm_update(self, pool):
        now = time.time()
        dt = max(0.0, min(now - pool["last"], 86400))
        pool["last"] = now
        if pool["total_staked"] > 0 and dt > 0:
            rate_per_day = pool["total_staked"] * pool["apr"] / 365.0
            pool["acc"] = pool["acc"] + rate_per_day * dt / pool["total_staked"]
        return pool["acc"]

    def _farm_earned(self, pool, addr):
        u = pool["users"].get(addr)
        if not u:
            return 0.0
        return u["shares"] * (self._farm_update(pool) - u["debt"])

    def _farm_pay(self, addr, amount, tx):
        """从生态基金支付挖矿奖励，不足则留存 pending。"""
        fund = self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0.0)
        pay = min(amount, fund)
        if pay > 0:
            self.store.balances[self.economy.ECOSYSTEM_FUND] = _amt(fund - pay)
            self.store.balances[addr] = self.store.balances.get(addr, 0.0) + pay
        return pay

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------
    OPS = {
        "nova:dex:pair:create": "_pair",
        "nova:dex:add": "_add",
        "nova:dex:remove": "_remove",
        "nova:dex:swap": "_swap",
        "nova:dex:farm:stake": "_farm",
        "nova:dex:farm:unstake": "_farm",
        "nova:dex:farm:claim": "_farm",
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

    # ---------------- 交易对 ----------------
    def _pair_validate(self, d, tx):
        if tx.amount != 0 or self.store.dex_paused:
            return False
        pair_id = d.get("pair_id", "")
        if not PAIR_ID_RE.match(pair_id):
            return False
        token1 = TOKEN_WRAP.get(pair_id.split("/")[1], "")
        return token1 in ("nUSDT", "nETH") and pair_id not in self.store.dex_pairs

    def _pair_apply(self, tx, d):
        self._create_pair_internal(d["pair_id"], tx)
        self._record(tx, d["op"], d["pair_id"], f"交易对 {d['pair_id']} 已创建")

    # ---------------- 添加 / 移除流动性 ----------------
    def _add_validate(self, d, tx):
        if self.store.dex_paused:
            return False
        pair_id = d.get("pair_id", "")
        p = self.store.dex_pairs.get(pair_id)
        amount0 = d.get("amount0")
        amount1 = d.get("amount1")
        if not p or p.get("paused"):
            return False
        if not isinstance(amount0, (int, float)) or isinstance(amount0, bool):
            return False
        if not isinstance(amount1, (int, float)) or isinstance(amount1, bool):
            return False
        if not math.isfinite(amount0) or not math.isfinite(amount1):
            return False
        if amount0 <= 0 or amount1 <= 0:
            return False
        if tx.amount != amount0:
            return False  # token0 = NOVA，金额必须等于 NOVA 注入量
        if self.store.balances.get(tx.sender, 0) < amount0 + 1e-9:
            return False
        if self._token1_balance(pair_id, tx.sender) < amount1 - 1e-9:
            return False
        if p["total_shares"] > 0:
            # 比例偏差 >5% 拒绝（等价于滑点保护）
            r0 = amount0 / p["reserve0"] if p["reserve0"] > 0 else 0.0
            r1 = amount1 / p["reserve1"] if p["reserve1"] > 0 else 0.0
            if abs(r0 - r1) / max(r0, r1) > 0.05:
                return False
        return True

    def _add_apply(self, tx, d):
        pair_id = d["pair_id"]
        p = self.store.dex_pairs[pair_id]
        amount0, amount1 = float(d["amount0"]), float(d["amount1"])
        holder = self._reserve_holder(pair_id)
        # NOVA 注入（tx.amount 已在入口扣除）+ nUSDT/nETH 注入
        self._transfer_wrapped(p["token1"], tx.sender, holder, amount1)
        if p["total_shares"] <= MINIMUM_LIQUIDITY:
            shares = math.sqrt(amount0 * amount1) - MINIMUM_LIQUIDITY
            p["reserve0"] = amount0
            p["reserve1"] = amount1
        else:
            shares = min(amount0 / p["reserve0"], amount1 / p["reserve1"]) * p["total_shares"]
            p["reserve0"] += amount0
            p["reserve1"] += amount1
        p["reserve0"] = _amt(p["reserve0"])
        p["reserve1"] = _amt(p["reserve1"])
        self._mint_lp(pair_id, tx.sender, shares)
        self._record(tx, d["op"], pair_id,
                     f"添加流动性 {_amt(amount0)} NOVA + {_amt(amount1)} {p['token1']}，获得 {_amt(shares)} LP")

    def _remove_validate(self, d, tx):
        if self.store.dex_paused:
            return False
        pair_id = d.get("pair_id", "")
        p = self.store.dex_pairs.get(pair_id)
        shares = d.get("shares")
        if not p or p.get("paused"):
            return False
        if not isinstance(shares, (int, float)) or isinstance(shares, bool):
            return False
        if not math.isfinite(shares) or shares <= 0:
            return False
        if tx.amount != 0:
            return False
        if self._lp_of(pair_id, tx.sender) < shares - 1e-9:
            return False
        if p["total_shares"] <= MINIMUM_LIQUIDITY:
            return False
        # 滑点保护：期望获得的两种资产不低于用户设定下限
        out0 = shares / p["total_shares"] * p["reserve0"]
        out1 = shares / p["total_shares"] * p["reserve1"]
        min0 = d.get("min0", 0.0)
        min1 = d.get("min1", 0.0)
        return out0 >= float(min0) - 1e-9 and out1 >= float(min1) - 1e-9

    def _remove_apply(self, tx, d):
        pair_id = d["pair_id"]
        p = self.store.dex_pairs[pair_id]
        shares = float(d["shares"])
        out0 = _amt(shares / p["total_shares"] * p["reserve0"])
        out1 = _amt(shares / p["total_shares"] * p["reserve1"])
        self._burn_lp(pair_id, tx.sender, shares)
        p["reserve0"] = _amt(p["reserve0"] - out0)
        p["reserve1"] = _amt(p["reserve1"] - out1)
        self.store.balances[tx.sender] = self.store.balances.get(tx.sender, 0.0) + out0
        self._transfer_wrapped(p["token1"], self._reserve_holder(pair_id), tx.sender, out1)
        self._record(tx, d["op"], pair_id,
                     f"移除流动性 {_amt(shares)} LP，取回 {_amt(out0)} NOVA + {_amt(out1)} {p['token1']}")

    # ---------------- 兑换 ----------------
    def _swap_validate(self, d, tx):
        if self.store.dex_paused:
            return False
        pair_id = d.get("pair_id", "")
        p = self.store.dex_pairs.get(pair_id)
        if not p or p.get("paused"):
            return False
        amount_in = d.get("amount_in")
        token_in = d.get("token_in")
        min_out = d.get("min_out")
        if not isinstance(amount_in, (int, float)) or isinstance(amount_in, bool):
            return False
        if not isinstance(min_out, (int, float)) or isinstance(min_out, bool):
            return False
        if not math.isfinite(amount_in) or not math.isfinite(min_out):
            return False
        if amount_in <= 0 or min_out < 0 or token_in not in (0, 1):
            return False
        q = self.quote(pair_id, amount_in, token_in)
        if not q:
            return False
        # 滑点保护：默认最大滑点 5%（min_out >= 期望值 * 0.95），超限自动取消
        if q["amount_out"] < min_out - 1e-9:
            return False
        if token_in == 0:
            if tx.amount != amount_in:
                return False
        else:
            if tx.amount != 0:
                return False
            if self._token1_balance(pair_id, tx.sender) < amount_in - 1e-9:
                return False
        return True

    def _swap_apply(self, tx, d):
        pair_id = d["pair_id"]
        p = self.store.dex_pairs[pair_id]
        amount_in = float(d["amount_in"])
        token_in = int(d["token_in"])
        q = self.quote(pair_id, amount_in, token_in)
        amount_out = q["amount_out"]
        buyback = amount_in * BUYBACK_RATE
        holder = self._reserve_holder(pair_id)
        if token_in == 0:
            # NOVA -> nUSDT：NOVA 已在入口扣除；nUSDT 从池子给用户；回购销毁 NOVA
            p["reserve0"] = _amt(p["reserve0"] + amount_in - buyback)
            p["reserve1"] = _amt(p["reserve1"] - amount_out)
            p["burned0"] = _amt(p["burned0"] + buyback)
            self._transfer_wrapped(p["token1"], holder, tx.sender, amount_out)
        else:
            # nUSDT -> NOVA：扣除用户包装资产入池；NOVA 从池子给用户；回购销毁 nUSDT
            self._transfer_wrapped(p["token1"], tx.sender, holder, amount_in)
            p["reserve1"] = _amt(p["reserve1"] + amount_in - buyback)
            p["reserve0"] = _amt(p["reserve0"] - amount_out)
            p["burned1"] = _amt(p["burned1"] + buyback)
            self.store.balances[tx.sender] = self.store.balances.get(tx.sender, 0.0) + amount_out
        self._record(tx, d["op"], pair_id,
                     f"兑换 {_amt(amount_in)} -> {_amt(amount_out)}（滑点 {q['price_impact']*100:.2f}%，回购销毁 {_amt(buyback)}）",
                     {"token_in": token_in, "burned": buyback})

    # ---------------- 流动性挖矿 ----------------
    def _farm_validate(self, d, tx):
        if self.store.dex_paused:
            return False
        op = d.get("op")
        pair_id = d.get("pair_id", "")
        pool = self.store.dex_farm.get(pair_id)
        if not pool or pair_id not in self.store.dex_pairs:
            return False
        if op == "nova:dex:farm:stake":
            shares = d.get("shares")
            if tx.amount != 0 or not isinstance(shares, (int, float)) or isinstance(shares, bool):
                return False
            if not math.isfinite(shares) or shares <= 0:
                return False
            return self._lp_of(pair_id, tx.sender) >= shares - 1e-9
        if op == "nova:dex:farm:unstake":
            shares = d.get("shares")
            if tx.amount != 0 or not isinstance(shares, (int, float)) or isinstance(shares, bool):
                return False
            if not math.isfinite(shares) or shares <= 0:
                return False
            u = pool["users"].get(tx.sender, {"shares": 0.0})
            return u["shares"] >= shares - 1e-9
        if op == "nova:dex:farm:claim":
            return tx.amount == 0
        return False

    def _farm_apply(self, tx, d):
        op = d["op"]
        pair_id = d["pair_id"]
        pool = self.store.dex_farm[pair_id]
        self._farm_update(pool)
        user = pool["users"].setdefault(tx.sender, {"shares": 0.0, "debt": 0.0, "pending": 0.0})
        if op == "nova:dex:farm:stake":
            shares = float(d["shares"])
            user["pending"] = _amt(user["pending"] + self._farm_earned(pool, tx.sender))
            user["shares"] = _amt(user["shares"] + shares)
            pool["total_staked"] = _amt(pool["total_staked"] + shares)
            user["debt"] = pool["acc"] * user["shares"]
            self._burn_lp(pair_id, tx.sender, shares)
            self._record(tx, op, pair_id, f"质押 {_amt(shares)} LP 进入挖矿池")
        elif op == "nova:dex:farm:unstake":
            shares = float(d["shares"])
            earned = self._farm_earned(pool, tx.sender)
            user["pending"] = _amt(user["pending"] + earned)
            user["shares"] = _amt(user["shares"] - shares)
            pool["total_staked"] = _amt(max(0.0, pool["total_staked"] - shares))
            if user["shares"] <= 0:
                del pool["users"][tx.sender]
            else:
                user["debt"] = pool["acc"] * user["shares"]
            self._mint_lp(pair_id, tx.sender, shares)
            self._record(tx, op, pair_id, f"解除质押 {_amt(shares)} LP")
        elif op == "nova:dex:farm:claim":
            earned = self._farm_earned(pool, tx.sender)
            total = _amt(user.get("pending", 0.0) + earned)
            user["pending"] = 0.0
            user["debt"] = pool["acc"] * user["shares"]
            paid = self._farm_pay(tx.sender, total, tx)
            self._record(tx, op, pair_id, f"领取挖矿奖励 {_amt(paid)} NOVA")
            return

    def maintain(self):
        return 0
