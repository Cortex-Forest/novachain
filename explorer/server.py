# -*- coding: utf-8 -*-
"""Nova 链浏览器 HTTP 服务。

- REST API：/api/blocks、/api/block/{height}、/api/txs、/api/tx/{txid}、
  /api/address/{addr}、/api/contract/{addr}、/api/search、/api/stats、/api/indexer/status
- GraphQL：GET/POST /graphql
- 常用查询 1 分钟缓存（TTLCache）；交易历史时间倒序 + 分页。

启动：python -m explorer --node-url http://127.0.0.1:8080 --db sqlite:///explorer.db
"""
import asyncio
import json
import time
from functools import wraps

from aiohttp import web

from .db import connect_db
from .graphql import GraphQL
from .indexer import ChainIndexer


class TTLCache:
    def __init__(self, ttl=60):
        self.ttl = ttl
        self._d = {}

    def get(self, key):
        item = self._d.get(key)
        if item and time.time() - item[1] < self.ttl:
            return item[0]
        return None

    def set(self, key, value):
        self._d[key] = (value, time.time())

    def clear(self):
        self._d.clear()


def cached(ttl=60):
    """读接口缓存装饰器：按 path + query 作为缓存键。

    缓存的是序列化后的响应数据（状态码/字节体/Content-Type），
    每次命中都新建 web.Response，避免复用已发送的响应对象导致挂起。
    """

    def deco(fn):
        @wraps(fn)
        async def wrapper(request):
            cache = request.app["_cache"]
            key = request.path + "?" + json.dumps(sorted(request.query.items()), ensure_ascii=False)
            hit = cache.get(key)
            if hit is not None:
                status, body, ctype = hit
                return web.Response(status=status, body=body, content_type=ctype)
            resp = await fn(request)
            if resp.status == 200:
                cache.set(key, (resp.status, resp.body, resp.content_type))
            return resp
        return wrapper
    return deco


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _int_arg(request, name, default, minimum=0):
    try:
        return max(minimum, int(request.query.get(name, default)))
    except (TypeError, ValueError):
        return default


