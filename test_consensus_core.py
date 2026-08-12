"""Unit tests for core.blockchain.Block and core.consensus.ConsensusEngine."""
import asyncio
import time

from core.blockchain import Block
from core.consensus import ConsensusEngine
from core.crypto import QuantumWallet
from core.economy import Economy
from nova_node import NovaNode


def _pos_node(seed_hex=None, epoch_len=10800, block_interval=60, proposer_timeout=None):
    node = NovaNode(host="127.0.0.1", p2p=9970, rpc=8290, use_tls=False, state_file=None,
                    consensus_mode="pos", validator_key=seed_hex, epoch_len=epoch_len,
                    block_interval=block_interval)
    if proposer_timeout is not None:
        node.consensus.proposer_timeout = proposer_timeout
    return node


def _signed_block(node, wallet, height, txids, prev_hash=None, timestamp=None):
    block = Block(height=height, txids=txids, prev_hash=prev_hash, proposer=wallet.address,
                  proposer_pubkey=wallet.public_key_hex(), timestamp=timestamp)
    block.signature = wallet.sign(block.hash)
    return block


# ---------------------------------------------------------------------------
# Block serialization / hashing
# ---------------------------------------------------------------------------

def test_block_txids_sorted_and_deduped():
    b = Block(height=3, txids=["b", "a", "b", "c"])
    assert b.txids == ["a", "b", "c"]


def test_block_hash_excludes_signature():
    b1 = Block(height=1, txids=["t"], proposer="alice", signature="s1", timestamp=1000.0)
    b2 = Block(height=1, txids=["t"], proposer="alice", signature="s2", timestamp=1000.0)
    assert b1.hash == b2.hash  # 签名不参与哈希
    b3 = Block(height=2, txids=["t"], proposer="alice", timestamp=1000.0)
    assert b1.hash != b3.hash


def test_block_roundtrip():
    b = Block(height=7, txids=["x", "y"], prev_hash="ab" * 32, proposer="node",
              timestamp=1234567890.5, proposer_pubkey="pk", signature="sig")
    r = Block.from_dict(b.to_dict())
    assert r.height == b.height
    assert r.txids == b.txids
    assert r.prev_hash == b.prev_hash
    assert r.proposer == b.proposer
    assert r.timestamp == b.timestamp
    assert r.proposer_pubkey == b.proposer_pubkey
    assert r.signature == b.signature
    assert r.hash == b.hash


def test_block_from_dict_defaults():
    r = Block.from_dict({})
    assert r.height == 0
    assert r.txids == []
    assert r.prev_hash == Block.GENESIS_PREV
    assert r.proposer == ""
    assert r.signature == ""


# ---------------------------------------------------------------------------
# Checkpoint mode
# ---------------------------------------------------------------------------

def test_adopt_block_rejects_non_block():
    node = NovaNode(host="127.0.0.1", p2p=9971, rpc=8291, use_tls=False, state_file=None)
    assert node.consensus.adopt_block({"height": 0}) is False
    assert node.consensus.adopt_block(None) is False


def test_adopt_block_rejects_future_height():
    node = NovaNode(host="127.0.0.1", p2p=9972, rpc=8292, use_tls=False, state_file=None)
    b = Block(height=5, txids=["t"])
    assert node.consensus.adopt_block(b) is False
    assert node.consensus.chain_height() == 0


def test_produce_block_caps_txs():
    node = NovaNode(host="127.0.0.1", p2p=9973, rpc=8293, use_tls=False, state_file=None)
    node.store.dag.update([f"tx{i}" for i in range(2500)])
    b = node.consensus.produce_block()
    assert b is not None
    assert len(b.txids) == ConsensusEngine.MAX_BLOCK_TXS
    assert len(node.consensus.sealed_txids()) == ConsensusEngine.MAX_BLOCK_TXS


