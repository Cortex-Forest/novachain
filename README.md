# Nova 链（抗量子版）

全球首个全抗量子创作者公链。采用 NIST 认证的 CRYSTALS-Dilithium5 签名算法。
8100 万 NOVA，零团队预留，全人类共有。

## 安装依赖
pip install -r requirements.txt   # 核心依赖（含 pyOpenSSL）
pip install liboqs-python         # 生产必装：启用 CRYSTALS-Dilithium5 抗量子签名

## 生成 TLS 证书
python cert_gen.py

## 本地测试（单机三节点，不使用 TLS）
python run_network.py

## 生产启动（启用 TLS）
python nova_node.py --host 0.0.0.0 --p2p 9000 --rpc 8080
python nova_node.py --host 0.0.0.0 --p2p 9000 --rpc 8080 --consensus pos --validator-key <hex>
# 公共节点接入前端：显式配置 CORS 白名单（见下方「RPC 安全与 CORS」）
python nova_node.py --host 0.0.0.0 --p2p 9000 --rpc 8080 --cors-origins https://你的前端域名.vercel.app

## 生成抗量子钱包
python -c "from core.crypto import QuantumWallet; w = QuantumWallet(); print('地址:', w.address); print('私钥:', w.private_key_hex())"

## 安全特性
- CRYSTALS-Dilithium5 抗量子签名（NIST PQC 标准）
- TLS 1.3 加密通信
- RPC 频率限制（100次/秒/IP）
- CORS 白名单（M-07：默认禁止跨域读取，需显式 `--cors-origins` 或 `NOVA_CORS_ORIGINS` 放开）
- 交易去重防重放
- 数据大小限制（100KB/交易，100KB/合约）
- 质押防女巫（最低100 NOVA）
- 签到防作弊（IP限制 + 设备指纹 + 20小时间隔 + 地址签名）

## RPC 安全与签名契约（v0.8）
### CORS（M-07 修复：不再无条件 `*`）
- 默认**不发** CORS 头，浏览器无法跨域读取节点数据（安全默认）；curl / SDK / 服务端调用不受影响。
- `--cors-origins <origin1,origin2>`（逗号分隔）或环境变量 `NOVA_CORS_ORIGINS` 显式放开：仅精确匹配的来源被回显并带 `Vary: Origin`。
- 本地演示 / 开发：`--cors-origins '*'`（`run_network.py` / `run_local_node.py` 已内置）。
- 生产公共节点：填入前端站点来源（如 `https://xxx.vercel.app`），不要用 `*`。

### 无签名端点补签（M-06 / M-08）
以下端点此前可被任意调用，现已要求地址所有者签名（Ed25519 回退 / Dilithium5 均可，前端 WebCrypto 与后端交叉验签）：
- 签到 `POST /api/checkin`：body 增加 `sender_public_key`、`signature`，签名消息 `checkin:{addr}`。
- 水龙头 `POST /api/faucet/request`：body 增加 `sender_public_key`、`signature`，签名消息 `faucet:{addr}`（仅测试网 `--faucet` 开启）。
- 聊天信箱读取 `GET /api/chat/inbox/{addr}?pk=<公钥>&sig=<签名>&ts=<时间戳>`：签名消息 `inbox:{addr}:{ts}`，时间戳 ±5 分钟窗口防重放；缺失/过期/非本人签名一律 401。
- 聊天 `send`/`ack` 原本已签名（前者 `chat_signature_data`，后者 `ack:{addr}:{ids}`），保持不变。

## 部署公共节点并接入前端 PUBLIC_RPC
前端 `../novachain-web/apps-common.js` 顶部 `PUBLIC_RPC` 需填入**公网可达且 CORS 开放**的节点地址，线上站点才能真正跑链上数据（也可用 `?rpc=` URL 参数或设置页临时指定）。
1. 准备一台有公网 IP 的服务器（VPS），放行端口（示例：P2P 9000 / RPC 8080，生产建议 443 反向代理 + TLS）。
2. `pip install -r requirements.txt && pip install liboqs-python`，再 `python cert_gen.py` 生成证书。
3. 启动公共节点（CORS 白名单填前端站点来源，如 `https://xxx.vercel.app`）：
   `python nova_node.py --host 0.0.0.0 --p2p 9000 --rpc 8080 --cors-origins https://xxx.vercel.app`
