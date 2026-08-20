"""Unit tests for network.security, network.p2p, network.rpc and node message/RPC handlers."""
import asyncio

import pytest
import json
import os
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.blockchain import Block
from core.crypto import QuantumWallet
from core.economy import Economy
from core.transaction import Tx
from core.vm import deploy_address
from network.p2p import P2PNetwork
from network.rpc import setup_routes
from network.security import SecurityManager
from nova_node import NovaNode


class FakeTime:
    def __init__(self, now):
        self._now = now

    def time(self):
        return self._now


class FakeWriter:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        pass

    def close(self):
        pass


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9990)
    kw.setdefault("rpc", 8310)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _signed_tx(wallet, receiver, amount, data="", ts=None):
    ts = int(time.time()) if ts is None else ts
    tx = Tx(wallet.address, receiver, amount, [], data, wallet.public_key_hex(), "",
            timestamp=ts)
    tx.signature = wallet.sign(tx.signing_data())
    return tx


# ---------------------------------------------------------------------------
# network.security
# ---------------------------------------------------------------------------

def test_rate_limit_blocks_after_limit(monkeypatch):
    sec = SecurityManager()
    now = 1000000.0
    monkeypatch.setattr("network.security.time", FakeTime(now))
    for _ in range(SecurityManager.RATE_LIMIT):
        assert sec.check_rate_limit("1.2.3.4") is True
    assert sec.check_rate_limit("1.2.3.4") is False
    monkeypatch.setattr("network.security.time", FakeTime(now + SecurityManager.RATE_WINDOW + 1))
    assert sec.check_rate_limit("1.2.3.4") is True  # 窗口滑动后放行


def test_validate_size_limits():
    sec = SecurityManager()
    assert sec.validate_size({"data": "x" * 100}) is True
    assert sec.validate_size({"data": "x" * (SecurityManager.MAX_CONTRACT_SIZE + 1)}) is False
    big = {"payload": "y" * (SecurityManager.MAX_TX_SIZE + 1)}
    assert sec.validate_size(big) is False


def test_replay_detection():
    sec = SecurityManager()
    assert sec.is_replay("tx1") is False
    sec.mark_processed("tx1")
    assert sec.is_replay("tx1") is True


def test_ip_limit_miner_light_once(monkeypatch):
    sec = SecurityManager()
    now = 1000000.0
    monkeypatch.setattr("network.security.time", FakeTime(now))
    sec.ip_registry["1.2.3.4"] = {"miner_0xa": now}
    assert sec.check_ip_limit("1.2.3.4", "miner") is False  # 已有矿工注册
    assert sec.check_ip_limit("1.2.3.4", "light") is True   # 不同角色不受影响
    sec.ip_registry["1.2.3.4"] = {"light_0xb": now}
    assert sec.check_ip_limit("1.2.3.4", "light") is False  # 已有轻节点注册
    sec.ip_registry["1.2.3.4"] = {"miner_0xa": now - 86401}
    assert sec.check_ip_limit("1.2.3.4", "miner") is True   # 24 小时后过期重置

def test_device_unique():
    sec = SecurityManager()
    assert sec.check_device_unique("fp1") is True
    sec.device_fingerprints["fp1"] = "0xaddr"
    assert sec.check_device_unique("fp1") is False
    assert sec.check_device_unique("fp2") is True


def test_checkin_interval(monkeypatch):
    sec = SecurityManager()
    now = 1000000.0
    monkeypatch.setattr("network.security.time", FakeTime(now))
    assert sec.check_checkin_interval("0xa") is True  # 无历史
    sec.checkin_history["0xa"] = [now - 100]
    assert sec.check_checkin_interval("0xa") is False  # 不足 20 小时
    sec.checkin_history["0xa"] = [now - 72000]
    assert sec.check_checkin_interval("0xa") is True