def test_snapshot_restore_roundtrip():
    node = NovaNode(host="127.0.0.1", p2p=9974, rpc=8294, use_tls=False, state_file=None)
    node.store.dag.update(["a", "b"])
    node.consensus.produce_block()
    node.consensus.epoch_stakes = {"0xabc": 500.0}
    snap = node.consensus.snapshot()

    node2 = NovaNode(host="127.0.0.1", p2p=9975, rpc=8295, use_tls=False, state_file=None)
    node2.consensus.restore(snap)
    assert node2.consensus.chain_height() == 1
    assert node2.consensus.latest_checkpoint() == node.consensus.latest_checkpoint()
    assert node2.consensus.epoch_stakes == {"0xabc": 500.0}
    assert node2.consensus.epoch_len == node.consensus.epoch_len


def test_snapshot_restore_refreshes_epoch_stakes():
    node = NovaNode(host="127.0.0.1", p2p=9976, rpc=8296, use_tls=False, state_file=None)
    node.store.stakes["0xabc"] = 300
    node.consensus.restore({"chain": [], "epoch_len": 10})  # epoch_stakes 非 dict
    assert node.consensus.epoch_stakes == {"0xabc": 300.0}


# ---------------------------------------------------------------------------
# PoS: epoch stakes, election, block production
# ---------------------------------------------------------------------------

def test_refresh_epoch_stakes_filters():
    node = _pos_node(QuantumWallet().private_key_hex())
    node.store.stakes.update({"a": 100, "b": 0, "c": -5, "d": 20000, "e": 500})
    node.store.jailed["e"] = 1000
    node.consensus._refresh_epoch_stakes(height=500)
    assert set(node.consensus.epoch_stakes) == {"a", "d"}
    assert node.consensus.epoch_stakes["d"] == Economy.MAX_STAKE  # 封顶 10000


def test_elect_proposer_deterministic_and_staked():
    node = _pos_node(QuantumWallet().private_key_hex())
    node.store.stakes.update({"a": 100, "b": 200})
    node.consensus._refresh_epoch_stakes()
    first = node.consensus.elect_proposer(1, "0" * 64)
    again = node.consensus.elect_proposer(1, "0" * 64)
    assert first == again
    assert node.consensus._is_staked(first)
    other = node.consensus.elect_proposer(1, "1" * 64)
    assert other in ("a", "b")


def test_elect_proposer_bootstrap_returns_none():
    node = _pos_node(QuantumWallet().private_key_hex())
    assert node.consensus.elect_proposer(0, Block.GENESIS_PREV) is None


def test_pos_produce_block_non_elected_waits():
    va = QuantumWallet()
    vb = QuantumWallet()
    node = _pos_node(va.private_key_hex())
    node.store.stakes[va.address] = 1
    node.store.stakes[vb.address] = 10000
    node.consensus._refresh_epoch_stakes()
    node.store.dag.add("tx1")
    elected = node.consensus.elect_proposer(0, Block.GENESIS_PREV)
    if elected == vb.address:
        assert node.consensus.produce_block() is None  # 非当选者等待
    else:
        b = node.consensus.produce_block()
        assert b is not None and b.proposer == va.address


def test_pos_produce_block_epoch_boundary_refresh():
    node = _pos_node(QuantumWallet().private_key_hex(), epoch_len=2)
    node.store.dag.add("tx0")
    node.store.stakes[node.validator.address] = 100
    node.consensus._refresh_epoch_stakes(0)
    node.consensus.epoch_stakes.clear()
    node.consensus._produce_block_pos()  # height=0 → 0 % 2 == 0 → 重建快照
    assert node.consensus.epoch_stakes


def test_pos_produce_block_requires_validator():
    node = NovaNode(host="127.0.0.1", p2p=9977, rpc=8297, use_tls=False, state_file=None,
                    consensus_mode="pos")
    node.store.dag.add("tx1")
    assert node.consensus.produce_block() is None


# ---------------------------------------------------------------------------
# PoS: block adoption / signatures / slashing
# ---------------------------------------------------------------------------

def test_pos_adopt_bootstrap_block_without_stakes():
    node = _pos_node(QuantumWallet().private_key_hex())
    w = QuantumWallet()
    b = _signed_block(node, w, 0, ["t0"])
    assert node.consensus.adopt_block(b) is True  # 无质押 → bootstrap


