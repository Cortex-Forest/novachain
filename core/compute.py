# -*- coding: utf-8 -*-
"""去中心化计算网络（算力任务市场 + 双节点冗余验证）。

对应《链上新增功能》第二阶段「算力任务市场」：
- 任务发起者发布计算任务并悬赏 NOVA（悬赏金进入链上托管）。
- 提供算力的节点“抢单”接受任务，在本地运算后提交结果哈希。
- 双节点冗余验证：任意两个不同节点提交相同结果哈希即视为验证通过，
  两个节点各获得悬赏金的一半；其余节点提交结果不再发放。
- 任务到期仍未完成时，悬赏金全额退回发起者。
"""
import re
import time

RESULT_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_WORKERS = 8
MAX_SPEC_LEN = 4096
MIN_EXPIRES = 300                     # 任务最短有效期：5 分钟
MAX_EXPIRES = 90 * 86400              # 任务最长有效期：90 天


class ComputeMarket:
    def __init__(self, store, economy):
        self.store = store
        self.economy = economy

    def publish(self, creator: str, spec: str, bounty: float, expires_in: float, task_id: str):
        self.store.balances[creator] -= float(bounty)   # 悬赏托管
        self.store.compute_tasks[task_id] = {
            "creator": creator,
            "spec": spec,
            "bounty": float(bounty),
            "status": "open",
            "accepted": [],
            "results": {},
            "created_at": time.time(),
            "expires_at": time.time() + float(expires_in),
        }

    def accept(self, worker: str, task_id: str) -> bool:
        task = self.store.compute_tasks[task_id]
        if task["status"] != "open" or worker in task["accepted"]:
            return False
        if len(task["accepted"]) >= MAX_WORKERS:
            return False
        task["accepted"].append(worker)
        return True

    def submit(self, worker: str, task_id: str, result_hash: str) -> dict:
        task = self.store.compute_tasks[task_id]
        if task["status"] != "open" or worker not in task["accepted"] or worker in task["results"]:
            return {"status": task.get("status", "unknown"), "reward": 0.0}
        rh = result_hash.lower()
        task["results"][worker] = rh
        for other, h in task["results"].items():
            if other != worker and h == rh:
                return self._complete(task, worker, other)
        return {"status": task["status"], "reward": 0.0}

    def _complete(self, task: dict, w1: str, w2: str) -> dict:
        if task.get("status") == "completed":
            return {"status": "completed", "reward": 0.0, "workers": [w1, w2]}
        task["status"] = "completed"
        each = round(task["bounty"] / 2, 8)
        self.store.balances[w1] = self.store.balances.get(w1, 0) + each
        self.store.balances[w2] = self.store.balances.get(w2, 0) + each
        refund = round(task["bounty"] - each * 2, 8)
        if refund > 0:
            self.store.balances[task["creator"]] = self.store.balances.get(task["creator"], 0) + refund
        return {"status": "completed", "reward": each, "workers": [w1, w2]}

    def expire(self, task_id: str) -> bool:
        task = self.store.compute_tasks.get(task_id)
        if not task or task["status"] != "open" or time.time() <= task["expires_at"]:
            return False
        task["status"] = "expired"
        self.store.balances[task["creator"]] = self.store.balances.get(task["creator"], 0) + task["bounty"]
        return True

    def expire_all(self) -> int:
        n = 0
        for tid in list(self.store.compute_tasks):
            if self.expire(tid):
                n += 1
        return n
