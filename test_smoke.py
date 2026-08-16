import asyncio
import hashlib
import os
import tempfile
import time
from collections import Counter

from aiohttp import web, ClientSession

from core.crypto import (QuantumWallet, ed25519_public_key, ed25519_sign,
                         ed25519_verify, verify_quantum_tx)
from core.blockchain import Block
from core.storage import StateStore
from core.transaction import Tx
from network.rpc import setup_routes
from nova_node import NovaNode


def test_wallet_and_tx_roundtrip():
    wallet = QuantumWallet()
    tx = Tx("alice", "bob", 1, [], "hello", wallet.public_key_hex(), "")
    assert tx.txid
    restored = Tx.from_dict(tx.to_dict())
    assert restored.txid == tx.txid
    assert restored.timestamp == tx.timestamp

    sig = wallet.sign(tx.signing_data())
    assert verify_quantum_tx(tx.signing_data(), sig, wallet.public_key_hex(), wallet.address)


def test_ed25519_rfc8032_vectors():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pub = ed25519_public_key(seed)
    assert pub.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    sig = ed25519_sign(seed, b"")
    assert sig.hex() == ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                         "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert ed25519_verify(pub, b"", sig)


def test_forgery_is_rejected():
    wallet = QuantumWallet()
    msg = "hello"
    forged = "00" * 64
    assert not verify_quantum_tx(msg, forged, wallet.public_key_hex(), wallet.address)


def test_validate_tx_rejects_bad_inputs():
    node = NovaNode(host="127.0.0.1", p2p=9911, rpc=8091, use_tls=False, state_file=None)
    wallet = QuantumWallet()
    node.balances[wallet.address] = 100
    ts = int(time.time())

    def make_tx(amount, receiver="bob"):
        tx = Tx(wallet.address, receiver, amount, [], "memo", wallet.public_key_hex(), "", timestamp=ts)
        tx.signature = wallet.sign(tx.signing_data())
        return tx

    assert node.validate_tx(make_tx(-1)) is False            # 负金额
    assert node.validate_tx(make_tx(0)) is False             # 非合约地址 0 金额
    assert node.validate_tx(make_tx(float("nan"))) is False
    assert node.validate_tx(make_tx(float("inf"))) is False
    assert node.validate_tx(make_tx("1.5")) is False         # 字符串金额
    assert node.validate_tx(make_tx(1e9)) is False           # 超过总供应量
    assert node.validate_tx(make_tx(1)) is True              # 正常交易


def test_validate_tx_rejects_stale_timestamp():
    node = NovaNode(host="127.0.0.1", p2p=9912, rpc=8092, use_tls=False, state_file=None)
    wallet = QuantumWallet()
    node.balances[wallet.address] = 100
    old_ts = int(time.time()) - 10000
    tx = Tx(wallet.address, "bob", 1, [], "memo", wallet.public_key_hex(), "", timestamp=old_ts)
    tx.signature = wallet.sign(tx.signing_data())
    assert node.validate_tx(tx) is False


def test_state_persistence_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        store = StateStore("genesis.json")
        store.balances["0xabc"] = 42.5
        store.dag.add("tx1")
        store.stakes["0xabc"] = 100
        store.unbonding["0xabc"] = (50, 1234567890.0)
        store.light_checkin_dates["0xabc"] = {"2026-08-01"}
        store.save(path)

        store2 = StateStore("genesis.json")
        assert store2.load(path)
        assert store2.balances.get("0xabc") == 42.5
        assert "tx1" in store2.dag
        assert store2.stakes.get("0xabc") == 100
        assert store2.unbonding["0xabc"] == (50, 1234567890.0)
        assert store2.light_checkin_dates["0xabc"] == {"2026-08-01"}


def test_canonical_amount_matches_frontend():
    from core.transaction import canonical_amount
    assert canonical_amount(1.5) == "1.5"
    assert canonical_amount(1) == "1"
    assert canonical_amount(0.000001) == "0.000001"
    assert canonical_amount(12150000) == "12150000"


