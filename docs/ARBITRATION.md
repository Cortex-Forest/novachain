# Nova 链上社区仲裁系统

> 覆盖五份需求：① 仲裁员注册与质押 ② 案件仲裁流程 ③ 激励与惩罚 ④ 防串通与利益回避 ⑤ 仲裁前端面板。
> 后端合约全部为确定性链上规则（随区块状态复制到全节点）；前端为 novachain-web 静态面板。
> 代码与测试均为中文注释；测试文件 `test_arbitration.py` 共 18 个用例全绿，全量回归 225 passed。

## 一、架构总览

```
投诉人/被投诉人 ── 前端（novachain-web/arbitration.html）
                        │  RPC：/api/arb/*（查询） + /api/op（OP 签名交易，nova:arb:*）
                        ▼
                   Nova 链（novachain）
                    ├─ core/arbitration.py   仲裁合约（注册/投票/案件/激励/防串通）
                    ├─ core/storage.py       链上状态（arb_* 系列字段，随区块同步）
                    ├─ nova_node.py          交易校验/应用 + RPC 端点
                    └─ network/rpc.py        路由注册
```

## 二、仲裁员注册与质押（提示词 1）

- **申请条件（合约自动校验）**：质押 500 NOVA（`nova:arb:apply`，从申请人余额扣除并锁定到
  `arb_pools["cand_<addr>"]`）；地址注册时长 ≥ 30 天（以 `tx_history` 首次链上交易时间计算）；
  历史无罚没记录（`arb_banned`）；信誉分 ≥ 70（读取 `socialfi.reputation`）。
- **社区投票**：投票期 7 天（`ARB_VOTE_PERIOD_DAYS`）；所有持币者可投票，1 NOVA = 1 票
  （按投票时余额计票，扣手续费后向下取整）；通过条件为 赞成 > 反对 × 1.5 且总票数 > 100
  （`ARB_PASS_RATIO=1.5`、`ARB_MIN_VOTES=100`）。
- **自动统计**：`maintain()` 每日轮询，投票期满自动结算（`_settle_candidate`），通过则写入
  `arb_arbitrators`（在职仲裁员池），未通过质押进入 7 天冷静期后自动返还。
- **任期**：初始任期 90 天（`ARB_TERM_DAYS`）；任期结束前 7 天可申请连任（`nova:arb:renew`），
  连任需重新社区投票；连任通过任期延长 90 天，未通过则资格立即结束、质押进入冷静期返还。
- **退出**：提前 7 天声明（`nova:arb:exit` → `leaving`）；声明期满质押进入 7 天冷静期
  （`arb_stake_pending`），到期 `maintain` 自动返还或 `nova:arb:claim_stake` 主动领取；
  有未完成案件（待抽取/投票中/二次仲裁中）时不可退出（校验是否在任一案件面板中）。

## 三、案件仲裁流程（提示词 2）

- **投诉发起**：买家支付 10 NOVA 保证金（恶意投诉名单内为 50 NOVA），携带交易 ID、卖家地址、
  投诉理由与证据链接；合约自动冻结卖家 2 倍保证金（`ARB_SELLER_FREEZE_MULT`），同一交易未结案不可重复投诉。
- **仲裁员抽取（VRF）**：从在职仲裁员池随机抽取 3 名；随机数 `_vrf(*parts)` = SHA3-256(参数串)，
  种子链 `arb_vrf_seed` 每次抽取前滚动（`_draw_panel`），上链前不可预测、可复算验证。
  自动排除：30 天内有转账/推荐关联者（`_conflicted`）、被标记排除者（`excluded`）、当事人本人。
  抽取在投诉发起后 1 小时内由 `maintain` 自动完成（`ARB_DRAW_WINDOW`），也可由任意节点调用
  `nova:arb:draw` 提前触发；抽取结果立即公开，但当事人仅见匿名编号（`panel` 编号→地址映射
  在 `revealed` 或结案前不公开）。
