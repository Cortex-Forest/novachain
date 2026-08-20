# -*- coding: utf-8 -*-
"""v0.11 EVM 兼容性测试：
① 密码学原语：Keccak-256 / RLP / ECDSA 验签与公钥恢复（官方向量子集）
② EVM 解释器：SimpleStorage / ERC-20 标准方法全流程回归
③ MetaMask RPC：eth_chainId / eth_getBalance / eth_call / eth_sendRawTransaction / 回执
④ 混合账户：nova:evm:bind / nova:evm:migrate（共享 NOVA 余额）
⑤ 跨引擎桥接：原生 NFT -> EVM 包装 -> revert（原子性 / 手续费回流验证者池）
"""
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from core.evm import (Evm, CHAIN_ID, keccak256, rlp_encode, rlp_decode, ecdsa_verify,
                      ecdsa_pubkey_from_private, evm_address_from_pubkey, create_address,
                      decode_signed_tx, _point_mul, _N, _recover_pubkey, nova_to_wei,
                      wei_to_nova, checksum_address)
from core.evm_examples import simple_storage_bytecode, erc20_bytecode
from core.evm_bridge import EVM_BRIDGE, wrapped_token_id_hex
from nova_node import NovaNode


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9950)
    kw.setdefault("rpc", 8110)
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
    assert node.validate_tx(tx), "交易校验失败"
    node.store.dag.add(tx.txid)
    node.apply_tx(tx)
    node._record_tx(tx)


# ---------------------------------------------------------------------------
# ① 密码学原语
# ---------------------------------------------------------------------------
class TestPrimitives:
    def test_keccak256_vectors(self):
        assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
        assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"

    def test_rlp_roundtrip(self):
        for obj in [b"dog", b"", [b"cat", b"dog"], b"\x7f" * 100, [b"a", [b"b", [b"c"]]]]:
            assert rlp_decode(rlp_encode(obj)) == obj

    def test_ecdsa_sign_verify_and_recover(self):
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        msg_hash = keccak256(b"hello nova")
        z = int.from_bytes(msg_hash, "big") % _N
        d = int.from_bytes(priv, "big")
        k = 123456789
        kG = _point_mul(k)
        r = kG[0] % _N
        s = (pow(k, _N - 2, _N) * (z + r * d)) % _N
        assert ecdsa_verify(pub, msg_hash, r, s)
        assert not ecdsa_verify(pub, keccak256(b"tampered"), r, s)
        recid = 0 if (kG[1] % 2 == 0) else 1
        assert _recover_pubkey(msg_hash, r, s, recid) == pub
        assert evm_address_from_pubkey(pub).startswith("0x") and len(evm_address_from_pubkey(pub)) == 42

    def test_checksum_address(self):
        # EIP-55 已知示例
        assert checksum_address("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed") == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"

    def test_decode_signed_tx(self):
        """构造 EIP-155 签名交易并解码，恢复 from 地址与 chainId 校验。"""
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        sender = evm_address_from_pubkey(pub)
        nonce, gas_price, gas_limit = 0, 10 ** 9, 21000
        to = "0x" + "22" * 20
        value, data = 10 ** 18, b""
        fields = [nonce, gas_price, gas_limit, bytes.fromhex(to[2:]), value, data, CHAIN_ID, b"", b""]
        digest = int.from_bytes(keccak256(rlp_encode(fields)), "big") % _N
        d = int.from_bytes(priv, "big")
        k = 424242
        kG = _point_mul(k)
        r = kG[0] % _N
        s = (pow(k, _N - 2, _N) * (digest + r * d)) % _N
        recid = 0 if (kG[1] % 2 == 0) else 1
        v = 35 + CHAIN_ID * 2 + recid
        raw = rlp_encode([nonce, gas_price, gas_limit, bytes.fromhex(to[2:]), value, data, v, r, s])
        tx = decode_signed_tx("0x" + raw.hex())
        assert tx["from"] == sender
        assert tx["chain_id"] == CHAIN_ID
        assert tx["to"] == to
        assert tx["value"] == value
        assert tx["nonce"] == nonce


# ---------------------------------------------------------------------------
# ② EVM 解释器：SimpleStorage / ERC-20
# ---------------------------------------------------------------------------
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


def _call(evm, addr, caller, data):
    r = evm.run(addr, caller=caller, data=data, gas_limit=10 ** 6)
    assert r["success"], r
    return r["return_data"]