def test_security_snapshot_restore():
    sec = SecurityManager()
    sec.mark_processed("tx9")
    sec.ip_registry["1.2.3.4"] = {"miner_0xa": 123.0}
    sec.device_fingerprints["fp"] = "0xa"
    sec.checkin_history["0xa"] = [1.0, 2.0]
    snap = sec.snapshot()

    sec2 = SecurityManager()
    sec2.restore(snap)
    assert sec2.is_replay("tx9")
    assert sec2.ip_registry == {"1.2.3.4": {"miner_0xa": 123.0}}
    assert sec2.device_fingerprints == {"fp": "0xa"}
    assert sec2.checkin_history == {"0xa": [1.0, 2.0]}


# ---------------------------------------------------------------------------
# network.p2p
# ---------------------------------------------------------------------------

def test_ssl_contexts_disabled_without_tls():
    node = _node()
    p2p = P2PNetwork(node, "127.0.0.1", 0, False, "cert.pem", "key.pem")
    assert p2p._create_ssl_context() is None
    assert p2p._create_client_ssl_context() is None


def test_ssl_contexts_with_tls():
    if not (os.path.exists("cert.pem") and os.path.exists("key.pem")):
        return  # 无证书文件则跳过
    import ssl
    node = _node()
    p2p = P2PNetwork(node, "127.0.0.1", 0, True, "cert.pem", "key.pem")
    ctx = p2p._create_ssl_context()
    assert ctx is not None and ctx.protocol == ssl.PROTOCOL_TLS_SERVER
    cctx = p2p._create_client_ssl_context()
    assert cctx is not None and cctx.verify_mode == ssl.CERT_NONE


async def test_p2p_hello_handshake():
    node_a, node_b = _node(p2p=9991), _node(p2p=9992)
    await node_a.p2p.start_server()
    port_a = node_a.p2p.server.sockets[0].getsockname()[1]
    node_a.node_id = f"127.0.0.1:{port_a}"
    await node_b.p2p.start_server()
    port_b = node_b.p2p.server.sockets[0].getsockname()[1]
    node_b.node_id = f"127.0.0.1:{port_b}"
    await node_b.p2p.connect_to_peer(node_a.node_id)
    await asyncio.sleep(0.4)
    assert node_a.node_id in node_b.peers
    assert node_b.node_id in node_a.peers
    node_a.p2p.close_all()
    node_b.p2p.close_all()


async def test_p2p_connect_unreachable_swallowed():
    node = _node(p2p=9993)
    await node.p2p.connect_to_peer("127.0.0.1:1")
    assert node.peers == set()
    assert node.p2p.connections == set()


async def test_p2p_gossip_delivers_message():
    node_a, node_b = _node(p2p=9994), _node(p2p=9995)
    await node_a.p2p.start_server()
    port_a = node_a.p2p.server.sockets[0].getsockname()[1]
    node_a.node_id = f"127.0.0.1:{port_a}"
    await node_b.p2p.start_server()
    port_b = node_b.p2p.server.sockets[0].getsockname()[1]
    node_b.node_id = f"127.0.0.1:{port_b}"

    received = []

    async def fake_process(msg, peer, writer=None):
        received.append(msg)

    node_b.process_message = fake_process
    await node_b.p2p.connect_to_peer(node_a.node_id)
    await asyncio.sleep(0.4)
    await node_a.p2p.gossip({"type": "ping", "payload": 1})
    await asyncio.sleep(0.4)
    assert any(m.get("type") == "ping" for m in received)
    node_a.p2p.close_all()
    node_b.p2p.close_all()


async def test_p2p_peer_removed_on_disconnect():
    node_a, node_b = _node(p2p=9996), _node(p2p=9997)
    await node_a.p2p.start_server()
    port_a = node_a.p2p.server.sockets[0].getsockname()[1]
    node_a.node_id = f"127.0.0.1:{port_a}"
    await node_b.p2p.start_server()
    port_b = node_b.p2p.server.sockets[0].getsockname()[1]
    node_b.node_id = f"127.0.0.1:{port_b}"
    await node_b.p2p.connect_to_peer(node_a.node_id)
    await asyncio.sleep(0.4)
    assert node_b.node_id in node_a.peers
    node_b.p2p.close_all()  # 断开后服务端清理对等节点
    await asyncio.sleep(0.6)
    assert node_b.node_id not in node_a.peers
    node_a.p2p.close_all()


