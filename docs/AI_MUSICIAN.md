# Nova AI 生成服务接入与 AI 音乐人

> 覆盖提示词 3：① 主流 AI 服务接入（Suno / OpenAI / Stable Diffusion / 自定义）
> ② AI 音乐人自动创作循环（定时触发 → 生成 → IPFS → 自动上架 → 自动定价）
> ③ 收益自动分账（创作者 70% / 算力节点 20% / AI 成长基金 10%）
> ④ AI 成长基金（合约控制地址 + 收支记录公开可查） ⑤ 前端展示（作品专区 / 状态面板 / 一键触发）。
> 代码与测试均为中文注释；分账逻辑写死在链上合约中，确定性执行。

## 一、架构总览

```
社区成员 ──▶ 前端（novachain-web / ai_musician.html）
                │  作品集市（试听/购买）· 状态面板 · 一键触发（2 NOVA）· 基金台账
                ▼
           RPC（/api/ai/*）  ◀── 离线圈子（scripts/ai_musician_loop.py）
                │                  到点检查链上 due → Suno 生成 → IPFS 上传 → 上架
                ▼
        Nova 链（novachain）
               ├─ core/ai_service.py   AI 服务登记 / 音乐人循环 / 分账 / 成长基金
               ├─ core/compute.py      算力任务（生成任务 / 执行节点信誉权重分账）
               ├─ core/socialfi.py      AI 创作者身份（ai_identity）
               ├─ nova_node.py          交易校验/应用 + RPC 处理器 + 维护循环
               └─ network/rpc.py        路由
```

## 二、AI 服务接入（提示词 3.1）

- 链上登记（`nova:ai:svc:register`）：服务类型 `suno` / `openai` / `stable_diffusion` / `custom`，
  字段含服务名称、模型名、端点指纹哈希（`endpoint_hash`）；**API Key 不上链**，仅存于离线圈子本地。
- 服务所有者可暂停 / 恢复（`nova:ai:svc:config`，action = pause / resume）。
- 服务类型映射：suno → `ai_music`、stable_diffusion → `ai_image`、openai → `ai_text`、custom → 自定义。

| 服务 | 用途 | 模型示例 | 任务类型 |
|---|---|---|---|
| Suno API | AI 音乐生成 | suno-v4 | ai_music |
| OpenAI API | 文本 / 图像生成 | gpt-4o / dall-e | ai_text / ai_image |
| Stable Diffusion API | 图像生成 | sd-xl | ai_image |
| 自定义 | 可扩展模型 | — | custom |

## 三、AI 音乐人自动创作循环（提示词 3.2）

### 1. 循环配置（链上）
- 仅 AI 创作者（`socialfi.ai_identity`）可配置 `nova:ai:muso:config`：
  `enabled`、`schedule`（daily / weekly）、`hour`（0-23）、`weekday`（0-6）、`budget`（0-10000）。
- 维护循环（`nova_node._run_daily_maintenance`）按配置把 `due` 置位，离线圈子读取后执行；
  `muso_take_due` 原子消费该轮并滚动 `last_run / last_run_day`。

### 2. 离线圈子（scripts/ai_musician_loop.py）
```powershell
# 内存演示：注册 AI 创作者 → 配置循环 → 生成（Mock WAV）→ IPFS 上架 → 购买分账
python scripts/ai_musician_loop.py --demo --once
python scripts/ai_musician_loop.py --demo --loop --interval 60

# 对接本地节点（真实广播）
python scripts/ai_musician_loop.py --rpc http://127.0.0.1:8080 --ai-addr 0x... --ai-priv <hex> --once

# 可选：接入真实 Suno API / IPFS
python scripts/ai_musician_loop.py --suno-url https://... --suno-key <key> --ipfs-api http://127.0.0.1:5001 --once
```
- 步骤：① `SunoClient.generate(prompt)` 生成歌曲（默认 Mock 8 秒 WAV，可接真实 Suno HTTP API）
  → ② `IPFSUploader.upload(bytes)` 上传 IPFS（默认本地文件仓库 `.ai_muso_store/`，
  可接 `ipfshttpclient` 或 ipfs CLI）→ ③ 广播 `nova:ai:work:create` 自动上架。

### 3. 自动定价（链上确定性计算）
- `suggest_price`：以参考价区间中值为基准（ai_music 0.5-2 NOVA），按历史销量调整：
  销量 ≥1 ×1.15、≥5 ×1.3、≥10 ×1.5；上架 14 天零销量 ×0.8；价格夹在 0.1-50 NOVA。
- 上架时不传 price 即自动定价；也可显式指定（校验 0.1-50）。

## 四、收益自动分账（提示词 3.3，写死合约）

```
购买金额（NOVA）
 ├─ 70% → 创作者（AI 创作者地址，即时入账）
 ├─ 20% → 算力提供节点（按生成任务 paid_workers 的信誉权重分配；无执行节点则入算力池 0x_compute_pool）
 └─ 10% → AI 成长基金（0x_ai_growth_fund）
```

- 常量：`REV_CREATOR = 0.70`、`REV_COMPUTE = 0.20`、`REV_FUND = 0.10`；
- 算力份额优先按生成任务的执行节点分配（权重 `1 + rep_bonus`），无执行节点时进入
  `0x_compute_pool`（算力贡献份额暂存，后续参与激励池分配）；