def test_pos_adopt_rejects_bad_signature():
    node = _pos_node(QuantumWallet().private_key_hex())
    node.store.stakes[node.validator.address] = 100
    node.consensus._refresh_epoch_stakes()
    b = Block(height=0, txids=["t0"], proposer=node.validator.address,
              proposer_pubkey=node.validator.public_key_hex())
    b.signature = "00" * 64  # 伪造签名
    assert node.consensus.adopt_block(b) is False


def test_pos_adopt_rejects_wrong_proposer_no_fallback():
    node = _pos_node(QuantumWallet().private_key_hex())
    node.store.stakes[node.validator.address] = 100
    node.consensus._refresh_epoch_stakes()
    w2 = QuantumWallet()
    b = _signed_block(node, w2, 0, ["t0"])  # 非当选者，且无超时回退
    assert node.consensus.adopt_block(b) is False


def test_pos_adopt_fallback_by_staked_proposer_after_timeout():
    va = QuantumWallet()
    vb = QuantumWallet()
    node = _pos_node(va.private_key_hex(), block_interval=10, proposer_timeout=20)
    node.store.stakes[va.address] = 100
    node.store.stakes[vb.address] = 100
    node.consensus._refresh_epoch_stakes()
    elected0 = node.consensus.elect_proposer(0, Block.GENESIS_PREV)
    b0 = _signed_block(node, va if elected0 == va.address else vb, 0, ["t0"])
    assert node.consensus.adopt_block(b0) is True
    elected1 = node.consensus.elect_proposer(1, b0.hash)
    fb = vb if elected1 == va.address else va
    b1 = _signed_block(node, fb, 1, ["t1"], prev_hash=b0.hash, timestamp=b0.timestamp + 100)
    assert node.consensus.adopt_block(b1) is True
    assert node.consensus.chain_height() == 2


def test_pos_adopt_rejects_unstaked_fallback():
    va = QuantumWallet()
    node = _pos_node(va.private_key_hex(), block_interval=10, proposer_timeout=20)
    node.store.stakes[va.address] = 100
    node.consensus._refresh_epoch_stakes()
    b0 = _signed_block(node, va, 0, ["t0"])
    assert node.consensus.adopt_block(b0) is True
    stranger = QuantumWallet()
    b1 = _signed_block(node, stranger, 1, ["t1"], prev_hash=b0.hash, timestamp=b0.timestamp + 100)
    assert node.consensus.adopt_block(b1) is False  # 无质押者不能回退补块


def test_slash_zero_stake_is_noop():
    node = _pos_node(QuantumWallet().private_key_hex())
    node.consensus._slash("0xghost", 0.01, "test", 1)
    assert node.store.stakes.get("0xghost", 0) == 0
    assert node.store.jailed == {}


def test_slash_min_one_nova_floor():
    node = _pos_node(QuantumWallet().private_key_hex())
    node.store.stakes["0xsmall"] = 1  # 1% < 1 NOVA 下限 → 扣 1 NOVA
    node.consensus._slash("0xsmall", 0.01, "test", 5)
    assert "0xsmall" not in node.store.stakes  # 扣至 0 被移除
    assert node.store.jailed["0xsmall"] > 5


def test_equivocation_negative_height_does_not_crash():
    node = _pos_node(QuantumWallet().private_key_hex())
    node.store.dag.add("t0")
    b0 = node.consensus.produce_block()
    assert b0 is not None
    bad = Block(height=-1, txids=["t-1"])
    assert node.consensus.adopt_block(bad) is False  # IndexError 分支安全处理


# ---------------------------------------------------------------------------
# checkpoint_loop
# ---------------------------------------------------------------------------

async def test_checkpoint_loop_gossips_new_block():
    node = NovaNode(host="127.0.0.1", p2p=9978, rpc=8298, use_tls=False, state_file=None,
                    block_interval=0.01)
    node.store.dag.add("tx1")
    sent = []

    async def fake_gossip(msg, exclude=None):
        sent.append(msg)

    node.p2p.gossip = fake_gossip
    task = asyncio.create_task(node.consensus.checkpoint_loop())
    try:
        for _ in range(200):
            if sent:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("checkpoint_loop 未广播区块")
    finally:
        task.cancel()
    assert sent[0]["type"] == "new_block"
    assert node.consensus.chain_height() == 1