# -*- coding: utf-8 -*-
"""索引器/浏览器测试：进程内节点 -> 出块 -> 索引 -> GraphQL/REST/搜索断言。"""
import asyncio
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from core.vm import deploy_address
from explorer.db import SQLiteDB, connect_db
from explorer.graphql import GraphQL
from explorer.indexer import ChainIndexer
from explorer.server import create_app
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9977)
    kw.setdefault("rpc", 8391)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    kw.setdefault("genesis", None)
    return NovaNode(**kw)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


def _signed_tx(w, amount=0.0, receiver=None, data=""):
    ts = int(time.time())
    tx = Tx(w.address, receiver or w.address, amount, [], data,
            w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _broadcast(node, tx, system=False):
    asyncio.run(node.broadcast_tx(tx, system=system))


def _deploy(node, creator_wallet, bytecode="PUSH 1 RETURN"):
    addr = deploy_address(bytecode)
    tx = Tx("0x0000", addr, 0, [], bytecode)
    node.store.dag.add(tx.txid)
    node.store.contracts[addr] = bytecode
    node.store.contract_creator[addr] = creator_wallet.address
    node.store.deploy_count += 1
    _broadcast(node, tx, system=True)
    return addr


def _seed_chain(node):
    """构造一条含 转账 + 部署 + 质押 的链，返回相关地址。"""
    alice = QuantumWallet()
    bob = QuantumWallet()
    _fund(node, alice.address, 50000.0)
    _fund(node, bob.address, 20000.0)
    # 普通转账
    _broadcast(node, _signed_tx(alice, amount=100.0, receiver=bob.address))
    _broadcast(node, _signed_tx(bob, amount=50.0, receiver=alice.address))
    # 合约部署（creator = alice）
    contract = _deploy(node, alice)
    # 质押操作（sender == receiver）
    _broadcast(node, _signed_tx(alice, amount=500.0, data="nova:stake"))
    # 封块
    node.consensus.produce_block()
    return alice, bob, contract


def _node_with_chain():
    node = _node()
    alice, bob, contract = _seed_chain(node)
    return node, alice, bob, contract


def test_db_schema_and_upsert():
    db = SQLiteDB(":memory:")
    db.upsert_block({"height": 0, "hash": "h0", "prev_hash": "0" * 64,
                     "proposer": "n1", "timestamp": 1.0, "txids": ["t0", "t1"]})
    db.upsert_block({"height": 1, "hash": "h1", "prev_hash": "h0",
                     "proposer": "n2", "timestamp": 2.0, "txids": ["t2"]})
    assert db.latest_height() == 1
    db.upsert_tx({"txid": "t0", "sender": "0xa", "receiver": "0xb",
                  "amount": 1.0, "gas": 0.1, "data": "", "ts": 1.0}, 0)
    db.upsert_tx({"txid": "t2", "sender": "0xb", "receiver": "0xa",
                  "amount": 2.0, "gas": 0.1, "data": "", "ts": 2.0}, 1)
    db.upsert_balance("0xa", 10.0)
    db.upsert_nft("0xa", "nova:did:creator", 3.0)
    db.recompute_addresses()
    row = db.address("0xa")
    assert row["tx_count"] == 2        # 既是发送方也是接收方
    assert row["balance"] == 10.0
    assert row["nft_count"] == 1
    assert len(db.txs_of_address("0xa")["txs"]) == 2
    # 幂等：重复写入不报错
    db.upsert_block({"height": 1, "hash": "h1", "prev_hash": "h0",
                     "proposer": "n2", "timestamp": 2.0, "txids": ["t2"]})
    assert db.latest_height() == 1
    # 搜索：区块高度
    res = db.search("1")
    assert any(r["type"] == "block" and r["id"] == 1 for r in res)
    db.close()


def test_indexer_inprocess_sync():
    node, alice, bob, contract = _node_with_chain()
    db = SQLiteDB(":memory:")
    indexer = ChainIndexer(db=db, node=node)
    counts = asyncio.run(indexer.sync_once())
    # 封块后有 3 笔交易（2 转账 + 1 部署）
    assert counts["txs"] == 4
    assert counts["blocks"] == 1
    assert counts["contracts"] == 1
    assert db.latest_height() == 0
    # 交易按时间倒序返回
    res = db.txs(limit=10)
    assert res["total"] == 4
    ts_list = [t["ts"] for t in res["txs"]]
    assert ts_list == sorted(ts_list, reverse=True)
    # 部署交易归属区块
    rows = db.txs_by_height(0)
    assert len(rows) == 4
    # 合约部署时间已从部署交易推导
    c = db.contract(contract)
    assert c is not None and c["created_at"] > 0
    # 地址聚合
    arow = db.address(alice.address)
    assert arow is not None and arow["tx_count"] >= 2
    # 统计
    st = db.stats()
    assert st["height"] == 1   # 链上高度（已封 1 块）
    assert st["total_txs"] == 4
    # 增量同步幂等：再次同步不新增
    counts2 = asyncio.run(indexer.sync_once())
    assert counts2["txs"] == 0 and counts2["blocks"] == 0
    db.close()


def test_indexer_incremental_new_block():
    node, alice, bob, contract = _node_with_chain()
    db = SQLiteDB(":memory:")
    indexer = ChainIndexer(db=db, node=node)
    asyncio.run(indexer.sync_once())
    assert db.latest_height() == 0
    # 新交易 + 新块
    _broadcast(node, _signed_tx(alice, amount=10.0, receiver=bob.address))
    node.consensus.produce_block()
    counts = asyncio.run(indexer.sync_once())
    assert counts["blocks"] == 1
    assert counts["txs"] == 1
    assert db.latest_height() == 1
    b = db.block_by_height(1)
    assert b["tx_count"] == 1
    tx = db.txs_by_height(1)[0]
    assert tx["sender"] == alice.address
    db.close()


def test_graphql_queries():
    node, alice, bob, contract = _node_with_chain()
    db = SQLiteDB(":memory:")
    asyncio.run(ChainIndexer(db=db, node=node).sync_once())
    gql = GraphQL(db, node)

    res = gql.execute("{ stats { height totalTxs totalContracts totalStaked } }")
    assert "errors" not in res
    st = res["data"]["stats"]
    assert st["totalTxs"] == 4 and st["totalContracts"] == 1

    res = gql.execute("{ blocks(first: 5) { height hash txCount } }")
    assert len(res["data"]["blocks"]) == 1
    assert res["data"]["blocks"][0]["txCount"] == 4

    res = gql.execute("{ block(height: 0) { height hash txs { txid sender amount } } }")
    b = res["data"]["block"]
    assert b["height"] == 0 and len(b["txs"]) == 4

    some_tx = db.txs()["txs"][0]
    res = gql.execute('{ tx(txid: "%s") { sender receiver block { height } } }' % some_tx["txid"])
    t = res["data"]["tx"]
    assert t["sender"] == some_tx["sender"]
    assert t["block"]["height"] == some_tx["block_height"]

    res = gql.execute('{ address(addr: "%s") { balance txCount txs { txid } } }' % alice.address)
    a = res["data"]["address"]
    assert a["txCount"] >= 2 and len(a["txs"]) >= 2
    assert a["balance"] is not None

    res = gql.execute('{ contract(address: "%s") { creator created_at call_count } }' % contract)
    assert res["data"]["contract"]["creator"] == alice.address

    res = gql.execute('{ search(q: "0") { results { type id label } } }')
    assert "errors" not in res

    res = gql.execute("{ unknownField }")
    assert "errors" in res
    db.close()


def test_rest_api_and_cache():
    node, alice, bob, contract = _node_with_chain()
    db = SQLiteDB(":memory:")
    app = create_app(db=db, node=node, auto_sync=False)
    asyncio.run(app["_indexer"].sync_once())

    async def run():
        from aiohttp import ClientSession, web
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = "http://127.0.0.1:%d" % port
        try:
            async with ClientSession() as client:
                async def get(path):
                    async with client.get(base + path) as r:
                        return r.status, await r.json()

                st, body = await get("/api/stats")
                assert st == 200 and body["stats"]["total_txs"] == 4

                st, body = await get("/api/blocks?limit=5")
                assert len(body["blocks"]) == 1 and body["blocks"][0]["tx_count"] == 4

                st, body = await get("/api/block/0")
                assert body["block"]["height"] == 0 and len(body["txs"]) == 4

                st, body = await get("/api/txs?limit=2&offset=0")
                assert body["total"] == 4 and len(body["txs"]) == 2

                txid = body["txs"][0]["txid"]
                st, body = await get("/api/tx/" + txid)
                assert body["tx"]["txid"] == txid

                st, body = await get("/api/address/" + alice.address)
                ad = body["address"]
                assert 49000 < ad["balance"] < 50000.0   # 扣转账/质押手续费后略低于初始值
                assert ad["reputation"] is not None       # 进程内节点实时声誉分
                assert len(ad["txs"]) >= 2
                assert any(c["address"] == contract for c in ad["contracts"])

                st, body = await get("/api/contract/" + contract)
                assert body["contract"]["creator"] == alice.address

                st, body = await get("/api/search?q=" + str(node.consensus.chain_height() - 1))
                assert any(x["type"] == "block" for x in body["results"])

                st, body = await get("/api/search?q=" + txid[:16])
                assert any(x["type"] == "tx" for x in body["results"])

                # GraphQL 走 HTTP
                async with client.post(base + "/graphql",
                                       json={"query": "{ stats { height } }"}) as r:
                    body = await r.json()
                assert body["data"]["stats"]["height"] == 1

                # 缓存：第二次命中（返回新建的 Response，不挂起）
                before = app["_cache"]
                st2, body2 = await get("/api/stats")
                assert st2 == 200 and before.get("/api/stats?[]") is not None

                # 404
                async with client.get(base + "/api/block/999") as r:
                    assert r.status == 404
        finally:
            await runner.cleanup()

    asyncio.run(run())
    db.close()

def test_remote_payload_shape():
    """远端同步载荷与进程内载荷字段一致（保证 RPC /api/chain/sync 可被索引）。"""
    node, alice, bob, contract = _node_with_chain()
    indexer = ChainIndexer(node=node)
    payload = indexer.build_payload()
    assert {"blocks", "txs", "contracts", "balances", "soulbound", "stats"} <= set(payload)
    assert payload["stats"]["total_txs"] == 4
    assert payload["stats"]["total_addresses"] > 0
    db = SQLiteDB(":memory:")
    counts = indexer.apply_payload(payload)
    assert counts["txs"] == 4 and counts["contracts"] == 1
    db.close()


def test_connect_db():
    db = connect_db("sqlite:///:memory:")
    assert isinstance(db, SQLiteDB)
    db.upsert_balance("0xabc", 5.0)
    assert db.address("0xabc")["balance"] == 5.0
    db.close()





