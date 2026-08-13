# Nova 链（抗量子版）

全球首个全抗量子创作者公链。采用 NIST 认证的 CRYSTALS-Dilithium5 签名算法。
8100 万 NOVA，零团队预留，全人类共有。

## 安装依赖
pip install -r requirements.txt   # 核心依赖（含 pyOpenSSL）
pip install oqs                    # 生产必装：启用 CRYSTALS-Dilithium5 抗量子签名

## 生成 TLS 证书
python cert_gen.py

## 本地测试（单机三节点，不使用 TLS）
python run_network.py

## 生产启动（启用 TLS）
python nova_node.py --host 0.0.0.0 --p2p 9000 --rpc 8080
python nova_node.py --host 0.0.0.0 --p2p 9000 --rpc 8080 --consensus pos --validator-key <hex>

## 生成抗量子钱包
python -c "from core.crypto import QuantumWallet; w = QuantumWallet(); print('地址:', w.address); print('私钥:', w.private_key_hex())"

## 安全特性
- CRYSTALS-Dilithium5 抗量子签名（NIST PQC 标准）
- TLS 1.3 加密通信
- RPC 频率限制（100次/秒/IP）
- 交易去重防重放
- 数据大小限制（100KB/交易，100KB/合约）
- 质押防女巫（最低100 NOVA）
- 签到防作弊（IP限制 + 设备指纹 + 20小时间隔）

## 经济参数
- 总量：8100万 NOVA（锁死，永不增发）
- 手续费：0.000001 NOVA/笔（固定，100%回流激励池）
- 出块奖励：0.5 NOVA起，每9个月减半，共9次，之后恒定 ~0.00097
- 质押：100-10000 NOVA，按比例分配奖励，7天冷静期

## 三层奖励
| 类型 | 初始奖励 | 减半条件 | 最低值 |
|------|---------|----------|--------|
| 部署合约 | 5 NOVA | 每5万合约 | 0.01 |
| 推荐奖励 | 1 NOVA | 每10万人 | 0.01 |
| 合约调用分红 | 0.1 NOVA | 每50万次 | 0.001 |
| 轻节点验证 | =出块奖励 | 同减半 | ~0.00097 |

## 早期激励
- 前81位超级节点矿工：注册即空投100 NOVA（锁定3年，之后逐月解锁10%）
- 前8100位轻节点签到者：首次签到即空投100 NOVA（锁定3年）
- 保持9个月在线：矿工额外1000 NOVA，轻节点额外100 NOVA
- 奖励发放时间：链上线满12个月

## 预售
- 仅收 USDT（BSC BEP-20）
- 9阶段阶梯定价：0.00001 → 0.001 (+9999%) → 每阶段×3 → 2.187
- 预售接收地址：0x6a5C3f17af93f690847208E68722afeaE7108bc5
- 必须先通过网页绑定 Nova 地址与 BSC 地址

## 文件结构
- 后端（节点/核心，本地运行）：`nova_node.py`、`core/`、`network/`、`genesis.json`、`run_network.py`、`test_smoke.py`、`test_pos_network.py`、`cert_gen.py`、`requirements.txt`。
- 前端（Vercel 静态站，独立目录 `../novachain-web/`）：`index.html`（产品落地页）、`nova.html`（交互体验页）、`apps.html`（应用中心）、`socialfi.html`（链上生态）、`404.html`、`vercel.json`、`.nojekyll`、`README_DEPLOY.md`。
- 部署说明见 `../novachain-web/README_DEPLOY.md`：Vercel 导入目录后即可静态托管全部页面。

## 安全说明（重要）
- 未安装 oqs 时，签名自动回退为 Ed25519（RFC 8032，Python 与浏览器 WebCrypto 实现）。Ed25519 **不是**抗量子算法；生产环境必须安装 oqs（pip install oqs）以启用 CRYSTALS-Dilithium5。节点 /api/status 会如实返回当前算法与 quantum_safe 状态。
- 交易签名基于规范化字符串：sender + receiver + 金额（8 位小数去尾零）+ 时间戳 + parents + data + 公钥。时间戳由客户端提供，节点校验 ±5 分钟窗口，缺失/过期时间戳的交易会被拒绝。
- 金额必须为正数（合约调用可为 0）且不超过总供应量 8100 万 NOVA，字符串、NaN、Infinity 等非法金额一律拒绝。

## 状态持久化
节点默认把余额、DAG、质押、签到、重放保护等状态保存到 `chain_state.json`（每 60 秒自动保存 + 退出时保存）。
启动参数：`--state` 指定快照文件（传空字符串禁用持久化）。
测试网络：`python run_network.py`（三个节点分别使用 state_seed.json / state_node1.json / state_node2.json）。

