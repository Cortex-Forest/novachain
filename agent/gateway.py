# -*- coding: utf-8 -*-
"""链网关（阶段 2）：Agent 与 Nova 链之间的统一访问面。

- LocalNodeGateway：进程内 NovaNode（演示/测试/单机托管）
- RpcGateway：HTTP 节点（生产部署，复用 /api/op、/api/ai/*、/api/balance/*）
"""
from __future__ import annotations

import json
import urllib.request

from core.economy import Economy


class AgentGatewayError(RuntimeError):
    pass


class ChainGateway:
    """接口约定：本地与远程实现保持一致。"""

    def ai_identity(self, addr):
        raise NotImplementedError

    def ai_budget_state(self, addr):
        raise NotImplementedError

    def balance(self, addr) -> float:
        raise NotImplementedError

    def text_deposit_required(self, tier, addr=None) -> float:
        raise NotImplementedError

    def gas_of(self, addr) -> float:
        raise NotImplementedError

    def recent_events(self, addr, n=20):
        raise NotImplementedError

    def submit(self, tx) -> str:
        raise NotImplementedError

    def validate_dry(self, tx) -> bool:
        raise NotImplementedError


class LocalNodeGateway(ChainGateway):
    """进程内节点网关：同步提交，直接走 validate_tx / apply_tx 流水线。"""

    def __init__(self, node):
        self.node = node

    def ai_identity(self, addr):
        return self.node.socialfi.ai_identity(addr)

    def ai_budget_state(self, addr):
        return self.node.socialfi.ai_budget_state(addr)

    def balance(self, addr) -> float:
        return float(self.node.balances.get(addr, 0.0))

    def text_deposit_required(self, tier, addr=None) -> float:
        return self.node.socialfi.text_deposit_required(tier, addr)

    def gas_of(self, addr) -> float:
        return self.node.gas_of(addr)

    def recent_events(self, addr, n=20):
        out = []
        for e in self.node.store.socialfi_events.values():
            op = e.get("op")
            if op in ("nova:text:buy", "nova:text:release_deposit"):
                a = self.node.store.text_assets.get(e.get("id"))
                if a and a.get("author") == addr:
                    amount = (float(a.get("price") or 0.0) if op == "nova:text:buy"
                              else float(a.get("deposit") or 0.0))
                    out.append({"op": op, "ts": e.get("ts", 0.0),
                                "amount": amount, "income": True})
            elif op.startswith("nova:ai:") and e.get("addr") == addr:
                out.append({"op": op, "ts": e.get("ts", 0.0),
                            "amount": 0.0, "income": False})
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out[:n]

    def submit(self, tx) -> str:
        if not self.node.validate_tx(tx):
            raise AgentGatewayError("交易校验失败（签名/规则/预算）")
        self.node.security.mark_processed(tx.txid)
        self.node.store.dag.add(tx.txid)
        self.node.apply_tx(tx)
        self.node._record_tx(tx)
        return tx.txid

    def validate_dry(self, tx) -> bool:
        return self.node.validate_tx(tx)


class RpcGateway(ChainGateway):
    """HTTP 网关：连接运行中的 Nova 节点（需已启用 RPC）。"""

    def __init__(self, rpc_url: str, timeout: float = 15.0):
        self.rpc_url = rpc_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path):
        with urllib.request.urlopen(self.rpc_url + path, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.rpc_url + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def ai_identity(self, addr):
        try:
            data = self._get(f"/api/ai/{addr}")
        except Exception:
            return None
        return data if isinstance(data, dict) and "addr" in data else None

    def ai_budget_state(self, addr):
        data = self.ai_identity(addr)
        return data.get("budget") if isinstance(data, dict) else None

    def balance(self, addr) -> float:
        try:
            data = self._get(f"/api/balance/{addr}")
            return float(data.get("balance", 0.0))
        except Exception:
            return 0.0

    def text_deposit_required(self, tier, addr=None) -> float:
        try:
            data = self._get("/api/socialfi/text")
            tiers = data.get("deposit_tiers", {}) if isinstance(data, dict) else {}
            return float(tiers.get(tier, 10.0))
        except Exception:
            return 10.0

    def gas_of(self, addr) -> float:
        return Economy.FIXED_GAS

    def recent_events(self, addr, n=20):
        data = self.ai_identity(addr)
        if not isinstance(data, dict):
            return []
        evs = data.get("recent_ops", []) if isinstance(data, dict) else []
        return [{"op": e.get("op"), "ts": e.get("ts", 0.0),
                 "amount": 0.0, "income": False} for e in evs][:n]

    def submit(self, tx) -> str:
        payload = {
            "addr": tx.sender, "data": tx.data, "amount": tx.amount,
            "sender_public_key": tx.sender_public_key,
            "signature": tx.signature, "timestamp": tx.timestamp,
        }
        try:
            resp = self._post("/api/op", payload)
        except Exception as e:
            raise AgentGatewayError(f"节点不可达：{e}") from e
        if resp.get("status") != "ok":
            raise AgentGatewayError(str(resp.get("error", "提交失败")))
        return str(resp.get("txid", ""))

    def validate_dry(self, tx) -> bool:
        # RPC 无 dry-run 端点；预算/规则由链上最终裁决，本地只做乐观校验。
        return True
