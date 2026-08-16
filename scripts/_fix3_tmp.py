# -*- coding: utf-8 -*-
import io
p = r"C:\Users\Administrator\novachain\scripts\e2e_storage_incentive.py"
s = io.open(p, encoding="utf-8").read()
old = "    assert res[\"rewards_paid\"] == 1.0 / 30.0"
new = "    assert abs(res[\"rewards_paid\"] - 1.0 / 30.0) < 1e-6"
assert old in s, "pattern missing"
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8").write(s)
print("patched assert")
