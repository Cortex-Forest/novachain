# -*- coding: utf-8 -*-
"""去中心化存储网络（链上注册、固定与存储挖矿奖励）。

对应《链上新增功能》第一阶段「存储挖矿」：
- 超级节点启动即可注册为存储提供者（声明贡献的硬盘容量）。
- 创作者固定（pin）作品文件（CID、大小、时长），生态基金按
  STORAGE_REWARD_PER_GB_PER_DAY 向该文件的“固定奖励池”注入 NOVA。
- 提供者认领 CID（每 CID 最多 MAX_REPLICAS 份），并提交一条哈希链的链顶作为“密封”。
- 提供者按天提交存储证明：揭示哈希链的下一个前像。链上验证哈希链一致性后，
  从固定奖励池发放 STORAGE_PROOF_REWARD，实现“存储挖矿”。
- 高级存储订单：创作者支付 NOVA 进入托管；订单有效期内完成证明的提供者
  按 amount / replicas 平分托管金，到期未发放部分退回创作者。

说明：链上节点无法直接读取硬盘，本实现用哈希链证明作为简化 PoSt（proof of
storage）。生产环境可将 CID 接入 IPFS，并把哈希链证明升级为 Filecoin 风格
的真实时空证明（PoSt）。
"""
import hashlib
import re
import time

CID_RE = re.compile(r"^(?:0x[0-9a-fA-F]{64}|bafy[a-z2-7]{46,58})$")
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

PROOF_PERIOD = 86400                 # 存储证明周期：1 天
MAX_REPLICAS = 10                    # 每个 CID 最大副本数
MIN_SIZE_GB = 0.001
MAX_SIZE_GB = 1024.0
MIN_DURATION_DAYS = 1
MAX_DURATION_DAYS = 3650
MAX_CAPACITY_GB = 1048576.0          # 单节点声明容量上限（1 PB）
DEFAULT_CHAIN_LEN = 365              # 哈希链长度：每天一次证明约可用一年


def day_index() -> int:
    """当前存储证明周期序号（UTC 自然日）。"""
    return int(time.time() // PROOF_PERIOD)


class StorageNetwork:
    def __init__(self, store, economy):
        self.store = store
        self.economy = economy

    @staticmethod
    def make_chain(secret: str, length: int = DEFAULT_CHAIN_LEN) -> list:
        """生成哈希链 [s1..sN]：s[i] = sha3_256(s[i-1])，链顶 chain[-1] 作为密封提交。"""
        chain = []
        cur = secret
        for _ in range(length):
            cur = hashlib.sha3_256(cur.encode()).hexdigest()
            chain.append(cur)
        return chain

    @staticmethod
    def _seal_key(provider: str, cid: str) -> str:
        return f"{provider}:{cid}"

    def pin_reward(self, size_gb: float, duration_days: float) -> float:
        return round(float(size_gb) * float(duration_days)
                     * self.economy.STORAGE_REWARD_PER_GB_PER_DAY, 8)

    # ---------- 存储提供者 ----------
    def register(self, addr: str, capacity_gb: float):
        self.store.storage_providers[addr] = {
            "registered_at": time.time(),
            "capacity_gb": float(capacity_gb),
        }

    # ---------- 固定文件（生态基金注入固定奖励池） ----------
    def pin(self, creator: str, cid: str, size_gb: float, duration_days: float) -> float:
        reward = self.pin_reward(size_gb, duration_days)
        self.store.balances[self.economy.ECOSYSTEM_FUND] -= reward
        self.store.storage_claims[cid] = {
            "owner": creator,
            "size_gb": float(size_gb),
            "duration_days": int(duration_days),
            "created_at": time.time(),
            "expires_at": time.time() + int(duration_days) * 86400,
            "providers": [],
            "reward_pool": reward,
        }
        return reward

    # ---------- 认领 ----------
    def claim(self, provider: str, cid: str, seal: str):
        self.store.storage_claims[cid]["providers"].append(provider)
        self.store.storage_seals[self._seal_key(provider, cid)] = {
            "tip": seal.lower(),
            "revealed": 0,
            "length": DEFAULT_CHAIN_LEN,
            "last_proof_day": 0,
        }

    # ---------- 存储证明（哈希链 PoSt） ----------
    def proof(self, provider: str, cid: str, reveal: str) -> dict:
        key = self._seal_key(provider, cid)
        seal = self.store.storage_seals[key]
        seal["tip"] = reveal.lower()
        seal["revealed"] += 1
        seal["last_proof_day"] = day_index()
        claim = self.store.storage_claims[cid]

        reward = 0.0
        if claim["reward_pool"] > 0:
            reward = min(claim["reward_pool"], self.economy.STORAGE_PROOF_REWARD)
            claim["reward_pool"] = round(claim["reward_pool"] - reward, 8)
            self.store.balances[provider] = self.store.balances.get(provider, 0) + reward
            self.store.storage_rewards[provider] = self.store.storage_rewards.get(provider, 0) + reward
        order_pay = self._order_payout(provider, cid)
        return {"reward": reward, "order_pay": order_pay}

    def _order_payout(self, provider: str, cid: str) -> float:
        paid = 0.0
        for oid, order in list(self.store.storage_orders.items()):
            if order["cid"] != cid:
                continue
            if order["status"] == "active" and time.time() > order["expires_at"]:
                self._refund_order(oid, order)
                continue
            if order["status"] != "active" or provider in order["paid"]:
                continue
            share = round(order["amount"] / order["replicas"], 8)
            order["paid"].append(provider)
            order["paid_amount"] = round(order["paid_amount"] + share, 8)
            self.store.balances[provider] = self.store.balances.get(provider, 0) + share
            paid += share
        return paid

    # ---------- 高级存储订单 ----------
    def create_order(self, creator: str, cid: str, replicas: int, duration_days: float,
                     amount: float, order_id: str):
        self.store.balances[creator] -= float(amount)   # 托管
        self.store.storage_orders[order_id] = {
            "creator": creator,
            "cid": cid,
            "replicas": int(replicas),
            "duration_days": int(duration_days),
            "amount": float(amount),
            "paid": [],
            "paid_amount": 0.0,
            "created_at": time.time(),
            "expires_at": time.time() + int(duration_days) * 86400,
            "status": "active",
        }

    def _refund_order(self, order_id: str, order: dict) -> float:
        order["status"] = "expired"
        refund = round(order["amount"] - order["paid_amount"], 8)
        if refund > 0:
            self.store.balances[order["creator"]] = self.store.balances.get(order["creator"], 0) + refund
        return refund

    def settle_expired(self) -> int:
        """结算所有到期订单：退回未发放的托管金。"""
        n = 0
        for oid, order in list(self.store.storage_orders.items()):
            if order["status"] == "active" and time.time() > order["expires_at"]:
                self._refund_order(oid, order)
                n += 1
        return n