async def test_rpc_send_e2e():
    with tempfile.TemporaryDirectory() as d:
        state_path = os.path.join(d, "node_state.json")
        node = NovaNode(host="127.0.0.1", p2p=9913, rpc=8093, use_tls=False, state_file=state_path)
        wallet = QuantumWallet()
        node.balances[wallet.address] = 100

        app = web.Application(client_max_size=262144)
        setup_routes(app, node)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}"

        sender = wallet.address
        to = "0x" + "ab" * 20
        ts = int(time.time())
        memo = "memo"
        pub = wallet.public_key_hex()
        amt = 1.5
        sig = wallet.sign(f"{sender}{to}1.5{ts}[]memo{pub}")

        async with ClientSession() as sess:
            # 正常交易
            async with sess.post(f"{base}/api/send", json={
                "sender": sender, "receiver": to, "amount": amt, "timestamp": ts,
                "parents": [], "data": memo, "sender_public_key": pub, "signature": sig,
            }) as r:
                assert r.status == 200, await r.text()
                body = await r.json()
            assert body["txid"] in node.dag
            assert node.balances[to] == 1.5

            # 负金额拒绝
            bad_sig = wallet.sign(f"{sender}{to}-1.5{ts}[]memo{pub}")
            async with sess.post(f"{base}/api/send", json={
                "sender": sender, "receiver": to, "amount": -1.5, "timestamp": ts,
                "parents": [], "data": memo, "sender_public_key": pub, "signature": bad_sig,
            }) as r:
                assert r.status == 400, await r.text()

            # 伪造签名拒绝
            fake_sig = wallet.sign("forged message")
            async with sess.post(f"{base}/api/send", json={
                "sender": sender, "receiver": to, "amount": 1, "timestamp": ts,
                "parents": [], "data": memo, "sender_public_key": pub, "signature": fake_sig,
            }) as r:
                assert r.status == 400, await r.text()

            # 缺失时间戳拒绝
            async with sess.post(f"{base}/api/send", json={
                "sender": sender, "receiver": to, "amount": 1, "parents": [],
                "data": memo, "sender_public_key": pub, "signature": sig,
            }) as r:
                assert r.status == 400, await r.text()

            # 轻节点验证奖励：同一交易只能领一次，同一地址每日一次
            node.balances[node.economy.VALIDATOR_POOL] = 100
            txid = body["txid"]
            async with sess.post(f"{base}/api/light/verify", json={"addr": sender, "txid": txid}) as r:
                assert r.status == 200, await r.text()
            async with sess.post(f"{base}/api/light/verify", json={"addr": sender, "txid": txid}) as r:
                assert r.status == 400, await r.text()
            async with sess.post(f"{base}/api/light/verify", json={"addr": sender, "txid": "another"}) as r:
                assert r.status == 400, await r.text()

            # 解锁接口（无锁仓则返回 0）
            async with sess.post(f"{base}/api/unlock", json={"addr": sender}) as r:
                unlock = await r.json()
            assert "unlocked" in unlock

            # 非法 JSON 返回 400 而非 500
            async with sess.post(f"{base}/api/send", data=b"not json") as r:
                assert r.status == 400, await r.text()

            # 状态接口
            async with sess.get(f"{base}/api/status") as r:
                st = await r.json()
            assert st["quantum_safe"] is False
            assert st["algorithm"] == "Ed25519"

        node.save_state()

        # 重启后状态恢复（余额、DAG、重放保护）
        node2 = NovaNode(host="127.0.0.1", p2p=9914, rpc=8094, use_tls=False, state_file=state_path)
        assert node2.balances[to] == 1.5
        assert body["txid"] in node2.dag
        assert node2.security.is_replay(body["txid"])

        await runner.cleanup()




def test_consensus_blocks():
    node = NovaNode(host="127.0.0.1", p2p=9921, rpc=8091, use_tls=False, state_file=None)
    node.store.dag.update(["tx1", "tx2"])
    b1 = node.consensus.produce_block()
    assert b1 is not None and b1.height == 0
    assert b1.prev_hash == "0" * 64
    assert set(b1.txids) == {"tx1", "tx2"}
    assert node.consensus.produce_block() is None          # 无新交易不再出块
    node.store.dag.add("tx3")
    b2 = node.consensus.produce_block()
    assert b2.height == 1 and b2.prev_hash == b1.hash
    assert set(b2.txids) == {"tx3"}

    node2 = NovaNode(host="127.0.0.1", p2p=9922, rpc=8092, use_tls=False, state_file=None)
    bad = Block(height=1, txids=["tx9"], prev_hash="0" * 64)
    assert node2.consensus.adopt_block(bad) is False       # prev_hash 不匹配
    assert node2.consensus.adopt_block(b1) is True
    assert node2.consensus.adopt_block(b2) is True
    assert node2.consensus.chain_height() == 2
    assert node2.consensus.adopt_block(b2) is False        # 高度已存在