4. 验证：`curl -s https://你的域名:8080/api/status`，且带 `Origin` 请求时响应头含 `Access-Control-Allow-Origin: https://xxx.vercel.app`。
5. 把 `https://你的域名:8080` 填入 `apps-common.js` 的 `PUBLIC_RPC`（或通过 `?rpc=` / 设置页临时指定）。

## Docker 部署（GHCR 自动构建）
镜像：`ghcr.io/cortex-forest/novachain`。镜像内置抗量子签名（liboqs-python + Dilithium5，构建期预编译，运行时无需联网/编译）、TLS 自签证书自动生成（`docker-entrypoint.sh`）、状态持久化与健康检查。
- **GitHub Actions 自动构建（零配置，推荐）**：推送到 GitHub 后，`.github/workflows/docker-publish.yml` 会在 push 到 main、打 `v*` 标签或手动触发时自动构建并推送 GHCR（用 GitHub 内置 token，**无需任何 Secrets / 无需 Docker Hub 账号**）。`git push` 即自动出镜像，镜像页：`https://github.com/Cortex-Forest/novachain/pkgs/container/novachain`。
- **本地一键启动**：`docker compose up -d --build`；链状态 / TLS 证书 / 聊天索引存于 `nova_data` 卷（容器内 `/data`）。
- **CORS 白名单**：`.env` 设 `NOVA_CORS_ORIGINS=https://xxx.vercel.app`（本地演示可 `*`）。
- **PoS 验证者**：`.env` 设 `NOVA_VALIDATOR_KEY=<hex>`，并在 `docker-compose.yml` 中放开 `--validator-key` 两行、把 `--consensus` 改为 `pos`。
- **拉取运行镜像**（服务器）：
  `docker pull ghcr.io/cortex-forest/novachain:latest`
  `docker run -d --name nova-node -p 8080:8080 -p 9000:9000 -v nova_data:/data -e NOVA_CORS_ORIGINS=https://xxx.vercel.app ghcr.io/cortex-forest/novachain:latest`
- **验证**：`curl -s http://127.0.0.1:8080/api/status`，应返回 `quantum_safe: true` 与 `algorithm: CRYSTALS-Dilithium5`。

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
- 未安装 oqs（即 `liboqs-python`）时，签名自动回退为 Ed25519（RFC 8032，Python 与浏览器 WebCrypto 实现）。Ed25519 **不是**抗量子算法；生产环境必须安装（`pip install liboqs-python`）以启用 CRYSTALS-Dilithium5。**注意**：PyPI 上名为 `oqs` 的包是另一个无关项目，装错会静默回退 Ed25519（代码不会报错），务必核对包名。节点 /api/status 会如实返回当前算法与 quantum_safe 状态。
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
| 内容创作 | 文本创作合约（公开/加密） | `nova:text:create` / `nova:text:buy` / `nova:text:unlist` / `nova:text:destroy` / `nova:text:release_deposit` / `nova:text:complain` / `nova:text:vote` |

- 内容类玩法携带 CID 时，链自动固定到存储网络，占用链的存储能力。
- 推荐引擎在链上确定性计算，并输出任务规格，可一键发布为算力市场的计算任务，占用链的算力能力。
- RPC：`/api/op`、`/api/socialfi/{domain}`、`/api/socialfi/overview`、`/api/reputation/{addr}`、`/api/graph/recommend/{addr}`、`/api/text/key`（文本合约公钥）。
- 文本创作合约：公开文本直接上链（内容全文）；密文文本只存密文 IPFS 哈希 + 标题 + 标识符 + 售价，正文密钥用文本合约公钥（P-256）锁定，购买后合约自动分账（90% 作者 / 10% 生态基金）并把密钥二次加密交付买家；阶梯式保证金（基础 10 / 进阶 100 / 专业 1000 NOVA，信誉 ≥80 自动下调 50%）托管在 `0x_text_escrow` 保证金池，下架 7 天无投诉自动退回，销毁密文 NFT 立即释放；投诉由社区验证者（矿工 / 质押 ≥100 / 信誉 ≥70）投票，≥2/3 支持买家自动赔付+罚没，平局进入二次仲裁（扩大至 7 人），超时自动按卖家处理。

