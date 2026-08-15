import asyncio, json, time, hashlib, math, os
from typing import Set
from aiohttp import web

from core.crypto import QuantumWallet, verify_quantum_tx, QUANTUM_SAFE
from core.blockchain import Block
from core.transaction import Tx
from core.vm import deploy_address, NexusVM
from nexlang_compiler import NexLangCompiler
from core.consensus import ConsensusEngine
from core.storage import StateStore
from core.economy import Economy
from core.chat import (ChatStore, chat_signature_data, validate_chat_payload,
                       message_id, ADDRESS_RE, PUBKEY_RE)
from core.storage_network import (StorageNetwork, CID_RE, HEX64_RE, MAX_REPLICAS,
                                  MAX_CAPACITY_GB, MIN_SIZE_GB, MAX_SIZE_GB,
                                  MIN_DURATION_DAYS, MAX_DURATION_DAYS, day_index)
from core.storage_incentive import (StorageIncentive, MAX_INC_REPLICAS, MIN_INC_SIZE_GB,
                                    MAX_INC_SIZE_GB, CHALLENGE_FILES, HEARTBEAT_INTERVAL)
from core.compute import (ComputeMarket, RESULT_HASH_RE, MAX_WORKERS, MAX_SPEC_LEN,
                          MIN_EXPIRES, MAX_EXPIRES, TASK_TYPES, IPFS_RE,
                          MIN_COMPUTE_STAKE, MAX_COMPUTE_STAKE, COMPUTE_POOL,
                          MAX_CPU, MAX_RAM_GB, MAX_STORAGE_GB, MAX_GPU_VRAM_GB, MAX_LATENCY_MS,
                          SPEC_LEN_MAX)
