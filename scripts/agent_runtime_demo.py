# -*- coding: utf-8 -*-
"""阶段 2：AI 创作者 Agent 运行时（数字生命体 2.0）演示。

故事线：
1. 人类创建者创建 AI 钱包并注资，链上注册身份（日预算 60 NOVA）；
2. Agent 运行时自动运转 3 个 tick：感知 → 决策 → 内容引擎生成 → 签名发布；
3. 粉丝购买后，运行时感知到链上收入（income 信号）；
4. 外部注入创作指令（prompt），下个 tick 优先按该主题创作；
5. owner 把日预算调低到 10（已花 40）→ 运行时护栏直接拦截（预算不足）；
6. owner 暂停 → 运行时空转等待；恢复并把预算调回 60 → 自动恢复创作；
7. 即使私钥泄露，绕过运行时直接构造超预算交易，也会被链上硬约束拒绝。

运行：python scripts/agent_runtime_demo.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from agent import AgentConfig, AgentRuntime, LocalNodeGateway
from core.crypto import QuantumWallet
from core.transaction import Tx
from nova_node import NovaNode

BUDGET = 60.0


def _node():
    return NovaNode(host="127.0.0.1", p2p=9902, rpc=8422, use_tls=False, state_file=None)


def _signed_tx(w, op, amount=0.0, **kw):
    payload = {"op": op}
    if amount:
        payload["amount"] = amount
    payload.update(kw)
    data = json.dumps(payload, ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _apply(node, tx, label=""):
    assert node.validate_tx(tx), f"校验失败：{label or tx.data[:60]}"
    node.apply_tx(tx)


def _send(node, sender, receiver, amount):
    ts = int(time.time())
    tx = Tx(sender.address, receiver, amount, [], "", sender.public_key_hex(), "", timestamp=ts)
    tx.signature = sender.sign(tx.signing_data())
    assert node.validate_tx(tx), "转账校验失败"
    node.apply_tx(tx)


def _buy_latest(node, fan):
    tid = next(reversed(list(node.store.text_assets)))
    price = float(node.store.text_assets[tid]["price"])
    _apply(node, _signed_tx(fan, "nova:text:buy", amount=price, text_id=tid), "粉丝购买")


def _owner_config(node, owner, action, target, budget=None):
    kw = {"action": action, "target": target}
    if budget is not None:
        kw["daily_budget"] = budget
    _apply(node, _signed_tx(owner, "nova:ai:config", **kw), f"owner {action}")


def _show_tick(rt, n):
    entry = rt.tick()
    d = entry.decision or {}
    if entry.status == "ok":
        print(f"    tick#{n:02d} [ok]      {d.get('reason','')} | txid {entry.txid[:16]}… "
              f"| 成本 {entry.cost} | 剩余预算 {entry.budget_remaining}")
    elif entry.status == "blocked":
        print(f"    tick#{n:02d} [blocked] 护栏拦截：{entry.error}")
    elif entry.status == "error":
        print(f"    tick#{n:02d} [error]   {entry.error}")
    else:
        print(f"    tick#{n:02d} [idle]    {entry.error}")
    return entry


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 76)
    print("阶段 2：AI 创作者 Agent 运行时 —— 数字生命体 2.0 自主创作演示")
    print("=" * 76)

    node = _node()
    human = QuantumWallet()
    ai = QuantumWallet()
    fan = QuantumWallet()
    node.balances[human.address] = 500.0
    node.balances[fan.address] = 50.0
    node.balances[node.economy.ECOSYSTEM_FUND] = 1000000.0

    print("\n[1] 创建 AI 钱包、注资 200 NOVA、链上注册身份（日预算 60 NOVA）")
    _send(node, human, ai.address, 200.0)
    _apply(node, _signed_tx(ai, "nova:ai:register", name="Nova 诗灵",
                            owner=human.address, daily_budget=BUDGET,
                            meta="model:novapoet-v1;runtime:agent-v2"),
           "AI 注册")
    print(f"    AI 地址 {ai.address[:12]}…  owner {human.address[:12]}…  余额 {node.balances[ai.address]}")

    tmp = tempfile.mkdtemp(prefix="nova_agent_demo_")
    cfg = AgentConfig(
        name="Nova 诗灵",
        owner=human.address,
        ai_key_hex=ai.private_key_hex(),
        daily_budget=BUDGET,
        min_interval=0.0,
        max_actions_per_day=20,
        state_file=os.path.join(tmp, "state.json"),
        audit_file=os.path.join(tmp, "audit.jsonl"),
    )
    rt = AgentRuntime(cfg, LocalNodeGateway(node))
    print(f"    运行时就绪：引擎={cfg.engine.kind}  自主度={cfg.autonomy_level}  "
          f"状态文件={os.path.basename(cfg.state_file)}")

    print("\n[2] Agent 自动运转 3 个 tick：感知 → 决策 → 生成 → 签名发布")
    for i in range(1, 4):
        _show_tick(rt, i)
    st = rt.status()
    print(f"    已发布 {st['total_published']} 篇，当日支出 {st['total_spent']:.2f}，"
          f"链上剩余预算 {st['budget']['remaining']}")

    print("\n[3] 粉丝购买最新作品 → 运行时感知链上收入")
    _buy_latest(node, fan)
    _show_tick(rt, 4)
    st = rt.status()
    print(f"    累计感知收入 {st['total_income']:.2f} NOVA（90% 分账入 AI 钱包）")

    print("\n[4] 注入创作指令 → 下个 tick 优先按该主题创作")
    rt.inject_prompt("时间是一面镜子")
    _show_tick(rt, 5)

    print("\n[5] owner 将日预算调低到 10（当日已花 40）→ 护栏拦截")
    _owner_config(node, human, "budget", ai.address, budget=10.0)
    _show_tick(rt, 6)

    print("\n[6] owner 暂停 → 空转等待；恢复 + 预算调回 60 → 自动恢复创作")
    _owner_config(node, human, "pause", ai.address)
    _show_tick(rt, 7)
    _owner_config(node, human, "resume", ai.address)
    _owner_config(node, human, "budget", ai.address, budget=BUDGET)
    _show_tick(rt, 8)

    print("\n[7] 私钥泄露场景：绕过运行时直接构造超预算交易 → 链上硬约束拒绝")
    leaked = _signed_tx(ai, "nova:text:create", amount=10.0,
                        title="攻击尝试", content="x", price=1.0,
                        tier="basic", visibility="public")
    print(f"    链上校验通过？{node.validate_tx(leaked)}（应 False，当日已支出 50 > 预算 60 上限校验拦截）")

    print("\n" + "-" * 76)
    st = rt.status()
    print("运行时状态：", json.dumps(st, ensure_ascii=False, indent=2))
    print("\n审计日志尾部（JSONL）：")
    with open(cfg.audit_file, "r", encoding="utf-8") as f:
        for line in f.readlines()[-4:]:
            e = json.loads(line)
            print("   ", e["tick"], e["status"], e.get("error", "")[:40],
                  e["txid"][:12] if e["txid"] else "")

    print("\n" + "=" * 76)
    print("阶段 2 演示完成 ✅  数字生命体 2.0：自主感知 → 决策 → 创作 → 发布 → 分账 → 护栏")
    print("=" * 76)


if __name__ == "__main__":
    main()
