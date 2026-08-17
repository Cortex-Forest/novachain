# -*- coding: utf-8 -*-
"""审核回归测试：覆盖 P0/P1 修复点。"""
import asyncio
import hashlib
import json
import time

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.crypto import QuantumWallet
from core.transaction import Tx
from core.vm import deploy_address
from network.rpc import setup_routes
from nova_node import NovaNode


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9972)
    kw.setdefault("rpc", 8324)
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


# ---------------------------------------------------------------------------
# P0-1: 部署奖励必须由 creator 签名认领
# ---------------------------------------------------------------------------
async def test_deploy_requires_signed_creator():
    node = _node()
    _fund_eco(node)
    app = web.Application()
    setup_routes(app, node)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        w = QuantumWallet()
        bytecode = "let x = 1;\nreturn x;"

        res = await client.post("/api/deploy", json={"bytecode": bytecode, "creator": w.address})
        assert res.status == 400
        assert "签名" in (await res.json())["error"]

        addr = deploy_address(bytecode)
        sig = w.sign("deploy:{0}:{1}".format(addr, bytecode))
        res = await client.post("/api/deploy", json={
            "bytecode": bytecode, "creator": w.address,
            "sender_public_key": w.public_key_hex(), "signature": sig,
        })
        body = await res.json()
        assert res.status == 200
        assert body["reward"] == node.economy.deploy_reward()
        assert node.store.contract_code[addr] == [0x01, 1, 0x02, 0, 0x06, 0, 0x05]

        # 同一地址只能领一次部署奖励
        bytecode2 = "let y = 2;\nreturn y;"
        addr2 = deploy_address(bytecode2)
        sig2 = w.sign("deploy:{0}:{1}".format(addr2, bytecode2))
        res = await client.post("/api/deploy", json={
            "bytecode": bytecode2, "creator": w.address,
            "sender_public_key": w.public_key_hex(), "signature": sig2,
        })
        assert res.status == 400
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# P1-4: 计算市场过期/重复提交不产生额外发放
# ---------------------------------------------------------------------------
def test_compute_expired_task_no_double_payout():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for a in (creator, w1, w2):
        _fund(node, a.address)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=10.0, spec="task", expires_in=300))
    tid = next(iter(node.store.compute_tasks))
    _apply(node, _signed_tx(w1, "nova:compute:accept", task_id=tid))
    node.store.compute_tasks[tid]["expires_at"] = time.time() - 1  # 已过期但状态仍 open

    t1 = _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash="11" * 32)
    assert node.validate_tx(t1)
    node.apply_tx(t1)
    assert node.store.compute_tasks[tid]["status"] == "expired"

    # 过期后第二个提交被拒绝
    t2 = _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash="11" * 32)
    assert not node.validate_tx(t2)

    # 无人获得赏金；creator 已全额退回（扣除手续费）
    assert node.balances[w1.address] < 100000.0
    assert node.balances[w2.address] == 100000.0
    assert node.balances[creator.address] > 99999.0


def test_compute_market_complete_idempotent():
    node = _node()
    store = node.store
    for a in ("a", "b", "c", "d"):
        store.balances[a] = 100.0
    cm = node.compute_market
    cm.publish("a", "spec", 10.0, 3600, "t1")
    cm.accept("b", "t1")
    cm.accept("c", "t1")
    r0 = cm.submit("b", "t1", "11" * 32)
    assert r0["status"] == "open"
    r1 = cm.submit("c", "t1", "11" * 32)
    assert r1["status"] == "completed"
    before = store.balances["b"] + store.balances["c"]
    r2 = cm.submit("c", "t1", "22" * 32)
    assert r2["reward"] == 0.0
    assert store.balances["b"] + store.balances["c"] == before


