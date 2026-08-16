# Nova 娱乐链新模块说明 / Nova Ecosystem Modules

本文档汇总新增的 8 大功能模块：预言机、跨链桥、DEX、治理、DID 与声誉、订阅、浏览器与索引器、JS SDK，外加测试网水龙头与 npm 打包。
所有模块遵循统一约定：**signed tx（sender == receiver，data 为 JSON `{"op":"nova:xxx:yyy", ...}`）**，
查询走 RPC `GET /api/<module>/...`，写操作走 `POST /api/<module>/op`。

## 1. 预言机 Oracle（core/oracle.py · test_oracle.py · oracle.html）

- **定位**：为链上合约提供外部数据：USDT/ETH 价格、VRF 可验证随机数、AI 生成结果验证、现实事件结果；多数据源聚合，不依赖单一来源。
- **VRF**：ECVRF-P256（`vrf_keygen/prove/verify`），随机数上链前不可预测、上链后可由公钥+证明验证；盲盒/抽奖/AI 验证共用。
- **价格**：Chainlink/Pyth/Binance/OKX/Gate 五源取中位数，5 分钟发布一次；单源偏离聚合价 >10% 拒绝，>25% 可举报罚没。
- **AI 验证**：`nova:oracle:ai:submit`（创作者）→ `nova:oracle:ai:verify`（节点，通过奖励 0.1 NOVA）→ 合约才允许上架。
- **节点**：质押 500 NOVA，作恶全额罚没进生态基金；冷启动用 Chainlink 公共测试网，主网由超级节点担任。
- 操作码：`nova:oracle:node:register/exit/claim`、`vrf:request/fulfill`、`price:update`、`report`、`ai:submit/verify`。
- 查询：`GET /api/oracle/summary`、`/price/{feed}`、`/vrf/{request_id}`、`/nodes`、`/ai/{content_hash}`。

## 2. 跨链桥 Bridge（core/bridge.py · test_bridge.py · bridge.html）

- **定位**：打通 BSC/ETH/Polygon；外部资产（USDT/ETH/BNB）跨入铸包装资产（nUSDT/nETH），NOVA 跨出到外部链。
- **跨入**：桥节点监听外部链存款事件 → `nova:bridge:deposit` 登记 → 3/5 节点 `deposit:sign` → `deposit:claim` 铸造。
- **跨出**：用户 `nova:bridge:withdraw`（NOVA 直接扣余额；包装资产销毁）→ 节点 `withdraw:sign` → `withdraw:confirm` 释放。
- **多签**：3/5 签名防单点作恶；节点质押 1000 NOVA，作恶罚没全部质押。
- **额度与延迟**：日额度上限 100 万 USDT；>10 万 USDT 大额延迟 24 小时。
- **手续费**：跨入/跨出 0.1%（最低 1 USDT），100% 回流验证者激励池（`nova:bridge:pool:flush`）。
- 支持资产：`USDT`、`ETH`（BNB 链 WETH）、`NOVA`；链名小写 `bsc/eth/polygon`。

## 3. 去中心化交易所 DEX（core/dex.py · test_dex.py · dex.html）

- **AMM**：恒定乘积 `x·y=k`；交易对 `NOVA/USDT`、`NOVA/nETH`（链上 id 为 `NOVA/USDT`、`NOVA/nETH`）。
- **手续费**：每笔 0.3%，0.25% 归 LP、0.05% 回购 NOVA 销毁（通缩）。
- **流动性**：`nova:dex:add` 注入 NOVA+包装资产得 LP；`nova:dex:remove` 取回资产+手续费分成；比例偏差 >5% 拒绝。
- **挖矿**：LP 质押 `nova:dex:farm:stake/unstake/claim`，APR 由治理调整（初期 20-50%）。
- **滑点保护**：`min_out`（默认期望值 95%），超限自动取消；`GET /api/dex/split` 大额分拆建议。
- **安全**：治理紧急暂停 `set_paused`；初始流动性由预售资金 `bootstrap()` 提供。
- 查询：`/api/dex/summary`、`/quote`、`/split`、`/lp/{addr}`、`/farm/{pair}/{addr}`。

## 4. 链上治理 Governance（core/governance.py · test_governance.py · governance.html）

- **范围**：经济参数（手续费/出块奖励/质押门槛/减半）、基金支出、协议升级（2/3 绝对多数）、仲裁参数。
- **投票权**：1 NOVA = 1 票（余额+质押+锁仓），可委托；`voting_power(addr)` 递归聚合防循环。
- **流程**：发起（权益 ≥1000 或 100 人联署）→ 公示 3 天 → 投票 7 天 → 通过（赞成>反对 且 投票率≥流通 10%）→ 时间锁 48h → 自动执行。
- **执行**：参数调整自动执行；基金支出需 3/5 节点 `nova:gov:confirm` 多签；`tick()` 确定性推进状态。
- 操作码：`nova:gov:propose/endorse/vote/delegate/confirm/execute/cancel`。
- 查询：`/api/gov/summary`、`/proposals`、`/proposals/{pid}`、`/power/{addr}`。

