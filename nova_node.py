import asyncio, json, time, hashlib
from typing import Set
from aiohttp import web

from core.crypto import QuantumWallet, verify_quantum_tx
from core.transaction import Tx
from core.vm import deploy_address
from core.consensus import ConsensusEngine
from core.storage import StateStore
from core.economy import Economy
from network.p2p import P2PNetwork
from network.rpc import setup_routes
from network.security import SecurityManager

class NovaNode:
    def __init__(self, host="0.0.0.0", p2p=9000, rpc=8080, seeds=None, genesis="genesis.json",
                 cert_file="cert.pem", key_file="key.pem", use_tls=True):
        self.host, self.p2p_port, self.rpc_port = host, p2p, rpc
        self.node_id = f"{host}:{p2p}"
        self.peers: Set[str] = set()
        self.seeds = seeds or []

        self.store = StateStore(genesis)
        self.economy = Economy(self.store)
        self.security = SecurityManager()
        self.consensus = ConsensusEngine(self)
        self.p2p = P2PNetwork(self, host, p2p, use_tls, cert_file, key_file)

    @property
    def dag(self): return self.store.dag
    @property
    def balances(self): return self.store.balances
    @property
    def contracts(self): return self.store.contracts

    # ---------- 交易处理 ----------
    def validate_tx(self, tx: Tx) -> bool:
        if self.security.is_replay(tx.txid): return False
        if not self.security.validate_size(tx.to_dict()): return False
        if tx.sender == "0x0000": return True
        if not tx.signature or not tx.sender_public_key: return False
        if not verify_quantum_tx(tx.signing_data(), tx.signature, tx.sender_public_key, tx.sender): return False
        return self.balances.get(tx.sender, 0) >= tx.amount + self.economy.FIXED_GAS

    def apply_tx(self, tx: Tx):
        old_balance = self.balances.get(tx.receiver, 0)
        self.balances[tx.sender] = self.balances.get(tx.sender, 0) - (tx.amount + self.economy.FIXED_GAS)
        self.balances[tx.receiver] = old_balance + tx.amount

        reward = self.economy.block_reward() + self.economy.FIXED_GAS
        pool = self.balances.get(self.economy.VALIDATOR_POOL, 0)
        if pool >= reward:
            self.balances[self.economy.VALIDATOR_POOL] = pool - reward
            self.economy.distribute(reward)
        elif pool > 0:
            self.balances[self.economy.VALIDATOR_POOL] = 0
            self.economy.distribute(pool)

        if tx.receiver in self.contracts:
            creator = self.store.contract_creator.get(tx.receiver)
            cr = self.economy.call_reward()
            if creator and self.balances.get(self.economy.ECOSYSTEM_FUND, 0) >= cr:
                self.balances[self.economy.ECOSYSTEM_FUND] -= cr
                self.balances[creator] = self.balances.get(creator, 0) + cr
                self.store.call_count += 1

        if old_balance == 0 and tx.amount > 0:
            if tx.receiver in self.store.referrals and tx.receiver not in self.store.referral_claimed:
                ref = self.store.referrals[tx.receiver]
                rwd = self.economy.referral_reward()
                if self.balances.get(self.economy.COMMUNITY_AIRDROP, 0) >= rwd:
                    self.balances[self.economy.COMMUNITY_AIRDROP] -= rwd
                    self.balances[ref] = self.balances.get(ref, 0) + rwd
                    self.store.referral_claimed.add(tx.receiver)
                    self.store.referral_issued += 1

    async def broadcast_tx(self, tx: Tx):
        if self.validate_tx(tx):
            self.security.mark_processed(tx.txid)
            self.store.dag.add(tx.txid)
            self.apply_tx(tx)
            await self.p2p.gossip({"type":"new_tx","tx":tx.to_dict()})

    async def process_message(self, msg, peer):
        if msg.get("type") == "new_tx":
            tx = Tx.from_dict(msg["tx"])
            if not self.security.is_replay(tx.txid) and self.validate_tx(tx):
                self.security.mark_processed(tx.txid)
                self.store.dag.add(tx.txid)
                self.apply_tx(tx)
                await self.p2p.gossip(msg, exclude=[peer])

    # ---------- RPC 守卫 ----------
    async def _rpc_guard(self, request):
        ip = request.remote
        if not self.security.check_rate_limit(ip):
            return web.json_response({"error":"请求过于频繁"}, status=429)
        return None

    # ---------- RPC 接口 ----------
    async def rpc_status(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({
            "node":self.node_id,"peers":len(self.peers),"dag":len(self.dag),
            "total_stake":self.economy.total_stake(),"deploy_count":self.store.deploy_count,
            "referral_issued":self.store.referral_issued,"call_count":self.store.call_count,
            "checkpoint":self.consensus.latest_checkpoint(),
            "quantum_safe":True,"algorithm":"CRYSTALS-Dilithium5"
        })

    async def rpc_send(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        tx = Tx(b["sender"],b["receiver"],b["amount"],b.get("parents",[]),b.get("data",""),b.get("sender_public_key",""),b.get("signature",""))
        await self.broadcast_tx(tx)
        return web.json_response({"txid":tx.txid})

    async def rpc_deploy(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        addr = deploy_address(b["bytecode"])
        creator = b.get("creator","")
        tx = Tx("0x0000", addr, 0, [], b["bytecode"])
        self.store.dag.add(tx.txid)
        self.store.contracts[addr] = b["bytecode"]
        rwd = 0
        if creator:
            self.store.contract_creator[addr] = creator
            rwd = self.economy.deploy_reward()
            if self.balances.get(self.economy.ECOSYSTEM_FUND, 0) >= rwd:
                self.balances[self.economy.ECOSYSTEM_FUND] -= rwd
                self.balances[creator] = self.balances.get(creator, 0) + rwd
                self.store.deploy_count += 1
        await self.broadcast_tx(tx)
        return web.json_response({"contract":addr,"txid":tx.txid,"reward":rwd})

    async def rpc_call(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        tx = Tx(b["sender"],b["contract"],b.get("amount",0),[],b.get("message",""),b.get("sender_public_key",""),b.get("signature",""))
        await self.broadcast_tx(tx)
        return web.json_response({"txid":tx.txid})

    async def rpc_balance(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        a = req.match_info['addr']
        return web.json_response({"addr":a,"balance":self.balances.get(a,0)})

    async def rpc_stake(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        addr, amt = b["addr"], b.get("amount", self.economy.MIN_STAKE)
        if amt < self.economy.MIN_STAKE: return web.json_response({"error":"最低100"}, status=400)
        if self.balances.get(addr,0) < amt: return web.json_response({"error":"余额不足"}, status=400)
        self.balances[addr] -= amt
        self.store.stakes[addr] = self.store.stakes.get(addr, 0) + amt
        # 早期矿工注册 + 前置空投
        if addr not in self.store.miner_registry and len(self.store.miner_registry) < 81:
            self.store.miner_registry[addr] = time.time()
            self.store.miner_uptime[addr] = 0.0
            if self.economy.early_airdrop(addr, "miner"):
                print(f"[MINER] 第{len(self.store.miner_registry)}位矿工: {addr[:12]}...")
        return web.json_response({"status":"已质押","amount":amt})

    async def rpc_unstake(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        addr = b["addr"]
        if self.store.stakes.get(addr,0) <= 0: return web.json_response({"error":"无质押"}, status=400)
        amt = self.store.stakes.pop(addr)
        self.store.unbonding[addr] = (amt, time.time() + self.economy.UNBOND)
        return web.json_response({"status":"7天冷静期","amount":amt})

    async def rpc_claim(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        addr = b["addr"]
        if addr not in self.store.unbonding: return web.json_response({"error":"无待领取"}, status=400)
        amt, release = self.store.unbonding[addr]
        if time.time() < release: return web.json_response({"error":"未到期"}, status=400)
        del self.store.unbonding[addr]
        self.balances[addr] += amt
        return web.json_response({"status":"已返还","amount":amt})

    async def rpc_stakes(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({"stakes":self.store.stakes,"total":self.economy.total_stake()})

    async def rpc_referral(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        invitee, referrer = b["invitee"], b["referrer"]
        if invitee in self.store.referrals: return web.json_response({"error":"已有推荐人"}, status=400)
        if invitee == referrer: return web.json_response({"error":"不能推荐自己"}, status=400)
        self.store.referrals[invitee] = referrer
        return web.json_response({"status":"绑定成功"})

    async def rpc_light_verify(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        addr, txid = b["addr"], b["txid"]
        if txid not in self.dag: return web.json_response({"error":"交易不存在"}, status=400)
        rwd = self.economy.light_verify_reward()
        if self.balances.get(self.economy.VALIDATOR_POOL, 0) >= rwd:
            self.balances[self.economy.VALIDATOR_POOL] -= rwd
            self.balances[addr] = self.balances.get(addr, 0) + rwd
            self.store.light_verifications[addr] = self.store.light_verifications.get(addr, 0) + 1
            return web.json_response({"status":"已验证","reward":rwd})
        return web.json_response({"error":"激励池不足"}, status=400)

    async def rpc_stats(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({
            "deploy_count":self.store.deploy_count,"deploy_reward":self.economy.deploy_reward(),
            "referral_issued":self.store.referral_issued,"referral_reward":self.economy.referral_reward(),
            "call_count":self.store.call_count,"call_reward":self.economy.call_reward(),
            "block_reward":self.economy.block_reward(),"light_verify_reward":self.economy.light_verify_reward(),
            "quantum_safe":True,"algorithm":"CRYSTALS-Dilithium5"
        })

    async def rpc_presale_bind(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        nova_addr, nova_pub, bsc_addr, sig = b["nova_address"], b["nova_public_key"], b["bsc_address"], b["signature"]
        msg = f"BIND_PRESALE:{bsc_addr}"
        if not verify_quantum_tx(msg, sig, nova_pub, nova_addr):
            return web.json_response({"error":"签名验证失败"}, status=400)
        self.store.presale_verified[nova_addr] = bsc_addr
        print(f"[PRESALE] 绑定成功: {nova_addr[:12]}... ↔ {bsc_addr[:12]}...")
        return web.json_response({"status":"绑定成功"})

    async def rpc_checkin(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await req.json()
        addr = b["addr"]
        fingerprint = b.get("fingerprint","")
        ip = req.remote

        if not self.security.check_ip_limit(ip, "light"):
            return web.json_response({"error":"同一IP 24小时内只能一个轻节点签到"}, status=400)
        if fingerprint and not self.security.check_device_unique(fingerprint):
            return web.json_response({"error":"设备已注册"}, status=400)
        if not self.security.check_checkin_interval(addr):
            return web.json_response({"error":"签到间隔需≥20小时"}, status=400)

        today = time.strftime("%Y-%m-%d")
        if addr not in self.store.light_checkin_dates:
            self.store.light_checkin_dates[addr] = set()
        if today in self.store.light_checkin_dates[addr]:
            return web.json_response({"error":"今日已签到"})

        self.store.light_checkin_dates[addr].add(today)
        self.store.light_checkins[addr] = len(self.store.light_checkin_dates[addr])
        self.security.checkin_history.setdefault(addr, []).append(time.time())

        if addr not in self.store.early_airdrop_received:
            active_light = sum(1 for a in self.store.light_checkins if self.store.light_checkins[a] > 0)
            if active_light <= 8100:
                self.economy.early_airdrop(addr, "light")

        self.security.ip_registry.setdefault(ip, {})[f"light_{addr}"] = time.time()
        if fingerprint:
            self.security.device_fingerprints[fingerprint] = addr

        if self.store.light_checkins[addr] >= 270:
            self.store.light_qualified.add(addr)

        return web.json_response({"status":"签到成功","total_days":self.store.light_checkins[addr]})

    async def rpc_early_info(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.query.get("addr","")
        return web.json_response({
            "miner_registered": addr in self.store.miner_registry,
            "miner_uptime_days": self.store.miner_uptime.get(addr, 0) / 86400,
            "light_checkin_days": self.store.light_checkins.get(addr, 0),
            "locked_balance": self.store.locked_balances.get(addr, {}).get("amount", 0),
            "miner_qualified": addr in self.store.miner_qualified,
            "light_qualified": addr in self.store.light_qualified,
        })

    # ---------- 启动 ----------
    async def start(self):
        await self.p2p.start_server()
        for s in self.seeds:
            asyncio.create_task(self.p2p.connect_to_peer(s))

        app = web.Application()
        setup_routes(app, self)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.rpc_port)
        await site.start()
        print(f"[RPC] 监听 {self.host}:{self.rpc_port}")

        asyncio.create_task(self.consensus.checkpoint_loop())
        async with self.p2p.server:
            await self.p2p.server.serve_forever()

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--p2p", type=int, default=9000)
    p.add_argument("--rpc", type=int, default=8080)
    p.add_argument("--seed", default="")
    p.add_argument("--genesis", default="genesis.json")
    p.add_argument("--no-tls", action="store_true")
    p.add_argument("--cert", default="cert.pem")
    p.add_argument("--key", default="key.pem")
    a = p.parse_args()
    node = NovaNode(host=a.host, p2p=a.p2p, rpc=a.rpc,
                    seeds=[a.seed] if a.seed else [],
                    genesis=a.genesis,
                    cert_file=a.cert, key_file=a.key, use_tls=not a.no_tls)
    asyncio.run(node.start())