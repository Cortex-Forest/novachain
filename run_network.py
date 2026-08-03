import asyncio
from nova_node import NovaNode

async def main():
    seed = NovaNode(host="127.0.0.1", p2p=9000, rpc=8080, use_tls=False)
    node1 = NovaNode(host="127.0.0.1", p2p=9001, rpc=8081, seeds=["127.0.0.1:9000"], use_tls=False)
    node2 = NovaNode(host="127.0.0.1", p2p=9002, rpc=8082, seeds=["127.0.0.1:9000"], use_tls=False)

    await asyncio.gather(seed.start(), node1.start(), node2.start())

asyncio.run(main())