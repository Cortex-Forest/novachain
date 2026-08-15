# Nova 存储激励系统与分层存储方案

> 覆盖五份需求：① 存储节点激励（24h 挑战证明 + 奖励 + 罚没）② 分层存储 + CDN 加速
> ③ 上传流程优化 ④ 链上存储状态合约 + RPC + 前端状态展示 ⑤ 节点监控与自动恢复。
> 代码与测试均为中文注释；后端逻辑全部为确定性链上规则，随区块状态复制到全节点。

## 一、架构总览

```
创作者/用户 ──▶ 前端（novachain-web）
                 │  upload-module.js（压缩/分片/断点续传）
                 │  storage-incentive.js（状态/创作者面板/节点监控）
                 │  player-lazy.js（试听 CDN → 付费后 IPFS 全量 + 进度条）
                 ▼
            RPC（/api/storage/*）  ◀── 存储节点守护进程（scripts/storage_node_daemon.py）
                 │                       心跳 / 挑战 / 1KB 片段证明 / 热文件缓存
                 ▼
          Nova 链（novachain）
                 ├─ core/storage_incentive.py   存储激励合约（挑战/奖励/罚没/配额/迁移）
                 ├─ core/storage_network.py     原有存储网络（兼容保留）
                 ├─ nova_node.py                交易校验/应用 + RPC 处理器
                 └─ network/rpc.py              路由
```

## 二、存储激励合约（core/storage_incentive.py）

### 1. 超级节点自动注册
- 质押 `nova:stake` 或注册旧提供者（`nova:storage:register`）即自动注册为存储节点，
  无需额外配置（`auto_register`，幂等）。
- 默认配额 10GB；配额 = 基础配额 + 质押 × 0.1 GB/NOVA（`node_quota`）。

### 2. 24 小时存储证明
- 链上确定性生成挑战（`current_challenge`）：以 `(day, addr, challenge_seq)` 为种子
  对节点声称存储的文件做 sha3 洗牌，取最多 3 个。
- 节点返回每个文件**前 1KB 片段**（hex）；合约比对 `sha256(fragment)` 与登记时的
  `fragment_commit`（`verify_proof`）。全部通过 → 记录 `last_proof_at/last_proof_epoch`。
- 同一周期重复证明被拒绝；错误片段计入 `fail_count`。

### 3. 奖励结算
- 每 GB 每月 1 NOVA → 日奖励 = `assigned_gb × 1 / 30`。
- `settle_epoch` 每 24 小时结算：从生态基金（`0x_ecosystem_fund`）扣除，发放给当日
  证明成功的节点；未证明节点 `fail_count + 1`，并刷新月度收益统计。

### 4. 作弊惩罚
- 连续 3 次失败（含失败尝试与当日未证明）→ 罚没质押 10%（`_slash`），
  罚没资金进入生态基金，触发链上事件 `node_slash`。

### 5. 存储状态（提示词 4）
- 每个文件按在线节点数计算健康度：🟢 3+ / 🟡 1-2 / 🔴 0（`file_status`）。
- 状态变 🔴 自动通知创作者（链上事件 `file_red`，前端提示“您的文件《…》存储状态异常，
  请重新上传”）。

### 6. 监控与自动恢复（提示词 5）
- 链节点每 5 分钟执行 `scan_offline`：心跳超时 30 分钟 → 离线；离线节点文件标记濒危。
- `reassign_endangered`：濒危文件自动从生态基金扣款，付费让健康节点接管
  （目标 ≥2 在线节点）。
- 配额管理：超过配额停止接收新任务；质押升级（`nova:storage:inc:upgrade`）。
- 收益统计：`node_stats` 返回本月收益 / 存储量 / 健康度（前端展示
  “本月存储收益 X NOVA，存储 Y GB，健康度 Z%”）。
- 数据迁移：`exit_notice` 提前 7 天声明 → `finalize_exits` 迁移文件 → 释放质押进解押队列。

### 7. 热门文件保护（提示词 2）
- `record_access` 记录链上访问量；`protect_hot_files` 每日统计前 1000 文件，
  从生态基金扣款付费让至少 3 个节点固定；冷门文件由创作者自行负责。

## 三、分层存储配置方案（提示词 2）

| 内容类型        | 存储层                                  | 说明 |
|----------------|----------------------------------------|------|
| 音乐文件        | IPFS + ≥3 超级节点固定                  | 上传登记时 `content_type=music`，热门保护保底 3 副本 |
| NFT 图片        | Arweave 永久存储（一次付费）             | 链上仅存 CID/元数据；Arweave 一次性付费（链外） |
| 密文正文        | IPFS + 购买者本地解密                    | 正文密钥链上合约二次封装（见 SocialFi 密文资产） |
| 合约元数据      | 直接上链（数据量小）                     | `nova:storage:inc:file` + 链上事件 |
| 30 秒试听片段   | 中心化 CDN 缓存（过渡期）                | `player-lazy.js` 先加载 CDN 试听，付费后 IPFS 全量 |

