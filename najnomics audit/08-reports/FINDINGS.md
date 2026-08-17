# FINDINGS.md — 审计发现（Phase 8）

仓库：C:\Users\Administrator\novachain（Nova 链，Python 非 EVM）
固定 commit：de1d28f8e37fbad89f8ac05ad478d661c875ad09
运行模式：PARTIAL SKILL RUN（Phase 7 Nemesis 不可用，见 07-tools）
严重级校准：对照「直接资金损失 / 前置条件 / 影响面」判定。

---

## [Critical] F-01 — 跨链桥女巫多签无储备铸造包装资产

### Summary
桥节点多签仅校验「3 个活跃节点签名」，不校验节点身份独立性，也完全不验证源链（BSC/ETH/Polygon）
存款事件的真实存在性。单实体注册 3 个桥节点后即可提交任意伪造的源链交易信息，经 3/5 多签后
在 Nova 链凭空铸造包装资产（nUSDT/nETH）。PoC 实测：无任何 BSC 存款证明铸造 49,950 nUSDT。

### Vulnerability Detail
- 文件：core/bridge.py
- 函数：`_is_node`(:124-128)、`_deposit_validate`(:298-344)、`_add_sig`(:142-147)、`_sigs_ok`(:149-150)、`_mint_wrapped`(:171-177)、`_deposit_apply`(:345-388)
- `_deposit_validate` 只检查：asset 白名单、chain 白名单、source_tx 为 64 hex、source_addr 为 0x 地址、
  amount 为正有限数、`chain:source_tx` 未重放、USD 额度。**没有任何字段证明源链存款真实存在**
  （无 BSC 区块/收据哈希可验证性，无跨链消息，无轻客户端证明）。
- 节点注册门槛仅为 1000 NOVA 质押（:258-267），无地址独立性/实体去重校验；
  `_sigs_ok` 只统计签名数量（3/5）。
- `_deposit_apply` 在 claim 时调用 `_mint_wrapped` 直接增加 supply 与用户余额。

### Impact
- 攻击者可无限伪造存款（每次更换 source_tx 即绕过重放检查），按每日 1,000,000 USD 额度铸造
  无储备包装资产；结合 F-03 价格操纵，额度计量失真可放大 10,000 倍（理论单日 10^10 nUSDT）。
- 无储备资产可流入 DEX 兑换原生 NOVA（D40 追踪），形成直接套利。
- 任何接受 nUSDT 的对手方将承担储备缺口损失；桥的 1:1 储备不变量被彻底打破。
- 攻击成本：3×1000 NOVA 质押（7 天冷却后可通过 exit_claimable 赎回）。

### Proof of Concept
运行：`poc_f01_bridge_sybil_mint.py`（05-pocs/）→ 输出：
```
deposit: pending sigs: 1
after 3 sigs: ready sigs: 3
minted nUSDT supply: 49950.0 user balance: 0
SYBIL MINT OK: attacker-created 50,000 nUSDT with no real BSC deposit
```
（user 字段取 source_addr=攻击者钱包，实际到账 49,950 nUSDT，见 poc_f01 复测。）

### Recommended Fix
1. 增加源链事件可验证性：桥节点必须提供可跨链验证的存款证明（如 BSC 区块头+SPV 证明或受信跨链
   消息源），链上校验证明字段与 source_addr/source_tx 一致。
2. 节点注册加入实体去重/惩罚性质押与多签节点身份审计；至少校验 3 个签名地址彼此独立
   （质押来源、注册时间、签名密钥派生）。
3. 铸造上限与储备金硬绑定：bridge_assets.supply 不得超出源链侧审计的存款总额。
4. 对超发资产建立熔断与罚没（slash）路径。

### References
- Ronin Bridge 多签密钥共谋、Wormhole 签名绕过等公开跨链桥事件（Phase 4.1 对照表）。

---

## [High] F-02 — 存储网络 pin 无真实性校验：生态基金锁定 + 假 PoSt 缓慢提取

### Summary
`nova:storage:pin` 允许任意地址（含存储提供者自身）对任意不存在的 CID 提交 pin，生态基金按
size×days×0.001 直接扣款注入「固定奖励池」；随后同一地址可注册为提供者、claim 该 CID 并以
「自选秘密哈希链」逐日提交存储证明提取奖励。证明只证明「知道一个自选秘密」，不证明真实存储。
PoC 实测：2 个最大 pin 锁定 7,475.2 NOVA（基金 10,000→2,524.8），攻击者提取 7.5 NOVA。

### Vulnerability Detail
- 文件：core/storage_network.py、nova_node.py
- 函数：`pin_reward`(:59-63)、`pin`(:71-84)、`claim`(:86-105)、`proof`(:107-127)、
  `_validate_storage_op`(nova_node.py:318-327 / :341-345)
- pin 校验只查：CID 格式、未重复、size∈(0,1024]、days∈[1,3650]、基金余额充足。
  **无每地址 pin 数量上限、无文件存在性证明、无创作者身份证明。**
