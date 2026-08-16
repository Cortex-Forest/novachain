# -*- coding: utf-8 -*-
import io
p = r"C:\Users\Administrator\novachain\test_storage_incentive.py"
s = io.open(p, encoding="utf-8").read()
old = """    _apply(node, _signed_tx(n1, "nova:storage:inc:prove", day=day,
                            files=ch["files"], fragments=[wrong.hex()]))
    assert node.store.inc_nodes[n1.address]["fail_count"] == 1
    assert node.store.inc_nodes[n1.address]["last_proof_epoch"] != day"""
new = """    _apply(node, _signed_tx(n1, "nova:storage:inc:prove", day=day,
                            files=ch["files"], fragments=[wrong.hex()]))
    assert node.store.inc_nodes[n1.address]["fail_count"] == 1
    assert node.store.inc_nodes[n1.address]["last_proof_epoch"] != day
    # 同一周期内多次失败尝试只计一次；结算也不再重复累计
    _apply(node, _signed_tx(n1, "nova:storage:inc:prove", day=day,
                            files=ch["files"], fragments=[wrong.hex()]))
    assert node.store.inc_nodes[n1.address]["fail_count"] == 1
    node.storage_incentive.settle_epoch(day)
    assert node.store.inc_nodes[n1.address]["fail_count"] == 1"""
assert old in s, "pattern missing"
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8").write(s)
print("test updated")