- **仲裁员投票**：72 小时内提交（`ARB_VOTE_WINDOW`，按编号+投票结果匿名提交）；投票记录链上公开；
  超时未投票自动扣 1 NOVA + 信誉分 -2（`_timeout_arbitrator`），并 VRF 重新抽取替代仲裁员。
- **裁决执行**：3 票一致或 2:1 多数即时执行（`_execute_case`）；支持买家 → 卖家冻结保证金全额赔付买家 +
  投诉保证金（扣仲裁报酬后）退还；支持卖家 → 投诉保证金剩余 40% 赔偿卖家、60% 进入生态基金
  （`ARB_SELLER_WIN_RATIO`），卖家冻结保证金退回。
- **二次仲裁**：当事人 7 天内可发起（`nova:arb:second`，50 NOVA 保证金）；随机抽取 7 名仲裁员
  （排除首轮已抽中者），为最终结果；推翻一次裁决时，首轮参与仲裁员各扣 10 NOVA + 信誉分 -5
  （`_execute_second` → `_slash_stake`），二次赔付按最终结果回滚重付（`_revert_and_repay`），
  余额不足由生态基金兜底。

## 四、激励与惩罚（提示词 3）

- **激励**：按时投票 +2 NOVA（`ARB_VOTE_REWARD`，`_pay_arb_reward`）；与最终多数一致 +1 信誉分
  （`ARB_MAJORITY_REP`）；连续 10 次正确裁决额外 +10 NOVA（`ARB_STREAK_LEN/ARB_STREAK_REWARD`）。
- **惩罚**：超时未投票 -1 NOVA + 信誉分 -2；裁决被二次仲裁推翻 -10 NOVA + 信誉分 -5；
  收受贿赂/明显偏袒（`nova:arb:charge` 举证成立）→ 罚没全部质押 + 永久取消资格（`_ban`）；
  与当事人串通 → 罚没质押并赔偿受害者损失（一半赔付受害者、其余进生态基金）。
- **信誉分管理**：满分 100（`ARB_REP_MAX`）、初始 50（`ARB_REP_INIT`）；低于 30（`ARB_REP_SUSPEND`）
  暂停资格，需重新质押激活（`nova:arb:reactivate`，信誉分恢复 50、任期重置 90 天）；归零 → 永久取消资格。
- **报酬来源**：仲裁员报酬 60% 出自生态基金、40% 出自投诉保证金池（`_pay_arb_reward` 按比例支出，
  生态基金不足时只付可付部分）。
- **收益统计**：`panel/{addr}` 返回累计收益（`revenue`）、累计案件数（`cases`）、当前信誉分
  （`rep`）、任期剩余天数；前端展示“已裁决 23 案，累计收益 156 NOVA，信誉分 87”。

## 五、防串通与利益回避（提示词 4）

- **利益回避（自动排除）**：仲裁员与买方/卖方近 30 天有直接转账记录 → 排除（`_has_direct_transfer`）；
  有推荐关系（`referrals` 双向/同组）→ 排除（`_has_referral`）；主动声明利益冲突
  （`nova:arb:decline`）→ 信誉分 +1（`ARB_DECLINE_REP`）并 VRF 重新抽取替代。
- **随机抽取防串通**：VRF 链上随机数上链前不可预测；抽取在投诉发起后 1 小时内自动完成；
  结果立即公开，仲裁员身份对当事人匿名（仅编号），`revealed` 前不公开投票人。
- **串通检测（`_detect_collusion`）**：同一组仲裁员 30 天内被抽取 > 3 次（`ARB_SUSPECT_PANEL_REPEAT`）
  或投票模式一致率 > 90%（≥10 次共裁，`ARB_SUSPECT_VOTE_AGREE`）→ 标记可疑，暂停抽取并进入
  7 天观察期（`observe_until`），期满自动恢复。
- **匿名投票**：仲裁员仅提交“编号 + 投票结果”（`nova:arb:vote` 携带 `number`），合约校验编号与
  发送者一致；投票结束后才公开具体投票人（`revealed=True` 或结案后 `case_public` 展示）。
