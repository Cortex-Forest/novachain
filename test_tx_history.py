import asyncio
import os
import tempfile
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9961)
    kw.setdefault("rpc", 8361)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


async def _make_client(node):
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    return TestClient(TestServer(app))


async def _send(client, sender, receiver, amt, memo):
    ts = int(time.time())
    pub = sender.public_key_hex()
    sig = sender.sign(f"{sender.address}{receiver}{amt}{ts}[]{memo}{pub}")
    async with client.post("/api/send", json={
        "sender": sender.address, "receiver": receiver, "amount": amt,
        "timestamp": ts, "parents": [], "data": memo,
        "sender_public_key": pub, "signature": sig,
    }) as r:
        assert r.status == 200, await r.text()
        return (await r.json())["txid"]


async def test_ledger_records_and_queries():
    node = _node()
    wallet = QuantumWallet()
    node.balances[wallet.address] = 1000
    client = await _make_client(node)
    async with client:
        to = "0x" + "ab" * 20
        txid = await _send(client, wallet, to, 5, "hello")

        entry = node.store.tx_history.get(txid)
        assert entry is not None, "交易未写入账本"
        assert entry["sender"] == wallet.address
        assert entry["receiver"] == to
        assert entry["amount"] == 5
        assert entry["gas"] == node.economy.FIXED_GAS
        assert entry["data"] == "hello"

        async with client.get(f"/api/txs/{wallet.address}") as r:
            assert r.status == 200, await r.text()
            body = await r.json()
        assert len(body["txs"]) == 1 and body["txs"][0]["txid"] == txid

        async with client.get(f"/api/txs/{to}") as r:
            body2 = await r.json()
        assert len(body2["txs"]) == 1

        async with client.get(f"/api/txs/0x{'cd' * 20}") as r:
            body3 = await r.json()
        assert body3["txs"] == []

        async with client.get(f"/api/tx/{txid}") as r:
            assert r.status == 200, await r.text()
            detail = await r.json()
        assert detail["txid"] == txid and detail["receiver"] == to

        async with client.get("/api/tx/" + "ff" * 32) as r:
            assert r.status == 404

        async with client.get("/api/txs/not-an-addr") as r:
            assert r.status == 400

        txid2 = await _send(client, wallet, to, 3, "second")
        async with client.get(f"/api/txs/{wallet.address}") as r:
            body4 = await r.json()
        assert [t["txid"] for t in body4["txs"]] == [txid2, txid]


async def test_ledger_persists_after_restart():
    with tempfile.TemporaryDirectory() as d:
        state_path = os.path.join(d, "chain_state.json")
        node = _node(state_file=state_path)
        wallet = QuantumWallet()
        node.balances[wallet.address] = 100
        client = await _make_client(node)
        async with client:
            to = "0x" + "12" * 20
            txid = await _send(client, wallet, to, 2, "persist")
        node.save_state()

        node2 = _node(state_file=state_path)
        entry = node2.store.tx_history.get(txid)
        assert entry is not None and entry["amount"] == 2
        assert txid in node2.dag


def run():
    asyncio.run(test_ledger_records_and_queries())
    asyncio.run(test_ledger_persists_after_restart())
    print("tx-history-test: ok")


if __name__ == "__main__":
    run()
