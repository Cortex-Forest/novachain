from aiohttp import web

def setup_routes(app, node):
    app.router.add_get('/api/status', node.rpc_status)
    app.router.add_post('/api/send', node.rpc_send)
    app.router.add_post('/api/deploy', node.rpc_deploy)
    app.router.add_post('/api/call', node.rpc_call)
    app.router.add_get('/api/balance/{addr}', node.rpc_balance)
    app.router.add_post('/api/stake', node.rpc_stake)
    app.router.add_post('/api/unstake', node.rpc_unstake)
    app.router.add_post('/api/claim', node.rpc_claim)
    app.router.add_get('/api/stakes', node.rpc_stakes)
    app.router.add_post('/api/referral', node.rpc_referral)
    app.router.add_post('/api/light/verify', node.rpc_light_verify)
    app.router.add_get('/api/stats', node.rpc_stats)
    app.router.add_post('/api/presale/bind', node.rpc_presale_bind)
    app.router.add_post('/api/checkin', node.rpc_checkin)
    app.router.add_get('/api/early/info', node.rpc_early_info)