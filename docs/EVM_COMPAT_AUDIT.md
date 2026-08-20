# Nova 链 EVM 兼容层 · 安全审计报告（v0.11）

> 审计日期：2026-08-20 ｜ 范围：EVM 兼容层 / MetaMask RPC / 混合账户 / 跨引擎桥接 / Solidity 部署

## 一、审计范围

| 模块 | 文件 | 说明 |
|---|---|---|
| EVM 解释器 | `core/evm.py` | 纯 Python 轻量 EVM（操作码子集 + keccak-256 + secp256k1 + RLP） |
| MetaMask RPC | `core/evm_rpc.py` | 以太坊 JSON-RPC 标准接口（`/rpc`，与 `/api/*` 并行） |
| 混合账户 | `nova_node.py` | `nova:evm:bind` / `nova:evm:migrate` |
| 跨引擎桥接 | `core/evm_bridge.py` | 原生资产 ↔ EVM 包装（原子性 + 0.1% 手续费回流） |
| Solidity 示例 | `core/evm_examples.py` | SimpleStorage / ERC20Nova 真实字节码 + 源码模板 |

## 二、安全重点审计结论

### ① EVM 执行沙盒资源上限 ✅
- **内存上限**：`MAX_MEMORY = 64KB`，`_mem_expand` 超限抛错 → 执行失败，无状态提交。
- **步数上限**：`MAX_STEPS = 200_000`，无限循环终止。
- **存储槽上限**：`MAX_STORAGE_KEYS = 100_000`/合约，超限 SSTORE 失败。
- **Gas 计量**：操作码近似标准 gas，`gas_limit` 耗尽 → 失败回滚，不产生状态变更。
- **验证**：`test_evm_security.py::TestEvmSandbox`（4 项全过）。

### ② 跨引擎桥接原子性与重入防护 ✅
- **原子性**：`convert_apply`/`revert_apply` 采用余额/资产快照 → 扣源 → 铸目标 → 失败整笔回滚（`_rollback` 恢复 balances、evm_wrapped、原生属主）。
- **重入防护**：`eth_burn` 校验包装记录存在且 `evm_owner == from`；重复 burn 因记录已删除而拒绝。
- **确定性**：`tokenId = keccak256(asset_id)` 跨节点一致，无重复铸造。
- **手续费**：FT 0.1% / NFT 0.001 NOVA，100% 进 `0x_validator_pool`。
- **验证**：`test_evm_security.py::TestBridgeSecurity` + `test_evm_stress.py::TestBridgeConservation`（1000 笔守恒）。

### ③ 混合账户签名验证边界 ✅
- `bind`：ECDSA 公钥长度/hex 校验、EVM 地址唯一（防重复绑定）。
- `migrate`：ECDSA 签名（对确定性消息）验证、仅 native 属主、`migrated` 不可逆标志。
- 无效签名 / 非属主 / 重复绑定均拒绝。
- **验证**：`test_evm_security.py::TestHybridSecurity`。

### ④ RPC 以太坊标准兼容性 ✅
- `eth_call` 只读（快照回滚 balances/storage/evm_wrapped）。
- EIP-155 chainId 校验（非 666666 拒绝）。
- nonce 严格递增（`store.evm_nonce`）。
- 余额不足（value + gas）拒绝。
- **验证**：`test_evm_security.py::TestRpcStandards`。

## 三、兼容性回归（官方向量子集）

- keccak-256：空串 / "abc" 已知向量通过。
- RLP 编解码往返通过。
- ECDSA：私钥→公钥→验签→恢复地址全链路通过。
- EIP-155 签名交易解码（chainId/from/value/nonce）通过。
- SimpleStorage `set`/`get`、ERC-20 `totalSupply/balanceOf/transfer/approve/allowance/transferFrom/mint/name/symbol/decimals` 全流程通过。
- 部署交易（`eth_sendRawTransaction` to 为空）→ DAG 同步 → 标准回执通过。
- **验证**：`test_evm_compat.py`（17 项）。

## 四、压力测试

| 场景 | 结果 |
|---|---|
| 100 并发 SimpleStorage 调用 | ✅ 确定性 |
| 100 并发 ERC-20 转账总额守恒 | ✅ 10000 → 0，接收者各 50 |
| 原生 Actor 与 EVM 混合并发 | ✅ 各自账本一致 |
| 1000 笔跨桥 convert/revert 无资产丢失 | ✅ 10000 份守恒 |

## 五、已知限制与未来优化

### 已知限制
1. **操作码子集**：解释器覆盖 Solidity 0.8 常见字节码所需子集（含 CALL/STATICCALL、CREATE/CREATE2、事件、存储），未实现 `BLOCKHASH` 历史、`GASLIMIT` 精确语义、EIP-1559 `TYPED_TRANSACTION`（仅 legacy EIP-155）。
2. **gas 计量近似**：按黄皮书近似定价（非逐操作码精确），`gasUsed` 供回执展示，不做链上精确计费（链上仍按 `FIXED_GAS`）。
3. **嵌套 CREATE 内部化**：EVM `CREATE` 由解释器执行 init code，合约间调用不支持完整重入上下文切换（`RETURNDATASIZE` 在子调用后全局态）。
4. **预编译合约**：未内置 `ecrecover/sha256/ripemd160` 等 EVM 预编译；EvmBridge 包装资产查询走专用 handler（非字节码）。
5. **erc-1155 示例**：仅提供 `.sol` 源码（BlindBox），未内置预编译字节码。
6. **eth_getLogs**：按地址过滤的简化实现，无完整日志 bloom 索引。
7. **非 EIP-55 校验**：`eth_sendRawTransaction` 的 `from` 为恢复地址小写，未强制 checksum。

### 优化方向
1. 接入官方 EVM 测试向量（`ethereum/tests`）逐操作码回归。
2. 增加 `ecrecover/sha256/ripemd160` 预编译合约。
3. EIP-1559 `type-2` 交易支持（maxFeePerGas/maxPriorityFeePerGas）。
4. 跨引擎桥接支持 ERC-1155 批量、ERC-20 反向包装。
5. 真实 Hardhat/Foundry 部署端到端测试（编译真实 Solidity → 部署 → 调用）。

## 六、审计检查清单

- [x] EVM 内存/步数/存储/gas 四重沙盒限制
- [x] 桥接快照回滚原子性（无半途状态）
- [x] 桥接重入防护（重复 burn 拒绝）
- [x] 混合账户 ECDSA 验签 / 属主 / 不可逆迁移
- [x] eth_call 只读（零状态变更）
- [x] chainId / nonce / 余额三重校验
- [x] 1000 笔跨桥资产守恒
- [x] 100 并发 EVM 调用确定性
- [x] 现有 287 项测试回归无破坏
