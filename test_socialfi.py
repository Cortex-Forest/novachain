# -*- coding: utf-8 -*-
"""SocialFi 层测试：粉丝代币 / 收益共享 / 成就 / 预测市场 / 盲盒 /
策展 / 社交图谱与推荐 / 声誉与手续费折扣 / 创作者债券 / 碎片化 NFT。"""
import asyncio
import hashlib
import json
import time

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from core.transaction import Tx
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9971)
    kw.setdefault("rpc", 8323)
    kw.setdefault("use_tls", False)
    kw.setdefault("state_file", None)
    return NovaNode(**kw)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


def _fund_eco(node, amt=1000000.0):
    node.balances[node.economy.ECOSYSTEM_FUND] = amt


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
    assert node.validate_tx(tx), "validate failed: " + tx.data[:80]
    node.apply_tx(tx)


def _cid(n=0):
    return "0x" + ("aabbccdd" + f"{n:056x}")


# ---------------------------------------------------------------------------
# 1. 粉丝代币发行平台
# ---------------------------------------------------------------------------
def test_fan_token_issue_buy_vote():
    node = _node()
    creator = QuantumWallet()
    fan = QuantumWallet()
    fan2 = QuantumWallet()
    _fund(node, creator.address)
    _fund(node, fan.address)
    _fund(node, fan2.address)

    _apply(node, _signed_tx(creator, "nova:fan:issue", symbol="NOVA", name="Nova 粉丝币",
                            supply=10000, price=0.1))
    tid = next(iter(node.store.fan_tokens))
    t = node.store.fan_tokens[tid]
    assert t["creator"] == creator.address

    c_bal0 = node.balances[creator.address]
    cost = node.socialfi.fan_price_at(tid, 100)
    _apply(node, _signed_tx(fan, "nova:fan:buy", amount=cost, tid=tid, qty=100))
    assert t["holders"][fan.address] == 100
    assert t["sold"] == 100
    assert node.balances[creator.address] == pytest.approx(c_bal0 + cost)

    # 价格随销量上涨（早期买家升值）
    cost2 = node.socialfi.fan_price_at(tid, 100)
    assert cost2 > cost

    # 持有代币的粉丝发起提案并按持仓投票
    _apply(node, _signed_tx(fan, "nova:fan:propose", tid=tid, title="是否发行新专辑？", closes_in=3600))
    pid = next(iter(t["proposals"]))
    _apply(node, _signed_tx(fan, "nova:fan:vote", tid=tid, proposal_id=pid, option=0))
    assert t["proposals"][pid]["votes"][0] == 100
    # 重复投票被拒绝
    bad = _signed_tx(fan, "nova:fan:vote", tid=tid, proposal_id=pid, option=1)
    assert not node.validate_tx(bad)
    # 无持仓者不能提案
    bad2 = _signed_tx(fan2, "nova:fan:propose", tid=tid, title="无持仓提案", closes_in=3600)
    assert not node.validate_tx(bad2)


# ---------------------------------------------------------------------------
# 2. 收益共享合约
# ---------------------------------------------------------------------------
def test_revenue_share_invest_royalty_claim():
    node = _node()
    creator = QuantumWallet()
    investor = QuantumWallet()
    _fund(node, creator.address)
    _fund(node, investor.address)

    _apply(node, _signed_tx(creator, "nova:rev:create", name="音乐人未来三年版税"))
    rid = next(iter(node.store.revenue_shares))
    assert node.store.revenue_shares[rid]["creator"] == creator.address

    _apply(node, _signed_tx(investor, "nova:rev:invest", amount=100, rid=rid))
    assert node.store.revenue_shares[rid]["investors"][investor.address] == 100
    assert node.balances[creator.address] == pytest.approx(100000.0 + 100)

    _apply(node, _signed_tx(creator, "nova:rev:royalty", amount=30, rid=rid))
    r = node.store.revenue_shares[rid]
    assert r["pool"] == 30

    bal0 = node.balances[investor.address]
    _apply(node, _signed_tx(investor, "nova:rev:claim", rid=rid))
    assert node.balances[investor.address] == pytest.approx(bal0 + 30)
    assert r["pool"] == 0


# ---------------------------------------------------------------------------
# 3. 链上成就系统（灵魂绑定）
# ---------------------------------------------------------------------------
def test_achievement_soulbound():
    node = _node()
    issuer = QuantumWallet()
    target = QuantumWallet()
    _fund(node, issuer.address)
    _fund(node, target.address)

    _apply(node, _signed_tx(issuer, "nova:ach:issue", title="连续签到365天", desc="灵魂绑定徽章", badge="🔥"))
    aid = next(iter(node.store.achievements))
    _apply(node, _signed_tx(issuer, "nova:ach:award", aid=aid, target=target.address))
    assert target.address in node.store.soulbound[aid]
    # 不能重复颁发
    bad = _signed_tx(issuer, "nova:ach:award", aid=aid, target=target.address)
    assert not node.validate_tx(bad)