## 5. DID 与声誉（core/did.py · test_did.py · did.html）

- **DID**：绑定 email/telegram/x/avatar 的 SHA3-512 哈希（原始数据永不上链），私钥签名确认，随时撤销，可设可见性。
- **创作者认证**：提交作品集（本人部署的合约地址列表）→ 社区投票（≥10 票、赞成 ≥50%）→ 获得不可转让 `nova:did:creator` 徽章。
- **声誉分**：满分 100 初始 50；创作质量 30% + 社区贡献 25% + 资产稳定 25% + 身份完整 20%。
- **用途**：>80 降手续费 20%/预售优先/高空投权重；<30 限制密文发布、提高投诉保证金。
- **隐私**：详情仅本人可见（`viewer` 参数），公开只显示总分。
- 操作码：`nova:did:bind/unbind/apply/vote/update`；查询 `/api/did/summary`、`/api/did/{addr}`、`/api/did/reputation/{addr}`。

## 6. 创作者订阅（core/subscription.py · test_subscription.py · subscription.html）

- **模式**：按月（30 天）/ 永久 / 分档（最多 8 档），档位字段 `{id, name, price, period, benefits}`。
- **自动续费**：`auto_renew=true` 时节点 `nova:sub:renew` 每月扣款；余额不足自动取消；用户可 `nova:sub:cancel`（永久会员不可）。
- **权益**：专属内容解锁、订阅者徽章（`nova:sub:*` soulbound）、新作 24h 优先购买权、私密社区。
- **分账**：90% 创作者 / 10% 生态基金；Gas 回流验证者池。
- 查询：`/api/sub/summary`、`/creator/{addr}`、`/status/{user}/{creator}`。

## 7. 浏览器与索引器（explorer/ · test_explorer.py · explore.html）

- **索引器**：`python -m explorer --node-url http://127.0.0.1:8080 --db sqlite:///explorer.db`；监听新区块/交易/合约部署，写入 PostgreSQL（或 SQLite 本地）。
- **查询**：REST `/api/indexer/*` + GraphQL `POST /graphql`；按地址/交易/合约/高度查询；常用结果缓存 1 分钟；倒序分页。
- **浏览器前端**：首页统计（总交易/地址/合约/质押）、地址/交易/合约/区块详情页、搜索即时下拉（`/api/chain/search`）。
- 链侧同步源：`/api/chain/sync`（增量）、`/api/chain/block/{height}`、`/api/chain/stats`。

## 8. JS SDK（novachain-web/sdk/nova-sdk-open.js · sdk/sdk-demo.html · docs/api/）

- **零依赖 UMD**：浏览器 `<script>` 与 Node `require` 通用；导出 `NovaWallet/NovaContract/NovaContent/NovaStaking/NovaSubscription/NovaOracle/NovaBridge/NovaDex/NovaGovernance/NovaDID/NovaChain/NovaEvents/NovaFaucet`。
- **加密**：内建 BIP39 + SLIP-10（`m/44'/223'/0'/0'/0'`）+ Ed25519 + SHA3-512 地址派生，与钱包页实现一致。
- **事件**：`NovaEvents` 轮询 `/api/chain/sync`，回调 `onTx/onBlock/onContractEvent/onStats`。
- **测试**：`sdk/test/nova-sdk-crypto.test.js`（离线，16 项）；`sdk/test/nova-sdk-e2e.js`（本地节点，33 项）。
- **npm 打包**：`sdk/package.json`（`@nova/sdk` v1.0.0，semver）+ `sdk/index.d.ts`（TypeScript 声明）+ `sdk/README.md`；零依赖 UMD，Node 18+ / 浏览器通用。
- **文档**：`docs/api/README.md`（中英双语 REST 文档）+ `docs/api/swagger.yaml`（OpenAPI 3.0，62 路径）。

## 9. 测试网水龙头 Faucet（nova_node.py 内置 RPC · test_faucet.py · faucet.html）

- **定位**：开发者免费领取测试 NOVA；仅测试网（节点 `--faucet` 启动）开放，主网自动 403 关闭。
- **领取**：`POST /api/faucet/request`（`{addr, fingerprint?}`，无需签名）→ 从 `0x_faucet_pool` 发放 100 NOVA 测试币，返回回执。
- **限频防滥用**：同一地址 24 小时限领 1 次 ｜ 同一 IP 每日最多 2 次 ｜ 可选设备指纹唯一性 ｜ 每日全网发放上限 20,000 NOVA。
- **状态**：`GET /api/faucet/status` 返回资金池余额、今日发放、限频参数与累计统计。
- **资金池**：测试网启动时一次性铸造 100 万测试 NOVA；状态持久化到 `faucet_claims/faucet_daily/faucet_receipts`。
- **SDK**：`NovaFaucet.status()/request(addr, fingerprint)`；页面 `faucet.html`（apps.html 卡片入口）。

## 测试运行 / Run tests

```bash
cd C:\Users\Administrator\novachain
python -m pytest -q          # 261 passed（含全部新模块与水龙头）
cd C:\Users\Administrator\novachain-web
node sdk/test/nova-sdk-crypto.test.js
```