# ---------------------------------------------------------------------------
# P1-6: 聊天 ack 必须由收件人签名
# ---------------------------------------------------------------------------
async def test_chat_ack_requires_signature():
    node = _node()
    app = web.Application()
    setup_routes(app, node)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        w = QuantumWallet()
        ids = ["ab" * 24]

        res = await client.post("/api/chat/ack", json={"addr": w.address, "ids": ids})
        assert res.status == 400

        sig_msg = "ack:" + w.address + ":" + json.dumps(sorted(set(ids)))
        res = await client.post("/api/chat/ack", json={
            "addr": w.address, "ids": ids,
            "sender_public_key": w.public_key_hex(), "signature": w.sign(sig_msg),
        })
        assert res.status == 200
        assert (await res.json())["removed"] == 0
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# P1-5: 存储模块守卫
# ---------------------------------------------------------------------------
def test_storage_module_guards():
    node = _node()
    sn = node.storage_net
    fake_addr = "0x" + "1" * 40
    fake_cid = "0x" + "a" * 64
    assert sn.claim(fake_addr, fake_cid, "0" * 64) is False
    assert "error" in sn.proof(fake_addr, fake_cid, "0" * 64)
    assert sn.pin(fake_addr, fake_cid, 1.0, 30) >= 0  # 生态基金不足时仍不抛异常


# ---------------------------------------------------------------------------
# P0-2: P2P 换行分帧支持大消息
# ---------------------------------------------------------------------------
async def test_p2p_framing_large_message_roundtrip():
    from network.p2p import _enc
    big = {"type": "state_snapshot", "payload": "x" * 200000}
    data = _enc(big)
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    reader.feed_data(data)
    reader.feed_eof()
    line = await reader.readuntil(b"\n")
    assert json.loads(line.strip()) == big

# ---------------------------------------------------------------------------
# P1-7: SocialFi ID 跨节点确定性（不得依赖墙钟时间）
# ---------------------------------------------------------------------------
def test_socialfi_ids_deterministic_across_nodes():
    w = QuantumWallet()
    payload = {"op": "nova:fan:issue", "symbol": "DET", "name": "Deterministic",
               "supply": 10000, "price": 0.5}
    data = json.dumps(payload, ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, 0, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())

    n1, n2 = _node(), _node()
    _fund(n1, w.address)
    _fund(n2, w.address)
    _apply(n1, tx)
    _apply(n2, tx)
    ids1 = set(n1.store.fan_tokens)
    ids2 = set(n2.store.fan_tokens)
    assert len(ids1) == 1 and ids1 == ids2

    for op, kw in [
        ("nova:rev:create", {"name": "R", "desc": "d"}),
        ("nova:ach:issue", {"title": "T", "badge": "B", "desc": "d"}),
        ("nova:market:create", {"question": "Q", "options": ["a", "b"], "closes_in": 3600}),
        ("nova:curate:create", {"title": "C", "items": ["a", "b"], "price": 1.0}),
        ("nova:bond:issue", {"name": "BD", "principal": 1000, "rate": 0.05, "term_days": 90}),
        ("nova:frac:split", {"nft_ref": "nft_1", "name": "F", "supply": 100, "price_per": 1.0}),
    ]:
        data2 = json.dumps({"op": op, **kw}, ensure_ascii=False)
        tx2 = Tx(w.address, w.address, 0, [], data2, w.public_key_hex(), "", timestamp=ts)
        tx2.signature = w.sign(tx2.signing_data())
        a1, a2 = _node(), _node()
        _fund(a1, w.address)
        _fund(a2, w.address)
        _apply(a1, tx2)
        _apply(a2, tx2)
        dom1 = set()
        for attr in ("revenue_shares", "achievements", "markets", "curations", "bonds", "fractions"):
            if getattr(a1.store, attr):
                dom1 |= set(getattr(a1.store, attr))
        dom2 = set()
        for attr in ("revenue_shares", "achievements", "markets", "curations", "bonds", "fractions"):
            if getattr(a2.store, attr):
                dom2 |= set(getattr(a2.store, attr))
        assert dom1 == dom2 and len(dom1) == 1, (op, dom1, dom2)