- 哈希链 PoSt 的链顶/揭示全部由攻击者自选 secret 生成；RPC 校验仅验证 `sha3_256(reveal)==tip`
  与每日一次（nova_node.py:341-345）。`storage_net.proof` 内部**无任何校验**（纵深缺口）。
- 前置条件：ECOSYSTEM_FUND 需先有余额（创世为 0，genesis.json alloc 仅 5 个 EOA）。

### Impact
- 基金被无效锁定：单 pin 最多 3,737.6 NOVA（1024GB×3650d×0.001），无上限 pin 数。
- 提取速率 0.05 NOVA/天/份（economy.py:53），实质为「基金冻结 + 缓慢套现 + 存储网络空转」。
- 攻击者可 pin 至基金枯竭，阻塞真实创作者/提供者的合法激励。

### Proof of Concept
运行：`poc_f02_storage_fund_drain.py`（05-pocs/）→ 输出：
```
pinned 2 CIDs, committed 7475.20 NOVA from fund -> pools; fund 10000.00 -> 2524.80
attacker extracted 7.50 NOVA via fake hash-chain proofs (no real storage)
attacker balance: 7.5
fund remaining: 2524.8
```
另见 `poc_f02_storage_fund_lockup.py`（演示无注资时 pin 被基金余额守卫拒绝）。

### Recommended Fix
1. pin 增加真实内容证明：要求 CID 对应已上链的内容哈希/内容注册记录，且 pin 者与内容作者关联。
2. 将哈希链证明升级为真实时空证明（PoSt）或至少绑定 IPFS 存储证明（模块 docstring 已计划）。
3. 增加每地址 pin 数量/总额上限与基金单日注资上限。
4. `storage_net.proof` 增加与 nova_node 同级的内部校验（纵深防御）。
5. 奖励池余额守卫 + 每日基金出账限额。

### References
- Filecoin PoSt 设计对照（模块 docstring）；存储激励「自我证明」类漏洞模式（Phase 4.2）。

---

## [High] F-03 — 预言机节点可女巫上报多数据源：冷启动任意定价（含桥计量 F-04）

### Summary
`nova:oracle:price:update` 不校验「上报者与数据源」的绑定关系：一个活跃节点可同时为
chainlink/pyth/binance 等多个 source 写入价格。当 feed 无聚合价（冷启动）时，无任何偏离校验，
首个上报即成为聚合价。PoC 实测：单节点 3 源上报使 USDT/USD=0.0001。桥的 USD 计量（F-04）
完全信任该价格，导致日额度/手续费计量失真（放大 10,000 倍）。

### Vulnerability Detail
- 文件：core/oracle.py
- 函数：`price`(:181-225)、`aggregate`(:227-241)、`_price_validate`(:444-458)、`_price_apply`(:462-471)
- `_price_validate` 检查：节点活跃、feed/source 白名单、价格范围；**不检查 source 是否已由该节点
  上报过、不检查同一节点对多 source 的一致性**。
- 偏离校验（>10% 拒绝）仅在「已有聚合价」时生效；冷启动 feed 直接跳过。
- `aggregate` 对 ≥2 源取中位数并剔除偏离 >5% 的源；当攻击者控制多数源时，真实源被剔除
  （4 源中 3 源 0.0001 → 聚合 0.0001）。
- 影响面：`core/bridge.py:62-70 _usd_value` 消费聚合价 → 桥日额度、大额延迟、手续费全部失真（F-04，
  与 F-03 同根因合并报告）。

### Impact
- 冷启动/被操纵 feed 可任意定价；桥额度按操纵价放大（50,000 nUSDT 计为 5 USD → 单日铸币上限
  10^10 nUSDT）。
- 手续费 `max(amount*0.001, 1/price)` 在 price=0.0001 时 min_fee=10,000 nUSDT，吞噬用户资产。
- 与 F-01 组合：女巫铸造在失真额度下几乎不受限。

### Proof of Concept
运行：`poc_f03_oracle_sybil_price.py`（05-pocs/）→ 输出：
```
aggregated USDT/USD after attacker 3-source report: 0.0001
```
（脚本中 bridge._usd_value 联动部分因 F-05 抛 TypeError，演示了 F-05 的联动阻断；
修复 F-05 后 `_usd_value(50000 nUSDT)=5 USD`，见 Phase 6 N-04/N-59 数值核算。）

### Recommended Fix
1. 绑定 node→source：每个 source 仅允许一个节点维护；多节点共源需随机分组与防合谋设计。
2. 冷启动引入引导价（fallback 价、聚合历史、治理设定锚）或在冷启动期内禁止写入聚合。
3. 对「单节点多源」直接判罚；举报/罚没路径已存在（:473-497），需与绑定检查联动。
4. 桥引入 TWAP 或对 oracle 价格做时间平滑与偏离护栏。

### References
- 预言机女巫/冷启动定价类漏洞（Phase 4.3）；ChainSwap/Pawnfi 价格操纵事件。

---

## [Medium] F-05 — 桥 `_usd_value` 接口契约错误：float×dict 致整桥功能 DoS

