import time

class Economy:
    TOTAL_SUPPLY = 81000000
    GENESIS_TIME = 1756569600
    INIT_REWARD = 0.5
    HALVING = 9 * 30 * 24 * 3600
    MAX_HALVINGS = 9
    FIXED_GAS = 0.000001
    MIN_STAKE = 100
    MAX_STAKE = 10000
    MAX_TOTAL_STAKE = int(TOTAL_SUPPLY * 0.30)   # 全网质押上限（30% 供应量）
    MAX_UNBONDING_RATIO = 0.25                   # 解押上限：冷却中总量 <= 当前质押的 25%
    INACTIVITY_SLASH_RATIO = 0.01                # 出块超时惩罚：1% 质押（最低 1 NOVA）
    EQUIVOCATION_SLASH_RATIO = 0.05              # 双签惩罚：5% 质押
    JAIL_EPOCHS = 1                              # 惩罚后禁用出块权 N 个 epoch
    UNBOND = 7 * 86400

    INIT_DEPLOY_REWARD = 5
    DEPLOY_HALVING_STEP = 50000
    MIN_DEPLOY_REWARD = 0.01

    INIT_REFERRAL_REWARD = 1
    REFERRAL_HALVING_STEP = 100000
    MIN_REFERRAL_REWARD = 0.01

    INIT_CALL_REWARD = 0.1
    CALL_HALVING_STEP = 500000
    MIN_CALL_REWARD = 0.001

    VALIDATOR_POOL = "0x_validator_pool"
    ECOSYSTEM_FUND = "0x_ecosystem_fund"
    COMMUNITY_AIRDROP = "0x_community_airdrop"

    # 早期激励
    EARLY_MINER_REWARD = 1000
    EARLY_LIGHT_REWARD = 100
    AIRDROP_AMOUNT = 100          # 前置空投100 NOVA
    LOCK_DURATION = 3 * 365 * 86400  # 3年
    UNLOCK_RATE = 0.1             # 每月10%
    RELEASE_TIME = GENESIS_TIME + 12 * 30 * 24 * 3600  # 12个月后发放

    def __init__(self, store):
        self.store = store

    def block_reward(self):
        h = min(int((time.time() - self.GENESIS_TIME) // self.HALVING), self.MAX_HALVINGS)
        return self.INIT_REWARD / (2 ** h)

    def deploy_reward(self):
        return max(self.INIT_DEPLOY_REWARD / (2 ** (self.store.deploy_count // self.DEPLOY_HALVING_STEP)), self.MIN_DEPLOY_REWARD)

    def referral_reward(self):
        return max(self.INIT_REFERRAL_REWARD / (2 ** (self.store.referral_issued // self.REFERRAL_HALVING_STEP)), self.MIN_REFERRAL_REWARD)

    def call_reward(self):
        return max(self.INIT_CALL_REWARD / (2 ** (self.store.call_count // self.CALL_HALVING_STEP)), self.MIN_CALL_REWARD)

    def light_verify_reward(self):
        return self.block_reward()

    def effective_stake(self, n):
        return min(self.store.stakes.get(n, 0), self.MAX_STAKE)

    def total_stake(self):
        return sum(self.effective_stake(n) for n in self.store.stakes)

    def distribute(self, reward):
        total = self.total_stake()
        if total == 0: return
        for n in self.store.stakes:
            e = self.effective_stake(n)
            if e > 0:
                self.store.balances[n] = self.store.balances.get(n, 0) + reward * (e / total)

    # ---------- 早期激励函数 ----------
    def early_airdrop(self, addr: str, role: str) -> bool:
        if addr in self.store.early_airdrop_received:
            return False
        if role == "miner" and len(self.store.miner_registry) >= 81:
            return False
        if role == "light" and sum(1 for a in self.store.light_checkins if self.store.light_checkins[a] > 0) >= 8100:
            return False
        if self.store.balances.get(self.ECOSYSTEM_FUND, 0) < self.AIRDROP_AMOUNT:
            return False

        self.store.balances[self.ECOSYSTEM_FUND] -= self.AIRDROP_AMOUNT
        self.store.locked_balances[addr] = {
            "amount": self.AIRDROP_AMOUNT,
            "start_time": time.time(),
            "unlocked": 0
        }
        self.store.early_airdrop_received.add(addr)
        print(f"[AIRDROP] {role} {addr[:12]}... 获得 {self.AIRDROP_AMOUNT} NOVA 锁仓")
        return True

    def check_unlock(self, addr: str) -> float:
        if addr not in self.store.locked_balances:
            return 0
        lock = self.store.locked_balances[addr]
        now = time.time()
        elapsed = now - lock["start_time"]
        if elapsed < self.LOCK_DURATION:
            return 0
        months = (elapsed - self.LOCK_DURATION) // (30 * 86400)
        total = lock["amount"] * min(months * self.UNLOCK_RATE, 1.0)
        new_unlock = total - lock["unlocked"]
        if new_unlock > 0:
            lock["unlocked"] += new_unlock
            self.store.balances[addr] = self.store.balances.get(addr, 0) + new_unlock
            return new_unlock
        return 0

    def release_early_rewards(self):
        if time.time() < self.RELEASE_TIME:
            return
        for addr in list(self.store.miner_qualified):
            if addr in self.store.early_rewards_paid:
                continue
            if self.store.balances.get(self.ECOSYSTEM_FUND, 0) >= self.EARLY_MINER_REWARD:
                self.store.balances[self.ECOSYSTEM_FUND] -= self.EARLY_MINER_REWARD
                self.store.balances[addr] = self.store.balances.get(addr, 0) + self.EARLY_MINER_REWARD
                self.store.early_rewards_paid.add(addr)
        for addr in list(self.store.light_qualified):
            if addr in self.store.early_rewards_paid:
                continue
            if self.store.balances.get(self.ECOSYSTEM_FUND, 0) >= self.EARLY_LIGHT_REWARD:
                self.store.balances[self.ECOSYSTEM_FUND] -= self.EARLY_LIGHT_REWARD
                self.store.balances[addr] = self.store.balances.get(addr, 0) + self.EARLY_LIGHT_REWARD
                self.store.early_rewards_paid.add(addr)
        print(f"[REWARD] 早期激励已发放")