# ---------------------------------------------------------------------------
# H-1: AI 作品购买 / 一键触发必须从发起者余额扣款（防凭空铸币）
# ---------------------------------------------------------------------------
def test_h1_ai_work_buy_and_trigger_deduct_amount():
    node = _node()
    owner, ai, buyer = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (owner, ai, buyer):
        _fund(node, w.address)

    _apply(node, _signed_tx(ai, "nova:ai:register", name="AI 诗灵", owner=owner.address,
                            daily_budget=19.0, meta="model:novapoet-v1"))
    _apply(node, _signed_tx(ai, "nova:ai:work:create", title="夜航",
                            cid="bafy" + "a" * 46, price=10.0))
    wid = next(iter(node.store.ai_works))
    gas = node.gas_of(buyer.address)

    # 购买：买家扣 10 + gas；分账基于实际支付 70/20/10（不再凭空铸造）
    bal0 = node.balances[buyer.address]
    artist0 = node.balances[ai.address]
    fund0 = node.balances.get("0x_ai_growth_fund", 0.0)
    _apply(node, _signed_tx(buyer, "nova:ai:work:buy", amount=10.0, wid=wid))
    assert node.balances[buyer.address] == pytest.approx(bal0 - 10.0 - gas)
    assert node.balances[ai.address] == pytest.approx(artist0 + 7.0)
    assert node.balances.get("0x_ai_growth_fund", 0.0) == pytest.approx(fund0 + 1.0)

    # 一键触发：触发者扣 TRIGGER_FEE(2) + gas，AI 基金 +2
    bal0 = node.balances[buyer.address]
    fund0 = node.balances.get("0x_ai_growth_fund", 0.0)
    _apply(node, _signed_tx(buyer, "nova:ai:trigger", amount=2.0, service_type="suno"))
    assert node.balances[buyer.address] == pytest.approx(bal0 - 2.0 - node.gas_of(buyer.address))
    assert node.balances.get("0x_ai_growth_fund", 0.0) == pytest.approx(fund0 + 2.0)


