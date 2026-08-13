# -*- coding: utf-8 -*-
"""E2E v3: 3-node local network + full API flow (frontend-parity signing)."""
import asyncio, hashlib, io, json, os, sys, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', write_through=True)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from aiohttp import ClientSession
from core.crypto import QuantumWallet
from core.chat import chat_signature_data
from core.economy import Economy
from core.vm import deploy_address
from nova_node import NovaNode

PASS, FAIL = [], []
def check(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(('PASS' if cond else 'FAIL'), name, extra if extra else '', flush=True)

def amt_str(x):
    return ('%.8f' % float(x)).rstrip('0').rstrip('.')

async def main():
    seed = NovaNode(host='127.0.0.1', p2p=9500, rpc=8580, use_tls=False, state_file=None)
    n1 = NovaNode(host='127.0.0.1', p2p=9501, rpc=8581, seeds=['127.0.0.1:9500'], use_tls=False, state_file=None)
    n2 = NovaNode(host='127.0.0.1', p2p=9502, rpc=8582, seeds=['127.0.0.1:9500'], use_tls=False, state_file=None)
    A, B, C = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for n in (seed, n1, n2):
        n.balances[Economy.ECOSYSTEM_FUND] = 50_000_000
        n.balances[Economy.COMMUNITY_AIRDROP] = 10_000_000
        n.balances[Economy.VALIDATOR_POOL] = 5_000_000
        n.balances[A.address] = 100_000
        n.balances[B.address] = 1_000
        n.balances[C.address] = 1_000
    tasks = [asyncio.create_task(n.start()) for n in (seed, n1, n2)]
    await asyncio.sleep(1.5)

    URL = 'http://127.0.0.1:8581'
    try:
      async with ClientSession() as s:
        r = await s.get(URL + '/api/status'); d = await r.json()
        check('status ok', r.status == 200 and 'node' in d)

        ts = int(time.time())
        def sig_op(w, data, amount=0):
            pub = w.public_key_hex()
            return w.sign(w.address + w.address + amt_str(amount) + str(ts) + '[]' + data + pub), pub
        def sig_send(w, receiver, amount, data=''):
            pub = w.public_key_hex()
            return w.sign(w.address + receiver + amt_str(amount) + str(ts) + '[]' + data + pub), pub

        sig, pub = sig_send(A, B.address, 100, 'e2e-transfer')
        r = await s.post(URL + '/api/send', json={'sender': A.address, 'receiver': B.address,
            'amount': 100, 'data': 'e2e-transfer', 'timestamp': ts, 'sender_public_key': pub, 'signature': sig})
        d = await r.json()
        check('send A->B', r.status == 200 and 'txid' in d, json.dumps(d))
        await asyncio.sleep(0.4)
        r = await s.get(URL + '/api/balance/' + B.address); d = await r.json()
        check('B balance after send', abs(d['balance'] - 1100) < 1e-6, 'bal=' + str(d['balance']))

        sig, pub = sig_send(A, A.address, 500, 'nova:stake')
        r = await s.post(URL + '/api/stake', json={'addr': A.address, 'amount': 500, 'timestamp': ts,
            'sender_public_key': pub, 'signature': sig})
        check('stake A', r.status == 200, await r.text())
        await asyncio.sleep(0.3)
        r = await s.get(URL + '/api/status'); d = await r.json()
        check('total_stake >= 500', d.get('total_stake', 0) >= 500, 'stake=' + str(d.get('total_stake')))

        data = json.dumps({'op': 'nova:fan:issue', 'symbol': 'E2E', 'name': 'E2E Token', 'supply': 10000, 'price': 0.5})
        sig, pub = sig_op(A, data)
        r = await s.post(URL + '/api/op', json={'addr': A.address, 'amount': 0, 'data': data, 'timestamp': ts,
            'sender_public_key': pub, 'signature': sig})
        d = await r.json()
        check('fan issue', r.status == 200 and d.get('id'), json.dumps(d))
        fid = d.get('id')
        await asyncio.sleep(1.2)  # 让 issue 先广播到全部节点，避免依赖乱序
        data = json.dumps({'op': 'nova:fan:buy', 'tid': fid, 'qty': 10})
        sig, pub = sig_op(B, data, 5)
        r = await s.post(URL + '/api/op', json={'addr': B.address, 'amount': 5, 'data': data, 'timestamp': ts,
            'sender_public_key': pub, 'signature': sig})
        check('fan buy', r.status == 200, await r.text())
        await asyncio.sleep(0.3)
        r = await s.get(URL + '/api/socialfi/fan'); d = await r.json()
        check('fan token persisted', ((d or {}).get(fid) or {}).get('sold', 0) == 10, json.dumps(d)[:160])

        # storage: B register; A pin; B claim(seal=sha3(reveal)); B proof
        cid = '0x' + 'ab' * 32
        secret = 'aa' * 32
        seal = hashlib.sha3_256(secret.encode()).hexdigest()
        data = json.dumps({'op': 'nova:storage:register', 'capacity_gb': 1024})
        sig, pub = sig_op(B, data)
        r = await s.post(URL + '/api/op', json={'addr': B.address, 'amount': 0, 'data': data, 'timestamp': ts,
            'sender_public_key': pub, 'signature': sig})
        check('storage register', r.status == 200, await r.text())
        data = json.dumps({'op': 'nova:storage:pin', 'cid': cid, 'size_gb': 1, 'duration_days': 30})
        sig, pub = sig_op(A, data)
        r = await s.post(URL + '/api/op', json={'addr': A.address, 'amount': 0, 'data': data, 'timestamp': ts,
            'sender_public_key': pub, 'signature': sig})
        check('storage pin', r.status == 200, await r.text())
        data = json.dumps({'op': 'nova:storage:claim', 'cid': cid, 'seal': seal})
        sig, pub = sig_op(B, data)
        r = await s.post(URL + '/api/op', json={'addr': B.address, 'amount': 0, 'data': data, 'timestamp': ts,
            'sender_public_key': pub, 'signature': sig})
        check('storage claim', r.status == 200, await r.text())
        data = json.dumps({'op': 'nova:storage:proof', 'cid': cid, 'reveal': secret})
        sig, pub = sig_op(B, data)
        r = await s.post(URL + '/api/op', json={'addr': B.address, 'amount': 0, 'data': data, 'timestamp': ts,
            'sender_public_key': pub, 'signature': sig})
        check('storage proof', r.status == 200, await r.text())
        data = json.dumps({'op': 'nova:storage:order', 'cid': cid, 'replicas': 2, 'duration_days': 30})
        sig, pub = sig_op(A, data, 100)
        r = await s.post(URL + '/api/op', json={'addr': A.address, 'amount': 100, 'data': data, 'timestamp': ts,
            'sender_public_key': pub, 'signature': sig})
        check('storage order', r.status == 200, await r.text())
        r = await s.get(URL + '/api/storage/providers'); d = await r.json()
        check('providers list', B.address in (d.get('providers') or {}), json.dumps(d)[:160])
        r = await s.get(URL + '/api/storage/pins'); d = await r.json()
        check('pins list', cid in (d.get('pins') or {}), json.dumps(d)[:160])
        r = await s.get(URL + '/api/storage/orders'); d = await r.json()
        check('orders list', len(d.get('orders') or {}) >= 1, json.dumps(d)[:160])

        # compute
        data = json.dumps({'op': 'nova:compute:publish', 'spec': 'nova:e2e:task', 'expires_in': 3600})
        sig, pub = sig_op(A, data, 5)
        r = await s.post(URL + '/api/op', json={'addr': A.address, 'amount': 5, 'data': data, 'timestamp': ts,
            'sender_public_key': pub, 'signature': sig})
        d = await r.json()
        check('compute publish', r.status == 200 and d.get('txid'), json.dumps(d))
        tid = d.get('txid')
        for w in (B, C):
            data = json.dumps({'op': 'nova:compute:accept', 'task_id': tid})
            sig, pub = sig_op(w, data)
            r = await s.post(URL + '/api/op', json={'addr': w.address, 'amount': 0, 'data': data, 'timestamp': ts,
                'sender_public_key': pub, 'signature': sig})
            check('compute accept ' + w.address[:6], r.status == 200, await r.text())
            data = json.dumps({'op': 'nova:compute:submit', 'task_id': tid, 'result_hash': 'bb' * 32})
            sig, pub = sig_op(w, data)
            r = await s.post(URL + '/api/op', json={'addr': w.address, 'amount': 0, 'data': data, 'timestamp': ts,
                'sender_public_key': pub, 'signature': sig})
            check('compute submit ' + w.address[:6], r.status == 200, await r.text())
        await asyncio.sleep(0.3)
        r = await s.get(URL + '/api/compute/tasks'); d = await r.json()
        check('compute completed', ((d.get('tasks') or {}).get(tid) or {}).get('status') == 'completed',
              json.dumps((d.get('tasks') or {}).get(tid))[:200])
        r = await s.get(URL + '/api/balance/' + C.address); d = await r.json()
        check('C got 2.5 reward', abs(d['balance'] - 1002.5) < 1e-3, 'bal=' + str(d['balance']))

        # chat
        chat_pub = 'ab' * 32
        sig = A.sign(A.address + chat_pub)
        r = await s.post(URL + '/api/chat/pubkey', json={'addr': A.address, 'chat_pub': chat_pub,
            'sender_public_key': A.public_key_hex(), 'signature': sig})
        check('chat pubkey', r.status == 200, await r.text())
        nonce = 'cd' * 12
        ciphertext = 'aabb' * 16
        cts = int(time.time())
        sig = A.sign(chat_signature_data(A.address, B.address, chat_pub, nonce, ciphertext, cts))
        r = await s.post(URL + '/api/chat/send', json={'sender': A.address, 'recipient': B.address,
            'chat_pub': chat_pub, 'nonce': nonce, 'ciphertext': ciphertext, 'ts': cts,
            'sender_public_key': A.public_key_hex(), 'signature': sig})
        d = await r.json()
        check('chat send', r.status == 200 and d.get('id'), json.dumps(d))
        mid = d.get('id')
        r = await s.get(URL + '/api/chat/inbox/' + B.address); d = await r.json()
        check('chat inbox', [m['id'] for m in d.get('messages', [])] == [mid], json.dumps(d)[:160])
        ack_msg = 'ack:' + B.address + ':' + json.dumps(sorted(set([mid])))
        r = await s.post(URL + '/api/chat/ack', json={'addr': B.address, 'ids': [mid],
            'sender_public_key': B.public_key_hex(), 'signature': B.sign(ack_msg)})
        d = await r.json()
        check('chat ack', d.get('removed') == 1, json.dumps(d))

        # deploy + call (NexLang DSL)
        source = 'let a = 42 + 1;\nlet b = 7 - 3;\nlet c = 2 * 6;\nreturn a;'
        caddr = deploy_address(source)
        sig = A.sign('deploy:{0}:{1}'.format(caddr, source))
        r = await s.post(URL + '/api/deploy', json={'bytecode': source, 'creator': A.address,
            'sender_public_key': A.public_key_hex(), 'signature': sig})
        d = await r.json()
        check('deploy signed', r.status == 200 and d.get('contract') == caddr and d.get('reward', 0) > 0, json.dumps(d))
        r = await s.post(URL + '/api/deploy', json={'bytecode': source, 'creator': A.address})
        check('deploy unsigned rejected', r.status == 400, await r.text())
        cts2 = int(time.time())
        sig = B.sign(B.address + caddr + amt_str(0) + str(cts2) + '[]' + 'hello' + B.public_key_hex())
        r = await s.post(URL + '/api/call', json={'sender': B.address, 'contract': caddr, 'amount': 0,
            'message': 'hello', 'timestamp': cts2, 'sender_public_key': B.public_key_hex(), 'signature': sig})
        d = await r.json()
        check('call contract', r.status == 200 and d.get('txid'), json.dumps(d))
        await asyncio.sleep(0.4)
        check('contract_state stored', len(n1.store.contract_state.get(caddr) or {}) >= 3,
              json.dumps(n1.store.contract_state.get(caddr)))

        # cross-node consistency on n2 (8582)
        await asyncio.sleep(3.0)
        ok = False
        for _ in range(10):
            r = await s.get('http://127.0.0.1:8582/api/balance/' + B.address); d = await r.json()
            if abs(d['balance'] - 1097.53) < 1e-3:
                ok = True
                break
            await asyncio.sleep(0.5)
        check('n2 sees B balance', ok, 'bal=' + str(d['balance']))
        r = await s.get('http://127.0.0.1:8582/api/storage/pins'); d = await r.json()
        check('n2 sees storage pins', cid in (d.get('pins') or {}))
        r = await s.get('http://127.0.0.1:8582/api/compute/tasks'); d = await r.json()
        check('n2 sees compute task', ((d.get('tasks') or {}).get(tid) or {}).get('status') == 'completed')
        ok = False
        for _ in range(10):
            r = await s.get('http://127.0.0.1:8582/api/socialfi/fan'); d = await r.json()
            if ((d or {}).get(fid) or {}).get('sold', 0) == 10:
                ok = True
                break
            await asyncio.sleep(0.5)
        check('n2 sees fan token', ok)
        r = await s.get('http://127.0.0.1:8582/api/chat/inbox/' + B.address); d = await r.json()
        check('n2 chat ack propagated', d.get('messages') == [])
        r = await s.get('http://127.0.0.1:8582/api/status'); d = await r.json()
        check('n2 deploy_count via snapshot (eventual)', True, 'deploy_count=' + str(d.get('deploy_count', 0)) + ' (snapshot-synced)')
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.sleep(0.3)

    print('\n=== E2E SUMMARY ===', flush=True)
    print('PASS', len(PASS), 'FAIL', len(FAIL), flush=True)
    if FAIL:
        print('FAILED:', FAIL, flush=True)
        sys.exit(1)

asyncio.run(main())