async def test_p2p_gossip_exclude_and_unreachable():
    node = _node(p2p=9998)
    node.peers.add("127.0.0.1:1")  # 不可达对等节点
    await node.p2p.gossip({"type": "ping"}, exclude=["127.0.0.1:1"])  # exclude 跳过
    await node.p2p.gossip({"type": "ping"})  # 不可达异常被吞掉
    assert node.peers == {"127.0.0.1:1"}


async def test_p2p_server_tolerates_garbage():
    node = _node(p2p=9999)
    await node.p2p.start_server()
    port = node.p2p.server.sockets[0].getsockname()[1]
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"not json at all")
    await w.drain()
    w.close()
    await asyncio.sleep(0.3)
    # 服务器仍存活，可接受合法连接
    r2, w2 = await asyncio.open_connection("127.0.0.1", port)
    w2.write(b"not json at all")
    await w2.drain()
    w2.close()
    await asyncio.sleep(0.2)
    node.p2p.close_all()


# ---------------------------------------------------------------------------
# nova_node.process_message
# ---------------------------------------------------------------------------

async def test_process_message_new_tx():
    node = _node()
    wallet = QuantumWallet()
    node.balances[wallet.address] = 100
    gossiped = []

    async def fake_gossip(msg, exclude=None):
        gossiped.append((msg, exclude))

    node.p2p.gossip = fake_gossip
    tx = _signed_tx(wallet, "0xbob", 1)
    await node.process_message({"type": "new_tx", "tx": tx.to_dict()}, "peer1")
    assert tx.txid in node.dag
    assert node.balances["0xbob"] == 1
    assert node.security.is_replay(tx.txid)
    assert gossiped and gossiped[0][1] == ["peer1"]
    # 重放不重复入账
    node.balances["0xbob"] = 0
    await node.process_message({"type": "new_tx", "tx": tx.to_dict()}, "peer1")
    assert node.balances["0xbob"] == 0


async def test_process_message_hello_requests_state():
    node = _node()
    writer = FakeWriter()
    await node.process_message({"type": "hello", "node_id": "peer9", "height": 5}, "peer9", writer)
    assert b"state_request" in b"".join(writer.sent)
    assert "peer9" in node.peers
    # 不高则不请求
    writer2 = FakeWriter()
    await node.process_message({"type": "hello", "node_id": "peer8", "height": 0}, "peer8", writer2)
    assert writer2.sent == []


async def test_process_message_state_request_responds():
    node = _node()
    writer = FakeWriter()
    await node.process_message({"type": "state_request"}, "peer", writer)
    assert writer.sent
    msg = json.loads(writer.sent[0].decode())
    assert msg["type"] == "state_snapshot"
    assert "consensus" in msg["snapshot"]


async def test_process_message_state_snapshot_applies():
    donor = _node()
    donor.store.dag.update(["t1", "t2", "t3"])
    donor.consensus.produce_block()
    donor.store.dag.add("t4")
    donor.consensus.produce_block()
    snap = donor.full_snapshot()
    assert donor.consensus.chain_height() == 2

    node = _node()
    # C-02：默认拒绝远程快照（防状态接管）
    await node.process_message({"type": "state_snapshot", "snapshot": snap}, "donor")
    assert node.consensus.chain_height() == 0
    # 显式开启种子同步后，仍拒绝非种子节点
    node.sync_from_seeds = True
    await node.process_message({"type": "state_snapshot", "snapshot": snap}, "untrusted")
    assert node.consensus.chain_height() == 0
    # 仅接受显式配置的种子节点
    node.seeds = ["donor"]
    await node.process_message({"type": "state_snapshot", "snapshot": snap}, "donor")
    assert node.consensus.chain_height() == 2
    assert node.consensus.latest_checkpoint() == donor.consensus.latest_checkpoint()


async def test_process_message_new_block_adopts():
    donor = _node()
    donor.store.dag.add("t1")
    b = donor.consensus.produce_block()
    assert b is not None
    gossiped = []

    async def fake_gossip(msg, exclude=None):
        gossiped.append(msg)

    node = _node()
    node.p2p.gossip = fake_gossip
    await node.process_message({"type": "new_block", "block": b.to_dict()}, "donor")
    assert node.consensus.chain_height() == 1
    assert gossiped and gossiped[0]["type"] == "new_block"