# ---------------------------------------------------------------------------
# H-2: 收益共享 claim 不得重复领取抽干整池
# ---------------------------------------------------------------------------
def test_h2_rev_claim_cannot_drain_pool():
    node = _node()
    creator, b, c = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, b, c):
        _fund(node, w.address)
    _apply(node, _signed_tx(creator, "nova:rev:create", name="版税基金"))
    rid = next(iter(node.store.revenue_shares))
    _apply(node, _signed_tx(b, "nova:rev:invest", amount=60.0, rid=rid))   # 60%
    _apply(node, _signed_tx(c, "nova:rev:invest", amount=40.0, rid=rid))   # 40%
    _apply(node, _signed_tx(creator, "nova:rev:royalty", amount=1000.0, rid=rid))

    # B 反复领取：合计不得超过其公平份额 600（修复前会抽干整池 1000）
    for _ in range(6):
        tx = _signed_tx(b, "nova:rev:claim", rid=rid)
        if not node.validate_tx(tx):
            break
        node.apply_tx(tx)
    r = node.store.revenue_shares[rid]
    total_b = float(r.get("paid", {}).get(b.address, 0.0))
    assert total_b == pytest.approx(600.0)
    assert r["pool"] == pytest.approx(400.0)
    # C 仍可领取自己的 400
    _apply(node, _signed_tx(c, "nova:rev:claim", rid=rid))
    assert r["pool"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# H-3: 盲盒 EV 约束 + 禁自开 + 奖励由储备金支付（不铸币）
# ---------------------------------------------------------------------------
def test_h3_blindbox_no_mint_and_no_self_open():
    node = _node()
    creator, player = QuantumWallet(), QuantumWallet()
    for w in (creator, player):
        _fund(node, w.address)

    # EV(1000) > price(0.01) 的盲盒被拒绝（修复前可每开一次凭空铸 1000）
    bad_tiers = [{"name": "大奖", "weight": 1, "reward_type": "nova", "reward_amount": 1000.0}]
    assert not node.validate_tx(_signed_tx(creator, "nova:blind:create",
                                           name="x", commit="ab" * 32, price=0.01, tiers=bad_tiers))

    seed = "cd" * 32
    commit = hashlib.sha3_256(seed.encode()).hexdigest()
    tiers = [{"name": "奖", "weight": 1, "reward_type": "nova", "reward_amount": 5.0}]
    _apply(node, _signed_tx(creator, "nova:blind:create", name="y", price=10.0,
                            commit=commit, tiers=tiers, reserve=100.0))
    bid = next(iter(node.store.blindboxes))
    _apply(node, _signed_tx(creator, "nova:blind:reveal", bid=bid, seed=seed))
    box = node.store.blindboxes[bid]

    # 创建者不得自开
    assert not node.validate_tx(_signed_tx(creator, "nova:blind:open", amount=10.0, bid=bid, draws=1))

    # 玩家开盒：奖励从 reserve 支付（余额变化 = -10 - gas + 5），不再凭空铸造
    reserve0 = box["reserve"]
    bal0 = node.balances[player.address]
    _apply(node, _signed_tx(player, "nova:blind:open", amount=10.0, bid=bid, draws=1))
    assert node.balances[player.address] == pytest.approx(bal0 - 10.0 - node.gas_of(player.address) + 5.0)
    assert box["reserve"] == pytest.approx(reserve0 - 5.0)

    # 无储备金的盲盒：nova 奖励只付 0（不铸币）；由原创建者再发一个盒子，玩家来开
    _apply(node, _signed_tx(creator, "nova:blind:create", name="z", price=10.0,
                            commit=commit, tiers=tiers))
    bid2 = next(k for k, b in node.store.blindboxes.items() if b["name"] == "z")
    _apply(node, _signed_tx(creator, "nova:blind:reveal", bid=bid2, seed=seed))
    bal0 = node.balances[player.address]
    _apply(node, _signed_tx(player, "nova:blind:open", amount=10.0, bid=bid2, draws=1))
    assert node.balances[player.address] == pytest.approx(bal0 - 10.0 - node.gas_of(player.address))


# ---------------------------------------------------------------------------
# M-1: 算力争议驳回时只回补实际回拨额（防重复入账/铸币）
# ---------------------------------------------------------------------------
def _stake_validator(node, w, amt):
    ts = int(time.time())
    tx = Tx(w.address, w.address, amt, [], "nova:stake", w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    _apply(node, tx)


def _register_node(node, w):
    _apply(node, _signed_tx(w, "nova:compute:register", cpu_cores=8, gpu_model="RTX 4090",
                            gpu_vram_gb=24, ram_gb=64, storage_gb=200, region="cn-east", latency_ms=30))


def _stake_node(node, w, amt=200.0):
    _apply(node, _signed_tx(w, "nova:compute:stake", amount=amt))


def test_m1_compute_dispute_dismiss_restores_claw_only():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2):
        _fund(node, w.address)
    for w in (w1, w2):
        _register_node(node, w)
        _stake_node(node, w)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=20.0,
                            spec="超分", task_type="ai_image", mode="grab", expires_in=3600))
    tid = next(iter(node.store.compute_tasks))
    _apply(node, _signed_tx(w1, "nova:compute:accept", task_id=tid))
    _apply(node, _signed_tx(w2, "nova:compute:accept", task_id=tid))
    r = "dd" * 32
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash=r))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash=r))
    task = node.store.compute_tasks[tid]
    assert task["status"] == "completed"
    share = task["shares"][w1.address]
    # w1 在异议窗口内把报酬转走（余额压到 1），争议冻结只能回拨 1
    node.balances[w1.address] = 1.0
    _apply(node, _signed_tx(creator, "nova:compute:dispute", task_id=tid, reason="再验一次"))
    task = node.store.compute_tasks[tid]
    assert task["status"] == "disputed"
    clawed = task["clawed"][w1.address]
    assert clawed <= 1.0 and clawed < share
    voters = []
    for _ in range(3):
        v = QuantumWallet()
        _fund(node, v.address)
        _stake_validator(node, v, 1000)
        voters.append(v)
    for v in voters:
        _apply(node, _signed_tx(v, "nova:compute:vote", task_id=tid, support="dismiss"))
    node.compute_market._settle_disputes()
    task = node.store.compute_tasks[tid]
    assert task["status"] == "completed"
    # 修复后：w1 被冻结时余额归零，驳回后只恢复实际回拨的 clawed（< share），
    # 不会因完整 share 重复入账（修复前会凭空铸造 share-claw）
    assert node.balances[w1.address] == pytest.approx(clawed)
    assert node.balances[w1.address] < share


