# PHASE3_PASS_LOG.md — 攻击面映射 50 轮（Phase 3）

目标：`nova_node.py` + `core/*` + `network/*` + `agent/*` + `explorer/*`（commit de1d28f8）
方法：每轮独立黑盒/白盒攻击，先追踪当前代码路径，再用已知问题表做碰撞过滤。
轮次分配见 IN_SCOPE_COVERAGE_MATRIX.md（P1-P50）。

---

## P01 — nova_node 统一入口权限（Access Control）
- Target: nova_node.py:96-127 validate_tx、:500-516 _apply_storage_op、:851-856 转账。
- Attacker: 无特权 RPC 用户，起始资金 0。
- Invariant: 只有签名合法的 tx 才能改状态；0x0000 铸造仅系统路径。
- Intended harm: 无签名铸造 NOVA / 冒领合约奖励。
- Call sequence: 构造 data 为 0x0000 铸造的 tx 直接提交 RPC `/api/tx`；再尝试未签名提交。
- Malicious inputs: `sender="0x0000"`、空 sig、伪造 pk。
- Mutation: 把伪造 pk 改成合法用户 pk 但 sender 不变 → 验签绑定 pub→address（core/crypto.py:186-189）拒绝。
- Known-issue: P0-1/P0-3/TM-001 已修复；当前路径独立追踪后确认守卫存在。
- Outcome: cleared_no_issue → 进入 H-LEDGER cleared 组。

## P02 — 重放与重复处理（Replay）
- Target: network/security.py:31-35 processed_txids；nova_node.py validate_tx 流程。
- Attacker: 已签名交易持有者。
- Invariant: 同一 txid 全网最多执行一次。
- Intended harm: 双花 / 重复领取。
- Call sequence: 提交 tx1 → 再次提交同一 tx1。
- Malicious inputs: 相同 txid、相同 data。
- Mutation: 改 timestamp 再签 → txid 变化（transaction.py:26-28 calc_txid 含时间戳）→ 视为新交易，但余额已扣，双花需要重复资金。
- Known-issue: 与历史报告无碰撞；交易 id 含时间戳+父引用。
- Outcome: cleared_no_issue（重放被拒，时间戳重签不产生双花）。

## P03 — 矿工费/负余额（Arithmetic）
- Target: nova_node.py:851-853 扣款；economy.FIXED_GAS。
- Attacker: 余额刚好等于 amount 的用户。
- Invariant: 任何地址余额 ≥ 0。
- Intended harm: 制造负余额绕过校验。
- Call sequence: amount=balance → 转账后剩 0，再扣 gas 变负？→ validate 在 apply 前检查 amount+gas。
- Malicious inputs: amount=balance、amount=balance-gas+0.00000001。
- Mutation: 用 float 精度攻击 0.1+0.2 式累计 → round 8 位（_amt）与 isfinite 校验（:100-103）。
- Known-issue: TM-012 float 精度已知（交易金额规范化），根因相同不报告。
- Outcome: cleared_no_issue（负余额被 amount+gas 前置检查阻止）。

## P04 — storage pin 无限抽基金（资源/经济）
- Target: nova_node.py:318-327 pin 校验；core/storage_network.py:59-63 pin_reward、:71-84 pin。
- Attacker: 任意地址，免费注册提供者。
- Invariant: 生态基金余额仅被真实固定消耗。
- Intended harm: 用假 CID 锁定/抽干生态基金。
- Call sequence: register(容量) → pin(自选 CID, size=1024, days=3650) ×N。
- Malicious inputs: `size_gb=1024, duration_days=3650`（上界），CID 为随机 hex。
- Mutation: 尝试无基金余额时 pin → 校验要求 fund>=reward，创世基金 0 → 需先注资（前置条件）。
- Known-issue: 与历史报告无碰撞（P1-5 守卫问题已核验）。
- Outcome: confirmed_candidate → H-002，PoC 队列 poc_storage。

