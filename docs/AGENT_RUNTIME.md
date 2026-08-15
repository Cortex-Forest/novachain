# Agent 运行时（阶段 2）—— 链上 AI 创作者的「数字生命体」

> 状态：v1 已实现，自主度 L1（预算内自动创作售卖）。

## 架构

```
链上（Nova，强制）            链外（agent/ 包，自主）
┌──────────────────┐         ┌──────────────────────────────┐
│ AI 身份/日预算    │ 事件流  │ Perceiver 感知器：状态/事件/收入 │
│ nova:ai:*        │ <────── │ Planner 决策循环：选题→生成→估算 │
│ 文本合约 90/10   │ 签名交易 │ Executor 执行器：签名+提交网关   │
│ 分账/保证金       │ ──────> │ Guardrail 护栏：白名单/冷却/暂停 │
└──────────────────┘         │ Runtime 调度：tick/loop/审计    │
                             └──────────────────────────────┘
```

一次 `tick()`：感知 → 决策 → 护栏 → 执行 → 审计（JSONL）→ 状态持久化。

## 模块

| 文件 | 职责 |
|------|------|
| `agent/config.py` | `AgentConfig`（名称/预算/白名单/冷却/每日上限/引擎/网关/文件路径） |
| `agent/models.py` | `ContentDraft` / `AgentSignal` / `AgentDecision` / `AuditEntry` / `AgentState` |
| `agent/perception.py` | 信号：paused / budget_low / income / prompt / idle |
| `agent/engine.py` | `MockContentEngine`（确定性）+ `LlmContentEngine`（OpenAI 兼容，失败回退） |
| `agent/planner.py` | 预算感知的选题与动作决策（L1：publish_text） |
| `agent/guardrail.py` | 动作白名单、本地暂停、预算/余额复核、冷却、每日上限 |
| `agent/executor.py` | 签名 `nova:text:create` 交易；dry-run 预演 |
| `agent/gateway.py` | `LocalNodeGateway`（进程内）/ `RpcGateway`（HTTP `/api/op`） |
| `agent/runtime.py` | `AgentRuntime`：tick / run_loop / status / audit / 状态服务 |

## 使用

```python
from agent import AgentConfig, AgentRuntime, LocalNodeGateway, RpcGateway
from nova_node import NovaNode

node = NovaNode(host="127.0.0.1", p2p=9902, rpc=8422, use_tls=False, state_file=None)
# 先注资并注册 nova:ai:register（见 scripts/agent_runtime_demo.py）

cfg = AgentConfig(
    name="Nova 诗灵", ai_key_hex="<AI私钥hex>", daily_budget=60.0,
    min_interval=300.0, max_actions_per_day=10,
    state_file="agent_state.json", audit_file="agent_audit.jsonl",
)
rt = AgentRuntime(cfg, LocalNodeGateway(node))   # 或 RpcGateway("http://127.0.0.1:8080")
entry = rt.tick()                                # 单次生命周期
rt.inject_prompt("时间是一面镜子")               # 注入创作指令
rt.emergency_pause() / rt.emergency_resume()     # 本地紧急暂停
rt.status() / rt.tail_audit(20)                  # 状态与审计
rt.run_loop(interval=60, max_ticks=100)          # 持续运转
```

命令行演示：

```powershell
python scripts/agent_runtime_demo.py
```

## LLM 引擎（可选）

`EngineConfig(kind="llm", api_key=os.environ["OPENAI_API_KEY"], model="gpt-4o-mini")`。
调用 OpenAI 兼容 `/chat/completions`，要求输出 JSON（title/content/tags）；
未配置密钥、网络失败或解析失败时自动回退 Mock 引擎（`last_fallback=True` 可查）。

## 安全模型

- 链上硬约束兜底：日预算/暂停在 `validate_tx`/`apply_tx` 强制，私钥泄露也花不出超预算金额。
- 运行策略护栏：白名单、冷却、每日上限、本地紧急暂停、余额/预算复核（提交前拦截）。
- 审计留痕：每次 tick 写 JSONL（决策、成本、txid、剩余预算、结果），可对账与复盘。
- 私钥托管：阶段 0/1 由运营方托管 `ai_key_hex`，远期演进 HSM/MPC 多签（阶段 4）。
