from aiohttp import web


def _normalize_origin(origin: str) -> str:
    return (origin or "").strip().lower().rstrip("/")


def cors_middleware_factory(allow_origins=None):
    """CORS 中间件工厂（M-07 修复：不再无条件放行 *）。

    - 默认（allow_origins 为空）：不返回任何 CORS 头，浏览器跨域读取被拦截（安全默认）。
      非浏览器客户端（curl / SDK / 服务端）不受影响。
    - allow_origins 含 "*"：回显 Access-Control-Allow-Origin: *（仅限本地开发 / 演示显式开启）。
    - 其余情况：仅当请求 Origin 精确匹配白名单中的来源时才回显该来源，并带 Vary: Origin。
    """
    wildcard = False
    allowed = set()
    for o in (allow_origins or []):
        o = _normalize_origin(o)
        if o == "*":
            wildcard = True
        elif o:
            allowed.add(o)

    @web.middleware
    async def cors_middleware(request, handler):
        origin = _normalize_origin(request.headers.get("Origin", ""))
        if not origin:
            # 无 Origin 的请求（curl / SDK / 服务端调用）不触发 CORS 语义
            if request.method == "OPTIONS":
                return web.Response(status=204)
            return await handler(request)

        allow = "*" if wildcard else (origin if origin in allowed else "")

        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            resp = await handler(request)

        if allow:
            resp.headers["Access-Control-Allow-Origin"] = allow
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    return cors_middleware