### Summary
`core/bridge.py:62-70 _usd_value` 假定 `oracle.price(feed)` 返回数值，但 `Oracle.price()` 返回
聚合价 **dict**（core/oracle.py:181-189）。当任一 feed 存在时，`float(amount) * p` 抛 TypeError，
被 `validate_op` 的 try/except（:209-213）吞掉并恒返回 False → 桥全部 deposit/withdraw 操作不可用。
无 feed 时（fallback 路径）桥反而正常，故障条件为「任一 feed 存在」。

### Vulnerability Detail
- 文件：core/bridge.py:62-70（`_usd_value`）、:204-215（`validate_op` try/except）
- `Oracle.price()`：普通 feed 返回 `oracle_feeds[feed]` dict；派生 feed 返回 float；
  无 feed 返回 None。三种返回类型并存，调用方未做类型契约处理。
- 影响所有调用 `_usd_value` 的路径：deposit 校验（:332-334）、withdraw 校验（:425-427）、
  `_fee`（:72-75）、大额判定。

### Impact
- 一旦预言机存在任何 feed（正常运营即如此），跨链桥整体不可用：入金/出金全部被拒（功能 DoS）。
- 与 F-03 组合：修复 F-05 后桥立即暴露于操纵价（F-04），需同步修复。

### Proof of Concept
运行：`poc_f05_bridge_usd_value_typeerror.py`（05-pocs/）→ 输出：
```
TYPE ERROR (bridge broken): unsupported operand type(s) for *: 'float' and 'dict'
deposit validate with live feed: False
```

### Recommended Fix
1. 在 `_usd_value` 内对 `p` 做类型归一：`float(p["price"]) if isinstance(p, dict) else float(p or fallback)`。
2. 或让 `Oracle.price()` 返回数值并新增 `price_record()` 返回完整 dict，统一契约。
3. 消除 validate_op 的裸 try/except 吞异常行为，改为按异常类型记录并区分「拒绝」与「故障」。

### References
- 接口契约（API 返回类型）类缺陷模式（Phase 4.1 桥-预言机耦合行）。

---

## [High] F-06 — 治理委托投票权放大：一币多票可主导治理

### Summary
`Governance.voting_power` 计算投票权为 余额+质押+锁仓+Σ 委托方投票权（递归），但**不扣除
委托方已委托的本金**。链式委托 A→B→C 使同一 1,000 NOVA 在 A/B/C 三人各计 1,000 票，合计 3,000 票
（放大 3 倍；链长 N 放大 N+1 倍）。PoC 实测 1,000 NOVA → 3,000 票。攻击者可用两条 ~1,000 层委托链
单方逼近 2,025,025 票的 quorum（Phase 6 N-29 核算）。

### Vulnerability Detail
- 文件：core/governance.py
- 函数：`voting_power`(:55-67)、`_vote_validate`(:270-290)、`_resolve`(:95-118)
- `voting_power` 对每个「委托给 addr 的 delegator」递归累加其全额投票权；
  **未从 delegator 余额中扣除已委托金额**；`_seen` 仅防环（A→B→A 返回 0），不防放大。
- 投票实时计权，无快照；提案进入 voting 后委托仍可改变计票结果。
- quorum = circulating × 2.5%（81,001,000 → 2,025,025 票）；放大使单实体可控 quorum 与简单多数。

### Impact
- 攻击者可主导治理决议（参数调整、基金支出提案的多签前置之外的部分）、影响升级类提案
  （需超级多数 66.7%）。治理权=协议控制权。
- 与 F-01 组合：治理基金支出多签（`_execute_validate`:323-327）要求 3 桥节点签名，女巫节点
  可同时提供签名 → 基金支出被单实体控制。

### Proof of Concept
运行：`poc_f06_gov_delegation_amplify.py`（05-pocs/）→ 输出：
```
power(A) before: 1000.0
power(A) after deleg: 1000.0
power(B): 1000.0
power(C): 1000.0
sum(A+B+C): 3000.0
circulating (true supply): 81001000.0
```

### Recommended Fix
1. 委托应扣减委托方投票权：`voting_power(delegator)` 中减去其委托给本地址的金额，或改为
   「净余额 = balance - Σ(delegated_amount)」后再参与递归。
2. 增加全局不变量校验：Σ voting_power ≤ circulating_supply（可在 D85 基础上加测试）。
3. 投票采用快照（提案进入 voting 时冻结权力），并限制委托深度/总数。
4. 联署门槛与提案人权益（1000 NOVA）同样受放大影响，需一并修复。

### References
- 治理委托「本金未扣除」类漏洞模式（Phase 4.4）；一币一票不变量（D85/D46 证伪）。

---

## 报告说明
- F-04（预言机操纵传导桥计量）与 F-03 同根因，已并入 F-03 报告，不单列（一根因一报告）。
- 已知项（TM-003/008/009/011/012/013、P1-8 等）未重复报告，见 00-scope 与 KNOWN_ISSUE_COLLISION_PROOF.md。
- 严重级校准依据见 10-validation/FALSE_POSITIVE_ELIMINATION.md（各 finding 的 Phase 10 判定）。
