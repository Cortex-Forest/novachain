# FORMULA_MUTATION_MATRIX.md — Nova 链公式/不变量变异审计（Phase 2K）

> 方法：对每条关键经济/状态公式列出「不变量(invariant)」「变异点(mutates)」「验证路径(validates)」「不对称(asymmetric)」「重排(reorder)」。
> 语言适配：Python 链，无 gas 计量；公式即浮点运算/记账，变异测试直接改参看影响。

## F-公式矩阵

| 公式 ID | 位置 | 公式 | 不变量 invariant | 变异 mutates | 验证 validates | 不对称 asymmetric | 重排 reorder |
|---|---|---|---|---|---|---|---|
| F-A 桥铸造 | core/bridge.py:171-177 `_mint_wrapped` | `supply += amount; balances[addr] += amount` | 包装资产 supply == Σbalances | 攻击者伪造存款 → `_deposit_apply` 在无源链事件时调 `_mint_wrapped` | `_deposit_validate`(:298) 只查格式/重放/额度，**不查**源链事件存在性 | 铸币无需任何源链签名证明，只有 3 个自报节点签名 | 先 claim 后 sign 被拒（:337-343 status 守卫），但 3 女巫节点 sign 顺序无关紧要 |
| F-B 存储固定 | core/storage_network.py:59-63 `pin_reward` | `reward = size_gb * duration_days * 0.001` | 生态基金余额应只被「真实文件固定」消耗 | 攻击者自选 size/days 上界（1024GB×3650d） | nova_node.py:322-327 仅限 range 与基金余额 | pin 者=创作者=提供者同一人，无需真实文件存在 | pin→claim→proof 顺序可全由单地址完成 |
| F-C 存储证明 | core/storage_network.py:107-127 `proof` | `reward = min(reward_pool, 0.05)` | 奖励只应支付给「真实存储者」 | 密封链顶/揭示全部由攻击者自选密钥生成 | nova_node.py:341-345 只验 `sha3_256(reveal)==tip`；**storage_net.proof 自身无任何校验**（防御纵深缺口） | 证明的是「知道一个自选秘密」，不是「存了文件」 | 顺序固定 claim→proof，但 claim 的 tip 攻击者自定 |
| F-D 委托投票 | core/governance.py:55-67 `voting_power` | `power = balance + stake + locked + Σ voting_power(delegator)` | Σ各地址投票权 ≤ 流通供应 | 委托链 A→B→C 使 1000 NOVA 计为 3000 票 | `_vote_validate`(:270) 直接调用 voting_power，无全局幂等 | 委托本金未从委托方扣除，递归加总造成放大 | 先委托后投票/先投票后委托均可利用；A 投票时仍含自己 1000 |
| F-E 预言机聚合 | core/oracle.py:227-241 `aggregate` | `median(剔除偏离>5%源)` | 聚合价应反映真实市场 | 单节点可对多个 source 同时上报（无 node→source 绑定） | `_price_validate`(:444) 不绑定上报者与 source；无聚合价时无偏离校验 | 新 feed 冷启动无基准：首报即聚合价 | 3 源先后上报顺序不影响结果 |
| F-F 桥 USD 换算 | core/bridge.py:62-70 `_usd_value` | `usd = amount * oracle.price(feed)` | 桥每日额度以真实 USD 计量 | `oracle.price()` 返回 dict 而非常量 → `float*dict` TypeError | validate_op(:204-215) try/except 吞异常 → 恒 False | 有 feed 时全部桥操作不可用；无 feed 用 fallback 正常工作 | — |
| F-G DEX k 值 | core/dex.py:72 quote / :429-461 swap | `x*y=k` | 池不变量 | 无 fee-on-transfer、无闪贷原语 | swap 路径固定 `_transfer_wrapped`(:207) 走包装资产 | 包装资产为链内记账，无外部代币钩子 | add/remove/swap 顺序由链内确定性重放保证 |
| F-H 转账 | nova_node.py:851-856 | `balances[sender]-=amount+gas; balances[receiver]+=amount` | 余额守恒 | validate_tx(:96-127) 金额 isfinite/范围 | apply_tx 扣款在入账前执行 | sender==receiver 时 amount 净 0（操作类 tx） | 无重入（单线程 asyncio + 同步 apply） |
| F-I 治理金支出 | core/governance.py:323-327 `_execute_validate` | `fund 支出需 ≥3 桥节点签名` | 基金支出需多签 | 桥节点可女巫（F-A 根因）→ 3 签名可被单实体控制 | 多签集合无去重校验地址独立性 | 攻击者注册 3 节点后基金即受控 | 与 F-01 叠加放大 |

## 不对称验证门（asymmetric gates）

1. **桥：验证强度不对称** — 铸造侧只验「3 个活跃节点签名」而**不验**源链事件（BSC 交易存在性/收款地址/金额一致性），单实体 3 节点即可通过（F-A/F-01）。
2. **存储：成本不对称** — pin 奖励成本由生态基金承担、pin 者零成本；证明成本是「生成一条自选哈希链」（本地 365 次 sha3，微秒级），收益 0.05 NOVA/份/天（F-B/F-C/F-02）。
3. **治理：权力不对称** — 委托不扣减委托方投票权，递归加总导致同一资产多次计票（F-D/F-06）。
4. **预言机：身份不对称** — 一个节点可同时扮演 3 个数据源，冷启动 feed 无基准价即被任意定价（F-E/F-03）。

## 重排测试（reorder tests）

| 操作对 | 反向顺序结果 | 结论 |
|---|---|---|
| bridge deposit:sign 先于 deposit | `_deposit_validate` :332-335 找不到 deposit → False | 安全（状态机守卫） |
| bridge deposit:claim 先于 3 签 | :337-343 status 非 held/ready → False | 安全 |
| storage claim 先于 pin | :328-330 claim 不存在 → False | 安全 |
| storage proof 先于 claim | :338-340 无 seal → False | 安全 |
| gov delegate 与 vote 交换 | 两者均可独立利用，无前置依赖 | 放大不受顺序限制 |
| oracle 3 源上报顺序 | 聚合取中位数，顺序无关 | 女巫定价不受顺序影响 |
| DEX remove 后 swap | 池余额不足则 _transfer_wrapped 失败 | 安全（余额守卫） |

## 结论
F-A..F-F 六条公式存在变异可利用性，映射为 H-001..H-006，进入 Phase 5 PoC 队列；F-G..F-I 变异被守卫杀死，记为 cleared。