# ---------------------------------------------------------------------------
# M-3: 存储激励升级质押不得绕过 MAX_STAKE / MAX_TOTAL_STAKE
# ---------------------------------------------------------------------------
def test_m3_storage_upgrade_respects_stake_caps():
    node = _node()
    w = QuantumWallet()
    _fund(node, w.address)
    # 注册存储节点
    _apply(node, _signed_tx(w, "nova:storage:register", capacity_gb=100))
    # 已质押到 MAX_STAKE 时，升级被拒绝（校验层）
    node.store.stakes[w.address] = node.economy.MAX_STAKE
    assert not node.validate_tx(_signed_tx(w, "nova:storage:inc:upgrade", amount=1.0))
    # 模块级纵深防御：单地址超上限时 upgrade_quota 返回 0
    node.store.stakes[w.address] = node.economy.MAX_STAKE - 1
    node.balances[w.address] = 1000000.0
    assert node.storage_incentive.upgrade_quota(w.address, 100.0) == 0.0
    # 正常升级仍可用（未超限）
    node.store.stakes[w.address] = 100.0
    added = node.storage_incentive.upgrade_quota(w.address, 50.0)
    assert added > 0.0
    assert node.store.stakes[w.address] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# M-5: 成就颁发必须由创建者（issuer）执行
# ---------------------------------------------------------------------------
def test_m5_achievement_award_requires_issuer():
    node = _node()
    issuer, other, target = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (issuer, other, target):
        _fund(node, w.address)
    _apply(node, _signed_tx(issuer, "nova:ach:issue", title="签到365", desc="d", badge="🔥"))
    aid = next(iter(node.store.achievements))
    # 非创建者不能颁发（修复前任意地址可颁给自己刷声誉分）
    assert not node.validate_tx(_signed_tx(other, "nova:ach:award", aid=aid, target=target.address))
    # 创建者可颁发
    _apply(node, _signed_tx(issuer, "nova:ach:award", aid=aid, target=target.address))
    assert target.address in node.store.soulbound[aid]


# ---------------------------------------------------------------------------
# M-7: 内容自动固定应用每地址上限（防无限抽干生态基金）
# ---------------------------------------------------------------------------
def test_m7_content_pin_respects_per_addr_cap():
    from core.storage_network import MAX_PINS_PER_ADDR
    node = _node()
    w = QuantumWallet()
    _fund(node, w.address)
    _fund_eco(node)
    for i in range(MAX_PINS_PER_ADDR):
        node.store.storage_claims[f"0x{i:064x}"] = {"owner": w.address}
    cid = "0x" + "f" * 64
    assert node.socialfi._pin_content(w.address, cid, 1.0, 30) is False


