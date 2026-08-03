import time
import hashlib
import json

class Tx:
    def __init__(self, sender, receiver, amount, parents=None, data="", pk="", sig="", timestamp=None, txid=None):
        self.sender = sender
        self.receiver = receiver
        self.amount = amount
        self.timestamp = time.time() if timestamp is None else timestamp
        self.parents = parents or []
        self.data = data
        self.sender_public_key = pk
        self.signature = sig
        self.txid = self.calc_txid() if txid is None else txid

    def calc_txid(self):
        raw = f"{self.sender}{self.receiver}{self.amount}{self.timestamp}{self.parents}{self.data}{self.sender_public_key}"
        return hashlib.sha3_256(raw.encode()).hexdigest()

    def to_dict(self):
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "amount": self.amount,
            "timestamp": self.timestamp,
            "parents": self.parents,
            "data": self.data,
            "sender_public_key": self.sender_public_key,
            "signature": self.signature,
            "txid": self.txid,
        }

    @staticmethod
    def from_dict(d):
        return Tx(
            d["sender"], d["receiver"], d["amount"],
            d.get("parents", []), d.get("data", ""),
            d.get("sender_public_key", ""), d.get("signature", ""),
            d.get("timestamp"), d.get("txid"),
        )

    def signing_data(self):
        return f"{self.sender}{self.receiver}{self.amount}{self.timestamp}{self.parents}{self.data}{self.sender_public_key}"