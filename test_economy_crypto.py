"""Unit tests for core.crypto, core.storage, core.economy and stake/state logic in NovaNode."""
import json
import os
import tempfile
import time

import pytest

from core.crypto import (QuantumWallet, ed25519_verify, verify_quantum_tx,
                         oqs)
from core.economy import Economy
from core.storage import StateStore
from core.transaction import Tx
from nova_node import NovaNode


class FakeTime:
    def __init__(self, now):
        self._now = now

    def time(self):
        return self._now


def _node(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("p2p", 9980)
    kw.setdefault("rpc", 8300)
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
# core.crypto
# ---------------------------------------------------------------------------

def test_ed25519_verify_rejects_bad_lengths():
    pub = bytes.fromhex(QuantumWallet().public_key_hex())
    sig = b"\x01" * 64
    assert ed25519_verify(b"\x01" * 33, b"m", sig) is False   # pub 长度错
    assert ed25519_verify(pub, b"m", b"\x02" * 63) is False   # sig 长度错


def test_ed25519_verify_rejects_invalid_point():
    pub = bytes.fromhex(QuantumWallet().public_key_hex())
    sig = (2).to_bytes(32, "little") + b"\x00" * 32  # r 点不在曲线上 → 内部抛错
    assert ed25519_verify(pub, b"m", sig) is False


def test_ed25519_verify_rejects_tampered_message():
    wallet = QuantumWallet()
    sig = bytes.fromhex(wallet.sign("hello"))
    assert ed25519_verify(wallet.pk, b"hello", sig) is True
    assert ed25519_verify(wallet.pk, b"hello!", sig) is False


def test_wallet_deterministic_from_seed():
    seed = "ab" * 32
    w1 = QuantumWallet(seed)
    w2 = QuantumWallet(seed)
    assert w1.address == w2.address
    assert w1.public_key_hex() == w2.public_key_hex()
    assert w1.private_key_hex() == seed
    assert w1.algorithm == "Ed25519"  # 未安装 oqs 时回退


def test_wallet_accepts_bytes_seed():
    w = QuantumWallet(bytes.fromhex("cd" * 32))
    assert w.private_key_hex() == "cd" * 32


def test_wallet_address_format():
    w = QuantumWallet()
    assert w.address.startswith("0x")
    assert len(w.address) == 42
    int(w.address[2:], 16)  # 合法 hex


def test_verify_quantum_tx_rejects_bad_pub_length():
    w = QuantumWallet()
    sig = w.sign("data")
    assert verify_quantum_tx("data", sig, "ab" * 33, w.address) is False


def test_verify_quantum_tx_dilithium_length_without_oqs():
    if oqs is not None:
        pytest.skip("本环境安装了 oqs")
    pub = "ab" * 1296  # 2592 字节公钥（Dilithium5 长度）
    assert verify_quantum_tx("data", "00" * 32, pub, "0x" + "ab" * 40) is False


def test_verify_quantum_tx_rejects_invalid_hex():
    w = QuantumWallet()
    assert verify_quantum_tx("data", "zz", w.public_key_hex(), w.address) is False
    assert verify_quantum_tx("data", w.sign("data"), "zz", w.address) is False


def test_verify_quantum_tx_rejects_wrong_claimed_address():
    w = QuantumWallet()
    sig = w.sign("data")
    assert verify_quantum_tx("data", sig, w.public_key_hex(), "0x" + "00" * 40) is False


# ---------------------------------------------------------------------------
# core.storage
# ---------------------------------------------------------------------------

def test_storage_load_missing_file_returns_false():
    store = StateStore("genesis.json")
    assert store.load(os.path.join(tempfile.gettempdir(), "no-such-state-12345.json")) is False


def test_storage_load_corrupt_json_returns_false():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "bad.json")
        with open(p, "w") as f:
            f.write("{not json")
        store = StateStore("genesis.json")
        assert store.load(p) is False


def test_storage_missing_genesis_is_tolerated():
    store = StateStore("no_such_genesis_abc.json")  # 不应抛异常
    assert store.balances == {}


