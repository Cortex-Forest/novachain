# -*- coding: utf-8 -*-
"""极简 GraphQL 执行器（自研，无第三方依赖）。

支持查询形态：
  query {
    stats { height totalTxs totalStaked }
    blocks(first: 10) { height hash txCount timestamp }
    block(height: 3) { height hash txs { txid sender amount } }
    tx(txid: "0x...") { sender receiver amount block { height } }
    txs(sender: "0x...", first: 20) { txid ts }
    address(addr: "0x...") { balance txCount txs { txid } contracts { address } nfts { badge } }
    contract(address: "0x...") { creator created_at call_count }
    search(q: "abc") { results { type id label } }
  }

字段名与 REST 保持一致（snake_case）；解析器忽略注释、支持字符串/数字/布尔参数。
"""
import re

_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|"
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r"-?[0-9]+(?:\.[0-9]+)?|"
    r"[{}():,]"
)


def tokenize(query):
    return _TOKEN_RE.findall(query)


class Field:
    __slots__ = ("name", "args", "selection")

    def __init__(self, name, args, selection):
        self.name = name
        self.args = args or {}
        self.selection = selection


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse_query(self):
        if self.peek() == "query":
            self.next()
            if self.peek() and self.peek() not in "{":
                self.next()  # 查询名
        if self.peek() != "{":
            raise ValueError("GraphQL 查询必须以 { 开头")
        return self.parse_selection()

    def parse_selection(self):
        if self.next() != "{":
            raise ValueError("缺少 {")
        fields = []
        while True:
            tok = self.peek()
            if tok is None:
                raise ValueError("GraphQL 查询未闭合")
            if tok == "}":
                self.next()
                return fields
            if tok == ",":
                self.next()
                continue
            fields.append(self.parse_field())

    def parse_field(self):
        name = self.next()
        args = {}
        if self.peek() == "(":
            self.next()
            while self.peek() and self.peek() != ")":
                arg_name = self.next()
                if self.peek() == ":":
                    self.next()
                args[arg_name] = self.parse_value()
                if self.peek() == ",":
                    self.next()
            if self.peek() == ")":
                self.next()
            else:
                raise ValueError("参数列表未闭合")
        selection = None
        if self.peek() == "{":
            selection = self.parse_selection()
        return Field(name, args, selection)

    def parse_value(self):
        tok = self.peek()
        if tok is None:
            raise ValueError("缺少参数值")
        if tok.startswith('"') or tok.startswith("'"):
            self.next()
            return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if tok == "true":
            self.next()
            return True
        if tok == "false":
            self.next()
            return False
        if tok == "null":
            self.next()
            return None
        if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", tok):
            self.next()
            return float(tok) if "." in tok else int(tok)
        self.next()
        return tok


