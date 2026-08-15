from aiohttp import web


@web.middleware
async def cors_middleware(request, handler):
    if request.method == 'OPTIONS':
        resp = web.Response(status=204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp

    response = await handler(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


def setup_routes(app, node):
    app.middlewares.append(cors_middleware)
    app.router.add_get('/api/status', node.rpc_status)
    app.router.add_post('/api/send', node.rpc_send)
    app.router.add_post('/api/deploy', node.rpc_deploy)
    app.router.add_post('/api/call', node.rpc_call)
    app.router.add_get('/api/balance/{addr}', node.rpc_balance)
    app.router.add_get('/api/txs/{addr}', node.rpc_txs)
    app.router.add_get('/api/tx/{txid}', node.rpc_tx)
    app.router.add_get('/api/contract/{addr}', node.rpc_contract)
    app.router.add_post('/api/stake', node.rpc_stake)
    app.router.add_post('/api/unstake', node.rpc_unstake)
    app.router.add_post('/api/claim', node.rpc_claim)
    app.router.add_post('/api/unlock', node.rpc_unlock)
    app.router.add_get('/api/stakes', node.rpc_stakes)
    app.router.add_post('/api/referral', node.rpc_referral)
    app.router.add_post('/api/light/verify', node.rpc_light_verify)
    app.router.add_get('/api/stats', node.rpc_stats)
    app.router.add_post('/api/presale/bind', node.rpc_presale_bind)
    app.router.add_post('/api/checkin', node.rpc_checkin)
    app.router.add_get('/api/early/info', node.rpc_early_info)
    app.router.add_get('/api/chat/pubkey/{addr}', node.rpc_chat_pubkey_get)
    app.router.add_post('/api/chat/pubkey', node.rpc_chat_pubkey_set)
    app.router.add_post('/api/chat/send', node.rpc_chat_send)
    app.router.add_get('/api/chat/inbox/{addr}', node.rpc_chat_inbox)
    app.router.add_post('/api/chat/ack', node.rpc_chat_ack)
    app.router.add_post('/api/storage/register', node.rpc_storage_register)
    app.router.add_post('/api/storage/pin', node.rpc_storage_pin)
    app.router.add_post('/api/storage/claim', node.rpc_storage_claim)
    app.router.add_post('/api/storage/proof', node.rpc_storage_proof)
    app.router.add_post('/api/storage/order', node.rpc_storage_order)
    app.router.add_get('/api/storage/pins', node.rpc_storage_pins)
    app.router.add_get('/api/storage/providers', node.rpc_storage_providers)
    app.router.add_get('/api/storage/orders', node.rpc_storage_orders)
    app.router.add_post('/api/compute/publish', node.rpc_compute_publish)
    app.router.add_post('/api/compute/accept', node.rpc_compute_accept)
    app.router.add_post('/api/compute/submit', node.rpc_compute_submit)
    app.router.add_get('/api/compute/tasks', node.rpc_compute_tasks)
    # ---------- SocialFi ----------
    app.router.add_post('/api/op', node.rpc_socialfi_action)
    app.router.add_post('/api/socialfi', node.rpc_socialfi_action)
    app.router.add_get('/api/socialfi/overview', node.rpc_socialfi_overview)
    app.router.add_get('/api/socialfi/{domain}', node.rpc_socialfi_domain)
    app.router.add_get('/api/text/key', node.rpc_text_key)
    app.router.add_get('/api/reputation/{addr}', node.rpc_reputation)
    app.router.add_get('/api/graph/recommend/{addr}', node.rpc_graph_recommend)
    app.router.add_get('/api/ai', node.rpc_ai_list)
    app.router.add_get('/api/ai/{addr}', node.rpc_ai_view)
