# -*- coding: utf-8 -*-
"""F-01..F-06 修复核验：通过真实节点管线（validate_tx -> apply_tx）重放攻击路径。
运行：python "najnomics audit/08-reports/VERIFY_FIXES.py"
"""
import json, time, sys
sys.path.insert(0, ".")
from core.crypto import QuantumWallet
from core.transaction import Tx
from nova_node import NovaNode

PASS, FAIL = 0, 0

def node():
    return NovaNode(host="127.0.0.1", p2p=9963, rpc=8315, use_tls=False, state_file=None)

def fund(n, addr, amt=100000.0):
    n.balances[addr] = amt

def signed(n, w, op, amount=0.0, data=None, **kw):
    payload = {"op": op}
    if data: payload.update(data)
    if amount: payload["amount"] = amount
    payload.update(kw)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], json.dumps(payload, ensure_ascii=False),
            w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx

def apply(n, tx):
    assert n.validate_tx(tx), "validate failed: " + tx.data[:120]
    n.apply_tx(tx)

def check(name, ok):
    global PASS, FAIL
    if ok: PASS += 1; print(f"  [PASS] {name}")
    else: FAIL += 1; print(f"  [FAIL] {name}")

print("== F-01 桥：女巫多签无储备铸造 ==")
n = node()
nodes = []
for i in range(3):
    w = QuantumWallet(); fund(n, w.address); apply(n, signed(n, w, "nova:bridge:node:register", amount=1000))
    nodes.append(w)
user = QuantumWallet()
apply(n, signed(n, nodes[0], "nova:bridge:deposit", data={"amount": 1000.0},
                asset="nUSDT", source_chain="bsc", source_tx="ab"*32, source_addr=user.address))
did = next(reversed(n.store.bridge_deposits))
# 原攻击形态 1：新注册节点立即签名 -> 最小年龄拦截
check("新注册节点立即签名被拒", not n.validate_tx(signed(n, nodes[1], "nova:bridge:deposit:sign",
      deposit_id=did, source_tx="ab"*32, source_addr=user.address, source_amount=1000.0)))
for w in nodes:  # 回拨注册时间，满足最小年龄
    n.store.bridge_nodes[w.address]["registered_at"] = time.time() - 7200
# 原攻击形态 2：无观察字段盲签名 -> 拦截
check("无观察字段盲签名被拒", not n.validate_tx(signed(n, nodes[1], "nova:bridge:deposit:sign", deposit_id=did)))
# 原攻击形态 3：观察不一致 -> 拦截
check("观察字段不一致被拒", not n.validate_tx(signed(n, nodes[1], "nova:bridge:deposit:sign",
      deposit_id=did, source_tx="cd"*32, source_addr=user.address, source_amount=1000.0)))
# 合法路径：观察一致且节点满足年龄 -> 3/3 达成并铸造
for w in nodes[1:3]:
    apply(n, signed(n, w, "nova:bridge:deposit:sign", deposit_id=did,
                    source_tx="ab"*32, source_addr=user.address, source_amount=1000.0))
dep = n.store.bridge_deposits[did]
check("合法多签后状态 ready 并铸造", dep["status"] in ("ready", "minted"))
apply(n, signed(n, nodes[0], "nova:bridge:deposit:claim", deposit_id=did))
check("铸造成功（合法路径仍可用）", n.store.bridge_assets["nUSDT"]["supply"] > 0)

print("== F-02 存储：pin 无限锁定基金 / 假 PoSt ==")
n = node()
eco_fund = n.economy.ECOSYSTEM_FUND
n.balances[eco_fund] = 10000.0
att = QuantumWallet(); fund(n, att.address)
cid1, cid2 = "0x" + "1"*64, "0x" + "2"*64
r = n.storage_net.pin_reward(1024.0, 3650)
check("大额 pin 第 1 个通过（<=5000/地址）", n.validate_tx(signed(n, att, "nova:storage:pin", cid=cid1, size_gb=1024.0, duration_days=3650)))
apply(n, signed(n, att, "nova:storage:pin", cid=cid1, size_gb=1024.0, duration_days=3650))
check("第 2 个同地址 pin 超 5000 上限被拒", not n.validate_tx(signed(n, att, "nova:storage:pin", cid=cid2, size_gb=1024.0, duration_days=3650)))
# 基金余额不足时 pin 被拒
n.balances[eco_fund] = 1.0
check("基金余额不足时 pin 被拒", not n.validate_tx(signed(n, att, "nova:storage:pin", cid=cid2, size_gb=1024.0, duration_days=3650)))
n.balances[eco_fund] = 10000.0
prov = QuantumWallet(); fund(n, prov.address)
apply(n, signed(n, prov, "nova:storage:register", capacity_gb=4096))
# 自认领拦截
seal = n.storage_net.make_chain("s"+cid1, 365)[-1]
check("固定者自认领被拒", not n.validate_tx(signed(n, att, "nova:storage:claim", cid=cid1, seal=seal)))
check("其他注册提供商可认领（合法路径可用）", n.validate_tx(signed(n, prov, "nova:storage:claim", cid=cid1, seal=seal)))

print("== F-03 预言机：单节点女巫多源 ==")
n = node()
from core.oracle import vrf_keygen
def orego(w):
    _, pub = vrf_keygen()
    apply(n, signed(n, w, "nova:oracle:node:register", amount=500, pubkey=pub))
a = QuantumWallet(); fund(n, a.address); orego(a)
b = QuantumWallet(); fund(n, b.address); orego(b)
apply(n, signed(n, a, "nova:oracle:price:update", feed="USDT/USD", source="chainlink", price=100.0))
check("同一节点上报第二个源被拒", not n.validate_tx(signed(n, a, "nova:oracle:price:update", feed="USDT/USD", source="pyth", price=101.0)))
check("其他节点接管已有源被拒", not n.validate_tx(signed(n, b, "nova:oracle:price:update", feed="USDT/USD", source="chainlink", price=99.0)))
apply(n, signed(n, b, "nova:oracle:price:update", feed="USDT/USD", source="pyth", price=101.0))
check("双独立节点聚合价正常", abs(n.store.oracle_feeds["USDT/USD"]["price"] - 100.5) < 1e-9)

print("== F-05 桥：_usd_value dict 契约 ==")
n = node()
n.store.oracle_feeds["USDT/USD"] = {"feed": "USDT/USD", "price": 1.0, "ts": time.time()}
v = n.bridge._usd_value("nUSDT", 100.0)
check(f"_usd_value 返回数值 {v}", isinstance(v, (int, float)) and v == 100.0)

print("== F-06 治理：委托投票权放大 ==")
n = node()
wallets = []
for amt in (1000, 1000, 1000, 500):
    w = QuantumWallet(); fund(n, w.address, amt); wallets.append(w)
a, b, c, d = wallets
apply(n, signed(n, a, "nova:gov:delegate", to=b.address))
apply(n, signed(n, b, "nova:gov:delegate", to=c.address))
pa, pb, pc = n.governance.voting_power(a.address), n.governance.voting_power(b.address), n.governance.voting_power(c.address)
check(f"已委托地址票权为 0（A={pa}, B={pb}）", pa == 0.0 and pb == 0.0)
check(f"C 汇总 3000 无放大（{pc}）", abs(pc - 3000.0) < 1e-3)
apply(n, signed(n, d, "nova:gov:delegate", to=a.address))
check(f"委托环不放大不循环（C={n.governance.voting_power(c.address)}）", abs(n.governance.voting_power(c.address) - 3500.0) < 1e-3)

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)