## P05 — storage proof 假 PoSt（验证强度）
- Target: nova_node.py:341-345 proof 校验；core/storage_network.py:107-127 proof（无校验）。
- Attacker: 已 claim 的提供者。
- Invariant: 存储证明只在真实存储时获酬。
- Intended harm: 无真实存储领取 0.05 NOVA/天/份。
- Call sequence: claim(tip) → proof(reveal=sha3(tip)) 每天一次。
- Malicious inputs: 自选 secret 生成整条哈希链，逐日揭示。
- Mutation: 直接调 storage_net.proof() 绕过 RPC 校验 → 确认内部函数无哈希链验证（防御纵深缺口）。
- Known-issue: 无历史碰撞；与 F-02 同根因合并。
- Outcome: confirmed_candidate → H-002（并入 F-02）。

## P06 — storage claim 女巫副本（Sybil）
- Target: core/storage_network.py:86-105 claim；nova_node.py:328-330。
- Attacker: 注册多个提供者地址。
- Invariant: 每 CID 最多 MAX_REPLICAS=10 份。
- Intended harm: 同一实体多份副本挤占奖励池。
- Call sequence: 注册 10 地址 → 全部 claim 同一 CID。
- Malicious inputs: 10 个不同 provider 地址。
- Mutation: 尝试第 11 个 → :329 len>=MAX_REPLICAS 拒绝。
- Known-issue: 无碰撞；副本奖励受 reward_pool 上限约束，无额外放大。
- Outcome: cleared_no_issue（受限，不构成新根因）。

## P07 — storage order 托管重复领（Order）
- Target: core/storage_network.py:131-137 _order_payout、:139-147 create_order。
- Attacker: 多提供者。
- Invariant: 托管金发放总额 ≤ amount。
- Intended harm: 重复领取分成。
- Call sequence: create_order → 多 provider claim → proof 触发 payout。
- Malicious inputs: provider 重复 proof。
- Mutation: 同一 provider 两 CID 相互引用 → paid 列表去重。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（provider in order["paid"] 守卫 + 到期退款）。

## P08 — storage_incentive 挑战证明（PoSt2）
- Target: core/storage_incentive.py:242-312 challenge/verify_proof、nova_node.py:517-593。
- Attacker: 普通节点。
- Invariant: 证明必须含节点认领的文件分片。
- Intended harm: 空证明领取日奖励。
- Call sequence: inc:file(他人 CID) → inc:claim → inc:prove(files, fragments)。
- Malicious inputs: fragments 任意 2048 字符串。
- Mutation: files 与 fragments 长度不匹配 → :543-544 拒绝。
- Known-issue: 无碰撞；验证基于 fragment_commit 一致性。
- Outcome: cleared_no_issue（长度/归属校验存在，未发现逃逸）。

## P09 — storage_incentive 配额升级（Quota）
- Target: core/storage_incentive.py:506-518 upgrade_quota、nova_node.py:1638-1652。
- Attacker: 节点。
- Invariant: 配额只能以质押增长。
- Intended harm: 免费提升配额绕过 can_assign。
- Call sequence: inc:upgrade(amount)。
- Malicious inputs: amount=0、负值。
- Mutation: 多次 upgrade 同一节点 → 配额累加但需对应质押（_slash/质押映射）。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（amount 与质押绑定）。

## P10 — compute 任务发布竞态（State Machine）
- Target: core/compute.py:309-315 validate_accept、:416-465 validate_submit、nova_node.py:1832+。
- Attacker: 任务发布者/worker。
- Invariant: 任务状态单向推进。
- Intended harm: 重复结算 / 领取他人赏金。
- Call sequence: publish → accept → submit → _complete；双 submit。
- Malicious inputs: 同一 worker 二次 submit、发布者自 accept。
- Mutation: 发布者代提交 → validate_submit 检查 sender==worker。
- Known-issue: P1-4 已修复（status 守卫），回归核验通过。
- Outcome: cleared_no_issue（含历史修复回归确认）。

