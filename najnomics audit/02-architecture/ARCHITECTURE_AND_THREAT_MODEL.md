# ARCHITECTURE_AND_THREAT_MODEL.md — Phase 2 协议理解

## 协议分类
Nova 链属于「创作者公链 + 模块化 DeFi 生态」混合原语：L1 公链（PoS/checkpoint 双共识）、
代币经济（8100 万锁死）、存储网络（PoSt 简化版）、算力市场、SocialFi（粉丝代币/预测市场/盲盒/债券/碎片 NFT/文本合约）、
跨链桥（BSC/ETH/Polygon）、DEX（AMM）、预言机（VRF + 价格聚合）、链上治理、DID/声誉、订阅、聊天中继、AI 创作者。

## 参与者与信任边界
| 角色 | 权限 | 信任级别 |
|------|------|----------|
| 任意用户 | 签名交易（sender==receiver 特殊 op）、无签名端点（referral/checkin/light_verify/faucet/chat_inbox） | 不可信 |
| 节点（RPC/P2P） | 收交易、出块（checkpoint 任意节点 / PoS 当选者+补块）、全量状态快照 | 半可信 |
| 验证者（PoS） | 质押 100-10000 出块；补块；被罚没风险 | 半可信 |
| 桥节点 | 注册质押 1000；多签 3/5 确认存款/提现 | 不可信（可女巫） |
| 预言机节点 | 注册质押 500；上报价格/VRF/AI 验证 | 不可信（可女巫） |
| 仲裁员 | 质押 500 + 信誉 70；投票裁决 | 半可信 |
| 存储节点 | 质押（超级节点自动注册）；claim/proof | 不可信 |
| AI 监护人 | 日支出 ≤20，大额需 2 监护人 | 半可信 |
| 创世 5 地址 | 持有 8100 万 | 运营方 |

## 关键不变量（协议声明）
1. 总供应量 8100 万，永不增发（创世仅 5 地址）。
2. 交易确定性：同状态同交易 → 同结果（跨节点收敛）。
3. 质押资金锁定（stake 从余额扣减，claim 返还）。
4. 签名绑定 sender 地址（sha3_512(pk) 前 40 hex）。
5. 各类奖励受基金/池余额上限约束。
6. 跨链：1 源链交易 = 1 次铸造（key 去重）；每日额度 100 万 USD。
7. AI 日预算硬约束（ai_can_spend 在 validate_tx 强制）。
8. 治理：1 NOVA = 1 票；quorum = 流通量 10%。

## 实际状态（审计中核验）
- 创世只给 5 个 EOA 分配 8100 万；ECOSYSTEM_FUND / VALIDATOR_POOL / COMMUNITY_AIRDROP / AI_FUND / FAUCET_POOL 均为 0，
  所有「生态基金支付」类奖励在资金注入前不生效（惰性）。
- 质押是真扣款（stake 锁仓）；slash 从 stake 扣减 = 真实燃烧。
- 区块只含 txid；状态靠全量快照同步；P2P 默认明文可关 TLS。

## 交易流水线
rpc_send/call/stake/op → Tx 构造 → validate_tx（重放/大小/0x0000/金额/op 校验/时间戳±300s/签名/余额+gas）
→ broadcast_tx（mark_processed + dag.add + apply_tx + gossip）
→ apply_tx（AI 日预算记录 → 按 op 类型分发 → 转账路径：sender -= amount+gas；receiver += amount；验证者池分配；VM 执行；首资 referral 奖励）
→ P2P 其他节点 process_message 重复验证+应用。

## 威胁模型（补充既有 novachain-threat-model.md 未覆盖的新模块）
新增风险面：bridge（女巫多签铸造）、oracle（任意源价格操控）、dex（假资产兑换）、governance（投票放大）、
storage（虚假 PoSt 抽基金）、faucet（测试网）、explorer（只读索引）、arbitration（复杂赔付）。
