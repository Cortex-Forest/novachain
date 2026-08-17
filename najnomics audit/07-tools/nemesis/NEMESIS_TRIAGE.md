# NEMESIS_TRIAGE.md — 人工 triage（自建替代）

## 每条候选的人工复核结论

| 候选 | 来源 | 复核方式 | 结论 |
|---|---|---|---|
| 桥女巫铸造 | Feynman 质疑 1 | 重跑 poc_f01 + 读 bridge.py:298-373 | VALID → F-01 |
| 存储基金锁定/假证明 | 质疑 2 | 重跑 poc_f02 x2 + 读 storage_network.py:59-127 | VALID → F-02 |
| 预言机定价 | 质疑 3 | 重跑 poc_f03 + 读 oracle.py:444-471 | VALID → F-03 |
| 桥 dict 契约 | 质疑 4 | 重跑 poc_f05 + 读 bridge.py:62-70 | VALID → F-05 |
| 委托放大 | 质疑 5 | 重跑 poc_f06 + 读 governance.py:55-67 | VALID → F-06 |
| 聊天读取无鉴权 | 状态表 | 与 TM-008 根因比对 | known_or_duplicate |
| CORS / TLS / 前缀匹配 | 状态表 | 与 TM-009/TM-013/P1-8 根因比对 | known_or_duplicate |
| 状态记账不一致 | 状态挑战 | 全状态对核对 | 无新漏洞 |

## 对账
全部候选已人工复核并写入 HYPOTHESIS_LEDGER.md（Destination=phase8_finding / known_or_duplicate）。
正式 Nemesis 运行仍 blocked；Phase 7 结论：无新增根因，F-01..F-06 不变。
