# FINAL_REPORT.md — Nova 链安全审计最终报告（PARTIAL SKILL RUN）

## 报告信息

- 目标：C:\Users\Administrator\novachain（Nova 链，Python 非 EVM 节点；PoS/checkpoint 双共识；NexLang VM）
- 审计 commit：de1d28f8e37fbad89f8ac05ad478d661c875ad09（固定）
- 审计方法：najnomics 对抗式工作流（Phase 0A-10 全流程）
- 运行模式：**PARTIAL SKILL RUN** —— Phase 7 的 Nemesis 独立技能不可用（无 Claude Code、网络受限），
  已记录 blocked 说明与自建替代；其余阶段证据完整（见 SKILL_EXECUTION_UPDATE.md）。
- 测试基线：pytest 261 passed（.venv-test，Python 3.14，无 oqs → Ed25519 回退）

## 执行摘要

共发现 **5 个根因、6 项报告条目**（F-01..F-06，其中 F-04 并入 F-03 同根因报告）：

| ID | 严重级 | 标题 | 受影响模块 | PoC 复现 |
|---|---|---|---|---|
| F-01 | Critical | 桥女巫多签无储备铸造包装资产 | core/bridge.py | ✅ 49,950 nUSDT 凭空铸造 |
| F-02 | High | 存储 pin 无真实性校验：基金锁定+假 PoSt 提取 | core/storage_network.py | ✅ 锁 7,475.2 / 提 7.5 NOVA |
| F-03 | High | 预言机单节点多源冷启动任意定价（含桥计量 F-04） | core/oracle.py | ✅ USDT/USD=0.0001 |
| F-05 | Medium | 桥 `_usd_value` float×dict 类型错误整桥 DoS | core/bridge.py:62-70 | ✅ 有 feed 时桥全断 |
| F-06 | High | 治理委托链投票放大（一币多票） | core/governance.py:55-67 | ✅ 1,000 NOVA→3,000 票 |

（F-04 为 F-03 的下游影响，与 F-03 同根因，合并报告。）

## 关键不变量被打破

1. 桥 1:1 储备：包装资产 supply 必须由源链真实存款支撑（F-01 打破）。
2. 激励真实性：存储奖励必须对应真实存储（F-02 打破）。
3. 价格完整性：聚合价必须反映独立多源市场（F-03 打破）。
4. 一币一票：Σ投票权 ≤ 流通供应（F-06 打破）。
5. 接口契约：模块间返回类型必须匹配（F-05 打破）。
内部记账守恒类不变量（余额守恒、池非负、快照确定性、副本上限等）经 100 轮回归全部成立。

## 已知问题处理（不重复报告）

- TM-003 补块时间戳、TM-008 聊天读取无鉴权、TM-009 CORS、TM-011 s<L、TM-012 float、
  TM-013 TLS、P1-8 前缀匹配残余 → 与历史报告同根因，见 KNOWN_ISSUE_COLLISION_PROOF.md。
- P0-1/2/3、P1-4/5/6/7 修复核验通过（00-scope/SCOPE_AND_KNOWN_ISSUES.md）。

## 证据链

- Phase 0A-2K：00-scope/、01-build/、02-architecture/（覆盖矩阵、kill-zone、公式变异矩阵、SAFE 证明日志）
- Phase 3/3S：03-pass-logs/（50 轮）、HYPOTHESIS_LEDGER.md
- Phase 4/4C：04-known-vuln-research/
- Phase 5/5C：05-pocs/（6 个可复现 PoC + 5 个利用战役）
- Phase 6/6S：06-deep-dive/（100 轮 + 英文追踪 + 数值附录）、HYPOTHESIS_LEDGER.md 对账
- Phase 7：07-tools/（blocked 说明 + nemesis 自建替代）
- Phase 9/9V/10：09-checklists/ARTIFACT_VALIDATION.md、10-validation/（误报消除 + 自我挑战）

## 修复优先级建议

1. P0（上线前必须）：F-01（桥源链证明+节点独立性）、F-03（node→source 绑定+冷启动锚）。
2. P1（尽快）：F-05（契约归一，一行级修复）、F-06（委托本金扣减+快照）、F-02（pin 真实性+限额）。
3. 验证：修复后重跑 261 项测试 + 6 个 PoC 回归；为每个修复增加不变量测试
   （supply≤储备、Σpower≤supply、feed 价格偏离护栏）。

## 限制

- PARTIAL SKILL RUN：Phase 7 无独立工具输出（Nemesis/x-ray/静态扫描均 blocked 或不可用），
  以 100 轮手工追踪与可复现 PoC 替代；若未来具备 Claude Code 环境，建议补跑 Nemesis 复核。
- 测试网/创世状态：激励池初始为 0，经济类漏洞（F-02/F-03 影响）需资金注入后触发，报告已注明前置条件。
- 前端/扩展/SDK（novachain-web）不在本次范围（另行审计）。
