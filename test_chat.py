import asyncio
import os
import tempfile
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from core.chat import (ChatStore, chat_signature_data, validate_chat_payload,
                       message_id, MAX_CIPHERTEXT_HEX)
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9981)
    kw.setdefault("rpc", 8311)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


async def _make_client(node):
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    return TestClient(TestServer(app))


def _chat_pub():
    return os.urandom(32).hex()


def _nonce():
    return os.urandom(12).hex()


def _msg_payload(sender, recipient):
    return {
        "sender": sender.address,
        "recipient": recipient.address,
        "chat_pub": _chat_pub(),
        "nonce": _nonce(),
        "ciphertext": "aabb" * 16,
        "ts": int(time.time()),
    }


def _sign_msg(wallet, payload):
    payload["sender_public_key"] = wallet.public_key_hex()
    payload["signature"] = wallet.sign(chat_signature_data(
        payload["sender"], payload["recipient"], payload["chat_pub"],
        payload["nonce"], payload["ciphertext"], payload["ts"]))
    return payload


# ---------------------------------------------------------------------------
# ChatStore
# ---------------------------------------------------------------------------

def test_chat_store_push_ack_persistence():
    store = ChatStore()
    alice = QuantumWallet()
    bob = QuantumWallet()
    p = _msg_payload(alice, bob)
    p["id"] = message_id(p["sender"], p["recipient"], p["chat_pub"],
                         p["nonce"], p["ciphertext"], p["ts"])
    assert store.push(p) is True
    assert store.push(p) is False          # 去重
    assert len(store.messages_for(bob.address)) == 1

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "chat.json")
        store.save(path)
        store2 = ChatStore()
        assert store2.load(path) is True
        assert len(store2.messages_for(bob.address)) == 1
        assert store2.messages_for(bob.address)[0]["ciphertext"] == p["ciphertext"]

    assert store.ack(bob.address, [p["id"]]) == 1
    assert store.ack(bob.address, [p["id"]]) == 0
    assert store.messages_for(bob.address) == []


def test_validate_chat_payload_checks():
    alice = QuantumWallet()
    bob = QuantumWallet()
    p = _msg_payload(alice, bob)

    assert validate_chat_payload(p) == ""

    bad = dict(p); bad["sender"] = "nope"
    assert validate_chat_payload(bad) == "地址格式无效"

    bad = dict(p); bad["chat_pub"] = "zz"
    assert validate_chat_payload(bad) == "聊天公钥无效"

    bad = dict(p); bad["nonce"] = "ab"
    assert validate_chat_payload(bad) == "nonce 无效"

    bad = dict(p); bad["ciphertext"] = "xx"
    assert validate_chat_payload(bad) == "密文格式无效"

    bad = dict(p); bad["ciphertext"] = "ab" * (MAX_CIPHERTEXT_HEX + 1)
    assert validate_chat_payload(bad) == "密文过长"

    bad = dict(p); bad["ts"] = int(time.time()) - 10**6
    assert validate_chat_payload(bad) == "时间戳过期"


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

async def test_rpc_chat_pubkey_roundtrip():
    node = _node()
    wallet = QuantumWallet()
    async with await _make_client(node) as client:
        # 签名覆盖的内容与提交的公钥不一致 -> 拒绝
        chat_pub_a = _chat_pub()
        chat_pub_b = _chat_pub()
        resp = await client.post("/api/chat/pubkey", json={
            "addr": wallet.address, "chat_pub": chat_pub_a,
            "sender_public_key": wallet.public_key_hex(),
            "signature": wallet.sign(wallet.address + chat_pub_b),
        })
        assert resp.status == 400

        chat_pub = _chat_pub()
        sig = wallet.sign(wallet.address + chat_pub)
        resp = await client.post("/api/chat/pubkey", json={
            "addr": wallet.address, "chat_pub": chat_pub,
            "sender_public_key": wallet.public_key_hex(), "signature": sig,
        })
        assert resp.status == 200

        resp = await client.get("/api/chat/pubkey/" + wallet.address)
        assert await resp.json() == {"addr": wallet.address, "chat_pub": chat_pub}


async def test_rpc_chat_send_inbox_ack():
    node = _node()
    alice = QuantumWallet()
    bob = QuantumWallet()
    async with await _make_client(node) as client:
        # 签名被篡改 -> 拒绝
        p = _msg_payload(alice, bob)
        p["sender_public_key"] = alice.public_key_hex()
        p["signature"] = "00" * 64
        resp = await client.post("/api/chat/send", json=p)
        assert resp.status == 400

        # 合法消息 -> 入箱
        p = _sign_msg(alice, _msg_payload(alice, bob))
        resp = await client.post("/api/chat/send", json=p)
        assert resp.status == 200
        mid = (await resp.json())["id"]

        # 地址格式错误 -> 拒绝
        bad = _sign_msg(alice, _msg_payload(alice, bob))
        bad["recipient"] = "0xbad"
        resp = await client.post("/api/chat/send", json=bad)
        assert resp.status == 400

        # 收件箱
        resp = await client.get("/api/chat/inbox/" + bob.address)
        body = await resp.json()
        assert [m["id"] for m in body["messages"]] == [mid]
        assert body["messages"][0]["ciphertext"] == p["ciphertext"]
        assert body["messages"][0]["sender"] == alice.address

        # 发件人自己看不到
        resp = await client.get("/api/chat/inbox/" + alice.address)
        body = await resp.json()
        assert body["messages"] == []

        # ack 后清除
        resp = await client.post("/api/chat/ack", json={"addr": bob.address, "ids": [mid]})
        assert (await resp.json())["removed"] == 1
        resp = await client.get("/api/chat/inbox/" + bob.address)
        assert (await resp.json())["messages"] == []


def test_chat_persists_with_node_state():
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "chain_state.json")
        node = _node(state_file=state)
        alice = QuantumWallet()
        bob = QuantumWallet()
        p = _sign_msg(alice, _msg_payload(alice, bob))
        p["id"] = message_id(p["sender"], p["recipient"], p["chat_pub"],
                             p["nonce"], p["ciphertext"], p["ts"])
        node.chat.push(p)
        node.save_state()
        assert os.path.exists(node.chat.chat_file)

        node2 = _node(state_file=state)
        assert len(node2.chat.messages_for(bob.address)) == 1
        assert node2.chat.messages_for(bob.address)[0]["id"] == p["id"]
