# HYPOTHESIS_LEDGER.md — 假设账本（Phase 3S 建立，Phase 6S 对账）

> 每行 = 一个假设或已清除调查项。列：Hypothesis ID | Title | First Seen | Related Passes | Affected Code | Root Cause | Status | Required proof | PoC Status | Scope / Known-Issue Status | Destination | Notes

## 主账本

| Hypothesis ID | Title | First Seen | Related Passes | Affected Code | Root Cause | Status | Required proof | PoC Status | Scope / Known-Issue Status | Destination | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| H-001 | 桥女巫 3 节点假铸造包装资产 | Phase3 P21/P22 | P21,P22,P36,P40,D61-D65 | core/bridge.py:124-128,142-151,298-344,345-373 | 多签仅查节点活跃与签名数，不验节点独立性，也无源链事件证明；`_mint_wrapped` 直接增 supply | confirmed_candidate | 端到端铸造 PoC（3 节点伪造 deposit→sign→claim） | done: poc_bridge.py 铸 49,950 nUSDT（无 BSC 存款证明） | unchecked（历史审计未覆盖桥） | phase8_finding → F-01 | 前置：3 节点各质押 1000 NOVA（7 天后可赎回）；影响上限受每日 100 万 USD 额度约束 |
| H-002 | 存储 pin 锁定/抽干生态基金（假 PoSt） | Phase3 P04/P05 | P04,P05,P19,P20,D30-D37 | core/storage_network.py:59-63,71-84,86-105,107-127；nova_node.py:318-327,341-345 | pin 无数量/真实性限制；proof 证明的是自选秘密哈希链而非真实存储；`storage_net.proof` 自身无校验 | confirmed_candidate | 注资后 pin→claim→proof 提取 PoC | done: poc_storage2.py 锁 7,475.2 NOVA、提取 7.5 NOVA | unchecked | phase8_finding → F-02 | 前置：ECOSYSTEM_FUND 需先有余额（创世 0）；提取受 0.05 NOVA/天/份限速；严重级 High |
| H-003 | 预言机单节点多源任意定价 | Phase3 P33 | P23,P33,D57-D60 | core/oracle.py:227-241,444-458,462-471 | `_price_validate` 不绑定 node→source；冷启动 feed 无基准价即无偏离校验 | confirmed_candidate | 单节点 3 源上报 PoC | done: poc_oracle.py 设 USDT/USD=0.0001 | unchecked | phase8_finding → F-03 | 影响：桥额度/费率以操纵价计量（F-04 并入） |
| H-004 | 治理委托链投票放大 | Phase3 P39 | P39,P40,D69-D72 | core/governance.py:55-67,270-290 | voting_power 递归加总不扣减委托本金，链式委托同一资金多次计票 | confirmed_candidate | 委托链 + 投票权 PoC | done: poc_gov.py 1000 NOVA→3000 票 | unchecked | phase8_finding → F-06 | 需提案进入投票期；影响治理决议 |
| H-005 | 桥 `_usd_value` float×dict 类型错误功能 DoS | Phase3 P35 | P35,D61-D65 | core/bridge.py:62-70,204-215 | `oracle.price()` 返回 dict，`float(amount)*p` 抛 TypeError，validate_op try/except 吞异常恒 False | confirmed_candidate | 有 feed 时任意桥操作返回 False 的 PoC | done: poc_dictbug.py 确认 validate=False | unchecked | phase8_finding → F-05 | 有任一 feed 即整桥不可用；Medium |
| H-006 | 预言机价格操纵传导桥额度/费率 | Phase3 P23/P33 | P23,P33,P36,D57-D60,D61-D65 | core/bridge.py:62-70,196-201；core/oracle.py:227-241 | 桥 USD 计量完全信任 oracle 聚合价，而聚合价可被女巫源操纵 | partial_or_constrained | 操纵后 bridge 额度测算演示 | partial: poc_oracle.py 打印额度影响（_usd_value 被 F-05 阻断） | unchecked | phase8_finding → F-04（并入 F-03 根因） | 与 F-03 同根因合并报告，避免重复 |
| H-007 | 存储订单托管重复领取 | Phase3 P07 | P07,D30-D33 | core/storage_network.py:131-137 | — | killed_invalid | — | — | unchecked | killed_invalid | `provider in order["paid"]` 去重 + 到期退款，无法重复领取 |
| H-008 | 聊天信箱无鉴权读取 | Phase3 P31 | P31,D56 | nova_node.py:1433-1438 | inbox 读取不验签名 | known_issue | — | — | TM-008 同根因 | known_or_duplicate | 见 KNOWN_ISSUE_COLLISION_PROOF.md |
| H-009 | RPC CORS 全放开 | Phase3 P43 | P43,D75 | network/rpc.py | CORS * | known_issue | — | — | TM-009 同根因 | known_or_duplicate | 见碰撞证明 |
| H-010 | P2P TLS 不校验证书 | Phase3 P44/P49 | P44,P49,D76 | network/p2p.py:30-37 | CERT_NONE | known_issue | — | — | TM-013 同根因 | known_or_duplicate | 见碰撞证明 |
| H-011 | IP 角色前缀匹配残余 | Phase3 P45 | P45,D77 | network/security.py:37-47 | startswith(role) | known_issue | — | — | P1-8 同根因 | known_or_duplicate | 见碰撞证明 |
| H-012 | 0x0000 任意铸造 | Phase3 P01 | P01,D1 | nova_node.py:96-99 | — | cleared_no_issue | 守卫 allow_system 存在 | — | P0-1 已修复 | cleared_no_issue | 独立追踪确认 |
| H-013 | 交易重放双花 | Phase3 P02 | P02,D21 | network/security.py:31-35 | — | cleared_no_issue | processed_txids 拒绝 | — | unchecked | cleared_no_issue | 时间戳重签不产生双花 |
| H-014 | 负余额/浮点溢出 | Phase3 P03 | P03,D21 | nova_node.py:851-856；transaction.py:6 | — | cleared_no_issue | amount+gas 前置校验 + _amt | — | TM-012 已知（同根因） | cleared_no_issue | 不重复报告 |
| H-015 | 算力结算竞态 | Phase3 P10 | P10,D38-D40 | core/compute.py:416-471 | — | cleared_no_issue | status 守卫 | — | P1-4 已修复 | cleared_no_issue | 回归确认 |
| H-016 | PoS 签名/双签伪造 | Phase3 P13-P15 | P13,P14,P15,D24-D26 | core/consensus.py:147-193 | — | cleared_no_issue | 签名+双签罚没 | — | TM-003 部分已知（补块时间戳） | cleared_no_issue | 时间戳项见碰撞证明 |
| H-017 | 存储 claim 女巫副本 | Phase3 P06 | P06,D32 | core/storage_network.py:86-105 | — | cleared_no_issue | MAX_REPLICAS 上限 | — | unchecked | cleared_no_issue | 无额外放大 |
| H-018 | SocialFi 记账不守恒 | Phase3 P25-P27 | P25,P26,P27,D43-D50 | core/socialfi.py:1353-1367 | — | cleared_no_issue | 供应守恒+_amt | — | unchecked | cleared_no_issue | 市场自设为设计风险 |
| H-019 | 仲裁托管双取/自裁 | Phase3 P29/P30 | P29,P30,D51-D55 | core/arbitration.py:671-737,450-520 | — | cleared_no_issue | 状态守卫+conflict 检测 | — | unchecked | cleared_no_issue | 全新无关联地址绕过成本高，lead_only 不报告 |
| H-020 | VM 死循环/任意转账 | Phase3 P17 | P17,D28 | core/vm.py:57-104 | — | cleared_no_issue | max_steps；SEND 仅事件 | — | unchecked | cleared_no_issue | 功能缺口非安全洞 |
| H-021 | DEX 恒定积破坏 | Phase3 P38 | P38,D66-D68 | core/dex.py:207-220,429-461 | — | cleared_no_issue | k 值校验+保留地址隔离 | — | unchecked | cleared_no_issue | 无外部代币钩子 |
| H-022 | AI 基金单监护人掏空 | Phase3 补（见 Scope TM-004） | D41-D42 | core/ai_service.py:304-317 | — | cleared_no_issue | 支出审批 | — | TM-004 已处理 | cleared_no_issue | 修复核验通过 |
| H-023 | 创世隐藏铸造 | Phase3 P50 | P50,D85 | genesis.json | — | cleared_no_issue | alloc 合计 81,000,000 | — | unchecked | cleared_no_issue | 激励池 0 为 F-02 前置条件 |
| H-024 | Explorer/GraphQL 注入 | Phase3 P47 | P47,D81-D82 | explorer/*.py | — | cleared_no_issue | 只读+参数化查询 | — | unchecked | cleared_no_issue | — |
| H-025 | Agent 提币/提示注入 | Phase3 P46 | P46,D78-D80 | agent/*.py | — | cleared_no_issue | executor 无资金原语 | — | unchecked | cleared_no_issue | — |

## Phase 6S 对账结果

- 全部 100 轮（D1-D100）已对账：无新增未登记假设；H-001..H-006 由 PoC 支撑，目的地 `phase8_finding`（F-01..F-06，H-006 并入 F-04 由 F-03 合并报告）。
- killed_invalid：H-007（订单去重守卫）；其余 cleared 行均有 SAFE_VERDICT_PROOF_LOG 或碰撞证明支撑。
- known_or_duplicate：H-008..H-011 与 00-scope 已知问题根因逐一比对（见 04-known-vuln-research/KNOWN_ISSUE_COLLISION_PROOF.md 的 root-cause comparison）。
- 无 `phase5_more_poc_needed` / `phase7_tool_challenge` 残留：PoC 全部完成，Phase 7 仅做工具补强（blocked 说明见 07-tools）。