# ---------------------------------------------------------------------------
# 4. 预言机驱动的预测市场
# ---------------------------------------------------------------------------
def test_prediction_market_bet_settle():
    node = _node()
    creator = QuantumWallet()
    oracle = QuantumWallet()
    alice = QuantumWallet()
    bob = QuantumWallet()
    for w in (creator, oracle, alice, bob):
        _fund(node, w.address)
    _fund_eco(node)

    _apply(node, _signed_tx(creator, "nova:market:create", question="这部电影票房能破10亿吗？",
                            options=["能", "不能"], closes_in=600, oracle=oracle.address))
    mid = next(iter(node.store.markets))
    m = node.store.markets[mid]
    assert m["oracle"] == oracle.address

    _apply(node, _signed_tx(alice, "nova:market:bet", amount=60, mid=mid, option=0))
    _apply(node, _signed_tx(bob, "nova:market:bet", amount=40, mid=mid, option=0))
    assert m["pool"][0] == 100

    m["closes_at"] = time.time() - 1
    eco0 = node.balances[node.economy.ECOSYSTEM_FUND]
    alice0 = node.balances[alice.address]
    bob0 = node.balances[bob.address]
    _apply(node, _signed_tx(oracle, "nova:market:settle", mid=mid, outcome=0))
    assert m["settled"] and m["outcome"] == 0
    # 全部押对：扣除 2% 平台费后按比例分配
    fee = 100 * 0.02
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(eco0 + fee)
    assert node.balances[alice.address] == pytest.approx(alice0 + 60 / 100 * (100 - fee))
    assert node.balances[bob.address] == pytest.approx(bob0 + 40 / 100 * (100 - fee))


# ---------------------------------------------------------------------------
# 5. 链上随机抽奖 / 盲盒（commit-reveal 可验证随机）
# ---------------------------------------------------------------------------
def test_blindbox_commit_reveal_open():
    node = _node()
    creator = QuantumWallet()
    player = QuantumWallet()
    _fund(node, creator.address)
    _fund(node, player.address)

    seed = "ab" * 32
    commit = hashlib.sha3_256(seed.encode()).hexdigest()
    tiers = [
        {"name": "传说", "weight": 1, "reward_type": "nova", "reward_amount": 50},
        {"name": "稀有", "weight": 5, "reward_type": "badge", "reward_cid": _cid(1)},
        {"name": "普通", "weight": 20, "reward_type": "nova", "reward_amount": 2},
    ]
    _apply(node, _signed_tx(creator, "nova:blind:create", name="星际盲盒", price=10, commit=commit, tiers=tiers))
    bid = next(iter(node.store.blindboxes))

    # 未揭示不能开盒
    assert not node.validate_tx(_signed_tx(player, "nova:blind:open", amount=10, bid=bid, draws=1))
    # 错误种子被拒绝
    assert not node.validate_tx(_signed_tx(creator, "nova:blind:reveal", bid=bid, seed="cd" * 32))
    _apply(node, _signed_tx(creator, "nova:blind:reveal", bid=bid, seed=seed))
    assert node.store.blind_reveals[bid] == seed

    bal0 = node.balances[player.address]
    _apply(node, _signed_tx(player, "nova:blind:open", amount=10, bid=bid, draws=1))
    box = node.store.blindboxes[bid]
    assert box["draws"][player.address] == 1
    # 开盒结果确定且可复现（含中奖奖励）
    tier = node.socialfi.blind_draw(box, seed, player.address, 0)
    assert tier in tiers
    expected = bal0 - 10 - node.gas_of(player.address)
    if tier["reward_type"] == "nova":
        expected += float(tier["reward_amount"])
    assert node.balances[player.address] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 6. 去中心化内容策展
