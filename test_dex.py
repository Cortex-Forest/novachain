# -*- coding: utf-8 -*-
"""DEX 模块测试：AMM 恒定乘积 / LP 代币 / 手续费与回购销毁 / 滑点保护 / 流动性挖矿。"""
import json
import time

import pytest

from core.crypto import QuantumWallet
from core.transaction import Tx
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9963)
    kw.setdefault("rpc", 8315)
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


def _seed(node):
    node.balances["0x_presale"] = 50000.0
    node.balances[node.economy.ECOSYSTEM_FUND] = 1000000.0
    assert node.dex.bootstrap()
    return node


# ---------------------------------------------------------------------------
# 1. 预售资金提供初始流动性
# ---------------------------------------------------------------------------
def test_bootstrap_initial_liquidity():
    node = _seed(_node())
    p = node.dex.pair("NOVA/USDT")
    assert p["reserve0"] == pytest.approx(10000.0)
    assert p["reserve1"] == pytest.approx(20000.0)
    p2 = node.dex.pair("NOVA/nETH")
    assert p2["reserve0"] > 0 and p2["reserve1"] > 0


# ---------------------------------------------------------------------------
# 2. 添加 / 移除流动性
# ---------------------------------------------------------------------------
def test_add_remove_liquidity():
    node = _seed(_node())
    lp = QuantumWallet()
    _fund(node, lp.address)
    node.bridge._mint_wrapped("nUSDT", lp.address, 5000.0)
    _apply(node, _signed_tx(lp, "nova:dex:add", amount=1000, amount0=1000, amount1=2000, pair_id="NOVA/USDT"))
    pos = node.dex.lp_position(lp.address, "NOVA/USDT")
    assert pos["shares"] > 0
    p = node.dex.pair("NOVA/USDT")
    assert p["reserve0"] == pytest.approx(11000.0)
    assert p["reserve1"] == pytest.approx(22000.0)
    _apply(node, _signed_tx(lp, "nova:dex:remove", pair_id="NOVA/USDT", shares=pos["shares"]))
    assert node.dex.lp_position(lp.address, "NOVA/USDT")["shares"] == 0.0
    assert p["reserve0"] == pytest.approx(10000.0, rel=1e-6)


# ---------------------------------------------------------------------------
# 3. 兑换：手续费 0.3%（0.25% LP + 0.05% 回购销毁）
# ---------------------------------------------------------------------------
def test_swap_fee_and_burn():
    node = _seed(_node())
    trader = QuantumWallet()
    _fund(node, trader.address)
    q = node.dex.quote("NOVA/USDT", 100, 0)
    out = q["amount_out"]
    assert out > 0
    _apply(node, _signed_tx(trader, "nova:dex:swap", amount=100, amount_in=100,
                            token_in=0, min_out=out, pair_id="NOVA/USDT"))
    p = node.dex.pair("NOVA/USDT")
    assert p["burned0"] == pytest.approx(100 * 0.0005)     # 0.05% 回购销毁
    assert p["reserve0"] == pytest.approx(10000 + 100 - 0.05)
    assert p["reserve1"] == pytest.approx(20000 - out)
    assert node.bridge.asset("nUSDT")["balances"][trader.address] == pytest.approx(out)


# ---------------------------------------------------------------------------
# 4. 滑点保护：超过最大滑点自动取消 + 大额分拆
# ---------------------------------------------------------------------------
def test_slippage_reject_and_split():
    node = _seed(_node())
    trader = QuantumWallet()
    _fund(node, trader.address)
    q = node.dex.quote("NOVA/USDT", 100, 0)
    bad = _signed_tx(trader, "nova:dex:swap", amount=100, amount_in=100,
                     token_in=0, min_out=q["amount_out"] + 1, pair_id="NOVA/USDT")
    assert not node.validate_tx(bad)
    split = node.dex.split_quote("NOVA/USDT", 5000, 0)
    assert split["pieces"] > 1
    # 反向兑换：卖 nUSDT 得 NOVA
    node.bridge._mint_wrapped("nUSDT", trader.address, 1000.0)
    q2 = node.dex.quote("NOVA/USDT", 100, 1)
    _apply(node, _signed_tx(trader, "nova:dex:swap", amount_in=100, token_in=1,
                            min_out=q2["amount_out"], pair_id="NOVA/USDT"))
    assert node.balances[trader.address] > 100000.0 - 1e-3


# ---------------------------------------------------------------------------
# 5. 流动性挖矿：质押 LP -> NOVA 奖励
# ---------------------------------------------------------------------------
def test_farm_stake_claim():
    node = _seed(_node())
    lp = QuantumWallet()
    _fund(node, lp.address)
    node.bridge._mint_wrapped("nUSDT", lp.address, 5000.0)
    _apply(node, _signed_tx(lp, "nova:dex:add", amount=1000, amount0=1000, amount1=2000, pair_id="NOVA/USDT"))
    shares = node.dex.lp_position(lp.address, "NOVA/USDT")["shares"]
    _apply(node, _signed_tx(lp, "nova:dex:farm:stake", pair_id="NOVA/USDT", shares=shares))
    pool = node.dex.farm_pool("NOVA/USDT")
    assert pool["total_staked"] == pytest.approx(shares)
    # 模拟一小时流逝后领取奖励
    pool["last"] = time.time() - 3600
    bal0 = node.balances[lp.address]
    _apply(node, _signed_tx(lp, "nova:dex:farm:claim", pair_id="NOVA/USDT"))
    assert node.balances[lp.address] > bal0


# ---------------------------------------------------------------------------
# 6. 紧急暂停
# ---------------------------------------------------------------------------
def test_pause_blocks_trades():
    node = _seed(_node())
    node.dex.set_paused(True)
    trader = QuantumWallet()
    _fund(node, trader.address)
    q = node.dex.quote("NOVA/USDT", 100, 0)
    bad = _signed_tx(trader, "nova:dex:swap", amount=100, amount_in=100, token_in=0,
                     min_out=q["amount_out"], pair_id="NOVA/USDT")
    assert not node.validate_tx(bad)
    node.dex.set_paused(False)
    assert node.validate_tx(_signed_tx(trader, "nova:dex:swap", amount=100, amount_in=100,
                                       token_in=0, min_out=0, pair_id="NOVA/USDT"))