def test_check_unlock():
    node = NovaNode(host="127.0.0.1", p2p=9923, rpc=8093, use_tls=False, state_file=None)
    addr = "0xunlock"
    node.store.locked_balances[addr] = {
        "amount": 100,
        "start_time": time.time() - (3 * 366 + 31) * 86400,  # 已过锁定 3 年 + 1 个月
        "unlocked": 0,
    }
    unlocked = node.economy.check_unlock(addr)
    assert unlocked == 10.0
    assert node.balances[addr] == unlocked
    assert node.economy.check_unlock(addr) == 0            # 不会重复解锁


def test_release_early_rewards_once():
    node = NovaNode(host="127.0.0.1", p2p=9924, rpc=8094, use_tls=False, state_file=None)
    node.economy.RELEASE_TIME = time.time() - 1
    node.store.miner_qualified.add("0xminer")
    node.balances[node.economy.ECOSYSTEM_FUND] = 100000
    node.economy.release_early_rewards()
    assert node.balances["0xminer"] == node.economy.EARLY_MINER_REWARD
    before = node.balances["0xminer"]
    node.economy.release_early_rewards()
    assert node.balances["0xminer"] == before              # 不重复发放


def test_state_sync_snapshot():
    node1 = NovaNode(host="127.0.0.1", p2p=9925, rpc=8095, use_tls=False, state_file=None)
    node2 = NovaNode(host="127.0.0.1", p2p=9926, rpc=8096, use_tls=False, state_file=None)
    node1.balances["0xalice"] = 77
    node1.store.dag.add("tx_sync")
    node1.consensus.produce_block()
    snap = node1.full_snapshot()
    assert node2.apply_snapshot(snap)
    assert node2.balances["0xalice"] == 77
    assert "tx_sync" in node2.dag
    assert node2.consensus.chain_height() == 1
    assert node2.consensus.latest_checkpoint() == node1.consensus.latest_checkpoint()


def test_consensus_persisted():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        node = NovaNode(host="127.0.0.1", p2p=9927, rpc=8097, use_tls=False, state_file=path)
        node.store.dag.add("txp1")
        node.consensus.produce_block()
        node.save_state()

        node2 = NovaNode(host="127.0.0.1", p2p=9928, rpc=8098, use_tls=False, state_file=path)
        assert node2.consensus.chain_height() == 1
        assert node2.consensus.latest_checkpoint() == node.consensus.latest_checkpoint()
        assert "txp1" in node2.store.dag


def test_daily_maintenance_accrues_uptime():
    node = NovaNode(host="127.0.0.1", p2p=9929, rpc=8099, use_tls=False, state_file=None)
    node.store.miner_registry["0xminer2"] = time.time()
    node._last_maintenance = time.time() - 86400
    node._run_daily_maintenance()
    assert node.store.miner_uptime["0xminer2"] == 86400
    node.store.miner_uptime["0xminer2"] = 270 * 86400 - 1
    node._last_maintenance = time.time() - 86400
    node._run_daily_maintenance()
    assert "0xminer2" in node.store.miner_qualified


def test_pos_election_deterministic():
    va, vb = QuantumWallet(), QuantumWallet()
    node1 = NovaNode(host="127.0.0.1", p2p=9940, rpc=8195, use_tls=False, state_file=None, consensus_mode="pos")
    node2 = NovaNode(host="127.0.0.1", p2p=9941, rpc=8196, use_tls=False, state_file=None, consensus_mode="pos")
    for n in (node1, node2):
        n.store.stakes[va.address] = 1000
        n.store.stakes[vb.address] = 3000
        n.consensus._refresh_epoch_stakes()
    for h in range(30):
        prev = hashlib.sha3_256(str(h).encode()).hexdigest()
        assert node1.consensus.elect_proposer(h, prev) == node2.consensus.elect_proposer(h, prev)
    counts = Counter()
    for h in range(400):
        counts[node1.consensus.elect_proposer(h, f"seed{h}")] += 1
    assert counts[vb.address] > counts[va.address]          # 高质押权重更高
    assert counts[vb.address] + counts[va.address] == 400


def test_pos_bootstrap_no_stakes():
    wa = QuantumWallet()
    node_a = NovaNode(host="127.0.0.1", p2p=9942, rpc=8197, use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=wa.private_key_hex())
    node_b = NovaNode(host="127.0.0.1", p2p=9943, rpc=8198, use_tls=False, state_file=None, consensus_mode="pos")
    node_a.store.dag.add("t_boot")
    b = node_a.consensus.produce_block()
    assert b is not None and b.proposer == wa.address and b.signature
    assert node_b.consensus.adopt_block(b)                  # 无质押 bootstrap：签名有效即可
    bad = Block(height=1, txids=["t_x"], prev_hash=b.hash, proposer=wa.address,
                proposer_pubkey=wa.public_key_hex())
    assert node_b.consensus.adopt_block(bad) is False       # 无签名仍被拒