class TestEvmInterpreter:
    def test_simple_storage_set_get(self):
        store = _evm_store()
        evm = Evm(store, _Eco())
        addr = _deploy(evm, store, simple_storage_bytecode(), "0x" + "11" * 20)
        _call(evm, addr, "0x" + "22" * 20,
              bytes.fromhex("60fe47b1") + (42).to_bytes(32, "big"))
        ret = _call(evm, addr, "0x" + "22" * 20, bytes.fromhex("6d4ce63c"))
        assert int.from_bytes(ret, "big") == 42

    def test_erc20_balance_transfer(self):
        store = _evm_store()
        evm = Evm(store, _Eco())
        owner = "0x" + "33" * 20
        user = "0x" + "44" * 20
        bob = "0x" + "55" * 20
        addr = _deploy(evm, store, erc20_bytecode(), owner)
        # mint owner 1000
        _call(evm, addr, owner, bytes.fromhex("40c10f19") + bytes.fromhex("33" * 32) + (1000).to_bytes(32, "big"))
        ret = _call(evm, addr, owner, bytes.fromhex("70a08231") + bytes.fromhex("33" * 32))
        assert int.from_bytes(ret, "big") == 1000
        # transfer owner -> bob 300
        _call(evm, addr, owner, bytes.fromhex("a9059cbb") + bytes.fromhex("55" * 32) + (300).to_bytes(32, "big"))
        ret = _call(evm, addr, owner, bytes.fromhex("70a08231") + bytes.fromhex("33" * 32))
        assert int.from_bytes(ret, "big") == 700
        ret = _call(evm, addr, owner, bytes.fromhex("70a08231") + bytes.fromhex("55" * 32))
        assert int.from_bytes(ret, "big") == 300

    def test_erc20_approve_allowance_transferFrom(self):
        store = _evm_store()
        evm = Evm(store, _Eco())
        owner = "0x" + "33" * 20
        user = "0x" + "44" * 20
        addr = _deploy(evm, store, erc20_bytecode(), owner)
        _call(evm, addr, owner, bytes.fromhex("40c10f19") + bytes.fromhex("33" * 32) + (1000).to_bytes(32, "big"))
        _call(evm, addr, owner, bytes.fromhex("095ea7b3") + bytes.fromhex("44" * 32) + (500).to_bytes(32, "big"))
        ret = _call(evm, addr, user, bytes.fromhex("dd62ed3e") + bytes.fromhex("33" * 32) + bytes.fromhex("44" * 32))
        assert int.from_bytes(ret, "big") == 500
        _call(evm, addr, user, bytes.fromhex("23b872dd") + bytes.fromhex("33" * 32) + bytes.fromhex("44" * 32) + (200).to_bytes(32, "big"))
        ret = _call(evm, addr, user, bytes.fromhex("dd62ed3e") + bytes.fromhex("33" * 32) + bytes.fromhex("44" * 32))
        assert int.from_bytes(ret, "big") == 300
        ret = _call(evm, addr, owner, bytes.fromhex("70a08231") + bytes.fromhex("44" * 32))
        assert int.from_bytes(ret, "big") == 200

    def test_erc20_metadata(self):
        store = _evm_store()
        evm = Evm(store, _Eco())
        owner = "0x" + "33" * 20
        addr = _deploy(evm, store, erc20_bytecode(), owner)
        for sel, expected in [("06fdde03", "NovaToken"), ("95d89b41", "NVT")]:
            ret = _call(evm, addr, owner, bytes.fromhex(sel))
            off = int.from_bytes(ret[0:32], "big")
            ln = int.from_bytes(ret[32:64], "big")
            assert ret[off:off + ln].decode() == expected
        ret = _call(evm, addr, owner, bytes.fromhex("313ce567"))
        assert int.from_bytes(ret, "big") == 18


