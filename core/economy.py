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
    MAX_TOTAL_STAKE = int(TOTAL_SUPPLY * 0.85)   # 全网质押绝对上限（v0.9 放宽至 85%，由档位机制接管治理）
    MAX_UNBONDING_RATIO = 0.25                   # 解押上限：冷却中总量 <= 当前质押的 25%
    # 质押过热保护档位（v0.9）：比例 = 全网质押 / 流通量（流通量 = 总供应 - 已质押 - 锁仓）
    STAKE_TIER_LOW = 0.50                        # 达 50%：新质押收益 -20%
    STAKE_TIER_MID = 0.70                        # 达 70%：新质押收益 -50%
    STAKE_TIER_HIGH = 0.80                       # 达 80%：暂停新质押（已有质押不受影响）
    STAKE_WEIGHT_LOW = 0.8                       # 50% 档新质押有效权重
    STAKE_WEIGHT_MID = 0.5                       # 70% 档新质押有效权重
    # 无感动态手续费档位（v0.9）
    LARGE_TRANSFER_THRESHOLD = 100_000.0         # 单笔纯转账超 10 万 NOVA
    LARGE_TRANSFER_MULT = 100.0                  # 大额转账手续费 100 倍
    HIGH_FREQ_CALL_LIMIT = 1000                  # 同地址单日合约调用次数上限（第 1001 次起）
    HIGH_FREQ_CALL_MULT = 10.0                   # 高频合约调用手续费 10 倍
    # 负载自适应（v0.9）：UTC 自然日已确认交易数
    LOAD_HIGH = 10_000_000                       # 高负载：重操作排队 1 分钟
    LOAD_EXTREME = 50_000_000                    # 极端负载：重操作排队 5 分钟
    LOAD_DISASTER = 100_000_000                  # 灾难负载：扩容激励
    HEAVY_QUEUE_DELAY_HIGH = 60                  # 高负载重操作排队秒数
    HEAVY_QUEUE_DELAY_EXTREME = 300              # 极端/灾难负载重操作排队秒数
    SCALE_REWARD = 500.0                         # 灾难负载期间新矿工空投（锁定 3 年）
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

    # 测试网水龙头：免费领取测试 NOVA（仅测试网模式启用，主网禁用）
    FAUCET_POOL = "0x_faucet_pool"            # 水龙头资金池地址
    FAUCET_AMOUNT = 100.0                     # 每次领取 100 NOVA 测试币
    FAUCET_COOLDOWN = 86400                   # 同一地址每 24 小时限领 1 次
    FAUCET_DAILY_IP_CAP = 2                   # 同一 IP 每日最多领取 2 次
    FAUCET_DAILY_CAP = 20000.0                # 每日全网发放上限（测试币）
    FAUCET_INITIAL_POOL = 1000000.0           # 测试网启动时一次性铸造的水龙头池

    # 算力网络经济模型（提示词 5）：激励池与存储共池，按贡献比例分配
    AI_GROWTH_FUND = "0x_ai_growth_fund"      # AI 成长基金地址（合约控制）
    COMPUTE_INCENTIVE_WEIGHT = 0.6            # 算力贡献权重 60%
    STORAGE_INCENTIVE_WEIGHT = 0.4            # 存储贡献权重 40%
    COMPUTE_MIN_STAKE = 100.0                 # 算力节点最低质押
    COMPUTE_MAX_STAKE = 10000.0               # 算力节点最高质押
    COMPUTE_UNBOND = 7 * 86400                # 解质押 7 天冷静期

    # 存储网络奖励参数（生态基金支付）
    STORAGE_REWARD_PER_GB_PER_DAY = 0.001   # 固定奖励：0.001 NOVA / GB / 天
    STORAGE_PROOF_REWARD = 0.05             # 每份存储证明奖励 0.05 NOVA

    # 早期激励
    EARLY_MINER_REWARD = 1000
    EARLY_LIGHT_REWARD = 100
    AIRDROP_AMOUNT = 100          # 前置空投100 NOVA
    LOCK_DURATION = 3 * 365 * 86400  # 3年
    UNLOCK_RATE = 0.1             # 每月10%
    RELEASE_TIME = GENESIS_TIME + 12 * 30 * 24 * 3600  # 12个月后发放

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _day_key():
        """UTC 自然日键（容错：测试环境可能替换 time 模块）。"""
        try:
            return time.strftime("%Y-%m-%d", time.gmtime())
        except Exception:
            return str(int(time.time()) // 86400)

    def disaster_load(self) -> bool:
        """灾难负载：UTC 自然日已确认交易数 >= LOAD_DISASTER（链上确定性判定）。"""
        return int(self.store.daily_tx_count.get(self._day_key(), 0)) >= self.LOAD_DISASTER

    def load_tier(self) -> int:
        """负载档位：0 正常 / 1 高负载 / 2 极端 / 3 灾难。"""
        n = int(self.store.daily_tx_count.get(self._day_key(), 0))
        if n >= self.LOAD_DISASTER:
            return 3
        if n >= self.LOAD_EXTREME:
            return 2
        if n >= self.LOAD_HIGH:
            return 1
        return 0

    def block_reward(self):
        h = min(int((time.time() - self.GENESIS_TIME) // self.HALVING), self.MAX_HALVINGS)
        r = self.INIT_REWARD / (2 ** h)
        if self.disaster_load():
            r *= 2.0  # 灾难负载扩容激励：临时提高出块奖励（由 validator pool 支付，非增发）
        return r

    def deploy_reward(self):
        return max(self.INIT_DEPLOY_REWARD / (2 ** (self.store.deploy_count // self.DEPLOY_HALVING_STEP)), self.MIN_DEPLOY_REWARD)

    def referral_reward(self):
        return max(self.INIT_REFERRAL_REWARD / (2 ** (self.store.referral_issued // self.REFERRAL_HALVING_STEP)), self.MIN_REFERRAL_REWARD)

    def call_reward(self):
        return max(self.INIT_CALL_REWARD / (2 ** (self.store.call_count // self.CALL_HALVING_STEP)), self.MIN_CALL_REWARD)

    def light_verify_reward(self):
        return self.block_reward()

    def effective_stake(self, n):
        """有效质押：按分层质押的入账时档位权重加权（v0.9 质押过热保护）。
        老质押权重永远 1.0（不追减）；过热期新增层按入账时档位打折。"""
        raw = self.store.stakes.get(n, 0)
        layers = self.store.stake_layers.get(n)
        if not layers:
            return min(raw, self.MAX_STAKE)  # 兼容旧状态
        eff = 0.0
        for layer in layers:
            try:
                amt, w = float(layer[1]), float(layer[2])
            except (IndexError, TypeError, ValueError):
                amt, w = float(layer[1]), 1.0
            eff += amt * w
        return min(eff, raw, self.MAX_STAKE)  # min(eff,raw) 兜底 slash 未同步层

    def raw_total_stake(self):
        return sum(float(v) for v in self.store.stakes.values())

    def locked_total(self):
        return sum(float(v.get("amount", 0)) - float(v.get("unlocked", 0))
                   for v in self.store.locked_balances.values())

    def circulating(self):
        """流通量 = 总供应量 - 已质押 - 锁仓量。"""
        return self.TOTAL_SUPPLY - self.raw_total_stake() - self.locked_total()

    def stake_ratio(self):
        """全网质押 / 流通量；无流通时视为 1.0（最高档）。"""
        circ = self.circulating()
        if circ <= 0:
            return 1.0
        return self.raw_total_stake() / circ

    def stake_tier(self):
        """返回 (档位名, 新质押权重, 是否暂停新质押)。"""
        r = self.stake_ratio()
        if r >= self.STAKE_TIER_HIGH:
            return "paused", 0.0, True
        if r >= self.STAKE_TIER_MID:
            return "hot", self.STAKE_WEIGHT_MID, False
        if r >= self.STAKE_TIER_LOW:
            return "warm", self.STAKE_WEIGHT_LOW, False
        return "normal", 1.0, False

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