## 共识与同步（v0.3）
- 区块：每 60 秒（可通过 NovaNode(block_interval=...) 调整）将未封装的交易打包为区块，形成哈希链；区块随 P2P 广播，节点校验 prev_hash/高度后采用。
- 状态同步：P2P 握手交换区块高度，落后节点自动向对端请求全量状态快照并恢复（余额/DAG/质押/签到/链）。
- 奖励上限：轻节点验证奖励同一交易只能领一次、同一地址每日一次；合约调用奖励按（发送方, 合约）每日一次。
- 解锁与维护：POST /api/unlock 领取到期锁仓空投；节点每日自动维护（矿工在线时长累计、9 个月达标、早期激励发放且不重复发放）。

## 共识 v0.4：PoS 出块权与区块签名
- 双模式：`--consensus pos|checkpoint`（默认 checkpoint 保持旧行为；pos 为质押加权出块 + 区块签名验证）。
- 出块权：以 `prev_hash + height` 为确定性种子，按有效质押（stake 100-10000 NOVA，封顶 10000）加权抽签；
  每个 epoch（`--epoch-len`，默认 10800 块 ≈ 7.5 天）边界重建质押快照，节点状态一致时选举结果一致。
- 区块签名：当选出块者用验证者私钥对区块哈希签名，其他节点校验签名与出块权；
  非当选且未超时的区块一律拒绝（bootstrap 期除外）。
- 活性兜底：当选者离线超过 2 个出块周期时，任意有质押的节点可补块；全网无质押时进入 bootstrap，
  任何持有验证者密钥的节点均可出块（仍需签名）。
- 质押即交易：stake/unstake/claim 已改为签名交易（data=`nova:stake` 等），经区块封存后全网确定性同步；
  前端质押/解押/领取会自动构造并签名交易（旧的无签名 /api/stake 请求将被拒绝）。
- 质押上限：单地址累计质押不超过 10000 NOVA；全网质押总量不超过供应量的 30%（24,300,000 NOVA）。
- 解押上限：支持部分解押（须指定金额，0 不再表示全部）；冷却中的质押总量不超过当前质押的 25%，
  配合 7 天冷静期实现渐进退出，防止大额瞬间抽逃。
- 惩罚机制：
  - 出块超时：当选者超过 2 个出块周期未出块，被补块后按 1% 质押惩罚（最低 1 NOVA），并禁用出块权 1 个 epoch
    （该惩罚随链确定性生效，全网一致）。
  - 双签：同一出块者对同一高度签署两个不同区块，按 5% 质押惩罚并禁用出块权 1 个 epoch
    （本地尽力而为检测，状态随快照同步）。
  - 被惩罚地址在下一 epoch 的质押快照重建时被排除，jail 到期后自动恢复。
- 集成测试：`python test_pos_network.py`（3 节点：质押收敛 / 每轮唯一当选者出块 / 状态全一致）。
## 链上新增功能（v0.5）

### 存储网络（存储挖矿）
创作者内容与 SocialFi 资产（粉丝代币头像 CID、策展封面、社交动态）通过签名交易固定到链上存储网络，真正占用链的存储能力。交易 `data` 为 JSON 字符串，包含 `op` 字段。

| 操作 | 说明 |
|------|------|
| `nova:storage:register` | 超级节点注册为存储提供者，声明贡献硬盘容量（单节点上限 1 PB） |
| `nova:storage:pin` | 创作者固定内容（CID/大小/时长），生态基金按 `STORAGE_REWARD_PER_GB_PER_DAY` 向固定奖励池注入 NOVA |
| `nova:storage:claim` | 提供者认领 CID（每 CID 最多 10 份），提交哈希链链顶作为密封 |
| `nova:storage:proof` | 提供者按天提交存储证明（揭示哈希链下一前像），链上验证后发放 `STORAGE_PROOF_REWARD` |
| `nova:storage:order` | 高级托管订单：支付 NOVA 进入托管，完成证明的提供者按份数平分托管金，到期未发放部分退回 |

- 链上节点无法直接读盘，本实现用哈希链证明作为简化 PoSt；生产环境可将 CID 接入 IPFS，并升级为 Filecoin 风格的真实时空证明（PoSt）。
- 奖励资金来自**生态基金**（`0x_ecosystem_fund`），与早期空投、预测市场平台费共用同一金库。
- RPC：`/api/storage/register`、`/api/storage/pin`、`/api/storage/claim`、`/api/storage/proof`、`/api/storage/order`、`/api/storage/pins`、`/api/storage/providers`、`/api/storage/orders`。

### 算力任务市场
任何节点可发布计算任务并悬赏 NOVA（赏金进链上托管），提供算力的节点抢单计算并提交结果哈希。

| 操作 | 说明 |
|------|------|
| `nova:compute:publish` | 发布任务（spec 规格 + 悬赏金 + 有效期） |
| `nova:compute:accept` | 节点接受任务（每任务最多 8 名工人） |
| `nova:compute:submit` | 提交结果哈希，双节点冗余验证通过即发放赏金 |
| 结算 | 任意两个不同节点提交相同结果哈希即视为验证通过，各得一半赏金；任务到期未完成全额退回发起者 |

