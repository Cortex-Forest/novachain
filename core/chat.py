"""Off-chain E2E-encrypted chat mailbox.

The node acts as a relay: it stores only ciphertext + metadata (sender,
recipient, sender chat pubkey, nonce, timestamp). The actual message key is
derived in the browser from X25519 ECDH, so the node can never read content.
"""
import hashlib
import json
import os
import re
import time

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
PUBKEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")     # 32-byte X25519 u-coordinate
NONCE_RE = re.compile(r"^[0-9a-fA-F]{24}$")      # 12-byte AES-GCM nonce

MAX_CIPHERTEXT_HEX = 16384                        # 8 KB ciphertext
MAX_INBOX_PER_ADDR = 1000
TS_WINDOW = 86400                                 # chat 时间戳窗口：±1 天


def chat_signature_data(sender: str, recipient: str, chat_pub: str,
                        nonce: str, ciphertext: str, ts) -> str:
    """签名覆盖的规范化字符串（前后端一致）。"""
    return f"{sender}{recipient}{chat_pub}{nonce}{ciphertext}{int(ts)}"


def message_id(sender: str, recipient: str, chat_pub: str,
               nonce: str, ciphertext: str, ts) -> str:
    raw = chat_signature_data(sender, recipient, chat_pub, nonce, ciphertext, ts)
    return hashlib.sha256(raw.encode()).hexdigest()[:48]


def validate_chat_payload(b: dict) -> str:
    """校验字段格式，返回错误消息或空字符串。"""
    sender = b.get("sender", "")
    recipient = b.get("recipient", "")
    chat_pub = b.get("chat_pub", "")
    nonce = b.get("nonce", "")
    ciphertext = b.get("ciphertext", "")
    ts = b.get("ts", 0)
    if not ADDRESS_RE.match(sender) or not ADDRESS_RE.match(recipient):
        return "地址格式无效"
    if not PUBKEY_RE.match(chat_pub):
        return "聊天公钥无效"
    if not NONCE_RE.match(nonce):
        return "nonce 无效"
    if not isinstance(ciphertext, str) or not re.fullmatch(r"[0-9a-fA-F]+", ciphertext or ""):
        return "密文格式无效"
    if len(ciphertext) > MAX_CIPHERTEXT_HEX:
        return "密文过长"
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "时间戳无效"
    if abs(time.time() - ts) > TS_WINDOW:
        return "时间戳过期"
    return ""


class ChatStore:
    """per-recipient 加密信箱，节点只保存密文。"""

    def __init__(self, chat_file=None):
        self.chat_file = chat_file
        self.inbox = {}          # recipient -> {msg_id: message}
        self.pubkeys = {}        # addr -> chat X25519 public key hex
        self._ids = set()

    # ---------- 读写 ----------
    def push(self, msg: dict) -> bool:
        mid = msg["id"]
        if mid in self._ids:
            return False
        self._ids.add(mid)
        recipient = msg["recipient"]
        box = self.inbox.setdefault(recipient, {})
        box[mid] = msg
        # 按时间戳裁剪单地址信箱
        if len(box) > MAX_INBOX_PER_ADDR:
            for old in sorted(box.values(), key=lambda m: m.get("ts", 0))[:len(box) - MAX_INBOX_PER_ADDR]:
                box.pop(old["id"], None)
        return True

    def messages_for(self, addr: str):
        box = self.inbox.get(addr, {})
        return sorted(box.values(), key=lambda m: m.get("ts", 0))

    def ack(self, addr: str, ids) -> int:
        removed = 0
        box = self.inbox.get(addr)
        if not box:
            return 0
        for mid in ids or []:
            if box.pop(mid, None) is not None:
                removed += 1
        return removed

    def set_pubkey(self, addr: str, chat_pub: str) -> None:
        self.pubkeys[addr] = chat_pub

    def get_pubkey(self, addr: str):
        return self.pubkeys.get(addr)

    # ---------- 持久化 ----------
    def to_dict(self):
        return {
            "version": 1,
            "inbox": {k: list(v.values()) for k, v in self.inbox.items()},
            "pubkeys": self.pubkeys,
        }

    def from_dict(self, d):
        inbox = d.get("inbox", {})
        self.inbox = {}
        self._ids = set()
        for addr, msgs in inbox.items():
            box = {}
            for m in msgs:
                if isinstance(m, dict) and m.get("id"):
                    box[m["id"]] = m
                    self._ids.add(m["id"])
            if box:
                self.inbox[addr] = box
        self.pubkeys = {k: v for k, v in d.get("pubkeys", {}).items() if PUBKEY_RE.match(v)}

    def save(self, path=None):
        path = path or self.chat_file
        if not path:
            return
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, path)

    def load(self, path=None) -> bool:
        path = path or self.chat_file
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, encoding="utf-8") as f:
                self.from_dict(json.load(f))
            return True
        except Exception:
            return False
