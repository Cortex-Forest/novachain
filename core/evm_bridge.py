# -*- coding: utf-8 -*-
"""跨引擎资产桥接（v0.11，G4）：
原生 Actor 合约资产（粉丝代币/碎片/盲盒成就 NFT）<-> EVM 包装资产（ERC-20/ERC-721）。

- 系统级桥接合约：原生侧 ActorBridge（NATIVE_BRIDGE）+ EVM 侧 EvmBridge（EVM_BRIDGE），
  两者均为固定确定性地址，部署时豁免部署奖励（系统合约）。
- 双向映射：原生资产 -> EVM 包装（ERC-721 tokenId 确定性派生，OpenSea 可识别）；
  EVM 资产 -> 原生 wrapped 资产（记入 store.evm_wrapped_native，可一键转回）。
- 手续费 0.1%（FT 按金额 / NFT 每枚固定 0.001 NOVA），100% 回流验证者激励池。
- 原子性：余额/资产快照 + 顺序扣源->铸目标，任一步失败整笔回滚（无半途状态）。
"""
import hashlib
import json
import time

from core.evm import keccak256, WEI_SCALE, nova_to_wei, wei_to_nova

# 系统桥接合约地址（确定性固定）
_NB = keccak256(b"nova:evm:bridge:actor").hex()[-40:]
_EB = keccak256(b"nova:evm:bridge:evm").hex()[-40:]
NATIVE_BRIDGE = "0x" + _NB
EVM_BRIDGE = "0x" + _EB

BRIDGE_FEE_RATE = 0.001          # 跨引擎桥接收取 0.1% 手续费
NFT_BRIDGE_FEE = 0.001           # NFT 类每枚固定手续费（NOVA）
WRAPPED_URI_BASE = "https://explorer.yourdomain.com/nova/evm/nft/"


def _amt(v):
    return round(float(v), 8)


def _h(s: str) -> str:
    return hashlib.sha3_256(s.encode()).hexdigest()


def wrapped_token_id(asset_id: str) -> int:
    """原生资产 -> EVM ERC-721 tokenId（确定性，跨节点一致）。"""
    return int.from_bytes(keccak256(asset_id.encode()), "big") & ((1 << 256) - 1)


def wrapped_token_id_hex(asset_id: str) -> str:
    return "0x" + wrapped_token_id(asset_id).to_bytes(32, "big").hex()


def asset_uri(asset_id: str) -> str:
    return WRAPPED_URI_BASE + _h(asset_id)[:64]


def evm_bridge_bytecode() -> str:
    """EvmBridge 系统合约字节码占位（运行时以专用 handler 响应标准方法）。
    部署地址与 eth_call 行为一致；字节码非空以保证 CODE 查询兼容。"""
    # 极小 runtime：直接 return 空（函数分派由 evm_bridge handler 完成）
    return "0x" + ("00" * 0) + "60006000f3"  # PUSH1 0 PUSH1 0 RETURN