# ---------------------------------------------------------------------------
# ③ MetaMask RPC（挂载在节点上）
# ---------------------------------------------------------------------------
class TestMetaMaskRpc:
    def test_network_info(self):
        node = _node()
        rpc = node.evm_rpc
        assert rpc.handle({"id": 1, "method": "eth_chainId", "params": []})["result"] == hex(CHAIN_ID)
        assert rpc.handle({"id": 1, "method": "net_version", "params": []})["result"] == str(CHAIN_ID)
        assert rpc.handle({"id": 1, "method": "eth_blockNumber", "params": []})["result"].startswith("0x")
        assert rpc.handle({"id": 1, "method": "eth_gasPrice", "params": []})["result"] == "0x3b9aca00"
        assert rpc.handle({"id": 1, "method": "eth_syncing", "params": []})["result"] is False

    def test_get_balance(self):
        node = _node()
        node.balances["0x" + "aa" * 20] = 100.0
        r = node.evm_rpc.handle({"id": 1, "method": "eth_getBalance",
                                 "params": ["0x" + "aa" * 20, "latest"]})
        assert int(r["result"], 16) == 100 * 10 ** 18

    def test_eth_call_erc20(self):
        node = _node()
        evm = node.evm
        owner = "0x" + "33" * 20
        store = node.store
        addr = _deploy(evm, store, erc20_bytecode(), owner)
        # mint
        _call(evm, addr, owner, bytes.fromhex("40c10f19") + bytes.fromhex("33" * 32) + (1000).to_bytes(32, "big"))
        # eth_call balanceOf
        data = bytes.fromhex("70a08231") + bytes.fromhex("33" * 32)
        r = node.evm_rpc.handle({"id": 1, "method": "eth_call",
                                 "params": [{"to": addr, "data": "0x" + data.hex()}, "latest"]})
        assert int(r["result"], 16) == 1000

    def test_eth_send_raw_transaction_and_receipt(self):
        """eth_sendRawTransaction -> DAG 同步 -> 标准回执 -> 余额变更共享。"""
        node = _node()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        sender = evm_address_from_pubkey(pub)
        node.balances[sender] = 10.0  # 共享同一 NOVA 账本
        to = "0x" + "22" * 20
        nonce, gas_price, gas_limit = 0, 10 ** 9, 21000
        value = int(1.5 * 10 ** 18)
        fields = [nonce, gas_price, gas_limit, bytes.fromhex(to[2:]), value, b"", CHAIN_ID, b"", b""]
        digest = int.from_bytes(keccak256(rlp_encode(fields)), "big") % _N
        d = int.from_bytes(priv, "big")
        k = 777
        kG = _point_mul(k)
        r = kG[0] % _N
        s = (pow(k, _N - 2, _N) * (digest + r * d)) % _N
        recid = 0 if (kG[1] % 2 == 0) else 1
        v = 35 + CHAIN_ID * 2 + recid
        raw = rlp_encode([nonce, gas_price, gas_limit, bytes.fromhex(to[2:]), value, b"", v, r, s])
        raw_hex = "0x" + raw.hex()
        resp = node.evm_rpc.handle({"id": 1, "method": "eth_sendRawTransaction", "params": [raw_hex]})
        txhash = resp.get("result")
        assert txhash and txhash.startswith("0x")
        # 回执
        rec = node.evm_rpc.handle({"id": 2, "method": "eth_getTransactionReceipt", "params": [txhash]})["result"]
        assert rec is not None
        assert rec["status"] == "0x1"
        assert rec["from"] == sender
        assert rec["to"] == to
        # 余额：sender 扣 1.5 + gas，receiver 加 1.5
        assert node.balances[to] == pytest.approx(1.5)
        assert node.balances[sender] == pytest.approx(10 - 1.5 - wei_to_nova(gas_price * 21000))
        # DAG 账本已同步
        assert txhash in node.store.dag
        assert txhash in node.store.tx_history


