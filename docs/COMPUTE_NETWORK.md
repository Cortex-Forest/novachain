# Nova 算力网络核心架构与任务市场

> 覆盖五份需求：① 算力网络核心架构（节点注册 / 任务类型 / 生命周期状态机 / 信誉分 / 调度策略）
> ② 算力任务市场（发布 / 接单 / 结算 / 争议） ③ AI 生成服务接入（见 docs/AI_MUSICIAN.md）
> ④ 验证与防作弊（双节点冗余 / 质押罚没 / 随机抽查 / 算力证明） ⑤ 算力节点激励与经济模型。
> 代码与测试均为中文注释；后端逻辑全部为确定性链上规则，随区块状态复制到全节点。

## 一、架构总览

```
创作者 / 开发者 ──▶ 前端（novachain-web）
                      │  compute.html（总览 / 发布 / 节点网络 / 任务市场 / 我的任务）
                      ▼
                 RPC（/api/compute/*、/api/ai/*）  ◀── 离线圈子（scripts/compute_network_demo.py）
                      │                                   scripts/ai_musician_loop.py
                      ▼
          Nova 链（novachain）
                 ├─ core/compute.py       算力网络合约（注册/信誉/市场/验证/审计/激励）
                 ├─ core/ai_service.py    AI 生成服务接入（AI 音乐人 / 分账 / 成长基金）
                 ├─ nova_node.py          交易校验/应用 + RPC 处理器 + 维护循环
                 ├─ core/economy.py       验证者激励池 / 生态基金
                 └─ network/rpc.py        路由
```

## 二、算力节点注册（提示词 1）

### 1. 超级节点自动具备算力提供资格
- 任何质押（`nova:stake`）、矿工注册或存储激励节点（`inc_nodes`）自动成为合格算力节点，
  无需额外注册（`is_super_node` / `is_qualified_node`，幂等）。
- 链上状态字段：`compute_nodes`（显式注册）、`compute_stats`（信誉与收益统计）、`compute_stakes`（质押）。

### 2. 算力规格声明
- `nova:compute:register` 声明 CPU 核心数、GPU 型号、显存（GB）、内存（GB）、可用存储（GB）、
  区域与网络延迟（ms），规格写入链上公开可查（`node_spec`）。
- 校验上限：CPU ≤ 1024 核、内存 ≤ 4096GB、存储 ≤ 1PB、显存 ≤ 512GB、延迟 ≤ 60000ms；
  超规格声明被拒。
- GPU 档位映射（`gpu_tier`）：high（A100/H100/4090/L40/A6000/MI300 等）/ mid（A40/3090/T4/L4/A10 等）/
  low（3060/2060/GTX/P100 等）/ none。

### 3. 算力证明
- 接单时合约按节点链上规格校验（`spec_meets`）：CPU / 内存 / 存储 / GPU 档位必须满足任务需求；
  超规格接单（实际规格不达标）视为作恶被拒，且提交虚假规格被仲裁 / 抽查后罚没。

## 三、任务类型与参考价（提示词 1 / 5）

| 任务类型 | 名称 | 需求 | 参考价（NOVA） |
|---|---|---|---|
| `ai_music` | AI 音乐生成 | 高 GPU，≥4 核 / 16GB 内存 / 20GB 存储 | 0.5 – 2.0 / 首 |
| `ai_image` | AI 图像生成 | 中 GPU，≥4 核 / 8GB 内存 / 10GB 存储 | 0.1 – 0.5 / 张 |
| `game_server` | 游戏服务器托管 | 低 GPU，≥4 核 / 32GB 内存 / 50GB 存储 | 0.1 – 1.0 |
| `video_transcode` | 视频转码 | 低 GPU，≥8 核 / 8GB 内存 / 50GB 存储 | 0.05 – 0.5 |
| `data_clean` | 数据清洗 / 标注 | 低 GPU，≥2 核 / 16GB 内存 / 10GB 存储 | 0.01 – 0.1 |

- 参考价为市场指导价（`reference_price`），实际成交价由抢单固定价或竞价决定。

## 四、任务生命周期状态机（提示词 1）

```
发布 ─▶ open ──┬─▶ (抢单) assigned ─▶ submitted ─▶ arbitrating ─▶ settled ─▶ completed
               │                                                     │
               └─▶ (竞价) bidding ─▶ assigned ──┘                     ├─▶ disputed ─▶ failed / completed
                                                        expired ◀────┘
```

- 状态全集：`open → bidding → assigned → submitted → arbitrating → settled → completed`，
  另有 `expired / failed / disputed`；每个阶段写入 `history` 链上记录（状态 / 时间 / 操作者 / 说明）。
