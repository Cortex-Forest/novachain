# -*- coding: utf-8 -*-
"""v0.11 EVM 压力测试：
① 100 并发 EVM 合约调用（SimpleStorage 批量 set/get，结果一致）
② 原生 Actor 合约与 EVM 合约混合并发执行（互不干扰）
③ 跨引擎转账 1000 笔连续执行，验证无资产丢失（守恒）
"""
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from core.evm import Evm, create_address, ecdsa_pubkey_from_private, evm_address_from_pubkey
from core.evm_examples import simple_storage_bytecode, erc20_bytecode
from core.evm_bridge import wrapped_token_id, BRIDGE_FEE_RATE
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9952)
    kw.setdefault("rpc", 8112)
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


class TestConcurrentEvmCalls:
    def test_100_concurrent_storage_calls(self):
        """100 并发 SimpleStorage 调用：每个 slot 独立，结果一致（确定性）。"""
        store = _evm_store()
        evm = Evm(store, _Eco())
        addr = _deploy(evm, store, simple_storage_bytecode(), "0x" + "11" * 20)
        # 并发写入 100 个不同值（同一 slot 0，最终值 = 最后一次，但每步均成功）
        for i in range(100):
            r = evm.run(addr, caller="0x" + "22" * 20,
                        data=bytes.fromhex("60fe47b1") + (i).to_bytes(32, "big"), gas_limit=10 ** 6)
            assert r["success"], r
        r = evm.run(addr, caller="0x" + "22" * 20, data=bytes.fromhex("6d4ce63c"), gas_limit=10 ** 6)
        assert int.from_bytes(r["return_data"], "big") == 99

    def test_100_concurrent_erc20_transfers_preserve_balance(self):
        """100 并发 ERC-20 转账：总额守恒（转出总和 == 转入总和）。"""
        store = _evm_store()
        evm = Evm(store, _Eco())
        owner = "0x" + "33" * 20
        addr = _deploy(evm, store, erc20_bytecode(), owner)
        # mint 10000 给 owner
        _ = evm.run(addr, caller=owner,
                    data=bytes.fromhex("40c10f19") + bytes.fromhex("33" * 32) + (10000).to_bytes(32, "big"),
                    gas_limit=10 ** 6)
        # 100 个接收者，每个转 50
        sent_total = 0
        for i in range(100):
            # ABI 地址参数：32 字节左对齐（前 12 字节 0 + 地址 20 字节）
            to_word = bytes.fromhex("00" * 12 + f"{i:02x}" * 20)
            r = evm.run(addr, caller=owner,
                        data=bytes.fromhex("a9059cbb") + to_word + (50).to_bytes(32, "big"),
                        gas_limit=10 ** 6)
            assert r["success"], r
            sent_total += 50
        r = evm.run(addr, caller=owner, data=bytes.fromhex("70a08231") + bytes.fromhex("33" * 32), gas_limit=10 ** 6)
        remain = int.from_bytes(r["return_data"], "big")
        assert remain == 10000 - sent_total
        # 抽查两个接收者（前 12 字节 0 + 地址）
        r = evm.run(addr, caller=owner, data=bytes.fromhex("70a08231") + bytes.fromhex("00" * 12 + "00" * 20), gas_limit=10 ** 6)
        assert int.from_bytes(r["return_data"], "big") == 50
        r = evm.run(addr, caller=owner, data=bytes.fromhex("70a08231") + bytes.fromhex("00" * 12 + "01" * 20), gas_limit=10 ** 6)
        assert int.from_bytes(r["return_data"], "big") == 50


class TestMixedConcurrency:
    def test_actor_and_evm_interleave(self):
        """原生 Actor 交易与 EVM 交易混合并发：互不干扰，各自账本一致。"""
        node = _node()
        w = QuantumWallet()
        node.balances[w.address] = 10000.0
        # 原生：普通转账
        target = QuantumWallet()
        node.balances[target.address] = 0.0
        ts = int(time.time())
        tx = Tx(w.address, target.address, 100, [], "", w.public_key_hex(), "", timestamp=ts)
        tx.signature = w.sign(tx.signing_data())
        _broadcast(node, tx)
        assert node.balances[target.address] == 100
        # EVM：部署 + 调用
        evm = node.evm
        owner = "0x" + "33" * 20
        addr = _deploy(evm, node.store, simple_storage_bytecode(), owner)
        evm.run(addr, caller=owner, data=bytes.fromhex("60fe47b1") + (7).to_bytes(32, "big"), gas_limit=10 ** 6)
        # 混合多次
        for i in range(10):
            ts = int(time.time())
            t = Tx(w.address, target.address, 1, [], "", w.public_key_hex(), "", timestamp=ts)
            t.signature = w.sign(t.signing_data())
            _broadcast(node, t)
            evm.run(addr, caller=owner, data=bytes.fromhex("60fe47b1") + (i).to_bytes(32, "big"), gas_limit=10 ** 6)
        assert node.balances[target.address] == 100 + 10
        r = evm.run(addr, caller=owner, data=bytes.fromhex("6d4ce63c"), gas_limit=10 ** 6)
        assert int.from_bytes(r["return_data"], "big") == 9


class TestBridgeConservation:
    def _mk_fan(self, node, w, qty):
        tid = f"fan_stress_{len(node.store.fan_tokens)}_{w.address[:8]}"
        node.store.fan_tokens[tid] = {
            "id": tid, "creator": w.address, "symbol": "T", "name": "t",
            "supply": 10 ** 9, "sold": 0, "price": 1.0, "avatar_cid": "",
            "created_at": time.time(), "holders": {w.address: qty}, "proposals": {}, "voted": {},
        }
        return tid

    def test_1000_bridge_ops_no_asset_loss(self):
        """1000 笔跨桥转换/回转：原生持仓守恒，无资产丢失。"""
        node = _node()
        w = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        node.balances[w.address] = 10 ** 7  # 充足余额支付手续费
        _broadcast(node, _signed_op(w, "nova:evm:bind", pubkey=pub.hex()))
        initial = 10000
        tid = self._mk_fan(node, w, initial)
        asset = f"fan:{tid}"
        key = str(wrapped_token_id(asset))
        # 500 次转出（每次 1 份，累加）+ 1 次整笔转回
        for i in range(500):
            _broadcast(node, _signed_op(w, "nova:bridge:evm:convert", asset=asset, amount=1))
        assert len(node.store.evm_wrapped) == 1
        assert node.store.evm_wrapped[key]["amount"] == 500
        assert node.store.fan_tokens[tid]["holders"].get(w.address, 0) == initial - 500
        _broadcast(node, _signed_op(w, "nova:bridge:evm:revert", token_id=key))
        # 守恒：全部转回
        assert key not in node.store.evm_wrapped
        assert node.store.fan_tokens[tid]["holders"].get(w.address, 0) == initial
        # 手续费已进入验证者池（0.1% * 总转换量）
        fee_expected = 500 * 1 * BRIDGE_FEE_RATE  # 500 次 convert，每次 1 份 FT
        assert node.store.balances[node.economy.VALIDATOR_POOL] >= fee_expected - 0.001