def test_pos_block_signing_and_adoption():
    va, vb = QuantumWallet(), QuantumWallet()
    node_a = NovaNode(host="127.0.0.1", p2p=9944, rpc=8199, use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=va.private_key_hex())
    node_b = NovaNode(host="127.0.0.1", p2p=9945, rpc=8200, use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=vb.private_key_hex())
    for n in (node_a, node_b):
        n.store.stakes[va.address] = 500
        n.store.stakes[vb.address] = 500
        n.store.dag.add("t1")
        n.consensus._refresh_epoch_stakes()
    b0 = node_a.consensus.produce_block()
    if b0 is None:                                          # 当选者是 B
        b0 = node_b.consensus.produce_block()
        assert b0 is not None and b0.proposer == vb.address
        assert node_a.consensus.adopt_block(b0)
    else:
        assert b0.proposer == va.address and b0.signature
        assert node_b.consensus.adopt_block(b0)
    assert node_a.consensus.chain_height() == 1 and node_b.consensus.chain_height() == 1

    no_sig = Block(height=1, txids=["t2"], prev_hash=b0.hash, proposer=va.address,
                   proposer_pubkey=va.public_key_hex())
    assert node_b.consensus.adopt_block(no_sig) is False    # 无签名
    forged = Block(height=1, txids=["t2"], prev_hash=b0.hash, proposer=va.address,
                   proposer_pubkey=va.public_key_hex(), signature="00" * 64)
    assert node_b.consensus.adopt_block(forged) is False    # 伪造签名
    elected1 = node_a.consensus.elect_proposer(1, b0.hash)
    non_elected = vb if elected1 == va.address else va
    nb = Block(height=1, txids=["t2"], prev_hash=b0.hash, proposer=non_elected.address,
               proposer_pubkey=non_elected.public_key_hex())
    nb.signature = non_elected.sign(nb.hash)
    assert node_a.consensus.adopt_block(nb) is False        # 非当选且未超时


def test_pos_fallback_after_timeout():
    va, vb = QuantumWallet(), QuantumWallet()
    node_a = NovaNode(host="127.0.0.1", p2p=9946, rpc=8201, use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=va.private_key_hex())
    node_b = NovaNode(host="127.0.0.1", p2p=9947, rpc=8202, use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=vb.private_key_hex())
    for n in (node_a, node_b):
        n.store.stakes[va.address] = 10000
        n.store.stakes[vb.address] = 1
        n.store.dag.add("t1")
        n.consensus._refresh_epoch_stakes()
    elected0 = node_a.consensus.elect_proposer(0, Block.GENESIS_PREV)
    p0 = va if elected0 == va.address else vb
    b0 = Block(height=0, txids=["t0"], prev_hash=Block.GENESIS_PREV, proposer=p0.address,
               proposer_pubkey=p0.public_key_hex(), timestamp=time.time() - 300)
    b0.signature = p0.sign(b0.hash)
    assert node_a.consensus.adopt_block(b0)
    assert node_b.consensus.adopt_block(b0)
    wc = QuantumWallet()                                     # 无质押
    bad = Block(height=1, txids=["t_bad"], prev_hash=b0.hash, proposer=wc.address,
                proposer_pubkey=wc.public_key_hex(), timestamp=time.time())
    bad.signature = wc.sign(bad.hash)
    assert node_a.consensus.adopt_block(bad) is False        # 非质押者不能补块
    b1 = node_b.consensus.produce_block()                    # 当选者超时，质押者补块
    assert b1 is not None and b1.proposer == vb.address and b1.signature
    assert node_a.consensus.adopt_block(b1)