# ---------------------------------------------------------------------------
# M-14: 普通转账扣费与校验一致（高信誉折扣不产生负余额）
# ---------------------------------------------------------------------------
def test_m14_normal_transfer_fee_matches_validation():
    node = _node()
    high_rep, receiver = QuantumWallet(), QuantumWallet()
    # 构造高信誉（>=80）发送者
    node.store.light_checkins[high_rep.address] = 270
    node.store.stakes[high_rep.address] = 1000
    for i in range(3):
        node.store.contract_creator["0x" + ("c%d" % i * 20)] = high_rep.address
    for i in range(2):
        node.store.referrals["0x" + ("r%d" % i * 20)] = high_rep.address
    node.store.text_reputation[high_rep.address] = 500
    rep = node.socialfi.reputation(high_rep.address)
    assert rep["score"] >= 80, rep
    gas_of = node.gas_of(high_rep.address)
    assert gas_of < node.economy.FIXED_GAS
    amount = 10.0
    # 余额介于 amount+gas_of 与 amount+FIXED_GAS 之间：应通过校验且扣费后不为负
    node.balances[high_rep.address] = amount + gas_of + 1e-6
    node.balances[receiver.address] = 0.0
    ts = int(time.time())
    tx = Tx(high_rep.address, receiver.address, amount, [], "",
            high_rep.public_key_hex(), "", timestamp=ts)
    tx.signature = high_rep.sign(tx.signing_data())
    assert node.validate_tx(tx)
    node.apply_tx(tx)
    assert node.balances[high_rep.address] >= 0.0
    assert node.balances[receiver.address] == pytest.approx(amount)


# ---------------------------------------------------------------------------
# M-2: 算力争议投票排除利益相关方（发起者 / 已获报酬工人）
# ---------------------------------------------------------------------------
def test_m2_compute_dispute_vote_excludes_stakeholders():
    node = _node()
    creator, w1, w2 = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, w1, w2):
        _fund(node, w.address)
    for w in (w1, w2):
        _register_node(node, w)
        _stake_node(node, w)
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=20.0,
                            spec="超分", task_type="ai_image", mode="grab", expires_in=3600))
    tid = next(iter(node.store.compute_tasks))
    _apply(node, _signed_tx(w1, "nova:compute:accept", task_id=tid))
    _apply(node, _signed_tx(w2, "nova:compute:accept", task_id=tid))
    r = "dd" * 32
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash=r))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash=r))
    _apply(node, _signed_tx(creator, "nova:compute:dispute", task_id=tid, reason="复查"))
    # 利益相关方（发起者 / 已获报酬的工人）不得投票
    assert not node.validate_tx(_signed_tx(creator, "nova:compute:vote", task_id=tid, support="dismiss"))
    assert not node.validate_tx(_signed_tx(w1, "nova:compute:vote", task_id=tid, support="dismiss"))
    assert not node.validate_tx(_signed_tx(w2, "nova:compute:vote", task_id=tid, support="dismiss"))
    # 独立验证者可投票
    v = QuantumWallet()
    _fund(node, v.address)
    _stake_validator(node, v, 1000)
    assert node.validate_tx(_signed_tx(v, "nova:compute:vote", task_id=tid, support="dismiss"))


# ---------------------------------------------------------------------------
# M-4: 存储激励奖励按「证明时点」assigned_gb 快照计酬（防先证明后认领虚增）
# ---------------------------------------------------------------------------
def test_m4_inc_reward_uses_proof_time_snapshot():
    node = _node()
    n1 = QuantumWallet()
    _fund(node, n1.address)
    _fund_eco(node)
    node.storage_incentive.auto_register(n1.address, 100.0)
    node.store.inc_nodes[n1.address]["assigned_gb"] = 100.0   # 结算前被虚增到 100GB
    node.store.inc_nodes[n1.address]["proof_assigned_gb"] = 10.0  # 证明时点只有 10GB
    reward = node.storage_incentive.daily_reward(node.store.inc_nodes[n1.address])
    assert reward == pytest.approx(10.0 * 1.0 / 30.0)  # 按快照 10GB，而非 100GB