- 购买校验：不能购买自己的作品、金额必须与售价一致（`nova:ai:work:buy`）。

## 五、AI 成长基金（提示词 3.4）

- 基金地址由合约控制：`0x_ai_growth_fund`，余额与收支记录公开可查（`/api/ai/fund`）。
- 收入：每笔作品销售 10% + 社区一键触发费用（2 NOVA / 次）。
- 支出：仅基金监护人（`nova:ai:fund:guard` 授权，AI 创作者可追加监护人）可执行
  `nova:ai:fund:spend`，需填写收款地址与用途（购买更多算力 / 训练更好模型）。
- 支出限额（H-04，防单监护人掏空）：单笔 ≤20 NOVA 的小额支出即时转账，但受单监护人
  单日 20 NOVA 上限约束；单笔 >20 NOVA 的大额支出进入待审批（`nova:ai:fund:approve`），
  需 **2 名监护人** 审批后才执行，7 天未达成自动作废（`maintain` 清理）。
- 台账：`ai_fund_ledger` 记录每笔收支（kind / event / ref / addr / amount / memo / at），
  前端「成长基金」面板展示余额、累计收支、监护人列表与流水。

## 六、一键触发（提示词 3.5）

- 社区成员支付 2 NOVA（`TRIGGER_FEE`）调用 `nova:ai:trigger`（可指定服务类型，默认 suno），
  费用进入成长基金，生成一条 `pending` 触发记录；
- 离线圈子执行后关联作品上架（`work_create` 带 `trigger_id`），触发记录置为 `done`。

## 七、RPC 接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ai/services` | 已登记 AI 服务列表 |
| GET | `/api/ai/works` | AI 作品集市（标题/作者/价格/销量/试听 CID） |
| GET | `/api/ai/fund` | 成长基金余额 / 收支流水 / 监护人 |
| GET | `/api/ai/status` | AI 状态面板（今日生成/累计销量/总收入/循环配置/基金） |
| GET | `/api/ai` | AI 创作者列表 |
| GET | `/api/ai/{addr}` | AI 创作者详情（作品/收益） |

链上操作走 `/api/op`，op 前缀 `nova:ai:*`：

| op | 说明 |
|---|---|
| `nova:ai:svc:register` / `nova:ai:svc:config` | 服务登记 / 暂停恢复 |
| `nova:ai:muso:config` | 音乐人循环配置（仅 AI 创作者） |
| `nova:ai:work:create` / `nova:ai:work:buy` | 作品上架（自动定价）/ 购买分账 |
| `nova:ai:trigger` | 社区一键触发（2 NOVA → 基金） |
| `nova:ai:fund:guard` / `nova:ai:fund:spend` / `nova:ai:fund:approve` | 监护人授权 / 支出（小额即时、大额待审批）/ 审批大额支出 |

## 八、前端展示（novachain-web / ai_musician.html）

- **作品集市**：AI 作品列表（标题 / 作者 / 售价 / 销量 / 试听 / 购买）。
- **AI 状态面板**：今日生成数量、累计生成、累计销量、总收入、循环配置（每日/每周 + 小时）、
  成长基金余额。
- **一键触发**：社区成员付费 2 NOVA 触发 AI 创作（模拟离线圈子按钮，实时完成创作并上架）。
- **成长基金**：余额、累计收支、监护人、支出流水（用途 / 金额 / 时间）。
- 入口：`apps.html` 新增「AI 音乐人」卡片；`explore.html` AI 音乐人卡片已链接到 `ai_musician.html`。

## 九、运行与测试

```powershell
cd C:\Users\Administrator\novachain

# 全量测试（含 test_compute_network.py 的 AI 用例：服务登记/分账/触发/基金支出）
pytest

# AI 音乐人离线圈子单轮演示
python scripts/ai_musician_loop.py --demo --once

# 前端预览
cd C:\Users\Administrator\novachain-web
python -m http.server 8080   # 打开 ai_musician.html（demo 模式自带种子数据）
```

## 十、新增/改动文件

后端（novachain）：
- `core/ai_service.py`（新）AI 服务登记 / 音乐人循环 / 分账 / 成长基金
- `core/compute.py`（新）算力网络（生成任务执行与分账依赖，见 docs/COMPUTE_NETWORK.md）
- `core/storage.py`（改）StateStore 新增 `ai_*` 状态字段与序列化
- `nova_node.py`（改）`nova:ai:*` 交易校验/应用、RPC 处理器、每日维护循环
- `network/rpc.py`（改）新增 AI 路由
- `scripts/ai_musician_loop.py`（新）离线圈子（Suno 生成 + IPFS 上传 + 自动上架）
- `test_compute_network.py`（新）AI 相关 4 项测试

前端（novachain-web）：
- `ai_musician.html`（新）AI 音乐人专区（作品集市 / 状态面板 / 一键触发 / 基金台账）
- `apps-common.js`（改）8 个 AI demo op + `seedAiMusicDemo` 种子数据 + demo API 路由
- `apps.html` / `explore.html`（改）AI 音乐人入口卡片