class GraphQL:
    """基于 db 的查询执行器；node 可选，用于补充声誉分等链上实时数据。"""

    def __init__(self, db, node=None):
        self.db = db
        self.node = node

    def execute(self, query):
        try:
            tokens = tokenize(query)
            if not tokens:
                return {"errors": [{"message": "空查询"}]}
            fields = _Parser(tokens).parse_query()
            data = {}
            for f in fields:
                data[f.name] = self._resolve_top(f)
            return {"data": data}
        except Exception as exc:
            return {"errors": [{"message": str(exc)}]}

    # ---------------- 顶层解析 ----------------
    def _resolve_top(self, f):
        name = f.name
        if name == "stats":
            return self._project(self._stats(), f.selection)
        if name == "blocks":
            return self._project(self._blocks(f.args), f.selection)
        if name == "block":
            return self._project(self._block(f.args), f.selection)
        if name == "tx":
            return self._project(self._tx(f.args), f.selection)
        if name == "txs":
            return self._project(self._txs(f.args), f.selection)
        if name == "address":
            return self._project(self._address(f.args), f.selection)
        if name == "contract":
            return self._project(self._contract(f.args), f.selection)
        if name == "search":
            return self._project(self._search(f.args), f.selection)
        raise ValueError(f"未知查询字段: {name}")

    # ---------------- 数据解析 ----------------
    def _stats(self):
        s = self.db.stats()
        return {"__type__": "stats", "height": s.get("height", 0),
                "totalTxs": s.get("total_txs", 0), "totalAddresses": s.get("total_addresses", 0),
                "totalContracts": s.get("total_contracts", 0),
                "totalStaked": s.get("total_staked", 0.0)}

    def _blocks(self, args):
        limit = int(args.get("first", args.get("limit", 20)))
        offset = int(args.get("offset", 0))
        return [self._deco_block(b) for b in self.db.recent_blocks(limit, offset)]

    @staticmethod
    def _deco_block(b):
        if b is None:
            return None
        b = dict(b)
        b["__type__"] = "block"
        return b

    def _block(self, args):
        if "height" in args:
            return self._deco_block(self.db.block_by_height(int(args["height"])))
        if "hash" in args:
            return self._deco_block(self.db.block_by_hash(args["hash"]))
        raise ValueError("block 查询需要 height 或 hash 参数")

    @staticmethod
    def _deco_tx(t):
        if t is None:
            return None
        t = dict(t)
        t["__type__"] = "tx"
        return t

    def _tx(self, args):
        if not args.get("txid"):
            raise ValueError("tx 查询需要 txid 参数")
        return self._deco_tx(self.db.tx_by_txid(args["txid"]))

    def _txs(self, args):
        limit = int(args.get("first", args.get("limit", 20)))
        offset = int(args.get("offset", 0))
        res = self.db.txs(limit=limit, offset=offset,
                          sender=args.get("sender"), receiver=args.get("receiver"))
        return [self._deco_tx(t) for t in res["txs"]]

    def _address(self, args):
        addr = args.get("addr") or args.get("address")
        if not addr:
            raise ValueError("address 查询需要 addr 参数")
        row = self.db.address(addr)
        if not row:
            return None
        out = dict(row)
        out["__type__"] = "address"
        out["reputation"] = None
        if self.node is not None:
            try:
                rep = self.node.did.reputation(addr)
                out["reputation"] = rep.get("score")
                out["tier"] = rep.get("tier")
            except Exception:
                pass
        return out

    def _contract(self, args):
        addr = args.get("address") or args.get("addr")
        if not addr:
            raise ValueError("contract 查询需要 address 参数")
        row = self.db.contract(addr)
        if not row:
            return None
        out = dict(row)
        out["__type__"] = "contract"
        return out

    def _search(self, args):
        q = args.get("q", "")
        results = self.db.search(q)
        return {"__type__": "search", "query": q, "results": results}

    # ---------------- 投影 / 嵌套 ----------------
    @staticmethod
    def _snake(name):
        """camelCase -> snake_case（txCount -> tx_count）。"""
        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

    def _project(self, value, selection):
        if selection is None:
            return value
        if isinstance(value, list):
            return [self._project(v, selection) for v in value]
        if isinstance(value, dict):
            out = {}
            for f in selection:
                if f.selection:
                    nested = self._nested(value, f)
                    out[f.name] = None if nested is None else self._project(nested, f.selection)
                else:
                    out[f.name] = value.get(f.name, value.get(self._snake(f.name)))
            return out
        return value

    def _nested(self, parent, field):
        name = field.name
        t = parent.get("__type__")
        if t == "block" and name == "txs":
            return [self._deco_tx(x) for x in self.db.txs_by_height(parent["height"])]
        if t == "tx" and name == "block":
            h = parent.get("block_height")
            return self._deco_block(self.db.block_by_height(h)) if h is not None else None
        if t == "address":
            if name == "txs":
                return [self._deco_tx(x) for x in self.db.txs_of_address(parent["address"])["txs"]]
            if name == "contracts":
                return [self._deco_contract(x) for x in self.db.contracts_of(parent["address"])]
            if name == "nfts":
                return self.db.nfts_of(parent["address"])
        if t == "contract" and name == "calls":
            return parent.get("call_count")
        if t == "search" and name == "results":
            return parent.get("results")
        return parent.get(name)

    @staticmethod
    def _deco_contract(c):
        c = dict(c)
        c["__type__"] = "contract"
        return c