### 测试
`python test_storage_compute.py` 覆盖存储/算力操作的链上确定性、结算与 RPC；`python test_socialfi.py` 覆盖 11 类 SocialFi 玩法（含文本创作合约的加密发布/购买解锁/保证金/仲裁）的验证、结算与声誉计算。


## 合约虚拟机（NexLang DSL，v0.6）
- 合约以 NexLang 源码部署（`/api/deploy`，需 creator 签名 `deploy:{addr}:{bytecode}`，同地址限一次），编译为栈式字节码（PUSH/STORE/LOAD/MUL/ADD/SUB/DIV/SEND/RET），由 `core/vm.py` 的 `NexusVM` 执行，10 万步上限防死循环。
- 示例：`let a = 42 + 1; let b = 7 - 3; let c = 2 * 6; return a;`
- 调用：`/api/call`（需 sender 签名），合约状态写入 `contract_state`，链上按日发放调用分红（防刷：同 sender/contract 每日一次）。
- 编译器：`nexlang_compiler.py`（函数体内 `let` 与顶层一致，含槽位分配）；测试见 `test_vm_nexlang.py`。

## AI 创作者：链上数字生命体（阶段 0 PoC，v0.7）

自主 AI 创作者拥有自己的地址、钱包与链上规则，可自主创作内容、发布售卖、自动分账。规划与路线图见 `docs/AI_CREATOR_PLAN.md`。

- 身份注册：`nova:ai:register`（name / owner / daily_budget / meta，一个地址唯一身份）。
- 预算控制：`nova:ai:config`（仅 owner 可 `pause` / `resume` / 调整 `daily_budget`，data 携带 `target`）。
- 链上强制：AI 地址发起任意交易时，`当日已支出 + 本次金额 <= 日预算` 才放行；暂停期间全部拒绝；
  支出按自然日窗口在 `apply_tx` 确定性累计，跨天自动重置。
- 收益闭环：沿用文本创作合约 90/10 自动分账，AI 钱包自动收款。
- 演示：`python scripts/ai_creator_demo.py`（身份注册 → 自动发布 → 粉丝购买分账 → 预算拒绝 → 跨天重置 → 暂停/恢复）。
- 测试：`python -m pytest test_ai_creator.py -q`（7 项，覆盖注册校验 / 预算约束 / 90:10 分账 / 窗口重置 / 暂停恢复 / 快照 / RPC）。
- RPC：`GET /api/ai`（列表）、`GET /api/ai/{addr}`（身份 + 当日预算窗口 + 最近操作）。
## 演示模式与前端联动（storage.html / compute.html）
- `apps-common.js` 演示模式新增 `demoStorageOp`（register/pin/claim/proof/order）与 `demoComputeOp`（publish/accept/submit，双节点一致结算），含守卫、奖励、余额变动、账本与事件，可与真实节点双模式切换。
- `novachain-web` 新增页面：`storage.html`（我的节点 / 固定 / 认领与证明 / 存储订单 / 提供者）、`compute.html`（发布 / 任务市场 / 我的任务）；`apps.html`、`socialfi.html` 已加入口。
- 签名方案前后端一致：Ed25519（无 oqs 时）/ Dilithium5（装 oqs 后），前端 WebCrypto 与后端可交叉验签。

## 无感守护机制（v0.9）
六大「无感」守护机制：**99% 普通用户永远感知不到，只对极端行为触发；规则事先公开、合约自动执行、无人工干预**。

### 1. 无感反 FOMO（防大户暴拉砸盘）
- 触发（只针对极端）：单地址 **24 小时内买入 >10 万 NOVA**，或 **7 天内累计买入 >50 万 NOVA** → 24 小时冷却。
- 动作：冷却期内仅暂停该地址「买入」类交易（`fan:buy` / `rev:invest` / `market:bet` / `blind:open` / `curate:buy` / `bond:buy` / `frac:buy` / `text:buy` / `ai:work:buy`）；卖出、转账、质押、签到、部署合约及全部娱乐功能**不受影响**。
- 透明化：触发前零提示；触发后钱包展示温和提示；**不标记用户、不公开名单、不影响信誉分**。
- 自动解除：冷却期固定 24h，结束自动恢复，多次触发不累加不翻倍。
- 确定性：买入记录按交易时间戳滚动统计，跨节点收敛一致；状态随快照同步。
- RPC：`GET /api/fomo/status?addr=`（冷却状态 + 提示文案）。

