import sys, time, json
sys.path.insert(0, ".")
from core.storage import StateStore
from core.economy import Economy
from core.oracle import Oracle
from core.bridge import Bridge

store = StateStore("genesis.json")
eco = Economy(store)
o = Oracle(store, eco)
b = Bridge(store, eco, o)

# commit a feed directly
store.oracle_feeds["USDT/USD"] = {"feed": "USDT/USD", "price": 1.0, "ts": time.time()}
try:
    v = b._usd_value("nUSDT", 100.0)
    print("usd_value:", v)
except TypeError as e:
    print("TYPE ERROR (bridge broken):", e)

# via validate path
att = "0x" + "a"*40
store.balances[att] = 2000.0
store.bridge_nodes[att] = {"addr": att, "stake": 1000.0, "status": "active", "registered_at": time.time(), "signed": 0}
class T: pass
t = T()
t.sender = att; t.receiver = att; t.amount = 0
t.data = json.dumps({"op": "nova:bridge:deposit", "asset": "nUSDT", "source_chain": "bsc",
                     "source_tx": "11"*32, "source_addr": "0x"+"e"*40, "amount": 50000.0})
t.txid = "t1"
print("deposit validate with live feed:", b.validate_op(t))