def setup_routes(app, node):
    app.middlewares.append(cors_middleware_factory(getattr(node, "cors_origins", None)))
    app.router.add_get('/api/status', node.rpc_status)
    # ---------- v0.9 无感机制查询接口 ----------
    app.router.add_get('/api/fomo/status', node.rpc_fomo_status)
    app.router.add_get('/api/fees', node.rpc_fees)
    app.router.add_get('/api/stake/protect', node.rpc_stake_protect)
    app.router.add_get('/api/content/exposure/{addr}', node.rpc_content_exposure)
    app.router.add_get('/api/content/feed', node.rpc_content_feed)
    app.router.add_get('/api/load', node.rpc_load)
    # ---------- v0.10 储备金与经济安全网 ----------
    app.router.add_get('/api/reserve/status', node.rpc_reserve_status)
    app.router.add_get('/api/node/guard', node.rpc_node_guard)
    app.router.add_get('/api/reserve/payouts', node.rpc_reserve_payouts)
    app.router.add_get('/api/reserve/freeze', node.rpc_reserve_freeze)
    app.router.add_get('/api/reserve/notices', node.rpc_reserve_notices)
    app.router.add_get('/api/reserve/sail', node.rpc_reserve_sail)
    app.router.add_post('/api/reserve/sail/buy', node.rpc_reserve_sail_buy)
    app.router.add_get('/api/loyalty/{addr}', node.rpc_loyalty)
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
    # ---------- 存储激励 / 存储状态 / 监控恢复 ----------
    app.router.add_post('/api/storage/inc/file', node.rpc_storage_inc_file)
    app.router.add_post('/api/storage/inc/claim', node.rpc_storage_inc_claim)
    app.router.add_post('/api/storage/prove', node.rpc_storage_prove)
    app.router.add_post('/api/storage/heartbeat', node.rpc_storage_heartbeat)
    app.router.add_post('/api/storage/inc/upgrade', node.rpc_storage_inc_upgrade)
    app.router.add_post('/api/storage/inc/exit', node.rpc_storage_inc_exit)
    app.router.add_post('/api/storage/inc/settle', node.rpc_storage_inc_settle)
    app.router.add_post('/api/storage/inc/protect', node.rpc_storage_inc_protect)
    app.router.add_post('/api/storage/inc/reassign', node.rpc_storage_inc_reassign)
    app.router.add_post('/api/storage/inc/access', node.rpc_storage_inc_access)
    app.router.add_post('/api/storage/inc/reupload', node.rpc_storage_inc_reupload)
    app.router.add_get('/api/storage/status/{file_hash}', node.rpc_storage_status)
    app.router.add_get('/api/storage/nodes', node.rpc_storage_nodes)
    app.router.add_get('/api/storage/nodes/{addr}/challenge', node.rpc_storage_challenge)
    app.router.add_get('/api/storage/nodes/{addr}/revenue', node.rpc_storage_revenue)
    app.router.add_get('/api/storage/creator/{addr}', node.rpc_storage_creator)
    app.router.add_get('/api/storage/events', node.rpc_storage_events)
    app.router.add_get('/api/storage/inc/summary', node.rpc_storage_inc_summary)
    app.router.add_post('/api/compute/publish', node.rpc_compute_publish)
    app.router.add_post('/api/compute/accept', node.rpc_compute_accept)
    app.router.add_post('/api/compute/submit', node.rpc_compute_submit)
    app.router.add_get('/api/compute/tasks', node.rpc_compute_tasks)
    app.router.add_post('/api/compute/register', node.rpc_compute_register)
    app.router.add_get('/api/compute/nodes', node.rpc_compute_nodes)
    app.router.add_get('/api/compute/node/{addr}', node.rpc_compute_node)
    app.router.add_get('/api/compute/income/{addr}', node.rpc_compute_income)
    app.router.add_get('/api/compute/overview', node.rpc_compute_overview)
    app.router.add_get('/api/compute/events', node.rpc_compute_events)
    # ---------- SocialFi ----------
    app.router.add_post('/api/op', node.rpc_socialfi_action)
    app.router.add_post('/api/socialfi', node.rpc_socialfi_action)
    app.router.add_get('/api/socialfi/overview', node.rpc_socialfi_overview)
    app.router.add_get('/api/socialfi/{domain}', node.rpc_socialfi_domain)
    app.router.add_get('/api/text/key', node.rpc_text_key)
    app.router.add_get('/api/reputation/{addr}', node.rpc_reputation)
    app.router.add_get('/api/graph/recommend/{addr}', node.rpc_graph_recommend)
    app.router.add_get('/api/ai/services', node.rpc_ai_services)
    app.router.add_get('/api/ai/works', node.rpc_ai_works)
    app.router.add_get('/api/ai/fund', node.rpc_ai_fund)
    app.router.add_get('/api/ai/status', node.rpc_ai_status)
    app.router.add_get('/api/ai', node.rpc_ai_list)
    app.router.add_get('/api/ai/{addr}', node.rpc_ai_view)
    # ---------- 社区仲裁 ----------
    app.router.add_get('/api/arb/summary', node.rpc_arb_summary)
    app.router.add_get('/api/arb/arbitrators', node.rpc_arb_arbitrators)
    app.router.add_get('/api/arb/candidates', node.rpc_arb_candidates)
    app.router.add_get('/api/arb/cases', node.rpc_arb_cases)
    app.router.add_get('/api/arb/cases/{case_id}', node.rpc_arb_case)
    app.router.add_get('/api/arb/user/{addr}', node.rpc_arb_user)
    app.router.add_get('/api/arb/panel/{addr}', node.rpc_arb_panel)
    # ---------- 预言机 / 跨链桥 / DEX / 治理 / DID / 订阅 ----------
    # ---------- 链浏览器 / 索引器 ----------
    app.router.add_get('/api/chain/sync', node.rpc_chain_sync)
    app.router.add_get('/api/chain/block/{height}', node.rpc_chain_block)
    app.router.add_get('/api/chain/search', node.rpc_chain_search)
    app.router.add_get('/api/chain/stats', node.rpc_chain_stats)
    app.router.add_post('/api/oracle/op', node.rpc_oracle_op)
    app.router.add_get('/api/oracle/summary', node.rpc_oracle_summary)
    app.router.add_get('/api/oracle/price/{feed}', node.rpc_oracle_price)
    app.router.add_get('/api/oracle/vrf/{request_id}', node.rpc_oracle_vrf)
    app.router.add_get('/api/oracle/nodes', node.rpc_oracle_nodes)
    app.router.add_get('/api/oracle/ai/{content_hash}', node.rpc_oracle_ai)
    app.router.add_post('/api/bridge/op', node.rpc_bridge_op)
    app.router.add_get('/api/bridge/summary', node.rpc_bridge_summary)
    app.router.add_get('/api/bridge/asset/{symbol}', node.rpc_bridge_asset)
    app.router.add_get('/api/bridge/deposits', node.rpc_bridge_deposits)
    app.router.add_get('/api/bridge/withdrawals', node.rpc_bridge_withdrawals)
    app.router.add_post('/api/dex/op', node.rpc_dex_op)
    app.router.add_get('/api/dex/summary', node.rpc_dex_summary)
    app.router.add_get('/api/dex/quote', node.rpc_dex_quote)
    app.router.add_get('/api/dex/split', node.rpc_dex_split)
    app.router.add_get('/api/dex/lp/{addr}', node.rpc_dex_lp)
    app.router.add_get('/api/dex/farm/{pair}/{addr}', node.rpc_dex_farm)
    app.router.add_post('/api/gov/op', node.rpc_gov_op)
    app.router.add_get('/api/gov/summary', node.rpc_gov_summary)
    app.router.add_get('/api/gov/proposals', node.rpc_gov_proposals)
    app.router.add_get('/api/gov/proposals/{pid}', node.rpc_gov_proposal)
    app.router.add_get('/api/gov/power/{addr}', node.rpc_gov_power)
    app.router.add_post('/api/did/op', node.rpc_did_op)
    app.router.add_get('/api/did/summary', node.rpc_did_summary)
    app.router.add_get('/api/did/{addr}', node.rpc_did_profile)
    app.router.add_get('/api/did/reputation/{addr}', node.rpc_did_reputation)
    app.router.add_post('/api/sub/op', node.rpc_sub_op)
    app.router.add_get('/api/sub/summary', node.rpc_sub_summary)
    app.router.add_get('/api/sub/creator/{addr}', node.rpc_sub_creator)
    app.router.add_get('/api/sub/status/{user}/{creator}', node.rpc_sub_status)
    app.router.add_get('/api/faucet/status', node.rpc_faucet_status)
    app.router.add_post('/api/faucet/request', node.rpc_faucet_request)
    app.router.add_get('/api/arb/notifications/{addr}', node.rpc_arb_notifications)
    app.router.add_post('/api/arb/notifications/read', node.rpc_arb_read)
