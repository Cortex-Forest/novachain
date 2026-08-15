# -*- coding: utf-8 -*-
"""存储激励端到端演示：真实 RPC 服务 + 存储节点守护脚本全流程。

流程：起本地节点 → 超级节点质押（自动注册）→ 创作者登记文件 → 节点认领 →
守护脚本心跳 + 挑战 + 片段证明 → 链上结算 → 输出节点收益统计。

用法：
  python scripts/e2e_storage_incentive.py
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web
from aiohttp.web_runner import AppRunner, TCPSite
from core.crypto import QuantumWallet
from core.transaction import Tx
from core.storage_network import day_index
from network.rpc import setup_routes
from nova_node import NovaNode
from scripts.storage_node_daemon import StorageNodeDaemon

PORT = 18313


def stake(node, w, amount=1000.0):
    node.balances[w.address] = 100000
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], "nova:stake",
            w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    assert node.validate_tx(tx), "质押交易校验失败"
    node.apply_tx(tx)


def signed_tx(node, w, op, amount=0.0, **kw):
    data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    assert node.validate_tx(tx), "invalid: " + data[:60]
    node.apply_tx(tx)


async def main():
    print("=" * 60)
    print("Nova 存储激励系统 · 端到端演示")
    print("=" * 60)
    node = NovaNode(host="127.0.0.1", p2p=19961, rpc=PORT, use_tls=False, state_file=None)
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    runner = AppRunner(app)
    await runner.setup()
    site = TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    print(f"[RPC] 本地节点已启动 http://127.0.0.1:{PORT}")

    # 1) 超级节点质押 → 自动注册为存储节点（无需额外配置）
    node_wallet = QuantumWallet()
    stake(node, node_wallet, amount=1000.0)
    assert node_wallet.address in node.store.inc_nodes
    print(f"[1] 超级节点 {node_wallet.address[:16]}... 质押 1000 NOVA，自动注册为存储节点")

    # 2) 创作者登记文件（前 1KB 片段承诺上链）
    creator = QuantumWallet()
    node.balances[creator.address] = 100000
    node.balances[node.economy.ECOSYSTEM_FUND] = 1000000
    content = b"e2e storage proof content " * 128
    cid = "0x" + hashlib.sha3_256(b"e2e-file").hexdigest()
    commit = hashlib.sha256(content[:1024]).hexdigest()
    signed_tx(node, creator, "nova:storage:inc:file", cid=cid, size_gb=1.0,
              fragment_commit=commit, title="《星轨回声》", content_type="music")
    print(f"[2] 创作者登记文件 {cid[:20]}...（1GB，fragment_commit={commit[:16]}...）")

    # 3) 节点认领并准备本地数据
    signed_tx(node, node_wallet, "nova:storage:inc:claim", cid=cid)
    store_dir = tempfile.mkdtemp(prefix="nova_store_")
    safe = cid.replace("/", "_")
    with open(os.path.join(store_dir, safe + ".bin"), "wb") as f:
        f.write(content)
    print(f"[3] 节点认领文件，本地仓库 {store_dir}")

    # 4) 守护脚本：心跳 + 获取挑战 + 提交 1KB 片段证明 + 触发维护
    daemon = StorageNodeDaemon(f"http://127.0.0.1:{PORT}", node_wallet.private_key_hex(),
                               store_dir, capacity_gb=500)
    await asyncio.to_thread(daemon.run_once, True)  # 放到线程，避免阻塞 RPC 事件循环
    print("[4] 守护脚本完成（心跳/证明/维护）")

    # 5) 链上结算 + 收益统计
    res = node.storage_incentive.settle_epoch(day_index())
    info = node.storage_incentive.node_stats(node_wallet.address)
    status = node.storage_incentive.file_status(cid)
    print(f"[5] 结算: 奖励支出 {res['rewards_paid']} NOVA，节点收益 {info['month_revenue']} NOVA")
    print(f"    存储 {info['stored_gb']}GB / 健康度 {info['health_pct']}% / "
          f"文件状态 {status['health']} ({status['online']} 在线节点)")
    assert info["last_proof_epoch"] == day_index()
    assert abs(res["rewards_paid"] - 1.0 / 30.0) < 1e-6
    print("=" * 60)
    print("E2E_OK ✅")
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