def test_pos_stake_tx_roundtrip():
    node1 = NovaNode(host="127.0.0.1", p2p=9948, rpc=8203, use_tls=False, state_file=None)
    node2 = NovaNode(host="127.0.0.1", p2p=9949, rpc=8204, use_tls=False, state_file=None)
    w = QuantumWallet()
    for n in (node1, node2):
        n.balances[w.address] = 1000
    ts = int(time.time())
    tx = Tx(w.address, w.address, 200, [], "nova:stake", w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    assert node1.validate_tx(tx)
    node1.apply_tx(tx)
    node2.apply_tx(tx)                                       # 同交易两节点一致
    assert node1.store.stakes[w.address] == node2.store.stakes[w.address] == 200
    assert node1.balances[w.address] == node2.balances[w.address] == 1000 - 200 - node1.economy.FIXED_GAS

    # 部分解押：200 质押中解出 50（= 25% 上限），必须指定金额
    tx_un0 = Tx(w.address, w.address, 0, [], "nova:unstake", w.public_key_hex(), "", timestamp=ts)
    tx_un0.signature = w.sign(tx_un0.signing_data())
    assert node1.validate_tx(tx_un0) is False                # 金额必须 > 0
    tx_un = Tx(w.address, w.address, 50, [], "nova:unstake", w.public_key_hex(), "", timestamp=ts)
    tx_un.signature = w.sign(tx_un.signing_data())
    assert node1.validate_tx(tx_un)
    node1.apply_tx(tx_un)
    node2.apply_tx(tx_un)
    assert node1.store.stakes[w.address] == 150
    assert node1.store.unbonding[w.address][0] == 50
    # 超过解押上限（冷却中 50 > 25% x 150 = 37.5）→ 拒绝
    tx_un2 = Tx(w.address, w.address, 1, [], "nova:unstake", w.public_key_hex(), "", timestamp=ts)
    tx_un2.signature = w.sign(tx_un2.signing_data())
    assert node1.validate_tx(tx_un2) is False

    tx_cl = Tx(w.address, w.address, 0, [], "nova:claim", w.public_key_hex(), "", timestamp=ts)
    tx_cl.signature = w.sign(tx_cl.signing_data())
    assert node1.validate_tx(tx_cl) is False                 # 冷静期内不能领
    node1.store.unbonding[w.address] = (50, time.time() - 1)
    node2.store.unbonding[w.address] = (50, time.time() - 1)
    tx_cl2 = Tx(w.address, w.address, 0, [], "nova:claim", w.public_key_hex(), "", timestamp=int(time.time()))
    tx_cl2.signature = w.sign(tx_cl2.signing_data())
    assert node1.validate_tx(tx_cl2)
    node1.apply_tx(tx_cl2)
    node2.apply_tx(tx_cl2)
    assert w.address not in node1.store.unbonding
    assert node1.balances[w.address] == node2.balances[w.address]


def test_pos_stake_caps():
    node = NovaNode(host="127.0.0.1", p2p=9950, rpc=8205, use_tls=False, state_file=None)
    w, w2 = QuantumWallet(), QuantumWallet()
    for a in (w.address, w2.address):
        node.balances[a] = 100000

    def stake_tx(addr, amt, wallet):
        tx = Tx(addr, addr, amt, [], "nova:stake", wallet.public_key_hex(), "", timestamp=int(time.time()))
        tx.signature = wallet.sign(tx.signing_data())
        return tx

    tx1 = stake_tx(w.address, 9000, w)
    assert node.validate_tx(tx1)
    node.apply_tx(tx1)
    assert node.validate_tx(stake_tx(w.address, 1000, w))      # 9000+1000=10000 恰好
    assert node.validate_tx(stake_tx(w.address, 1001, w)) is False  # 单地址上限 10000
    node.economy.MAX_TOTAL_STAKE = 12000                       # 收窄全网上限便于测试
    assert node.validate_tx(stake_tx(w2.address, 2000, w2))    # 10000+2000=12000 恰好
    assert node.validate_tx(stake_tx(w2.address, 1, w2)) is False  # 超过全网上限


def test_pos_inactivity_slash():
    va, vb = QuantumWallet(), QuantumWallet()
    node_a = NovaNode(host="127.0.0.1", p2p=9951, rpc=8206, use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=va.private_key_hex())
    node_b = NovaNode(host="127.0.0.1", p2p=9952, rpc=8207, use_tls=False, state_file=None,
                      consensus_mode="pos", validator_key=vb.private_key_hex())
    for n in (node_a, node_b):
        n.store.stakes[va.address] = 10000
        n.store.stakes[vb.address] = 1
        n.consensus._refresh_epoch_stakes()
    # 当选者出块后“离线”5 分钟
    elected0 = node_a.consensus.elect_proposer(0, Block.GENESIS_PREV)
    p0 = va if elected0 == va.address else vb
    b0 = Block(height=0, txids=["t0"], prev_hash=Block.GENESIS_PREV, proposer=p0.address,
               proposer_pubkey=p0.public_key_hex(), timestamp=time.time() - 300)
    b0.signature = p0.sign(b0.hash)
    assert node_a.consensus.adopt_block(b0)
    assert node_b.consensus.adopt_block(b0)
    # 其他质押者补块 → 当选者连续错过多个窗口后（H-03）才被惩罚 1% 并禁用出块权
    elected1 = node_a.consensus.elect_proposer(1, b0.hash)
    fb = vb if elected1 == va.address else va
    node_a.store.pos_missed[elected1] = 2  # 已连续错过 2 个窗口，本次回退达到阈值
    b1 = Block(height=1, txids=["t1"], prev_hash=b0.hash, proposer=fb.address,
               proposer_pubkey=fb.public_key_hex(), timestamp=time.time())
    b1.signature = fb.sign(b1.hash)
    assert node_a.consensus.adopt_block(b1)
    if elected1 == va.address:
        assert node_a.store.stakes[va.address] == 9900         # 10000 - 1%
    else:
        assert node_a.store.stakes.get(vb.address, 0) == 0     # 质押 1 NOVA 全部扣罚
    assert node_a.store.jailed.get(elected1, 0) > node_a.consensus.chain_height()
    # 被禁用的地址在下一 epoch 边界重建快照时被排除，jail 到期后恢复
    next_epoch = ((node_a.consensus.chain_height() // node_a.consensus.epoch_len) + 1) * node_a.consensus.epoch_len
    node_a.consensus._refresh_epoch_stakes(next_epoch)
    assert elected1 not in node_a.consensus.epoch_stakes
    if elected1 == va.address:
        node_a.consensus._refresh_epoch_stakes(next_epoch + node_a.consensus.epoch_len)
        assert elected1 in node_a.consensus.epoch_stakes


def test_pos_equivocation_slash():
    va = QuantumWallet()
    node = NovaNode(host="127.0.0.1", p2p=9953, rpc=8208, use_tls=False, state_file=None,
                    consensus_mode="pos", validator_key=va.private_key_hex())
    node.store.stakes[va.address] = 1000
    node.consensus._refresh_epoch_stakes()
    b0 = Block(height=0, txids=["t0"], prev_hash=Block.GENESIS_PREV, proposer=va.address,
               proposer_pubkey=va.public_key_hex())
    b0.signature = va.sign(b0.hash)
    assert node.consensus.adopt_block(b0)
    # 同一出块者对同一高度签署不同区块 → 双签惩罚 5%
    b0x = Block(height=0, txids=["t0x"], prev_hash=Block.GENESIS_PREV, proposer=va.address,
                proposer_pubkey=va.public_key_hex())
    b0x.signature = va.sign(b0x.hash)
    assert node.consensus.adopt_block(b0x) is False
    assert node.store.stakes[va.address] == 950
    assert node.store.jailed.get(va.address, 0) > 0
    # 不同出块者的冲突区块不构成双签（不惩罚）
    w2 = QuantumWallet()
    node.store.stakes[va.address] = 1000
    node.store.stakes[w2.address] = 500
    node.store.jailed = {}
    node.consensus._refresh_epoch_stakes()
    b0y = Block(height=0, txids=["t0y"], prev_hash=Block.GENESIS_PREV, proposer=w2.address,
                proposer_pubkey=w2.public_key_hex())
    b0y.signature = w2.sign(b0y.hash)
    assert node.consensus.adopt_block(b0y) is False
    assert node.store.stakes[w2.address] == 500                # 未受惩罚
    assert node.store.jailed == {}


def run():
    test_wallet_and_tx_roundtrip()
    test_ed25519_rfc8032_vectors()
    test_forgery_is_rejected()
    test_validate_tx_rejects_bad_inputs()
    test_validate_tx_rejects_stale_timestamp()
    test_state_persistence_roundtrip()
    test_canonical_amount_matches_frontend()
    test_consensus_blocks()
    test_check_unlock()
    test_release_early_rewards_once()
    test_state_sync_snapshot()
    test_consensus_persisted()
    test_daily_maintenance_accrues_uptime()
    test_pos_election_deterministic()
    test_pos_bootstrap_no_stakes()
    test_pos_block_signing_and_adoption()
    test_pos_fallback_after_timeout()
    test_pos_stake_tx_roundtrip()
    test_pos_stake_caps()
    test_pos_inactivity_slash()
    test_pos_equivocation_slash()
    asyncio.run(test_rpc_send_e2e())
    print("smoke-test: ok")


if __name__ == "__main__":
    run()
