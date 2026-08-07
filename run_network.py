import asyncio
from nova_node import NovaNode

async def main():
    seed = NovaNode(host="127.0.0.1", p2p=9000, rpc=8080, use_tls=False, state_file="state_seed.json")
    node1 = NovaNode(host="127.0.0.1", p2p=9001, rpc=8081, seeds=["127.0.0.1:9000"], use_tls=False, state_file="state_node1.json")
    node2 = NovaNode(host="127.0.0.1", p2p=9002, rpc=8082, seeds=["127.0.0.1:9000"], use_tls=False, state_file="state_node2.json")

    await asyncio.gather(seed.start(), node1.start(), node2.start())

asyncio.run(main())