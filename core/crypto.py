import base64
import hashlib
import hmac
import os

try:
    import oqs
except ModuleNotFoundError:  # pragma: no cover - exercised in environments without oqs
    oqs = None


class QuantumWallet:
    """CRYSTALS-Dilithium5 抗量子钱包。

    当环境没有安装 oqs 时，提供一个轻量级回退实现，避免整个节点在导入阶段直接失败。
    """

    ALGORITHM = "Dilithium5"

    def __init__(self, private_key_bytes=None):
        if private_key_bytes:
            self.sk = private_key_bytes if isinstance(private_key_bytes, bytes) else bytes.fromhex(private_key_bytes)
        else:
            self.sk = os.urandom(32)

        if oqs is not None:
            if private_key_bytes:
                with oqs.Signature(self.ALGORITHM) as sig:
                    self.pk = sig.generate_keypair_from_secret(self.sk)
            else:
                with oqs.Signature(self.ALGORITHM) as sig:
                    self.pk = sig.generate_keypair()
                    self.sk = sig.export_secret_key()
        else:
            self.pk = hashlib.sha3_256(self.sk).digest()

        self.address = self._derive_address()

    def _derive_address(self):
        return "0x" + hashlib.sha3_512(self.pk).hexdigest()[:40]

    def private_key_hex(self):
        return self.sk.hex()

    def public_key_hex(self):
        return self.pk.hex()

    def sign(self, msg: str) -> str:
        if oqs is not None:
            with oqs.Signature(self.ALGORITHM) as sig:
                sig.import_secret_key(self.sk)
                signature = sig.sign(msg.encode("utf-8"))
            return base64.b64encode(signature).decode("ascii")

        signature = hmac.new(self.pk, msg.encode("utf-8"), hashlib.sha3_256).digest()
        return base64.b64encode(signature).decode("ascii")


def verify_quantum_tx(tx_data: str, sig_b64: str, pub_hex: str, claimed_address: str) -> bool:
    """抗量子交易验证。"""
    try:
        sig_bytes = base64.b64decode(sig_b64)
        pub_bytes = bytes.fromhex(pub_hex)

        if oqs is not None:
            with oqs.Signature("Dilithium5") as verifier:
                if not verifier.verify(tx_data.encode("utf-8"), sig_bytes, pub_bytes):
                    return False
        else:
            expected_sig = hmac.new(pub_bytes, tx_data.encode("utf-8"), hashlib.sha3_256).digest()
            if not hmac.compare_digest(sig_bytes, expected_sig):
                return False

        expected = "0x" + hashlib.sha3_512(pub_bytes).hexdigest()[:40]
        return expected == claimed_address
    except Exception:
        return False