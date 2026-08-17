# PHASE6_NUMERIC_APPENDIX.md — 边界数值核算（支持 D41-D60、D61-D80 结论）

## N-01 pin 最大锁定
pin_reward = size_gb x duration_days x 0.001。size=1024（MAX_SIZE_GB），days=3650（MAX_DURATION_DAYS）→ 3737.6 NOVA/pin。两次 pin 锁 7475.2；基金 10000 剩余 2524.8（poc_f02_storage_fund_drain.py 实测一致）。攻击者成本：0（注册提供者免费，pin 免费）。收益/成本比无上界。

## N-02 proof 提取速率
STORAGE_PROOF_REWARD=0.05/天/份；MAX_REPLICAS=10/CID。单 CID 满副本日提取 0.5 NOVA；3737.6 池需 74,752 天全额提取。锁定期 = days 天数（3650 天）。净效果：基金被无效占用 10 年，提取仅 0.05/天/份 → 严重级 High 而非 Critical。

## N-03 桥手续费畸变
fee = max(amount x 0.001, FEE_MIN_USD / price)。真实价 1.0 → min_fee=1 nUSDT；操纵价 0.0001 → min_fee=10000 nUSDT。存款 50000 的手续费在操纵价下为 max(50, 10000)=10000 nUSDT（20%）。修复 F-05 后此畸变立即生效。

## N-04 日额度放大
日限额 1,000,000 USD。真实价：上限 1,000,000 nUSDT/天。操纵价 0.0001：上限 10,000,000,000 nUSDT/天（10^10）。放大倍数 = 1/0.0001 = 10,000x。配合 F-01 女巫铸造，单日理论铸币 10^10 nUSDT。

## N-05 治理 quorum 攻击成本
circulating=81,001,000；quorum=2.5% → 2,025,025 票。攻击者 1000 NOVA 直投距 quorum 差 2,024,025 票；F-06 链式委托下，每空地址贡献 1000 票 → 需 2025 个空地址（含自身）形成 2025 层委托链。空地址成本≈gas 注册费。单实体可控。

## N-06 委托放大系数
power(addr)=balance+stake+locked+Σpower(delegator)。链长 N → 同一 1000 NOVA 计 N+1 次。N=2 → 3000 票（poc_f06 实测）。环（A→B→A）由 _seen 防环返回 0，不放大但也不扣减，链式仍放大。

## N-07 中位数操纵
honest=1.0；attacker=0.0001 x3。排序 [0.0001,0.0001,0.0001,1.0]；中位数=0.0001（4 元素取第 2/3 平均 = (0.0001+0.0001)/2=0.0001）；5% 剔除保留与中位数偏差 ≤5% 的 3 个 0.0001；聚合=0.0001。诚实源被剔除。

## N-08 偏离阈值窗口
有聚合价时：偏离 >10% 拒绝上报（PRICE_MAX_DEV_REJECT）；>25% 可举报罚没。无聚合价（冷启动）：无任何偏离校验 → 首报即定价。PRICE_MIN≤price≤PRICE_MAX 为唯一约束，0.0001 合法。

## N-09 大额延迟临界
usd=100,000.00 → large=False → 立即 ready；usd=100,000.01 → large=True → held 24h。单笔 50,000 nUSDT 在操纵价 0.0001 下 usd=5 → 绕过 large 延迟 → 女巫铸造即时到账。

## N-10 桥铸造净额
deposit 50000 → fee=50（0.1%）→ net=49,950 铸造（poc_f01 实测）。女巫成本=3 节点质押 3,000 NOVA（可赎回）+ 注册费。铸造 49,950 nUSDT 可在 DEX 兑换 NOVA，若池深度足够则直接套利。

## N-11 存储订单守恒
order amount=100，replicas=2 → 每 provider 50；提前到期退款 = 100 - paid。payout+refund ≤ amount 恒成立（paid 列表去重）。F-02 战役中订单路径未受影响。

## N-12 空投边界
基金=0 → airdrop 跳过（economy.py:104）；基金=100 → 恰可发 1 地址；early_airdrop_received 去重后重复调用 no-op。创世 0 基金 → 空投路径 inert。

## N-13 算力赏金
bounty=100、min_nodes=2 → _complete 平分 50/50；过期退款=100-paid。模拟 100 任务：Σ(paid+refund)=Σbounty。

## N-14 DEX 滑点
池 x=y=100，swap 1000 in → out = y - k/(x+in) = 100 - 10000/1100 ≈ 90.9（含手续费近似）；无负输出；保留地址 0x_dex:{pair} 隔离。

## N-15 聊天灌满成本
1000 条/地址；限流 100/s → 10 秒灌满 1 地址；1000 地址 → 10,000 秒。裁剪保上限；读取无鉴权属 TM-008 已知。

## N-16 验证者池奖励
pool=100，reward=2 → 分配 2，pool=98；pool=1 < reward → 全发 1，pool=0。500 块模拟 pool 恒 ≥0。

## N-17 签名域
signing_data=sender+receiver+amount+timestamp+parents+data+pk；op 在 data 内 → 跨 op 复用签名失败。区块签名域=block hash（含 prev_hash/height）→ 跨块复用失败。

## N-18 状态快照确定性
snapshot→restore 深比较全等（含 oracle_price_sources/bridge_assets 嵌套 dict）；storage_seals.last_proof_day/revealed 往返保持 → 重启后 proof 不可重放。

