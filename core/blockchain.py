import hashlib
import time


class Block:
    """区块：交易集合 + 前块哈希 + 出块者，形成哈希链。

    PoS 模式下出块者还需提供公钥与签名（签名对区块哈希生效，不进哈希）。
    """

    GENESIS_PREV = "0" * 64

    def __init__(self, height, txids, prev_hash=None, proposer="", timestamp=None,
                 proposer_pubkey="", signature=""):
        self.height = height
        self.txids = sorted(set(txids))
        self.prev_hash = prev_hash or self.GENESIS_PREV
        self.proposer = proposer
        self.timestamp = time.time() if timestamp is None else timestamp
        self.proposer_pubkey = proposer_pubkey
        self.signature = signature
        self.hash = self.calc_hash()

    def calc_hash(self):
        raw = f"{self.height}{self.timestamp}{self.prev_hash}{self.txids}{self.proposer}{self.proposer_pubkey}"
        return hashlib.sha3_256(raw.encode()).hexdigest()

    def to_dict(self):
        return {
            "height": self.height,
            "txids": self.txids,
            "prev_hash": self.prev_hash,
            "proposer": self.proposer,
            "timestamp": self.timestamp,
            "hash": self.hash,
            "proposer_pubkey": self.proposer_pubkey,
            "signature": self.signature,
        }

    @staticmethod
    def from_dict(d):
        block = Block(
            height=d.get("height", 0),
            txids=d.get("txids", []),
            prev_hash=d.get("prev_hash"),
            proposer=d.get("proposer", ""),
            timestamp=d.get("timestamp"),
            proposer_pubkey=d.get("proposer_pubkey", ""),
            signature=d.get("signature", ""),
        )
        return block
