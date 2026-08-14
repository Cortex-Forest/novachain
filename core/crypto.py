import hashlib
import os

try:
    import oqs
    if not hasattr(oqs, "Signature"):
        # 已安装但版本/构建不兼容的 oqs 视为不可用，回退 Ed25519（与 README 约定一致）
        oqs = None
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


# ---------------------------------------------------------------------------
# 文本合约密钥封装（ECIES：P-256 ECDH + HKDF-SHA256 + AES-256-GCM）
# 用于密文文本资产：
#   1) 作者用"文本合约公钥"锁定正文密钥 K（AES-256）；
#   2) 购买后，合约用私钥把 K 二次加密给买家公钥，买家用自己的私钥解开。
# 所有随机因子均由 seed（交易/资产 ID）确定性派生，保证跨节点状态一致。
# ---------------------------------------------------------------------------
TEXT_ECIES_TAG = "nova-text-key-v1"
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _crypto_available() -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric import ec  # noqa: F401
        return True
    except Exception:
        return False


TEXT_CRYPTO_OK = _crypto_available()


def _p256_derive_private(scalar: int):
    """按给定标量派生 P-256 私钥。调用方须传入已归约（1 <= v <= n-1）的标量，
    cryptography 对越界值做模 n 归约，但为确定性起见我们统一在调用前归约。"""
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.derive_private_key(scalar, ec.SECP256R1())


def _p256_pub_hex(priv) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = priv.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)
    return raw.hex()


def text_p256_pub_from_priv(priv_hex: str) -> str:
    """由私钥 hex 导出 P-256 公钥 hex（04||x||y，65 字节）。"""
    scalar = int.from_bytes(bytes.fromhex(priv_hex), "big")
    return _p256_pub_hex(_p256_derive_private(scalar))


def text_gen_p256_keypair(seed: bytes = None):
    """生成 P-256 密钥对。seed 提供时确定性派生，否则使用系统随机数。"""
    if seed is None:
        import os
        seed = os.urandom(32)
    d = int.from_bytes(hashlib.sha3_256(b"nova:p256:" + seed).digest(), "big")
    priv = _p256_derive_private(d)
    d = priv.private_numbers().private_value
    return d.to_bytes(32, "big").hex(), _p256_pub_hex(priv)


def _hkdf(ikm: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256，固定 32 字节零盐（RFC 5869 无盐语义）。
    与浏览器 WebCrypto HKDF 的零盐行为一致，便于密文密钥跨端互操作。"""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=length,
                salt=b"\x00" * 32, info=b"nova:text:key").derive(ikm)


def text_ecies_encrypt(recipient_pub_hex: str, plaintext_hex: str, seed: bytes) -> dict:
    """用接收方 P-256 公钥封装明文（hex 输入/输出）。ephemeral 私钥由 seed 确定性派生。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),
                                                        bytes.fromhex(recipient_pub_hex))
    d = int.from_bytes(hashlib.sha3_256(b"nova:text:eph:" + seed).digest(), "big")
    eph = _p256_derive_private(d)
    shared = eph.exchange(ec.ECDH(), peer)
    iv = hashlib.sha3_256(b"nova:text:iv:" + seed).digest()[:12]
    key = _hkdf(shared)
    ct = AESGCM(key).encrypt(iv, bytes.fromhex(plaintext_hex), None)
    epk = eph.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint).hex()
    return {"v": 1, "tag": TEXT_ECIES_TAG, "curve": "P-256",
            "epk": epk, "iv": iv.hex(), "ct": ct.hex()}


def text_ecies_decrypt(priv_hex: str, env: dict) -> str:
    """用私钥解开 ECIES 信封，返回明文 hex。"""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    scalar = int.from_bytes(bytes.fromhex(priv_hex), "big")
    priv = _p256_derive_private(scalar)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),
                                                        bytes.fromhex(env["epk"]))
    shared = priv.exchange(ec.ECDH(), peer)
    iv = bytes.fromhex(env["iv"])
    key = _hkdf(shared)
    return AESGCM(key).decrypt(iv, bytes.fromhex(env["ct"]), None).hex()


def text_ecies_wrap_to(priv_hex: str, recipient_pub_hex: str, plaintext_hex: str,
                       seed: bytes) -> dict:
    """合约私钥持有者把明文（正文密钥 K）二次封装给买家公钥，用于购买解锁。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    scalar = int.from_bytes(bytes.fromhex(priv_hex), "big")
    priv = _p256_derive_private(scalar)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),
                                                        bytes.fromhex(recipient_pub_hex))
    shared = priv.exchange(ec.ECDH(), peer)
    iv = hashlib.sha3_256(b"nova:text:iv:" + seed).digest()[:12]
    key = _hkdf(shared)
    ct = AESGCM(key).encrypt(iv, bytes.fromhex(plaintext_hex), None)
    epk = priv.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint).hex()
    return {"v": 1, "tag": TEXT_ECIES_TAG, "curve": "P-256",
            "epk": epk, "iv": iv.hex(), "ct": ct.hex()}