# ---------------------------------------------------------------------------
# M-6: 预测市场 oracle / 创建者不得自我下注（防内幕套利）
# ---------------------------------------------------------------------------
def test_m6_market_oracle_and_creator_cannot_bet():
    node = _node()
    creator, oracle, alice = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (creator, oracle, alice):
        _fund(node, w.address)
    _apply(node, _signed_tx(creator, "nova:market:create", question="Q?",
                            options=["a", "b"], closes_in=3600, oracle=oracle.address))
    mid = next(iter(node.store.markets))
    assert not node.validate_tx(_signed_tx(oracle, "nova:market:bet", amount=10, mid=mid, option=0))
    assert not node.validate_tx(_signed_tx(creator, "nova:market:bet", amount=10, mid=mid, option=0))
    _apply(node, _signed_tx(alice, "nova:market:bet", amount=10, mid=mid, option=0))


# ---------------------------------------------------------------------------
# M-8: 文本合约密钥由稳定种子派生（不再依赖易变余额状态）
# ---------------------------------------------------------------------------
def test_m8_text_key_stable_across_balance_state():
    n1, n2 = _node(), _node()
    # 两个节点余额状态不同（n2 额外注入两笔余额），但文本合约公钥必须一致
    n2.balances["0x" + "a" * 40] = 12345.0
    n2.balances["0x" + "b" * 40] = 67890.0
    pk1 = n1.socialfi.text_contract_pubkey()
    pk2 = n2.socialfi.text_contract_pubkey()
    assert pk1 == pk2
    assert pk1.startswith("04")  # P-256 未压缩公钥


# ---------------------------------------------------------------------------
# M-9: 仲裁翻案资金守恒（追回金额按二次结果重新入账，不再蒸发）
# ---------------------------------------------------------------------------
def test_m9_arb_revert_conserves_funds():
    node = _node()
    buyer, seller = QuantumWallet(), QuantumWallet()
    _fund(node, buyer.address, 10000.0)
    _fund(node, seller.address, 10000.0)
    _fund_eco(node)
    # 首次卖家胜已执行：卖家收到冻结 20 + 投诉保证金 40% 份额 8，生态基金收 60% 份额 12
    case = {
        "id": "case_t1", "buyer": buyer.address, "seller": seller.address,
        "result": "seller", "seller_frozen": 20.0,
        "payouts": {"first_seller": {"to_seller_frozen": 20.0, "to_seller_share": 8.0, "to_eco": 12.0}},
    }
    node.balances[seller.address] += 20.0 + 8.0
    node.balances[node.economy.ECOSYSTEM_FUND] += 12.0
    before_total = sum(node.balances.values()) + sum(node.store.arb_pools.values())
    # 二次仲裁翻案为买家胜
    node.arbitration._revert_and_repay(case, "buyer")
    after_total = sum(node.balances.values()) + sum(node.store.arb_pools.values())
    # 资金守恒：翻案前后总量一致（修复前卖家份额被追回后从未入账 → 蒸发）
    assert after_total == pytest.approx(before_total)
    # 买家拿回冻结保证金 + 卖家此前分得的投诉保证金份额
    assert node.balances[buyer.address] == pytest.approx(10000.0 + 20.0 + 8.0)


# ---------------------------------------------------------------------------
# M-11: 存储维护类 op 每地址每类每日限频 + 仅存储节点可触发
# ---------------------------------------------------------------------------
def test_m11_inc_maintain_op_daily_limit():
    node = _node()
    w, other = QuantumWallet(), QuantumWallet()
    _fund(node, w.address)
    _fund(node, other.address)
    _apply(node, _signed_tx(w, "nova:storage:register", capacity_gb=100))
    # 首次触发允许
    assert node.validate_tx(_signed_tx(w, "nova:storage:inc:settle"))
    _apply(node, _signed_tx(w, "nova:storage:inc:settle"))
    # 同日再次触发被拒（每地址每类每日一次）
    assert not node.validate_tx(_signed_tx(w, "nova:storage:inc:settle"))
    # 非存储节点不能触发
    assert not node.validate_tx(_signed_tx(other, "nova:storage:inc:settle"))