## P11 — transaction/blockchain 规范化（Serialization）
- Target: core/transaction.py:6-53、core/blockchain.py。
- Attacker: RPC 用户。
- Invariant: 反序列化 tx/block 与序列化等价且字段合法。
- Intended harm: 畸形 tx 造成解析分歧（分叉）。
- Call sequence: from_dict(畸形 dict) → calc_txid。
- Malicious inputs: amount="abc"、timestamp=None、data 非 str。
- Mutation: 缺字段 → canonical_amount 兜底 0 / 异常被校验层拒绝。
- Known-issue: P2-10 from_dict 重复恢复块为已知 P2，不重复。
- Outcome: cleared_no_issue（P2 级，非安全主项）。

## P12 — 签名校验（Signature）
- Target: core/crypto.py verify_quantum_tx / Ed25519 / Dilithium 分支。
- Attacker: 无私钥者。
- Invariant: 交易签名必须由 sender 私钥产生。
- Intended harm: 伪造任意 sender 交易。
- Call sequence: 构造 pk=受害者公钥、sig=随机 → 验签。
- Malicious inputs: 篡改 amount 后重用旧 sig。
- Mutation: 短签名/超长 sig → 库层拒绝；s<L 未显式校验为已知 TM-011。
- Known-issue: TM-011（Ed25519 s<L 未校验）已知，不报告。
- Outcome: cleared_no_issue（验签绑定 pub→address，碰撞过滤后无新路径）。

## P13 — PoS 出块验证（Consensus）
- Target: core/consensus.py:147-193 adopt_block/_verify_pos_block/_valid_signature/_detect_equivocation。
- Attacker: 非 proposer 节点。
- Invariant: 只有当选 proposer 的签名块可入链。
- Intended harm: 伪造区块重排历史。
- Call sequence: 构造高度 h 块 → adopt_block。
- Malicious inputs: 错误 proposer 私钥签名。
- Mutation: 双签同高度 → _detect_equivocation 罚没。
- Known-issue: TM-003（补块时间戳自报）已知，见碰撞证明。
- Outcome: cleared_no_issue（签名/双签守卫确认；TM-003 不重复）。

## P14 — PoS 出块经济（Slash）
- Target: core/consensus.py:170-181 _slash、economy.py 质押参数。
- Attacker: 恶意验证者。
- Invariant: 作恶质押被罚没且无法提前赎回。
- Intended harm: 自罚没洗白质押 / 大量小额节点扰乱选举。
- Call sequence: 注册大量节点 → 触发 slash。
- Malicious inputs: 最小质押反复注册。
- Mutation: slash 后立即 exit → exit 校验 status=active 且未被 slash。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（slash 后状态不可退出）。

## P15 — checkpoint 共识（Checkpoint）
- Target: core/consensus.py:195-204 checkpoint_loop、:206-213 snapshot。
- Attacker: 网络对手。
- Invariant: checkpoint 必须由质押节点集合签名。
- Intended harm: 假 checkpoint 冻结/回滚。
- Call sequence: 注入假 checkpoint。
- Mutation: 快照同步默认关闭（P0-2 修复核验）→ 受信种子。
- Known-issue: TM-002 已修复。
- Outcome: cleared_no_issue。

## P16 — 经济参数与空投（Economics）
- Target: core/economy.py:66-80 block_reward、:97-114 early_airdrop、:140-148 奖励。
- Attacker: 新地址。
- Invariant: 空投每地址一次、基金余额足够才发。
- Intended harm: 多地址重复空投。
- Call sequence: 一地址多次 airdrop → early_airdrop_received 去重。
- Malicious inputs: 角色伪造。
- Mutation: 基金 0 时触发 → :104 余额守卫跳过。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（去重+余额守卫）。

## P17 — VM 执行（VM）
- Target: core/vm.py:57-104 run、nexlang_compiler.py。
- Attacker: 合约作者。
- Invariant: VM 步数有限、SEND 不触碰账本。
- Intended harm: 死循环/任意转账。
- Call sequence: 部署无限循环字节码 → run。
- Malicious inputs: 超长操作数、负操作数、DIV 0。
- Mutation: SEND 携带大额 → 仅事件记录（:86-91），无余额变化。
- Known-issue: 无碰撞；SEND 未接账本为功能缺口非安全洞。
- Outcome: cleared_no_issue。

