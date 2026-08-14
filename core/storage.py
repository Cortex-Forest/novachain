import json
import os
import time
from typing import Dict, Set

class StateStore:
    def __init__(self, genesis_file="genesis.json"):
        self.balances: Dict[str, float] = {}
        self.contracts: Dict[str, str] = {}
        self.contract_creator: Dict[str, str] = {}
        self.contract_code: Dict[str, list] = {}
        self.contract_state: Dict[str, dict] = {}
        self.stakes: Dict[str, float] = {}
        self.unbonding: Dict[str, tuple] = {}
        self.dag: Set[str] = set()
        self.tx_history: Dict[str, dict] = {}
        self.deploy_count = 0
        self.referral_issued = 0
        self.call_count = 0
        self.referrals: Dict[str, str] = {}
        self.referral_claimed: Set[str] = set()
        self.light_verifications: Dict[str, int] = {}
        self.verified_txids: Set[str] = set()
        self.light_verify_last: Dict[str, str] = {}
        self.call_reward_dates: Dict[str, str] = {}
        self.presale_verified: Dict[str, str] = {}

        # 早期激励
        self.miner_registry: Dict[str, float] = {}
        self.miner_uptime: Dict[str, float] = {}
        self.miner_qualified: Set[str] = set()
        self.light_checkins: Dict[str, int] = {}
        self.light_checkin_dates: Dict[str, Set[str]] = {}
        self.light_qualified: Set[str] = set()
        self.early_airdrop_received: Set[str] = set()
        self.locked_balances: Dict[str, dict] = {}
        self.early_rewards_paid: Set[str] = set()
        self.jailed: Dict[str, float] = {}
        # 去中心化存储网络状态
        self.storage_providers: Dict[str, dict] = {}
        self.storage_claims: Dict[str, dict] = {}
        self.storage_seals: Dict[str, dict] = {}
        self.storage_orders: Dict[str, dict] = {}
        self.storage_rewards: Dict[str, float] = {}
        # 算力任务市场状态
        self.compute_tasks: Dict[str, dict] = {}
        # SocialFi: fan tokens / revenue / achievement / market / blindbox / curation / graph / bond / fraction
        self.fan_tokens: Dict[str, dict] = {}
        self.revenue_shares: Dict[str, dict] = {}
        self.achievements: Dict[str, dict] = {}
        self.soulbound: Dict[str, dict] = {}
        self.markets: Dict[str, dict] = {}
        self.blindboxes: Dict[str, dict] = {}
        self.blind_reveals: Dict[str, str] = {}
        self.curations: Dict[str, dict] = {}
        self.graph_posts: Dict[str, dict] = {}
        self.graph_follows: Dict[str, list] = {}
        self.bonds: Dict[str, dict] = {}
        self.fractions: Dict[str, dict] = {}
        # 文本创作合约：公开/密文文本资产、作者信誉分、合约密钥对
        self.text_assets: Dict[str, dict] = {}
        self.text_reputation: Dict[str, float] = {}
        self.text_contract_priv: str = ""
        self.socialfi_events: Dict[str, dict] = {}

        self._load_genesis(genesis_file)
    def to_dict(self):
        return {
            "balances": self.balances,
            "contracts": self.contracts,
            "contract_creator": self.contract_creator,
            "contract_code": self.contract_code,
            "contract_state": self.contract_state,
            "stakes": self.stakes,
            "unbonding": {k: [v[0], v[1]] for k, v in self.unbonding.items()},
            "dag": sorted(self.dag),
            "tx_history": self.tx_history,
            "deploy_count": self.deploy_count,
            "referral_issued": self.referral_issued,
            "call_count": self.call_count,
            "referrals": self.referrals,
            "referral_claimed": sorted(self.referral_claimed),
            "light_verifications": self.light_verifications,
            "verified_txids": sorted(self.verified_txids),
            "light_verify_last": self.light_verify_last,
            "call_reward_dates": self.call_reward_dates,
            "presale_verified": self.presale_verified,
            "miner_registry": self.miner_registry,
            "miner_uptime": self.miner_uptime,
            "miner_qualified": sorted(self.miner_qualified),
            "light_checkins": self.light_checkins,
            "light_checkin_dates": {k: sorted(v) for k, v in self.light_checkin_dates.items()},
            "light_qualified": sorted(self.light_qualified),
            "early_airdrop_received": sorted(self.early_airdrop_received),
            "locked_balances": self.locked_balances,
            "early_rewards_paid": sorted(self.early_rewards_paid),
            "jailed": self.jailed,
            "storage_providers": self.storage_providers,
            "storage_claims": self.storage_claims,
            "storage_seals": self.storage_seals,
            "storage_orders": self.storage_orders,
            "storage_rewards": self.storage_rewards,
            "compute_tasks": self.compute_tasks,
            "fan_tokens": {k: {**v, "voted": {pk: sorted(pv) for pk, pv in v.get("voted", {}).items()}}
                             for k, v in self.fan_tokens.items()},
            "revenue_shares": self.revenue_shares,
            "achievements": self.achievements,
            "soulbound": self.soulbound,
            "markets": self.markets,
            "blindboxes": self.blindboxes,
            "blind_reveals": self.blind_reveals,
            "curations": self.curations,
            "graph_posts": self.graph_posts,
            "graph_follows": self.graph_follows,
            "bonds": self.bonds,
            "fractions": self.fractions,
            "text_assets": self.text_assets,
            "text_reputation": self.text_reputation,
            "text_contract_priv": self.text_contract_priv,
            "socialfi_events": self.socialfi_events,
        }

    def from_dict(self, d):
        self.balances = {k: float(v) for k, v in d.get("balances", {}).items()}
        self.contracts = dict(d.get("contracts", {}))
        self.contract_creator = dict(d.get("contract_creator", {}))
        self.contract_code = {k: list(v) for k, v in d.get("contract_code", {}).items()}
        self.contract_state = dict(d.get("contract_state", {}))
        self.stakes = {k: float(v) for k, v in d.get("stakes", {}).items()}
        self.unbonding = {k: (float(v[0]), float(v[1])) for k, v in d.get("unbonding", {}).items()}
        self.dag = set(d.get("dag", []))
        self.tx_history = dict(d.get("tx_history", {}))
        self.deploy_count = d.get("deploy_count", 0)
        self.referral_issued = d.get("referral_issued", 0)
        self.call_count = d.get("call_count", 0)
        self.referrals = dict(d.get("referrals", {}))
        self.referral_claimed = set(d.get("referral_claimed", []))
        self.light_verifications = {k: int(v) for k, v in d.get("light_verifications", {}).items()}
        self.verified_txids = set(d.get("verified_txids", []))
        self.light_verify_last = dict(d.get("light_verify_last", {}))
        self.call_reward_dates = dict(d.get("call_reward_dates", {}))
        self.presale_verified = dict(d.get("presale_verified", {}))
        self.miner_registry = {k: float(v) for k, v in d.get("miner_registry", {}).items()}
        self.miner_uptime = {k: float(v) for k, v in d.get("miner_uptime", {}).items()}
        self.miner_qualified = set(d.get("miner_qualified", []))
        self.light_checkins = {k: int(v) for k, v in d.get("light_checkins", {}).items()}
        self.light_checkin_dates = {k: set(v) for k, v in d.get("light_checkin_dates", {}).items()}
        self.light_qualified = set(d.get("light_qualified", []))
        self.early_airdrop_received = set(d.get("early_airdrop_received", []))
        self.locked_balances = dict(d.get("locked_balances", {}))
        self.early_rewards_paid = set(d.get("early_rewards_paid", []))
        self.jailed = {k: float(v) for k, v in d.get("jailed", {}).items()}
        self.storage_providers = dict(d.get("storage_providers", {}))
        self.storage_claims = dict(d.get("storage_claims", {}))
        self.storage_seals = dict(d.get("storage_seals", {}))
        self.storage_orders = dict(d.get("storage_orders", {}))
        self.storage_rewards = {k: float(v) for k, v in d.get("storage_rewards", {}).items()}
        self.compute_tasks = dict(d.get("compute_tasks", {}))
        self.fan_tokens = {k: {**v, "voted": {pk: set(pv) for pk, pv in v.get("voted", {}).items()}}
                           for k, v in d.get("fan_tokens", {}).items()}
        self.revenue_shares = dict(d.get("revenue_shares", {}))
        self.achievements = dict(d.get("achievements", {}))
        self.soulbound = dict(d.get("soulbound", {}))
        self.markets = dict(d.get("markets", {}))
        self.blindboxes = dict(d.get("blindboxes", {}))
        self.blind_reveals = dict(d.get("blind_reveals", {}))
        self.curations = dict(d.get("curations", {}))
        self.graph_posts = dict(d.get("graph_posts", {}))
        self.graph_follows = dict(d.get("graph_follows", {}))
        self.bonds = dict(d.get("bonds", {}))
        self.fractions = dict(d.get("fractions", {}))
        self.text_assets = dict(d.get("text_assets", {}))
        self.text_reputation = {k: float(v) for k, v in d.get("text_reputation", {}).items()}
        self.text_contract_priv = str(d.get("text_contract_priv", ""))
        self.socialfi_events = dict(d.get("socialfi_events", {}))


    def save(self, path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "saved_at": time.time(), "state": self.to_dict()}, f, ensure_ascii=False)
        os.replace(tmp, path)

    def load(self, path) -> bool:
        try:
            with open(path, encoding="utf-8") as f:
                self.from_dict(json.load(f).get("state", {}))
            return True
        except Exception:
            return False


    def _load_genesis(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                alloc = json.load(f)["genesis"]["alloc"]
                for a, v in alloc.items():
                    self.balances[a] = v
            print(f"[GENESIS] 已加载 {len(self.balances)} 个创世地址")
            for ph in ["0x_presale","0x_ecosystem_fund","0x_community_airdrop","0x_validator_pool","0x_reserve"]:
                if ph in self.balances:
                    print(f"[WARNING] 检测到占位地址 {ph}")
        except Exception as e:
            print(f"[GENESIS] 未找到创世文件: {e}")