# -*- coding: utf-8 -*-
"""Nova 链跨链桥：打通 BSC/ETH/Polygon，外部资产跨入铸造包装资产、NOVA/包装资产跨出释放。

设计（对应需求）：
- 跨入：用户在 BSC/ETH 存入 USDT 等 -> 桥节点监听源链存款事件 -> 3/5 多签确认 ->
  Nova 链铸造等额包装资产（nUSDT/nETH）给用户（扣 0.1% 手续费，最低 1 USDT）。
- 跨出：用户在 Nova 销毁包装资产（或支付 NOVA）-> 节点多签确认 -> 源链释放原始
  资产给用户（扣 0.1% 手续费）。
- 节点：Nova 超级节点担任，额外质押 1000 NOVA；3/5 多签防单点作恶；作恶罚没全部质押。
- 手续费 100% 回流验证者激励池（bridge_fee_pool，每日统一转入 validator pool）。
- 安全：每日跨链额度上限 100 万 USDT（初期）；单笔 >10 万 USDT 延迟 24 小时到账。

与其它模块一致：signed tx（sender == receiver，data 为 JSON {op, ...}），
全部状态在 store 上，确定性重放。
"""
import hashlib
import json
import math
import re
import time

HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{2,10}$")

BRIDGE_STAKE = 1000.0            # 桥节点质押（NOVA）
BRIDGE_MAX_NODES = 21
REQUIRED_SIGS = 3                # 3/5 多签
TOTAL_SIGS = 5
NODE_MIN_AGE = 3600              # 桥节点注册后最小年龄（秒）：未满不可参与多签（防快速女巫）
FEE_RATIO = 0.001                # 0.1% 手续费
FEE_MIN_USD = 1.0                # 最低 1 USDT
# v0.9 无感跨链大额保护：分档延迟 + 单日风控审核
DAILY_LIMIT_USD = 10_000_000.0   # 每日跨链额度软上限（USDT，不再硬拒，达线转风控审核）
REVIEW_LIMIT_USD = 10_000_000.0  # 单日单地址跨入达 1000 万 USDT：触发风控审核
REVIEW_DELAY = 72 * 3600         # 审核态延迟 72 小时（次日自动恢复）
DELAY_TIER1_USD = 100_000.0      # 10 万 USDT 以下：立即到账，无任何延迟
DELAY_TIER2_USD = 1_000_000.0    # 10 万-100 万：延迟 1 小时
DELAY_1H = 3600
DELAY_24H = 24 * 3600            # >100 万：延迟 24 小时
UNBOND = 7 * 86400
CHAINS = ("bsc", "eth", "polygon")
SUPPORTED = {
    "nUSDT": {"underlying": "USDT", "chains": ("bsc", "eth", "polygon"), "fallback_usd": 1.0},
    "nETH": {"underlying": "WETH", "chains": ("eth", "bsc"), "fallback_usd": 1500.0},
    "NOVA": {"underlying": "NOVA", "chains": ("bsc", "eth"), "fallback_usd": 0.5},
}


def _amt(v):
    return round(float(v), 8)


def _day(ts=None):
    # UTC 自然日（审计：统一 UTC，避免跨时区节点额度窗口不一致）
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


