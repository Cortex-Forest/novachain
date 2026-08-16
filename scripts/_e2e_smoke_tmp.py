# -*- coding: utf-8 -*-
"""端到端冒烟：真实 RPC 服务 + 存储节点守护脚本提交流程。"""
import asyncio
import hashlib
import os
import sys
import tempfile

from aiohttp import web
from aiohttp.web_runner import AppRunner, TCPSite
from core.crypto import QuantumWallet
from core.transaction import Tx
from network.rpc import setup_routes
from nova_node import NovaNode
from scripts.storage_node_daemon import ChainRPC, StorageNodeDaemon

PORT = 18313


def signed_tx(node, w, op, amount=0.0, **kw):
    import json, time
    data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    assert node.validate_tx(tx), "invalid: " + data[:60]
    node.apply_tx(tx)


async def main():
    node = NovaNode(host="127.0.0.1", p2p=19961, rpc=PORT, use_tls=False, state_file=None)
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    runner = AppRunner(app)
    await runner.setup()
    site = TCPSite(runner, "127.0.0.1", PORT)
    await site.start()

    # 超级节点质押（自动注册）
    node_wallet = QuantumWallet()
    node.balances[node_wallet.address] = 100000
    signed_tx(node, node_wallet, None, amount=1000.0)  # placeholder replaced below

    # 创作者登记文件
    creator = QuantumWallet()
    node.balances[creator.address] = 100000
    node.balances[node.economy.ECOSYSTEM_FUND] = 1000000
    content = b"e2e storage proof content " * 128
    cid = "0x" + hashlib.sha3_256(b"e2e-file").hexdigest()
    commit = hashlib.sha256(content[:1024]).hexdigest()
    signed_tx(node, creator, "nova:storage:inc:file", cid=cid, size_gb=1.0, fragment_commit=commit, title="e2e")
    # 节点认领
    signed_tx(node, node_wallet, "nova:storage:inc:claim", cid=cid)
    # 本地仓库放好文件，供 daemon 读取片段
    store_dir = tempfile.mkdtemp()
    safe = cid.replace("/", "_")
    with open(os.path.join(store_dir, safe + ".bin"), "wb") as f:
        f.write(content)

    # daemon 单次执行（心跳 + 挑战 + 证明）
    daemon = StorageNodeDaemon(f"http://127.0.0.1:{PORT}", node_wallet.private_key_hex(),
                               store_dir, capacity_gb=500)
    daemon.run_once(maintain=True)

    # 结算当前周期（daemon 证明成功 → 发放奖励）
    from core.storage_network import day_index
    res = node.storage_incentive.settle_epoch(day_index())
    info = node.storage_incentive.node_stats(node_wallet.address)
    print("settle:", res)
    print("node stats: stored_gb=%s month_revenue=%s last_proof_epoch=%s" % (
        info["stored_gb"], info["month_revenue"], info["last_proof_epoch"]))
    assert info["last_proof_epoch"] == day_index()
    assert res["rewards_paid"] == 1.0 / 30.0
    print("E2E_OK")

    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