class EvmBridge:
    """跨引擎桥接引擎（挂载在 NovaNode 上，op 经 validate/apply 路由调用）。"""

    OPS = ("nova:bridge:evm:convert", "nova:bridge:evm:revert")

    def __init__(self, store, economy, evm):
        self.store = store
        self.economy = economy
        self.evm = evm

    # ------------------------------------------------------------------
    # 原生资产读取（统一抽象）
    # ------------------------------------------------------------------
    def native_hold(self, addr, asset_id):
        """返回 (kind, amount)。asset_id 格式：fan:<tid> / frac:<fid> / ach:<aid>。"""
        try:
            prefix, rid = asset_id.split(":", 1)
        except (ValueError, AttributeError):
            return None, 0.0
        if prefix == "fan":
            t = self.store.fan_tokens.get(rid)
            if t:
                return "ft", float(t.get("holders", {}).get(addr, 0))
        elif prefix == "frac":
            f = self.store.fractions.get(rid)
            if f:
                return "nft", float(f.get("fractions", {}).get(addr, 0))
        elif prefix == "ach":
            if rid in self.store.achievements and addr in self.store.soulbound.get(rid, {}):
                return "nft", 1.0
        return None, 0.0

    def native_deduct(self, addr, asset_id, amount):
        """扣减原生资产（源侧）。amount<=0 或不足抛 ValueError。"""
        prefix, rid = asset_id.split(":", 1)
        if prefix == "fan":
            t = self.store.fan_tokens.get(rid)
            if not t or float(t.get("holders", {}).get(addr, 0)) < amount:
                raise ValueError("fan 持仓不足")
            if amount <= 0:
                raise ValueError("数量无效")
            t["holders"][addr] = round(float(t["holders"].get(addr, 0)) - amount, 8)
            if t["holders"][addr] <= 0:
                del t["holders"][addr]
        elif prefix == "frac":
            f = self.store.fractions.get(rid)
            if not f or float(f.get("fractions", {}).get(addr, 0)) < amount:
                raise ValueError("frac 持仓不足")
            if amount <= 0:
                raise ValueError("数量无效")
            f["fractions"][addr] = round(float(f["fractions"].get(addr, 0)) - amount, 8)
            if f["fractions"][addr] <= 0:
                del f["fractions"][addr]
        elif prefix == "ach":
            if rid not in self.store.achievements or addr not in self.store.soulbound.get(rid, {}):
                raise ValueError("成就 NFT 不存在")
            del self.store.soulbound[rid][addr]
            if not self.store.soulbound[rid]:
                del self.store.soulbound[rid]
        else:
            raise ValueError("未知资产类型")

    def native_restore(self, addr, asset_id, amount):
        """释放：把原生资产恢复给 native 属主（EVM->原生 或 revert 方向）。"""
        prefix, rid = asset_id.split(":", 1)
        if prefix == "fan":
            t = self.store.fan_tokens.get(rid)
            if t:
                t.setdefault("holders", {})[addr] = round(float(t["holders"].get(addr, 0)) + amount, 8)
        elif prefix == "frac":
            f = self.store.fractions.get(rid)
            if f:
                f.setdefault("fractions", {})[addr] = round(float(f["fractions"].get(addr, 0)) + amount, 8)
        elif prefix == "ach":
            self.store.soulbound.setdefault(rid, {})[addr] = time.time()
        else:
            raise ValueError("未知资产类型")

    # ------------------------------------------------------------------
    # 手续费
    # ------------------------------------------------------------------
    def bridge_fee(self, kind, amount):
        if kind == "nft":
            return NFT_BRIDGE_FEE
        return _amt(float(amount) * BRIDGE_FEE_RATE)

    def _pay_fee(self, addr, fee):
        """从 addr 扣手续费入验证者激励池（0.1% 100% 回流）。"""
        if fee <= 0:
            return
        if self.store.balances.get(addr, 0) < fee:
            raise ValueError("余额不足以支付跨引擎桥接手续费")
        self.store.balances[addr] = _amt(self.store.balances.get(addr, 0) - fee)
        self.store.balances[self.economy.VALIDATOR_POOL] = _amt(
            self.store.balances.get(self.economy.VALIDATOR_POOL, 0) + fee)

    # ------------------------------------------------------------------
    # 原生 -> EVM 转换（nova:bridge:evm:convert）
    # ------------------------------------------------------------------
    def convert_validate(self, d, tx):
        op = d.get("op")
        asset = d.get("asset", "")
        if op != "nova:bridge:evm:convert" or tx.sender != tx.receiver:
            return False
        if not isinstance(asset, str) or ":" not in asset:
            return False
        evm_addr = self.store.evm_bindings.get(tx.sender, "")
        if not evm_addr:
            return False  # 需先 nova:evm:bind
        kind, hold = self.native_hold(tx.sender, asset)
        if not kind:
            return False
        if kind == "ft":
            amount = float(d.get("amount", 0))
            if amount <= 0 or hold < amount:
                return False
        else:
            amount = 1.0
        fee = self.bridge_fee(kind, amount)
        return self.store.balances.get(tx.sender, 0) >= fee

    def convert_apply(self, tx, d):
        """原子：快照 -> 扣源 -> 扣费 -> 铸目标；任一失败回滚。"""
        addr = tx.sender
        asset = d.get("asset", "")
        evm_addr = self.store.evm_bindings.get(addr, "")
        kind, _ = self.native_hold(addr, asset)
        amount = float(d.get("amount", 1.0)) if kind == "ft" else 1.0
        fee = self.bridge_fee(kind, amount)

        # 快照（回滚用）
        snap = {
            "addr_bal": self.store.balances.get(addr, 0),
            "vp_bal": self.store.balances.get(self.economy.VALIDATOR_POOL, 0),
            "wrapped_snap": {k: dict(v) for k, v in self.store.evm_wrapped.items()},
            "native_state": self._asset_state(asset),
        }
        try:
            self.native_deduct(addr, asset, amount)
            self._pay_fee(addr, fee)
            token_id = wrapped_token_id(asset)
            key = str(token_id)
            if key in self.store.evm_wrapped:
                # 同资产多次 convert 累加（确定性，跨节点一致）
                rec = self.store.evm_wrapped[key]
                rec["amount"] = _amt(float(rec.get("amount", 0.0)) + amount)
            else:
                self.store.evm_wrapped[key] = {
                    "native_asset": asset,
                    "kind": kind,
                    "evm_owner": evm_addr,
                    "native_owner": addr,
                    "amount": amount,
                    "uri": asset_uri(asset),
                    "ts": time.time(),
                }
            # 事件记录
            self.store.evm_bridge_events.setdefault(str(self.store.evm_bridge_seq), {
                "seq": self.store.evm_bridge_seq, "op": "convert", "addr": addr,
                "asset": asset, "amount": amount, "evm_owner": evm_addr,
                "token_id": wrapped_token_id_hex(asset), "ts": time.time(),
            })
            self.store.evm_bridge_seq += 1
        except Exception:
            self._rollback(snap, addr, asset)
            raise

    def _asset_state(self, asset_id):
        """资产属主状态快照（回滚用）：{addr: qty}。"""
        prefix, rid = asset_id.split(":", 1)
        if prefix == "fan":
            t = self.store.fan_tokens.get(rid)
            return dict(t.get("holders", {})) if t else {}
        if prefix == "frac":
            f = self.store.fractions.get(rid)
            return dict(f.get("fractions", {})) if f else {}
        if prefix == "ach":
            return dict(self.store.soulbound.get(rid, {}))
        return {}

    def _rollback(self, snap, addr, asset):
        self.store.balances[addr] = snap["addr_bal"]
        self.store.balances[self.economy.VALIDATOR_POOL] = snap["vp_bal"]
        self.store.evm_wrapped = {k: dict(v) for k, v in snap["wrapped_snap"].items()}
        # 恢复资产属主
        prefix, rid = asset.split(":", 1)
        if prefix == "fan":
            t = self.store.fan_tokens.get(rid)
            if t:
                t["holders"] = {k: float(v) for k, v in snap["native_state"].items()}
        elif prefix == "frac":
            f = self.store.fractions.get(rid)
            if f:
                f["fractions"] = {k: float(v) for k, v in snap["native_state"].items()}
        elif prefix == "ach":
            self.store.soulbound[rid] = {k: v for k, v in snap["native_state"].items()}

    # ------------------------------------------------------------------
    # EVM -> 原生（nova:bridge:evm:revert / EvmBridge burn）
    # ------------------------------------------------------------------
    def revert_validate(self, d, tx):
        op = d.get("op")
        if op != "nova:bridge:evm:revert" or tx.sender != tx.receiver:
            return False
        token_id = str(d.get("token_id", ""))
        rec = self.store.evm_wrapped.get(token_id)
        if not rec:
            return False
        # 请求者必须是被包装资产的 native 属主（或已绑定到该 EVM 属主）
        native_owner = rec.get("native_owner", "")
        if tx.sender != native_owner:
            # 允许 EVM 属主经绑定反查请求
            if self.store.evm_bindings.get(tx.sender) != rec.get("evm_owner"):
                return False
        kind = rec.get("kind", "nft")
        fee = self.bridge_fee(kind, float(rec.get("amount", 1.0)))
        return self.store.balances.get(tx.sender, 0) >= fee

    def revert_apply(self, tx, d):
        addr = tx.sender
        token_id = str(d.get("token_id", ""))
        rec = self.store.evm_wrapped.get(token_id)
        if not rec:
            return
        kind = rec.get("kind", "nft")
        amount = float(rec.get("amount", 1.0))
        fee = self.bridge_fee(kind, amount)
        snap = {
            "addr_bal": self.store.balances.get(addr, 0),
            "vp_bal": self.store.balances.get(self.economy.VALIDATOR_POOL, 0),
            "wrapped_snap": {k: dict(v) for k, v in self.store.evm_wrapped.items()},
            "rec": dict(rec),
        }
        try:
            self._pay_fee(addr, fee)
            del self.store.evm_wrapped[token_id]
            native_owner = rec.get("native_owner", "")
            self.native_restore(native_owner, rec["native_asset"], amount)
            self.store.evm_bridge_events.setdefault(str(self.store.evm_bridge_seq), {
                "seq": self.store.evm_bridge_seq, "op": "revert", "addr": addr,
                "asset": rec["native_asset"], "amount": amount,
                "native_owner": native_owner, "token_id": token_id, "ts": time.time(),
            })
            self.store.evm_bridge_seq += 1
        except Exception:
            self.store.balances[addr] = snap["addr_bal"]
            self.store.balances[self.economy.VALIDATOR_POOL] = snap["vp_bal"]
            self.store.evm_wrapped = {k: dict(v) for k, v in snap["wrapped_snap"].items()}
            raise

    # ------------------------------------------------------------------
    # MetaMask 调用 EvmBridge（eth 交易到 EVM_BRIDGE，选择器分派）
    # ------------------------------------------------------------------
    def eth_burn(self, from_evm, calldata, block_time=0):
        """MetaMask 对 EvmBridge 的 burn(tokenId) 调用：
        释放包装资产回原生属主（经 evm_bindings 反查）。返回 (success, log)。"""
        if len(calldata) < 4:
            return False, "calldata 过短"
        sel = calldata[:4].hex()
        # burn(uint256) = 0x42966c68
        if sel == "42966c68":
            token_id = str(int.from_bytes(calldata[4:36], "big"))
            rec = self.store.evm_wrapped.get(token_id)
            if not rec:
                return False, "包装资产不存在"
            if rec.get("evm_owner") != from_evm:
                return False, "非包装资产属主"
            kind = rec.get("kind", "nft")
            amount = float(rec.get("amount", 1.0))
            fee = self.bridge_fee(kind, amount)
            bal_wei = int(float(self.store.balances.get(from_evm, 0.0)) * WEI_SCALE)
            if bal_wei < nova_to_wei(fee):
                return False, "EVM 余额不足以支付手续费"
            self._pay_fee(from_evm, fee)
            del self.store.evm_wrapped[token_id]
            self.native_restore(rec.get("native_owner", ""), rec["native_asset"], amount)
            self.store.evm_bridge_events.setdefault(str(self.store.evm_bridge_seq), {
                "seq": self.store.evm_bridge_seq, "op": "eth_burn", "addr": from_evm,
                "asset": rec["native_asset"], "amount": amount,
                "native_owner": rec.get("native_owner", ""), "token_id": token_id, "ts": block_time,
            })
            self.store.evm_bridge_seq += 1
            return True, "burn 成功，资产已释放回原生侧"
        # 其它方法走标准 ERC 查询 handler
        return self.handle_eth_call(calldata, block_time), "query"

    # ------------------------------------------------------------------
    # EvmBridge 标准 ERC-721/ERC-20 查询 handler（eth_call 与 EVM 内部 CALL 共用）
    # ------------------------------------------------------------------
    def handle_eth_call(self, calldata, block_time=0):
        """解析标准方法选择器，从 store.evm_wrapped 返回包装资产数据。
        返回 32 字节 ABI word（bytes），失败返回 b""。"""
        if len(calldata) < 4:
            return b""
        sel = calldata[:4].hex()
        try:
            if sel == "06fdde03":  # name()
                return _abi_string("Nova Wrapped Asset")
            if sel == "95d89b41":  # symbol()
                return _abi_string("NWA")
            if sel == "0dfe1681":  # token()
                return _abi_address(EVM_BRIDGE)
            if sel == "18160ddd":  # totalSupply()
                return int(len(self.store.evm_wrapped)).to_bytes(32, "big")
            if sel == "70a08231":  # balanceOf(address)
                owner = _abi_to_address(calldata[4:36])
                n = sum(1 for r in self.store.evm_wrapped.values() if r.get("evm_owner") == owner)
                return n.to_bytes(32, "big")
            if sel == "6352211e":  # ownerOf(uint256)
                tid = str(int.from_bytes(calldata[4:36], "big"))
                rec = self.store.evm_wrapped.get(tid)
                if not rec:
                    return b""
                return _abi_address(rec.get("evm_owner", "0x" + "00" * 20))
            if sel == "c87b56dd":  # tokenURI(uint256)
                tid = str(int.from_bytes(calldata[4:36], "big"))
                rec = self.store.evm_wrapped.get(tid)
                if not rec:
                    return b""
                return _abi_string(rec.get("uri", ""))
            if sel == "2e1a7d4d":  # withdraw()（release 辅助）
                return b""
            if sel == "a9059cbb":  # transfer(address,uint256) 仅查询模式返回 false
                return b""
        except Exception:
            return b""
        return b""


def _abi_word(x):
    return (x & ((1 << 256) - 1)).to_bytes(32, "big")


def _abi_address(addr):
    return bytes.fromhex(addr[2:].rjust(64, "0")[-40:]) if addr.startswith("0x") else b""


def _abi_string(s: str):
    data = s.encode()
    off = _abi_word(32)
    ln = _abi_word(len(data))
    payload = data + b"\x00" * ((32 - len(data) % 32) % 32)
    return off + ln + payload


def _abi_to_address(data):
    return "0x" + data[-20:].hex()
