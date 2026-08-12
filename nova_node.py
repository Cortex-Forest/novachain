import asyncio, json, time, hashlib, math, os
from typing import Set
from aiohttp import web

from core.crypto import QuantumWallet, verify_quantum_tx, QUANTUM_SAFE
from core.blockchain import Block
from core.transaction import Tx
from core.vm import deploy_address
from core.consensus import ConsensusEngine
from core.storage import StateStore
from core.economy import Economy
from core.chat import (ChatStore, chat_signature_data, validate_chat_payload,
                       message_id, ADDRESS_RE, PUBKEY_RE)
from core.storage_network import (StorageNetwork, CID_RE, HEX64_RE, MAX_REPLICAS,
                                  MAX_CAPACITY_GB, MIN_SIZE_GB, MAX_SIZE_GB,
                                  MIN_DURATION_DAYS, MAX_DURATION_DAYS, day_index)
from core.compute import (ComputeMarket, RESULT_HASH_RE, MAX_WORKERS, MAX_SPEC_LEN,
                          MIN_EXPIRES, MAX_EXPIRES)
from core.socialfi import SocialFi
from network.p2p import P2PNetwork
from network.rpc import setup_routes
from network.security import SecurityManager

class NovaNode:
    def __init__(self, host="0.0.0.0", p2p=9000, rpc=8080, seeds=None, genesis="genesis.json",
                 cert_file="cert.pem", key_file="key.pem", use_tls=True, state_file="chain_state.json",
                 block_interval=60, consensus_mode="checkpoint", validator_key=None, epoch_len=10800):
        self.host, self.p2p_port, self.rpc_port = host, p2p, rpc
        self.node_id = f"{host}:{p2p}"
        self.peers: Set[str] = set()
        self.seeds = seeds or []

        self.state_file = state_file
        self.store = StateStore(genesis)
        self.economy = Economy(self.store)
        self.storage_net = StorageNetwork(self.store, self.economy)
        self.compute_market = ComputeMarket(self.store, self.economy)
        self.socialfi = SocialFi(self.store, self.economy, self.storage_net)
        self.security = SecurityManager()
        self.chat = ChatStore()
        if state_file:
            self.chat.chat_file = (state_file[:-5] + "_chat.json"
                                   if state_file.endswith(".json") else state_file + "_chat.json")
        self.validator = QuantumWallet(validator_key) if validator_key else None
        self.consensus = ConsensusEngine(self, block_interval=block_interval,
                                         mode=consensus_mode, epoch_len=epoch_len)
        if state_file and os.path.exists(state_file):
            self._load_state()
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
        if not isinstance(tx.amount, (int, float)) or isinstance(tx.amount, bool): return False
        if not math.isfinite(tx.amount): return False
        if tx.amount < 0 or tx.amount > self.economy.TOTAL_SUPPLY: return False
        if self._is_stake_op(tx):
            if tx.data == "nova:stake":
                if tx.amount < self.economy.MIN_STAKE or tx.amount > self.economy.MAX_STAKE:
                    return False
                if self.store.stakes.get(tx.sender, 0) + tx.amount > self.economy.MAX_STAKE:
                    return False  # 单地址质押上限
                if sum(self.store.stakes.values()) + tx.amount > self.economy.MAX_TOTAL_STAKE:
                    return False  # 全网质押上限
            if tx.data == "nova:unstake":
                staked = self.store.stakes.get(tx.sender, 0)
                if tx.amount <= 0 or tx.amount > staked:
                    return False
                pending = self.store.unbonding.get(tx.sender, (0, 0))[0]
                if pending + tx.amount > self.economy.MAX_UNBONDING_RATIO * staked:
                    return False  # 解押上限：冷却中总量 <= 当前质押的 25%
            if tx.data == "nova:claim":
                entry = self.store.unbonding.get(tx.sender)
                if not entry or time.time() < entry[1]:
                    return False
        elif self._is_storage_op(tx):
            if not self._validate_storage_op(tx):
                return False
        elif self._is_compute_op(tx):
            if not self._validate_compute_op(tx):
                return False
        elif self._is_socialfi_op(tx):
            if not self._validate_socialfi_op(tx):
                return False
        elif tx.amount == 0 and tx.receiver not in self.contracts:
            return False
        if not isinstance(tx.timestamp, (int, float)) or abs(time.time() - tx.timestamp) > 300: return False
        if not tx.signature or not tx.sender_public_key: return False
        if not verify_quantum_tx(tx.signing_data(), tx.signature, tx.sender_public_key, tx.sender): return False
        return self.balances.get(tx.sender, 0) >= tx.amount + self.gas_of(tx.sender)

    STAKE_OPS = ("nova:stake", "nova:unstake", "nova:claim")
    STORAGE_OPS = ("nova:storage:register", "nova:storage:pin", "nova:storage:claim",
                   "nova:storage:proof", "nova:storage:order")
    COMPUTE_OPS = ("nova:compute:publish", "nova:compute:accept", "nova:compute:submit")
    SOCIALFI_OPS = tuple(SocialFi.OPS)

    def _is_storage_op(self, tx):
        if tx.sender != tx.receiver:
            return False
        d = self._parse_op_data(tx)
        return isinstance(d, dict) and d.get("op") in self.STORAGE_OPS

    def _is_compute_op(self, tx):
        if tx.sender != tx.receiver:
            return False
        d = self._parse_op_data(tx)
        return isinstance(d, dict) and d.get("op") in self.COMPUTE_OPS

    def _is_socialfi_op(self, tx):
        if tx.sender != tx.receiver:
            return False
        d = self._parse_op_data(tx)
        return isinstance(d, dict) and d.get("op") in self.SOCIALFI_OPS

    def _validate_socialfi_op(self, tx):
        return self.socialfi.validate_op(tx)

    @staticmethod
    def _parse_op_data(tx):
        try:
            d = json.loads(tx.data)
        except Exception:
            return None
        return d if isinstance(d, dict) else None

    def _validate_storage_op(self, tx):
        d = self._parse_op_data(tx)
        if d is None:
            return False
        if d.get("op") == "nova:storage:register":
            cap = d.get("capacity_gb")
            return (tx.amount == 0 and isinstance(cap, (int, float)) and not isinstance(cap, bool)
                    and math.isfinite(cap) and 0 < cap <= MAX_CAPACITY_GB
                    and tx.sender not in self.store.storage_providers)
        if d.get("op") == "nova:storage:pin":
            cid = d.get("cid", "")
            size = d.get("size_gb", 0)
            days = d.get("duration_days", 0)
            if tx.amount != 0 or not CID_RE.match(cid) or cid in self.store.storage_claims:
                return False
            if not (isinstance(size, (int, float)) and not isinstance(size, bool)
                    and MIN_SIZE_GB <= size <= MAX_SIZE_GB):
                return False
            if not (isinstance(days, (int, float)) and not isinstance(days, bool)
                    and MIN_DURATION_DAYS <= days <= MAX_DURATION_DAYS):
                return False
            return self.balances.get(self.economy.ECOSYSTEM_FUND, 0) >= self.storage_net.pin_reward(size, days)
        if d.get("op") == "nova:storage:claim":
            cid = d.get("cid", "")
            seal = d.get("seal", "")
            claim = self.store.storage_claims.get(cid)
            if tx.amount != 0 or not HEX64_RE.match(seal):
                return False
            if not claim or time.time() > claim["expires_at"]:
                return False
            if tx.sender not in self.store.storage_providers:
                return False
            if tx.sender in claim["providers"] or len(claim["providers"]) >= MAX_REPLICAS:
                return False
            return self.storage_net._seal_key(tx.sender, cid) not in self.store.storage_seals
        if d.get("op") == "nova:storage:proof":
            cid = d.get("cid", "")
            reveal = d.get("reveal", "")
            claim = self.store.storage_claims.get(cid)
            if tx.amount != 0 or not HEX64_RE.match(reveal):
                return False
            if not claim or time.time() > claim["expires_at"]:
                return False
            if tx.sender not in claim["providers"]:
                return False
            seal = self.store.storage_seals.get(self.storage_net._seal_key(tx.sender, cid))
            if not seal or seal["revealed"] >= seal["length"]:
                return False
            if hashlib.sha3_256(reveal.lower().encode()).hexdigest() != seal["tip"]:
                return False
            return seal["last_proof_day"] != day_index()
        if d.get("op") == "nova:storage:order":
            cid = d.get("cid", "")
            replicas = d.get("replicas", 0)
            days = d.get("duration_days", 0)
            claim = self.store.storage_claims.get(cid)
            if not claim or time.time() > claim["expires_at"]:
                return False
            if not (isinstance(replicas, int) and not isinstance(replicas, bool) and 1 <= replicas <= MAX_REPLICAS):
                return False
            if not (isinstance(days, (int, float)) and not isinstance(days, bool)
                    and MIN_DURATION_DAYS <= days <= MAX_DURATION_DAYS):
                return False
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) and tx.amount > 0
        return False

    def _validate_compute_op(self, tx):
        d = self._parse_op_data(tx)
        if d is None:
            return False
        if d.get("op") == "nova:compute:publish":
            spec = d.get("spec", "")
            exp = d.get("expires_in", 0)
            if not (isinstance(spec, str) and 0 < len(spec) <= MAX_SPEC_LEN):
                return False
            if not (isinstance(exp, (int, float)) and not isinstance(exp, bool)
                    and MIN_EXPIRES <= exp <= MAX_EXPIRES):
                return False
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) and tx.amount > 0
        if d.get("op") == "nova:compute:accept":
            tid = d.get("task_id", "")
            if tx.amount != 0:
                return False
            task = self.store.compute_tasks.get(tid)
            if not task or task["status"] != "open":
                return False
            if tx.sender == task["creator"] or tx.sender in task["accepted"]:
                return False
            return len(task["accepted"]) < MAX_WORKERS
        if d.get("op") == "nova:compute:submit":
            tid = d.get("task_id", "")
            rh = d.get("result_hash", "")
            if tx.amount != 0 or not RESULT_HASH_RE.match(rh):
                return False
            task = self.store.compute_tasks.get(tid)
            if not task or task["status"] != "open":
                return False
            return tx.sender in task["accepted"] and tx.sender not in task["results"] and tx.sender != task["creator"]
        return False

    def _apply_storage_op(self, tx):
        addr = tx.sender
        self.balances[addr] = self.balances.get(addr, 0) - self.gas_of(addr)
        d = json.loads(tx.data)
        if d.get("op") == "nova:storage:register":
            self.storage_net.register(addr, d["capacity_gb"])
        elif d.get("op") == "nova:storage:pin":
            self.storage_net.pin(addr, d["cid"], d["size_gb"], d["duration_days"])
        elif d.get("op") == "nova:storage:claim":
            self.storage_net.claim(addr, d["cid"], d["seal"])
        elif d.get("op") == "nova:storage:proof":
            self.storage_net.proof(addr, d["cid"], d["reveal"])
        elif d.get("op") == "nova:storage:order":
            self.storage_net.create_order(addr, d["cid"], d["replicas"], d["duration_days"],
                                          tx.amount, tx.txid)

    def _apply_compute_op(self, tx):
        addr = tx.sender
        self.balances[addr] = self.balances.get(addr, 0) - self.gas_of(addr)
        d = json.loads(tx.data)
        if d.get("op") == "nova:compute:publish":
            self.compute_market.publish(addr, d["spec"], tx.amount, d["expires_in"], tx.txid)
        elif d.get("op") == "nova:compute:accept":
            self.compute_market.expire(d["task_id"])
            self.compute_market.accept(addr, d["task_id"])
        elif d.get("op") == "nova:compute:submit":
            self.compute_market.expire(d["task_id"])
            self.compute_market.submit(addr, d["task_id"], d["result_hash"])

    def gas_of(self, addr) -> float:
        """声誉驱动的交易费：信誉分 >= 80 享受 50% 折扣。"""
        try:
            rep = self.socialfi.reputation(addr)
            return round(self.economy.FIXED_GAS * rep["fee_multiplier"], 8)
        except Exception:
            return self.economy.FIXED_GAS

    def _is_stake_op(self, tx: Tx) -> bool:
        return tx.data in self.STAKE_OPS and tx.sender == tx.receiver

    def _apply_stake_op(self, tx: Tx):
        addr = tx.sender
        gas = self.gas_of(addr)
        if tx.data == "nova:stake":
            self.balances[addr] = self.balances.get(addr, 0) - (tx.amount + gas)
            self.store.stakes[addr] = self.store.stakes.get(addr, 0) + tx.amount
            # 早期矿工注册 + 前置空投（随交易确定性复制到全节点）
            if addr not in self.store.miner_registry and len(self.store.miner_registry) < 81:
                self.store.miner_registry[addr] = time.time()
                self.store.miner_uptime[addr] = 0.0
                if self.economy.early_airdrop(addr, "miner"):
                    print(f"[MINER] 已注册矿工（{len(self.store.miner_registry)} 位）: {addr[:12]}...")
        elif tx.data == "nova:unstake":
            self.balances[addr] = self.balances.get(addr, 0) - gas
            amt = min(tx.amount, self.store.stakes.get(addr, 0))
            if amt > 0:
                self.store.stakes[addr] = self.store.stakes.get(addr, 0) - amt
                if self.store.stakes[addr] <= 0:
                    del self.store.stakes[addr]
                old = self.store.unbonding.get(addr, (0, 0))[0]
                self.store.unbonding[addr] = (old + amt, time.time() + self.economy.UNBOND)
        elif tx.data == "nova:claim":
            self.balances[addr] = self.balances.get(addr, 0) - gas
            if addr in self.store.unbonding:
                amt, release = self.store.unbonding[addr]
                if time.time() >= release:
                    del self.store.unbonding[addr]
                    self.balances[addr] = self.balances.get(addr, 0) + amt

    def apply_tx(self, tx: Tx):
        if self._is_stake_op(tx):
            self._apply_stake_op(tx)
            return
        if self._is_storage_op(tx):
            self._apply_storage_op(tx)
            return
        if self._is_compute_op(tx):
            self._apply_compute_op(tx)
            return
        if self._is_socialfi_op(tx):
            self.balances[tx.sender] = self.balances.get(tx.sender, 0) - self.gas_of(tx.sender)
            self.socialfi.apply_op(tx)
            return
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
            today = time.strftime("%Y-%m-%d")
            reward_key = f"{tx.sender}:{tx.receiver}"
            if (creator and self.balances.get(self.economy.ECOSYSTEM_FUND, 0) >= cr
                    and self.store.call_reward_dates.get(reward_key) != today):
                self.balances[self.economy.ECOSYSTEM_FUND] -= cr
                self.balances[creator] = self.balances.get(creator, 0) + cr
                self.store.call_reward_dates[reward_key] = today
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

    async def process_message(self, msg, peer, writer=None):
        mtype = msg.get("type")
        if mtype == "new_tx":
            tx = Tx.from_dict(msg["tx"])
            if not self.security.is_replay(tx.txid) and self.validate_tx(tx):
                self.security.mark_processed(tx.txid)
                self.store.dag.add(tx.txid)
                self.apply_tx(tx)
                await self.p2p.gossip(msg, exclude=[peer])
        elif mtype == "hello":
            peer_id = msg.get("node_id")
            if peer_id and peer_id != self.node_id:
                self.peers.add(peer_id)
            if writer is not None and msg.get("height", 0) > self.consensus.chain_height():
                writer.write(json.dumps({"type": "state_request"}).encode())
                await writer.drain()
        elif mtype == "new_block":
            block = Block.from_dict(msg.get("block", {}))
            if self.consensus.adopt_block(block):
                print(f"[BLOCK] 采用 #{block.height} {block.hash[:16]}...")
                await self.p2p.gossip(msg, exclude=[peer])
        elif mtype == "state_request":
            if writer is not None:
                writer.write(json.dumps({"type": "state_snapshot", "snapshot": self.full_snapshot()}).encode())
                await writer.drain()
        elif mtype == "state_snapshot":
            snap = msg.get("snapshot", {})
            peer_chain = snap.get("consensus", {}).get("chain", [])
            if len(peer_chain) >= self.consensus.chain_height() and self.apply_snapshot(snap):
                print(f"[SYNC] 已同步节点 {peer} 状态（高度 {len(peer_chain)}）")

    async def _read_json(self, req):
        try:
            return await req.json()
        except Exception:
            return None

    def full_snapshot(self):
        return {
            "version": 1,
            "saved_at": time.time(),
            "state": self.store.to_dict(),
            "security": self.security.snapshot(),
            "consensus": self.consensus.snapshot(),
        }

    def apply_snapshot(self, snap) -> bool:
        try:
            self.store.from_dict(snap.get("state", {}))
            self.security.restore(snap.get("security", {}))
            self.consensus.restore(snap.get("consensus", {}))
            return True
        except Exception:
            return False

    def save_state(self):
        self.chat.save()
        if not self.state_file: return
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.full_snapshot(), f, ensure_ascii=False)
        os.replace(tmp, self.state_file)

    def _load_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                snap = json.load(f)
            if self.apply_snapshot(snap):
                if self.chat.load():
                    print(f"[CHAT] 已从 {self.chat.chat_file} 恢复")
                print(f"[STATE] 已从 {self.state_file} 恢复")
            else:
                print(f"[STATE] 状态恢复失败: {self.state_file}")
        except Exception as e:
            print(f"[STATE] 状态恢复失败: {e}")

    async def _maintenance_loop(self):
        last_day = None
        while True:
            await asyncio.sleep(3600)
            today = time.strftime("%Y-%m-%d")
            if today == last_day:
                continue
            last_day = today
            try:
                self._run_daily_maintenance()
            except Exception as e:
                print(f"[MAINT] 维护失败: {e}")

    def _run_daily_maintenance(self):
        for addr in list(self.store.miner_registry):
            self.store.miner_uptime[addr] = self.store.miner_uptime.get(addr, 0) + 86400
            if self.store.miner_uptime[addr] >= 270 * 86400:
                self.store.miner_qualified.add(addr)
        self.storage_net.settle_expired()
        self.compute_market.expire_all()
        self.socialfi.maintain()
        self.economy.release_early_rewards()
        if self.state_file:
            self.save_state()
        print("[MAINT] 每日维护完成")

    async def _autosave_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                self.save_state()
                print(f"[STATE] 已自动保存到 {self.state_file}")
            except Exception as e:
                print(f"[STATE] 自动保存失败: {e}")

    async def _save_on_cleanup(self, app):
        try:
            self.save_state()
            print(f"[STATE] 退出时已保存到 {self.state_file}")
        except Exception as e:
            print(f"[STATE] 退出保存失败: {e}")

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
            "height":self.consensus.chain_height(),
            "checkpoint":self.consensus.latest_checkpoint(),
            "consensus":self.consensus.mode,
            "validator":self.validator.address if self.validator else None,
            "storage_providers":len(self.store.storage_providers),
            "pins":len(self.store.storage_claims),
            "compute_tasks":len(self.store.compute_tasks),
            "fan_tokens":len(self.store.fan_tokens),
            "markets":len(self.store.markets),
            "socialfi_events":len(self.store.socialfi_events),
            "quantum_safe":QUANTUM_SAFE,"algorithm":"CRYSTALS-Dilithium5" if QUANTUM_SAFE else "Ed25519"
        })

    async def rpc_send(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict): return web.json_response({"error":"请求体不是合法 JSON"}, status=400)
        try:
            ts = int(b.get("timestamp"))
        except (TypeError, ValueError):
            ts = None
        tx = Tx(b.get("sender",""), b.get("receiver",""), b.get("amount", 0), b.get("parents", []),
                b.get("data",""), b.get("sender_public_key",""), b.get("signature",""), timestamp=ts)
        if not self.validate_tx(tx):
            return web.json_response({"error":"交易校验失败"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"txid": tx.txid})

    async def rpc_deploy(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("bytecode"):
            return web.json_response({"error":"缺少 bytecode"}, status=400)
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
        b = await self._read_json(req)
        if not isinstance(b, dict): return web.json_response({"error":"请求体不是合法 JSON"}, status=400)
        try:
            ts = int(b.get("timestamp"))
        except (TypeError, ValueError):
            ts = None
        tx = Tx(b.get("sender",""), b.get("contract",""), b.get("amount", 0), [],
                b.get("message",""), b.get("sender_public_key",""), b.get("signature",""), timestamp=ts)
        if not self.validate_tx(tx):
            return web.json_response({"error":"调用校验失败"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"txid": tx.txid})

    async def rpc_balance(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        a = req.match_info['addr']
        return web.json_response({"addr":a,"balance":self.balances.get(a,0)})

    def _stake_tx(self, b, data, amt=0):
        """构造质押类签名交易（sender==receiver，data 为 nova:stake/unstake/claim）。"""
        try:
            ts = int(b.get("timestamp"))
        except (TypeError, ValueError):
            ts = None
        addr = b.get("addr", "")
        return Tx(addr, addr, amt, [], data, b.get("sender_public_key", ""), b.get("signature", ""), timestamp=ts)

    async def rpc_stake(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error":"缺少 addr"}, status=400)
        amt = b.get("amount", self.economy.MIN_STAKE)
        if not isinstance(amt, (int, float)) or isinstance(amt, bool) or not math.isfinite(amt):
            return web.json_response({"error":"金额无效"}, status=400)
        tx = self._stake_tx(b, "nova:stake", amt)
        if not self.validate_tx(tx):
            return web.json_response({"error":"质押校验失败（需签名交易，最低 100 NOVA）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status":"已质押","txid":tx.txid,"amount":amt})

    async def rpc_unstake(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error":"缺少 addr"}, status=400)
        amt = b.get("amount", 0)
        if not isinstance(amt, (int, float)) or isinstance(amt, bool) or not math.isfinite(amt) or amt <= 0:
            return web.json_response({"error":"金额无效"}, status=400)
        tx = self._stake_tx(b, "nova:unstake", amt)
        if not self.validate_tx(tx):
            return web.json_response({"error":"解押校验失败（无质押或签名无效）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status":"7天冷静期","txid":tx.txid})

    async def rpc_claim(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error":"缺少 addr"}, status=400)
        tx = self._stake_tx(b, "nova:claim")
        if not self.validate_tx(tx):
            return web.json_response({"error":"领取校验失败（无待领取/未到期/签名无效）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status":"已返还","txid":tx.txid})


    async def rpc_unlock(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error":"缺少 addr"}, status=400)
        unlocked = self.economy.check_unlock(b["addr"])
        return web.json_response({"unlocked": unlocked})

    async def rpc_stakes(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({"stakes":self.store.stakes,"total":self.economy.total_stake()})

    async def rpc_referral(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict): return web.json_response({"error":"请求体不是合法 JSON"}, status=400)
        invitee, referrer = b["invitee"], b["referrer"]
        if invitee in self.store.referrals: return web.json_response({"error":"已有推荐人"}, status=400)
        if invitee == referrer: return web.json_response({"error":"不能推荐自己"}, status=400)
        self.store.referrals[invitee] = referrer
        return web.json_response({"status":"绑定成功"})

    async def rpc_light_verify(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict): return web.json_response({"error":"请求体不是合法 JSON"}, status=400)
        addr, txid = b["addr"], b["txid"]
        if txid not in self.dag: return web.json_response({"error":"交易不存在"}, status=400)
        if txid in self.store.verified_txids: return web.json_response({"error":"该交易已验证过"}, status=400)
        today = time.strftime("%Y-%m-%d")
        if self.store.light_verify_last.get(addr) == today:
            return web.json_response({"error":"今日已领取验证奖励"}, status=400)
        rwd = self.economy.light_verify_reward()
        if self.balances.get(self.economy.VALIDATOR_POOL, 0) >= rwd:
            self.balances[self.economy.VALIDATOR_POOL] -= rwd
            self.balances[addr] = self.balances.get(addr, 0) + rwd
            self.store.light_verifications[addr] = self.store.light_verifications.get(addr, 0) + 1
            self.store.verified_txids.add(txid)
            self.store.light_verify_last[addr] = today
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
            "storage_proof_reward":self.economy.STORAGE_PROOF_REWARD,
            "storage_reward_per_gb_day":self.economy.STORAGE_REWARD_PER_GB_PER_DAY,
            "storage_rewards_paid":sum(self.store.storage_rewards.values()),
            "quantum_safe":QUANTUM_SAFE,"algorithm":"CRYSTALS-Dilithium5" if QUANTUM_SAFE else "Ed25519"
        })

    async def rpc_presale_bind(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict): return web.json_response({"error":"请求体不是合法 JSON"}, status=400)
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
        b = await self._read_json(req)
        if not isinstance(b, dict): return web.json_response({"error":"请求体不是合法 JSON"}, status=400)
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

    # ---------- 加密聊天 RPC ----------
    async def rpc_chat_pubkey_get(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info['addr']
        return web.json_response({"addr": addr, "chat_pub": self.chat.get_pubkey(addr)})

    async def rpc_chat_pubkey_set(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict): return web.json_response({"error": "请求体不是合法 JSON"}, status=400)
        addr = b.get("addr", "")
        chat_pub = b.get("chat_pub", "")
        if not ADDRESS_RE.match(addr):
            return web.json_response({"error": "地址格式无效"}, status=400)
        if not PUBKEY_RE.match(chat_pub):
            return web.json_response({"error": "聊天公钥无效"}, status=400)
        if not verify_quantum_tx(addr + chat_pub, b.get("signature", ""),
                                 b.get("sender_public_key", ""), addr):
            return web.json_response({"error": "签名验证失败"}, status=400)
        self.chat.set_pubkey(addr, chat_pub)
        return web.json_response({"status": "已发布聊天公钥"})

    async def rpc_chat_send(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict): return web.json_response({"error": "请求体不是合法 JSON"}, status=400)
        err = validate_chat_payload(b)
        if err:
            return web.json_response({"error": err}, status=400)
        sender = b["sender"]
        recipient = b["recipient"]
        payload = chat_signature_data(sender, recipient, b["chat_pub"],
                                      b["nonce"], b["ciphertext"], b["ts"])
        if not verify_quantum_tx(payload, b.get("signature", ""),
                                 b.get("sender_public_key", ""), sender):
            return web.json_response({"error": "签名验证失败"}, status=400)
        msg = {
            "id": message_id(sender, recipient, b["chat_pub"],
                              b["nonce"], b["ciphertext"], b["ts"]),
            "sender": sender, "recipient": recipient,
            "chat_pub": b["chat_pub"], "nonce": b["nonce"],
            "ciphertext": b["ciphertext"], "ts": int(b["ts"]),
        }
        self.chat.push(msg)
        return web.json_response({"id": msg["id"], "status": "queued"})

    async def rpc_chat_inbox(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info['addr']
        msgs = self.chat.messages_for(addr)
        return web.json_response({"addr": addr, "messages": msgs})

    async def rpc_chat_ack(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict): return web.json_response({"error": "请求体不是合法 JSON"}, status=400)
        addr = b.get("addr", "")
        ids = b.get("ids", [])
        removed = self.chat.ack(addr, ids)
        return web.json_response({"removed": removed})

    def _special_tx(self, b, data, amt=0):
        """特殊交易：合约类操作 sender == receiver，data 为 JSON 字符串。"""
        try:
            ts = int(b.get("timestamp"))
        except (TypeError, ValueError):
            ts = None
        addr = b.get("addr", "")
        return Tx(addr, addr, amt, [], data,
                  b.get("sender_public_key", ""), b.get("signature", ""), timestamp=ts)

    # ---------- 存储网络 RPC ----------
    async def rpc_storage_register(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error": "缺少 addr"}, status=400)
        try:
            cap = float(b["capacity_gb"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "容量无效"}, status=400)
        tx = self._special_tx(b, json.dumps({"op": "nova:storage:register", "capacity_gb": cap}))
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已注册为存储提供者", "txid": tx.txid})

    async def rpc_storage_pin(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("cid"):
            return web.json_response({"error": "缺少 addr/cid"}, status=400)
        try:
            data = json.dumps({"op": "nova:storage:pin", "cid": str(b["cid"]), "size_gb": float(b["size_gb"]),
                               "duration_days": float(b["duration_days"])})
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "参数无效"}, status=400)
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已固定内容", "cid": b["cid"], "txid": tx.txid})

    async def rpc_storage_claim(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("cid") or not b.get("seal"):
            return web.json_response({"error": "缺少 addr/cid/seal"}, status=400)
        data = json.dumps({"op": "nova:storage:claim", "cid": str(b["cid"]), "seal": str(b["seal"])})
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已认领存储", "cid": b["cid"], "txid": tx.txid})

    async def rpc_storage_proof(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("cid") or not b.get("reveal"):
            return web.json_response({"error": "缺少 addr/cid/reveal"}, status=400)
        data = json.dumps({"op": "nova:storage:proof", "cid": str(b["cid"]), "reveal": str(b["reveal"])})
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已提交存储证明", "cid": b["cid"], "txid": tx.txid})

    async def rpc_storage_order(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("cid"):
            return web.json_response({"error": "缺少 addr/cid"}, status=400)
        amount = b.get("amount", 0)
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount) or amount <= 0:
            return web.json_response({"error": "金额无效"}, status=400)
        try:
            data = json.dumps({"op": "nova:storage:order", "cid": str(b["cid"]), "replicas": int(b["replicas"]),
                               "duration_days": float(b["duration_days"])})
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "参数无效"}, status=400)
        tx = self._special_tx(b, data, amount)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已创建存储订单", "order_id": tx.txid, "txid": tx.txid})

    async def rpc_storage_pins(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({"pins": self.store.storage_claims})

    async def rpc_storage_providers(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({"providers": self.store.storage_providers,
                                  "total": len(self.store.storage_providers)})

    async def rpc_storage_orders(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        self.storage_net.settle_expired()
        return web.json_response({"orders": self.store.storage_orders})

    # ---------- 算力任务 RPC ----------
    async def rpc_compute_publish(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("spec"):
            return web.json_response({"error": "缺少 addr/spec"}, status=400)
        bounty = b.get("bounty", 0)
        if not isinstance(bounty, (int, float)) or isinstance(bounty, bool) or not math.isfinite(bounty) or bounty <= 0:
            return web.json_response({"error": "悬赏金无效"}, status=400)
        try:
            data = json.dumps({"op": "nova:compute:publish", "spec": str(b["spec"]), "expires_in": float(b["expires_in"])})
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "参数无效"}, status=400)
        tx = self._special_tx(b, data, bounty)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已发布计算任务", "task_id": tx.txid, "txid": tx.txid})

    async def rpc_compute_accept(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("task_id"):
            return web.json_response({"error": "缺少 addr/task_id"}, status=400)
        data = json.dumps({"op": "nova:compute:accept", "task_id": str(b["task_id"])})
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已接受任务", "task_id": b["task_id"], "txid": tx.txid})

    async def rpc_compute_submit(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("task_id") or not b.get("result_hash"):
            return web.json_response({"error": "缺少 addr/task_id/result_hash"}, status=400)
        data = json.dumps({"op": "nova:compute:submit", "task_id": str(b["task_id"]), "result_hash": str(b["result_hash"])})
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已提交结果", "task_id": b["task_id"], "txid": tx.txid})

    async def rpc_compute_tasks(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        self.compute_market.expire_all()
        return web.json_response({"tasks": self.store.compute_tasks})

    # ---------- SocialFi RPC（粉丝经济/预测市场/盲盒/策展/社交图谱/债券/碎片 NFT） ----------
    @staticmethod
    def _ser(obj):
        """递归转 JSON 可序列化（set -> sorted list）。"""
        if isinstance(obj, dict):
            return {k: NovaNode._ser(v) for k, v in obj.items()}
        if isinstance(obj, (set, tuple)):
            return sorted(NovaNode._ser(v) for v in obj)
        if isinstance(obj, list):
            return [NovaNode._ser(v) for v in obj]
        return obj

    async def rpc_socialfi_action(self, req):
        """通用 SocialFi 操作：data 为客户端构造的 JSON 字符串（op + 字段），
        签名覆盖 data 原串，避免服务端重建导致的序列化差异。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not isinstance(b.get("data"), str):
            return web.json_response({"error": "缺少 addr/data"}, status=400)
        try:
            ts = int(b.get("timestamp"))
        except (TypeError, ValueError):
            ts = None
        amt = b.get("amount", 0)
        if not isinstance(amt, (int, float)) or isinstance(amt, bool) or not math.isfinite(amt) or amt < 0:
            return web.json_response({"error": "金额无效"}, status=400)
        tx = Tx(b["addr"], b["addr"], amt, [], b["data"],
                b.get("sender_public_key", ""), b.get("signature", ""), timestamp=ts)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        ev = self.store.socialfi_events.get(tx.txid, {})
        return web.json_response({"status": "ok", "txid": tx.txid, **ev})

    async def rpc_socialfi_domain(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        domain = req.match_info['domain']
        m = {
            "fan": self.store.fan_tokens,
            "revenue": self.store.revenue_shares,
            "achievement": {"defs": self.store.achievements, "soulbound": self.store.soulbound},
            "market": self.store.markets,
            "blindbox": {"boxes": self.store.blindboxes, "reveals": self.store.blind_reveals},
            "curation": self.store.curations,
            "graph": {"posts": self.store.graph_posts, "follows": self.store.graph_follows},
            "bond": self.store.bonds,
            "fraction": self.store.fractions,
            "events": sorted(self.store.socialfi_events.values(), key=lambda e: e.get("ts", 0), reverse=True)[:50],
        }
        if domain not in m:
            return web.json_response({"error": "未知领域"}, status=404)
        return web.json_response(self._ser(m[domain]))

    async def rpc_socialfi_overview(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({
            "fan_tokens": len(self.store.fan_tokens),
            "revenue_shares": len(self.store.revenue_shares),
            "achievements": len(self.store.achievements),
            "markets": len(self.store.markets),
            "blindboxes": len(self.store.blindboxes),
            "curations": len(self.store.curations),
            "posts": len(self.store.graph_posts),
            "follows": sum(len(v) for v in self.store.graph_follows.values()),
            "bonds": len(self.store.bonds),
            "fractions": len(self.store.fractions),
            "events": len(self.store.socialfi_events),
            "graph_hash": self.socialfi.graph_hash(),
        })

    async def rpc_reputation(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info['addr']
        return web.json_response(self.socialfi.reputation(addr))

    async def rpc_graph_recommend(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info['addr']
        return web.json_response({
            "addr": addr,
            "recommendations": self.socialfi.recommendations(addr),
            "task_spec": self.socialfi.recommend_task_spec(addr),
            "graph_hash": self.socialfi.graph_hash(),
        })
    # ---------- 启动 ----------
    async def start(self):
        await self.p2p.start_server()
        for s in self.seeds:
            asyncio.create_task(self.p2p.connect_to_peer(s))

        app = web.Application(client_max_size=262144)
        setup_routes(app, self)
        app.on_cleanup.append(self._save_on_cleanup)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.rpc_port)
        await site.start()
        print(f"[RPC] 监听 {self.host}:{self.rpc_port}")

        if self.state_file:
            self.save_state()
            asyncio.create_task(self._autosave_loop())
        asyncio.create_task(self._maintenance_loop())
        asyncio.create_task(self.consensus.checkpoint_loop())
        try:
            await self.p2p.server.serve_forever()
        finally:
            self.p2p.close_all()
            self.save_state()

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
    p.add_argument("--state", default="chain_state.json", help="状态快照文件，传空字符串禁用持久化")
    p.add_argument("--consensus", choices=["pos", "checkpoint"], default="checkpoint", help="共识模式（pos=质押加权出块+签名验证）")
    p.add_argument("--validator-key", default="", help="PoS 验证者私钥（hex seed，32 字节），pos 模式下用于出块签名")
    p.add_argument("--epoch-len", type=int, default=10800, help="PoS epoch 块数（默认 10800 ≈ 7.5 天）")
    a = p.parse_args()
    node = NovaNode(host=a.host, p2p=a.p2p, rpc=a.rpc,
                    seeds=[a.seed] if a.seed else [],
                    genesis=a.genesis,
                    cert_file=a.cert, key_file=a.key, use_tls=not a.no_tls, state_file=a.state,
                    consensus_mode=a.consensus, validator_key=a.validator_key or None,
                    epoch_len=a.epoch_len)
    asyncio.run(node.start())