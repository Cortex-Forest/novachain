# -*- coding: utf-8 -*-
"""创作者订阅与会员测试：分档订阅 / 90-10 分账 / 自动续费 / 永久会员 / 取消。"""
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9966)
    kw.setdefault("rpc", 8318)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


def _signed_tx(w, op, amount=0.0, **kw):
    payload = {"op": op}
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


def _creator(node):
    creator = QuantumWallet()
    _fund(node, creator.address)
    _apply(node, _signed_tx(creator, "nova:sub:create", tiers=[
        {"id": "basic", "name": "基础档", "price": 10, "period": "monthly", "benefits": ["密文解锁"]},
        {"id": "lifetime", "name": "永久会员", "price": 100, "period": "lifetime"},
    ]))
    return creator


# ---------------------------------------------------------------------------
# 1. 月付订阅：90% 归创作者 / 10% 归生态基金 + 订阅者徽章
# ---------------------------------------------------------------------------
def test_subscribe_monthly_split():
    node = _node()
    creator = _creator(node)
    user = QuantumWallet()
    _fund(node, user.address, 10000)
    c0 = node.balances[creator.address]
    e0 = node.balances.get(node.economy.ECOSYSTEM_FUND, 0.0)
    _apply(node, _signed_tx(user, "nova:sub:subscribe", amount=10,
                            creator=creator.address, tier_id="basic", auto_renew=True))
    assert node.subscription.is_active(user.address, creator.address)
    assert node.balances[creator.address] == pytest.approx(c0 + 9.0)
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(e0 + 1.0)
    badge = "nova:sub:" + creator.address.lower()
    assert user.address in node.store.soulbound.get(badge, [])
    # 重复订阅被拒（续费走 renew）
    bad = _signed_tx(user, "nova:sub:subscribe", amount=10,
                     creator=creator.address, tier_id="basic")
    assert not node.validate_tx(bad)


# ---------------------------------------------------------------------------
# 2. 自动续费：成功续期 / 余额不足自动取消
# ---------------------------------------------------------------------------
def test_auto_renew_success_and_cancel():
    node = _node()
    creator = _creator(node)
    user = QuantumWallet()
    _fund(node, user.address, 10000)
    _apply(node, _signed_tx(user, "nova:sub:subscribe", amount=10,
                            creator=creator.address, tier_id="basic", auto_renew=True))
    sub = node.subscription.subscription(user.address, creator.address)
    sub["expires_at"] = time.time() - 1
    bal0 = node.balances[user.address]
    _apply(node, _signed_tx(user, "nova:sub:renew", creator=creator.address))
    assert node.subscription.is_active(user.address, creator.address)
    assert node.balances[user.address] == pytest.approx(bal0 - 10.0)
    # 余额不足自动取消
    sub["expires_at"] = time.time() - 1
    node.balances[user.address] = 1.0
    _apply(node, _signed_tx(user, "nova:sub:renew", creator=creator.address))
    assert node.subscription.subscription(user.address, creator.address)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 3. 永久会员：一次支付永久有效，不可取消
# ---------------------------------------------------------------------------
def test_lifetime_subscription():
    node = _node()
    creator = _creator(node)
    user = QuantumWallet()
    _fund(node, user.address, 10000)
    _apply(node, _signed_tx(user, "nova:sub:subscribe", amount=100,
                            creator=creator.address, tier_id="lifetime"))
    assert node.subscription.is_active(user.address, creator.address)
    bad = _signed_tx(user, "nova:sub:cancel", creator=creator.address)
    assert not node.validate_tx(bad)


# ---------------------------------------------------------------------------
# 4. 手动取消自动续费
# ---------------------------------------------------------------------------
def test_cancel_auto_renew():
    node = _node()
    creator = _creator(node)
    user = QuantumWallet()
    _fund(node, user.address, 10000)
    _apply(node, _signed_tx(user, "nova:sub:subscribe", amount=10,
                            creator=creator.address, tier_id="basic", auto_renew=True))
    _apply(node, _signed_tx(user, "nova:sub:cancel", creator=creator.address))
    sub = node.subscription.subscription(user.address, creator.address)
    assert sub["auto_renew"] is False
    assert node.subscription.is_active(user.address, creator.address)  # 仍有效到到期日
    # 未到期不续费
    bad = _signed_tx(user, "nova:sub:renew", creator=creator.address)
    assert not node.validate_tx(bad)
