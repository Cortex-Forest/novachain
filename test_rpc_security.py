import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9962)
    kw.setdefault("rpc", 8362)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


async def _make_client(node):
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    return TestClient(TestServer(app))


async def test_contract_detection():
    node = _node()
    client = await _make_client(node)
    wallet = QuantumWallet()
    async with client:
        # 普通账户不是合约
        async with client.get(f"/api/contract/{wallet.address}") as r:
            assert r.status == 200, await r.text()
            body = await r.json()
        assert body["is_contract"] is False

        # 已部署合约返回创建者与代码大小
        caddr = "0x" + "ab" * 20
        node.store.contracts[caddr] = "mint(addr)"
        node.store.contract_creator[caddr] = wallet.address
        async with client.get(f"/api/contract/{caddr}") as r:
            assert r.status == 200, await r.text()
            body2 = await r.json()
        assert body2["is_contract"] is True
        assert body2["creator"] == wallet.address
        assert body2["code_size"] == len("mint(addr)")

        # 非法地址 → 400
        async with client.get("/api/contract/nope") as r:
            assert r.status == 400


def run():
    asyncio.run(test_contract_detection())
    print("rpc-security-test: ok")


if __name__ == "__main__":
    run()