# ---------------------------------------------------------------------------
# network.rpc + NovaNode RPC handlers
# ---------------------------------------------------------------------------

async def _make_client(node):
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    return TestClient(TestServer(app))


async def test_rpc_cors_preflight():
    # 显式配置 "*"（本地开发/演示）时，预检与响应回显 Access-Control-Allow-Origin
    node = _node(cors_origins=["*"])
    async with await _make_client(node) as client:
        resp = await client.options("/api/status", headers={"Origin": "http://localhost:8080"})
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == "*"


async def test_rpc_cors_secure_default():
    # M-07：默认（未配置 CORS 白名单）不发 CORS 头，浏览器跨域读取被拦截
    node = _node()
    async with await _make_client(node) as client:
        resp = await client.options("/api/status", headers={"Origin": "https://evil.example"})
        assert resp.status == 204
        assert "Access-Control-Allow-Origin" not in resp.headers
        resp = await client.get("/api/status", headers={"Origin": "https://evil.example"})
        assert resp.status == 200
        assert "Access-Control-Allow-Origin" not in resp.headers


async def test_rpc_cors_allowlist_reflects_only_matching():
    # 白名单模式：仅匹配的来源被回显，未匹配的来源不带 CORS 头
    node = _node(cors_origins=["https://app.example.com"])
    async with await _make_client(node) as client:
        resp = await client.get("/api/status", headers={"Origin": "https://app.example.com"})
        assert resp.headers["Access-Control-Allow-Origin"] == "https://app.example.com"
        assert resp.headers.get("Vary") == "Origin"
        resp = await client.get("/api/status", headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in resp.headers


async def test_rpc_balance_and_status():
    node = _node()
    node.balances["0xabc"] = 42.5
    async with await _make_client(node) as client:
        resp = await client.get("/api/balance/0xabc")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"addr": "0xabc", "balance": 42.5}
        resp = await client.get("/api/status")
        st = await resp.json()
        assert st["consensus"] == "checkpoint"
        assert "height" in st and "validator" in st


async def test_rpc_deploy_and_call():
    node = _node()
    # v0.10：生态基金高于安全线（202.5 万），避免减支把部署奖励降到最低档
    node.balances[Economy.ECOSYSTEM_FUND] = 3_000_000
    creator = QuantumWallet()
    bytecode = "code123"
    async with await _make_client(node) as client:
        sig_msg = "deploy:{0}:{1}".format(deploy_address(bytecode), bytecode)
        resp = await client.post("/api/deploy", json={
            "bytecode": bytecode, "creator": creator.address,
            "sender_public_key": creator.public_key_hex(),
            "signature": creator.sign(sig_msg),
        })
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["contract"].startswith("0x")
        assert body["reward"] == Economy.INIT_DEPLOY_REWARD
        assert body["contract"] in node.contracts
        assert node.store.deploy_count == 1
        assert node.balances[creator.address] == pytest.approx(Economy.INIT_DEPLOY_REWARD)

        # 未签名部署 -> 拒绝
        resp = await client.post("/api/deploy", json={"bytecode": bytecode, "creator": creator.address})
        assert resp.status == 400

        resp = await client.post("/api/deploy", json={})
        assert resp.status == 400

        # 向合约转账（0 金额允许）
        wallet = QuantumWallet()
        node.balances[wallet.address] = 10
        ts = int(time.time())
        tx = Tx(wallet.address, body["contract"], 0, [], "hello", wallet.public_key_hex(), "", timestamp=ts)
        tx.signature = wallet.sign(tx.signing_data())
        resp = await client.post("/api/call", json={
            "sender": wallet.address, "contract": body["contract"], "amount": 0,
            "message": "hello", "timestamp": ts,
            "sender_public_key": wallet.public_key_hex(), "signature": tx.signature,
        })
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["txid"] == tx.txid

        # 非法调用签名
        resp = await client.post("/api/call", json={
            "sender": wallet.address, "contract": body["contract"], "amount": 0,
            "message": "x", "timestamp": ts,
            "sender_public_key": wallet.public_key_hex(), "signature": "00" * 64,
        })
        assert resp.status == 400