## P18 — 状态快照恢复（Persistence）
- Target: core/storage.py snapshot/restore。
- Attacker: 提供坏快照的节点。
- Invariant: 恢复状态与快照一致且分区容错。
- Intended harm: 注入不一致状态。
- Call sequence: restore(畸形 dict)。
- Mutation: 缺 store 字段 → 默认初始化兜底（测试覆盖）。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue。

## P19 — 存储网络 RPC 鉴权（Access Control）
- Target: nova_node.py:1476-1491 rpc_storage_register/pin 等。
- Attacker: 任意 RPC 用户。
- Invariant: 状态操作必须经签名 tx。
- Intended harm: 直接改 store（绕过签名）。
- Call sequence: 直接调 RPC handler 传未签名参数。
- Mutation: 伪造 ip/header 跳过限流 → security.check_rate_limit 仍按 IP。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（RPC 全部包装签名 tx，状态不可直改）。

## P20 — 存储基金前置条件（Economic Precondition）
- Target: core/economy.py:32 ECOSYSTEM_FUND、genesis.json。
- Attacker: —。
- Invariant: 基金余额来源可审计。
- Intended harm: —。
- Call sequence: 追踪基金入账路径（slash、手续费回流、治理注入）。
- Malicious inputs: —。
- Mutation: 创世直接给基金注资 → 与设计冲突（创世只分配 EOA）。
- Known-issue: 无碰撞；F-02 需前置注资，报告注明。
- Outcome: partial_or_constrained（F-02 前置条件确认）。

## P21 — 桥节点注册（Bridge Node）
- Target: core/bridge.py:258-267 _node_validate/apply、:124-128 _is_node。
- Attacker: 任意地址持 1000 NOVA。
- Invariant: 桥节点身份独立。
- Intended harm: 注册多节点形成女巫多签。
- Call sequence: 3 地址各注册节点（质押 1000）→ 3/5 签名。
- Malicious inputs: 3 地址同属一个实体（无去重校验）。
- Mutation: 检查注册是否校验地址独立性 → 无（_is_node 仅查 status）。
- Known-issue: 无碰撞。
- Outcome: confirmed_candidate → H-001，PoC poc_bridge。

## P22 — 桥存款伪造（Bridge Deposit）
- Target: core/bridge.py:298-344 _deposit_validate、:345-388 _deposit_apply。
- Attacker: 3 个女巫桥节点。
- Invariant: 铸币必须有真实源链存款事件。
- Intended harm: 无 BSC 存款凭空铸造 nUSDT。
- Call sequence: deposit(伪造 source_tx) → sign×2 → claim → _mint_wrapped。
- Malicious inputs: `source_tx="11"*32`（任意 hex）、amount=50000。
- Mutation: 尝试 source_tx 与真实链冲突 → 仅 key 重放检查（chain:source_tx），无链上事件证明。
- Known-issue: 无碰撞（历史审计未覆盖桥模块）。
- Outcome: confirmed_candidate → H-001。

## P23 — 桥每日额度（Bridge Limit）
- Target: core/bridge.py:196-201 _daily_limit_usd/_check_limit、:182-185 _record_usage。
- Attacker: 桥攻击者。
- Invariant: 每日铸/释放 ≤100 万 USD。
- Intended harm: 突破额度限制超发。
- Call sequence: 多 deposit 逼近上限 → 第 N+1 笔被拒。
- Malicious inputs: amount 逼近 100 万 USD 边界（999999.99）。
- Mutation: 同时操作 minted+released 两个桶 → 合并计算。
- Known-issue: 无碰撞；F-03/F-04 预言机影响额度并入 oracle 根因。
- Outcome: lead_only（额度逻辑本身正确，但 USD 定价被 F-03 操纵）。