class Bridge:
    def __init__(self, store, economy, oracle=None):
        self.store = store
        self.economy = economy
        self.oracle = oracle

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def _usd_value(self, asset, amount):
        """按预言机价格换算 USD；无价格时用冷启动兜底价。"""
        if self.oracle is not None:
            feed = {"nUSDT": "USDT/USD", "nETH": "ETH/USD", "NOVA": "NOVA/USD"}.get(asset)
            if feed:
                p = self.oracle.price(feed)
                if p:
                    # oracle.price() 普通 feed 返回 dict、派生 feed 返回 float、无价返回 None；
                    # 统一归一为数值，避免 float * dict 类型错误（审计 F-05）
                    px = p.get("price") if isinstance(p, dict) else p
                    if px is not None:
                        px = float(px)
                        if px > 0:
                            return float(amount) * px
        return float(amount) * SUPPORTED[asset]["fallback_usd"]

    def _fee(self, asset, amount):
        fee = float(amount) * FEE_RATIO
        min_fee = FEE_MIN_USD / max(self._usd_value(asset, 1.0), 1e-12)
        return _amt(max(fee, min_fee))

    def _daily_limit_usd(self):
        override = self.store.gov_params.get("bridge.daily_limit_usd")
        return float(override) if override else DAILY_LIMIT_USD

    def _daily_used_usd(self):
        usage = self.store.bridge_daily_usage.get(_day(), {})
        return float(usage.get("minted_usd", 0.0)) + float(usage.get("released_usd", 0.0))

    def _check_limit(self, usd) -> bool:
        # v0.9：1000 万软上限不再硬拒（转风控审核延迟）；仅保留 3 倍最终硬顶防总量失控
        return self._daily_used_usd() + usd <= self._daily_limit_usd() * 3.0

    def _delay_for(self, usd) -> float:
        """无感分档延迟：<10 万立即 / 10 万-100 万 1h / >100 万 24h。"""
        if usd < DELAY_TIER1_USD:
            return 0.0
        if usd < DELAY_TIER2_USD:
            return DELAY_1H
        return DELAY_24H

    def _addr_daily_usd(self, addr) -> float:
        usage = self.store.bridge_addr_daily.get(_day(), {})
        return float(usage.get(addr, 0.0))

    def _in_review(self, addr, usd) -> bool:
        """单日单地址跨入累计（含本次）>= 1000 万 USDT → 风控审核（延迟 72h，次日自动恢复）。"""
        return self._addr_daily_usd(addr) + usd >= REVIEW_LIMIT_USD

    def _record_addr_usage(self, addr, usd):
        usage = self.store.bridge_addr_daily.setdefault(_day(), {})
        usage[addr] = float(usage.get(addr, 0.0)) + usd

    def asset(self, symbol):
        return self.store.bridge_assets.get(symbol)

    def deposit(self, did):
        return self.store.bridge_deposits.get(did)

    def withdrawal(self, wid):
        return self.store.bridge_withdrawals.get(wid)

    def summary(self):
        return {
            "nodes": len(self.store.bridge_nodes),
            "assets": {s: {"supply": a.get("supply", 0.0), "holders": len(a.get("balances", {}))}
                       for s, a in self.store.bridge_assets.items()},
            "deposits": len(self.store.bridge_deposits),
            "withdrawals": len(self.store.bridge_withdrawals),
            "fee_pool": _amt(self.store.bridge_fee_pool),
            "daily_usage_usd": _amt(self._daily_used_usd()),
            "daily_limit_usd": self._daily_limit_usd(),
            "events": len(self.store.bridge_events),
            "slashed": _amt(self.store.bridge_slashed),
        }

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _record(self, tx, op, target, msg, extra=None):
        self.store.bridge_event_seq += 1
        ev = {"seq": self.store.bridge_event_seq, "op": op, "addr": tx.sender,
              "target": target, "msg": msg, "ts": time.time()}
        if extra:
            ev.update(extra)
        self.store.bridge_events[tx.txid] = ev

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _is_node(self, addr):
        n = self.store.bridge_nodes.get(addr)
        return n is not None and n.get("status") == "active"

    def _slash(self, addr, reason, tx):
        node = self.store.bridge_nodes.get(addr)
        if not node:
            return
        stake = float(node.get("stake", 0.0))
        if stake > 0:
            self.store.balances[self.economy.ECOSYSTEM_FUND] = \
                self.store.balances.get(self.economy.ECOSYSTEM_FUND, 0) + stake
            self.store.bridge_slashed = _amt(self.store.bridge_slashed + stake)
        node["status"] = "slashed"
        node["stake"] = 0.0
        node["slash_reason"] = reason
        self._record(tx, "nova:bridge:node:slash", addr, f"桥节点作恶罚没：{reason}")

    def _add_sig(self, record, key, addr):
        sigs = record.setdefault(key, [])
        if addr not in sigs:
            sigs.append(addr)
            return True
        return False

    def _sigs_ok(self, record):
        return len(record.get("sigs", [])) >= REQUIRED_SIGS

    def _finalizable(self, record):
        """多签齐后，大额需等 24 小时冷却，普通金额立即可执行。"""
        if not self._sigs_ok(record):
            return False, "sigs"
        if record.get("large"):
            return time.time() >= record.get("available_at", 0), "hold"
        return True, "ok"

    def _burn_wrapped(self, asset, addr, amount):
        a = self.store.bridge_assets[asset]
        bal = float(a["balances"].get(addr, 0.0))
        if amount <= 0 or amount > bal + 1e-9:
            return False
        a["balances"][addr] = _amt(bal - amount)
        if a["balances"][addr] <= 0:
            del a["balances"][addr]
        a["supply"] = _amt(a["supply"] - amount)
        return True

    def _mint_wrapped(self, asset, addr, amount):
        a = self.store.bridge_assets.setdefault(asset, {
            "symbol": asset, "supply": 0.0, "balances": {},
            "created_at": time.time(),
        })
        a["supply"] = _amt(a["supply"] + amount)
        a["balances"][addr] = _amt(a["balances"].get(addr, 0.0) + amount)

    def _charge_fee(self, fee):
        self.store.bridge_fee_pool = _amt(self.store.bridge_fee_pool + fee)

    def _record_usage(self, kind, usd):
        key = _day()
        usage = self.store.bridge_daily_usage.setdefault(key, {"minted_usd": 0.0, "released_usd": 0.0})
        usage[kind] = _amt(float(usage.get(kind, 0.0)) + usd)

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------
    OPS = {
        "nova:bridge:node:register": "_node",
        "nova:bridge:node:exit": "_node",
        "nova:bridge:node:claim": "_node",
        "nova:bridge:asset:register": "_asset",
        "nova:bridge:deposit": "_deposit",
        "nova:bridge:deposit:sign": "_deposit",
        "nova:bridge:deposit:claim": "_deposit",
        "nova:bridge:deposit:cancel": "_deposit",  # 无感跨链保护：延迟期可取消（撤回原链）
        "nova:bridge:withdraw": "_withdraw",
        "nova:bridge:withdraw:sign": "_withdraw",
        "nova:bridge:withdraw:confirm": "_withdraw",
        "nova:bridge:pool:flush": "_pool",
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

    # ---------------- 节点 ----------------
    def _node_validate(self, d, tx):
        op = d.get("op")
        if op == "nova:bridge:node:register":
            return (tx.amount >= BRIDGE_STAKE and tx.sender not in self.store.bridge_nodes
                    and len(self.store.bridge_nodes) < BRIDGE_MAX_NODES)
        if op == "nova:bridge:node:exit":
            n = self.store.bridge_nodes.get(tx.sender)
            return tx.amount == 0 and n is not None and n.get("status") == "active" \
                and not n.get("exiting")
        if op == "nova:bridge:node:claim":
            return tx.amount == 0 and self.exit_claimable(tx.sender) > 0
        return False

    def _node_apply(self, tx, d):
        op = d["op"]
        if op == "nova:bridge:node:register":
            self.store.bridge_nodes[tx.sender] = {
                "addr": tx.sender, "stake": tx.amount, "status": "active",
                "registered_at": time.time(), "signed": 0,
            }
            self._record(tx, op, tx.sender, f"跨链桥节点注册，质押 {_amt(tx.amount)} NOVA")
        elif op == "nova:bridge:node:exit":
            self.store.bridge_nodes[tx.sender]["exiting"] = time.time() + UNBOND
            self._record(tx, op, tx.sender, "桥节点退出，进入 7 天冷却期")
        elif op == "nova:bridge:node:claim":
            amt = self.claim_exit(tx.sender, tx)

    def exit_claimable(self, addr):
        n = self.store.bridge_nodes.get(addr)
        if not n or n.get("status") != "active" or not n.get("exiting"):
            return 0.0
        if time.time() < n["exiting"]:
            return 0.0
        return float(n.get("stake", 0.0))

    def claim_exit(self, addr, tx):
        amt = self.exit_claimable(addr)
        if amt <= 0:
            return 0.0
        self.store.balances[addr] = self.store.balances.get(addr, 0) + amt
        n = self.store.bridge_nodes[addr]
        n["stake"] = 0.0
        n["status"] = "exited"
        self._record(tx, "nova:bridge:node:claim", addr, f"桥节点退出质押返还 {_amt(amt)} NOVA")
        return amt

    # ---------------- 资产注册 ----------------
    def _asset_validate(self, d, tx):
        symbol = d.get("symbol", "")
        if not SYMBOL_RE.match(symbol) or symbol not in SUPPORTED:
            return False
        if symbol in self.store.bridge_assets:
            return False
        # 仅已注册桥节点可注册资产
        return tx.amount == 0 and self._is_node(tx.sender)

    def _asset_apply(self, tx, d):
        symbol = d["symbol"]
        self.store.bridge_assets[symbol] = {
            "symbol": symbol, "supply": 0.0, "balances": {},
            "created_at": time.time(),
        }
        self._record(tx, d["op"], symbol, f"包装资产 {symbol} 已注册")

    # ---------------- 跨入（外部资产 -> Nova） ----------------
    def _deposit_validate(self, d, tx):
        op = d.get("op")
        if op == "nova:bridge:deposit":
            if not self._is_node(tx.sender) or tx.amount != 0:
                return False
            asset = d.get("asset", "")
            if asset not in SUPPORTED:
                return False
            chain = d.get("source_chain", "")
            source_tx = d.get("source_tx", "")
            source_addr = d.get("source_addr", "")
            amount = d.get("amount")
            if chain not in CHAINS or not HEX64_RE.match(source_tx):
                return False
            if not ADDRESS_RE.match(source_addr) or source_addr == "0x0000":
                return False
            if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                return False
            if not math.isfinite(amount) or amount <= 0:
                return False
            # 同一源链交易只能入账一次（防重放）
            key = f"{chain}:{source_tx}"
            for dep in self.store.bridge_deposits.values():
                if dep.get("key") == key and dep.get("status") != "cancelled":
                    return False
            # 额度检查（含手续费）
            usd = self._usd_value(asset, float(amount))
            return self._check_limit(usd)
        if op == "nova:bridge:deposit:sign":
            if not self._is_node(tx.sender) or tx.amount != 0:
                return False
            dep = self.store.bridge_deposits.get(d.get("deposit_id", ""))
            if not dep or dep.get("status") not in ("pending", "held", "ready"):
                return False
            if len(dep.get("sigs", [])) >= REQUIRED_SIGS:
                return False
            if tx.sender in dep.get("sigs", []):
                return False
            # 防女巫：节点注册后需经过最小年龄才能参与多签（审计 F-01）
            node = self.store.bridge_nodes.get(tx.sender)
            if node and time.time() < float(node.get("registered_at", 0)) + NODE_MIN_AGE:
                return False
            # 观察一致性：签名节点必须提供与存款一致的源链事件观察，
            # 防止「单节点提交伪造存款、其余节点无脑签名」（审计 F-01）
            obs_tx = d.get("source_tx", "")
            obs_addr = d.get("source_addr", "")
            obs_amount = d.get("source_amount")
            if obs_tx != dep.get("source_tx") or obs_addr != dep.get("source_addr"):
                return False
            if not isinstance(obs_amount, (int, float)) or isinstance(obs_amount, bool):
                return False
            if abs(float(obs_amount) - float(dep.get("amount", 0.0))) > 1e-9:
                return False
            return True
        if op == "nova:bridge:deposit:claim":
            if tx.amount != 0:
                return False
            dep = self.store.bridge_deposits.get(d.get("deposit_id", ""))
            if not dep or dep.get("status") not in ("held", "ready"):
                return False
            ok, why = self._finalizable(dep)
            return ok
        if op == "nova:bridge:deposit:cancel":
            # 无感跨链保护：仅 user 本人、且未到账（minted/cancelled 除外）可取消，撤回原链
            if tx.amount != 0:
                return False
            dep = self.store.bridge_deposits.get(d.get("deposit_id", ""))
            if not dep or dep.get("status") in ("minted", "cancelled"):
                return False
            if dep.get("user") != tx.sender:
                return False
            return True
        return False

    def _deposit_apply(self, tx, d):
        op = d["op"]
        if op == "nova:bridge:deposit":
            self.store.bridge_deposit_seq += 1
            did = f"dep-{self.store.bridge_deposit_seq:012d}"
            amount = float(d["amount"])
            fee = self._fee(d["asset"], amount)
            usd = self._usd_value(d["asset"], amount)
            delay = self._delay_for(usd)
            review = self._in_review(d.get("user", d["source_addr"]), usd)
            if review:
                delay = max(delay, REVIEW_DELAY)
            large = delay > 0
            dep = {
                "deposit_id": did, "key": f"{d['source_chain']}:{d['source_tx']}",
                "asset": d["asset"], "source_chain": d["source_chain"],
                "source_tx": d["source_tx"], "source_addr": d["source_addr"],
                "user": d.get("user", d["source_addr"]), "amount": amount,
                "fee": fee, "usd": _amt(usd), "large": large, "review": bool(review),
                "status": "pending", "sigs": [], "created_at": time.time(),
                "available_at": time.time() + delay if large else 0.0,
            }
            self.store.bridge_deposits[did] = dep
            self._add_sig(dep, "sigs", tx.sender)
            self._record(tx, op, did,
                         f"跨入存款 {_amt(amount)} {d['asset']}（{d['source_chain']}）")
        elif op == "nova:bridge:deposit:sign":
            dep = self.store.bridge_deposits[d["deposit_id"]]
            self._add_sig(dep, "sigs", tx.sender)
            dep.setdefault("sig_observations", {})[tx.sender] = {
                "source_tx": d.get("source_tx", ""),
                "source_addr": d.get("source_addr", ""),
                "amount": float(d.get("source_amount", 0.0)),
                "ts": time.time(),
            }
            if self._sigs_ok(dep):
                dep["status"] = "held" if dep.get("large") else "ready"
                dep["confirmed_at"] = time.time()
                self._record(tx, op, dep["deposit_id"],
                             f"跨入存款多签达成 {len(dep['sigs'])}/{TOTAL_SIGS}",
                             {"status": dep["status"]})
            else:
                self._record(tx, op, dep["deposit_id"],
                             f"节点 {tx.sender[:10]}... 已签名（{len(dep['sigs'])}/{REQUIRED_SIGS}）")
        elif op == "nova:bridge:deposit:claim":
            dep = self.store.bridge_deposits[d["deposit_id"]]
            if dep["status"] == "held" and time.time() < dep["available_at"]:
                return
            net = _amt(dep["amount"] - dep["fee"])
            self._mint_wrapped(dep["asset"], dep["user"], net)
            self._charge_fee(dep["fee"])
            self._record_usage("minted_usd", dep["usd"])
            self._record_addr_usage(dep["user"], dep["usd"])
            dep["status"] = "minted"
            dep["minted_at"] = time.time()
            self._record(tx, op, dep["deposit_id"],
                         f"已铸造 {_amt(net)} {dep['asset']}（手续费 {_amt(dep['fee'])}）")
        elif op == "nova:bridge:deposit:cancel":
            dep = self.store.bridge_deposits[d["deposit_id"]]
            if dep.get("status") in ("minted", "cancelled"):
                return
            dep["status"] = "cancelled"
            dep["cancelled_at"] = time.time()
            self._record(tx, op, dep["deposit_id"],
                         f"跨入已取消（撤回原链），未铸造 {_amt(dep['amount'])} {dep['asset']}")

    # ---------------- 跨出（Nova -> 外部资产） ----------------
    def _withdraw_validate(self, d, tx):
        op = d.get("op")
        if op == "nova:bridge:withdraw":
            asset = d.get("asset", "")
            if asset not in SUPPORTED:
                return False
            target_chain = d.get("target_chain", "")
            target_addr = d.get("target_addr", "")
            if target_chain not in CHAINS or not ADDRESS_RE.match(target_addr):
                return False
            if asset == "NOVA":
                amount = tx.amount
                if not isinstance(amount, (int, float)) or amount <= 0:
                    return False
                if self.store.balances.get(tx.sender, 0) < amount + 1e-9:
                    return False
            else:
                if tx.amount != 0:
                    return False
                amount = d.get("amount")
                if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                    return False
                if not math.isfinite(amount) or amount <= 0:
                    return False
                a = self.store.bridge_assets.get(asset)
                if not a or float(a.get("balances", {}).get(tx.sender, 0.0)) < amount - 1e-9:
                    return False
            usd = self._usd_value(asset, float(amount))
            return self._check_limit(usd)
        if op == "nova:bridge:withdraw:sign":
            if not self._is_node(tx.sender) or tx.amount != 0:
                return False
            wd = self.store.bridge_withdrawals.get(d.get("withdraw_id", ""))
            if not wd or wd.get("status") not in ("pending", "held", "ready"):
                return False
            if len(wd.get("sigs", [])) >= REQUIRED_SIGS:
                return False
            if tx.sender in wd.get("sigs", []):
                return False
            # 防女巫：节点最小年龄同样适用于跨出多签（审计 F-01）
            node = self.store.bridge_nodes.get(tx.sender)
            if node and time.time() < float(node.get("registered_at", 0)) + NODE_MIN_AGE:
                return False
            return True
        if op == "nova:bridge:withdraw:confirm":
            if not self._is_node(tx.sender) or tx.amount != 0:
                return False
            wd = self.store.bridge_withdrawals.get(d.get("withdraw_id", ""))
            if not wd or wd.get("status") not in ("held", "ready"):
                return False
            ok, why = self._finalizable(wd)
            return ok
        return False

    def _withdraw_apply(self, tx, d):
        op = d["op"]
        if op == "nova:bridge:withdraw":
            asset = d["asset"]
            amount = tx.amount if asset == "NOVA" else float(d["amount"])
            fee = self._fee(asset, amount)
            usd = self._usd_value(asset, amount)
            delay = self._delay_for(usd)   # 跨出与跨入对称分档延迟（防洗钱口径一致）
            large = delay > 0
            # 扣减：NOVA 原生余额在 apply 入口统一扣除；包装资产在此销毁
            if asset != "NOVA":
                self._burn_wrapped(asset, tx.sender, amount)
            self.store.bridge_withdrawal_seq += 1
            wid = f"wd-{self.store.bridge_withdrawal_seq:012d}"
            self.store.bridge_withdrawals[wid] = {
                "withdraw_id": wid, "asset": asset, "user": tx.sender,
                "target_chain": d["target_chain"], "target_addr": d["target_addr"],
                "amount": amount, "fee": fee, "usd": _amt(usd), "large": large,
                "status": "pending", "sigs": [], "created_at": time.time(),
                "available_at": time.time() + delay if large else 0.0,
                "release_tx": "",
            }
            self._record(tx, op, wid,
                         f"跨出请求 {_amt(amount)} {asset} -> {d['target_chain']}")
        elif op == "nova:bridge:withdraw:sign":
            wd = self.store.bridge_withdrawals[d["withdraw_id"]]
            self._add_sig(wd, "sigs", tx.sender)
            if self._sigs_ok(wd):
                wd["status"] = "held" if wd.get("large") else "ready"
                wd["confirmed_at"] = time.time()
                self._record(tx, op, wd["withdraw_id"],
                             f"跨出多签达成 {len(wd['sigs'])}/{TOTAL_SIGS}",
                             {"status": wd["status"]})
            else:
                self._record(tx, op, wd["withdraw_id"],
                             f"节点 {tx.sender[:10]}... 已签名（{len(wd['sigs'])}/{REQUIRED_SIGS}）")
        elif op == "nova:bridge:withdraw:confirm":
            wd = self.store.bridge_withdrawals[d["withdraw_id"]]
            if wd["status"] == "held" and time.time() < wd["available_at"]:
                return
            wd["status"] = "released"
            wd["release_tx"] = d.get("release_tx", "")
            wd["released_at"] = time.time()
            self._charge_fee(wd["fee"])
            self._record_usage("released_usd", wd["usd"])
            self._record(tx, op, wd["withdraw_id"],
                         f"已在 {wd['target_chain']} 释放 {_amt(wd['amount'] - wd['fee'])} {wd['asset']}")

    # ---------------- 手续费回流 ----------------
    def _pool_validate(self, d, tx):
        if tx.amount != 0 or not self._is_node(tx.sender):
            return False
        return True

    def _pool_apply(self, tx, d):
        # 每日最多回流一次
        key = f"flush:{_day()}"
        for ev in self.store.bridge_events.values():
            if ev.get("op") == "nova:bridge:pool:flush" and ev.get("flush_day") == _day():
                return
        amt = self.store.bridge_fee_pool
        if amt > 0:
            self.store.balances[self.economy.VALIDATOR_POOL] = \
                self.store.balances.get(self.economy.VALIDATOR_POOL, 0) + amt
            self.store.bridge_fee_pool = 0.0
            self._record(tx, d["op"], "validator_pool",
                         f"跨链手续费 {_amt(amt)} NOVA 回流验证者激励池",
                         {"flush_day": _day()})

    def maintain(self):
        """自动回流手续费（每日）并释放到期大额跨链。"""
        flushed = False
        if self.store.bridge_fee_pool > 0:
            key = f"flush:{_day()}"
            if not any(ev.get("flush_day") == _day() for ev in self.store.bridge_events.values()):
                self.store.balances[self.economy.VALIDATOR_POOL] = \
                    self.store.balances.get(self.economy.VALIDATOR_POOL, 0) + self.store.bridge_fee_pool
                self.store.bridge_fee_pool = 0.0
                flushed = True
        return flushed


