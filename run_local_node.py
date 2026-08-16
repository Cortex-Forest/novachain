import asyncio, traceback
from aiohttp import web
from network.rpc import setup_routes
from nova_node import NovaNode

WALLET = "0xb86a48fe63a9e65ee72bd7245bb62fe1e0751084"

@web.middleware
async def err_middleware(request, handler):
    try:
        return await handler(request)
    except Exception:
        with open("_sdk_err.log", "a", encoding="utf-8") as f:
            f.write("\n--- " + str(request.path_qs) + " ---\n")
            traceback.print_exc(file=f)
        raise

def main():
    node = NovaNode(host="127.0.0.1", p2p=9967, rpc=18081, use_tls=False, state_file=None, faucet=True)
    node.store.balances[WALLET] = 20000.0
    app = web.Application(client_max_size=262144, middlewares=[err_middleware])
    setup_routes(app, node)
    web.run_app(app, host="127.0.0.1", port=18081, print=lambda *a: None)

if __name__ == "__main__":
    main()
