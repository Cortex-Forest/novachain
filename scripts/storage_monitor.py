# -*- coding: utf-8 -*-
"""存储节点监控与自动恢复看板（提示词 5）。

- 每 5 分钟由链节点自动扫描心跳（30 分钟超时判离线）并触发濒危文件重新分配；
  本脚本负责链外监控展示与人工/定时触发：
  * 展示全网存储节点健康度、配额、收益（本月收益 / 存储量 / 健康度%）
  * 标记离线节点与濒危文件
  * 汇总生态基金奖励/罚没
  * --maintain 触发链上结算 / 热门保护 / 濒危恢复

用法：
  python scripts/storage_monitor.py --rpc http://127.0.0.1:8080
  python scripts/storage_monitor.py --rpc ... --maintain --priv-key <hex>   # 触发维护
"""
import argparse
import json
import sys
import time
import urllib.request

HEARTBEAT_TIMEOUT = 1800   # 30 分钟
HEALTH_GREEN = 3
HEALTH_YELLOW = 1


def get(rpc, path):
    with urllib.request.urlopen(rpc.rstrip("/") + path, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def emoji(health):
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(health, "⚪")


def main():
    p = argparse.ArgumentParser(description="Nova 存储监控看板")
    p.add_argument("--rpc", default="http://127.0.0.1:8080")
    p.add_argument("--maintain", action="store_true", help="触发链上维护（结算/热门保护/濒危恢复）")
    p.add_argument("--priv-key", default="", help="触发维护所需私钥（hex 种子）")
    a = p.parse_args()

    summary = get(a.rpc, "/api/storage/inc/summary")
    nodes = get(a.rpc, "/api/storage/nodes")["nodes"]
    print("=" * 64)
    print(f"📊 Nova 存储网络监控  {time.strftime('%F %T')}")
    print(f"   节点 {summary['nodes']} 个 / 文件 {summary['files']} 个 "
          f"/ 🟢{summary['green']} 🟡{summary['yellow']} 🔴{summary['red']}")
    print(f"   已发放奖励 {summary['rewards_paid']} NOVA / 罚没 {summary['slashed']} NOVA "
          f"/ 生态基金余额 {summary['ecosystem_fund']} NOVA")
    print("=" * 64)

    now = time.time()
    offline, online = [], []
    for addr, n in nodes.items():
        (offline if not n.get("online") else online).append((addr, n))
    for tag, group in (("🟢 在线", online), ("⚫ 离线", offline)):
        for addr, n in group:
            status = ""
            if not n.get("online"):
                status = "  [离线]"
            elif n.get("exit_at"):
                status = f"  [退出中 {max(0, int(n['exit_at'] - now) // 86400)} 天]"
            elif n.get("last_proof_at"):
                age = now - n["last_proof_at"]
                status = f"  [上次证明 {int(age // 3600)} 小时前]"
            print(f"  {tag} {addr[:14]}... 配额 {n['quota_gb']:.1f}GB 存储 {n['stored_gb']:.2f}GB "
                  f"本月收益 {n['month_revenue']:.3f} NOVA 健康度 {n['health_pct']:.0f}%{status}")

    # 濒危文件（通过创作者/状态接口采样太贵，这里直接扫描文件健康度汇总即可）
    if summary["red"] > 0:
        print(f"  🔴 {summary['red']} 个文件无在线节点（创作者已收到链上通知）")
    if summary["yellow"] > 0:
        print(f"  🟡 {summary['yellow']} 个文件存储节点不足（1-2 个）")

    if a.maintain:
        if not a.priv_key:
            print("  --maintain 需要 --priv-key")
            sys.exit(1)
        sys.path.insert(0, ".")
        from core.crypto import QuantumWallet
        from scripts.storage_node_daemon import ChainRPC
        rpc = ChainRPC(a.rpc, QuantumWallet(a.priv_key))
        rpc.maintain()
        print("  已触发链上维护（结算/热门保护/濒危恢复）")
    print("=" * 64)


if __name__ == "__main__":
    main()
