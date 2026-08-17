# NEMESIS_STATE_BLOCKED.md — Nemesis 状态不一致阶段（blocked + 自建替代）

## 阻断声明
同 NEMESIS_FEYNMAN_BLOCKED.md：Nemesis 技能不可用，以下为自建替代记录，Phase 7 正式状态 blocked。

## 自建状态不一致检测：跨存储键耦合挑战

对以下状态对做「同一次操作前后 / 重启前后」一致性挑战（全部基于 StateStore 分区键）：

| 状态对 | 挑战操作 | 结果 |
|---|---|---|
| balances[ECOSYSTEM_FUND] ↔ storage_claims[].reward_pool | pin 扣款 | 扣款=池注入，一致；但池注入无真实性（F-02） |
| bridge_assets[].supply ↔ bridge_deposits[].status | 铸造流程 | supply 增=net mint；一致（F-01 问题在外部储备） |
| gov_delegations ↔ voting_power 结果 | 链式委托 | 状态一致但语义错误（F-06） |
| oracle_price_sources ↔ oracle_feeds | 聚合发布 | sources 女巫多源 → feed 一致但失真（F-03） |
| storage_seals.last_proof_day ↔ proof 发放 | 每日一次 | 守卫一致；storage_net.proof 内部无校验（纵深缺口） |
| bridge_daily_usage ↔ 限额 | 每日记录 | 记账一致，计量失真（F-04） |
| snapshot → restore | 全状态往返 | 深比较全等，确定性成立 |
| processed_txids ↔ tx 执行 | 重放 | 拒绝一致 |

## 状态不一致结论
未发现「记账不一致」类新漏洞（如双重入账、残留状态被扫走）；已确认的 6 个根因均为
「状态一致但语义/真实性不变量被违反」型，与状态不一致检测互补。正式 Nemesis 运行仍 blocked。
