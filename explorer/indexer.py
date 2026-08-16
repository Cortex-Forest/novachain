# -*- coding: utf-8 -*-
"""Nova 链浏览器索引器：增量同步与数据解析。

两种数据源：
- 进程内模式（node 对象）：测试/单机直接读取节点状态，确定性同步；
- 远端模式（node_url）：通过节点 REST 接口 /api/chain/sync 增量拉取。

同步流程（幂等）：
1. 拉取 after_height 之后的新区块与全部已封存交易、合约、余额、soulbound 徽章；
2. 解析并写入 blocks / txs / contracts / addresses / nfts / stats 表；
3. 聚合地址统计（交易数/合约数/NFT 数）。
"""
import asyncio
import time

from .db import SQLiteDB, connect_db


class ChainIndexer:
    def __init__(self, db=None, node_url=None, node=None, poll_interval=15):
        self.db = db or SQLiteDB(":memory:")
        self.db.init_schema()
        self.node_url = node_url
        self.node = node
        self.poll_interval = max(1, int(poll_interval))
        self.last_sync = 0.0
        self.last_error = ""
        self.sync_count = 0

    # ------------------------------------------------------------------
    # 数据源
    # ------------------------------------------------------------------
    def build_payload(self):
        """进程内模式：直接从 NovaNode 读取同步载荷（与 RPC /api/chain/sync 同构）。"""
        node = self.node
        chain = node.consensus.chain
        sealed = node.consensus.sealed_txids()
        blocks = [b.to_dict() for b in chain]
        txs = [v for k, v in node.store.tx_history.items() if k in sealed]
        contracts = [{"address": a, "creator": node.store.contract_creator.get(a, ""),
                      "created_at": None} for a in node.store.contracts]
        soulbound = []
        for holder, val in node.store.soulbound.items():
            if isinstance(val, dict):
                for badge, ts in val.items():
                    soulbound.append({"holder": holder, "badge": badge,
                                      "created_at": ts if isinstance(ts, (int, float)) else 0.0})
            else:
                for badge in (val or []):
                    soulbound.append({"holder": holder, "badge": badge, "created_at": 0.0})
        return {
            "height": node.consensus.chain_height(),
            "blocks": blocks,
            "txs": txs,
            "contracts": contracts,
            "balances": {a: float(v) for a, v in node.store.balances.items()},
            "soulbound": soulbound,
            "stats": {
                "height": node.consensus.chain_height(),
                "total_txs": len(node.store.tx_history),
                "total_addresses": len(node.store.balances),
                "total_contracts": len(node.store.contracts),
                "total_staked": round(sum(float(v) for v in node.store.stakes.values()), 8),
            },
        }

    async def fetch_payload(self):
        """按增量游标（本地最高区块高度）拉取同步载荷。"""
        after = self.db.latest_height()
        if self.node is not None:
            payload = self.build_payload()
            if after >= 0:
                payload["blocks"] = [b for b in payload["blocks"] if b["height"] > after]
            return payload
        if not self.node_url:
            raise RuntimeError("索引器未配置数据源：需要 node 对象或 node_url")
        import aiohttp
        url = f"{self.node_url.rstrip('/')}/api/chain/sync?after_height={max(0, after)}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"节点同步接口失败 HTTP {resp.status}")
                return await resp.json()

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------
    def apply_payload(self, payload):
        """解析并写入一个同步载荷，返回统计。"""
        db = self.db
        n_blocks = n_txs = n_contracts = 0
        tx_block = {}
        for b in payload.get("blocks", []):
            n_blocks += db.upsert_block(b)
            for txid in b.get("txids", []):
                tx_block[txid] = b["height"]
        # 合约部署时间：从系统部署交易（sender=0x0000, receiver=合约地址）推导
        deploy_ts = {}
        for t in payload.get("txs", []):
            if t.get("sender") == "0x0000" and t.get("receiver"):
                deploy_ts[t["receiver"]] = float(t.get("ts", t.get("confirmed_at", 0)) or 0)
        for c in payload.get("contracts", []):
            addr = c["address"]
            ts = c.get("created_at") or deploy_ts.get(addr)
            n_contracts += db.upsert_contract(addr, c.get("creator", ""), ts)
        for t in payload.get("txs", []):
            n_txs += db.upsert_tx(t, tx_block.get(t.get("txid")))
        for addr, bal in payload.get("balances", {}).items():
            db.upsert_balance(addr, bal)
        for nft in payload.get("soulbound", []):
            db.upsert_nft(nft.get("holder"), nft.get("badge", ""), nft.get("created_at", 0))
        db.recompute_addresses()
        for k, v in (payload.get("stats") or {}).items():
            db.set_stat(k, v)
        return {"blocks": n_blocks, "txs": n_txs, "contracts": n_contracts,
                "height": int(payload.get("height", 0))}

    async def sync_once(self):
        """执行一次增量同步，返回新增统计。"""
        payload = await self.fetch_payload()
        counts = self.apply_payload(payload)
        self.last_sync = time.time()
        self.sync_count += 1
        return counts

    async def run(self):
        """后台轮询循环：监听新区块/新交易/新合约。"""
        while True:
            try:
                await self.sync_once()
                self.last_error = ""
            except Exception as exc:  # 节点短暂不可用时跳过，下一轮重试
                self.last_error = str(exc)[:200]
            await asyncio.sleep(self.poll_interval)

    def status(self):
        return {
            "mode": "in-process" if self.node is not None else "remote",
            "node_url": self.node_url,
            "latest_height": self.db.latest_height(),
            "total_txs": self.db.count_txs(),
            "total_contracts": self.db.count_contracts(),
            "last_sync": self.last_sync,
            "sync_count": self.sync_count,
            "last_error": self.last_error,
        }
