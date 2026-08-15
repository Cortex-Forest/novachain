# -*- coding: utf-8 -*-
"""执行器（阶段 2）：把决策落地为签名交易并提交到链。

签名服务：私钥由运行时持有（阶段 0/1 运营方托管），
链上日预算/状态硬约束负责兜底；支持 dry_run 预演。
"""
from __future__ import annotations

import json
import time

from core.transaction import Tx

from .config import AgentConfig
from .gateway import AgentGatewayError


class Executor:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def build_publish_tx(self, wallet, draft, deposit: float, now: float = None):
        payload = {
            "op": "nova:text:create",
            "title": draft.title,
            "content": draft.content,
            "price": draft.price,
            "tier": self.cfg.tier,
            "visibility": self.cfg.visibility,
        }
        data = json.dumps(payload, ensure_ascii=False)
        ts = int(now if now is not None else time.time())
        tx = Tx(wallet.address, wallet.address, deposit, [], data,
                wallet.public_key_hex(), "", timestamp=ts)
        tx.signature = wallet.sign(tx.signing_data())
        return tx

    def execute(self, decision, wallet, chain, dry_run: bool = False) -> dict:
        if decision.action == "publish_text":
            deposit = chain.text_deposit_required(self.cfg.tier, wallet.address)
            tx = self.build_publish_tx(wallet, decision.draft, deposit)
            if dry_run:
                return {"action": "publish_text", "txid": "", "cost": decision.cost,
                        "dry_run": True, "accepted": bool(chain.validate_dry(tx)),
                        "title": decision.draft.title}
            txid = chain.submit(tx)
            return {"action": "publish_text", "txid": txid, "cost": decision.cost,
                    "dry_run": False, "title": decision.draft.title}
        raise AgentGatewayError(f"执行器不支持的动作：{decision.action}")