# ---------------------------------------------------------------------------
def test_curation_create_buy_with_cid_pin():
    node = _node()
    curator = QuantumWallet()
    buyer = QuantumWallet()
    _fund(node, curator.address)
    _fund(node, buyer.address)
    _fund_eco(node)

    cover = _cid(7)
    _apply(node, _signed_tx(curator, "nova:curate:create", title="2026 最佳单曲歌单",
                            items=["星轨回声", "量子夜航", "超新星原石"], price=5,
                            cid=cover, size_gb=0.001, duration_days=30))
    cur = next(iter(node.store.curations))
    c = node.store.curations[cur]
    assert c["curator"] == curator.address
    # 封面 CID 自动固定到存储网络（存储能力被使用）
    assert cover in node.store.storage_claims

    cur0 = node.balances[curator.address]
    eco0 = node.balances[node.economy.ECOSYSTEM_FUND]
    _apply(node, _signed_tx(buyer, "nova:curate:buy", amount=5, cur_id=cur))
    assert buyer.address in c["owners"]
    assert node.balances[curator.address] == pytest.approx(cur0 + 5 * 0.9)
    assert node.balances[node.economy.ECOSYSTEM_FUND] == pytest.approx(eco0 + 5 * 0.1)
    # 重复购买被拒绝
    assert not node.validate_tx(_signed_tx(buyer, "nova:curate:buy", amount=5, cur_id=cur))


# ---------------------------------------------------------------------------
# 7. 社交图谱与推荐引擎（联动算力任务）
# ---------------------------------------------------------------------------
def test_graph_follow_like_recommend_and_compute_spec():
    node = _node()
    alice = QuantumWallet()
    bob = QuantumWallet()
    carol = QuantumWallet()
    dave = QuantumWallet()
    for w in (alice, bob, carol, dave):
        _fund(node, w.address)
    _fund_eco(node)

    cid = _cid(2)
    _apply(node, _signed_tx(bob, "nova:graph:post", content="我的新专辑上线了", cid=cid,
                            size_gb=0.001, duration_days=30))
    pid = next(iter(node.store.graph_posts))
    assert node.store.graph_posts[pid]["addr"] == bob.address
    assert cid in node.store.storage_claims          # 内容上链存储

    _apply(node, _signed_tx(alice, "nova:graph:follow", target=bob.address))
    _apply(node, _signed_tx(bob, "nova:graph:follow", target=carol.address))
    _apply(node, _signed_tx(alice, "nova:graph:like", pid=pid))

    recs = node.socialfi.recommendations(alice.address)
    assert recs and any(r["addr"] == carol.address for r in recs)
    spec = node.socialfi.recommend_task_spec(alice.address)
    assert spec.startswith("nova:recommend:")
    assert node.socialfi.graph_hash() in spec          # 可验证算力任务输入
    # 该规格可直接发布为算力市场任务
    _apply(node, _signed_tx(alice, "nova:compute:publish", amount=20, spec=spec, expires_in=600))
    assert any(t.get("spec") == spec for t in node.store.compute_tasks.values())


# ---------------------------------------------------------------------------
# 8. 链上声誉系统与手续费折扣
# ---------------------------------------------------------------------------
def test_reputation_gas_discount():
    node = _node()
    vip = QuantumWallet()
    normal = QuantumWallet()
    _fund(node, vip.address)
    _fund(node, normal.address)

    st = node.store
    st.stakes[vip.address] = 1000.0
    st.light_checkins[vip.address] = 270
    st.contract_creator["c1"] = vip.address
    st.contract_creator["c2"] = vip.address
    st.referrals["r1"] = vip.address
    st.referrals["r2"] = vip.address
    for i in range(5):
        st.soulbound["a" + str(i)] = {vip.address: time.time()}
    for i in range(5):
        st.curations["cu" + str(i)] = {"curator": vip.address, "owners": [vip.address]}
    st.fan_tokens["ft"] = {"holders": {vip.address: 500}, "creator": "0x0000",
                           "sold": 500, "supply": 10000, "price": 0.1}
    for i in range(5):
        st.graph_posts["p" + str(i)] = {"addr": vip.address, "likes": ["0x" + "1" * 40] * 6}

    rep = node.socialfi.reputation(vip.address)
    assert rep["score"] >= 80
    assert rep["fee_multiplier"] == 0.5

    vip_gas = node.gas_of(vip.address)
    normal_gas = node.gas_of(normal.address)
    assert vip_gas == pytest.approx(node.economy.FIXED_GAS * 0.5)
    assert normal_gas == node.economy.FIXED_GAS

    # 高声誉地址实际交易费减半
    bal0 = node.balances[normal.address]
    _apply(node, _signed_tx(normal, "nova:graph:follow", target="0x" + "2" * 40))
    assert node.balances[normal.address] == pytest.approx(bal0 - normal_gas)
    bal1 = node.balances[vip.address]
    _apply(node, _signed_tx(vip, "nova:graph:follow", target="0x" + "3" * 40))
    assert node.balances[vip.address] == pytest.approx(bal1 - vip_gas)


