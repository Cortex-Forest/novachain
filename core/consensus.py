import hashlib
import time
import asyncio

class ConsensusEngine:
    def __init__(self, node):
        self.node = node
        self.checkpoints = []

    async def checkpoint_loop(self):
        while True:
            await asyncio.sleep(60)
            if self.node.dag:
                cp = hashlib.sha3_256(str(sorted(self.node.dag)).encode()).hexdigest()
                self.checkpoints.append(cp)
                print(f"[CHECKPOINT] {cp[:16]}...")

    def latest_checkpoint(self):
        return self.checkpoints[-1] if self.checkpoints else None