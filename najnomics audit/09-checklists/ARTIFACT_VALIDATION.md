# ARTIFACT_VALIDATION.md — Phase 9 后审清单与校验器输出

## Phase 9 清单

- [x] 每个 Critical/High 有可工作 PoC（F-01/F-02/F-03/F-06 有，F-05 Medium 亦有）
- [x] 全部 finding 在固定 commit de1d28f8 上验证
- [x] 重复检查：一根因一报告（F-04 并入 F-03）
- [x] 严重级校准：对照 Phase 10 判定表（VALID + 约束）
- [x] 修复建议技术上正确（源码行级修复建议）
- [x] gas/优化与安全发现分离（无 gas 项混入）
- [x] 架构图/描述：02-architecture/ARCHITECTURE_AND_THREAT_MODEL.md
- [x] 不变量列表与验证：FORMULA_MUTATION_MATRIX.md / SAFE_VERDICT_PROOF_LOG.md / Phase 6 D81-D100
- [x] 工具输出复核：07-tools/（blocked 说明 + triage）
- [x] 报告可读性：FINDINGS.md 面向非专家可理解（摘要/影响/修复）

## Phase 9V 校验器输出

```text
（见下方追加的 validate_najnomics_artifacts.py 输出）
```

## 校验器实际输出（Phase 9V，2026-08-16）

```text
Najnomics artifact validation
============================
PASS: artifact gates satisfied
```

结论：机械校验门通过（exit 0）。由于 Phase 7 的 Nemesis 独立技能 blocked，最终报告仍如实标注
PARTIAL SKILL RUN，不宣称完整。