### 2. 无感动态手续费（防刷量 / 高频套利）
- 普通转账 / 娱乐消费 / 合约部署 / 前 1000 次合约调用：固定 `0.000001` NOVA，永不改变。
- 大额转账（单笔 >10 万 NOVA）：手续费 ×100（钱包提交前明确展示，不会悄悄扣）。
- 高频合约调用（同地址单日 >1000 次）：第 1001 次起 ×10；次日自动重置。
- 同笔同时命中多档取最高倍率（不叠乘）；先按档位再乘信誉折扣（信誉 ≥80 享 50% 折扣）。
- RPC：`GET /api/fees?addr=`（费率表 + 该地址当日适用档位）。

### 3. 无感质押过热保护（防全链锁仓）
- 比例 = 全网质押 / 流通量（流通量 = 总供应 − 已质押 − 锁仓）。
- 档位：`<50% 正常` / `≥50% 新质押收益 −20%` / `≥70% 新质押收益 −50%` / `≥80% 暂停新质押`（已有质押不受影响，可随时退出）。
- 分层质押：每笔质押按入账时档位定有效权重，**老质押永不追减**；流动性回落后新质押权重自动恢复。
- 全网质押绝对上限由 30% 放宽至 85%，由档位机制接管治理。
- RPC：`GET /api/stake/protect`（比例 / 档位 / 新质押权重 / 是否暂停 + 提示文案）。

### 4. 无感跨链大额保护（防洗钱 / 闪电贷）
- 分档延迟：单笔跨入 `<10 万 USDT 立即到账` / `10 万-100 万 延迟 1 小时` / `>100 万 延迟 24 小时`（跨出对称）。
- 单日单地址跨入累计 **≥1000 万 USDT**：该地址当日后续跨入进入风控审核（延迟 72h），次日自动恢复。
- 延迟不是冻结：自动排队，到点自动到账；`nova:bridge:deposit:cancel` 支持延迟期本人取消（撤回原链）。
- 前端显示预计到账时间（`available_at`）。
- 测试：`test_bridge.py`（分档 / 取消 / 审核）。

### 5. 无感内容质量守护（不删不封，只调曝光）
- 曝光权重完全由链上数据确定性计算：`信誉 >80 首页推荐池` / `50-80 正常曝光` / `30-50 新星池自动展示` / `<30 新星池需策展筛选` / `近 30 天败诉投诉 ≥3 仅主页展示`。
- 新星池策展：策展人（矿工 / 质押 ≥100 / 信誉 ≥70）经 `nova:curate:vote` 投票，**≥3 票**作品进入推荐池。
- 创作者随时可查自己的曝光权重与原因；通过真实创作提升信誉自动恢复曝光。
- RPC：`GET /api/content/exposure/{addr}`、`GET /api/content/feed?tier=hot`。

### 6. 无感系统负载自适应（优雅降级 / 节点扩容）
- 负载指标：UTC 自然日已确认交易数（链上确定性，全节点一致）。
- 档位：`<1000 万笔/日 全部即时` / `≥1000 万 重操作排队 1 分钟` / `≥5000 万 排队 5 分钟` / `≥1 亿 扩容激励（出块奖励 ×2 + 新矿工空投 500 NOVA 锁定）`。
- 排队不是拒绝：自动延迟，钱包显示「部署中，预计 X 秒后完成」，排队期间可继续其他操作；普通转账 / 小额消费永不受影响。
- RPC：`GET /api/load`（档位 / 当日交易数 / 排队信息）。

### 测试
`python -m pytest test_anti_fomo_mechanisms.py -q`（14 项，覆盖六机制）；`test_bridge.py` 覆盖跨链分档/取消/审核。

## 储备金与经济安全网（v0.10）
五大自动保障机制：规则公开、合约自动执行、无人工干预。新增 `0x_reserve` 储备金账户（初始 1215 万 NOVA），生态基金/验证者池安全线分别为 202.5 万 / 283.5 万。

### 1. 储备金自动回购与价格托底
- NOVA/USDT 按 UTC 日采样存 7 日价格历史；跌破 7 日均线 **30%→储备金 1% / 50%→2% / 70%→紧急 5%** 回购（单日 ≤1%、单周 ≤3%），回购 NOVA 转入 `0x_dead` 销毁，每笔（原因/金额/价格/均线）链上记录公开。
- 治理 2/3 可暂停回购（`gov_params["reserve.buyback_paused"]`）。