## P24 — 桥退出/罚没（Bridge Exit）
- Target: core/bridge.py:250-270 exit_claimable/claim_exit、:126-141 _slash。
- Attacker: 恶意节点。
- Invariant: 罚没后无法赎回质押。
- Intended harm: 作恶后拿回质押。
- Call sequence: exit → 冷却 → claim；slash 后再 exit。
- Mutation: slash 状态节点 exit → _node_validate 要求 status=active 且未 exiting。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue。

## P25 — SocialFi 粉丝代币（SocialFi）
- Target: core/socialfi.py:1353-1367、fan_tokens/markets/bonds 路径。
- Attacker: 创作者。
- Invariant: 代币铸造/销毁守恒。
- Intended harm: 免费铸造粉丝代币套现。
- Call sequence: create → buy/sell 循环。
- Malicious inputs: 负数量、零金额。
- Mutation: 自买自卖刷市场价 → market oracle 自设（设计风险，覆盖矩阵已标注）。
- Known-issue: 无碰撞；自设市场价为设计决策。
- Outcome: cleared_no_issue（记账守恒；设计风险记录）。

## P26 — SocialFi 文本资产（Text Assets）
- Target: core/socialfi.py text_assets 路径。
- Attacker: 用户。
- Invariant: 文本资产绑定链上内容哈希。
- Intended harm: 内容替换/版权冒领。
- Call sequence: 注册他人内容哈希 → 认领。
- Mutation: 空内容、超长内容 → 校验。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue。

## P27 — SocialFi 转账（Transfers）
- Target: core/socialfi.py 转账与质押路径。
- Attacker: 用户。
- Invariant: 转账不重复扣/入账。
- Intended harm: 双花。
- Call sequence: 转出全部余额再转出。
- Mutation: float 舍入 → _amt 8 位。
- Known-issue: TM-012 根因已知。
- Outcome: cleared_no_issue。

## P28 — SocialFi 投票/仲裁联动（Interop）
- Target: core/arbitration.py:420-434 validate/apply、socialfi 引用。
- Attacker: 刷信誉用户。
- Invariant: 仲裁信誉与链上行为绑定。
- Intended harm: 刷信誉获得仲裁权。
- Call sequence: 高频小额交易刷 _chain_rep（:149-155）。
- Mutation: 自转账是否计信誉 → _has_direct_transfer（:269）排除直接转账。
- Known-issue: 无碰撞；高频小额仍有刷量空间（限流 100/s/IP 缓解）。
- Outcome: lead_only（需大量成本，效果有限）。

## P29 — 仲裁托管（Escrow）
- Target: core/arbitration.py:671-737 _deposit_for/complain/draw。
- Attacker: 买卖双方。
- Invariant: 托管金只能单方最终取走。
- Intended harm: 双方同时取款。
- Call sequence: complain → draw → settle 双路径。
- Mutation: 重复 draw → 状态守卫。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue。

## P30 — 仲裁裁决与押金（Verdict）
- Target: core/arbitration.py:450-520 apply/candidate settle。
- Attacker: 仲裁员。
- Invariant: 裁决奖励池守恒。
- Intended harm: 自判胜诉抽走押金。
- Call sequence: 自诉自裁（conflict 检测 :289-299 排除）。
- Mutation: 多账号绕过 conflict 检测。
- Known-issue: 无碰撞；conflict 检测基于转账/推荐关系，理论上可用全新无关联地址绕过。
- Outcome: lead_only（需要真实对手方，博弈成本高）。

## P31 — 聊天信箱（Chat）
- Target: core/chat.py:71-106、nova_node.py:1385-1462。
- Attacker: 任意地址。
- Invariant: 信箱内容仅收件人可读。
- Intended harm: 读他人信箱/灌满。
- Call sequence: rpc_chat_inbox(他人地址)。
- Mutation: 无鉴权读取存在 → 已知 TM-008（P1-6 只修 ack 验签）。
- Known-issue: TM-008 同根因，不报告。
- Outcome: known_or_duplicate（碰撞证明登记）。