超级节点缓存（`storage_node_daemon.py`）：
- LRU 缓存最近访问的 100 个热门文件，7 天过期（`HotCache`）；
- 用户请求优先本地缓存，未命中再从 IPFS 拉取（`serve_file`）。

## 四、上传流程优化（提示词 3，novachain-web/upload-module.js）

- 压缩：音乐 → ffmpeg.wasm 128kbps MP3（移动端 96k）；图片 → 宽 2000px/质量 80%
  （移动端 1400px/70%）；视频 → 720p（移动端 480p）。
- 分片：1MB 切片、并行 3 片、断点续传（localStorage 记录已上传切片序号）。
- 进度：“已上传 45% (4.5MB/10MB)”；完成后返回 IPFS 哈希。
- 移动端：自动激进压缩；3G/4G 提示切换 WiFi。
- 失败重试 3 次，仍失败提示检查网络。

## 五、RPC 接口清单

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/storage/status/{file_hash}` | 文件存储状态（🟢/🟡/🔴 + 节点列表） |
| GET | `/api/storage/nodes` | 全网存储节点列表（配额/在线/收益/健康度） |
| GET | `/api/storage/nodes/{addr}/challenge` | 节点当前挑战 |
| GET | `/api/storage/nodes/{addr}/revenue` | 节点收益统计 |
| GET | `/api/storage/creator/{addr}` | 创作者面板（文件状态 + 事件） |
| GET | `/api/storage/events[?addr=]` | 链上存储事件（通知/惩罚） |
| GET | `/api/storage/inc/summary` | 汇总（节点/文件/奖励/罚没/生态基金） |
| POST | `/api/storage/prove` | 节点提交存储证明（挑战文件片段） |
| POST | `/api/storage/heartbeat` | 节点心跳 |
| POST | `/api/storage/inc/file` | 登记文件（片段承诺上链） |
| POST | `/api/storage/inc/claim` | 节点认领文件 |
| POST | `/api/storage/inc/upgrade` | 质押升级配额 |
| POST | `/api/storage/inc/exit` | 声明退出（7 天迁移） |
| POST | `/api/storage/inc/reupload` | 创作者一键重新上传（替换哈希） |
| POST | `/api/storage/inc/access` | 记录文件访问量 |
| POST | `/api/storage/inc/settle` | 触发结算（昨日周期） |
| POST | `/api/storage/inc/protect` | 触发热门文件保护 |
| POST | `/api/storage/inc/reassign` | 触发濒危文件恢复 |

## 六、运行与测试

```powershell
cd C:\Users\Administrator\novachain

# 全量测试（含 test_storage_incentive.py 新增 10 项）
pytest

# 端到端演示：本地节点 + 守护脚本全流程（注册→登记→认领→证明→结算）
python scripts/e2e_storage_incentive.py

# 存储节点守护进程（心跳/挑战证明/热文件缓存）
python scripts/storage_node_daemon.py --rpc http://127.0.0.1:8080 --priv-key <hex> --store ./node_store
python scripts/storage_node_daemon.py --rpc http://127.0.0.1:8080 --prove --once   # cron 单次证明

# 监控看板
python scripts/storage_monitor.py --rpc http://127.0.0.1:8080

# 前端预览
cd C:\Users\Administrator\novachain-web
python -m http.server 8080   # 打开 storage.html（存储状态/创作者面板/节点监控/激励证明）
```

## 七、新增/改动文件

后端（novachain）：
- `core/storage_incentive.py`（新）存储激励合约
- `core/storage.py`（改）StateStore 新增 `inc_*` 状态字段与序列化
- `nova_node.py`（改）`nova:storage:inc:*` 交易校验/应用、RPC 处理器、维护与监控循环
- `network/rpc.py`（改）新增存储激励路由
- `scripts/storage_node_daemon.py`（新）节点守护进程（证明 + 心跳 + 热缓存）
- `scripts/storage_monitor.py`（新）监控看板
- `scripts/e2e_storage_incentive.py`（新）端到端演示
- `test_storage_incentive.py`（新）10 项合约/接口测试

前端（novachain-web）：
- `upload-module.js`（新）上传模块（压缩/分片/续传/进度/重试）
- `storage-incentive.js`（新）存储状态/创作者面板/节点监控/激励证明
- `player-lazy.js`（新）分层加载播放器（试听 → IPFS 全量 + 进度条）
- `storage.html`（改）新增 4 个面板
- `music.html`（改）分层播放演示区
- `apps-common.js`（改）demo 路由/演示模拟/种子数据
