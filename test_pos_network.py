# -*- coding: utf-8 -*-
import asyncio, time, sys
from nova_node import NovaNode
from core.crypto import QuantumWallet
from core.transaction import Tx

async def main():
    va, vb, vc = QuantumWallet(), QuantumWallet(), QuantumWallet()
    node_a = NovaNode(host="127.0.0.1", p2p=9350, rpc=8280, use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=va.private_key_hex(), block_interval=60)
    node_b = NovaNode(host="127.0.0.1", p2p=9351, rpc=8281, seeds=["127.0.0.1:9350"], use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=vb.private_key_hex(), block_interval=60)
    node_c = NovaNode(host="127.0.0.1", p2p=9352, rpc=8282, seeds=["127.0.0.1:9350"], use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=vc.private_key_hex(), block_interval=60)
    nodes = [node_a, node_b, node_c]

    for n in nodes:
        for w in (va, vb, vc):
            n.balances[w.address] = 1000
        await n.p2p.start_server()
    await node_b.p2p.connect_to_peer("127.0.0.1:9350")
    await node_c.p2p.connect_to_peer("127.0.0.1:9350")
    await asyncio.sleep(2)

    def signed_tx(w, receiver, amt, data=""):
        ts = int(time.time())
        tx = Tx(w.address, receiver, amt, [], data, w.public_key_hex(), "", timestamp=ts)
        tx.signature = w.sign(tx.signing_data())
        return tx

    # 三笔质押签名交易分别从三个节点广播
    for tx, node in [(signed_tx(va, va.address, 500, "nova:stake"), node_a),
                     (signed_tx(vb, vb.address, 300, "nova:stake"), node_b),
                     (signed_tx(vc, vc.address, 100, "nova:stake"), node_c)]:
        await node.broadcast_tx(tx)
    await asyncio.sleep(3)

    ok = True
    if not all(set(n.store.stakes.keys()) == set(nodes[0].store.stakes.keys()) for n in nodes) or len(nodes[0].store.stakes) != 3:
        print("FAIL: 质押未在 3 节点间收敛"); ok = False
    else:
        print(f"[POS] 质押已收敛: {dict(nodes[0].store.stakes)}")

    # 驱动 6 个区块：每轮先广播一笔转账，再让当选者出块
    for i in range(6):
        sender = (va, vb, vc)[i % 3]
        tx = signed_tx(sender, "0x" + f"{i:040x}", 1)
        await nodes[i % 3].broadcast_tx(tx)
        await asyncio.sleep(0.5)
        producers = []
        for n in nodes:
            b = n.consensus.produce_block()
            if b:
                producers.append(n.node_id)
                await n.p2p.gossip({"type": "new_block", "block": b.to_dict()})
        await asyncio.sleep(0.6)
        heights = [n.consensus.chain_height() for n in nodes]
        print(f"[POS] round#{i}: producers={producers} heights={heights}")
        if len(producers) != 1:
            print(f"FAIL: 轮次 {i} 出块者数量={len(producers)}（应恰 1 个）"); ok = False
        if len(set(heights)) != 1 or heights[0] != i + 1:
            print("FAIL: 三节点高度不一致或未增长"); ok = False

    hashes = {n.consensus.latest_checkpoint() for n in nodes}
    bal_eq = all(n.balances == nodes[0].balances for n in nodes)
    stake_eq = all(n.store.stakes == nodes[0].store.stakes for n in nodes)
    dag_eq = all(n.dag == nodes[0].dag for n in nodes)
    print(f"[POS] 链顶一致={len(hashes)==1} 余额一致={bal_eq} 质押一致={stake_eq} dag一致={dag_eq}")
    if len(hashes) != 1 or not bal_eq or not stake_eq or not dag_eq:
        print("FAIL: 状态未收敛"); ok = False
    print(f"[POS] chain_height={nodes[0].consensus.chain_height()}")

    for n in nodes:
        n.p2p.close_all()
    print("POS-NETWORK-3N: " + ("ok" if ok else "FAILED"))
    sys.exit(0 if ok else 1)

asyncio.run(main())