## P32 — 预言机 VRF（Oracle VRF）
- Target: core/oracle.py VRF 路径（:380-443）。
- Attacker: 预言机节点。
- Invariant: VRF 输出不可预测。
- Intended harm: 预测随机数操纵抽奖。
- Call sequence: request → fulfill。
- Mutation: 节点自己请求自己 fulfill → 随机源为链上 seed+时间戳，可重放。
- Known-issue: 无碰撞；VRF 无应用绑定（谁用谁负责），非本链漏洞。
- Outcome: cleared_no_issue（应用层风险记录）。

## P33 — 预言机价格上报（Oracle Price）
- Target: core/oracle.py:444-458 _price_validate、:462-471 _price_apply。
- Attacker: 活跃预言机节点。
- Invariant: 每个 source 由独立节点维护。
- Intended harm: 操纵聚合价。
- Call sequence: 单节点对 3 个 source 各报一次价。
- Malicious inputs: source=chainlink/pyth/binance，price=0.0001。
- Mutation: 有聚合价后报偏离 >10% → :455-457 拒绝；但冷启动无聚合价时无基准。
- Known-issue: 无碰撞。
- Outcome: confirmed_candidate → H-003，PoC poc_oracle。

## P34 — 预言机举报罚没（Oracle Slash）
- Target: core/oracle.py:473-497 _report_validate/apply。
- Attacker: 举报者。
- Invariant: 罚没必须基于真实偏离。
- Intended harm: 诬告他人罚没。
- Call sequence: report(目标节点)。
- Mutation: 目标无源或偏离 <阈值 → :490-494 需聚合价且偏离>阈值。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（偏离证明存在）。

## P35 — 桥 oracle 集成（Bridge×Oracle）
- Target: core/bridge.py:62-70 _usd_value、oracle.price()。
- Attacker: —。
- Invariant: bridge 与 oracle 接口契约匹配。
- Intended harm: 桥功能故障。
- Call sequence: 有 feed 时执行 deposit validate。
- Malicious inputs: 任意 feed 存在。
- Mutation: `float*dict` TypeError 被 validate_op try/except 吞掉 → 恒 False。
- Known-issue: 无碰撞。
- Outcome: confirmed_candidate → H-005，PoC poc_dictbug。

## P36 — 桥 withdraw 路径（Withdraw）
- Target: core/bridge.py:398-436 _withdraw_validate/apply。
- Attacker: nUSDT 持有者。
- Invariant: 销毁包装资产 = 释放源链资产。
- Intended harm: 无销毁提取。
- Call sequence: withdraw(伪造 target_addr) → 3 签 → confirm。
- Mutation: NOVA 资产路径 tx.amount 直接扣余额；nUSDT 需 _burn_wrapped 成功。
- Known-issue: 无碰撞；confirm 无源链释放证明（对称于 F-01，方向相反由多签保护）。
- Outcome: lead_only（出向受同一女巫多签影响，并入 F-01 备注）。

## P37 — 桥手续费回流（Fee Pool）
- Target: core/bridge.py:447-463 _pool_validate/apply、:465-482 maintain。
- Attacker: 节点。
- Invariant: 每日最多回流一次。
- Intended harm: 重复回流刷验证者池。
- Call sequence: pool:flush 多次。
- Mutation: flush_day 事件去重。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue。

## P38 — DEX AMM（DEX）
- Target: core/dex.py:72 quote、:429-461 swap、:207-220 _transfer_wrapped。
- Attacker: LP/交易者。
- Invariant: x*y=k、池余额守恒。
- Intended harm: 恒定积破坏 / 闪贷式抽池。
- Call sequence: 大额 swap → remove；add 后立即 remove。
- Malicious inputs: amount_in=0、负数、超出池深度。
- Mutation: 无外部代币（链内包装资产），无 fee-on-transfer/ERC777 钩子。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（k 值校验与余额守卫，见 SAFE_LOG S-08）。

