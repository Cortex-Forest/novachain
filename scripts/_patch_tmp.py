import io

# --- nova_node.py /api/status 统计 ---
p = r"C:\Users\Administrator\novachain\nova_node.py"
s = io.open(p, encoding="utf-8").read()
old = '            "storage_providers":len(self.store.storage_providers),\n            "pins":len(self.store.storage_claims),'
new = old + '\n            "storage_nodes":len(self.store.inc_nodes),\n            "storage_files":len(self.store.inc_files),'
assert old in s
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8").write(s)
print("nova_node status ok")

# --- network/rpc.py 路由 ---
p2 = r"C:\Users\Administrator\novachain\network\rpc.py"
s2 = io.open(p2, encoding="utf-8").read()
anchor = "    app.router.add_get('/api/storage/orders', node.rpc_storage_orders)"
routes = anchor + """
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
    app.router.add_get('/api/storage/inc/summary', node.rpc_storage_inc_summary)"""
assert anchor in s2
s2 = s2.replace(anchor, routes)
io.open(p2, "w", encoding="utf-8").write(s2)
print("rpc.py routes ok")