- 发布即质押全额预算进托管；任务到期未完成 → `expired`，预算全额退回发起者（`expire_all`）。

## 五、算力任务市场（提示词 2）

### 1. 任务发布
- 字段：任务类型（`task_type`）、需求规格（`spec`）、预算（`bounty`，NOVA）、截止时间
  （`expires_in`，5 分钟 – 90 天）、验收标准（`acceptance`）、模式（`grab` 抢单 / `bid` 竞价）、
  最少执行节点（默认 2）。
- 发布时从发起者余额扣除全额预算托管；链上事件 `task_publish`。

### 2. 节点接单
- 抢单模式（`accept`）：先到先得、固定预算；满足规格、信誉与质押校验后，满 `min_nodes`
  即进入 `assigned`。
- 竞价模式（`bid` + `award`）：节点报价，发起者在 2-8 个出价节点中挑选执行者（`award`），
  记录中标价并进入执行。

### 3. 结果提交
- 执行节点提交结果哈希（64 位 hex）+ 结果存储地址（IPFS CIDv0/CIDv1 或链上引用），
  合约记录提交时间戳（`submit` / `results`）。

### 4. 验证与结算
- 双节点冗余：任一哈希一致 → 自动结算（`_settle`）：1% 手续费回流验证者激励池，
  其余按信誉权重分配（含 5-15% 信誉加成），节点信誉 +1（完成）/ +2（正确）。
- 双节点结果不一致 → `arbitrating`，引入第三节点仲裁（`arbitrate`，不能是执行节点或发起者）：
  - 仲裁与某一执行节点一致 → 该节点结算，错误方信誉 -10；
  - 仲裁与双方均不一致 → 串通检测，双方作恶罚没（`_slash_cheat`），预算退回发起者。

### 5. 争议处理
- 发起者可在任务完成后 24 小时内提出异议（`dispute`，`DISPUTE_WINDOW = 24h`），
  已结算报酬回拨冻结，任务进入 `disputed`，预算冻结。
- 社区仲裁：矿工 / 质押者 / 算力节点均可投票（`vote`，uphold / dismiss），
  达到 3 票（`DISPUTE_QUORUM`）即结算（`_settle_disputes`，由维护循环触发）：
  - 发起者胜诉 → 冻结预算退回发起者，执行节点按串通作恶罚没；
  - 驳回异议 → 恢复已结算报酬，任务回到 `completed`。

## 六、验证与防作弊（提示词 4）

| 机制 | 规则 |
|---|---|
| 双节点冗余执行 | 同一任务分配给两个节点，结果哈希一致即通过 |
| 第三方裁决 | 不一致引入第三节点仲裁，仲裁节点不能是执行节点或发起者 |
| 节点质押 | 接单前质押 ≥ max(100, 预算 × 信誉档位质押系数)；质押范围 100-10000 NOVA |
| 作恶检测 | 提交虚假结果 / 仲裁判负 → 罚没质押 + 信誉分清零（-100） |
| 串通检测 | 两节点结果一致但被证明错误（仲裁或争议）→ 双双罚没 |
| 随机抽查 | 已完成任务约 5%（`AUDIT_RATE`）确定性选中（sha3 掷骰），第三方节点重跑验证（`audit_submit`） |
| 抽查惩罚 | 发现错误 → 原执行节点罚没双倍质押（`AUDIT_SLASH_MULT = 2`），信誉清零 |
| 抽查奖励 | 审计节点获 0.5 NOVA（生态基金支付）+ 信誉 +1/+2 |
| 超规格接单 | 链上规格不满足任务需求视为作恶，接单被拒 |

### 信誉分（满分 100，初始 50）
- 完成 +1、结果正确 +2、被投诉 -10、仲裁/抽查判错 -10、作恶 -100（清零）。

| 档位 | 信誉下限 | 加成 | 质押系数 | 最大任务金额 |
|---|---|---|---|---|
| 恒星节点 | 80 | +15% | 1.0× | 10000 NOVA |
| 星核节点 | 60 | +10% | 0.8× | 2000 NOVA |
| 星云节点 | 40 | +5% | 0.6× | 500 NOVA |
| 轻量节点 | 0 | 0% | 0.3× | 50 NOVA |

- 低于 40 分自动降级为轻量任务提供者，仅可接 `data_clean`；
  信誉分决定接单优先级（`compute_reputation` 评分排序）与最大任务金额（`max_budget`）。

## 七、算力节点激励与经济模型（提示词 5）