def create_app(db=None, node_url=None, node=None, auto_sync=True, poll_interval=15):
    db = db or connect_db("sqlite:///:memory:")
    indexer = ChainIndexer(db=db, node_url=node_url, node=node, poll_interval=poll_interval)
    gql = GraphQL(db, node)

    app = web.Application()
    app["_db"] = db
    app["_indexer"] = indexer
    app["_gql"] = gql
    app["_cache"] = TTLCache(60)
    app["_sync_task"] = None

    # ---------------- 首页 / 状态 ----------------
    @cached(ttl=10)
    async def index(request):
        return _json({
            "service": "Nova 链浏览器索引器",
            "version": "1.0.0",
            "endpoints": ["/api/stats", "/api/blocks", "/api/block/{height}",
                          "/api/txs", "/api/tx/{txid}", "/api/address/{addr}",
                          "/api/contract/{addr}", "/api/search", "/graphql"],
        })

    async def indexer_status(request):
        return _json(indexer.status())

    async def manual_sync(request):
        counts = await indexer.sync_once()
        return _json({"ok": True, "counts": counts})

    # ---------------- 统计 / 区块 ----------------
    @cached(ttl=60)
    async def stats(request):
        return _json({"stats": db.stats()})

    @cached(ttl=60)
    async def blocks(request):
        limit = _int_arg(request, "limit", 20)
        limit = min(limit, 100)
        offset = _int_arg(request, "offset", 0)
        return _json({"blocks": db.recent_blocks(limit, offset), "total": db.latest_height() + 1})

    @cached(ttl=60)
    async def block_detail(request):
        try:
            height = int(request.match_info["height"])
        except (TypeError, ValueError):
            return _json({"error": "高度无效"}, status=400)
        b = db.block_by_height(height)
        if not b:
            return _json({"error": "not_found"}, status=404)
        return _json({"block": b, "txs": db.txs_by_height(height)})

    # ---------------- 交易 ----------------
    @cached(ttl=60)
    async def txs(request):
        limit = min(_int_arg(request, "limit", 20), 100)
        offset = _int_arg(request, "offset", 0)
        res = db.txs(limit=limit, offset=offset,
                     sender=request.query.get("sender"),
                     receiver=request.query.get("receiver"))
        return _json({"total": res["total"], "limit": limit, "offset": offset, "txs": res["txs"]})

    @cached(ttl=60)
    async def tx_detail(request):
        t = db.tx_by_txid(request.match_info["txid"])
        if not t:
            return _json({"error": "not_found"}, status=404)
        return _json({"tx": t})

    # ---------------- 地址 / 合约 ----------------
    @cached(ttl=60)
    async def address_detail(request):
        addr = request.match_info["addr"]
        row = db.address(addr)
        if not row:
            return _json({"error": "not_found"}, status=404)
        out = dict(row)
        out["txs"] = db.txs_of_address(addr, limit=50)["txs"]
        out["contracts"] = db.contracts_of(addr)
        out["nfts"] = db.nfts_of(addr)
        out["reputation"] = None
        out["tier"] = None
        if node is not None:
            try:
                rep = node.did.reputation(addr)
                out["reputation"] = rep.get("score")
                out["tier"] = rep.get("tier")
            except Exception:
                pass
        return _json({"address": out})

    @cached(ttl=60)
    async def contract_detail(request):
        addr = request.match_info["addr"]
        c = db.contract(addr)
        if not c:
            return _json({"error": "not_found"}, status=404)
        return _json({"contract": c})

    # ---------------- 搜索 ----------------
    @cached(ttl=5)
    async def search(request):
        q = request.query.get("q", "").strip()
        return _json({"query": q, "results": db.search(q)})

    # ---------------- GraphQL ----------------
    async def graphql(request):
        if request.method == "GET":
            query = request.query.get("query", "")
        else:
            try:
                body = await request.json()
            except Exception:
                return _json({"errors": [{"message": "请求体不是合法 JSON"}]}, status=400)
            query = (body or {}).get("query", "")
        result = gql.execute(query)
        status = 400 if "errors" in result else 200
        return _json(result, status=status)

    # ---------------- 路由 ----------------
    app.router.add_get("/", index)
    app.router.add_get("/api/indexer/status", indexer_status)
    app.router.add_post("/api/indexer/sync", manual_sync)
    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/blocks", blocks)
    app.router.add_get("/api/block/{height}", block_detail)
    app.router.add_get("/api/txs", txs)
    app.router.add_get("/api/tx/{txid}", tx_detail)
    app.router.add_get("/api/address/{addr}", address_detail)
    app.router.add_get("/api/contract/{addr}", contract_detail)
    app.router.add_get("/api/search", search)
    app.router.add_get("/graphql", graphql)
    app.router.add_post("/graphql", graphql)

    # ---------------- 生命周期 ----------------
    async def on_startup(application):
        await indexer.sync_once()
        if auto_sync:
            application["_sync_task"] = asyncio.create_task(indexer.run())

    async def on_cleanup(application):
        task = application["_sync_task"]
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        db.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nova 链浏览器索引器服务")
    parser.add_argument("--node-url", default="http://127.0.0.1:8080",
                        help="Nova 节点 RPC 地址（默认 http://127.0.0.1:8080）")
    parser.add_argument("--db", default="sqlite:///explorer.db",
                        help="数据库连接串：sqlite:///explorer.db 或 postgres://user:pass@host/db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--poll", type=int, default=15, help="轮询间隔（秒）")
    args = parser.parse_args()
    db = connect_db(args.db)
    app = create_app(db=db, node_url=args.node_url, auto_sync=True, poll_interval=args.poll)
    print(f"[EXPLORER] 索引器启动 node_url={args.node_url} db={args.db} "
          f"http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)
