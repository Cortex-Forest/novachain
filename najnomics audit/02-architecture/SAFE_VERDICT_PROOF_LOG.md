# SAFE_VERDICT_PROOF_LOG.md — [SAFE] 判定证明日志（Phase 2K）

> 每个 [SAFE] 判定必须给出 CONSTRAINT（守卫）、MECHANISM（机制）、COVERAGE（覆盖）与行号因果。
> 覆盖范围：Phase 3/6 中作出 cleared_no_issue / killed_invalid 的路径，按模块登记。

## S-01 交易转账余额守恒 — [SAFE]
- CONSTRAINT：validate_tx 金额校验 nova_node.py:96-103（isfinite、范围、非负）；apply_tx 扣款 nova_node.py:851-853 在入账 :855 之前；FIXED_GAS 由同一发送者承担。
- MECHANISM：sender==receiver 的操作类交易金额净零（nova_node.py:851-856），余额不会凭空增减；无外部合约调用，无重入窗口（asyncio 单线程同步 apply）。
- COVERAGE：P11/P21、D21 全量追踪 validate→apply 路径；测试套件 261 项含转账/矿工费/合约调用奖励用例（01-build 记录 261 passed）。
- 变异尝试：把 receiver 入账提前到扣款前 → 需要负数余额；把 gas 从 receiver 扣 → 与 tx 语义冲突。两者均被现有代码顺序阻止。

## S-02 VM 步数与除零 — [SAFE]
- CONSTRAINT：core/vm.py:65 `max_steps=100000`；:53-55 ZeroDivisionError→0。
- MECHANISM：操作数来自 ≤100KB 字节码（nova_node.py:2600 请求上限），单步为 Python 大整数运算，步数上限保证资源有界；除零显式兜底。
- COVERAGE：P17/D28；测试含 VM 除零与步数用例。
- 变异尝试：无限循环字节码 → 步数上限截断；超大数 MUL → 大整数无溢出但耗时受步数限制。

## S-03 存储订单托管退款 — [SAFE]
- CONSTRAINT：core/storage_network.py:161-176 `_refund_order` 到期未发放部分退回 creator；:131-132 `_order_payout` 仅 active 订单且 provider 未领过。
- MECHANISM：托管金在 create_order(:139-147) 立即扣除进订单记录，未发放部分到期必退，不产生资金黑洞。
- COVERAGE：P20-22/D30-33；F-02 战役中该路径未受影响（攻击面在 pin/proof 而非订单）。
- 变异尝试：provider 重复领订单分成 → :136 `provider in order["paid"]` 守卫拒绝。

## S-04 算力市场结算竞态 — [SAFE]
- CONSTRAINT：core/compute.py:416-465 validate_submit / :467-471 `_complete` 带 status 前置守卫（publish/accept/submit 状态机严格单向）。
- MECHANISM：worker 提交时任务必须处于 assigned；_complete 后状态变为 completed，重复提交被拒。
- COVERAGE：P23/D38-40；历史 P1-4 已修复并有回归测试。
- 变异尝试：同一 worker 双提交 → validate_submit 第二次 False；任务方代 worker 提交 → 签名校验（sender==worker）阻止。

## S-05 聊天 ack 重放 — [SAFE]
- CONSTRAINT：nova_node.py:1440-1462 ack 需验签 `ack:{addr}:{ids}`；core/chat.py:96-101 信箱单地址 ≤1000 条。
- MECHANISM：ack 幂等（重复 ack 已读 id 无副作用）；inbox 读取无鉴权为已知问题 TM-008（另见碰撞证明），不重复报告。
- COVERAGE：P31/D56；P1-6 修复核验。
- 变异尝试：伪造 ack 任意地址 → 验签失败；灌满信箱 → 1000 条裁剪。

## S-06 0x0000 系统铸造防护 — [SAFE]
- CONSTRAINT：nova_node.py:96-99 仅 `allow_system` 时 0x0000 铸造；外部提交必须走完整签名（P0-1 修复）。
- MECHANISM：0x0000 铸造是内部维护路径，网络入口不可达；历史 P0-1 已加验签与 creator 存在性检查（:1116-1122）。
- COVERAGE：P1/D1；回归测试覆盖。

## S-07 PoS 出块签名与双签检测 — [SAFE]
- CONSTRAINT：core/consensus.py:166-168 `_valid_signature` 用 verify_quantum_tx 校验区块哈希签名；:183-193 `_detect_equivocation`。
- MECHANISM：区块必须由当选 proposer 私钥签名；双签（同一高度两区块）被检测罚没。
- COVERAGE：P13-15/D24-26；补块时间戳问题为已知 TM-003，见碰撞证明。

## S-08 DEX 池资产隔离 — [SAFE]
- CONSTRAINT：core/dex.py:207-220 `_transfer_wrapped` 仅经 swap/add/remove 路径转移；池余额存于保留地址 `0x_dex:{pair}`。
- MECHANISM：用户无法直接取走保留地址余额；swap 出入金按 k 值公式与余额守卫执行。
- COVERAGE：P38/D66-68；变体尝试（直接改池余额、负数量 swap）被守卫拒绝。

## S-09 治理参数白名单 — [SAFE]
- CONSTRAINT：core/governance.py:33-35 ECONOMY_PARAMS 有限集合且 value>=0。
- MECHANISM：治理只能调整白名单内参数，无法篡改代码路径或铸造函数。
- COVERAGE：P39-40/D69-72；F-06 战役中未发现参数逃逸。

## S-10 RPC 限流与重放 — [SAFE]
- CONSTRAINT：network/security.py:9 RATE_LIMIT=100；:19-23 每 IP 每秒窗口；:31-35 processed_txids 重放拒绝。
- MECHANISM：同一 txid 二次提交被拒；IP 超额 429。
- COVERAGE：P43-45/D75-77；P1-8 部分修复核验（前缀匹配残余为已知）。
- 变异尝试：txid 碰撞 → txid 由签名数据 hash 生成（transaction.py:26-28），碰撞需预映像攻击。

## S-11 生态基金空投/早期奖励余额守卫 — [SAFE]
- CONSTRAINT：core/economy.py:104、140-141、147-148 均先检查 `ECOSYSTEM_FUND >= 金额` 再扣减。
- MECHANISM：基金余额不足时奖励不发放，不会出现负余额。
- COVERAGE：P16/D27；创世基金为 0（genesis.json alloc 仅 5 EOA 共 81,000,000 NOVA），相关奖励路径在资金注入前不触发。

## S-12 DID/订阅/社交 记账 — [SAFE]
- CONSTRAINT：core/socialfi.py:1353-1367 统一 validate/apply；did.py/subscription.py 均走 nova_node 统一入口与金额校验。
- MECHANISM：所有状态写入经 validate 门 + apply 确定性重放，无外部调用。
- COVERAGE：P25-28/P41-42、D43-50/D73-74；市场 oracle 自设为设计风险（覆盖矩阵已标注）。

## 汇总
12 条 [SAFE] 判定全部带 CONSTRAINT/MECHANISM/COVERAGE 三要素与行号；4 条 [VULNERABLE]（F-01/F-02/F-03/F-05/F-06 五根因）进入假设账本与 PoC 队列。
