import asyncio
from nova_node import NovaNode

async def main():
    # cors_origins=["*"]：本地演示显式放开 CORS（浏览器 file:// 或 localhost 访问前端时可用）；生产节点请配置具体前端来源或留空
    seed = NovaNode(host="127.0.0.1", p2p=9000, rpc=8080, use_tls=False, state_file="state_seed.json", cors_origins=["*"])
    node1 = NovaNode(host="127.0.0.1", p2p=9001, rpc=8081, seeds=["127.0.0.1:9000"], use_tls=False, state_file="state_node1.json", sync_from_seeds=True, cors_origins=["*"])
    node2 = NovaNode(host="127.0.0.1", p2p=9002, rpc=8082, seeds=["127.0.0.1:9000"], use_tls=False, state_file="state_node2.json", sync_from_seeds=True, cors_origins=["*"])

    await asyncio.gather(seed.start(), node1.start(), node2.start())

asyncio.run(main())