- 典型场景是 **AIGC 生成与推理**：AI 生成任务在链上发布，由外部算力节点完成并验证。
- RPC：`/api/compute/publish`、`/api/compute/accept`、`/api/compute/submit`、`/api/compute/tasks`。

### SocialFi：链上生态（10 类玩法）
全部以签名交易（`sender == receiver`，`data` 为 JSON `{op, ...}`）确定性上链，网页 `socialfi.html` 演示与节点双模式全覆盖。

| 类别 | 玩法 | 核心操作 |
|------|------|----------|
| 粉丝经济 | 粉丝代币发行平台 | `nova:fan:issue` / `nova:fan:buy` / `nova:fan:propose` / `nova:fan:vote` |
| 粉丝经济 | 收益共享合约 | `nova:rev:create` / `nova:rev:invest` / `nova:rev:royalty` / `nova:rev:claim` |
| 游戏互动 | 链上成就系统（灵魂绑定） | `nova:ach:issue` / `nova:ach:award` |
| 游戏互动 | 预言机预测市场 | `nova:market:create` / `nova:market:bet` / `nova:market:settle` |
| 游戏互动 | 可验证随机盲盒 | `nova:blind:create` / `nova:blind:reveal` / `nova:blind:open` |
| 社交身份 | 去中心化内容策展 | `nova:curate:create` / `nova:curate:buy` |
| 社交身份 | 社交图谱与推荐引擎 | `nova:graph:post` / `nova:graph:follow` / `nova:graph:like` |
| 社交身份 | 链上声誉系统 | 实时计算 0-100 信誉分，高信誉（≥80）享 50% 交易费折扣 |
| 金融投资 | 创作者债券 | `nova:bond:issue` / `nova:bond:buy` / `nova:bond:fund` / `nova:bond:redeem` |
| 金融投资 | 碎片化 NFT 市场 | `nova:frac:split` / `nova:frac:buy` |

- 内容类玩法携带 CID 时，链自动固定到存储网络，占用链的存储能力。
- 推荐引擎在链上确定性计算，并输出任务规格，可一键发布为算力市场的计算任务，占用链的算力能力。
- RPC：`/api/op`、`/api/socialfi/{domain}`、`/api/socialfi/overview`、`/api/reputation/{addr}`、`/api/graph/recommend/{addr}`。

### 测试
`python test_storage_compute.py` 覆盖存储/算力操作的链上确定性、结算与 RPC；`python test_socialfi.py` 覆盖 10 类 SocialFi 玩法的验证、结算与声誉计算。


## 合约虚拟机（NexLang DSL，v0.6）
- 合约以 NexLang 源码部署（`/api/deploy`，需 creator 签名 `deploy:{addr}:{bytecode}`，同地址限一次），编译为栈式字节码（PUSH/STORE/LOAD/MUL/ADD/SUB/DIV/SEND/RET），由 `core/vm.py` 的 `NexusVM` 执行，10 万步上限防死循环。
- 示例：`let a = 42 + 1; let b = 7 - 3; let c = 2 * 6; return a;`
- 调用：`/api/call`（需 sender 签名），合约状态写入 `contract_state`，链上按日发放调用分红（防刷：同 sender/contract 每日一次）。
- 编译器：`nexlang_compiler.py`（函数体内 `let` 与顶层一致，含槽位分配）；测试见 `test_vm_nexlang.py`。

## 演示模式与前端联动（storage.html / compute.html）
- `apps-common.js` 演示模式新增 `demoStorageOp`（register/pin/claim/proof/order）与 `demoComputeOp`（publish/accept/submit，双节点一致结算），含守卫、奖励、余额变动、账本与事件，可与真实节点双模式切换。
- `novachain-web` 新增页面：`storage.html`（我的节点 / 固定 / 认领与证明 / 存储订单 / 提供者）、`compute.html`（发布 / 任务市场 / 我的任务）；`apps.html`、`socialfi.html` 已加入口。
- 签名方案前后端一致：Ed25519（无 oqs 时）/ Dilithium5（装 oqs 后），前端 WebCrypto 与后端可交叉验签。

## 安全审计与回归
- 完整审计报告：`docs/AUDIT_2026-08-13.md`（P0×3 / P1×6 / P2×8 + 接口契约）。
- 回归测试：`test_audit_regressions.py`（P0/P1 修复点）、`test_vm_nexlang.py`（VM/编译器）。
- 全量测试：`python -m pytest -q`（180 项通过）；端到端 3 节点联调脚本：`scripts/e2e_full.py`（需 `PYTHONPATH` 含依赖目录，37 项通过）。
- 已知约束：SocialFi 对象 ID 由交易内容确定（不依赖墙钟时间），跨节点状态可收敛一致。