def test_storage_full_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        store = StateStore("genesis.json")
        store.balances.clear()
        store.balances["0xa"] = 1.5
        store.contracts["0xc"] = "code"
        store.contract_creator["0xc"] = "0xa"
        store.stakes["0xa"] = 500
        store.unbonding["0xa"] = (50, 123.0)
        store.dag.update(["t1", "t2"])
        store.deploy_count = 3
        store.referral_issued = 4
        store.call_count = 5
        store.referrals["0xb"] = "0xa"
        store.referral_claimed.add("0xb")
        store.light_verifications["0xb"] = 2
        store.verified_txids.add("t1")
        store.light_verify_last["0xb"] = "2026-08-01"
        store.call_reward_dates["k"] = "2026-08-01"
        store.presale_verified["0xa"] = "0xbsc"
        store.miner_registry["0xa"] = 1.0
        store.miner_uptime["0xa"] = 100.0
        store.miner_qualified.add("0xa")
        store.light_checkins["0xb"] = 5
        store.light_checkin_dates["0xb"] = {"2026-08-01", "2026-08-02"}
        store.light_qualified.add("0xb")
        store.early_airdrop_received.add("0xa")
        store.locked_balances["0xa"] = {"amount": 100, "start_time": 1, "unlocked": 0}
        store.early_rewards_paid.add("0xa")
        store.jailed["0xa"] = 99.0
        store.save(path)

        store2 = StateStore("genesis.json")
        assert store2.load(path)
        assert store2.balances == {"0xa": 1.5}
        assert store2.contracts == {"0xc": "code"}
        assert store2.contract_creator == {"0xc": "0xa"}
        assert store2.stakes == {"0xa": 500.0}
        assert store2.unbonding == {"0xa": (50.0, 123.0)}
        assert store2.dag == {"t1", "t2"}
        assert store2.deploy_count == 3
        assert store2.referral_issued == 4
        assert store2.call_count == 5
        assert store2.referrals == {"0xb": "0xa"}
        assert store2.referral_claimed == {"0xb"}
        assert store2.light_verifications == {"0xb": 2}
        assert store2.verified_txids == {"t1"}
        assert store2.light_verify_last == {"0xb": "2026-08-01"}
        assert store2.call_reward_dates == {"k": "2026-08-01"}
        assert store2.presale_verified == {"0xa": "0xbsc"}
        assert store2.miner_registry == {"0xa": 1.0}
        assert store2.miner_uptime == {"0xa": 100.0}
        assert store2.miner_qualified == {"0xa"}
        assert store2.light_checkins == {"0xb": 5}
        assert store2.light_checkin_dates == {"0xb": {"2026-08-01", "2026-08-02"}}
        assert store2.light_qualified == {"0xb"}
        assert store2.early_airdrop_received == {"0xa"}
        assert store2.locked_balances == {"0xa": {"amount": 100, "start_time": 1, "unlocked": 0}}
        assert store2.early_rewards_paid == {"0xa"}
        assert store2.jailed == {"0xa": 99.0}


# ---------------------------------------------------------------------------
# core.economy
# ---------------------------------------------------------------------------

def _economy(monkeypatch, now=None):
    store = StateStore("genesis.json")
    if now is not None:
        monkeypatch.setattr("core.economy.time", FakeTime(now))
    return Economy(store), store


def test_block_reward_halving(monkeypatch):
    for k in range(11):
        now = Economy.GENESIS_TIME + k * Economy.HALVING
        eco, _ = _economy(monkeypatch, now)
        expected = Economy.INIT_REWARD / (2 ** min(k, Economy.MAX_HALVINGS))
        assert eco.block_reward() == pytest.approx(expected)


def test_block_reward_before_genesis(monkeypatch):
    eco, _ = _economy(monkeypatch, Economy.GENESIS_TIME - 100)
    assert eco.block_reward() == Economy.INIT_REWARD * 2  # h = -1


