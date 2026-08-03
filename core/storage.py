import json
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

        self._load_genesis(genesis_file)

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