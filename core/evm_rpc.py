# -*- coding: utf-8 -*-
"""MetaMask 兼容 RPC（v0.11，G3）：
以太坊 JSON-RPC 标准接口，与 Nova 原生 /api/* 并行、端口复用、按 method 分发。

方法集：eth_chainId / eth_blockNumber / eth_getBalance / eth_getTransactionCount /
eth_sendRawTransaction / eth_sendTransaction / eth_call / eth_estimateGas /
eth_gasPrice / eth_getTransactionReceipt / eth_getTransactionByHash / eth_getCode /
eth_getStorageAt / eth_accounts / net_version / net_listening / eth_syncing。

交易路径：RLP 签名交易 -> ECDSA 验签/恢复 -> nonce/余额校验 -> EVM 执行（或部署/转账）
-> DAG 账本同步 -> 标准回执。NOVA 以 18 位 wei 计价，链内换算 8 位。
"""
import hashlib
import time
import json

from core.evm import (CHAIN_ID, WEI_SCALE, GAS_WEI, decode_signed_tx, keccak256,
                      create_address, nova_to_wei, wei_to_nova, evm_address_from_pubkey,
                      EvmExecutionError, EvmRevert, UINT256_MAX)
from core.evm_bridge import EVM_BRIDGE, NATIVE_BRIDGE, wrapped_token_id_hex

CHAIN_ID_HEX = "0x" + format(CHAIN_ID, "x")           # 0xa23a2
NET_VERSION = str(CHAIN_ID)
GAS_LIMIT_DEFAULT = 10_000_000
TX_GAS = 21_000                                        # 外部转账标准 gas
BLOCK_GAS_LIMIT = 30_000_000


def _hex(n):
    if isinstance(n, bool):
        return "0x0"
    return "0x" + format(int(n), "x")


def _hex32(x):
    return "0x" + (int(x) & UINT256_MAX).to_bytes(32, "big").hex()


def _wei_hex(nova):
    return _hex(int(nova * WEI_SCALE))


def _addr_hex(addr):
    return addr if addr.startswith("0x") else "0x" + addr