### 2. 质押保护期 + 逆风补偿池
- 价格暴跌 ≥50% → **30 天解质押冻结**（禁 unstake/退出）；价格回升至均线 80% 或 30 天到期自动解除。
- 生态基金划拨 5% 建逆风补偿池；冻结期内坚持运行且未解质押的验证者（质押 ≥100）每节点每天 1 NOVA。

### 3. 最低节点保障 / 紧急招募 / 种子基金 / 网络重建
- 活跃节点（质押 ≥100）= 安全线 50：低于 → **紧急招募**（质押门槛 100→50、出块奖励 ×2、矿工上限 81→1000），恢复 >50 结束。
- 储备金 3% 建种子运维基金；逆风期最早 21 位种子节点每月 +100 运维补贴（自动发放）。
- 活跃节点跌破 **10** → 网络重建：暂停新交易确认 1 小时、储备金支付重启成本、bootstrap 临时出块，恢复 >50 自动正常。
- 每 5 分钟记录节点数链上公开；前端文案「全网节点 47 个，低于安全线 50，紧急招募中」。

### 4. 事故赔付 / 紧急冻结 / 链上公告
- 储备金 2% 建事故赔付基金；治理 `payout` 提案（2/3 通过）按**实际损失 ×80%** 自动赔付，受害者签链上确认书（`nova:payout:accept`）后到账（20% 自担防道德风险）。
- 治理 `freeze` 提案（**6 小时投票、通过即生效**）冻结目标合约/模块 48 小时。
- `nova:notice:post` 链上公告（原因/影响/应对/时间表）永久存证、不删改。

### 5. 自动补血 / 减支 / 重新起航
- 生态基金 <10% → 储备金划拨 20%；验证者池 <10% → 划拨 30%；储备金 <50%（607.5 万）→ 补血暂停。
- 任一资金池低于安全线 → 所有奖励自动降至最低档（部署 0.01 / 推荐 0.01 / 调用 0.001 / 轻节点验证 0），恢复后回升。
- 生态基金与验证者池同时低于安全线 → **重新起航纪念 NFT** 销售（限量 1000，每枚 100 NOVA），收入 100% 进生态基金。

### ② 联动调整（逆风期与全网动态手续费）
- 全网手续费基准档位随**昨日** UTC 日交易量每 24h 调整：`>100 万笔 1× / 10万-100万 10× / <10万 100×`，与 v0.9 大额/高频倍率叠乘，信誉折扣最后乘。
- 逆风期（昨日 <10 万笔）：连续签到 30 天 → 忠诚者徽章（不可转让，未来空投权重 ×2）；创作者部署奖励 5→10、调用分红 0.1→0.2。
- 脚本刷签到检测：1 小时内 ≥3 次失败自动标记并临时限签。

### 测试
`python -m pytest test_reserve_safety.py -q`（19 项，覆盖五组）；RPC：`/api/reserve/status`、`/api/node/guard`、`/api/reserve/payouts`、`/api/reserve/freeze`、`/api/reserve/notices`、`/api/reserve/sail`、`/api/loyalty/{addr}`。

## EVM 兼容层 / MetaMask RPC / 跨引擎桥接（v0.11）

### 1. EVM 兼容层（`core/evm.py`）
- 纯 Python **轻量 EVM 解释器**：覆盖 Solidity 0.8 常见字节码操作码子集（算术/比较/位运算/内存/存储/跳转/事件/CALL/STATICCALL/CREATE/CREATE2），无第三方依赖。
- 密码学原语：**Keccak-256**（以太坊版）、**secp256k1 ECDSA** 验签与公钥恢复、**RLP** 编解码。
- 沙盒限制：内存 64KB / 步数 20 万 / 单合约存储槽 10 万 / gas 计量。
- 部署地址：`create_address = keccak256(rlp([sender, nonce]))[-20:]`（CREATE 语义，Hardhat/Foundry 可对接）。