- **恶意投诉检测（`_detect_malicious`）**：同一买方 30 天内投诉败诉 > 3 次（`ARB_MALICIOUS_LOSS_LIMIT`）
  → 列入恶意投诉名单，投诉保证金提高至 50 NOVA（`ARB_MALICIOUS_DEPOSIT`）；连续恶意投诉 ≥ 5 次
  （`ARB_MALICIOUS_LOCK_COUNT`）→ 限制密文交易权限 30 天（`cipher_locked`，拦截
  `nova:text:create(sealed)` 与 `nova:text:buy` 密文购买，锁定期间不可发起新投诉）。

## 六、RPC 接口清单

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/arb/summary` | 汇总：仲裁员数/候选数/案件数/生态基金/罚没总额 |
| GET | `/api/arb/arbitrators` | 在职仲裁员列表（地址/信誉分/累计案件/任期） |
| GET | `/api/arb/candidates` | 候选池列表（地址/申请时间/投票状态） |
| GET | `/api/arb/cases[?viewer=]` | 案件列表（当事人可见全量，公众匿名化） |
| GET | `/api/arb/cases/{id}[?viewer=]` | 案件详情（面板匿名编号、投票/证据/结果） |
| GET | `/api/arb/user/{addr}` | 普通用户面板：我的投诉/二次仲裁入口/投诉历史 |
| GET | `/api/arb/panel/{addr}` | 仲裁员面板：待处理案件/历史裁决/信誉分/收益/任期 |
| GET | `/api/arb/notifications/{addr}` | 链上通知（被抽中/案件结果/任期提醒/可疑标记等） |
| POST | `/api/arb/notifications/read` | 标记通知已读 |
| OP | `nova:arb:apply` 等 13 个操作 | 签名交易写链（质押/投票/投诉/裁决/二次仲裁/退出等） |

## 七、前端面板（提示词 5，novachain-web/arbitration.html）

- **入口**：`rewards.html`（权益页）新增“社区仲裁”入口，跳转 `arbitration.html`。
- **普通用户界面**：发起投诉（交易 ID + 理由 + 支付保证金）、我的投诉（待抽取/仲裁中/已裁决进度）、
  二次仲裁入口（7 天窗口内 50 NOVA 上诉）、投诉历史与结果。
- **仲裁员界面**：待处理案件（匿名编号列表）、案件详情（交易信息/证据链接/投票按钮）、
  我的裁决（历史裁决与正确率）、信誉分与收益（当前信誉分/累计收益/任期剩余天数）。
- **公众界面**：在职仲裁员列表（地址/信誉分/累计案件数）、申请成为仲裁员（质押入口 + 申请状态）、
  对候选仲裁员投票、案件公示（已裁决案件公开可查）。
- **通知系统**：被抽中仲裁员 → 网页弹窗；案件结果 → 投诉人/被投诉人通知；任期快到期 → 提醒申请连任。
  前端 `apps-common.js` 轮询 `/api/arb/notifications/{addr}` 并弹窗；演示模式使用内置 demo 数据。

## 八、链上硬约束与测试

- 所有 OP 必须 `sender == receiver`，data 为 JSON（`op` 键），金额/状态由合约确定性校验。
- `nova_node.validate_tx`：先做通用校验（防重放/金额/签名），仲裁 OP 走 `arbitration.validate_op`；
  恶意投诉锁定期间拦截密文交易与新投诉。
- 测试 `test_arbitration.py` 覆盖：申请条件与质押锁定、低信誉/封禁拒绝、真实投票计票、投票未过退款、
  投诉冻结、VRF 排除关联、买家胜诉自动赔付、超时惩罚与替补、二次仲裁推翻与惩罚、暂停与重新激活、
  信誉归零封禁、声明冲突 +1 与重抽、同组重复标记可疑、受贿罚没、恶意投诉保证金与密文锁、
  退出冷静期、连任重新投票、RPC 端点（18 用例全部通过）。
