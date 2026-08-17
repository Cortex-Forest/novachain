import sys, time, json
sys.path.insert(0, ".")
from core.storage import StateStore
from core.economy import Economy
from core.oracle import Oracle

store = StateStore("genesis.json")
eco = Economy(store)
o = Oracle(store, eco)

att = "0x" + "a"*40
store.balances[att] = 1000.0
store.oracle_nodes[att] = {"addr": att, "pubkey": "0x" + "1"*128, "stake": 500.0,
                           "status": "active", "registered_at": time.time(),
                           "fulfills": 0, "updates": 0, "ai_verified": 0}

def tx(d):
    class T: pass
    t = T()
    t.sender = att; t.receiver = att; t.amount = 0; t.data = json.dumps(d)
    t.txid = f"t{time.time_ns()}"
    return t

for src in ("chainlink", "pyth", "binance"):
    d = {"op": "nova:oracle:price:update", "feed": "USDT/USD", "source": src, "price": 0.0001}
    assert o.validate_op(tx(d)), f"price validate failed for {src}"
    o.apply_op(tx(d))
agg = store.oracle_feeds.get("USDT/USD")
print("aggregated USDT/USD after attacker 3-source report:", agg["price"] if agg else None)

# bridge impact: nUSDT usd value at manipulated price
from core.bridge import Bridge
b = Bridge(store, eco, o)
print("bridge _usd_value(50000 nUSDT) with manipulated feed:", b._usd_value("nUSDT", 50000.0))
print("=> daily limit 1,000,000 USD now allows 50000 nUSDT to count as", b._usd_value("nUSDT", 50000.0), "USD")
