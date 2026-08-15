# -*- coding: utf-8 -*-
"""阶段 0 PoC：AI 创作者（链上数字生命体）最小闭环演示。

故事线：
1. 人类创建者创建 AI 钱包（QuantumWallet）并注资；
2. 链上注册 AI 创作者身份（nova:ai:register），设定日预算 19 NOVA；
3. Agent 运行时（此处为脚本模拟）"醒来"→ 决策选题 → 生成内容 →
   自动签名发布文本合约（nova:text:create，保证金 10 NOVA）；
4. 粉丝购买 → 链上自动分账 90% 归 AI、10% 归生态基金；
5. 日预算硬约束：同日再次大额发布被链上拒绝；
6. 跨天窗口自动重置，发布恢复；
7. owner 可暂停 / 恢复 AI 的支出能力。

运行：python scripts/ai_creator_demo.py
"""
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)  # 保证创世文件与相对路径可用

from core.crypto import QuantumWallet
from core.transaction import Tx
from nova_node import NovaNode

TEXT_SHARE = 0.9


def _node():
    return NovaNode(host="127.0.0.1", p2p=9901, rpc=8421, use_tls=False, state_file=None)


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


def _send(node, sender, receiver, amount):
    ts = int(time.time())
    tx = Tx(sender.address, receiver, amount, [], "", sender.public_key_hex(), "", timestamp=ts)
    tx.signature = sender.sign(tx.signing_data())
    assert node.validate_tx(tx), "转账校验失败"
    node.apply_tx(tx)
    return tx


def _apply(node, tx, label=""):
    assert node.validate_tx(tx), f"校验失败：{label or tx.data[:60]}"
    node.apply_tx(tx)


def _ai_publish(node, ai, title, content, price):
    """Agent 执行器：签名并发布一篇公开文本（保证金入 escrow）。"""
    deposit = node.socialfi.text_deposit_required("basic", ai.address)
    return _signed_tx(ai, "nova:text:create", amount=deposit,
                      title=title, content=content, price=price, tier="basic",
                      visibility="public")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 72)
    print("阶段 0 PoC：Nova 链上的自主 AI 创作者（数字生命体 1.0）")
    print("=" * 72)

    node = _node()
    human = QuantumWallet()            # 人类创建者
    ai = QuantumWallet()               # AI 创作者（自己的地址 + 钱包）
    fan = QuantumWallet()              # 粉丝
    node.balances[human.address] = 500.0
    node.balances[fan.address] = 50.0
    node.balances[node.economy.ECOSYSTEM_FUND] = 1000000.0  # 生态基金（用于展示分账入账）

    print("\n[1] 创建 AI 钱包并注资")
    _send(node, human, ai.address, 100.0)
    print(f"    人类创建者 {human.address[:12]}…  →  AI {ai.address[:12]}…  +100 NOVA")
    print(f"    AI 钱包余额：{node.balances[ai.address]:.2f} NOVA")

    print("\n[2] 链上注册 AI 创作者身份（日预算 19 NOVA）")
    _apply(node, _signed_tx(ai, "nova:ai:register", name="Nova 诗灵",
                            owner=human.address, daily_budget=19.0,
                            meta="model:novapoet-v1;fingerprint:" + ai.public_key_hex()[:16]),
           "AI 注册")
    identity = node.socialfi.ai_identity(ai.address)
    print(f"    名称：{identity['name']}   状态：{identity['status']}   "
          f"日预算：{identity['daily_budget']} NOVA   owner：{human.address[:12]}…")

    print("\n[3] Agent 醒来 → 决策选题 → 生成内容 → 自动签名发布")
    poem = ("夜是流动的墨，\n每一行都通往一颗未被命名的星。")
    _apply(node, _ai_publish(node, ai, "给夜的情书（AI 原创）", poem, price=5.0), "AI 自动发布")
    tid = next(iter(node.store.text_assets))
    asset = node.store.text_assets[tid]
    print(f"    已发布：{asset['title']}（保证金 {asset['deposit']} NOVA 入托管）")
    b = node.socialfi.ai_budget_state(ai.address)
    print(f"    日预算窗口：已用 {b['spent']} / 剩余 {b['remaining']} NOVA")

    print("\n[4] 粉丝购买 → 链上自动分账（90% 归 AI / 10% 归生态基金）")
    ai_bal0 = node.balances[ai.address]
    eco_bal0 = node.balances[node.economy.ECOSYSTEM_FUND]
    _apply(node, _signed_tx(fan, "nova:text:buy", amount=5.0, text_id=tid), "粉丝购买")
    author_share = 5.0 * TEXT_SHARE
    eco_share = 5.0 - author_share
    print(f"    售价 5 NOVA → AI 收入 {author_share} / 生态基金 {eco_share}")
    print(f"    AI 钱包：{ai_bal0:.2f} → {node.balances[ai.address]:.2f} NOVA（+{node.balances[ai.address] - ai_bal0:.2f}）")
    print(f"    生态基金：{eco_bal0:.2f} → {node.balances[node.economy.ECOSYSTEM_FUND]:.2f} NOVA")
    assert abs((node.balances[ai.address] - ai_bal0) - author_share) < 1e-6
    assert abs((node.balances[node.economy.ECOSYSTEM_FUND] - eco_bal0) - eco_share) < 1e-6

    print("\n[5] 日预算硬约束：同日再发布（+10 NOVA）被链上拒绝")
    second = _ai_publish(node, ai, "第二篇（超预算）", "内容", price=3.0)
    if node.validate_tx(second):
        print("    [异常] 超预算交易竟然通过了！")
        sys.exit(1)
    print("    链上拒绝 ✓（已用 10 + 本次 10 > 预算 19）")
    print(f"    当前窗口：已用 {node.socialfi.ai_budget_state(ai.address)['spent']} NOVA")

    print("\n[6] 跨天窗口自动重置（模拟日期滚动到次日）")
    node.store.ai_daily_spend[ai.address] = {"date": "2000-01-01", "spent": 0.0}
    b2 = node.socialfi.ai_budget_state(ai.address)
    print(f"    新窗口：已用 {b2['spent']} / 剩余 {b2['remaining']} NOVA")
    _apply(node, _ai_publish(node, ai, "次日的诗", "新的一天。", price=3.0), "次日发布")
    print("    次日自动发布成功 ✓")

    print("\n[7] owner 暂停 → AI 失去支出能力；恢复 → 能力回归")
    _apply(node, _signed_tx(human, "nova:ai:config", action="pause", target=ai.address), "owner 暂停")
    blocked = _ai_publish(node, ai, "暂停中的尝试", "x", price=1.0)
    print(f"    AI 状态：{node.socialfi.ai_identity(ai.address)['status']} → "
          f"发布被拒：{not node.validate_tx(blocked)}")
    _apply(node, _signed_tx(human, "nova:ai:config", action="resume", target=ai.address), "owner 恢复")
    print(f"    恢复后状态：{node.socialfi.ai_identity(ai.address)['status']}")

    print("\n" + "=" * 72)
    print("阶段 0 PoC 全部通过 ✅  数字生命体 1.0 闭环成立：")
    print("身份注册 → 自动创作发布 → 自动售卖 → 自动分账 → 预算约束 → 可暂停")
    print("=" * 72)


if __name__ == "__main__":
    main()