def test_deploy_reward_halving_and_floor(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.deploy_count = Economy.DEPLOY_HALVING_STEP * 1
    assert eco.deploy_reward() == Economy.INIT_DEPLOY_REWARD / 2
    store.deploy_count = Economy.DEPLOY_HALVING_STEP * 3
    assert eco.deploy_reward() == Economy.INIT_DEPLOY_REWARD / 8
    store.deploy_count = Economy.DEPLOY_HALVING_STEP * 20
    assert eco.deploy_reward() == Economy.MIN_DEPLOY_REWARD


def test_referral_reward_halving_and_floor(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.referral_issued = Economy.REFERRAL_HALVING_STEP * 1
    assert eco.referral_reward() == Economy.INIT_REFERRAL_REWARD / 2
    store.referral_issued = Economy.REFERRAL_HALVING_STEP * 20
    assert eco.referral_reward() == Economy.MIN_REFERRAL_REWARD


def test_call_reward_halving_and_floor(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.call_count = Economy.CALL_HALVING_STEP * 2
    assert eco.call_reward() == Economy.INIT_CALL_REWARD / 4
    store.call_count = Economy.CALL_HALVING_STEP * 50
    assert eco.call_reward() == Economy.MIN_CALL_REWARD


def test_light_verify_reward_tracks_block_reward(monkeypatch):
    now = Economy.GENESIS_TIME + 2 * Economy.HALVING
    eco, _ = _economy(monkeypatch, now)
    assert eco.light_verify_reward() == eco.block_reward()


def test_effective_and_total_stake(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.stakes.update({"a": 100, "b": 99999, "c": 0})
    assert eco.effective_stake("b") == Economy.MAX_STAKE
    assert eco.effective_stake("c") == 0
    assert eco.total_stake() == 100 + Economy.MAX_STAKE


def test_distribute_noop_without_stakes(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.balances.clear()
    eco.distribute(10)
    assert store.balances == {}


def test_distribute_proportional(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.stakes.update({"a": 100, "b": 300})
    eco.distribute(40)
    assert store.balances["a"] == pytest.approx(10)
    assert store.balances["b"] == pytest.approx(30)


def test_early_airdrop_duplicate_rejected(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.balances[Economy.ECOSYSTEM_FUND] = 1000
    assert eco.early_airdrop("0xa", "miner") is True
    assert eco.early_airdrop("0xa", "miner") is False


def test_early_airdrop_miner_cap(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.balances[Economy.ECOSYSTEM_FUND] = 1000
    store.miner_registry = {f"m{i}": 1.0 for i in range(81)}
    assert eco.early_airdrop("0xa", "miner") is False


def test_early_airdrop_light_cap(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.balances[Economy.ECOSYSTEM_FUND] = 1000
    store.light_checkins = {f"l{i}": 1 for i in range(8100)}
    assert eco.early_airdrop("0xa", "light") is False


def test_early_airdrop_insufficient_fund(monkeypatch):
    eco, store = _economy(monkeypatch)
    store.balances[Economy.ECOSYSTEM_FUND] = Economy.AIRDROP_AMOUNT - 1
    assert eco.early_airdrop("0xa", "miner") is False


def test_early_airdrop_success(monkeypatch):
    now = 1000000.0
    eco, store = _economy(monkeypatch, now)
    store.balances[Economy.ECOSYSTEM_FUND] = 1000
    assert eco.early_airdrop("0xa", "miner") is True
    assert store.balances[Economy.ECOSYSTEM_FUND] == 900
    assert store.locked_balances["0xa"]["amount"] == Economy.AIRDROP_AMOUNT
    assert store.locked_balances["0xa"]["start_time"] == now
    assert "0xa" in store.early_airdrop_received


def test_check_unlock_no_lock(monkeypatch):
    eco, store = _economy(monkeypatch, 1000000.0)
    assert eco.check_unlock("0xnobody") == 0


def test_check_unlock_before_duration(monkeypatch):
    now = 1000000.0
    eco, store = _economy(monkeypatch, now)
    store.locked_balances["0xa"] = {"amount": 100, "start_time": now - 100, "unlocked": 0}
    assert eco.check_unlock("0xa") == 0


def test_check_unlock_partial_then_exhausted(monkeypatch):
    now = 1000000.0
    eco, store = _economy(monkeypatch, now)
    store.locked_balances["0xa"] = {
        "amount": 100,
        "start_time": now - Economy.LOCK_DURATION - 3 * 30 * 86400,
        "unlocked": 0,
    }
    first = eco.check_unlock("0xa")
    assert first == pytest.approx(30)             # 3 个月 × 10%
    assert store.balances["0xa"] == pytest.approx(30)
    assert eco.check_unlock("0xa") == 0           # 已解锁部分不重复发放


def test_check_unlock_full_after_ten_months(monkeypatch):
    now = 1000000.0
    eco, store = _economy(monkeypatch, now)
    store.locked_balances["0xa"] = {
        "amount": 100,
        "start_time": now - Economy.LOCK_DURATION - 10 * 30 * 86400,
        "unlocked": 0,
    }
    assert eco.check_unlock("0xa") == pytest.approx(100)  # 10 个月 × 10% 封顶 100%


def test_release_early_rewards_before_release_time(monkeypatch):
    eco, store = _economy(monkeypatch, Economy.RELEASE_TIME - 1)
    store.miner_qualified.add("0xm")
    store.balances[Economy.ECOSYSTEM_FUND] = 10000
    eco.release_early_rewards()
    assert store.balances.get("0xm", 0) == 0
    assert store.early_rewards_paid == set()


def test_release_early_rewards_after_release_time(monkeypatch):
    eco, store = _economy(monkeypatch, Economy.RELEASE_TIME + 100)
    store.miner_qualified.add("0xm")
    store.light_qualified.add("0xl")
    store.balances[Economy.ECOSYSTEM_FUND] = 10000
    eco.release_early_rewards()
    assert store.balances["0xm"] == Economy.EARLY_MINER_REWARD
    assert store.balances["0xl"] == Economy.EARLY_LIGHT_REWARD
    assert store.early_rewards_paid == {"0xm", "0xl"}
    fund = store.balances[Economy.ECOSYSTEM_FUND]
    assert fund == 10000 - Economy.EARLY_MINER_REWARD - Economy.EARLY_LIGHT_REWARD
    eco.release_early_rewards()  # 不重复发放
    assert store.balances["0xm"] == Economy.EARLY_MINER_REWARD


def test_release_early_rewards_skips_when_fund_low(monkeypatch):
    eco, store = _economy(monkeypatch, Economy.RELEASE_TIME + 100)
    store.miner_qualified.add("0xm")
    store.light_qualified.add("0xl")
    store.balances[Economy.ECOSYSTEM_FUND] = Economy.EARLY_LIGHT_REWARD  # 不够矿工奖励
    eco.release_early_rewards()
    assert store.balances.get("0xm", 0) == 0
    assert store.balances["0xl"] == Economy.EARLY_LIGHT_REWARD


# ---------------------------------------------------------------------------
# NovaNode：质押校验 / 奖励 / 状态
# ---------------------------------------------------------------------------

def test_validate_tx_mint_from_zero_address():
    node = _node()
    tx = Tx("0x0000", "0xabc", 100, [], "mint")
    assert node.validate_tx(tx) is True  # 系统铸币交易短路


def test_validate_tx_zero_amount_to_contract():
    node = _node()
    wallet = QuantumWallet()
    node.balances[wallet.address] = 10
    node.contracts["0xcontract"] = "code"
    tx = _signed_tx(wallet, "0xcontract", 0)
    assert node.validate_tx(tx) is True


def test_validate_tx_rejects_zero_amount_to_plain_addr():
    node = _node()
    wallet = QuantumWallet()
    node.balances[wallet.address] = 10
    assert node.validate_tx(_signed_tx(wallet, "0xbob", 0)) is False


def test_validate_tx_unstake_limits():
    node = _node()
    wallet = QuantumWallet()
    node.balances[wallet.address] = 10000
    node.store.stakes[wallet.address] = 1000
    assert node.validate_tx(_signed_tx(wallet, wallet.address, 0, "nova:unstake")) is False
    assert node.validate_tx(_signed_tx(wallet, wallet.address, 1001, "nova:unstake")) is False
    # 冷却中总量限制：250 已处于 25% 上限 → 再解 1 被拒
    node.store.unbonding[wallet.address] = (250, 9999999999.0)
    assert node.validate_tx(_signed_tx(wallet, wallet.address, 1, "nova:unstake")) is False
    node.store.unbonding[wallet.address] = (0, 9999999999.0)
    assert node.validate_tx(_signed_tx(wallet, wallet.address, 100, "nova:unstake")) is True


def test_validate_tx_claim_timing():
    node = _node()
    wallet = QuantumWallet()
    node.balances[wallet.address] = 10000
    node.store.unbonding[wallet.address] = (50, time.time() + 1000)  # 未到期
    assert node.validate_tx(_signed_tx(wallet, wallet.address, 0, "nova:claim")) is False
    node.store.unbonding[wallet.address] = (50, time.time() - 1)     # 已到期
    assert node.validate_tx(_signed_tx(wallet, wallet.address, 0, "nova:claim")) is True


def test_apply_tx_referral_reward_once():
    node = _node()
    sender = QuantumWallet()
    invitee = "0xinvitee"
    referrer = "0xreferrer"
    node.balances[sender.address] = 1000
    node.balances[Economy.COMMUNITY_AIRDROP] = 10
    node.store.referrals[invitee] = referrer
    node.apply_tx(_signed_tx(sender, invitee, 5))
    assert node.balances[invitee] == 5
    assert node.balances[referrer] == Economy.INIT_REFERRAL_REWARD
    assert invitee in node.store.referral_claimed
    assert node.store.referral_issued == 1
    # 二次收款不再发推荐奖励
    node.apply_tx(_signed_tx(sender, invitee, 1))
    assert node.balances[referrer] == Economy.INIT_REFERRAL_REWARD


def test_apply_tx_call_reward_daily_once():
    node = _node()
    sender = QuantumWallet()
    creator = "0xcreator"
    contract = "0xcontract"
    node.balances[sender.address] = 1000
    node.balances[Economy.ECOSYSTEM_FUND] = 10
    node.contracts[contract] = "code"
    node.store.contract_creator[contract] = creator
    node.apply_tx(_signed_tx(sender, contract, 1))
    assert node.balances[creator] == pytest.approx(Economy.INIT_CALL_REWARD)
    assert node.store.call_count == 1
    node.apply_tx(_signed_tx(sender, contract, 1))
    assert node.balances[creator] == pytest.approx(Economy.INIT_CALL_REWARD)  # 同日不重复


def test_apply_tx_distributes_validator_pool():
    node = _node()
    sender = QuantumWallet()
    node.balances[sender.address] = 1000
    node.store.stakes.update({"a": 100, "b": 300})
    reward = node.economy.block_reward() + node.economy.FIXED_GAS
    node.balances[Economy.VALIDATOR_POOL] = reward
    node.apply_tx(_signed_tx(sender, "0xbob", 1))
    assert node.balances[Economy.VALIDATOR_POOL] == 0
    assert node.balances["a"] == pytest.approx(reward * 0.25)
    assert node.balances["b"] == pytest.approx(reward * 0.75)


def test_apply_tx_stake_op_basic():
    node = _node()
    wallet = QuantumWallet()
    node.balances[wallet.address] = 10000
    node.apply_tx(_signed_tx(wallet, wallet.address, 500, "nova:stake"))
    assert node.store.stakes[wallet.address] == 500
    assert node.balances[wallet.address] == 10000 - 500 - Economy.FIXED_GAS
    assert wallet.address in node.store.miner_registry  # 首次质押注册矿工


def test_daily_maintenance_accrues_uptime():
    node = _node()
    addr = "0xminer"
    node.store.miner_registry[addr] = time.time()
    node.store.miner_uptime[addr] = 269 * 86400
    node._run_daily_maintenance()
    assert node.store.miner_uptime[addr] == 270 * 86400
    assert addr in node.store.miner_qualified


def test_state_save_load_roundtrip(tmp_path):
    state_path = str(tmp_path / "state.json")
    node = _node(state_file=state_path)
    wallet = QuantumWallet()
    node.balances[wallet.address] = 42
    node.store.stakes[wallet.address] = 100
    node.save_state()
    assert os.path.exists(state_path)
    node2 = _node(state_file=state_path)
    assert node2.balances.get(wallet.address) == 42
    assert node2.store.stakes.get(wallet.address) == 100


def test_full_snapshot_structure():
    node = _node()
    snap = node.full_snapshot()
    assert set(snap) == {"version", "saved_at", "state", "security", "consensus"}
    assert node.apply_snapshot(snap) is True


def test_apply_snapshot_rejects_bad_data():
    node = _node()
    assert node.apply_snapshot({"state": 123}) is False