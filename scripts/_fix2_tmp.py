# -*- coding: utf-8 -*-
import io
p = r"C:\Users\Administrator\novachain\scripts\e2e_storage_incentive.py"
s = io.open(p, encoding="utf-8").read()
old = "    daemon.run_once(maintain=True)"
new = "    await asyncio.to_thread(daemon.run_once, True)  # 放到线程，避免阻塞 RPC 事件循环"
assert old in s, "pattern missing"
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8").write(s)
print("patched e2e")
