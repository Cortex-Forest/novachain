import json
import os
import time
from typing import Dict, Set

class StateStore:
    def __init__(self, genesis_file="genesis.json"):
        self.balances: Dict[str, float] = {}
        self.contracts: Dict[str, str] = {}
        self.contract_creator: Dict[str, str] = {}
        self.stakes: Dict[str, float] = {}
        self.unbonding: Dict[str, tuple] = {}
        self.dag: Set[str] = set()
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

        self._load_genesis(genesis_file)
    def to_dict(self):
        return {
            "balances": self.balances,
            "contracts": self.contracts,
            "contract_creator": self.contract_creator,
            "stakes": self.stakes,
            "unbonding": {k: [v[0], v[1]] for k, v in self.unbonding.items()},
            "dag": sorted(self.dag),
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
        }

    def from_dict(self, d):
        self.balances = {k: float(v) for k, v in d.get("balances", {}).items()}
        self.contracts = dict(d.get("contracts", {}))
        self.contract_creator = dict(d.get("contract_creator", {}))
        self.stakes = {k: float(v) for k, v in d.get("stakes", {}).items()}
        self.unbonding = {k: (float(v[0]), float(v[1])) for k, v in d.get("unbonding", {}).items()}
        self.dag = set(d.get("dag", []))
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