async def test_rpc_stake_unstake_claim():
    node = _node()
    wallet = QuantumWallet()
    node.balances[wallet.address] = 10000
    ts = int(time.time())
    async with await _make_client(node) as client:
        # 缺 addr
        resp = await client.post("/api/stake", json={})
        assert resp.status == 400
        # 金额非法
        resp = await client.post("/api/stake", json={"addr": wallet.address, "amount": "abc",
                                                     "sender_public_key": "", "signature": ""})
        assert resp.status == 400

        stake_tx = _signed_tx(wallet, wallet.address, 500, "nova:stake", ts)
        resp = await client.post("/api/stake", json={
            "addr": wallet.address, "amount": 500, "timestamp": ts,
            "sender_public_key": wallet.public_key_hex(), "signature": stake_tx.signature,
        })
        assert resp.status == 200, await resp.text()
        assert node.store.stakes[wallet.address] == 500

        # 解押：金额非法
        resp = await client.post("/api/unstake", json={"addr": wallet.address, "amount": 0})
        assert resp.status == 400
        unstake_tx = _signed_tx(wallet, wallet.address, 100, "nova:unstake", ts)
        resp = await client.post("/api/unstake", json={
            "addr": wallet.address, "amount": 100, "timestamp": ts,
            "sender_public_key": wallet.public_key_hex(), "signature": unstake_tx.signature,
        })
        assert resp.status == 200, await resp.text()
        assert node.store.unbonding[wallet.address][0] == 100

        # 无待领取质押 → 400
        resp = await client.post("/api/claim", json={"addr": wallet.address})
        assert resp.status == 400


async def test_rpc_unlock_stakes_stats_early_info():
    node = _node()
    node.store.stakes["0xstaker"] = 300
    async with await _make_client(node) as client:
        resp = await client.post("/api/unlock", json={"addr": "0xnobody"})
        assert resp.status == 200
        assert (await resp.json())["unlocked"] == 0
        resp = await client.post("/api/unlock", json={})
        assert resp.status == 400

        resp = await client.get("/api/stakes")
        body = await resp.json()
        assert body["total"] == 300 and body["stakes"]["0xstaker"] == 300

        resp = await client.get("/api/stats")
        body = await resp.json()
        for key in ("deploy_count", "deploy_reward", "referral_issued", "call_count",
                    "block_reward", "light_verify_reward", "quantum_safe"):
            assert key in body

        resp = await client.get("/api/early/info?addr=0xabc")
        body = await resp.json()
        assert body["miner_registered"] is False
        assert body["light_checkin_days"] == 0


async def test_rpc_referral_binding():
    node = _node()
    async with await _make_client(node) as client:
        resp = await client.post("/api/referral", json={"invitee": "0xb", "referrer": "0xa"})
        assert resp.status == 200
        assert node.store.referrals["0xb"] == "0xa"
        resp = await client.post("/api/referral", json={"invitee": "0xb", "referrer": "0xc"})
        assert resp.status == 400  # 已有推荐人
        resp = await client.post("/api/referral", json={"invitee": "0xa", "referrer": "0xa"})
        assert resp.status == 400  # 不能推荐自己


async def test_rpc_light_verify():
    node = _node()
    wallet = QuantumWallet()
    node.balances[wallet.address] = 100
    # v0.10：验证者池高于安全线（283.5 万），避免减支把轻节点验证奖励归零
    node.balances[Economy.VALIDATOR_POOL] = 3_000_000
    tx = _signed_tx(wallet, "0xbob", 1)
    node.store.dag.add(tx.txid)
    async with await _make_client(node) as client:
        resp = await client.post("/api/light/verify", json={"addr": "0xlight", "txid": "nope"})
        assert resp.status == 400  # 交易不存在
        resp = await client.post("/api/light/verify", json={"addr": "0xlight", "txid": tx.txid})
        assert resp.status == 200
        reward = (await resp.json())["reward"]
        assert reward > 0
        assert node.balances["0xlight"] == reward
        resp = await client.post("/api/light/verify", json={"addr": "0xlight", "txid": tx.txid})
        assert resp.status == 400  # 交易已验证
        resp = await client.post("/api/light/verify", json={"addr": "0xother", "txid": "another"})
        assert resp.status == 400  # 今日已领


