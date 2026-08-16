# -*- coding: utf-8 -*-
"""跨链桥模块测试：跨入铸造 / 跨出释放 / 3/5 多签 / 手续费回流 / 额度与延迟。"""
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9962)
    kw.setdefault("rpc", 8314)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


def _signed_tx(w, op, amount=0.0, data=None, **kw):
    payload = {"op": op}
    if data:
        payload.update(data)
    if amount:
        payload["amount"] = amount
    payload.update(kw)
    data = json.dumps(payload, ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _apply(node, tx):
    assert node.validate_tx(tx), "validate failed: " + tx.data[:100]
    node.apply_tx(tx)


def _nodes(node, n=5):
    ws = []
    for _ in range(n):
        w = QuantumWallet()
        _fund(node, w.address)
        _apply(node, _signed_tx(w, "nova:bridge:node:register", amount=1000))
        ws.append(w)
    return ws


def _asset(node, node_w, symbol="nUSDT"):
    _apply(node, _signed_tx(node_w, "nova:bridge:asset:register", symbol=symbol))


def _day():
    return time.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 1. 跨入：存款 -> 3/5 多签 -> 铸造
# ---------------------------------------------------------------------------
def test_deposit_mint():
    node = _node()
    nodes = _nodes(node)
    user = QuantumWallet()
    _asset(node, nodes[0])
    _apply(node, _signed_tx(nodes[0], "nova:bridge:deposit", data={"amount": 1000.0},
                            asset="nUSDT", source_chain="bsc",
                            source_tx="ab" * 32, source_addr=user.address))
    did = next(reversed(node.store.bridge_deposits))
    dep = node.store.bridge_deposits[did]
    assert dep["status"] == "pending" and len(dep["sigs"]) == 1
    _apply(node, _signed_tx(nodes[1], "nova:bridge:deposit:sign", deposit_id=did))
    assert dep["status"] == "pending"
    _apply(node, _signed_tx(nodes[2], "nova:bridge:deposit:sign", deposit_id=did))
    assert dep["status"] == "ready"                       # 3/5 达成
    _apply(node, _signed_tx(nodes[0], "nova:bridge:deposit:claim", deposit_id=did))
    assert dep["status"] == "minted"
    # 手续费 0.1%（最低 1 USDT）：1000 * 0.1% = 1
    assert node.bridge.asset("nUSDT")["balances"][user.address] == pytest.approx(999.0)
    assert node.store.bridge_fee_pool == pytest.approx(1.0)
    # 同一源链交易不可重放
    dup = _signed_tx(nodes[0], "nova:bridge:deposit", data={"amount": 1000.0},
                     asset="nUSDT", source_chain="bsc",
                     source_tx="ab" * 32, source_addr=user.address)
    assert not node.validate_tx(dup)


# ---------------------------------------------------------------------------
# 2. 每日额度上限（100 万 USDT）
# ---------------------------------------------------------------------------
def test_daily_limit():
    node = _node()
    nodes = _nodes(node)
    user = QuantumWallet()
    _asset(node, nodes[0])
    node.store.bridge_daily_usage[_day()] = {"minted_usd": 999_999.0, "released_usd": 0.0}
    bad = _signed_tx(nodes[0], "nova:bridge:deposit", data={"amount": 100.0},
                     asset="nUSDT", source_chain="bsc",
                     source_tx="cd" * 32, source_addr=user.address)
    assert not node.validate_tx(bad)


# ---------------------------------------------------------------------------
# 3. 大额跨链（>10 万 USDT）延迟 24 小时
# ---------------------------------------------------------------------------
def test_large_deposit_hold_24h():
    node = _node()
    nodes = _nodes(node)
    user = QuantumWallet()
    _asset(node, nodes[0])
    _apply(node, _signed_tx(nodes[0], "nova:bridge:deposit", data={"amount": 200_000.0},
                            asset="nUSDT", source_chain="eth",
                            source_tx="ef" * 32, source_addr=user.address))
    did = next(reversed(node.store.bridge_deposits))
    _apply(node, _signed_tx(nodes[1], "nova:bridge:deposit:sign", deposit_id=did))
    _apply(node, _signed_tx(nodes[2], "nova:bridge:deposit:sign", deposit_id=did))
    assert node.store.bridge_deposits[did]["status"] == "held"
    bad = _signed_tx(nodes[0], "nova:bridge:deposit:claim", deposit_id=did)
    assert not node.validate_tx(bad)                       # 24h 内不可到账
    node.store.bridge_deposits[did]["available_at"] = time.time() - 1
    _apply(node, _signed_tx(nodes[0], "nova:bridge:deposit:claim", deposit_id=did))
    assert node.store.bridge_deposits[did]["status"] == "minted"


# ---------------------------------------------------------------------------
# 4. 跨出：销毁包装资产 -> 多签 -> 源链释放
# ---------------------------------------------------------------------------
def test_withdraw_burn_release():
    node = _node()
    nodes = _nodes(node)
    user = QuantumWallet()
    _fund(node, user.address)
    _asset(node, nodes[0])
    node.bridge._mint_wrapped("nUSDT", user.address, 500.0)
    _apply(node, _signed_tx(user, "nova:bridge:withdraw", data={"amount": 500.0},
                            asset="nUSDT", target_chain="bsc",
                            target_addr=user.address))
    wid = next(reversed(node.store.bridge_withdrawals))
    wd = node.store.bridge_withdrawals[wid]
    assert wd["status"] == "pending"
    assert node.bridge.asset("nUSDT")["balances"].get(user.address, 0.0) == pytest.approx(0.0)
    _apply(node, _signed_tx(nodes[1], "nova:bridge:withdraw:sign", withdraw_id=wid))
    _apply(node, _signed_tx(nodes[2], "nova:bridge:withdraw:sign", withdraw_id=wid))
    _apply(node, _signed_tx(nodes[3], "nova:bridge:withdraw:sign", withdraw_id=wid))
    assert wd["status"] == "ready"                          # 3/5 达成
    _apply(node, _signed_tx(nodes[0], "nova:bridge:withdraw:confirm", withdraw_id=wid,
                            release_tx="11" * 32))
    assert wd["status"] == "released"
    # 500*0.1% = 0.5 < 最低 1 USDT -> 收 1
    assert node.store.bridge_fee_pool == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 5. NOVA 跨出（原生扣减）
# ---------------------------------------------------------------------------
def test_nova_outbound():
    node = _node()
    nodes = _nodes(node)
    user = QuantumWallet()
    _fund(node, user.address, 10000.0)
    _apply(node, _signed_tx(user, "nova:bridge:withdraw", amount=1000.0, asset="NOVA",
                            target_chain="bsc", target_addr=user.address))
    wid = next(reversed(node.store.bridge_withdrawals))
    wd = node.store.bridge_withdrawals[wid]
    assert wd["amount"] == 1000.0
    _apply(node, _signed_tx(nodes[1], "nova:bridge:withdraw:sign", withdraw_id=wid))
    _apply(node, _signed_tx(nodes[2], "nova:bridge:withdraw:sign", withdraw_id=wid))
    _apply(node, _signed_tx(nodes[3], "nova:bridge:withdraw:sign", withdraw_id=wid))
    _apply(node, _signed_tx(nodes[0], "nova:bridge:withdraw:confirm", withdraw_id=wid))
    assert wd["status"] == "released"


# ---------------------------------------------------------------------------
# 6. 手续费 100% 回流验证者激励池
# ---------------------------------------------------------------------------
def test_fee_pool_flush():
    node = _node()
    nodes = _nodes(node)
    user = QuantumWallet()
    _asset(node, nodes[0])
    _apply(node, _signed_tx(nodes[0], "nova:bridge:deposit", data={"amount": 1000.0},
                            asset="nUSDT", source_chain="bsc",
                            source_tx="22" * 32, source_addr=user.address))
    did = next(reversed(node.store.bridge_deposits))
    _apply(node, _signed_tx(nodes[1], "nova:bridge:deposit:sign", deposit_id=did))
    _apply(node, _signed_tx(nodes[2], "nova:bridge:deposit:sign", deposit_id=did))
    _apply(node, _signed_tx(nodes[0], "nova:bridge:deposit:claim", deposit_id=did))
    assert node.store.bridge_fee_pool == pytest.approx(1.0)
    pool0 = node.balances[node.economy.VALIDATOR_POOL]
    _apply(node, _signed_tx(nodes[0], "nova:bridge:pool:flush"))
    assert node.store.bridge_fee_pool == pytest.approx(0.0)
    assert node.balances[node.economy.VALIDATOR_POOL] == pytest.approx(pool0 + 1.0)