### 1. 节点收益构成（`node_income`）
- 任务报酬（大头，来自发起者托管预算，扣除 1% 手续费）；
- 出块奖励（验证者激励池，`settle_incentive_epoch`：存储 40% / 算力 60%，按质押 / 配额贡献分配）；
- 信誉加成（恒星 15% / 星核 10% / 星云 5%，结算时按权重加权分配）；
- 抽查审计奖励（0.5 NOVA / 次，生态基金支付）。

### 2. 质押与解押
- 质押范围 100-10000 NOVA（`MIN_COMPUTE_STAKE / MAX_COMPUTE_STAKE`）；
- 解质押进入 7 天冷静期（`UNBOND_COMPUTE`），到期后 `claim` 领取；
- 作恶罚没进入生态基金，链上事件 `node_slash`。

### 3. 手续费
- 每笔任务成交收 1% 手续费（`MARKET_FEE_RATE`），100% 回流验证者激励池。

### 4. 激励池分配
- 算力节点奖励与存储激励共用验证者激励池：存储贡献权重 40%（按 `quota_gb` 配额），
  算力贡献权重 60%（按 `compute_stakes` 质押），按比例自动结算（`settle_incentive_epoch`）。

### 5. 调度策略
- 优先选择信誉高、延迟低、报价合理的节点（规格校验 + 信誉档位 + 质押门槛 + 抢单/竞价）；
- 支持一任务多节点（2-8 个）冗余执行，结果对比验证。

## 八、RPC 接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/compute/publish` | 发布任务（类型/规格/预算/截止/验收/模式） |
| POST | `/api/compute/accept` | 抢单接单 |
| POST | `/api/compute/submit` | 提交结果哈希 + IPFS 地址 |
| GET | `/api/compute/tasks` | 任务列表（含状态机 history） |
| POST | `/api/compute/register` | 节点注册（算力规格上链） |
| GET | `/api/compute/nodes` | 全网算力节点（规格/信誉/质押/收益） |
| GET | `/api/compute/node/{addr}` | 节点详情（规格/信誉/收入/合格状态） |
| GET | `/api/compute/income/{addr}` | 节点收益统计（任务/加成/出块/审计） |
| GET | `/api/compute/overview` | 汇总（节点/任务/质押/罚没/激励池/参考价） |
| GET | `/api/compute/events` | 链上算力事件流 |

其余链上操作走通用交易接口（`/api/op` 或 `/api/send`），op 前缀 `nova:compute:*`：

| op | 说明 |
|---|---|
| `nova:compute:bid` / `nova:compute:award` | 竞价出价 / 发起者选标 |
| `nova:compute:arbitrate` | 第三方仲裁（结果不一致时） |
| `nova:compute:dispute` / `nova:compute:vote` | 24h 异议 / 社区仲裁投票 |
| `nova:compute:stake` / `unstake` / `claim` | 质押 / 解押（7 天冷静期）/ 领取 |
| `nova:compute:audit` | 随机抽查节点提交复核结果 |

## 九、运行与测试

```powershell
cd C:\Users\Administrator\novachain

# 全量测试（含 test_compute_network.py 17 项）
pytest

# 端到端演示：节点注册 → 质押 → 抢单/竞价 → 执行 → 仲裁 → 争议 → 抽查 → 激励结算
python scripts/compute_network_demo.py

# 前端预览
cd C:\Users\Administrator\novachain-web
python -m http.server 8080   # 打开 compute.html（总览/发布/节点网络/任务市场/我的任务）
```

## 十、新增/改动文件

后端（novachain）：
- `core/compute.py`（新）算力网络合约（注册 / 信誉 / 市场 / 验证 / 审计 / 激励）
- `core/ai_service.py`（新）AI 生成服务接入层（见 docs/AI_MUSICIAN.md）
- `core/storage.py`（改）StateStore 新增 `compute_*` / `ai_*` 状态字段与序列化
- `core/economy.py`（改）激励池 / 生态基金联动
- `nova_node.py`（改）`nova:compute:*` / `nova:ai:*` 交易校验与应用、RPC 处理器、维护循环
- `network/rpc.py`（改）新增算力 / AI 路由
- `scripts/compute_network_demo.py`（新）算力网络端到端演示
- `scripts/ai_musician_loop.py`（新）AI 音乐人离线圈子（见 docs/AI_MUSICIAN.md）
- `test_compute_network.py`（新）17 项合约 / RPC 测试

前端（novachain-web）：
- `compute.html`（新）算力总览 / 任务发布 / 节点网络 / 任务市场 / 我的任务 5 面板
- `ai_musician.html`（新）AI 音乐人专区（见 docs/AI_MUSICIAN.md）
- `apps-common.js`（改）demo 模拟 / 种子数据 / demo API 路由
- `apps.html` / `explore.html`（改）AI 音乐人入口