class EvmRpc:
    """MetaMask RPC 处理器（挂载在 NovaNode 上）。"""

    def __init__(self, node):
        self.node = node
        self.store = node.store
        self.economy = node.economy
        self.evm = node.evm
        self.bridge = node.evm_bridge

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def handle(self, payload):
        """payload 为标准 JSON-RPC dict。返回 dict（含 id/jsonrpc/result|error）。"""
        rid = payload.get("id", None)
        method = payload.get("method", "")
        params = payload.get("params", []) or []
        try:
            result = self.dispatch(method, params)
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except EvmRpcError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": str(e)}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32000, "message": f"internal error: {e}"}}

    def dispatch(self, method, params):
        fn = getattr(self, method.replace(".", "_"), None)
        if fn is None:
            # 未实现的 eth_* 方法返回一致空（MetaMask 兼容，不报错）
            if method.startswith("eth_"):
                return None
            raise EvmRpcError(-32601, "method not found")
        return fn(params)

    # ------------------------------------------------------------------
    # 网络信息
    # ------------------------------------------------------------------
    def eth_chainId(self, params):
        return CHAIN_ID_HEX

    def net_version(self, params):
        return NET_VERSION

    def net_listening(self, params):
        return True

    def eth_syncing(self, params):
        return False

    def eth_coinbase(self, params):
        return self.node.validator.address if self.node.validator else "0x0000000000000000000000000000000000000000"

    def eth_blockNumber(self, params):
        return _hex(self.node.consensus.chain_height())

    def eth_gasPrice(self, params):
        return _hex(GAS_WEI)

    def eth_maxPriorityFeePerGas(self, params):
        return _hex(GAS_WEI // 2)

    def eth_feeHistory(self, params):
        return {"oldestBlock": "0x0", "baseFeePerGas": [_hex(GAS_WEI)], "gasUsedRatio": [0.5], "reward": [[]]}

    def eth_accounts(self, params):
        # 节点默认不持有用户账户；前端可通过 Web3 注入关联钱包
        accounts = []
        if self.node.validator:
            accounts.append(self.node.validator.address)
        return accounts

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def eth_getBalance(self, params):
        addr = _addr_hex(str(params[0]))
        return _wei_hex(self.store.balances.get(addr, 0.0))

    def eth_getTransactionCount(self, params):
        addr = _addr_hex(str(params[0]))
        return _hex(self.store.evm_nonce.get(addr, 0))

    def eth_getCode(self, params):
        addr = _addr_hex(str(params[0]))
        c = self.store.evm_contracts.get(addr)
        if not c:
            return "0x"
        return c.get("bytecode", "0x") or "0x"

    def eth_getStorageAt(self, params):
        addr = _addr_hex(str(params[0]))
        try:
            slot = int(str(params[1]), 16)
        except (ValueError, IndexError):
            slot = 0
        return _hex32(self.store.evm_storage.get(addr, {}).get(slot, 0))

    def eth_blockNumber_alt(self, params):
        return self.eth_blockNumber(params)

    # ------------------------------------------------------------------
    # eth_call / estimateGas
    # ------------------------------------------------------------------
    def eth_call(self, params):
        call = params[0] if params else {}
        to = _addr_hex(str(call.get("to", "")))
        data = self._hex_to_bytes(call.get("data", call.get("input", "0x")))
        value = int(str(call.get("value", "0x0")), 16)
        fr = _addr_hex(str(call.get("from", "0x" + "00" * 20)))
        return self._read_call(to, fr, value, data)

    def eth_estimateGas(self, params):
        call = params[0] if params else {}
        to = _addr_hex(str(call.get("to", "")))
        if to in self.store.evm_contracts:
            return _hex(GAS_LIMIT_DEFAULT)
        return _hex(TX_GAS)

    def _read_call(self, to, fr, value, data):
        """只读执行：快照 balances/evm_storage，执行后回滚。返回 0x hex。"""
        snap_bal = dict(self.store.balances)
        snap_storage = {k: dict(v) for k, v in self.store.evm_storage.items()}
        snap_wrapped = {k: dict(v) for k, v in self.store.evm_wrapped.items()}
        try:
            if to == EVM_BRIDGE:
                res = self.bridge.handle_eth_call(data)
                return "0x" + res.hex() if res else "0x"
            if to in self.store.evm_contracts:
                r = self.evm.run(to, caller=fr, origin=fr, value=value, data=data,
                                 gas_limit=GAS_LIMIT_DEFAULT,
                                 block_height=self.node.consensus.chain_height(),
                                 block_time=int(time.time()),
                                 coinbase=self.eth_coinbase([]))
                if not r.get("success"):
                    raise EvmRpcError(-32000, "execution reverted")
                return "0x" + r.get("return_data", b"").hex()
            # 外部账户 eth_call：返回空（无返回值）
            return "0x"
        finally:
            self.store.balances = snap_bal
            self.store.evm_storage = {k: dict(v) for k, v in snap_storage.items()}
            self.store.evm_wrapped = {k: dict(v) for k, v in snap_wrapped.items()}

    # ------------------------------------------------------------------
    # 交易提交
    # ------------------------------------------------------------------
    def eth_sendRawTransaction(self, params):
        raw = str(params[0])
        tx = decode_signed_tx(raw)
        if tx["chain_id"] not in (0, CHAIN_ID):
            raise EvmRpcError(-32000, f"invalid chain id {tx['chain_id']}, expected {CHAIN_ID}")
        receipt = self._execute_evm_tx(tx)
        return receipt["transactionHash"]

    def eth_sendTransaction(self, params):
        """参数化交易（MetaMask 在某些场景/工具下使用）。由节点构造并签名执行。"""
        p = params[0] if params else {}
        fr = _addr_hex(str(p.get("from", "")))
        to = _addr_hex(str(p.get("to", ""))) if p.get("to") else ""
        value = int(str(p.get("value", "0x0")), 16)
        data = self._hex_to_bytes(p.get("data", "0x"))
        nonce = int(str(p.get("nonce", "0x0")), 16)
        gas_price = int(str(p.get("gasPrice", _hex(GAS_WEI))), 16)
        gas_limit = int(str(p.get("gas", _hex(GAS_LIMIT_DEFAULT))), 16)
        # 构造 EIP-155 交易（v = 35 + chainId*2，r/s=0 表示未签名 -> 节点本地执行）
        tx = {
            "chain_id": CHAIN_ID, "nonce": nonce, "gas_price": gas_price,
            "gas_limit": gas_limit, "to": to, "value": value, "data": data,
            "v": 35 + CHAIN_ID * 2, "r": 0, "s": 0, "from": fr,
            "hash": "0x" + keccak256(b"node:" + fr.encode() + str(nonce).encode() + str(int(time.time())).encode()).hex(),
        }
        return self._execute_evm_tx(tx)["transactionHash"]

    def eth_getTransactionReceipt(self, params):
        h = str(params[0]).lower()
        r = self.store.evm_receipts.get(h)
        if not r:
            return None
        return r

    def eth_getTransactionByHash(self, params):
        h = str(params[0]).lower()
        r = self.store.evm_receipts.get(h)
        if not r:
            return None
        return {
            "hash": h,
            "nonce": r.get("nonce", "0x0"),
            "blockHash": r.get("blockHash"),
            "blockNumber": r.get("blockNumber"),
            "transactionIndex": r.get("transactionIndex"),
            "from": r.get("from"),
            "to": r.get("to"),
            "value": r.get("value", "0x0"),
            "gas": r.get("gas", "0x0"),
            "gasPrice": r.get("effectiveGasPrice"),
            "input": r.get("input", "0x"),
        }

    def eth_getLogs(self, params):
        """按区块/地址过滤日志（简化：返回全部日志或按地址过滤）。"""
        logs = []
        f = (params[0] if params else {}) or {}
        addr_filter = (f.get("address") or "").lower()
        for r in self.store.evm_receipts.values():
            for lg in r.get("logs", []):
                if addr_filter and lg.get("address", "").lower() != addr_filter:
                    continue
                logs.append(lg)
        return logs

    # ------------------------------------------------------------------
    # 核心：EVM 交易执行 -> DAG 同步 -> 回执
    # ------------------------------------------------------------------
    def _execute_evm_tx(self, tx):
        fr = tx["from"]
        nonce = tx["nonce"]
        # nonce 校验
        if nonce != self.store.evm_nonce.get(fr, 0):
            raise EvmRpcError(-32000, f"invalid nonce: got {nonce}, expected {self.store.evm_nonce.get(fr, 0)}")
        value_wei = tx["value"]
        gas_price = tx.get("gas_price") or GAS_WEI
        gas_limit = tx.get("gas_limit") or GAS_LIMIT_DEFAULT

        # ---- 沙盒预检：余额下限（value + 最小 gas） ----
        min_cost = value_wei + gas_price * TX_GAS
        if self.store.balances.get(fr, 0.0) * WEI_SCALE < min_cost:
            raise EvmRpcError(-32000, "insufficient funds for transfer")

        logs = []
        contract_address = None
        status = "0x1"
        to = tx["to"]
        gas_used = TX_GAS
        revert_reason = ""

        # 快照（执行失败回滚用）
        snap_bal = dict(self.store.balances)
        snap_storage = {k: dict(v) for k, v in self.store.evm_storage.items()}
        snap_contracts = {k: dict(v) for k, v in self.store.evm_contracts.items()}
        snap_nonce = dict(self.store.evm_nonce)
        snap_wrapped = {k: dict(v) for k, v in self.store.evm_wrapped.items()}

        try:
            # 1) 部署交易（to 为空）
            if not to:
                gas_used = 21000 + 200 * ((len(tx["data"]) + 31) // 32)
                new_addr = create_address(fr, nonce)
                # 运行 init code 获取 runtime（临时合约）
                res = self.evm.run_init(new_addr, tx["data"], creator=fr)
                if not res.get("success"):
                    status = "0x0"
                    gas_used = res.get("gas_used", gas_used) or TX_GAS
                else:
                    runtime = res.get("return_data", b"")
                    self.store.evm_contracts[new_addr] = {
                        "bytecode": "0x" + runtime.hex(),
                        "creator": fr,
                        "ts": int(time.time()),
                    }
                    contract_address = new_addr
                    to = new_addr
                if tx["data"]:
                    gas_used += 32000 + 200 * ((len(tx["data"]) + 31) // 32)

            # 2) 系统桥接合约调用
            elif to == EVM_BRIDGE:
                ok, _ = self.bridge.eth_burn(fr, tx["data"], block_time=int(time.time()))
                status = "0x1" if ok else "0x0"
                gas_used = TX_GAS + 20000

            # 3) EVM 合约调用
            elif to in self.store.evm_contracts:
                r = self.evm.run(to, caller=fr, origin=fr, value=value_wei, data=tx["data"],
                                 gas_limit=gas_limit,
                                 block_height=self.node.consensus.chain_height(),
                                 block_time=int(time.time()),
                                 coinbase=self.eth_coinbase([]))
                gas_used = max(r.get("gas_used", TX_GAS), TX_GAS)
                if not r.get("success"):
                    status = "0x0"
                    revert_reason = "execution reverted"
                for ev in r.get("events", []):
                    logs.append(self._to_eth_log(ev, len(logs)))

            # 4) 外部账户 / 系统地址转账
            else:
                gas_used = TX_GAS
                if value_wei:
                    self._move_balance(fr, to, value_wei)

            # 5) 结算：扣费（gas_price * gas_used）入验证者池
            gas_cost = gas_price * gas_used
            if self.store.balances.get(fr, 0.0) * WEI_SCALE < gas_cost:
                raise EvmRpcError(-32000, "insufficient funds for gas")
            self._charge_gas(fr, gas_cost)

            # 6) nonce +1
            self.store.evm_nonce[fr] = nonce + 1
        except (EvmRpcError, EvmExecutionError, ValueError):
            self.store.balances = snap_bal
            self.store.evm_storage = {k: dict(v) for k, v in snap_storage.items()}
            self.store.evm_contracts = {k: dict(v) for k, v in snap_contracts.items()}
            self.store.evm_nonce = dict(snap_nonce)
            self.store.evm_wrapped = {k: dict(v) for k, v in snap_wrapped.items()}
            raise

        # 7) DAG 账本同步
        txhash = tx["hash"]
        self.store.dag.add(txhash)
        block_number = self.node.consensus.chain_height()
        block_hash = "0x" + hashlib.sha3_256(
            f"{block_number}{txhash}".encode()).hexdigest()[:64]
        receipt = {
            "transactionHash": txhash,
            "transactionIndex": "0x0",
            "blockHash": block_hash,
            "blockNumber": _hex(block_number),
            "from": fr,
            "to": to or None,
            "cumulativeGasUsed": _hex(gas_used),
            "gasUsed": _hex(gas_used),
            "gas": _hex(gas_limit),
            "contractAddress": contract_address,
            "logs": logs,
            "logsBloom": "0x" + "00" * 256,
            "status": status,
            "effectiveGasPrice": _hex(gas_price),
            "nonce": _hex(nonce),
            "value": _hex(value_wei),
            "input": "0x" + (tx["data"].hex() if isinstance(tx["data"], bytes) else tx["data"]),
            "type": "0x0",
            "chainId": CHAIN_ID_HEX,
            "revertReason": revert_reason or None,
            "ts": time.time(),
        }
        self.store.evm_receipts[txhash] = receipt
        # 同步 tx_history（Nova 浏览器可见）
        self.store.tx_history[txhash] = {
            "txid": txhash, "sender": fr, "receiver": to or contract_address or "",
            "amount": wei_to_nova(value_wei), "gas": wei_to_nova(gas_cost),
            "data": "evm:" + txhash, "ts": time.time(), "confirmed_at": time.time(),
        }
        # 负载指标（与原生一致）
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self.store.daily_tx_count[day] = int(self.store.daily_tx_count.get(day, 0)) + 1
        return receipt

    def _move_balance(self, fr, to, value_wei):
        nova = wei_to_nova(value_wei)
        if nova <= 0:
            return
        if self.store.balances.get(fr, 0.0) < nova:
            raise EvmRpcError(-32000, "insufficient funds")
        self.store.balances[fr] = round(self.store.balances.get(fr, 0.0) - nova, 8)
        self.store.balances[to] = round(self.store.balances.get(to, 0.0) + nova, 8)

    def _charge_gas(self, fr, gas_cost_wei):
        nova = wei_to_nova(gas_cost_wei)
        if nova <= 0:
            return
        if self.store.balances.get(fr, 0.0) < nova:
            raise EvmRpcError(-32000, "insufficient funds for gas")
        self.store.balances[fr] = round(self.store.balances.get(fr, 0.0) - nova, 8)
        self.store.balances[self.economy.VALIDATOR_POOL] = round(
            self.store.balances.get(self.economy.VALIDATOR_POOL, 0.0) + nova, 8)

    @staticmethod
    def _to_eth_log(ev, idx):
        return {
            "address": ev.get("address", ""),
            "topics": ev.get("topics", []),
            "data": ev.get("data", "0x"),
            "blockNumber": _hex(0),
            "transactionHash": "0x",
            "transactionIndex": "0x0",
            "blockHash": "0x" + "00" * 32,
            "logIndex": _hex(idx),
            "removed": False,
        }

    @staticmethod
    def _hex_to_bytes(s):
        if not isinstance(s, str):
            return b""
        h = s[2:] if s.startswith("0x") else s
        try:
            return bytes.fromhex(h)
        except ValueError:
            return b""


class EvmRpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
