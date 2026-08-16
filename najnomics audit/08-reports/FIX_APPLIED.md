# F-01..F-06 修复核验报告

> 对应 najnomics 审计发现（`FINDINGS.md`），修复已合入工作区（未提交）。
> 核验方式：全量测试（262 passed）+ 真实节点管线攻击重放脚本（`VERIFY_FIXES.py`，17/17 通过）。

## 修复清单

### F-01（Critical）跨链桥女巫多签无储备铸造
- `core/bridge.py`
  - 新增 `NODE_MIN_AGE = 3600`：节点注册满 1 小时才能参与多签（第 30 行）。
  - `deposit:sign` 校验：签名交易必须携带 `source_tx` / `source_addr` / `source_amount`，且与存款记录完全一致（第 344-361 行）。
  - `withdraw:sign` 增加最小年龄校验（第 464 行）；apply 侧记录 `sig_observations` 留痕（第 394-398 行）。
- 测试：`test_bridge.py::test_bridge_sign_requires_observation_consistency_and_min_age`
- 重放验证：新注册节点立即签名被拒；无观察字段盲签名被拒；观察不一致被拒；合法路径（观察一致 + 年龄满足）仍可正常多签铸造。

### F-02（High）存储 pin 无真实性校验：基金锁定 + 假 PoSt 提取
- `core/storage_network.py`：模块常量 `MAX_PINS_PER_ADDR = 200`、`MAX_PIN_COMMIT_PER_ADDR = 5000.0`（第 33-34 行）；`pin` 增加基金余额守卫，余额不足返回 0.0（第 76-77 行）；`claim` 禁止自认领（provider == owner 拒绝，第 99-100 行）。
- `nova_node.py::_validate_storage_op`：pin 分支增加每地址数量/承诺总额上限与基金余额检查（第 324-330 行）；claim 分支禁止自认领（第 334 行）。
- 测试：`test_storage_compute.py::test_storage_pin_limits_and_self_claim`
- 重放验证：大额 pin 第 2 个（超 5000/地址）被拒；基金余额不足时 pin 被拒；固定者自认领被拒；其他注册提供商认领仍可用。

### F-03（High）预言机节点女巫多源定价（含 F-04 桥计量）
- `core/oracle.py`
  - `aggregate`：要求 ≥2 个独立节点维护的源（第 235-237 行）。
  - `_price_validate`：绑定 node→source，同一节点同一 feed 只能上报一个源；已有源不可被其他节点接管（第 463-472 行）。
- 测试：`test_oracle.py::test_oracle_node_source_binding`
- 重放验证：同一节点第二源被拒；接管他人源被拒；两个独立节点上报后正常聚合。

### F-05（Medium）桥 `_usd_value` float×dict 类型错误 DoS
- `core/bridge.py::_usd_value`：`px = p.get("price") if isinstance(p, dict) else p`（第 71 行），兼容预言机 dict 返回。
- 测试：`test_bridge.py::test_bridge_usd_value_accepts_dict_feed`
- 重放验证：dict feed 下 `_usd_value` 正常返回数值，deposit 校验不再被阻断。

### F-06（High）治理委托投票权放大
- `core/governance.py`：`voting_power` 对已委托地址返回 0；新增 `_gross_power` 递归（仅收集未委托方资产 + 收到的委托，防环，第 55-65 行）。
- 测试：`test_governance.py::test_gov_delegation_chain_no_amplification`
- 重放验证：A→B→C 委托后 A/B 票权为 0、C=3000 无放大；委托环（D→A→B→C）不放大也不死循环。

## 回归结果
- 全量测试：`262 passed`（含 4 个新增回归测试；临时目录问题通过 `--basetemp` 规避，与代码无关）。
- 攻击重放脚本 `VERIFY_FIXES.py`：17/17 通过（经 `NovaNode.validate_tx -> apply_tx` 真实共识管线）。
- 注：审计期遗留的旧 PoC 脚本多绕过节点校验直连模块 API（`bridge.apply_op` / `sn.pin`），不属于链上可达路径，不再作为漏洞复现依据；链上入口统一经过 `validate_tx`。
