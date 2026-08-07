import hashlib
import os

try:
    import oqs
except ModuleNotFoundError:  # pragma: no cover - exercised in environments without oqs
    oqs = None

QUANTUM_SAFE = oqs is not None


# ---------------------------------------------------------------------------
# Ed25519 (RFC 8032) 纯 Python 回退实现。
# 仅在未安装 oqs 时使用；Ed25519 不是抗量子算法，
# 生产环境请安装 oqs 以启用 CRYSTALS-Dilithium5。
# ---------------------------------------------------------------------------
_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P
_ED_BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_ED_BY = 46316835694926478169428394003475163141307993866256225615783033603165251855960
_ED_B = (_ED_BX, _ED_BY, 1, _ED_BX * _ED_BY % _ED_P)


def _ed_inv(x):
    return pow(x, _ED_P - 2, _ED_P)


def _ed_add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _ED_P
    b = (y1 + x1) * (y2 + x2) % _ED_P
    c = 2 * _ED_D * t1 * t2 % _ED_P
    d = 2 * z1 * z2 % _ED_P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P


def _ed_scalarmult(p, e):
    q = (0, 1, 1, 0)
    while e:
        if e & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        e >>= 1
    return q


def _ed_encodeint(n):
    return n.to_bytes(32, "little")


def _ed_decodeint(s):
    return int.from_bytes(s, "little")


def _ed_encodepoint(p):
    x, y, z, _ = p
    z_inv = _ed_inv(z)
    xr = x * z_inv % _ED_P
    yr = y * z_inv % _ED_P
    bits = yr | ((xr & 1) << 255)
    return _ed_encodeint(bits)


def _ed_xrecover(y):
    xx = (y * y - 1) * _ed_inv(_ED_D * y * y + 1) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P:
        x = x * pow(2, (_ED_P - 1) // 4, _ED_P) % _ED_P
    if (x * x - xx) % _ED_P:
        raise ValueError("ed25519: invalid point")
    return x


def _ed_decodepoint(s):
    y = _ed_decodeint(s) & ((1 << 255) - 1)
    x = _ed_xrecover(y)
    if (x & 1) != ((s[31] >> 7) & 1):
        x = _ED_P - x
    return x, y, 1, x * y % _ED_P


def _ed_clamp(a):
    a &= ((1 << 255) - 1) - 7
    a |= 1 << 254
    return a


def ed25519_public_key(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = _ed_clamp(_ed_decodeint(h[:32]))
    return _ed_encodepoint(_ed_scalarmult(_ED_B, a))


def ed25519_sign(seed: bytes, msg: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = _ed_clamp(_ed_decodeint(h[:32]))
    prefix = h[32:]
    r = _ed_decodeint(hashlib.sha512(prefix + msg).digest()) % _ED_L
    a_bytes = ed25519_public_key(seed)
    r_bytes = _ed_encodepoint(_ed_scalarmult(_ED_B, r))
    k = _ed_decodeint(hashlib.sha512(r_bytes + a_bytes + msg).digest()) % _ED_L
    s = (r + k * a) % _ED_L
    return r_bytes + _ed_encodeint(s)


def ed25519_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        if len(pub) != 32 or len(sig) != 64:
            return False
        r_point = _ed_decodepoint(sig[:32])
        a_point = _ed_decodepoint(pub)
        k = _ed_decodeint(hashlib.sha512(sig[:32] + pub + msg).digest()) % _ED_L
        sb = _ed_scalarmult(_ED_B, _ed_decodeint(sig[32:]))
        ra = _ed_add(r_point, _ed_scalarmult(a_point, k))
        return _ed_encodepoint(sb) == _ed_encodepoint(ra)
    except Exception:
        return False


class QuantumWallet:
    """抗量子钱包：安装 oqs 时使用 CRYSTALS-Dilithium5，否则回退 Ed25519。

    回退模式不具备抗量子性，仅用于保证开发环境可用。
    """

    ALGORITHM = "Dilithium5"

    def __init__(self, private_key_bytes=None):
        if private_key_bytes:
            self.sk = private_key_bytes if isinstance(private_key_bytes, bytes) else bytes.fromhex(private_key_bytes)
            self.seed = self.sk
        else:
            self.sk = os.urandom(32)
            self.seed = self.sk

        if oqs is not None:
            self.algorithm = "Dilithium5"
            with oqs.Signature(self.ALGORITHM) as sig:
                if private_key_bytes:
                    self.pk = sig.generate_keypair_from_secret(self.sk)
                else:
                    self.pk = sig.generate_keypair()
                self.sk = sig.export_secret_key()
        else:
            self.algorithm = "Ed25519"
            self.pk = ed25519_public_key(self.seed)

        self.address = self._derive_address()

    def _derive_address(self):
        return "0x" + hashlib.sha3_512(self.pk).hexdigest()[:40]

    def private_key_hex(self):
        return self.seed.hex()

    def public_key_hex(self):
        return self.pk.hex()

    def sign(self, msg: str) -> str:
        if oqs is not None:
            with oqs.Signature(self.algorithm) as sig:
                sig.import_secret_key(self.sk)
                signature = sig.sign(msg.encode("utf-8"))
        else:
            signature = ed25519_sign(self.seed, msg.encode("utf-8"))
        return signature.hex()


def verify_quantum_tx(tx_data: str, sig_hex: str, pub_hex: str, claimed_address: str) -> bool:
    """抗量子交易验证：Dilithium5（2592 字节公钥）或 Ed25519 回退（32 字节公钥）。"""
    try:
        sig = bytes.fromhex(sig_hex)
        pub = bytes.fromhex(pub_hex)

        if len(pub) == 32 and len(sig) == 64:
            if not ed25519_verify(pub, tx_data.encode("utf-8"), sig):
                return False
        elif len(pub) == 2592:
            if oqs is None:
                return False
            with oqs.Signature("Dilithium5") as verifier:
                if not verifier.verify(tx_data.encode("utf-8"), sig, pub):
                    return False
        else:
            return False

        expected = "0x" + hashlib.sha3_512(pub).hexdigest()[:40]
        return expected == claimed_address
    except Exception:
        return False