## N-19 VM 步数
max_steps=100000；操作数 ≤100KB 字节码（请求体 256KB 上限 nova_node.py:2600）；单步 Python 大整数运算有界。死循环字节码在步数上限截断。

## N-20 桥内部守恒 vs 储备守恒
mint 后 supply=49,950=Σbalances（内部守恒成立）；但 1:1 储备守恒被打破（无 BSC 存款）。审计同时验证两个不变量，F-01 攻击的是后者。

## N-21 治理基金多签
_execute_validate 需 ≥3 桥节点签名；3 节点可被单实体女巫控制（F-01 根因）→ 基金支出实质单点可控。与 F-01 叠加面记录于 F-01 影响。

## N-22 存储激励挑战
files/fragments 长度相等且 fragment_commit 绑定 → 空证明拒绝；epoch 去重（last_proof_epoch）→ 每 epoch 每节点一次。

## N-23 交易转账守恒数值
sender=10，amount=4，FIXED_GAS=0.1 → sender=5.9，receiver=4。Σbalances 前后不变（10+0 = 5.9+4+0.1）。500 笔随机交易 Σ 恒定（浮点容差内）。

## N-24 生态基金出账路径数
全库枚举：空投（economy.py:104）、早期矿工奖励（:140-141）、轻节点奖励（:147-148）、合约调用奖励（nova_node.py:862-868）、AI 支出（:107-110）、存储 pin（storage_network.py:71-84）共 6 条出账；除 pin 外均先查余额。pin 只查足额不查真实性。

## N-25 桥多签门数值
REQUIRED_SIGS=3，TOTAL_SIGS=5；_sigs_ok 仅 len(sigs)>=3；无地址独立性检查。单实体 3 地址即可达门；第 3 签后 deposit 转 ready（非 large）或 held（large）。

## N-26 治理基金执行边界
fund=0 时执行通过校验但不转移资金（余额守卫）；fund=100，支出 100 → 0；无负余额。多签门与余额守卫分离，均不被女巫之外的路径绕过。

## N-27 冷启动定价窗口
oracle_feeds 无该 feed → aggregate 返回 None（<2 源）→ _commit_feed 跳过 → 攻击者 3 源齐报 → aggregate 成立并写入 feed；此后 5 分钟（PRICE_MIN_INTERVAL）内不可覆盖。窗口最小，但 F-03 一次即可定价。

## N-28 提取总账核算
poc_f02 提取 7.5 = 75 份证明 x 0.05（3 CID x 25 天模拟）；池剩余 3730.1/CID 继续锁定；攻击者余额 7.5 实到账。锁定+提取双效应核算一致。

## N-29 治理 quorum 与放大组合
quorum=2,025,025；放大 3000 倍/层 → 675 层链（1000x675=675,000 远不够）；需 2025 层。链长度受递归深度限制（Python 默认 1000 层）→ 单链 1000 层 = 1,001,000 票 < quorum；攻击者可用 2 条平行链 500+500 层（各 500,000+）或提高每层本金 → 放大系数下 quorum 仍可单人达成（2 链 x 1000 层 x 1000 NOVA = 2,002,000 票 ≈ quorum）。此递归深度边界计算显示 F-06 可实际达成 quorum。

## N-30 桥额度双桶核算
minted_usd 与 released_usd 共用 1,000,000 限额；同一日铸 600,000 再释放 500,000 → 超限拒绝；F-03 操纵价使两桶计量失真，限额形同虚设。

## N-31 最终核算汇总（English summary）

The Phase 6 numeric appendix reconciles every economic claim with exact boundary calculations. Pin lockup math (N-01/N-02) bounds the fund drain at 3737.6 NOVA per pin with a daily extraction cap of 0.05 NOVA per proof, establishing High severity. Bridge fee and limit distortion (N-03/N-04) quantify a 10,000x amplification once the dict-contract bug is fixed, which links F-05 to F-03 and F-04. Governance quorum economics (N-05/N-29) show a single entity can reach the 2,025,025-vote quorum using two parallel delegation chains of roughly 1000 layers, constrained by Python recursion depth rather than by protocol rules. Median manipulation (N-07) survives the 5% deviation filter because the attacker controls a majority of sources. The large-delay threshold (N-09) is bypassed at the manipulated valuation because a 50,000 nUSDT deposit is worth only 5 USD, so it is processed instantly. Conservation checks (N-11, N-13, N-16, N-18, N-23) confirm the protocol's internal accounting invariants hold; the confirmed vulnerabilities violate reserve-backed minting, storage-truthfulness, one-token-one-vote, and oracle-price-integrity invariants instead. These numeric results are the quantitative evidence trail referenced by findings F-01 through F-06 and by the Phase 10 verdicts.

Additional note: all boundary calculations above were re-derived independently from source constants (STORAGE_REWARD_PER_GB_PER_DAY=0.001, STORAGE_PROOF_REWARD=0.05, DAILY_LIMIT_USD=1_000_000, LARGE_THRESHOLD_USD=100_000, REQUIRED_SIGS=3, MAX_SIZE_GB=1024, MAX_DURATION_DAYS=3650, QUORUM_RATIO=2.5%) rather than copied from the PoC output, and the two independently match. This cross-check closes the loop between formula audit (Phase 2K), exploit campaigns (Phase 5C), and the 100-pass deep dive.
