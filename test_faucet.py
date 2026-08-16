# -*- coding: utf-8 -*-
"""测试网水龙头测试：资金池引导 / 领取发放 / 限频（地址 24h、IP 每日、全局日限额）/ 设备唯一 / 主网关闭。"""
import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from core.crypto import QuantumWallet
from nova_node import NovaNode
from network.rpc import setup_routes


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9966)
    kw.setdefault("rpc", 8318)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _client(node):
    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    return TestClient(TestServer(app))


async def _claim(client, addr, fingerprint=""):
    body = {"addr": addr}
    if fingerprint:
        body["fingerprint"] = fingerprint
    return await client.post("/api/faucet/request", json=body)


# ---------------------------------------------------------------------------
# 1. 测试网模式：资金池引导铸造；主网模式：未启用、403
# ---------------------------------------------------------------------------
async def test_faucet_bootstrap_and_mainnet_closed():
    node = _node(faucet=True)
    assert node.store.balances[node.economy.FAUCET_POOL] == pytest.approx(node.economy.FAUCET_INITIAL_POOL)

    # 主网模式（默认关闭）：不铸造，请求返回 403
    node2 = _node()
    assert node2.store.balances.get(node2.economy.FAUCET_POOL, 0) == 0
    async with _client(node2) as client:
        r = await client.get("/api/faucet/status")
        d = await r.json()
        assert d["enabled"] is False
        addr = QuantumWallet().address
        r = await _claim(client, addr)
        assert r.status == 403


# ---------------------------------------------------------------------------
# 2. 领取成功：+100 NOVA、池减少、回执与记录落库
# ---------------------------------------------------------------------------
async def test_faucet_claim_success():
    node = _node(faucet=True)
    wallet = QuantumWallet()
    addr = wallet.address
    pool0 = node.store.balances[node.economy.FAUCET_POOL]
    async with _client(node) as client:
        r = await client.get("/api/faucet/status")
        st = await r.json()
        assert st["enabled"] is True
        assert st["amount"] == node.economy.FAUCET_AMOUNT

        r = await _claim(client, addr, "fp-claim-success")
        d = await r.json()
        assert d["status"] == "领取成功"
        assert d["amount"] == node.economy.FAUCET_AMOUNT
        assert d["addr"] == addr
        assert node.balances[addr] == node.economy.FAUCET_AMOUNT
        assert node.store.balances[node.economy.FAUCET_POOL] == pytest.approx(pool0 - node.economy.FAUCET_AMOUNT)
        assert d["receipt"] in node.store.faucet_receipts
        assert node.store.faucet_claims[addr]["count"] == 1
        import time as _t
        assert node.store.faucet_daily[_t.strftime("%Y-%m-%d")]["ips"].get("127.0.0.1", 0) == 1


# ---------------------------------------------------------------------------
# 3. 限频：同一地址 24h 内重复领取被拒；非法/保留地址被拒
# ---------------------------------------------------------------------------
async def test_faucet_limits_addr_and_format():
    node = _node(faucet=True)
    wallet = QuantumWallet()
    addr = wallet.address
    async with _client(node) as client:
        r = await _claim(client, addr)
        assert (await r.json())["status"] == "领取成功"

        r = await _claim(client, addr)
        assert r.status == 400
        assert "24 小时" in (await r.json())["error"]

        r = await _claim(client, "not-an-address")
        assert r.status == 400

        r = await _claim(client, node.economy.FAUCET_POOL)
        assert r.status == 400

        r = await _claim(client, "0x_ecosystem_fund")
        assert r.status == 400


# ---------------------------------------------------------------------------
# 4. 每日 IP 上限：同一 IP 第 3 次被拒（前 2 次不同地址）
# ---------------------------------------------------------------------------
async def test_faucet_ip_cap():
    node = _node(faucet=True)
    async with _client(node) as client:
        r = await _claim(client, QuantumWallet().address)
        assert (await r.json())["status"] == "领取成功"
        r = await _claim(client, QuantumWallet().address)
        assert (await r.json())["status"] == "领取成功"
        r = await _claim(client, QuantumWallet().address)
        assert r.status == 400
        assert "IP" in (await r.json())["error"]


# ---------------------------------------------------------------------------
# 5. 设备指纹唯一：同一指纹第二个地址被拒
# ---------------------------------------------------------------------------
async def test_faucet_device_unique():
    node = _node(faucet=True)
    async with _client(node) as client:
        r = await _claim(client, QuantumWallet().address, "fp-device-1")
        assert (await r.json())["status"] == "领取成功"
        r = await _claim(client, QuantumWallet().address, "fp-device-1")
        assert r.status == 400
        assert "设备" in (await r.json())["error"]


# ---------------------------------------------------------------------------
# 6. 全局日限额：超过 daily_cap 后拒绝；资金池不足拒绝
# ---------------------------------------------------------------------------
async def test_faucet_daily_cap_and_pool():
    node = _node(faucet=True)
    node.economy.FAUCET_DAILY_IP_CAP = 999
    node.economy.FAUCET_DAILY_CAP = node.economy.FAUCET_AMOUNT * 2  # 只够 2 次
    async with _client(node) as client:
        for _ in range(2):
            r = await _claim(client, QuantumWallet().address)
            assert (await r.json())["status"] == "领取成功"
        r = await _claim(client, QuantumWallet().address)
        assert r.status == 400
        assert "额度" in (await r.json())["error"]

    # 资金池不足
    node2 = _node(faucet=True)
    node2.store.balances[node2.economy.FAUCET_POOL] = node2.economy.FAUCET_AMOUNT - 1
    async with _client(node2) as client:
        r = await _claim(client, QuantumWallet().address)
        assert r.status == 400
        assert "资金池" in (await r.json())["error"]


# ---------------------------------------------------------------------------
# 7. 状态接口汇总字段
# ---------------------------------------------------------------------------
async def test_faucet_status_fields():
    node = _node(faucet=True)
    wallet = QuantumWallet()
    async with _client(node) as client:
        r = await _claim(client, wallet.address)
        assert (await r.json())["status"] == "领取成功"
        r = await client.get("/api/faucet/status")
        d = await r.json()
        assert d["pool_balance"] == pytest.approx(node.economy.FAUCET_INITIAL_POOL - node.economy.FAUCET_AMOUNT)
        assert d["today"]["count"] == 1
        assert d["today"]["amount"] == node.economy.FAUCET_AMOUNT
        assert d["total_claimed"] == node.economy.FAUCET_AMOUNT
        assert d["total_recipients"] == 1
