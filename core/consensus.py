import asyncio
import hashlib
import time

from core.blockchain import Block
from core.crypto import verify_quantum_tx


class ConsensusEngine:
    """PoS / checkpoint 双模式共识引擎。

    checkpoint（默认，兼容旧行为）：任意节点可出块，仅校验 prev_hash/高度。
    pos：按有效质押（effective_stake，封顶 MAX_STAKE）加权抽签选出每高度出块者，
         出块者用验证者私钥签名区块，其他节点校验签名与出块权。

    简化约定：
    - epoch 质押快照：在每个 epoch 边界（height % epoch_len == 0）从本地 stake 状态重建；
      质押变更以签名交易形式经区块封存，节点间通过广播/状态快照保持一致。
    - bootstrap：全网无质押时，任何持有验证者密钥的节点均可出块（仍需签名）。
    - 活性兜底：当选出块者离线超过 proposer_timeout（默认 2 个出块周期）时，
      任意有质押的节点可补块，避免链停滞。
    """

    MAX_BLOCK_TXS = 2000

    POS_SLASH_MISS_THRESHOLD = 3  # 连续错过出块窗口达到该次数才惩罚当选者（H-03：防补块恶意惩罚）

    def __init__(self, node, block_interval=60, mode="checkpoint", epoch_len=10800, proposer_timeout=None):
        self.node = node
        self.block_interval = block_interval
        self.mode = mode
        self.epoch_len = max(1, int(epoch_len))
        self.proposer_timeout = proposer_timeout or (2 * block_interval)
        self.chain = []
        self.epoch_stakes = {}

    def chain_height(self):
        return len(self.chain)

    def latest_checkpoint(self):
        return self.chain[-1].hash if self.chain else None

    def sealed_txids(self):
        sealed = set()
        for b in self.chain:
            sealed.update(b.txids)
        return sealed

    # ---------- PoS 出块权 ----------
    def _refresh_epoch_stakes(self, height=None):
        """在 epoch 边界重建质押快照（按 MAX_STAKE 封顶，排除被惩罚禁用的地址）。"""
        if height is None:
            height = self.chain_height()
        self.epoch_stakes = {}
        for addr, stake in self.node.store.stakes.items():
            if not stake or stake <= 0:
                continue
            if self.node.store.jailed.get(addr, 0) > height:
                continue
            self.epoch_stakes[addr] = min(stake, self.node.economy.MAX_STAKE)

    def elect_proposer(self, height, prev_hash):
        """按质押加权抽签：以 prev_hash+height 为确定性种子。无质押返回 None（bootstrap）。"""
        total = sum(self.epoch_stakes.values())
        if total <= 0:
            return None
        seed = hashlib.sha3_256(f"{prev_hash}{height}".encode()).digest()
        r = int.from_bytes(seed, "big") % total
        for addr, stake in sorted(self.epoch_stakes.items()):
            r -= stake
            if r < 0:
                return addr
        return sorted(self.epoch_stakes.keys())[-1]

    def _is_staked(self, addr):
        return self.epoch_stakes.get(addr, 0) > 0

    # ---------- 出块 ----------
    def produce_block(self):
        if self.mode == "pos":
            return self._produce_block_pos()
        return self._produce_block_checkpoint()

    def _produce_block_checkpoint(self):
        pending = [t for t in self.node.dag if t not in self.sealed_txids()]
        if not pending:
            return None
        prev = self.chain[-1].hash if self.chain else Block.GENESIS_PREV
        block = Block(height=len(self.chain), txids=pending[: self.MAX_BLOCK_TXS],
                      prev_hash=prev, proposer=self.node.node_id)
        self.chain.append(block)
        return block

    def _produce_block_pos(self):
        validator = getattr(self.node, "validator", None)
        if validator is None:
            return None
        pending = [t for t in self.node.dag if t not in self.sealed_txids()]
        if not pending:
            return None
        height = len(self.chain)
        prev = self.chain[-1].hash if self.chain else Block.GENESIS_PREV
        if height % self.epoch_len == 0:
            self._refresh_epoch_stakes(height)
        elected = self.elect_proposer(height, prev)
        last_ts = self.chain[-1].timestamp if self.chain else 0
        fallback = bool(self.chain) and time.time() - last_ts >= self.proposer_timeout
        if elected is not None and elected != validator.address and not fallback:
            return None  # 非当选出块者，等待当选者出块
        block = Block(height=height, txids=pending[: self.MAX_BLOCK_TXS],
                      prev_hash=prev, proposer=validator.address,
                      proposer_pubkey=validator.public_key_hex())
        block.signature = validator.sign(block.hash)
        self.chain.append(block)
        return block

    # ---------- 采用 ----------
    def adopt_block(self, block) -> bool:
        if not isinstance(block, Block):
            return False
        if block.height < len(self.chain):
            if self.mode == "pos":
                self._detect_equivocation(block)
            return False
        if block.height == len(self.chain):
            prev = self.chain[-1].hash if self.chain else Block.GENESIS_PREV
            if block.prev_hash != prev:
                return False
            if self.mode == "pos":
                ok, elected = self._verify_pos_block(block)
                if not ok:
                    return False
                self.chain.append(block)
                if elected is not None and block.proposer != elected:
                    # 回退补块：仅当当选者连续错过多个窗口才惩罚（H-03，防单次网络延迟被恶意补块罚没）
                    missed = int(self.node.store.pos_missed.get(elected, 0)) + 1
                    self.node.store.pos_missed[elected] = missed
                    if missed >= self.POS_SLASH_MISS_THRESHOLD:
                        self._slash(elected, self.node.economy.INACTIVITY_SLASH_RATIO,
                                    "连续出块超时", block.height)
                        self.node.store.pos_missed[elected] = 0
                return True
            self.chain.append(block)
            return True
        return False

    def _verify_pos_block(self, block):
        """校验 PoS 区块，返回 (是否采纳, 该高度当选者地址)。"""
        if not self._valid_signature(block):
            return False, None
        height = block.height
        if height % self.epoch_len == 0:
            self._refresh_epoch_stakes(height)
        elected = self.elect_proposer(height, block.prev_hash)
        if elected is None:
            return True, None  # bootstrap：无质押时任意签名有效的验证者均可出块
        if block.proposer == elected:
            self.node.store.pos_missed.pop(elected, None)  # 当选者正常出块，清零缺失计数
            return True, elected
        prev_ts = self.chain[-1].timestamp if self.chain else 0
        fallback = bool(self.chain) and (block.timestamp - prev_ts) >= self.proposer_timeout
        if fallback and self._is_staked(block.proposer):
            return True, elected
        return False, elected

    def _valid_signature(self, block) -> bool:
        return bool(block.proposer and block.proposer_pubkey and block.signature
                    and verify_quantum_tx(block.hash, block.signature, block.proposer_pubkey, block.proposer))

    def _slash(self, addr, ratio, reason, height):
        """惩罚：按比例扣减质押（最低 1 NOVA）并禁用出块权（jail N 个 epoch）。"""
        stake = self.node.store.stakes.get(addr, 0)
        if stake <= 0:
            return
        amount = min(stake, max(1.0, stake * ratio))
        self.node.store.stakes[addr] = stake - amount
        if self.node.store.stakes[addr] <= 0:
            del self.node.store.stakes[addr]
        jail_until = height + self.epoch_len * self.node.economy.JAIL_EPOCHS
        self.node.store.jailed[addr] = max(self.node.store.jailed.get(addr, 0), jail_until)
        print(f"[SLASH] {reason}: {addr[:12]}... -{amount:.4f} NOVA, 禁用出块权至高度 {jail_until}")

    def _detect_equivocation(self, block):
        """双签检测：同一出块者对同一高度签署两个不同区块 → 本地惩罚（尽力而为，随状态快照同步）。"""
        try:
            existing = self.chain[block.height]
        except IndexError:
            return
        if not (existing.proposer and existing.proposer == block.proposer and existing.hash != block.hash):
            return
        if not self._valid_signature(block):
            return
        self._slash(block.proposer, self.node.economy.EQUIVOCATION_SLASH_RATIO, "双签", self.chain_height())

    async def checkpoint_loop(self):
        while True:
            await asyncio.sleep(self.block_interval)
            try:
                block = self.produce_block()
                if block:
                    await self.node.p2p.gossip({"type": "new_block", "block": block.to_dict()})
                    print(f"[BLOCK] #{block.height} {block.hash[:16]}... {len(block.txids)} txs (proposer={block.proposer[:12]}...)")
            except Exception as e:
                print(f"[BLOCK] 出块失败: {e}")

    def snapshot(self):
        return {
            "chain": [b.to_dict() for b in self.chain],
            "epoch_stakes": self.epoch_stakes,
            "epoch_len": self.epoch_len,
        }

    def restore(self, d):
        self.chain = [Block.from_dict(b) for b in d.get("chain", [])]
        es = d.get("epoch_stakes")
        if isinstance(es, dict):
            self.epoch_stakes = {k: float(v) for k, v in es.items()}
        else:
            self._refresh_epoch_stakes()
