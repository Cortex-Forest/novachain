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
        self.pos_missed: Dict[str, int] = {}    # 连续错过出块窗口计数（防补块恶意惩罚，H-03）
        # 去中心化存储网络状态
        self.storage_providers: Dict[str, dict] = {}
        self.storage_claims: Dict[str, dict] = {}
        self.storage_seals: Dict[str, dict] = {}
        self.storage_orders: Dict[str, dict] = {}
        self.storage_rewards: Dict[str, float] = {}
        # 存储激励合约状态（超级节点存储挖矿 / 挑战证明 / 奖惩 / 监控恢复）
        self.inc_nodes: Dict[str, dict] = {}          # 存储节点：配额/心跳/收益/退出
        self.inc_files: Dict[str, dict] = {}          # 存储文件：片段承诺/副本列表/健康度
        self.inc_rewards: Dict[str, float] = {}       # 累计奖励（含保护/接管付费）
        self.inc_events: Dict[str, dict] = {}         # 链上事件（创作者通知/惩罚）
        self.inc_event_seq: int = 0
        self.inc_access_counts: Dict[int, dict] = {}  # 每日访问量统计（热门保护）
        self.inc_settled_epochs: set = set()          # 已结算周期
        self.inc_slashed: float = 0.0                 # 累计罚没（进入生态基金）
        # 算力任务市场状态
        self.compute_tasks: Dict[str, dict] = {}
        # 算力网络：节点注册/信誉/质押/竞价/争议/抽查/事件
        self.compute_nodes: Dict[str, dict] = {}
        self.compute_stats: Dict[str, dict] = {}
        self.compute_stakes: Dict[str, float] = {}
        self.compute_unbonding: Dict[str, tuple] = {}
        self.compute_bids: Dict[str, list] = {}
        self.compute_disputes: Dict[str, dict] = {}
        self.compute_audits: Dict[str, dict] = {}
        self.compute_events: Dict[str, dict] = {}
        self.compute_event_seq: int = 0
        self.compute_slashed: float = 0.0
        self.last_incentive_epoch: float = 0.0
        # AI 生成服务：服务登记 / 音乐人循环 / 作品 / 触发 / 成长基金
        self.ai_services: Dict[str, dict] = {}
        self.ai_muso: dict = {}
        self.ai_works: Dict[str, dict] = {}
        self.ai_triggers: Dict[str, dict] = {}
        self.ai_fund_ledger: Dict[str, dict] = {}
        self.ai_fund_seq: int = 0
        self.ai_fund_guardians: Set[str] = set()
        self.ai_fund_spend_day: Dict[str, float] = {}   # 监护人单日支出统计（key: 日期|地址，H-04）
        self.ai_fund_pending: Dict[str, dict] = {}     # 大额支出待审批（H-04）
        self.ai_fund_pending_seq: int = 0
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
        # AI 创作者身份与日预算（阶段 0 PoC）
        self.ai_creators: Dict[str, dict] = {}
        self.ai_daily_spend: Dict[str, dict] = {}
        self.text_assets: Dict[str, dict] = {}
        self.text_reputation: Dict[str, float] = {}
        self.text_contract_priv: str = ""
        self.socialfi_events: Dict[str, dict] = {}
        # 链上社区仲裁合约：仲裁员/候选池/案件/通知/信誉分/质押/防串通
        self.arb_arbitrators: Dict[str, dict] = {}     # 在职仲裁员（质押/信誉分/任期/累计裁决）
        self.arb_candidates: Dict[str, dict] = {}      # 候选池（申请时间/投票状态）
        self.arb_cases: Dict[str, dict] = {}           # 仲裁案件（面板/投票/裁决/赔付）
        self.arb_case_seq: int = 0
        self.arb_notifications: Dict[str, list] = {}   # 链上通知（被抽中/结果/任期提醒）
        self.arb_notif_seq: int = 0
        self.arb_events: list = []                     # 公开案件公示
        self.arb_event_seq: int = 0
        self.arb_vrf_seed: str = "0x9a2b" + "0" * 62   # VRF 种子链（每次抽取前滚）
        self.arb_banned: Set[str] = set()              # 永久取消资格
        self.arb_suspicious: Dict[str, dict] = {}      # 可疑仲裁员（7 天观察期）
        self.arb_malicious: Dict[str, dict] = {}       # 恶意投诉名单（保证金 50 / 锁密文）
        self.arb_stake_pending: Dict[str, list] = {}   # 质押冷静期返还 [金额, 到期时间]
        self.arb_pools: Dict[str, float] = {}          # 保证金池（投诉/二次/候选质押）
        self.arb_slashed: float = 0.0                  # 累计罚没（进入生态基金）

        self._load_genesis(genesis_file)
        # 预言机：VRF 随机数 / 多源价格聚合 / AI 生成结果验证
        self.oracle_nodes: Dict[str, dict] = {}          # 预言机节点（质押 500 NOVA / 公钥 / 状态）
        self.oracle_requests: Dict[str, dict] = {}       # VRF 随机数请求（request_id -> 结果）
        self.oracle_request_seq: int = 0
        self.oracle_price_sources: Dict[str, dict] = {}  # 各数据源上报的价格（feed -> source -> price）
        self.oracle_feeds: Dict[str, dict] = {}          # 聚合后的价格（feed -> {price, ts, sources}）
        self.oracle_ai_verifications: Dict[str, dict] = {}  # AI 生成结果哈希验证
        self.oracle_events: Dict[str, dict] = {}
        self.oracle_event_seq: int = 0
        self.oracle_slashed: float = 0.0                 # 累计罚没（进入生态基金）
        # 跨链桥：节点多签 / 包装资产 / 存款铸造 / 销毁释放 / 额度与延迟
        self.bridge_nodes: Dict[str, dict] = {}          # 桥节点（质押 1000 NOVA / 多签权重 1）
        self.bridge_assets: Dict[str, dict] = {}         # 包装资产（nUSDT/nETH...）
        self.bridge_deposits: Dict[str, dict] = {}       # 跨入事件（源链证明 -> 铸造）
        self.bridge_deposit_seq: int = 0
        self.bridge_withdrawals: Dict[str, dict] = {}    # 跨出事件（销毁 -> 节点签名 -> 释放确认）
        self.bridge_withdrawal_seq: int = 0
        self.bridge_daily_usage: Dict[str, float] = {}   # 每日跨链额度使用（key: 日期）
        self.bridge_fee_pool: float = 0.0                # 手续费回流验证者激励池
        self.bridge_events: Dict[str, dict] = {}
        self.bridge_event_seq: int = 0
        self.bridge_slashed: float = 0.0
        # DEX：AMM 交易对 / LP 持仓 / 流动性挖矿
        self.dex_pairs: Dict[str, dict] = {}             # 交易对（reserve0/reserve1/fee）
        self.dex_lp: Dict[str, dict] = {}                # LP 持仓（pair -> holder -> {shares,...}）
        self.dex_farm: Dict[str, dict] = {}              # 挖矿（pair -> pool 参数 + 用户质押）
        self.dex_events: Dict[str, dict] = {}
        self.dex_event_seq: int = 0
        self.dex_paused: bool = False
        # 链上治理：提案 / 投票 / 委托 / 时间锁
        self.gov_proposals: Dict[str, dict] = {}
        self.gov_proposal_seq: int = 0
        self.gov_delegations: Dict[str, str] = {}        # 委托关系（from -> to）
        self.gov_endorsements: Dict[str, list] = {}      # 社区联署（proposal_id -> [addr]）
        self.gov_timelock: Dict[str, dict] = {}          # 待执行（时间锁 48h）
        self.gov_events: Dict[str, dict] = {}
        self.gov_params: Dict[str, object] = {}          # 治理调整后的参数覆盖（模块读取）
        self.gov_event_seq: int = 0
        # DID 与声誉：身份绑定（只存哈希）/ 创作者认证 / 声誉分
        self.did_profiles: Dict[str, dict] = {}          # DID 档案（哈希绑定 + 可见性）
        self.did_applications: Dict[str, dict] = {}      # 创作者认证申请
        self.did_application_seq: int = 0
        self.did_badges: Dict[str, list] = {}            # 认证徽章（address -> badge id 列表）
        self.did_reputation: Dict[str, dict] = {}        # 声誉分详情（详情仅本人可见，总分公开）
        self.did_events: Dict[str, dict] = {}
        self.did_event_seq: int = 0
        # 创作者订阅与会员：档位 / 订阅 / 自动续费
        self.sub_creators: Dict[str, dict] = {}          # 创作者订阅档位
        self.sub_subscriptions: Dict[str, dict] = {}     # 订阅关系（key: user|creator）
        self.sub_events: Dict[str, dict] = {}
        self.sub_event_seq: int = 0
        # 测试网水龙头：领取记录 / 每日统计 / 发放回执
        self.faucet_claims: Dict[str, dict] = {}   # addr -> {last_ts, total, count, ip}
        self.faucet_daily: Dict[str, dict] = {}    # date -> {count, amount, ips}
        self.faucet_receipts: Dict[str, dict] = {} # receipt_id -> 发放记录
        self.faucet_seq: int = 0

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
            "pos_missed": self.pos_missed,
            "storage_providers": self.storage_providers,
            "storage_claims": self.storage_claims,
            "storage_seals": self.storage_seals,
            "storage_orders": self.storage_orders,
            "storage_rewards": self.storage_rewards,
            "inc_nodes": self.inc_nodes,
            "inc_files": self.inc_files,
            "inc_rewards": self.inc_rewards,
            "inc_events": self.inc_events,
            "inc_event_seq": self.inc_event_seq,
            "inc_access_counts": {str(k): v for k, v in self.inc_access_counts.items()},
            "inc_settled_epochs": sorted(self.inc_settled_epochs),
            "inc_slashed": self.inc_slashed,
            "compute_tasks": self.compute_tasks,
            "compute_nodes": self.compute_nodes,
            "compute_stats": self.compute_stats,
            "compute_stakes": self.compute_stakes,
            "compute_unbonding": {k: [v[0], v[1]] for k, v in self.compute_unbonding.items()},
            "compute_bids": self.compute_bids,
            "compute_disputes": self.compute_disputes,
            "compute_audits": self.compute_audits,
            "compute_events": self.compute_events,
            "compute_event_seq": self.compute_event_seq,
            "compute_slashed": self.compute_slashed,
            "last_incentive_epoch": self.last_incentive_epoch,
            "ai_services": self.ai_services,
            "ai_muso": self.ai_muso,
            "ai_works": self.ai_works,
            "ai_triggers": self.ai_triggers,
            "ai_fund_ledger": self.ai_fund_ledger,
            "ai_fund_seq": self.ai_fund_seq,
            "ai_fund_guardians": sorted(self.ai_fund_guardians),
            "ai_fund_spend_day": self.ai_fund_spend_day,
            "ai_fund_pending": self.ai_fund_pending,
            "ai_fund_pending_seq": self.ai_fund_pending_seq,
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
            "arb_arbitrators": self.arb_arbitrators,
            "arb_candidates": self.arb_candidates,
            "arb_cases": self.arb_cases,
            "arb_case_seq": self.arb_case_seq,
            "arb_notifications": self.arb_notifications,
            "arb_notif_seq": self.arb_notif_seq,
            "arb_events": self.arb_events,
            "arb_event_seq": self.arb_event_seq,
            "arb_vrf_seed": self.arb_vrf_seed,
            "arb_banned": sorted(self.arb_banned),
            "arb_suspicious": self.arb_suspicious,
            "arb_malicious": self.arb_malicious,
            "arb_stake_pending": self.arb_stake_pending,
            "arb_pools": self.arb_pools,
            "arb_slashed": self.arb_slashed,
            "oracle_nodes": self.oracle_nodes,
            "oracle_requests": self.oracle_requests,
            "oracle_request_seq": self.oracle_request_seq,
            "oracle_price_sources": self.oracle_price_sources,
            "oracle_feeds": self.oracle_feeds,
            "oracle_ai_verifications": self.oracle_ai_verifications,
            "oracle_events": self.oracle_events,
            "oracle_event_seq": self.oracle_event_seq,
            "oracle_slashed": self.oracle_slashed,
            "bridge_nodes": self.bridge_nodes,
            "bridge_assets": self.bridge_assets,
            "bridge_deposits": self.bridge_deposits,
            "bridge_deposit_seq": self.bridge_deposit_seq,
            "bridge_withdrawals": self.bridge_withdrawals,
            "bridge_withdrawal_seq": self.bridge_withdrawal_seq,
            "bridge_daily_usage": self.bridge_daily_usage,
            "bridge_fee_pool": self.bridge_fee_pool,
            "bridge_events": self.bridge_events,
            "bridge_event_seq": self.bridge_event_seq,
            "bridge_slashed": self.bridge_slashed,
            "dex_pairs": self.dex_pairs,
            "dex_lp": self.dex_lp,
            "dex_farm": self.dex_farm,
            "dex_events": self.dex_events,
            "dex_event_seq": self.dex_event_seq,
            "dex_paused": self.dex_paused,
            "gov_proposals": self.gov_proposals,
            "gov_proposal_seq": self.gov_proposal_seq,
            "gov_delegations": self.gov_delegations,
            "gov_endorsements": self.gov_endorsements,
            "gov_timelock": self.gov_timelock,
            "gov_events": self.gov_events,
            "gov_params": self.gov_params,
            "gov_event_seq": self.gov_event_seq,
            "did_profiles": self.did_profiles,
            "did_applications": self.did_applications,
            "did_application_seq": self.did_application_seq,
            "did_badges": self.did_badges,
            "did_reputation": self.did_reputation,
            "did_events": self.did_events,
            "did_event_seq": self.did_event_seq,
            "sub_creators": self.sub_creators,
            "sub_subscriptions": self.sub_subscriptions,
            "sub_events": self.sub_events,
            "sub_event_seq": self.sub_event_seq,
            "faucet_claims": self.faucet_claims,
            "faucet_daily": self.faucet_daily,
            "faucet_receipts": self.faucet_receipts,
            "faucet_seq": self.faucet_seq,
            "ai_creators": self.ai_creators,
            "ai_daily_spend": self.ai_daily_spend,
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
        self.pos_missed = {k: int(v) for k, v in d.get("pos_missed", {}).items()}
        self.storage_providers = dict(d.get("storage_providers", {}))
        self.storage_claims = dict(d.get("storage_claims", {}))
        self.storage_seals = dict(d.get("storage_seals", {}))
        self.storage_orders = dict(d.get("storage_orders", {}))
        self.storage_rewards = {k: float(v) for k, v in d.get("storage_rewards", {}).items()}
        self.inc_nodes = dict(d.get("inc_nodes", {}))
        self.inc_files = dict(d.get("inc_files", {}))
        self.inc_rewards = {k: float(v) for k, v in d.get("inc_rewards", {}).items()}
        self.inc_events = dict(d.get("inc_events", {}))
        self.inc_event_seq = int(d.get("inc_event_seq", 0))
        self.inc_access_counts = {int(k): v for k, v in d.get("inc_access_counts", {}).items()}
        self.inc_settled_epochs = set(int(x) for x in d.get("inc_settled_epochs", []))
        self.inc_slashed = float(d.get("inc_slashed", 0.0))
        self.compute_tasks = dict(d.get("compute_tasks", {}))
        self.compute_nodes = dict(d.get("compute_nodes", {}))
        self.compute_stats = dict(d.get("compute_stats", {}))
        self.compute_stakes = {k: float(v) for k, v in d.get("compute_stakes", {}).items()}
        self.compute_unbonding = {k: (float(v[0]), float(v[1])) for k, v in d.get("compute_unbonding", {}).items()}
        self.compute_bids = dict(d.get("compute_bids", {}))
        self.compute_disputes = dict(d.get("compute_disputes", {}))
        self.compute_audits = dict(d.get("compute_audits", {}))
        self.compute_events = dict(d.get("compute_events", {}))
        self.compute_event_seq = int(d.get("compute_event_seq", 0))
        self.compute_slashed = float(d.get("compute_slashed", 0.0))
        self.last_incentive_epoch = float(d.get("last_incentive_epoch", 0.0))
        self.ai_services = dict(d.get("ai_services", {}))
        self.ai_muso = dict(d.get("ai_muso", {})) if d.get("ai_muso") else {}
        self.ai_works = dict(d.get("ai_works", {}))
        self.ai_triggers = dict(d.get("ai_triggers", {}))
        self.ai_fund_ledger = dict(d.get("ai_fund_ledger", {}))
        self.ai_fund_seq = int(d.get("ai_fund_seq", 0))
        self.ai_fund_guardians = set(d.get("ai_fund_guardians", []))
        self.ai_fund_spend_day = {k: float(v) for k, v in d.get("ai_fund_spend_day", {}).items()}
        self.ai_fund_pending = dict(d.get("ai_fund_pending", {}))
        self.ai_fund_pending_seq = int(d.get("ai_fund_pending_seq", 0))
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
        self.arb_arbitrators = dict(d.get("arb_arbitrators", {}))
        self.arb_candidates = dict(d.get("arb_candidates", {}))
        self.arb_cases = dict(d.get("arb_cases", {}))
        self.arb_case_seq = int(d.get("arb_case_seq", 0))
        self.arb_notifications = dict(d.get("arb_notifications", {}))
        self.arb_notif_seq = int(d.get("arb_notif_seq", 0))
        self.arb_events = list(d.get("arb_events", []))
        self.arb_event_seq = int(d.get("arb_event_seq", 0))
        self.arb_vrf_seed = d.get("arb_vrf_seed", "0x9a2b" + "0" * 62)
        self.arb_banned = set(d.get("arb_banned", []))
        self.arb_suspicious = dict(d.get("arb_suspicious", {}))
        self.arb_malicious = dict(d.get("arb_malicious", {}))
        self.arb_stake_pending = {k: list(v) for k, v in d.get("arb_stake_pending", {}).items()}
        self.arb_pools = {k: float(v) for k, v in d.get("arb_pools", {}).items()}
        self.arb_slashed = float(d.get("arb_slashed", 0.0))
        self.ai_creators = dict(d.get("ai_creators", {}))
        self.ai_daily_spend = dict(d.get("ai_daily_spend", {}))
        self.oracle_nodes = dict(d.get("oracle_nodes", {}))
        self.oracle_requests = dict(d.get("oracle_requests", {}))
        self.oracle_request_seq = int(d.get("oracle_request_seq", 0))
        self.oracle_price_sources = dict(d.get("oracle_price_sources", {}))
        self.oracle_feeds = dict(d.get("oracle_feeds", {}))
        self.oracle_ai_verifications = dict(d.get("oracle_ai_verifications", {}))
        self.oracle_events = dict(d.get("oracle_events", {}))
        self.oracle_event_seq = int(d.get("oracle_event_seq", 0))
        self.oracle_slashed = float(d.get("oracle_slashed", 0.0))
        self.bridge_nodes = dict(d.get("bridge_nodes", {}))
        self.bridge_assets = dict(d.get("bridge_assets", {}))
        self.bridge_deposits = dict(d.get("bridge_deposits", {}))
        self.bridge_deposit_seq = int(d.get("bridge_deposit_seq", 0))
        self.bridge_withdrawals = dict(d.get("bridge_withdrawals", {}))
        self.bridge_withdrawal_seq = int(d.get("bridge_withdrawal_seq", 0))
        self.bridge_daily_usage = {k: float(v) for k, v in d.get("bridge_daily_usage", {}).items()}
        self.bridge_fee_pool = float(d.get("bridge_fee_pool", 0.0))
        self.bridge_events = dict(d.get("bridge_events", {}))
        self.bridge_event_seq = int(d.get("bridge_event_seq", 0))
        self.bridge_slashed = float(d.get("bridge_slashed", 0.0))
        self.dex_pairs = dict(d.get("dex_pairs", {}))
        self.dex_lp = dict(d.get("dex_lp", {}))
        self.dex_farm = dict(d.get("dex_farm", {}))
        self.dex_events = dict(d.get("dex_events", {}))
        self.dex_event_seq = int(d.get("dex_event_seq", 0))
        self.dex_paused = bool(d.get("dex_paused", False))
        self.gov_proposals = dict(d.get("gov_proposals", {}))
        self.gov_proposal_seq = int(d.get("gov_proposal_seq", 0))
        self.gov_delegations = dict(d.get("gov_delegations", {}))
        self.gov_endorsements = {k: list(v) for k, v in d.get("gov_endorsements", {}).items()}
        self.gov_timelock = dict(d.get("gov_timelock", {}))
        self.gov_events = dict(d.get("gov_events", {}))
        self.gov_params = dict(d.get("gov_params", {}))
        self.gov_event_seq = int(d.get("gov_event_seq", 0))
        self.did_profiles = dict(d.get("did_profiles", {}))
        self.did_applications = dict(d.get("did_applications", {}))
        self.did_application_seq = int(d.get("did_application_seq", 0))
        self.did_badges = {k: list(v) for k, v in d.get("did_badges", {}).items()}
        self.did_reputation = dict(d.get("did_reputation", {}))
        self.did_events = dict(d.get("did_events", {}))
        self.did_event_seq = int(d.get("did_event_seq", 0))
        self.sub_creators = dict(d.get("sub_creators", {}))
        self.sub_subscriptions = dict(d.get("sub_subscriptions", {}))
        self.sub_events = dict(d.get("sub_events", {}))
        self.sub_event_seq = int(d.get("sub_event_seq", 0))
        self.faucet_claims = {k: dict(v) for k, v in d.get("faucet_claims", {}).items()}
        self.faucet_daily = {k: dict(v) for k, v in d.get("faucet_daily", {}).items()}
        self.faucet_receipts = dict(d.get("faucet_receipts", {}))
        self.faucet_seq = int(d.get("faucet_seq", 0))


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


