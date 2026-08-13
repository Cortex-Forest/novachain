import time
from collections import defaultdict
import json
from typing import Dict, List

class SecurityManager:
    MAX_TX_SIZE = 102400
    MAX_CONTRACT_SIZE = 102400
    RATE_LIMIT = 100
    RATE_WINDOW = 1

    def __init__(self):
        self.request_log = defaultdict(list)
        self.processed_txids = set()
        self.ip_registry: Dict[str, Dict[str, float]] = {}
        self.device_fingerprints: Dict[str, str] = {}
        self.checkin_history: Dict[str, List[float]] = {}

    def check_rate_limit(self, ip: str) -> bool:
        now = time.time()
        self.request_log[ip] = [t for t in self.request_log[ip] if now - t < self.RATE_WINDOW]
        if len(self.request_log[ip]) >= self.RATE_LIMIT: return False
        self.request_log[ip].append(now)
        return True

    def validate_size(self, tx_dict: dict) -> bool:
        if len(json.dumps(tx_dict)) > self.MAX_TX_SIZE: return False
        if tx_dict.get("data") and len(tx_dict["data"]) > self.MAX_CONTRACT_SIZE: return False
        return True

    def is_replay(self, txid: str) -> bool:
        return txid in self.processed_txids

    def mark_processed(self, txid: str):
        self.processed_txids.add(txid)

    def check_ip_limit(self, ip: str, role: str) -> bool:
        now = time.time()
        if ip not in self.ip_registry:
            self.ip_registry[ip] = {}
        self.ip_registry[ip] = {k:v for k,v in self.ip_registry[ip].items() if now - v < 86400}
        role_count = sum(1 for k in self.ip_registry[ip] if k.startswith(role))
        if role == "miner" and role_count >= 1: return False
        if role == "light" and role_count >= 1: return False
        return True

    MAX_DEVICE_FINGERPRINTS = 100000
    MAX_CHECKIN_HISTORY = 30

    def check_device_unique(self, fingerprint: str) -> bool:
        return fingerprint not in self.device_fingerprints

    def record_device(self, fingerprint: str, addr: str):
        if fingerprint not in self.device_fingerprints and len(self.device_fingerprints) >= self.MAX_DEVICE_FINGERPRINTS:
            self.device_fingerprints.pop(next(iter(self.device_fingerprints)))
        self.device_fingerprints[fingerprint] = addr

    def record_checkin(self, addr: str):
        self.checkin_history.setdefault(addr, []).append(time.time())
        self.checkin_history[addr] = self.checkin_history[addr][-self.MAX_CHECKIN_HISTORY:]


    def snapshot(self):
        return {
            "processed_txids": sorted(self.processed_txids),
            "ip_registry": self.ip_registry,
            "device_fingerprints": self.device_fingerprints,
            "checkin_history": self.checkin_history,
        }

    def restore(self, d):
        self.processed_txids = set(d.get("processed_txids", []))
        self.ip_registry = {k: dict(v) for k, v in d.get("ip_registry", {}).items()}
        self.device_fingerprints = dict(d.get("device_fingerprints", {}))
        self.checkin_history = {k: list(v) for k, v in d.get("checkin_history", {}).items()}
    def check_checkin_interval(self, addr: str) -> bool:
        if addr not in self.checkin_history or not self.checkin_history[addr]:
            return True
        return time.time() - self.checkin_history[addr][-1] >= 72000