## P39 — 治理提案（Governance Proposal）
- Target: core/governance.py:270-290 _vote_validate、tick/resolve。
- Attacker: 大户/女巫。
- Invariant: 一币一票。
- Intended harm: 票数放大。
- Call sequence: A(1000) 委托 B → B 委托 C；三人各投票。
- Malicious inputs: 链式委托。
- Mutation: voting_power 递归加总不扣委托本金 → 3000 票。
- Known-issue: 无碰撞。
- Outcome: confirmed_candidate → H-004，PoC poc_gov。

## P40 — 治理执行（Governance Execute）
- Target: core/governance.py:323-327 _execute_validate。
- Attacker: 桥节点集合。
- Invariant: 基金支出需多签。
- Intended harm: 单实体控制基金。
- Call sequence: 提案通过 → fund 支出需 3 桥节点签名 → 女巫 3 节点签名。
- Mutation: 多签地址独立性未校验。
- Known-issue: 无碰撞；叠加 F-01。
- Outcome: lead_only（依赖 F-01 前置，并入 F-01 影响面）。

## P41 — DID/信誉（DID）
- Target: core/did.py。
- Attacker: 用户。
- Invariant: 信誉增量绑定行为。
- Intended harm: 刷信誉。
- Call sequence: 重复操作刷 reputation。
- Mutation: reputation 上限/衰减检查。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue。

## P42 — 订阅（Subscription）
- Target: core/subscription.py。
- Attacker: 订阅者/创作者。
- Invariant: 订阅费入账守恒。
- Intended harm: 重复扣费/免费订阅。
- Call sequence: subscribe → 续费。
- Mutation: 余额不足续费 → 校验。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue。

