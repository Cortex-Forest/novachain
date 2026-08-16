# -*- coding: utf-8 -*-
"""Nova 链浏览器索引器存储层。

双后端：
- SQLite（默认，零依赖，适合低配单机部署，百万级交易仍可亚秒查询）；
- PostgreSQL（生产推荐，连接串 postgres://user:pass@host/db，需 psycopg2）。

表结构：
- blocks      区块（高度/哈希/前块哈希/出块者/时间戳/交易数）
- txs         交易（txid/所在区块/发送/接收/金额/Gas/操作数据/时间/状态）
- contracts   合约（地址/部署者/部署时间/调用次数）
- addresses   地址聚合（余额/交易数/合约数/NFT 徽章数）
- nfts        不可转让徽章（持有人/徽章名）
- stats       全网统计缓存

写入全部幂等（ON CONFLICT 更新），支持断点续跑与增量重放。
业务 SQL 统一使用 %s 占位符，SQLite 后端在执行前经 _P() 转换为 ?。
"""
import json
import sqlite3
import threading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    height INTEGER PRIMARY KEY,
    hash TEXT UNIQUE,
    prev_hash TEXT,
    proposer TEXT,
    timestamp REAL,
    tx_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS txs (
    txid TEXT PRIMARY KEY,
    block_height INTEGER,
    sender TEXT,
    receiver TEXT,
    amount REAL,
    gas REAL,
    data TEXT,
    ts REAL,
    status TEXT DEFAULT 'confirmed'
);
CREATE INDEX IF NOT EXISTS idx_txs_sender ON txs(sender, ts DESC);
CREATE INDEX IF NOT EXISTS idx_txs_receiver ON txs(receiver, ts DESC);
CREATE INDEX IF NOT EXISTS idx_txs_block ON txs(block_height);
CREATE INDEX IF NOT EXISTS idx_txs_ts ON txs(ts DESC);
CREATE TABLE IF NOT EXISTS contracts (
    address TEXT PRIMARY KEY,
    creator TEXT,
    created_at REAL,
    call_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_contracts_creator ON contracts(creator);
CREATE TABLE IF NOT EXISTS addresses (
    address TEXT PRIMARY KEY,
    balance REAL DEFAULT 0,
    tx_count INTEGER DEFAULT 0,
    contract_count INTEGER DEFAULT 0,
    nft_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nfts (
    holder TEXT,
    badge TEXT,
    created_at REAL DEFAULT 0,
    PRIMARY KEY (holder, badge)
);
CREATE INDEX IF NOT EXISTS idx_nfts_holder ON nfts(holder);
CREATE TABLE IF NOT EXISTS stats (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class IndexDB:
    """存储层基类：SQLite / PostgreSQL 共用同一套业务 SQL。"""

    def __init__(self):
        self._lock = threading.RLock()

    def _P(self, sql):
        """把 %s 占位符转换为具体后端的写法（PG 保持 %s，SQLite 转 ?）。"""
        return sql

    def _vals(self, n):
        return ",".join(["%s"] * n)

    def _execute(self, sql, params=()):
        raise NotImplementedError

    def _query(self, sql, params=()):
        raise NotImplementedError

    def _query_one(self, sql, params=()):
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def init_schema(self):
        for stmt in _SCHEMA.split(";"):
            s = stmt.strip()
            if s:
                self._execute(s)

    # ---------------- 区块 ----------------
    def upsert_block(self, b):
        """写入区块，返回 1（新）或 0（已存在）。"""
        exists = self._query_one("SELECT 1 AS x FROM blocks WHERE height=%s", (b["height"],))
        sql = ("INSERT INTO blocks(height,hash,prev_hash,proposer,timestamp,tx_count) "
               "VALUES (" + self._vals(6) + ") ON CONFLICT (height) DO UPDATE SET "
               "hash=excluded.hash,prev_hash=excluded.prev_hash,proposer=excluded.proposer,"
               "timestamp=excluded.timestamp,tx_count=excluded.tx_count")
        self._execute(sql, (b["height"], b.get("hash", ""), b.get("prev_hash", ""),
                            b.get("proposer", ""), float(b.get("timestamp", 0)),
                            len(b.get("txids", []))))
        return 0 if exists else 1

    def latest_height(self):
        row = self._query_one("SELECT MAX(height) AS h FROM blocks")
        return int(row["h"]) if row and row["h"] is not None else -1

    def recent_blocks(self, limit=20, offset=0):
        return self._query("SELECT height,hash,prev_hash,proposer,timestamp,tx_count "
                           "FROM blocks ORDER BY height DESC LIMIT " + self._vals(1) +
                           " OFFSET " + self._vals(1),
                           (int(limit), int(offset)))

    def block_by_height(self, height):
        return self._query_one("SELECT height,hash,prev_hash,proposer,timestamp,tx_count "
                               "FROM blocks WHERE height=%s", (int(height),))

    def block_by_hash(self, hsh):
        return self._query_one("SELECT height,hash,prev_hash,proposer,timestamp,tx_count "
                               "FROM blocks WHERE hash=%s", (hsh,))

    def txs_by_height(self, height, limit=100):
        return self._query("SELECT txid,block_height,sender,receiver,amount,gas,data,ts,status "
                           "FROM txs WHERE block_height=%s ORDER BY ts DESC LIMIT " + self._vals(1),
                           (int(height), int(limit)))

    # ---------------- 交易 ----------------
    def upsert_tx(self, t, block_height=None):
        """写入交易（幂等）。block_height 为 None 时不覆盖已有区块归属。"""
        exists = self._query_one("SELECT 1 AS x FROM txs WHERE txid=%s", (t["txid"],))
        if block_height is None and exists:
            cols = ["sender", "receiver", "amount", "gas", "data", "ts", "status"]
            sql = ("UPDATE txs SET " +
                   ",".join(c + "=" + self._vals(1) for c in cols) +
                   " WHERE txid=" + self._vals(1))
            self._execute(sql, (t.get("sender", ""), t.get("receiver", ""),
                                float(t.get("amount", 0)), float(t.get("gas", 0)),
                                str(t.get("data", "")), float(t.get("ts", t.get("confirmed_at", 0))),
                                t.get("status", "confirmed"), t["txid"]))
            return 0
        sql = ("INSERT INTO txs(txid,block_height,sender,receiver,amount,gas,data,ts,status) "
               "VALUES (" + self._vals(9) + ") ON CONFLICT (txid) DO UPDATE SET "
               "block_height=excluded.block_height,sender=excluded.sender,"
               "receiver=excluded.receiver,amount=excluded.amount,gas=excluded.gas,"
               "data=excluded.data,ts=excluded.ts,status=excluded.status")
        self._execute(sql, (t["txid"], block_height, t.get("sender", ""), t.get("receiver", ""),
                            float(t.get("amount", 0)), float(t.get("gas", 0)),
                            str(t.get("data", "")), float(t.get("ts", t.get("confirmed_at", 0))),
                            t.get("status", "confirmed")))
        return 0 if exists else 1

    def tx_by_txid(self, txid):
        return self._query_one("SELECT txid,block_height,sender,receiver,amount,gas,data,ts,status "
                               "FROM txs WHERE txid=%s", (txid,))

    def txs(self, limit=20, offset=0, sender=None, receiver=None, addr=None):
        """交易历史：时间倒序 + 分页；支持按发送方/接收方/地址过滤。"""
        where, params = [], []
        if sender:
            where.append("sender=" + self._vals(1))
            params.append(sender)
        if receiver:
            where.append("receiver=" + self._vals(1))
            params.append(receiver)
        if addr:
            where.append("(sender=" + self._vals(1) + " OR receiver=" + self._vals(1) + ")")
            params += [addr, addr]
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        total_row = self._query_one("SELECT COUNT(*) AS n FROM txs" + cond, tuple(params))
        total = int(total_row["n"]) if total_row else 0
        rows = self._query("SELECT txid,block_height,sender,receiver,amount,gas,data,ts,status "
                           "FROM txs" + cond + " ORDER BY ts DESC LIMIT " + self._vals(1) +
                           " OFFSET " + self._vals(1),
                           tuple(params) + (int(limit), int(offset)))
        return {"total": total, "txs": rows}

    def txs_of_address(self, addr, limit=50, offset=0):
        return self.txs(limit=limit, offset=offset, addr=addr)

    # ---------------- 合约 ----------------
    def upsert_contract(self, address, creator="", created_at=None, call_count=0):
        exists = self._query_one("SELECT 1 AS x FROM contracts WHERE address=%s", (address,))
        sql = ("INSERT INTO contracts(address,creator,created_at,call_count) "
               "VALUES (" + self._vals(4) + ") ON CONFLICT (address) DO UPDATE SET "
               "creator=excluded.creator,"
               "created_at=COALESCE(excluded.created_at, contracts.created_at),"
               "call_count=excluded.call_count")
        self._execute(sql, (address, creator or "", created_at if created_at is not None else 0,
                            int(call_count)))
        return 0 if exists else 1

    def contract(self, address):
        return self._query_one("SELECT address,creator,created_at,call_count "
                               "FROM contracts WHERE address=%s", (address,))

    def contracts_of(self, creator, limit=100):
        return self._query("SELECT address,creator,created_at,call_count FROM contracts "
                           "WHERE creator=%s ORDER BY created_at DESC LIMIT " + self._vals(1),
                           (creator, int(limit)))

    def count_contracts(self):
        row = self._query_one("SELECT COUNT(*) AS n FROM contracts")
        return int(row["n"]) if row else 0

    # ---------------- 地址 / NFT ----------------
    def upsert_balance(self, addr, balance):
        sql = ("INSERT INTO addresses(address,balance) VALUES (" + self._vals(2) + ") "
               "ON CONFLICT (address) DO UPDATE SET balance=excluded.balance")
        self._execute(sql, (addr, float(balance)))

    def upsert_nft(self, holder, badge, created_at=0.0):
        sql = ("INSERT INTO nfts(holder,badge,created_at) VALUES (" + self._vals(3) + ") "
               "ON CONFLICT (holder,badge) DO UPDATE SET created_at=excluded.created_at")
        self._execute(sql, (holder, badge, float(created_at)))

    def recompute_addresses(self):
        """从交易/合约/NFT 表聚合地址统计（幂等，每次同步后执行）。"""
        self._execute("UPDATE addresses SET tx_count=0")
        self._execute("INSERT INTO addresses(address,tx_count) "
                      "SELECT a,COUNT(*) FROM (SELECT sender AS a FROM txs "
                      "UNION ALL SELECT receiver AS a FROM txs) t "
                      "GROUP BY a ON CONFLICT(address) DO UPDATE SET tx_count=excluded.tx_count")
        self._execute("UPDATE addresses SET contract_count=0")
        self._execute("INSERT INTO addresses(address,contract_count) "
                      "SELECT creator,COUNT(*) FROM contracts WHERE creator<>'' GROUP BY creator "
                      "ON CONFLICT(address) DO UPDATE SET contract_count=excluded.contract_count")
        self._execute("UPDATE addresses SET nft_count=0")
        self._execute("INSERT INTO addresses(address,nft_count) "
                      "SELECT holder,COUNT(*) FROM nfts GROUP BY holder "
                      "ON CONFLICT(address) DO UPDATE SET nft_count=excluded.nft_count")

    def address(self, addr):
        return self._query_one("SELECT address,balance,tx_count,contract_count,nft_count "
                               "FROM addresses WHERE address=%s", (addr,))

    def nfts_of(self, holder, limit=100):
        return self._query("SELECT holder,badge,created_at FROM nfts "
                           "WHERE holder=%s ORDER BY created_at DESC LIMIT " + self._vals(1),
                           (holder, int(limit)))

    # ---------------- 统计 / 搜索 ----------------
    def set_stat(self, key, value):
        self._execute("INSERT INTO stats(key,value) VALUES (" + self._vals(2) + ") "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))

    def get_stat(self, key, default=None):
        row = self._query_one("SELECT value FROM stats WHERE key=%s", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def stats(self):
        out = {}
        for k in ("height", "total_txs", "total_addresses", "total_contracts", "total_staked"):
            out[k] = self.get_stat(k, 0)
        if not out["total_txs"]:
            out["total_txs"] = self.count_txs()
        if not out["total_contracts"]:
            out["total_contracts"] = self.count_contracts()
        return out

    def count_txs(self):
        row = self._query_one("SELECT COUNT(*) AS n FROM txs")
        return int(row["n"]) if row else 0

    def search(self, q):
        """即时搜索：交易/地址/合约前缀 + 区块高度精确。"""
        q = q.strip().lower()
        results = []
        if not q:
            return results
        if len(q) >= 8 and all(c in "0123456789abcdef" for c in q):
            for row in self._query("SELECT txid FROM txs WHERE txid LIKE %s "
                                   "ORDER BY ts DESC LIMIT 6", (q + "%",)):
                results.append({"type": "tx", "id": row["txid"],
                                "label": "交易 " + row["txid"][:18] + "..."})
            for row in self._query("SELECT address FROM addresses WHERE address LIKE %s LIMIT 6",
                                   (q + "%",)):
                results.append({"type": "address", "id": row["address"],
                                "label": "地址 " + row["address"][:18] + "..."})
            for row in self._query("SELECT address FROM contracts WHERE address LIKE %s LIMIT 6",
                                   (q + "%",)):
                results.append({"type": "contract", "id": row["address"],
                                "label": "合约 " + row["address"][:18] + "..."})
        if q.isdigit():
            h = int(q)
            row = self.block_by_height(h)
            if row:
                results.append({"type": "block", "id": h, "label": "区块 #" + str(h)})
        return results[:20]

    def close(self):
        pass


class SQLiteDB(IndexDB):
    def __init__(self, path=":memory:"):
        super().__init__()
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
        self.init_schema()

    def _P(self, sql):
        return sql.replace("%s", "?")

    def _execute(self, sql, params=()):
        sql = self._P(sql)
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql, params=()):
        sql = self._P(sql)
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def close(self):
        with self._lock:
            self._conn.close()


class PostgresDB(IndexDB):
    """PostgreSQL 后端（生产推荐）。连接串示例：
    postgres://user:pass@127.0.0.1:5432/nova_explorer
    """

    def __init__(self, dsn):
        super().__init__()
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError("PostgreSQL 后端需要安装 psycopg2：pip install psycopg2-binary")
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = True
        self.init_schema()

    def _execute(self, sql, params=()):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql, tuple(params))
            cur.close()

    def _query(self, sql, params=()):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
            return rows

    def close(self):
        with self._lock:
            self._conn.close()


def connect_db(dsn):
    """按连接串创建后端：sqlite:///path 或 postgres://...。"""
    if dsn.startswith("postgres") or dsn.startswith("postgresql"):
        return PostgresDB(dsn)
    path = dsn[len("sqlite:///"):] if dsn.startswith("sqlite:///") else dsn
    if path in ("", ":memory:"):
        path = ":memory:"
    return SQLiteDB(path)