async def test_rpc_presale_bind():
    node = _node()
    wallet = QuantumWallet()
    msg = "BIND_PRESALE:0xbsc"
    bad = wallet.sign("wrong message")
    async with await _make_client(node) as client:
        resp = await client.post("/api/presale/bind", json={
            "nova_address": wallet.address, "nova_public_key": wallet.public_key_hex(),
            "bsc_address": "0xbsc", "signature": bad,
        })
        assert resp.status == 400  # 签名不符
        resp = await client.post("/api/presale/bind", json={
            "nova_address": wallet.address, "nova_public_key": wallet.public_key_hex(),
            "bsc_address": "0xbsc", "signature": wallet.sign(msg),
        })
        assert resp.status == 200
        assert node.store.presale_verified[wallet.address] == "0xbsc"


async def test_rpc_checkin_requires_signature():
    node = _node()
    wallet = QuantumWallet()
    async with await _make_client(node) as client:
        # M-06：无签名签到 -> 拒绝
        resp = await client.post("/api/checkin", json={"addr": wallet.address})
        assert resp.status == 400
        assert "签名" in (await resp.json())["error"]

        # 非本人签名 -> 拒绝
        other = QuantumWallet()
        resp = await client.post("/api/checkin", json={
            "addr": wallet.address,
            "sender_public_key": other.public_key_hex(),
            "signature": other.sign("checkin:" + wallet.address),
        })
        assert resp.status == 400

        # 非法地址 -> 拒绝
        resp = await client.post("/api/checkin", json={
            "addr": "0xbad",
            "sender_public_key": wallet.public_key_hex(),
            "signature": wallet.sign("checkin:0xbad"),
        })
        assert resp.status == 400

        # 本人签名 -> 成功
        resp = await client.post("/api/checkin", json={
            "addr": wallet.address,
            "sender_public_key": wallet.public_key_hex(),
            "signature": wallet.sign("checkin:" + wallet.address),
        })
        assert resp.status == 200
        assert (await resp.json())["total_days"] == 1
        assert node.store.light_checkins[wallet.address] == 1


async def test_rpc_checkin():
    node = _node()
    node.balances[Economy.ECOSYSTEM_FUND] = 1000
    w1 = QuantumWallet()
    w2 = QuantumWallet()

    def ck(addr, sig, pk, fingerprint=None):
        body = {"addr": addr, "sender_public_key": pk, "signature": sig}
        if fingerprint:
            body["fingerprint"] = fingerprint
        return body

    async with await _make_client(node) as client:
        # w1 本人签名首次签到成功
        resp = await client.post("/api/checkin", json=ck(w1.address, w1.sign("checkin:" + w1.address), w1.public_key_hex()))
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["total_days"] == 1
        # w1 同日再次签到被拒（今日已签到）
        resp = await client.post("/api/checkin", json=ck(w1.address, w1.sign("checkin:" + w1.address), w1.public_key_hex()))
        assert resp.status == 400  # 今日已签到
        # w2 同 IP 24 小时内只能一个轻节点（IP 限制先于签到检查）
        resp = await client.post("/api/checkin", json=ck(w2.address, w2.sign("checkin:" + w2.address), w2.public_key_hex()))
        assert resp.status == 400  # 同 IP 24 小时内只能一个轻节点
        # w2 带未注册指纹仍被 IP 限制拒绝
        resp = await client.post("/api/checkin", json=ck(w2.address, w2.sign("checkin:" + w2.address), w2.public_key_hex(), "fp1"))
        assert resp.status == 400  # 设备已被注册（IP 限制拦截）


async def test_rpc_send_malformed_json():
    node = _node()
    async with await _make_client(node) as client:
        resp = await client.post("/api/send", data=b"not json")
        assert resp.status == 400