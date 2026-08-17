# PoC: governance delegation vote-power amplification
import sys, time
sys.path.insert(0, ".")
from core.storage import StateStore
from core.economy import Economy
from core.governance import Governance

store = StateStore("genesis.json")
eco = Economy(store)
gov = Governance(store, eco)

# A has 1000 NOVA balance
store.balances["0x" + "a"*40] = 1000.0
A = "0x" + "a"*40
B = "0x" + "b"*40
C = "0x" + "c"*40

print("power(A) before:", gov.voting_power(A))
# A delegates to B, B delegates to C
store.gov_delegations[A] = B
store.gov_delegations[B] = C
print("power(A) after deleg:", gov.voting_power(A))
print("power(B):", gov.voting_power(B))
print("power(C):", gov.voting_power(C))
print("sum(A+B+C):", gov.voting_power(A)+gov.voting_power(B)+gov.voting_power(C))
print("circulating (true supply):", gov.circulating_supply())
