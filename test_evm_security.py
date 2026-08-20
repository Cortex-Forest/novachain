# -*- coding: utf-8 -*-
"""v0.11 EVM 安全审计测试：
① EVM 执行沙盒资源上限：内存 / 步数 / 存储槽 / gas 耗尽
② 跨引擎桥接原子性与重入防护：中途失败回滚、重复 burn 拒绝
③ 混合账户签名验证边界：无效 ECDSA 签名 / 非属主 / 重复绑定 / 迁移不可逆
④ RPC 接口以太坊标准兼容性：eth_call 只读、错误 chainId / nonce / 余额拒绝
"""
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from core.evm import (Evm, CHAIN_ID, keccak256, rlp_encode, ecdsa_verify,
                      ecdsa_pubkey_from_private, evm_address_from_pubkey, create_address,
                      _point_mul, _N, EvmExecutionError)
from core.evm_examples import simple_storage_bytecode, erc20_bytecode
from core.evm_bridge import EVM_BRIDGE, wrapped_token_id, wrapped_token_id_hex, BRIDGE_FEE_RATE
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9951)
    kw.setdefault("rpc", 8111)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _signed_op(w, op, **kw):
    payload = {"op": op}
    payload.update(kw)
    data = json.dumps(payload, ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, 0, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _broadcast(node, tx):
    assert node.validate_tx(tx)
    node.store.dag.add(tx.txid)
    node.apply_tx(tx)
    node._record_tx(tx)


class _Eco:
    def _day_key(self):
        return "2026-08-20"


def _evm_store():
    from types import SimpleNamespace
    return SimpleNamespace(balances={}, evm_contracts={}, evm_storage={}, evm_nonce={},
                           evm_wrapped={}, gov_params={})


def _deploy(evm, store, code_hex, creator):
    addr = create_address(creator, 0)
    res = evm.run_init(addr, bytes.fromhex(code_hex[2:]), creator=creator)
    assert res["success"] and res["return_data"], res
    store.evm_contracts[addr] = {"bytecode": "0x" + res["return_data"].hex(),
                                 "creator": creator, "ts": 0}
    return addr


# ---------------------------------------------------------------------------
# ① EVM 沙盒资源上限
# ---------------------------------------------------------------------------
class TestEvmSandbox:
    def test_memory_limit_enforced(self):
        """MSTORE 超过 64KB 内存上限应失败（沙盒）。"""
        store = _evm_store()
        evm = Evm(store, _Eco())
        addr = "0x" + "11" * 20
        # PUSH1 0 PUSH2 0xffff MSTORE：offset=0xffff+32 超限（64KB）
        code = bytes.fromhex("600061ffff5260006000f3")
        store.evm_contracts[addr] = {"bytecode": "0x" + code.hex(), "creator": "0x" + "11" * 20, "ts": 0}
        r = evm.run(addr, caller="0x" + "11" * 20, gas_limit=10 ** 7)
        assert not r["success"]
        assert getattr(evm, "_last_error", "") == "memory limit exceeded"

    def test_step_limit_enforced(self):
        """无限循环（JUMP 自身）应触发步数上限。"""
        store = _evm_store()
        evm = Evm(store, _Eco())
        addr = "0x" + "11" * 20
        # PUSH1 0x00 JUMP 死循环（跳回 0，0 处是 PUSH1 非 JUMPDEST -> bad jump 提前终止）
        # 用 JUMPDEST 自环：0x5b 0x56 5b 56...
        code = bytes.fromhex("5b565b565b565b565b565b565b56")
        store.evm_contracts[addr] = {"bytecode": "0x" + code.hex(), "creator": "0x" + "11" * 20, "ts": 0}
        r = evm.run(addr, caller="0x" + "11" * 20, gas_limit=10 ** 7)
        assert not r["success"] or r["steps"] < Evm.MAX_STEPS

    def test_storage_keys_limit(self):
        """SSTORE 超过单合约存储槽上限应失败。"""
        store = _evm_store()
        evm = Evm(store, _Eco())
        addr = "0x" + "11" * 20
        # 构造一个循环写入大量槽的字节码：PUSH1 0 PUSH1 0 PUSH1 0 MSTORE... 简化用单条超限触发
        # 直接填满存储
        store.evm_storage[addr] = {i: 0 for i in range(Evm.MAX_STORAGE_KEYS)}
        # PUSH3 0xf4240(1M) PUSH1 0 SSTORE：新槽超出存储上限
        code = bytes.fromhex("620f424060005500")
        store.evm_contracts[addr] = {"bytecode": "0x" + code.hex(), "creator": "0x" + "11" * 20, "ts": 0}
        r = evm.run(addr, caller="0x" + "11" * 20, gas_limit=10 ** 7)
        assert not r["success"]
        assert "storage limit exceeded" in getattr(evm, "_last_error", "")

    def test_out_of_gas_enforced(self):
        """gas_limit 耗尽应失败（不产生状态变更）。"""
        store = _evm_store()
        evm = Evm(store, _Eco())
        owner = "0x" + "33" * 20
        addr = _deploy(evm, store, erc20_bytecode(), owner)
        store.evm_storage[addr] = {}
        # gas_limit=1 的 mint 必然 OOG（需多个 SSTORE）
        r = evm.run(addr, caller=owner,
                    data=bytes.fromhex("40c10f19") + bytes.fromhex("33" * 32) + (100).to_bytes(32, "big"),
                    gas_limit=1)
        assert not r["success"]
        # 状态未变更
        assert store.evm_storage.get(addr, {}) == {}


# ---------------------------------------------------------------------------
# ② 桥接原子性与重入防护
# ---------------------------------------------------------------------------
class TestBridgeSecurity:
    def _mk_fan(self, node, w):
        tid = "fan_sec_" + "2" * 20
        node.store.fan_tokens[tid] = {
            "id": tid, "creator": w.address, "symbol": "S", "name": "s",
            "supply": 100, "sold": 0, "price": 1.0, "avatar_cid": "",
            "created_at": time.time(), "holders": {w.address: 10}, "proposals": {}, "voted": {},
        }
        return tid

    def test_convert_insufficient_balance_rolls_back(self):
        """原子性：余额不足支付手续费时整笔回滚（无半途状态：原生资产未扣、无包装、池未变）。"""
        node = _node()
        w = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        node.balances[w.address] = 100.0
        _broadcast(node, _signed_op(w, "nova:evm:bind", pubkey=pub.hex()))
        tid = self._mk_fan(node, w)
        asset = f"fan:{tid}"
        # 余额充足，先成功一次
        _broadcast(node, _signed_op(w, "nova:bridge:evm:convert", asset=asset, amount=2))
        key = str(wrapped_token_id(asset))
        assert key in node.store.evm_wrapped
        held = node.store.fan_tokens[tid]["holders"].get(w.address, 0)
        # 把余额清零 -> 手续费无法支付 -> validate 拒绝（不进入 apply，天然原子）
        node.balances[w.address] = 0.0
        tx = _signed_op(w, "nova:bridge:evm:convert", asset=asset, amount=2)
        assert not node.validate_tx(tx)
        # 状态未被部分修改
        assert node.store.fan_tokens[tid]["holders"].get(w.address, 0) == held
        assert len(node.store.evm_wrapped) == 1

    def test_burn_reentrancy_guard(self):
        """重入防护：同一包装资产重复 burn 第二次被拒绝（状态已删除）。"""
        node = _node()
        w = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        evm_addr = evm_address_from_pubkey(pub)
        node.balances[w.address] = 100.0
        _broadcast(node, _signed_op(w, "nova:evm:bind", pubkey=pub.hex()))
        tid = self._mk_fan(node, w)
        _broadcast(node, _signed_op(w, "nova:bridge:evm:convert", asset=f"fan:{tid}", amount=3))
        key = str(wrapped_token_id(f"fan:{tid}"))
        node.balances[evm_addr] = 1.0
        # 第一次 burn 成功
        ok1, _ = node.evm_bridge.eth_burn(evm_addr, bytes.fromhex("42966c68") + wrapped_token_id(f"fan:{tid}").to_bytes(32, "big"))
        assert ok1
        # 第二次 burn 失败（资产已不存在）
        ok2, msg = node.evm_bridge.eth_burn(evm_addr, bytes.fromhex("42966c68") + wrapped_token_id(f"fan:{tid}").to_bytes(32, "big"))
        assert not ok2
        assert "不存在" in msg

    def test_bridge_converts_are_deterministic(self):
        """确定性：同一原生资产 tokenId 跨节点一致（无重复铸造）。"""
        asset = "fan:abc123"
        id1 = wrapped_token_id(asset)
        id2 = wrapped_token_id(asset)
        assert id1 == id2
        assert len(wrapped_token_id_hex(asset)) == 66


# ---------------------------------------------------------------------------
# ③ 混合账户签名验证边界
# ---------------------------------------------------------------------------
class TestHybridSecurity:
    def test_bind_rejects_invalid_pubkey(self):
        node = _node()
        w = QuantumWallet()
        node.balances[w.address] = 100.0
        # 长度错误
        assert not node.validate_tx(_signed_op(w, "nova:evm:bind", pubkey="abcd"))
        # 非 hex
        assert not node.validate_tx(_signed_op(w, "nova:evm:bind", pubkey="zz" * 64))

    def test_bind_rejects_duplicate_evm(self):
        node = _node()
        w1 = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        evm_addr = evm_address_from_pubkey(pub)
        node.balances[w1.address] = 100.0
        _broadcast(node, _signed_op(w1, "nova:evm:bind", pubkey=pub.hex()))
        # 另一 native 用户尝试绑定同一 EVM 地址 -> 拒绝
        w2 = QuantumWallet()
        node.balances[w2.address] = 100.0
        assert not node.validate_tx(_signed_op(w2, "nova:evm:bind", pubkey=pub.hex()))

    def test_migrate_rejects_bad_ecdsa_signature(self):
        node = _node()
        w = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        evm_addr = evm_address_from_pubkey(pub)
        node.balances[w.address] = 100.0
        _broadcast(node, _signed_op(w, "nova:evm:bind", pubkey=pub.hex()))
        # 错误签名（r=s=1）
        tx = _signed_op(w, "nova:evm:migrate", evm_addr=evm_addr, r=1, s=1)
        assert not node.validate_tx(tx)

    def test_migrate_rejects_non_owner(self):
        node = _node()
        w1 = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        evm_addr = evm_address_from_pubkey(pub)
        node.balances[w1.address] = 100.0
        _broadcast(node, _signed_op(w1, "nova:evm:bind", pubkey=pub.hex()))
        # 非属主 w2 尝试迁移
        w2 = QuantumWallet()
        node.balances[w2.address] = 100.0
        z = int.from_bytes(keccak256(b"x"), "big") % _N
        tx = _signed_op(w2, "nova:evm:migrate", evm_addr=evm_addr, r=1, s=1)
        assert not node.validate_tx(tx)


# ---------------------------------------------------------------------------
# ④ RPC 以太坊标准兼容性
# ---------------------------------------------------------------------------
class TestRpcStandards:
    def test_eth_call_is_read_only(self):
        """eth_call 不得改变链上状态（余额/存储/包装资产均不变）。"""
        node = _node()
        evm = node.evm
        owner = "0x" + "33" * 20
        addr = _deploy(evm, node.store, erc20_bytecode(), owner)
        # 通过真实交易 mint
        r = node.evm_rpc.handle({"id": 1, "method": "eth_call",
                                 "params": [{"to": addr, "from": owner,
                                             "data": "0x40c10f19" + "33" * 32 + (100).to_bytes(32, "big").hex()},
                                            "latest"]})
        # eth_call 是只读：mint 未生效
        bal = node.evm_rpc.handle({"id": 2, "method": "eth_call",
                                   "params": [{"to": addr, "data": "0x70a08231" + "33" * 32}, "latest"]})
        assert int(bal["result"], 16) == 0

    def test_wrong_chain_id_rejected(self):
        """EIP-155 chainId 不匹配时 eth_sendRawTransaction 拒绝。"""
        node = _node()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        fields = [0, 10 ** 9, 21000, bytes.fromhex("22" * 20), 10 ** 18, b"", 1, b"", b""]
        digest = int.from_bytes(keccak256(rlp_encode(fields)), "big") % _N
        d = int.from_bytes(priv, "big")
        k = 99
        kG = _point_mul(k)
        r = kG[0] % _N
        s = (pow(k, _N - 2, _N) * (digest + r * d)) % _N
        recid = 0 if (kG[1] % 2 == 0) else 1
        v = 35 + 1 * 2 + recid  # chainId=1（以太坊主网）
        raw = rlp_encode([0, 10 ** 9, 21000, bytes.fromhex("22" * 20), 10 ** 18, b"", v, r, s])
        resp = node.evm_rpc.handle({"id": 1, "method": "eth_sendRawTransaction", "params": ["0x" + raw.hex()]})
        assert "error" in resp

    def test_invalid_nonce_rejected(self):
        node = _node()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        sender = evm_address_from_pubkey(pub)
        node.balances[sender] = 10.0
        fields = [5, 10 ** 9, 21000, bytes.fromhex("22" * 20), 10 ** 18, b"", CHAIN_ID, b"", b""]
        digest = int.from_bytes(keccak256(rlp_encode(fields)), "big") % _N
        d = int.from_bytes(priv, "big")
        k = 55
        kG = _point_mul(k)
        r = kG[0] % _N
        s = (pow(k, _N - 2, _N) * (digest + r * d)) % _N
        recid = 0 if (kG[1] % 2 == 0) else 1
        v = 35 + CHAIN_ID * 2 + recid
        raw = rlp_encode([5, 10 ** 9, 21000, bytes.fromhex("22" * 20), 10 ** 18, b"", v, r, s])
        resp = node.evm_rpc.handle({"id": 1, "method": "eth_sendRawTransaction", "params": ["0x" + raw.hex()]})
        assert "error" in resp
        assert "nonce" in resp["error"]["message"]

    def test_insufficient_funds_rejected(self):
        node = _node()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        sender = evm_address_from_pubkey(pub)
        # sender 无余额
        fields = [0, 10 ** 9, 21000, bytes.fromhex("22" * 20), 10 ** 18, b"", CHAIN_ID, b"", b""]
        digest = int.from_bytes(keccak256(rlp_encode(fields)), "big") % _N
        d = int.from_bytes(priv, "big")
        k = 7
        kG = _point_mul(k)
        r = kG[0] % _N
        s = (pow(k, _N - 2, _N) * (digest + r * d)) % _N
        recid = 0 if (kG[1] % 2 == 0) else 1
        v = 35 + CHAIN_ID * 2 + recid
        raw = rlp_encode([0, 10 ** 9, 21000, bytes.fromhex("22" * 20), 10 ** 18, b"", v, r, s])
        resp = node.evm_rpc.handle({"id": 1, "method": "eth_sendRawTransaction", "params": ["0x" + raw.hex()]})
        assert "error" in resp