# ---------------------------------------------------------------------------
# ④ 混合账户
# ---------------------------------------------------------------------------
class TestHybridAccounts:
    def test_bind_and_info(self):
        node = _node()
        w = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        evm_addr = evm_address_from_pubkey(pub)
        node.balances[w.address] = 100.0
        _broadcast(node, _signed_op(w, "nova:evm:bind", pubkey=pub.hex()))
        assert node.store.evm_bindings.get(w.address) == evm_addr
        assert node.store.evm_accounts[evm_addr]["owner"] == w.address
        assert node.store.evm_accounts[evm_addr]["migrated"] is False

    def test_migrate_transfers_balance(self):
        node = _node()
        w = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        evm_addr = evm_address_from_pubkey(pub)
        node.balances[w.address] = 500.0
        _broadcast(node, _signed_op(w, "nova:evm:bind", pubkey=pub.hex()))
        # ECDSA 签名迁移（对确定性消息）
        ts = int(time.time())
        msg = f"nova:evm:migrate:{w.address}:{evm_addr}:{ts}"
        z = int.from_bytes(keccak256(msg.encode()), "big") % _N
        d = int.from_bytes(priv, "big")
        k = 888
        kG = _point_mul(k)
        r = kG[0] % _N
        s = (pow(k, _N - 2, _N) * (z + r * d)) % _N
        tx = _signed_op(w, "nova:evm:migrate", evm_addr=evm_addr, r=r, s=s, ts=ts)
        tx.timestamp = ts
        tx.txid = tx.calc_txid()
        _broadcast(node, tx)
        assert node.balances[w.address] == 0
        assert node.balances[evm_addr] == pytest.approx(500.0)
        assert node.store.evm_accounts[evm_addr]["migrated"] is True
        # 不可逆：再次迁移被拒绝
        tx2 = _signed_op(w, "nova:evm:migrate", evm_addr=evm_addr, r=r, s=s, ts=ts)
        tx2.timestamp = ts
        tx2.txid = tx2.calc_txid()
        assert not node.validate_tx(tx2)


# ---------------------------------------------------------------------------
# ⑤ 跨引擎桥接
# ---------------------------------------------------------------------------
class TestEvmBridge:
    def _mk_fan_token(self, node, w):
        # 直接用 store 构造一个粉丝代币持有（简化：模拟 nova:fan:issue/buy）
        tid = "fan_test_" + "1" * 20
        node.store.fan_tokens[tid] = {
            "id": tid, "creator": w.address, "symbol": "T", "name": "t",
            "supply": 100, "sold": 0, "price": 1.0, "avatar_cid": "",
            "created_at": time.time(), "holders": {w.address: 10}, "proposals": {}, "voted": {},
        }
        return tid

    def test_convert_and_revert(self):
        node = _node()
        w = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        evm_addr = evm_address_from_pubkey(pub)
        node.balances[w.address] = 100.0
        _broadcast(node, _signed_op(w, "nova:evm:bind", pubkey=pub.hex()))
        tid = self._mk_fan_token(node, w)
        asset = f"fan:{tid}"
        # convert 5 份
        _broadcast(node, _signed_op(w, "nova:bridge:evm:convert", asset=asset, amount=5))
        tid_key = str(wrapped_token_id_hex(asset).split("x")[1])
        # 实际 key 是十进制字符串（token_id int）
        from core.evm_bridge import wrapped_token_id
        key = str(wrapped_token_id(asset))
        rec = node.store.evm_wrapped.get(key)
        assert rec is not None, node.store.evm_wrapped
        assert rec["evm_owner"] == evm_addr
        assert rec["native_owner"] == w.address
        assert rec["amount"] == 5
        # 手续费 0.1% 回流验证者池
        assert node.store.balances[node.economy.VALIDATOR_POOL] > 0
        # 原生持有已扣减
        assert node.store.fan_tokens[tid]["holders"].get(w.address, 0) == 5
        # revert 转回
        node.balances[w.address] = 100.0
        _broadcast(node, _signed_op(w, "nova:bridge:evm:revert", token_id=key))
        assert key not in node.store.evm_wrapped
        assert node.store.fan_tokens[tid]["holders"].get(w.address, 0) == 10

    def test_bridge_fee_goes_to_validator_pool(self):
        node = _node()
        w = QuantumWallet()
        priv = bytes.fromhex("fad9c8855b740a0b7ed4c221dbad0f33a83a49cad6b3fe8d5817ac83d38b6a19")
        pub = ecdsa_pubkey_from_private(priv)
        node.balances[w.address] = 100.0
        _broadcast(node, _signed_op(w, "nova:evm:bind", pubkey=pub.hex()))
        tid = self._mk_fan_token(node, w)
        vp_before = node.store.balances.get(node.economy.VALIDATOR_POOL, 0.0)
        _broadcast(node, _signed_op(w, "nova:bridge:evm:convert", asset=f"fan:{tid}", amount=10))
        # NFT 固定 0.001 NOVA 手续费（此处 fan 视为 FT，0.1% of 10 = 0.01）
        from core.evm_bridge import BRIDGE_FEE_RATE
        assert node.store.balances[node.economy.VALIDATOR_POOL] == pytest.approx(
            vp_before + 10 * BRIDGE_FEE_RATE)
