# SCOPE_AND_KNOWN_ISSUES.md — Nova 链 najnomics 审计（Phase 0A）

## 审计目标

- 目标仓库：`C:\Users\Administrator\novachain`（Nova 链，Python 实现）
- 固定 commit：`de1d28f8e37fbad89f8ac05ad478d661c875ad09`（2026-08-16 23:16:24 +0800, "feat(chain): 预言机/跨链桥/DEX/治理/DID/订阅/浏览器与水龙头模块，配套测试与文档"）
- 目标链：Nova 链自身（非 EVM，Python 节点；PoS/checkpoint 双共识；NexLang 合约 VM）
- 运行模式：完整 najnomics run（若受环境限制，最终报告标注 PARTIAL SKILL RUN）

## 在范围内（in-scope）

### 运行时与入口
- `nova_node.py`（RPC 路由、交易流水线 validate/apply、每日维护、状态持久化）
- `run_network.py`、`run_local_node.py`（本地多节点启动）
- `sign_tx.py`（离线签名工具）
- `cert_gen.py`（TLS 证书生成）
- `nexlang_compiler.py`（NexLang 编译器）
- `genesis.json`（创世状态）

### 核心模块 core/
- `transaction.py`、`blockchain.py`、`crypto.py`、`consensus.py`、`economy.py`、`vm.py`
- `socialfi.py`、`ai_service.py`、`arbitration.py`、`chat.py`、`compute.py`
- `storage.py`、`storage_network.py`、`storage_incentive.py`
- `oracle.py`、`bridge.py`、`dex.py`、`governance.py`、`did.py`、`subscription.py`

### 网络层 network/
- `rpc.py`、`p2p.py`、`security.py`

### Agent 运行时 agent/
- `runtime.py`、`engine.py`、`executor.py`、`gateway.py`、`guardrail.py`、`planner.py`、`perception.py`、`config.py`、`models.py`

### 浏览器/索引器 explorer/（新攻击面）
- `server.py`、`indexer.py`、`db.py`、`graphql.py`、`__main__.py`

### 生产脚本 scripts/
- `storage_monitor.py`、`storage_node_daemon.py`、`ai_musician_loop.py`、`e2e_full.py`、`e2e_storage_incentive.py`、`agent_runtime_demo.py`、`ai_creator_demo.py`、`compute_network_demo.py`

### 配置
- `requirements.txt`、`pytest.ini`

## 范围外（out-of-scope）

- 测试文件 `test_*.py`（仅作为证据引用）
- 临时修复脚本 `scripts/_*tmp.py`、`scripts/_fix*.py`、`scripts/_patch*.py`
- `novachain-web`（前端/扩展/SDK，独立仓库，威胁模型已覆盖，另行审计）
- 第三方依赖库源码、`.venv`、`.testdeps`、缓存目录
- CI/构建流程
- 问题类型范围外：纯 gas/性能优化（无安全影响）、代码风格、中心化风险若已在威胁模型中标注为设计权衡（标注说明）

## 历史审计报告

### 1. docs/AUDIT_2026-08-13.md（P0×3 / P1×6 / P2×8）
- 基线：后端 165 项测试全绿；范围含后端 + 前端

#### P0 修复核验
| ID | 问题 | 修复状态 | 证据 |
|----|------|---------|------|
| P0-1 | deploy creator 无签名校验可冒领奖励 | 已修复 | `nova_node.py:1116-1121` 验签；`:1122` 校验 creator 已存在 |
| P0-2 | P2P 16KB 截断大消息 | 已修复 | `network/p2p.py` 改换行分帧 `readuntil(b"\n")` + `MAX_MSG_BYTES=64MB` |
| P0-3 | VM 占位实现 | 已修复 | `core/vm.py` 现含 PUSH/STORE/MUL/SEND/RET/LOAD/ADD/SUB/DIV 真实执行 |