### 2. MetaMask 兼容与 RPC 适配（`core/evm_rpc.py`，`POST /rpc`）
- 以太坊 JSON-RPC 标准接口，与 `/api/*` **端口复用、按方法名区分、互不冲突**：
  `eth_chainId`、`eth_blockNumber`、`eth_getBalance`、`eth_getTransactionCount`、`eth_sendRawTransaction`（完整 RLP 解码 + ECDSA 恢复）、`eth_sendTransaction`、`eth_call`、`eth_estimateGas`、`eth_gasPrice`、`eth_getTransactionReceipt`、`eth_getTransactionByHash`、`eth_getCode`、`eth_getStorageAt`、`eth_accounts`、`eth_getLogs`、`net_version`、`net_listening`、`eth_syncing`、`eth_coinbase`。
- **MetaMask 网络配置**：Chain ID `666666`（`0xa23a2`）、RPC `https://你的节点域名/rpc`、符号 `NOVA`（18 位 wei，链内换算 8 位）、区块浏览器 `https://explorer.yourdomain.com`。
- 交易路径：RLP 签名交易 → ECDSA 验签/恢复 → nonce/余额校验 → EVM 执行（或部署/转账）→ **DAG 账本同步** → 标准回执（`txHash/from/to/gasUsed/status/logs`）。

### 3. 混合账户与签名模型（`nova:evm:bind` / `nova:evm:migrate`）
- 账户双钥：量子主钥（原生）+ ECDSA 从钥（MetaMask）。
- `nova:evm:bind`：native 绑定 ECDSA 公钥 → 派生 EVM 地址（共享同一 `balances` 账本）。
- `nova:evm:migrate`：ECDSA 签名（确定性消息）验证 → native 余额迁至 EVM 地址 → `migrated` **不可逆**。
- 前端支持"用 MetaMask 参与娱乐内容交易"切换。

### 4. 跨引擎资产桥接（`core/evm_bridge.py`）
- 系统级桥接合约：EVM 侧 `EvmBridge`（`EVM_BRIDGE`）+ 原生侧 `ActorBridge`（`NATIVE_BRIDGE`），部署豁免部署奖励。
- 原生资产（粉丝代币/碎片/盲盒成就）→ EVM 包装 **ERC-721**（tokenId 确定性，OpenSea 可识别）；支持一键转回。
- **原子性**：余额/资产快照 + 顺序扣源→铸目标，失败整笔回滚（无半途状态）。
- 手续费 **0.1%**（NFT 每枚 0.001 NOVA），100% 回流验证者激励池。
- 用户端操作：`nova:bridge:evm:convert` / `nova:bridge:evm:revert`。

### 5. Solidity 部署与开发工具链（`core/evm_examples.py`）
- 真实可执行示例合约（微型汇编器生成）：`SimpleStorage`、`ERC20Nova`（totalSupply/balanceOf/transfer/approve/allowance/transferFrom/mint/name/symbol/decimals）。
- Solidity 源码模板：`MusicNFT.sol`（ERC-721 + 版税）、`BlindBox.sol`（ERC-1155 + 可验证随机）、`Subscription.sol`（ERC-20 订阅门控）。
- 工具链配置：`hardhat.config.ts`、`foundry.toml`、`networks.json`、MetaMask 添加指南。
- 测试网水龙头：`POST /api/faucet/evm`（发放测试 NOVA 到 EVM 地址）。

### RPC / 测试
- EVM RPC：`GET /api/evm/network`（网络配置）、`GET /api/evm/bind/{native}`、`GET /api/evm/bridge/summary`、`GET /api/evm/wrapped/{addr}`、`GET /api/evm/receipt/{txhash}`。
- 测试：`test_evm_compat.py`（17 项）、`test_evm_security.py`（15 项）、`test_evm_stress.py`（4 项）；审计报告：`docs/EVM_COMPAT_AUDIT.md`。

## 安全审计与回归
- 完整审计报告：`docs/AUDIT_2026-08-13.md`（P0×3 / P1×6 / P2×8 + 接口契约）。
- 回归测试：`test_audit_regressions.py`（P0/P1 修复点）、`test_vm_nexlang.py`（VM/编译器）。
- 全量测试：`python -m pytest -q`（287 项通过）；端到端 3 节点联调脚本：`scripts/e2e_full.py`（需 `PYTHONPATH` 含依赖目录，37 项通过）。
- 已知约束：SocialFi 对象 ID 由交易内容确定（不依赖墙钟时间），跨节点状态可收敛一致。