from core.ai_service import AIService, AI_FUND, TRIGGER_FEE
from core.socialfi import SocialFi, TEXT_ESCROW
from core.arbitration import Arbitration, ARB_STAKE, ARB_COMPLAINT_DEPOSIT
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
        self.storage_incentive = StorageIncentive(self.store, self.economy)
        self.compute_market = ComputeMarket(self.store, self.economy)
        self.socialfi = SocialFi(self.store, self.economy, self.storage_net)
        self.arbitration = Arbitration(self.store, self.economy, self.socialfi)
        self.ai_service = AIService(self.store, self.economy, self.compute_market, self.socialfi)
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
        ai = self.socialfi.ai_identity(tx.sender)
        if ai is not None and not self.socialfi.ai_can_spend(tx.sender, tx.amount):
            return False  # AI 创作者日预算硬约束（链上强制）
        if self.arbitration.cipher_locked(tx.sender, self._parse_op_data(tx)):
            return False  # 恶意投诉名单：限制密文交易权限
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
        elif self._is_storage_inc_op(tx):
            if not self._validate_storage_inc_op(tx):
                return False
        elif self._is_compute_op(tx):
            if not self._validate_compute_op(tx):
                return False
        elif self._is_ai_op(tx):
            if not self._validate_ai_op(tx):
                return False
        elif self._is_socialfi_op(tx):
            if not self._validate_socialfi_op(tx):
                return False
        elif self._is_arb_op(tx):
            if not self._validate_arb_op(tx):
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
    STORAGE_INC_OPS = ("nova:storage:inc:file", "nova:storage:inc:claim",
                       "nova:storage:inc:prove", "nova:storage:inc:heartbeat",
                       "nova:storage:inc:upgrade", "nova:storage:inc:exit",
                       "nova:storage:inc:settle", "nova:storage:inc:protect",
                       "nova:storage:inc:reassign", "nova:storage:inc:access",
                       "nova:storage:inc:reupload")
    COMPUTE_OPS = ("nova:compute:publish", "nova:compute:accept", "nova:compute:submit",
                   "nova:compute:register", "nova:compute:bid", "nova:compute:award",
                   "nova:compute:arbitrate", "nova:compute:dispute", "nova:compute:vote",
                   "nova:compute:stake", "nova:compute:unstake", "nova:compute:claim",
                   "nova:compute:audit")
    AI_OPS = ("nova:ai:svc:register", "nova:ai:svc:config", "nova:ai:muso:config",
              "nova:ai:work:create", "nova:ai:work:buy", "nova:ai:trigger",
              "nova:ai:fund:guard", "nova:ai:fund:spend")
    SOCIALFI_OPS = tuple(SocialFi.OPS)
    ARB_OPS = tuple(Arbitration.OPS)

    def _is_storage_op(self, tx):
        if tx.sender != tx.receiver:
            return False
        d = self._parse_op_data(tx)
        return isinstance(d, dict) and d.get("op") in self.STORAGE_OPS

    def _is_storage_inc_op(self, tx):
        if tx.sender != tx.receiver:
            return False
        d = self._parse_op_data(tx)
        return isinstance(d, dict) and d.get("op") in self.STORAGE_INC_OPS

    def _is_compute_op(self, tx):
        if tx.sender != tx.receiver:
            return False
        d = self._parse_op_data(tx)
        return isinstance(d, dict) and d.get("op") in self.COMPUTE_OPS

    def _is_ai_op(self, tx):
        if tx.sender != tx.receiver:
            return False
        d = self._parse_op_data(tx)
        return isinstance(d, dict) and d.get("op") in self.AI_OPS

    def _is_socialfi_op(self, tx):
        if tx.sender != tx.receiver:
            return False
        d = self._parse_op_data(tx)
        return isinstance(d, dict) and d.get("op") in self.SOCIALFI_OPS

    def _is_arb_op(self, tx):
        if tx.sender != tx.receiver:
            return False
        d = self._parse_op_data(tx)
        return isinstance(d, dict) and d.get("op") in self.ARB_OPS

    def _validate_socialfi_op(self, tx):
        return self.socialfi.validate_op(tx)

    def _validate_arb_op(self, tx):
        return self.arbitration.validate_op(tx)

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
        op = d.get("op")
        addr = tx.sender
        if op == "nova:compute:publish":
            spec = d.get("spec", "")
            exp = d.get("expires_in", 0)
            if not (isinstance(spec, str) and 0 < len(spec) <= MAX_SPEC_LEN):
                return False
            if not (isinstance(exp, (int, float)) and not isinstance(exp, bool)
                    and MIN_EXPIRES <= exp <= MAX_EXPIRES):
                return False
            tt = d.get("task_type")
            if tt is not None and tt not in TASK_TYPES:
                return False
            mode = d.get("mode", "grab")
            if mode not in ("grab", "bid"):
                return False
            mn = d.get("min_nodes", 2)
            if not (isinstance(mn, int) and not isinstance(mn, bool) and 2 <= mn <= MAX_WORKERS):
                return False
            acc = d.get("acceptance", "")
            if not (isinstance(acc, str) and len(acc) <= MAX_SPEC_LEN):
                return False
            return isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool) and tx.amount > 0
        if op == "nova:compute:accept":
            tid = d.get("task_id", "")
            if tx.amount != 0:
                return False
            return self.compute_market.validate_accept(addr, tid)[0]
        if op == "nova:compute:submit":
            tid = d.get("task_id", "")
            rh = d.get("result_hash", "")
            rc = d.get("result_cid", "")
            if tx.amount != 0 or not RESULT_HASH_RE.match(rh):
                return False
            return self.compute_market.validate_submit(addr, tid, rh, rc)[0]
        if op == "nova:compute:register":
            if tx.amount != 0:
                return False
            cpu = d.get("cpu_cores", 0)
            ram = d.get("ram_gb", 0)
            storage = d.get("storage_gb", 0)
            vram = d.get("gpu_vram_gb", 0)
            latency = d.get("latency_ms", 50)
            if not (isinstance(cpu, int) and not isinstance(cpu, bool) and 0 < cpu <= MAX_CPU):
                return False
            for v, lo, hi in ((ram, 0.5, MAX_RAM_GB), (storage, 1.0, MAX_STORAGE_GB),
                              (vram, 0.0, MAX_GPU_VRAM_GB), (latency, 0.0, MAX_LATENCY_MS)):
                if not (isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(v) and lo <= v <= hi):
                    return False
            gpu = d.get("gpu_model", "")
            if not (isinstance(gpu, str) and len(gpu) <= SPEC_LEN_MAX):
                return False
            region = d.get("region", "")
            return isinstance(region, str) and len(region) <= 32
        if op == "nova:compute:bid":
            tid = d.get("task_id", "")
            price = d.get("price", 0)
            if tx.amount != 0:
                return False
            return self.compute_market.validate_bid(addr, tid, price)[0]
        if op == "nova:compute:award":
            tid = d.get("task_id", "")
            workers = d.get("workers", [])
            if tx.amount != 0:
                return False
            return self.compute_market.validate_award(addr, tid, workers)[0]
        if op == "nova:compute:arbitrate":
            tid = d.get("task_id", "")
            rh = d.get("result_hash", "")
            if tx.amount != 0:
                return False
            return self.compute_market.validate_arbitrate(addr, tid, rh)[0]
        if op == "nova:compute:dispute":
            tid = d.get("task_id", "")
            reason = d.get("reason", "")
            if tx.amount != 0:
                return False
            return self.compute_market.validate_dispute(addr, tid, reason)[0]
        if op == "nova:compute:vote":
            tid = d.get("task_id", "")
            support = d.get("support", "")
            if tx.amount != 0:
                return False
            return self.compute_market.validate_vote(addr, tid, support)[0]
        if op == "nova:compute:stake":
            if tx.amount == 0:
                return False
            return self.compute_market.validate_stake(addr, tx.amount)[0]
        if op == "nova:compute:unstake":
            if tx.amount == 0:
                return False
            return self.compute_market.validate_unstake(addr, tx.amount)[0]
        if op == "nova:compute:claim":
            return tx.amount == 0 and self.compute_market.validate_claim(addr)[0]
        if op == "nova:compute:audit":
            tid = d.get("task_id", "")
            rh = d.get("result_hash", "")
            if tx.amount != 0:
                return False
            return self.compute_market.validate_audit(addr, tid, rh)[0]
        return False

    def _validate_ai_op(self, tx):
        d = self._parse_op_data(tx)
        if d is None:
            return False
        op = d.get("op")
        addr = tx.sender
        if op == "nova:ai:svc:register":
            return tx.amount == 0 and self.ai_service.validate_svc_register(d, addr)[0]
        if op == "nova:ai:svc:config":
            return tx.amount == 0 and self.ai_service.validate_svc_config(d, addr)[0]
        if op == "nova:ai:muso:config":
            return tx.amount == 0 and self.ai_service.validate_muso_config(d, addr)[0]
        if op == "nova:ai:work:create":
            return tx.amount == 0 and self.ai_service.validate_work_create(d, addr)[0]
        if op == "nova:ai:work:buy":
            return self.ai_service.validate_work_buy(d, addr, tx.amount)[0]
        if op == "nova:ai:trigger":
            return self.ai_service.validate_trigger(d, addr, tx.amount)[0]
        if op == "nova:ai:fund:guard":
            return tx.amount == 0 and self.ai_service.validate_fund_guard(d, addr)[0]
        if op == "nova:ai:fund:spend":
            return self.ai_service.validate_fund_spend(d, addr, tx.amount)[0]
        return False

    def _apply_storage_op(self, tx):
        addr = tx.sender
        self.balances[addr] = self.balances.get(addr, 0) - self.gas_of(addr)
        d = json.loads(tx.data)
        if d.get("op") == "nova:storage:register":
            self.storage_net.register(addr, d["capacity_gb"])
            self.storage_incentive.auto_register(addr, d["capacity_gb"])
        elif d.get("op") == "nova:storage:pin":
            self.storage_net.pin(addr, d["cid"], d["size_gb"], d["duration_days"])
        elif d.get("op") == "nova:storage:claim":
            self.storage_net.claim(addr, d["cid"], d["seal"])
        elif d.get("op") == "nova:storage:proof":
            self.storage_net.proof(addr, d["cid"], d["reveal"])
        elif d.get("op") == "nova:storage:order":
            self.storage_net.create_order(addr, d["cid"], d["replicas"], d["duration_days"],
                                          tx.amount, tx.txid)

    def _validate_storage_inc_op(self, tx):
        """存储激励合约操作校验（链上硬约束）。"""
        d = self._parse_op_data(tx)
        if d is None:
            return False
        op = d.get("op")
        if op == "nova:storage:inc:file":
            cid = d.get("cid", "")
            size = d.get("size_gb", 0)
            commit = d.get("fragment_commit", "")
            if tx.amount != 0 or not CID_RE.match(cid) or cid in self.store.inc_files:
                return False
            if not HEX64_RE.match(commit):
                return False
            return (isinstance(size, (int, float)) and not isinstance(size, bool)
                    and MIN_INC_SIZE_GB <= size <= MAX_INC_SIZE_GB)
        if op == "nova:storage:inc:claim":
            cid = d.get("cid", "")
            f = self.store.inc_files.get(cid)
            if tx.amount != 0 or not CID_RE.match(cid) or not f:
                return False
            if tx.sender not in self.store.inc_nodes:
                return False
            if tx.sender in f.get("replicas", []) or len(f.get("replicas", [])) >= MAX_INC_REPLICAS:
                return False
            return self.storage_incentive.can_assign(tx.sender, f["size_gb"])
        if op == "nova:storage:inc:prove":
            day = d.get("day", 0)
            files = d.get("files", [])
            fragments = d.get("fragments", [])
            if tx.amount != 0:
                return False
            if not isinstance(day, int) or isinstance(day, bool) or day < 0:
                return False
            node = self.store.inc_nodes.get(tx.sender)
            if not node or node.get("last_proof_epoch") == day:
                return False
            if not isinstance(files, list) or not isinstance(fragments, list):
                return False
            if len(files) != len(fragments) or not files:
                return False
            if any(not CID_RE.match(c) for c in files):
                return False
            if any(not (isinstance(x, str) and len(x) == 2048) for x in fragments):
                return False
            ch = self.storage_incentive.current_challenge(tx.sender, day)
            return bool(ch.get("found")) and list(files) == ch["files"]
        if op == "nova:storage:inc:heartbeat":
            return tx.amount == 0 and tx.sender in self.store.inc_nodes
        if op == "nova:storage:inc:upgrade":
            if tx.sender not in self.store.inc_nodes:
                return False
            return (isinstance(tx.amount, (int, float)) and not isinstance(tx.amount, bool)
                    and math.isfinite(tx.amount) and tx.amount > 0)
        if op == "nova:storage:inc:exit":
            node = self.store.inc_nodes.get(tx.sender)
            return tx.amount == 0 and bool(node) and not node.get("exit_at")
        if op in ("nova:storage:inc:settle", "nova:storage:inc:protect", "nova:storage:inc:reassign"):
            return tx.amount == 0
        if op == "nova:storage:inc:access":
            return tx.amount == 0 and CID_RE.match(d.get("cid", "")) and d.get("cid") in self.store.inc_files
        if op == "nova:storage:inc:reupload":
            old_cid = d.get("old_cid", "")
            new_cid = d.get("new_cid", "")
            commit = d.get("fragment_commit", "")
            size = d.get("size_gb", 0)
            f = self.store.inc_files.get(old_cid)
            if tx.amount != 0 or not CID_RE.match(old_cid) or not CID_RE.match(new_cid):
                return False
            if not f or f.get("owner") != tx.sender or new_cid == old_cid:
                return False
            if new_cid in self.store.inc_files or not HEX64_RE.match(commit):
                return False
            return (isinstance(size, (int, float)) and not isinstance(size, bool)
                    and MIN_INC_SIZE_GB <= size <= MAX_INC_SIZE_GB)
        return False

    def _apply_storage_inc_op(self, tx):
        addr = tx.sender
        self.balances[addr] = self.balances.get(addr, 0) - self.gas_of(addr)
        d = json.loads(tx.data)
        op = d.get("op")
        if op == "nova:storage:inc:file":
            self.storage_incentive.file_register(addr, d["cid"], d["size_gb"],
                                                 d["fragment_commit"], d.get("title", ""),
                                                 d.get("content_type", "music"))
        elif op == "nova:storage:inc:claim":
            self.storage_incentive.claim(addr, d["cid"])
        elif op == "nova:storage:inc:prove":
            self.storage_incentive.verify_proof(addr, d["day"], d["files"], d["fragments"])
        elif op == "nova:storage:inc:heartbeat":
            self.storage_incentive.heartbeat(addr)
        elif op == "nova:storage:inc:upgrade":
            self.storage_incentive.upgrade_quota(addr, tx.amount)
        elif op == "nova:storage:inc:exit":
            self.storage_incentive.exit_notice(addr)
        elif op == "nova:storage:inc:settle":
            # 结算上一个完整周期（与每日维护一致），避免提前惩罚当日未证明节点
            self.storage_incentive.settle_epoch(day_index() - 1)
        elif op == "nova:storage:inc:protect":
            self.storage_incentive.protect_hot_files()
        elif op == "nova:storage:inc:reassign":
            self.storage_incentive.reassign_endangered()
        elif op == "nova:storage:inc:access":
            self.storage_incentive.record_access(d["cid"])
        elif op == "nova:storage:inc:reupload":
            self.storage_incentive.file_reupload(addr, d["old_cid"], d["new_cid"],
                                                 d["size_gb"], d["fragment_commit"],
                                                 d.get("title", ""))

    def _apply_compute_op(self, tx):
        addr = tx.sender
        self.balances[addr] = self.balances.get(addr, 0) - self.gas_of(addr)
        d = json.loads(tx.data)
        op = d.get("op")
        if op == "nova:compute:publish":
            self.compute_market.publish(addr, d["spec"], tx.amount, d["expires_in"], tx.txid,
                                        task_type=d.get("task_type"), mode=d.get("mode", "grab"),
                                        min_nodes=d.get("min_nodes", 2),
                                        acceptance=d.get("acceptance", ""))
        elif op == "nova:compute:accept":
            self.compute_market.expire(d["task_id"])
            self.compute_market.accept(addr, d["task_id"])
        elif op == "nova:compute:submit":
            self.compute_market.expire(d["task_id"])
            self.compute_market.submit(addr, d["task_id"], d["result_hash"], d.get("result_cid", ""))
        elif op == "nova:compute:register":
            self.compute_market.register(addr, d["cpu_cores"], d.get("gpu_model", ""),
                                         d.get("gpu_vram_gb", 0), d["ram_gb"], d["storage_gb"],
                                         region=d.get("region", ""),
                                         latency_ms=d.get("latency_ms", 50))
        elif op == "nova:compute:bid":
            self.compute_market.bid(addr, d["task_id"], d["price"])
        elif op == "nova:compute:award":
            self.compute_market.award(addr, d["task_id"], d["workers"])
        elif op == "nova:compute:arbitrate":
            self.compute_market.arbitrate(addr, d["task_id"], d["result_hash"])
        elif op == "nova:compute:dispute":
            self.compute_market.dispute(addr, d["task_id"], d.get("reason", ""))
        elif op == "nova:compute:vote":
            self.compute_market.vote(addr, d["task_id"], d["support"])
        elif op == "nova:compute:stake":
            self.compute_market.stake(addr, tx.amount)
        elif op == "nova:compute:unstake":
            self.compute_market.unstake(addr, tx.amount)
        elif op == "nova:compute:claim":
            self.compute_market.claim(addr)
        elif op == "nova:compute:audit":
            self.compute_market.audit_submit(addr, d["task_id"], d["result_hash"])

    def _apply_ai_op(self, tx):
        addr = tx.sender
        self.balances[addr] = self.balances.get(addr, 0) - self.gas_of(addr)
        d = json.loads(tx.data)
        op = d.get("op")
        if op == "nova:ai:svc:register":
            self.ai_service.svc_register(addr, tx.txid, d)
        elif op == "nova:ai:svc:config":
            self.ai_service.svc_config(addr, d)
        elif op == "nova:ai:muso:config":
            self.ai_service.muso_config(addr, d)
        elif op == "nova:ai:work:create":
            self.ai_service.work_create(addr, tx.txid, d)
        elif op == "nova:ai:work:buy":
            self.ai_service.work_buy(addr, d["wid"], tx.amount)
        elif op == "nova:ai:trigger":
            self.ai_service.trigger(addr, tx.txid, d, tx.amount)
        elif op == "nova:ai:fund:guard":
            self.ai_service.fund_guard(addr, d)
        elif op == "nova:ai:fund:spend":
            self.ai_service.fund_spend(addr, d, tx.amount)

    def _apply_arb_op(self, tx):
        """仲裁合约操作：金额随交易上链（质押/保证金），合约内部完成锁定与释放。"""
        addr = tx.sender
        gas = self.gas_of(addr)
        self.balances[addr] = self.balances.get(addr, 0) - (tx.amount + gas)
        self.arbitration.apply_op(tx)

    def gas_of(self, addr) -> float:
        """声誉驱动的交易费：信誉分 >= 80 享受 50% 折扣。"""
        try:
            rep = self.socialfi.reputation(addr)
            return round(self.economy.FIXED_GAS * rep["fee_multiplier"], 8)
        except Exception:
            return self.economy.FIXED_GAS

    def _record_tx(self, tx: Tx):
        """Record a confirmed tx into the local ledger (idempotent by txid)."""
        if tx.txid in self.store.tx_history:
            return
        if tx.sender == "0x0000":
            gas = 0.0
        elif (self._is_stake_op(tx) or self._is_storage_op(tx) or self._is_storage_inc_op(tx)
              or self._is_compute_op(tx) or self._is_ai_op(tx) or self._is_socialfi_op(tx)
              or self._is_arb_op(tx)):
            gas = self.gas_of(tx.sender)
        else:
            gas = self.economy.FIXED_GAS
        self.store.tx_history[tx.txid] = {
            "txid": tx.txid,
            "sender": tx.sender,
            "receiver": tx.receiver,
            "amount": tx.amount,
            "gas": gas,
            "data": tx.data,
            "ts": tx.timestamp,
            "confirmed_at": time.time(),
        }

    def _is_stake_op(self, tx: Tx) -> bool:
        return tx.data in self.STAKE_OPS and tx.sender == tx.receiver

    def _apply_stake_op(self, tx: Tx):
        addr = tx.sender
        gas = self.gas_of(addr)
        if tx.data == "nova:stake":
            self.balances[addr] = self.balances.get(addr, 0) - (tx.amount + gas)
            self.store.stakes[addr] = self.store.stakes.get(addr, 0) + tx.amount
            # 超级节点自动注册为存储节点（激励系统，无需额外配置）
            self.storage_incentive.auto_register(addr)
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
        if tx.sender != "0x0000":
            self.socialfi.ai_record_spend(tx.sender, tx.amount)
        if self._is_stake_op(tx):
            self._apply_stake_op(tx)
            return
        if self._is_storage_op(tx):
            self._apply_storage_op(tx)
            return
        if self._is_storage_inc_op(tx):
            self._apply_storage_inc_op(tx)
            return
        if self._is_compute_op(tx):
            self._apply_compute_op(tx)
            return
        if self._is_ai_op(tx):
            self._apply_ai_op(tx)
            return
        if self._is_socialfi_op(tx):
            self.balances[tx.sender] = self.balances.get(tx.sender, 0) - self.gas_of(tx.sender)
            self.socialfi.apply_op(tx)
            return
        if self._is_arb_op(tx):
            self._apply_arb_op(tx)
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

        if tx.receiver in self.contracts and tx.sender != "0x0000":
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
            code = self.store.contract_code.get(tx.receiver)
            if not code:
                try:
                    code = NexLangCompiler().compile(self.contracts[tx.receiver])
                    self.store.contract_code[tx.receiver] = code
                except Exception:
                    code = []
            if code:
                try:
                    vm = NexusVM(code)
                    vm.run(msg=tx.data, amount=tx.amount, sender=tx.sender,
                           storage=self.store.contract_state.setdefault(tx.receiver, {}))
                except Exception:
                    pass

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
            self._record_tx(tx)
            await self.p2p.gossip({"type":"new_tx","tx":tx.to_dict()})

    async def process_message(self, msg, peer, writer=None):
        mtype = msg.get("type")
        if mtype == "new_tx":
            tx = Tx.from_dict(msg["tx"])
            if not self.security.is_replay(tx.txid) and self.validate_tx(tx):
                self.security.mark_processed(tx.txid)
                self.store.dag.add(tx.txid)
                self.apply_tx(tx)
                self._record_tx(tx)
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
                writer.write(json.dumps({"type": "state_snapshot", "snapshot": self.full_snapshot()}).encode() + b"\n")
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
        now = time.time()
        last = getattr(self, "_last_maintenance", None)
        delta = min(now - last, 86400) if last else 0.0
        self._last_maintenance = now
        for addr in list(self.store.miner_registry):
            self.store.miner_uptime[addr] = self.store.miner_uptime.get(addr, 0) + delta
            if self.store.miner_uptime[addr] >= 270 * 86400:
                self.store.miner_qualified.add(addr)
        self.storage_net.settle_expired()
        # 存储激励：结算昨日奖励、扫描离线、热门文件保护、濒危恢复、退出迁移
        self.storage_incentive.settle_epoch(day_index() - 1)
        self.storage_incentive.finalize_exits()
        self.storage_incentive.scan_offline()
        self.storage_incentive.protect_hot_files(day_index() - 2)
        self.storage_incentive.reassign_endangered()
        self.compute_market.maintain()
        self.ai_service.maintain()
        self.socialfi.maintain()
        self.arbitration.maintain()
        self.economy.release_early_rewards()
        if self.state_file:
            self.save_state()
        print("[MAINT] 每日维护完成")

    async def _storage_monitor_loop(self):
        """存储节点监控：每 5 分钟扫描心跳（30 分钟超时判离线）并自动恢复濒危文件。"""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                self.storage_incentive.scan_offline()
                self.storage_incentive.reassign_endangered()
            except Exception as e:
                print(f"[STORAGE-MONITOR] 监控失败: {e}")

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
            "storage_nodes":len(self.store.inc_nodes),
            "storage_files":len(self.store.inc_files),
            "compute_tasks":len(self.store.compute_tasks),
            "compute_nodes": len(self.store.compute_nodes),
            "compute_stakes": round(sum(self.store.compute_stakes.values()), 8),
            "ai_works": len(self.store.ai_works),
            "ai_fund": self.store.balances.get(AI_FUND, 0.0),
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
        bytecode = str(b["bytecode"])
        if not bytecode or len(bytecode) > self.security.MAX_CONTRACT_SIZE:
            return web.json_response({"error":"bytecode 无效或过大"}, status=400)
        addr = deploy_address(bytecode)
        creator = b.get("creator", "")
        if creator:
            sig_msg = "deploy:{0}:{1}".format(addr, bytecode)
            if not verify_quantum_tx(sig_msg, b.get("signature", ""),
                                     b.get("sender_public_key", ""), creator):
                return web.json_response({"error":"creator 签名验证失败"}, status=400)
            if creator in self.store.contract_creator.values():
                return web.json_response({"error":"该地址已有合约"}, status=400)
        tx = Tx("0x0000", addr, 0, [], bytecode)
        self.store.dag.add(tx.txid)
        self.store.contracts[addr] = bytecode
        try:
            compiled = NexLangCompiler().compile(bytecode)
        except Exception:
            compiled = []
        self.store.contract_code[addr] = compiled
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

    async def rpc_txs(self, req):
        """List confirmed txs for an address (local ledger, newest first)."""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info['addr']
        if not ADDRESS_RE.match(addr):
            return web.json_response({"error": "地址格式无效"}, status=400)
        items = [(i, v) for i, v in enumerate(self.store.tx_history.values())
                 if v["sender"] == addr or v["receiver"] == addr]
        items.sort(key=lambda iv: (iv[1].get("ts", 0), iv[0]), reverse=True)
        return web.json_response({"addr": addr, "txs": [v for _, v in items]})

    async def rpc_tx(self, req):
        """Fetch a single confirmed tx by txid."""
        guard = await self._rpc_guard(req)
        if guard: return guard
        entry = self.store.tx_history.get(req.match_info['txid'])
        if not entry:
            return web.json_response({"error": "交易不存在或尚未上链"}, status=404)
        return web.json_response(entry)

    async def rpc_contract(self, req):
        """查询地址是否为合约（用于钱包转账前的恶意合约风险提示）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info['addr']
        if not ADDRESS_RE.match(addr):
            return web.json_response({"error": "地址格式无效"}, status=400)
        if addr not in self.store.contracts:
            return web.json_response({"addr": addr, "is_contract": False})
        return web.json_response({
            "addr": addr,
            "is_contract": True,
            "creator": self.store.contract_creator.get(addr) or "0x0000",
            "code_size": len(self.store.contracts.get(addr, "")),
        })

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
        self.security.record_checkin(addr)

        if addr not in self.store.early_airdrop_received:
            active_light = sum(1 for a in self.store.light_checkins if self.store.light_checkins[a] > 0)
            if active_light <= 8100:
                self.economy.early_airdrop(addr, "light")

        self.security.ip_registry.setdefault(ip, {})[f"light_{addr}"] = time.time()
        if fingerprint:
            self.security.record_device(fingerprint, addr)

        if self.store.light_checkins[addr] >= 270:
            self.store.light_qualified.add(addr)

        return web.json_response({"status":"签到成功","total_days":self.store.light_checkins[addr]})

    async def rpc_early_info(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.query.get("addr","")
        lock = self.store.locked_balances.get(addr, {})
        return web.json_response({
            "miner_registered": addr in self.store.miner_registry,
            "miner_uptime_days": self.store.miner_uptime.get(addr, 0) / 86400,
            "light_checkin_days": self.store.light_checkins.get(addr, 0),
            "locked_balance": lock.get("amount", 0),
            "lock_start_time": lock.get("start_time", 0),
            "lock_unlocked": lock.get("unlocked", 0),
            "referral_count": sum(1 for inv in self.store.referrals.values() if inv == addr),
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
        if not ADDRESS_RE.match(addr):
            return web.json_response({"error": "地址格式无效"}, status=400)
        if not isinstance(ids, list) or not ids:
            return web.json_response({"error": "ids 无效"}, status=400)
        for mid in ids:
            if not isinstance(mid, str) or len(mid) != 48:
                return web.json_response({"error": "ids 格式无效"}, status=400)
            try:
                int(mid, 16)
            except ValueError:
                return web.json_response({"error": "ids 格式无效"}, status=400)
        sig_msg = "ack:" + addr + ":" + json.dumps(sorted(set(ids)))
        if not verify_quantum_tx(sig_msg, b.get("signature", ""),
                                 b.get("sender_public_key", ""), addr):
            return web.json_response({"error": "签名验证失败"}, status=400)
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

    # ---------- 存储激励 RPC（存储状态 / 节点 / 挑战证明 / 收益 / 监控） ----------
    async def rpc_storage_inc_file(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("cid") or not b.get("fragment_commit"):
            return web.json_response({"error": "缺少 addr/cid/fragment_commit"}, status=400)
        try:
            data = json.dumps({"op": "nova:storage:inc:file", "cid": str(b["cid"]),
                               "size_gb": float(b["size_gb"]), "fragment_commit": str(b["fragment_commit"]),
                               "title": str(b.get("title", "")), "content_type": str(b.get("content_type", "music"))},
                              ensure_ascii=False)
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "参数无效"}, status=400)
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "文件已登记（片段承诺已上链）", "cid": b["cid"], "txid": tx.txid})

    async def rpc_storage_inc_claim(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("cid"):
            return web.json_response({"error": "缺少 addr/cid"}, status=400)
        data = json.dumps({"op": "nova:storage:inc:claim", "cid": str(b["cid"])})
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已认领存储", "cid": b["cid"], "txid": tx.txid})

    async def rpc_storage_prove(self, req):
        """节点提交存储证明：返回挑战文件的正确片段（前 1KB）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error": "缺少 addr"}, status=400)
        try:
            day = int(b.get("day", self.storage_incentive.current_challenge(b["addr"]).get("day", 0)))
            files = [str(x) for x in b.get("files", [])]
            fragments = [str(x) for x in b.get("fragments", [])]
        except (TypeError, ValueError):
            return web.json_response({"error": "参数无效"}, status=400)
        data = json.dumps({"op": "nova:storage:inc:prove", "day": day,
                           "files": files, "fragments": fragments}, ensure_ascii=False)
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则/挑战不匹配）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已提交存储证明", "day": day, "txid": tx.txid})

    async def rpc_storage_heartbeat(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error": "缺少 addr"}, status=400)
        tx = self._special_tx(b, json.dumps({"op": "nova:storage:inc:heartbeat"}))
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "心跳已记录", "txid": tx.txid})

    async def rpc_storage_inc_upgrade(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error": "缺少 addr"}, status=400)
        amount = b.get("amount", 0)
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount) or amount <= 0:
            return web.json_response({"error": "质押金额无效"}, status=400)
        tx = self._special_tx(b, json.dumps({"op": "nova:storage:inc:upgrade"}), amount)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "配额升级质押已提交", "txid": tx.txid})

    async def rpc_storage_inc_exit(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error": "缺少 addr"}, status=400)
        tx = self._special_tx(b, json.dumps({"op": "nova:storage:inc:exit"}))
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已声明退出，7 天后迁移数据并释放质押", "txid": tx.txid})

    async def rpc_storage_inc_settle(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict):
            return web.json_response({"error": "缺少参数"}, status=400)
        tx = self._special_tx(b, json.dumps({"op": "nova:storage:inc:settle"}))
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已触发结算", "txid": tx.txid})

    async def rpc_storage_inc_protect(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict):
            return web.json_response({"error": "缺少参数"}, status=400)
        tx = self._special_tx(b, json.dumps({"op": "nova:storage:inc:protect"}))
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已触发热门文件保护", "txid": tx.txid})

    async def rpc_storage_inc_reassign(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict):
            return web.json_response({"error": "缺少参数"}, status=400)
        tx = self._special_tx(b, json.dumps({"op": "nova:storage:inc:reassign"}))
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已触发濒危文件自动恢复", "txid": tx.txid})

    async def rpc_storage_inc_access(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("cid"):
            return web.json_response({"error": "缺少 addr/cid"}, status=400)
        tx = self._special_tx(b, json.dumps({"op": "nova:storage:inc:access", "cid": str(b["cid"])}))
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "访问量已记录", "txid": tx.txid})

    async def rpc_storage_inc_reupload(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr") or not b.get("old_cid") or not b.get("new_cid"):
            return web.json_response({"error": "缺少 addr/old_cid/new_cid"}, status=400)
        try:
            data = json.dumps({"op": "nova:storage:inc:reupload", "old_cid": str(b["old_cid"]),
                               "new_cid": str(b["new_cid"]), "size_gb": float(b["size_gb"]),
                               "fragment_commit": str(b["fragment_commit"]),
                               "title": str(b.get("title", ""))}, ensure_ascii=False)
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "参数无效"}, status=400)
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "已重新上传并替换 IPFS 哈希", "txid": tx.txid})

    async def rpc_storage_status(self, req):
        """GET /api/storage/status/{file_hash}：查询文件存储状态（🟢/🟡/🔴）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        cid = req.match_info.get("file_hash", "")
        st = self.storage_incentive.file_status(cid)
        if not st.get("found"):
            return web.json_response({"error": "文件未登记", "cid": cid}, status=404)
        return web.json_response(st)

    async def rpc_storage_nodes(self, req):
        """GET /api/storage/nodes：查询全网存储节点列表（含配额/在线/收益/健康度）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        nodes = {}
        for addr in self.store.inc_nodes:
            st = self.storage_incentive.node_stats(addr)
            nodes[addr] = st
        return web.json_response({"nodes": nodes, "total": len(nodes)})

    async def rpc_storage_challenge(self, req):
        """GET /api/storage/nodes/{addr}/challenge：获取节点当前存储证明挑战。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info.get("addr", "")
        return web.json_response(self.storage_incentive.current_challenge(addr))

    async def rpc_storage_revenue(self, req):
        """GET /api/storage/nodes/{addr}/revenue：节点存储收益统计。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info.get("addr", "")
        st = self.storage_incentive.node_stats(addr)
        if not st.get("found"):
            return web.json_response({"error": "节点未注册", "addr": addr}, status=404)
        return web.json_response(st)

    async def rpc_storage_creator(self, req):
        """GET /api/storage/creator/{addr}：创作者面板（已发布文件存储状态 + 事件通知）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info.get("addr", "")
        files = []
        for cid, f in self.store.inc_files.items():
            if f.get("owner") != addr:
                continue
            st = self.storage_incentive.file_status(cid)
            files.append(st)
        events = [e for e in self.store.inc_events.values() if e.get("creator") == addr]
        events.sort(key=lambda e: e.get("at", 0), reverse=True)
        return web.json_response({"addr": addr, "files": files, "events": events[:50],
                                  "unread": sum(1 for e in events if not e.get("read"))})

    async def rpc_storage_events(self, req):
        """GET /api/storage/events[?addr=]：链上存储事件（创作者通知/惩罚记录）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.query.get("addr", "")
        events = [e for e in self.store.inc_events.values() if not addr or e.get("creator") == addr]
        events.sort(key=lambda e: e.get("at", 0), reverse=True)
        return web.json_response({"events": events[:100], "total": len(self.store.inc_events)})

    async def rpc_storage_inc_summary(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response(self.storage_incentive.summary())

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
        return web.json_response({"tasks": self._ser(self.store.compute_tasks)})

    async def rpc_compute_register(self, req):
        """POST /api/compute/register：节点声明算力规格（CPU/GPU/内存/存储）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error": "缺少 addr"}, status=400)
        try:
            data = json.dumps({
                "op": "nova:compute:register",
                "cpu_cores": int(b["cpu_cores"]),
                "gpu_model": str(b.get("gpu_model", "")),
                "gpu_vram_gb": float(b.get("gpu_vram_gb", 0)),
                "ram_gb": float(b["ram_gb"]),
                "storage_gb": float(b["storage_gb"]),
                "region": str(b.get("region", "")),
                "latency_ms": float(b.get("latency_ms", 50)),
            }, ensure_ascii=False)
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "参数无效"}, status=400)
        tx = self._special_tx(b, data)
        if not self.validate_tx(tx):
            return web.json_response({"error": "交易校验失败（签名/规则）"}, status=400)
        await self.broadcast_tx(tx)
        return web.json_response({"status": "算力节点已注册", "txid": tx.txid})

    async def rpc_compute_nodes(self, req):
        """GET /api/compute/nodes：算力节点列表（规格/信誉/质押/收益，公开可查）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        nodes = {}
        seen = set(self.store.compute_nodes)
        for addr in self.store.compute_nodes:
            nodes[addr] = self.compute_market.node_view(addr)
        # 超级节点自动具备算力提供资格
        auto = (set(self.store.stakes) | set(self.store.miner_registry) | set(self.store.inc_nodes))
        for addr in auto:
            if addr not in seen:
                nodes[addr] = self.compute_market.node_view(addr)
        return web.json_response({"nodes": nodes, "total": len(nodes)})

    async def rpc_compute_node(self, req):
        """GET /api/compute/node/{addr}：单节点详情（规格/信誉/收益）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info.get("addr", "")
        v = self.compute_market.node_view(addr)
        if not v.get("found"):
            return web.json_response({"error": "节点未注册", "addr": addr}, status=404)
        return web.json_response(v)

    async def rpc_compute_income(self, req):
        """GET /api/compute/income/{addr}：节点收益统计接口。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info.get("addr", "")
        return web.json_response(self.compute_market.node_income(addr))

    async def rpc_compute_overview(self, req):
        """GET /api/compute/overview：算力网络总览（节点/任务/质押/审计/激励池/参考价）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response(self._ser(self.compute_market.overview()))

    async def rpc_compute_events(self, req):
        """GET /api/compute/events：算力网络链上事件流。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({"events": self._ser(self.compute_market.events())})

    # ---------- AI 生成服务 RPC（提示词 3） ----------
    async def rpc_ai_services(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({"services": self._ser(self.store.ai_services)})

    async def rpc_ai_works(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        works = sorted(self.store.ai_works.values(), key=lambda w: w.get("created_at", 0), reverse=True)
        return web.json_response({"works": self._ser(works), "total": len(works)})

    async def rpc_ai_fund(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response(self._ser(self.ai_service.fund_view()))

    async def rpc_ai_status(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response(self._ser(self.ai_service.overview()))

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
            "text": {"assets": self.store.text_assets,
                     "contract_pubkey": self.socialfi.text_contract_pubkey(),
                     "deposit_tiers": {"basic": self.socialfi.text_deposit_required("basic"),
                                       "advanced": self.socialfi.text_deposit_required("advanced"),
                                       "pro": self.socialfi.text_deposit_required("pro")},
                     "reputation": self.store.text_reputation,
                     "escrow": self.store.balances.get(TEXT_ESCROW, 0)},
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
            "text_assets": len(self.store.text_assets),
            "text_escrow": self.store.balances.get(TEXT_ESCROW, 0),
            "events": len(self.store.socialfi_events),
            "graph_hash": self.socialfi.graph_hash(),
        })

    async def rpc_text_key(self, req):
        """返回文本合约公钥：作者用它锁定密文正文密钥，购买后合约二次加密给买家。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({"public_key": self.socialfi.text_contract_pubkey()})

    async def rpc_reputation(self, req):
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info['addr']
        return web.json_response(self.socialfi.reputation(addr))

    async def rpc_ai_list(self, req):
        out = []
        for addr, identity in self.store.ai_creators.items():
            v = dict(identity)
            v["budget"] = self.socialfi.ai_budget_state(addr)
            out.append(v)
        return web.json_response({"count": len(out), "creators": out})

    async def rpc_ai_view(self, req):
        addr = req.match_info.get("addr", "")
        identity = self.socialfi.ai_identity(addr)
        if not identity:
            return web.json_response({"error": "not_found"}, status=404)
        view = dict(identity)
        view["budget"] = self.socialfi.ai_budget_state(addr)
        events = sorted((e for e in self.store.socialfi_events.values()
                         if e.get("op", "").startswith("nova:ai:") and
                         (e.get("addr") == addr or e.get("id") == addr)),
                        key=lambda e: e.get("ts", 0), reverse=True)[:20]
        view["recent_ops"] = events
        return web.json_response(view)

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
    # ---------- 社区仲裁 RPC ----------
    async def rpc_arb_summary(self, req):
        """GET /api/arb/summary：仲裁系统全局概况。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response(self.arbitration.summary())

    async def rpc_arb_arbitrators(self, req):
        """GET /api/arb/arbitrators：在职仲裁员列表（地址/信誉分/累计案件数）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({"arbitrators": self.arbitration.list_arbitrators(),
                                  "total": len(self.store.arb_arbitrators)})

    async def rpc_arb_candidates(self, req):
        """GET /api/arb/candidates：候选池（地址/申请时间/投票状态）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        return web.json_response({"candidates": self.arbitration.list_candidates(),
                                  "total": len(self.store.arb_candidates)})

    async def rpc_arb_cases(self, req):
        """GET /api/arb/cases：案件公示列表（已裁决公开，在途匿名）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        viewer = req.query.get("viewer", "")
        return web.json_response({"cases": self.arbitration.list_cases(viewer),
                                  "total": len(self.store.arb_cases)})

    async def rpc_arb_case(self, req):
        """GET /api/arb/cases/{case_id}：案件详情（当事人匿名，仅编号）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        cid = req.match_info.get("case_id", "")
        viewer = req.query.get("viewer", "")
        pub = self.arbitration.case_public(cid, viewer)
        if not pub:
            return web.json_response({"error": "案件不存在", "case_id": cid}, status=404)
        return web.json_response(pub)

    async def rpc_arb_user(self, req):
        """GET /api/arb/user/{addr}：普通用户面板（我的投诉/历史/保证金档位）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info.get("addr", "")
        return web.json_response(self.arbitration.user_panel(addr))

    async def rpc_arb_panel(self, req):
        """GET /api/arb/panel/{addr}：仲裁员面板（待处理/裁决历史/信誉分收益/任期）。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info.get("addr", "")
        return web.json_response(self.arbitration.arbitrator_panel(addr))

    async def rpc_arb_notifications(self, req):
        """GET /api/arb/notifications/{addr}：链上通知列表。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        addr = req.match_info.get("addr", "")
        return web.json_response({"notifications": self.arbitration.notifications(addr),
                                  "unread": sum(1 for n in self.arbitration.notifications(addr)
                                                if not n.get("read"))})

    async def rpc_arb_read(self, req):
        """POST /api/arb/notifications/read：标记通知已读。"""
        guard = await self._rpc_guard(req)
        if guard: return guard
        b = await self._read_json(req)
        if not isinstance(b, dict) or not b.get("addr"):
            return web.json_response({"error": "缺少 addr"}, status=400)
        n = self.arbitration.mark_read(str(b["addr"]), b.get("ids"))
        return web.json_response({"status": "ok", "marked": n})

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
        asyncio.create_task(self._storage_monitor_loop())
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