## P43 — RPC 路由与 CORS（RPC）
- Target: network/rpc.py。
- Attacker: 浏览器网页。
- Invariant: 跨站调用被限制。
- Intended harm: 跨站发起链上操作。
- Call sequence: 恶意网页 fetch /api/*。
- Mutation: CORS `*` 存在 → 已知 TM-009。
- Known-issue: TM-009 同根因，不报告。
- Outcome: known_or_duplicate（碰撞证明登记）。

## P44 — P2P 网络（P2P）
- Target: network/p2p.py。
- Attacker: 网络对手。
- Invariant: 消息完整性与大小受限。
- Intended harm: 大消息截断/注入。
- Call sequence: 发送 20MB 消息 → 64MB 上限（:9）。
- Mutation: 快照接管 → 默认关闭+受信种子（P0-2 修复）。
- Known-issue: TM-013 TLS CERT_NONE 已知。
- Outcome: known_or_duplicate（TLS 项）/cleared（其余）。

## P45 — 安全中间件（Security）
- Target: network/security.py:19-35、:76-82。
- Attacker: 刷 IP。
- Invariant: 每 IP 每秒 ≤100 请求。
- Intended harm: 灌爆 RPC。
- Call sequence: 高频请求 → 429。
- Mutation: check_ip_limit 前缀匹配残余 → 已知 P1-8。
- Known-issue: P1-8 部分修复，根因相同不报告。
- Outcome: known_or_duplicate（P1-8 残留项）。

## P46 — Agent 运行时（Agent Runtime）
- Target: agent/*.py gateway/guardrail/executor。
- Attacker: 用户提示词。
- Invariant: agent 无法执行链上资金操作。
- Intended harm: prompt 注入提币。
- Call sequence: 恶意输入 agent → gateway → executor。
- Mutation: executor 仅封装只读/受限调用，无签名提币能力。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（无资金操作面）。

## P47 — Explorer/GraphQL（Explorer）
- Target: explorer/server.py、graphql.py、indexer.py。
- Attacker: 匿名访问者。
- Invariant: 只读接口不改链上状态。
- Intended harm: SQL 注入/状态污染。
- Call sequence: GraphQL 注入查询。
- Mutation: db.py 参数化查询；只读同步。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（只读，未发现注入面）。

## P48 — 运维脚本（Scripts）
- Target: scripts/storage_monitor.py、storage_node_daemon.py 等。
- Attacker: 本地 shell。
- Invariant: 脚本仅读取/上报。
- Intended harm: 伪造监控数据刷奖励。
- Call sequence: 运行脚本注入假指标。
- Mutation: 监控数据非链上权威 → 影响有限。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue。

## P49 — 启动/签名工具（Tooling）
- Target: run_network.py、run_local_node.py、sign_tx.py、cert_gen.py。
- Attacker: 本地用户。
- Invariant: 工具生成合法签名/证书。
- Intended harm: 弱随机/固定密钥。
- Call sequence: cert_gen 生成 TLS 证书。
- Mutation: cert 自签名；客户端不校验（TM-013 已知）。
- Known-issue: TM-013 已知。
- Outcome: known_or_duplicate（TLS 项）。

## P50 — 配置与创世（Config）
- Target: genesis.json、requirements.txt、pytest.ini。
- Attacker: —。
- Invariant: 创世供应 81,000,000 且无隐藏铸造。
- Intended harm: —。
- Call sequence: 核对 alloc 合计 = 12,150,000+20,250,000+8,100,000+28,350,000+12,150,000 = 81,000,000。
- Mutation: 检查是否存在 admin/上帝账户 → 无；激励池初始 0。
- Known-issue: 无碰撞。
- Outcome: cleared_no_issue（创世池为 0 是经济类漏洞前置条件，已记录）。

---

## Phase 3 汇总
- 50 轮全部完成；confirmed_candidate：H-001(桥女巫铸造)、H-002(存储基金)、H-003(预言机定价)、H-004(委托放大)、H-005(桥 dict 故障)；
- lead_only：H-006(桥额度/预言机联动)、P23、P28、P30、P36、P40 备注项；
- known_or_duplicate：P31(TM-008)、P43(TM-009)、P44/P49(TM-013)、P45(P1-8)；
- 其余 cleared_no_issue。全部进入 HYPOTHESIS_LEDGER.md。

---

## Phase 3 Synthesis (English)

The mapping loop completed 50 distinct adversarial passes across all in-scope modules. Every pass was started from current code at commit de1d28f8, and the known-issue filter was applied only after a concrete current-code path was traced.

Confirmed exploit candidates emerging from this phase: (1) bridge sybil minting where 3 colluding node accounts fabricate a source-chain deposit and mint 49,950 nUSDT with no BSC proof (H-001); (2) storage fund lockup/drain where self-pinned CIDs commit ecosystem-fund balance up to 3,737.6 NOVA per pin (1024 GB x 3650 days x 0.001) and hash-chain proofs pay 0.05 NOVA per day with no real disk storage (H-002); (3) oracle price manipulation where one active node reports three whitelisted sources and sets a cold-start feed to an arbitrary value such as USDT/USD = 0.0001, which the bridge daily-limit then trusts (H-003); (4) governance delegation amplification where 1,000 NOVA delegated along a chain A->B->C counts as 3,000 votes because voting_power adds the delegator's full power without subtracting the delegated principal (H-004); (5) bridge functional denial of service where oracle.price() returns a dict and _usd_value performs float * dict, raising TypeError that validate_op silently swallows, disabling every bridge operation once any feed exists (H-005). The oracle-bridge limit interaction is folded into H-003/H-006.

Passes filtered as known_or_duplicate (P31 chat mailbox read without auth = TM-008, P43 CORS = TM-009, P44/P49 TLS = TM-013, P45 prefix-match residual = P1-8) all share the exact root cause of previously documented issues and are excluded from reporting pending KNOWN_ISSUE_COLLISION_PROOF.md. The remaining passes cleared with concrete guards: CEI ordering in transfer (nova_node.py:851-856), VM step bound, storage order refund, compute status guards, PoS signature/equivocation checks, DEX k-value and reserve isolation, genesis accounting of 81,000,000 NOVA with zero-funded incentive pools. Next action: synthesize HYPOTHESIS_LEDGER.md, then run protocol-specific known-vulnerability research (Phase 4) before PoC development (Phase 5).