# ---------------------------------------------------------------------------
# 9. 创作者债券
# ---------------------------------------------------------------------------
def test_creator_bond_issue_buy_fund_redeem():
    node = _node()
    creator = QuantumWallet()
    investor = QuantumWallet()
    _fund(node, creator.address)
    _fund(node, investor.address)

    _apply(node, _signed_tx(creator, "nova:bond:issue", name="新专辑未来版税债券",
                            principal=1000, rate=0.08, term_days=365))
    bid = next(iter(node.store.bonds))
    b = node.store.bonds[bid]
    assert b["creator"] == creator.address

    _apply(node, _signed_tx(investor, "nova:bond:buy", amount=100, bid=bid))
    assert b["sold"][investor.address] == 100
    assert node.balances[creator.address] == pytest.approx(100000.0 + 100)

    _apply(node, _signed_tx(creator, "nova:bond:fund", amount=120, bid=bid))
    assert b["pool"] == 120

    # 未到期不能赎回
    assert not node.validate_tx(_signed_tx(investor, "nova:bond:redeem", bid=bid))
    b["matures_at"] = time.time() - 1
    bal0 = node.balances[investor.address]
    _apply(node, _signed_tx(investor, "nova:bond:redeem", bid=bid))
    assert node.balances[investor.address] == pytest.approx(bal0 + 100 * (1 + 0.08))
    assert b["settled"] is True


# ---------------------------------------------------------------------------
# 10. 碎片化 NFT 市场
# ---------------------------------------------------------------------------
def test_fractional_nft_split_buy():
    node = _node()
    owner = QuantumWallet()
    buyer = QuantumWallet()
    _fund(node, owner.address)
    _fund(node, buyer.address)

    _apply(node, _signed_tx(owner, "nova:frac:split", name="热门歌曲版权", nft_ref=_cid(9),
                            supply=10000, price_per=0.01))
    fid = next(iter(node.store.fractions))
    f = node.store.fractions[fid]
    assert f["supply"] == 10000 and f["owner_hold"] == 10000

    bal0 = node.balances[buyer.address]
    _apply(node, _signed_tx(buyer, "nova:frac:buy", amount=50, fid=fid, qty=5000))
    assert f["fractions"][buyer.address] == 5000
    assert f["owner_hold"] == 5000
    assert node.balances[buyer.address] == pytest.approx(bal0 - 50)
    # 超过持有量被拒绝
    assert not node.validate_tx(_signed_tx(buyer, "nova:frac:buy", amount=60, fid=fid, qty=6000))


# ---------------------------------------------------------------------------
# 状态持久化往返
# ---------------------------------------------------------------------------
def test_socialfi_state_roundtrip(tmp_path):
    sf = tmp_path / "sf_state.json"
    node = _node(state_file=str(sf))
    creator = QuantumWallet()
    _fund(node, creator.address)
    _apply(node, _signed_tx(creator, "nova:fan:issue", symbol="AAA", name="往返测试币",
                            supply=1000, price=0.1))
    node.save_state()

    node2 = _node(state_file=str(sf))
    assert len(node2.store.fan_tokens) == 1
    assert len(node2.store.socialfi_events) == 1
    tid = next(iter(node2.store.fan_tokens))
    assert node2.store.fan_tokens[tid]["voted"] == {}


# ---------------------------------------------------------------------------
# RPC 端点（HTTP 集成）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_socialfi_rpc_endpoints():
    node = _node()
    wallet = QuantumWallet()
    _fund(node, wallet.address)
    _fund_eco(node)

    app = web.Application(client_max_size=262144)
    setup_routes(app, node)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # 通用操作：签名 data 由客户端原样构造
        op = "nova:fan:issue"
        data = json.dumps(dict(op=op, symbol="RPC", name="RPC 粉丝币", supply=1000, price=0.1),
                          ensure_ascii=False)
        ts = int(time.time())
        tx = Tx(wallet.address, wallet.address, 0, [], data, wallet.public_key_hex(), "", timestamp=ts)
        tx.signature = wallet.sign(tx.signing_data())
        resp = await client.post("/api/socialfi", json={
            "addr": wallet.address, "amount": 0, "data": data, "timestamp": ts,
            "sender_public_key": wallet.public_key_hex(), "signature": tx.signature,
        })
        body = await resp.json()
        assert resp.status == 200 and body.get("id", "").startswith("fan_")

        # 领域读取
        resp = await client.get("/api/socialfi/fan")
        fan = await resp.json()
        assert len(fan) == 1

        resp = await client.get("/api/socialfi/overview")
        ov = await resp.json()
        assert ov["fan_tokens"] == 1

        resp = await client.get("/api/reputation/" + wallet.address)
        rep = await resp.json()
        assert "score" in rep and "fee_multiplier" in rep

        resp = await client.get("/api/graph/recommend/" + wallet.address)
        rec = await resp.json()
        assert "task_spec" in rec and "graph_hash" in rec
    finally:
        await client.close()