#### P1 修复核验
| ID | 问题 | 修复状态 | 证据 |
|----|------|---------|------|
| P1-4 | compute 结算竞态 | 已修复 | `core/compute.py` submit/_complete/expire 均带 status 前置守卫（:449/:467-471） |
| P1-5 | storage_network 无守卫 | 待核验 | 需读 `core/storage_network.py` |
| P1-6 | chat ack 无鉴权 | 已修复 | `nova_node.py:1440-1462` 验签 `ack:{addr}:{ids}` |
| P1-7 | deploy 0x0000 无内容校验 | 已修复 | `nova_node.py:98` 0x0000 需 `allow_system`；外部提交走完整签名 |
| P1-8 | security.py 内存膨胀/前缀匹配 | 部分修复 | `network/security.py`：device_fingerprints 上限 10 万、checkin_history 上限 30 条；request_log 仅按时间窗裁剪；`check_ip_limit` 仍用 `startswith(role)` 前缀匹配（残余） |
| P1-9 | 矿工在线时长离线也计入 | 部分修复 | `nova_node.py:1003` `delta = min(now-last, 86400)` 封顶每日 1 天；仍非真实在线度量（每日运行 1 分钟可累计 1 天） |

#### P2 记录（优化/一致性，非安全主项，不作重复报告）
P2-10 from_dict 重复恢复块；P2-11 RPC 校验文案不统一；P2-12 减半用墙钟；P2-13 TLS CERT_NONE；P2-14/15/16/18 前端问题（范围外）；P2-17 保存并发低风险。

### 2. novachain-threat-model.md（TM-001..TM-014，security-threat-model 技能产出）
- TM-001（0x0000 任意铸造）：已修复（C-01，`validate_tx` 需 allow_system）
- TM-002（P2P 快照接管）：已修复（C-02，快照同步默认关闭/受信种子）
- TM-003（PoS 补块/slash 滥用）：部分处理（H-03/H-04，b17ebcd）；补块超时判定仍用签名者自报时间戳（`consensus.py` 待核验）
- TM-004（AI 基金单监护人掏空）：已处理（H-04 支出审批，`ai_service.py:304-317` 待核验）
- TM-005（无签名签到/轻验证刷奖励）：开放（IP+设备指纹，客户端可伪造）
- TM-006（前端私钥明文）：范围外（前端）
- TM-007（扩展注入所有站点）：范围外（前端）
- TM-008（聊天信箱无授权读取/灌满）：部分（ack 已验签；inbox 读取仍无鉴权，`nova_node.py:1253` 待核验）
- TM-009（CORS `*` 跨站调用）：开放（`network/rpc.py` 待核验）
- TM-010（重复部署覆盖 creator）：已修复（nova_node.py:1122）
- TM-011（Ed25519 未校验 s<L）：待核验 `core/crypto.py`
- TM-012（float 金额精度）：开放（`core/transaction.py` canonical_amount 用 float）
- TM-013（TLS CERT_NONE）：开放（p2p.py:30-37 自签名客户端不校验）
- TM-014（CSP/安全头）：范围外（前端）

## 内联开发者标注（Inline Developer Notes）

- `core/storage.py:503`：`print("[WARNING] 检测到占位地址 {ph}")` — 占位地址处理路径，审计时关注
- 其余 TODO/FIXME/HACK/@audit 等标记：全仓库扫描未发现生产代码残留

## 假设

1. 节点当前以开发/演示为主；生产 PoS 模式尚未正式上线（沿用威胁模型假设）。
2. 链上 NOVA 视为具有经济价值，经济类威胁按最高风险处理。
3. RPC 无外部鉴权、CORS 放开，视为公网可达（沿用威胁模型假设 3）。
4. 本审计聚焦后端链（novachain）；前端/扩展/SDK 由另行审计覆盖。

## 审计过滤器

任何 Phase 1-10 发现必须与本文件对照：
- 范围外 → 不报告
- 与已知问题同根因 → 不报告（需 KNOWN_ISSUE_COLLISION_PROOF）
- 与已报告问题重复 → 不报告
- 其余真实、范围内、未报告 → 报告
