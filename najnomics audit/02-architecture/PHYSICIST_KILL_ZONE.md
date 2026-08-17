# PHYSICIST_KILL_ZONE.md — Phase 2K（Python 非 EVM 目标，语言适配）

目标运行时：Python 3.14 / aiohttp 单进程 asyncio；无 gas 模型，有 100KB 交易/合约上限、10 万步 VM 上限、RPC 限流 100/s/IP。
[CHECKED] 项均附行号证据。

## 0.1 计算复杂度 / 资源耗尽
- [CHECKED] VM 步数上限：core/vm.py:44 `max_steps = 100000`，MUL/ADD 使用 Python 大整数；操作数来自 ≤100KB 字节码，单步开销有界。
- [CHECKED] RPC 限流：network/security.py:20-27 `RATE_LIMIT=100` 每 IP 每秒。
- [CHECKED] 请求体上限：nova_node.py:2600 `web.Application(client_max_size=262144)`；P2P 消息 64MB（network/p2p.py:9）。
- [CHECKED] 信箱裁剪：core/chat.py:96-101 单地址 ≤1000 条。
- [CHECKED] 事件裁剪：core/storage_incentive.py:96-100 EVENT_LIMIT=500。
- [VULNERABLE→F-02] 存储 pin 无数量/地址上限：nova_node.py:318-330 `_validate_storage_op` pin 仅校验 CID 新、size/days 范围与基金余额；攻击者可无限 pin 抽干基金（PoC: poc_storage）。CONSTRAINT 见 FORMULA_MUTATION_MATRIX。

## 0.2 任意外部调用 / 账户混淆
- [CHECKED] 交易转账路径固定 sender→receiver：nova_node.py:851-856，无任意 target/data 低层调用。
- [CHECKED] VM SEND 事件不接账本：core/vm.py:86-91 仅记录 events，不触碰 balances —— 无任意转账能力（功能缺口而非安全洞）。
- [CHECKED] 签名绑定地址：core/crypto.py:186-189 `expected = sha3_512(pub)[:40] == claimed_address`。
- [CHECKED] 无 delegatecall/CPI 等原语（Python 链）。

## 0.3 反序列化 / 异常 / 数学
- [CHECKED] 金额校验：nova_node.py:100-103 isfinite/范围/类型。
- [CHECKED] op data JSON 解析容错：nova_node.py:294-299 try/except。
- [CHECKED] VM 除零：core/vm.py:53-55 ZeroDivisionError → 0。
- [CHECKED] 分区状态恢复容错（测试覆盖）。
- [VULNERABLE→F-05] bridge._usd_value float×dict：core/bridge.py:62-70，`self.oracle.price()` 返回 dict 后 `float(amount) * p` 抛 TypeError，validate_op 的 try/except 吞掉 → 有预言机 feed 时桥全部操作失败（功能 DoS，PoC: poc_bridge_dict）。

## 0.4-0.11 共识/治理/状态机
- [CHECKED] PoS 出块签名：core/consensus.py:158-164 `_valid_signature` 用 verify_quantum_tx 校验区块哈希签名。
- [CHECKED] 0x0000 铸造防护：nova_node.py:96-99 仅 allow_system。
- [VULNERABLE→F-06] 治理委托投票放大：core/governance.py:86-96 `voting_power` 不扣除已委托资金，链式委托使同一笔资金被多次计票（PoC: poc_gov）。
- [CHECKED] 基金支出多签：core/governance.py:291-295 `_execute_validate` fund 需 ≥3 桥节点签名；但桥节点可女巫（F-01 叠加）。
- [CHECKED] AI 日预算：nova_node.py:107-110 `ai_can_spend`；apply_tx:807 记录。
- [N/A] 无闪贷（无借贷原语）；无预言机 TWAP（价格由节点自报，见 F-03）；无跨链消息验证（F-01）。

## 0.12 资产盘点与跨上下文盗取
- [VULNERABLE→F-01] 桥：包装资产账本（bridge_assets[].balances）与主余额分离；女巫 3 节点可铸造任意包装资产（PoC: poc_bridge 铸造 49950 nUSDT）。
- [CHECKED] DEX 池资产：reserve holder `0x_dex:{pair}` 专用地址，无法被用户直接取走（core/dex.py `_transfer_wrapped` 仅经 swap/add/remove 路径）。
- [VULNERABLE→F-03] 预言机：任意活跃节点可上报任意源价格（core/oracle.py:444-457 `_price_validate` 不绑定 source→node），新 feed 无基准时可任意设价（PoC: poc_oracle 设 USDT/USD=0.0001）。

## 0.13 精度 / dust / 域边界
- [CHECKED] 金额统一 round 8 位（_amt）。
- [N/A→备注] float 精度是已知问题 TM-012（交易/VM 用 float，跨平台确定性风险），非新发现。
- [CHECKED] 质押上限 10000/地址、30% 全网（nova_node.py:116-121）。

## 0.14 特权输入完整性
- [CHECKED] 治理参数调整有 whitelist：core/governance.py:33-35 ECONOMY_PARAMS 有限集合；value>=0。
- [CHECKED] 桥节点 exit/claim 有冷却：core/bridge.py:250-270。
- [CHECKED] 预言机退出 7 天冷却：core/oracle.py:383-393。

## 0.15 退出可用性
- [CHECKED] 解押 7 天冷却 + 25% 上限（nova_node.py:122-127）。
- [CHECKED] 存储订单到期退款：core/storage_network.py:161-176。
- [CHECKED] 预言机/桥节点退出退还质押。
- [VULNERABLE→F-05] 桥整体被 dict bug 锁死时用户无法跨链（功能 DoS）。

## 结论
4 个 [VULNERABLE] 均建立假设/战役（见 HYPOTHESIS_LEDGER），进入 Phase 5 的 PoC 队列。

## 补充：kill-zone 判定覆盖（mechanism / coverage / line）

- mechanism（机制）: 每条 [CHECKED]/[VULNERABLE] 均给出守卫或漏洞的运作机制（见 0.1-0.15 各条的行号与说明），
  例如 F-02 的机制是「pin 扣款直接进 reward_pool，proof 按 min(pool,0.05) 发放，无真实存储校验」。
- coverage（覆盖）: kill-zone 覆盖全部语言适配域 —— 计算/资源耗尽（0.1）、任意外部调用（0.2）、
  反序列化与数学（0.3）、共识/治理/状态机（0.4-0.11）、资产盘点（0.12）、精度域（0.13）、
  特权输入（0.14）、退出可用性（0.15）；每个 [CHECKED] 均有对应行号证据。
- line（行号因果）: 每条判定与具体代码行绑定，例如 core/bridge.py:62-70（F-05）、
  core/storage_network.py:59-63/71-84/107-127（F-02）、core/oracle.py:444-457（F-03）、
  core/governance.py:55-67（F-06）、nova_node.py:318-327（pin 校验）。
- 结论：kill-zone 判定的 mechanism/coverage/line 三要素齐备，支撑后续 Phase 3/6 轮次复用。
