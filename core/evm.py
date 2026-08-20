# -*- coding: utf-8 -*-
"""EVM 兼容层（v0.11）：
① 纯 Python Keccak-256（以太坊版，非 NIST SHA-3）
② secp256k1 椭圆曲线：ECDSA 验签 + 公钥恢复（MetaMask 签名交易用）
③ RLP 编码/解码（以太坊签名交易格式）
④ 轻量 EVM 解释器：覆盖 Solidity 0.8 常见字节码操作码子集
   （算术/比较/位运算/内存/存储/跳转/事件/调用），带 gas 计量与沙盒限制。

不引入任何第三方依赖（无 eth 库），保证可离线确定性执行。
"""
import hashlib

CHAIN_ID = 666666                 # Nova EVM Chain ID（0xA23A2）
ADDR_LEN = 20                     # EVM 地址 20 字节
WEI_SCALE = 10 ** 18              # NOVA 1 = 1e18 wei（MetaMask 精度）
GAS_WEI = 10 ** 9                 # 1 gas 定价基准（gwei 级）


# ===========================================================================
# ① Keccak-256（以太坊哈希）
# ===========================================================================
_KECCAK_ROUNDS = 24
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
]
_PI = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
]


def _rol(x, n):
    n %= 64
    return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def _keccak_f(state):
    for rc in _RC:
        # Theta
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= d[x]
        # Rho + Pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(state[x][y], _ROT[x][y])
        # Chi
        for x in range(5):
            for y in range(5):
                state[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        # Iota
        state[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    """Keccak-256（以太坊）：rate=136 bytes（0x01 填充）。"""
    rate = 136
    state = [[0] * 5 for _ in range(5)]
    # absorb
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)
    # squeeze 32 bytes
    out = bytearray()
    lane_idx = 0
    while len(out) < 32:
        lane = state[lane_idx % 5][lane_idx // 5]
        out += lane.to_bytes(8, "little")
        lane_idx += 1
        if lane_idx == 25:
            _keccak_f(state)
            lane_idx = 0
    return bytes(out[:32])


# ===========================================================================
# ② secp256k1 + ECDSA
# ===========================================================================
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
_A = 0


def _inv(a, m):
    return pow(a, m - 2, m)


def _point_add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p == q:
        lam = (3 * x1 * x1 + _A) * _inv(2 * y1, _P) % _P
    else:
        lam = (y2 - y1) * _inv((x2 - x1) % _P, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _point_mul(k, p=None):
    p = p or _G
    r = None
    while k:
        if k & 1:
            r = _point_add(r, p)
        p = _point_add(p, p)
        k >>= 1
    return r


def _bytes_to_int(b):
    return int.from_bytes(b, "big")


def ecdsa_pubkey_from_private(priv: bytes) -> bytes:
    """由私钥导出 64 字节未压缩公钥（用于测试/工具，不保存私钥）。"""
    d = _bytes_to_int(priv) % _N
    q = _point_mul(d)
    return q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")


def _recover_pubkey(msg_hash: bytes, r, s, recid: int) -> bytes:
    """从签名恢复 64 字节未压缩公钥（0x04 || X || Y）。
    recid 为 0/1（pre-EIP155: v-27；EIP155: (v-35)%2）。
    Q = r^-1 * (sR - eG)，R 的 x = r 或 r+N 取决于 recid 的 x 段。"""
    if not (0 <= recid <= 3):
        raise ValueError("invalid recovery id")
    e = _bytes_to_int(msg_hash)
    # 候选 x：recid 0/1 -> x=r；recid 2/3 -> x=r+N
    x = r if recid < 2 else r + _N
    x %= _P
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)  # P % 4 == 3
    if pow(y, 2, _P) != y_sq:
        raise ValueError("invalid point x")
    if (y & 1) != (recid & 1):
        y = _P - y
    R = (x, y)
    r_inv = pow(r, _N - 2, _N)
    sR = _point_mul(s, R)
    eG = _point_mul(e % _N, _G)
    Q = _point_mul(r_inv, _point_add(sR, (eG[0], (-eG[1]) % _P)))
    return Q[0].to_bytes(32, "big") + Q[1].to_bytes(32, "big")


def ecdsa_verify(pub: bytes, msg_hash: bytes, r, s) -> bool:
    """secp256k1 ECDSA 验签（pub 为 64 字节未压缩）。"""
    try:
        if len(pub) != 64:
            return False
        if not (1 <= r < _N and 1 <= s < _N):
            return False
        z = _bytes_to_int(msg_hash)
        w = _inv(s, _N)
        u1 = z * w % _N
        u2 = r * w % _N
        pt = _point_add(_point_mul(u1, _G), _point_mul(u2, (int.from_bytes(pub[:32], "big"), int.from_bytes(pub[32:], "big"))))
        if pt is None:
            return False
        return pt[0] % _N == r
    except Exception:
        return False


def evm_address_from_pubkey(pub: bytes) -> str:
    """EVM 地址 = keccak256(pub)[-20:]（标准以太坊地址派生）。"""
    return "0x" + keccak256(pub)[-ADDR_LEN:].hex()


def create_address(sender_hex: str, nonce: int) -> str:
    """CREATE 语义合约地址 = keccak256(rlp([sender, nonce]))[-20:]。"""
    rlp_s = rlp_encode([bytes.fromhex(sender_hex[2:]), nonce])
    return "0x" + keccak256(rlp_s)[-ADDR_LEN:].hex()


# ===========================================================================
# ③ RLP 编码/解码
# ===========================================================================
def rlp_encode(obj):
    """RLP 编码：bytes / str(hex) / int / list 递归。"""
    if isinstance(obj, str):
        if obj.startswith("0x"):
            obj = bytes.fromhex(obj[2:])
        else:
            obj = obj.encode()
    if isinstance(obj, int):
        if obj == 0:
            obj = b""
        else:
            obj = obj.to_bytes((obj.bit_length() + 7) // 8, "big")
    if isinstance(obj, bytes):
        if len(obj) == 1 and obj[0] < 0x80:
            return obj
        if len(obj) <= 55:
            return bytes([0x80 + len(obj)]) + obj
        ln = len(obj)
        len_bytes = ln.to_bytes((ln.bit_length() + 7) // 8, "big")
        return bytes([0xB7 + len(len_bytes)]) + len_bytes + obj
    if isinstance(obj, list):
        payload = b"".join(rlp_encode(item) for item in obj)
        if len(payload) <= 55:
            return bytes([0xC0 + len(payload)]) + payload
        ln = len(payload)
        len_bytes = ln.to_bytes((ln.bit_length() + 7) // 8, "big")
        return bytes([0xF7 + len(len_bytes)]) + len_bytes + payload
    raise TypeError(f"unsupported rlp type: {type(obj)}")


def _rlp_decode(data: bytes, pos=0):
    """返回 (obj, new_pos)。str(bytes) 返回 bytes，int 保持 bytes（由调用方解释）。"""
    if pos >= len(data):
        raise ValueError("rlp: unexpected end")
    prefix = data[pos]
    if prefix < 0x80:
        return data[pos:pos + 1], pos + 1
    if prefix <= 0xB7:
        ln = prefix - 0x80
        if pos + 1 + ln > len(data):
            raise ValueError("rlp: bad string length")
        return data[pos + 1:pos + 1 + ln], pos + 1 + ln
    if prefix <= 0xBF:
        ln_len = prefix - 0xB7
        ln = _bytes_to_int(data[pos + 1:pos + 1 + ln_len])
        start = pos + 1 + ln_len
        return data[start:start + ln], start + ln
    if prefix <= 0xF7:
        ln = prefix - 0xC0
        end = pos + 1 + ln
        items, p = [], pos + 1
        while p < end:
            it, p = _rlp_decode(data, p)
            items.append(it)
        return items, end
    ln_len = prefix - 0xF7
    ln = _bytes_to_int(data[pos + 1:pos + 1 + ln_len])
    start = pos + 1 + ln_len
    end = start + ln
    items, p = [], start
    while p < end:
        it, p = _rlp_decode(data, p)
        items.append(it)
    return items, end


def rlp_decode(data: bytes):
    obj, pos = _rlp_decode(data, 0)
    if pos != len(data):
        raise ValueError("rlp: trailing bytes")
    return obj


def _to_int(b):
    return _bytes_to_int(b) if b else 0


def decode_signed_tx(raw_hex: str):
    """解码以太坊签名交易（EIP-155 legacy 或 pre-EIP-155）。
    返回 dict：chain_id, nonce, gas_price, gas_limit, to, value, data, v, r, s, from。
    raw_hex 可以是 0x 前缀 hex 或纯 hex。"""
    hexstr = raw_hex[2:] if raw_hex.startswith("0x") else raw_hex
    raw = bytes.fromhex(hexstr)
    fields = rlp_decode(raw)
    if not isinstance(fields, list) or len(fields) < 9:
        raise ValueError("invalid signed tx rlp")
    nonce = _to_int(fields[0])
    gas_price = _to_int(fields[1])
    gas_limit = _to_int(fields[2])
    to_raw = fields[3]
    to = ("0x" + to_raw.hex()) if to_raw else ""
    value = _to_int(fields[4])
    data = fields[5]
    v = _to_int(fields[6])
    r = _to_int(fields[7])
    s = _to_int(fields[8])

    chain_id = None
    if v in (27, 28):
        # pre-EIP-155
        recid = v - 27
        chain_id = 0
    elif v >= 35:
        chain_id = (v - 35) // 2
        recid = (v - 35) % 2
    else:
        raise ValueError(f"invalid v: {v}")

    # 签名数据：pre-EIP-155 用 [nonce, gas_price, gas_limit, to, value, data]
    # EIP-155 用 [nonce, gas_price, gas_limit, to, value, data, chain_id, 0, 0]
    if chain_id:
        sign_fields = [fields[0], fields[1], fields[2], fields[3], fields[4], fields[5],
                       chain_id, b"", b""]
    else:
        sign_fields = fields[:6]
    signing_hash = keccak256(rlp_encode(sign_fields))

    pub = _recover_pubkey(signing_hash, r, s, recid)
    from_addr = evm_address_from_pubkey(pub)
    return {
        "chain_id": chain_id,
        "nonce": nonce,
        "gas_price": gas_price,
        "gas_limit": gas_limit,
        "to": to,
        "value": value,
        "data": data,
        "v": v, "r": r, "s": s,
        "from": from_addr,
        "pubkey": pub,
        "hash": "0x" + keccak256(raw).hex(),
        "raw": raw_hex,
    }


# ===========================================================================
# ④ 轻量 EVM 解释器
# ===========================================================================
UINT256_MAX = (1 << 256) - 1
UINT255_MAX = (1 << 255) - 1


def _to_word(x):
    return x & UINT256_MAX


def _signed(x):
    return x - (1 << 256) if x & (1 << 255) else x


class EvmExecutionError(Exception):
    """EVM 执行失败（REVERT / 异常 / 沙盒超限）。"""


class EvmRevert(EvmExecutionError):
    def __init__(self, data=b""):
        super().__init__("revert")
        self.data = data


class EvmContext:
    """单次消息调用上下文（外部账户/合约统一）。"""

    def __init__(self, address="", caller="", origin="", value=0, data=b"", gas_limit=10 ** 7,
                 block_height=0, block_time=0, coinbase="0x0000000000000000000000000000000000000000",
                 chain_id=CHAIN_ID, nonce=0):
        self.address = address          # 本次调用目标（合约地址）
        self.caller = caller            # 调用者
        self.origin = origin            # 原始发起者
        self.value = value              # 随调用传入的 wei
        self.data = data                # calldata
        self.gas_limit = gas_limit
        self.gas_left = gas_limit
        self.gas_used = 0
        self.block_height = block_height
        self.block_time = block_time
        self.coinbase = coinbase
        self.chain_id = chain_id
        self.nonce = nonce
        self.return_data = b""
        self.success = True


class Evm:
    """EVM 执行器。store 注入：{evm_storage, evm_contracts, balances(nova), evm_nonce, ...}。
    gas 以 wei 计价（由外部换算 NOVA），操作码按标准 gas 近似计费。"""

    MAX_STEPS = 200_000          # 沙盒：最大指令步数
    MAX_MEMORY = 64 * 1024       # 沙盒：最大内存 64KB
    MAX_STORAGE_KEYS = 100_000   # 沙盒：单合约最大存储槽

    GAS_BASE = 2
    GAS_VERYLOW = 3
    GAS_LOW = 5
    GAS_MID = 8
    GAS_HIGH = 10
    GAS_SLOAD = 100
    GAS_SSTORE_SET = 20_000
    GAS_SSTORE_RESET = 5_000
    GAS_LOG_BASE = 375
    GAS_LOG_TOPIC = 375
    GAS_LOG_DATA = 8
    GAS_SHA3 = 30
    GAS_SHA3_WORD = 6
    GAS_CREATE = 32_000
    GAS_CALL = 700
    GAS_CALL_VALUE = 9_000
    GAS_EXTCODE = 700
    GAS_MEMORY = 3            # 每 word 内存扩展
    GAS_COPY = 3
    GAS_EXP = 10
    GAS_EXP_BYTE = 10
    GAS_SELFDESTRUCT = 5_000
    GAS_JUMPDEST = 1
    GAS_BALANCE = 700
    GAS_RETURN = 0

    def __init__(self, store, economy):
        self.store = store
        self.economy = economy

    # ---------------- 外部状态接口（与 Nova 账本桥接） ----------------
    def _balance_wei(self, addr):
        return int(float(self.store.balances.get(addr, 0.0)) * WEI_SCALE)

    def _set_balance_wei(self, addr, wei_val):
        nova = round(wei_val / WEI_SCALE, 8)
        if nova <= 0:
            self.store.balances.pop(addr, None)
        else:
            self.store.balances[addr] = nova

    def _code(self, addr):
        c = self.store.evm_contracts.get(addr)
        if not c:
            return b""
        return bytes.fromhex(c["bytecode"][2:]) if isinstance(c.get("bytecode"), str) else bytes(c.get("bytecode", b""))

    # ---------------- 内存辅助 ----------------
    @staticmethod
    def _mem_expand(mem, offset, size):
        """返回扩展后的 bytearray；超限抛错。"""
        if size == 0:
            return mem
        need = offset + size
        if need > Evm.MAX_MEMORY:
            raise EvmExecutionError("memory limit exceeded")
        if len(mem) < need:
            mem.extend(b"\x00" * (need - len(mem)))
        return mem

    @staticmethod
    def _mem_cost(mem, offset, size):
        if size == 0:
            return 0
        need = offset + size
        words = (need + 31) // 32
        return Evm.GAS_MEMORY * words + Evm.GAS_MEMORY * words * words // 512

    # ---------------- 栈/位宽辅助 ----------------
    @staticmethod
    def _u(x):
        return x & UINT256_MAX

    @staticmethod
    def _byte_at(data, pos):
        return data[pos] if 0 <= pos < len(data) else 0

    def _push(self, stack, val):
        stack.append(self._u(val))

    def _pop(self, stack):
        if not stack:
            raise EvmExecutionError("stack underflow")
        return stack.pop()

    def _pop2(self, stack):
        b = self._pop(stack)
        a = self._pop(stack)
        return a, b

    def _dup(self, stack, n):
        if len(stack) < n:
            raise EvmExecutionError("stack underflow")
        stack.append(stack[-n])

    def _swap(self, stack, n):
        if len(stack) < n + 1:
            raise EvmExecutionError("stack underflow")
        stack[-1], stack[-1 - n] = stack[-1 - n], stack[-1]

    # ---------------- 主执行 ----------------
    def run(self, address, caller="", origin="", value=0, data=b"", gas_limit=10 ** 7,
            block_height=0, block_time=0, coinbase="", storage=None):
        """在 address 合约上执行。storage 为 {slot(int)->int}（缺省读 self.store.evm_storage）。
        返回 {success, gas_used, return_data, storage_delta, events, steps}。"""
        code = self._code(address)
        if not code:
            return {"success": True, "gas_used": gas_limit, "return_data": b"",
                    "storage_delta": {}, "events": [], "steps": 0}
        ctx = EvmContext(address=address, caller=caller or address, origin=origin or (caller or address),
                         value=value, data=data, gas_limit=gas_limit,
                         block_height=block_height, block_time=block_time,
                         coinbase=coinbase, chain_id=self.store.gov_params.get("evm.chain_id", CHAIN_ID)
                         if isinstance(self.store.gov_params.get("evm.chain_id"), int) else CHAIN_ID)
        base_storage = dict(storage) if storage is not None else dict(self.store.evm_storage.get(address, {}))
        storage_delta = {}
        stack = []
        mem = bytearray()
        pc = 0
        steps = 0
        events = []
        success = True
        return_data = b""
        max_gas = ctx.gas_left
        try:
            while pc < len(code) and steps < self.MAX_STEPS:
                steps += 1
                if ctx.gas_left <= 0:
                    raise EvmExecutionError("out of gas")
                op = code[pc]
                pc += 1

                # ---- 0x00 STOP / INVALID ----
                if op == 0x00:
                    break
                if op == 0xFE:
                    raise EvmExecutionError("invalid opcode")

                # ---- 0x01-0x0B 算术 ----
                elif op == 0x01:  # ADD
                    a, b = self._pop2(stack); self._push(stack, a + b); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x02:  # MUL
                    a, b = self._pop2(stack); self._push(stack, a * b); ctx.gas_left -= self.GAS_LOW
                elif op == 0x03:  # SUB
                    a, b = self._pop2(stack); self._push(stack, a - b); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x04:  # DIV
                    a, b = self._pop2(stack); self._push(stack, 0 if b == 0 else a // b); ctx.gas_left -= self.GAS_LOW
                elif op == 0x05:  # SDIV
                    a, b = self._pop2(stack)
                    if b == 0:
                        self._push(stack, 0)
                    else:
                        sa, sb = _signed(a), _signed(b)
                        q = abs(sa) // abs(sb)
                        self._push(stack, q if (sa < 0) == (sb < 0) else -q)
                    ctx.gas_left -= self.GAS_LOW
                elif op == 0x06:  # MOD
                    a, b = self._pop2(stack); self._push(stack, 0 if b == 0 else a % b); ctx.gas_left -= self.GAS_LOW
                elif op == 0x07:  # SMOD
                    a, b = self._pop2(stack)
                    if b == 0:
                        self._push(stack, 0)
                    else:
                        r = abs(_signed(a)) % abs(_signed(b))
                        self._push(stack, r if _signed(a) >= 0 else -r)
                    ctx.gas_left -= self.GAS_LOW
                elif op == 0x08:  # ADDMOD：栈顶 m（先弹），其次 b，再次 a
                    m, b, a = self._pop(stack), self._pop(stack), self._pop(stack)
                    self._push(stack, 0 if m == 0 else (a + b) % m); ctx.gas_left -= self.GAS_MID
                elif op == 0x09:  # MULMOD
                    m, b, a = self._pop(stack), self._pop(stack), self._pop(stack)
                    self._push(stack, 0 if m == 0 else (a * b) % m); ctx.gas_left -= self.GAS_MID
                elif op == 0x0A:  # EXP
                    a, b = self._pop2(stack)
                    ctx.gas_left -= self.GAS_EXP + self.GAS_EXP_BYTE * ((b.bit_length() + 7) // 8)
                    self._push(stack, pow(a, b, 1 << 256))
                elif op == 0x0B:  # SIGNEXTEND：x=次顶，k=栈顶
                    x, k = self._pop2(stack)
                    if k < 32:
                        bit = 8 * k + 7
                        self._push(stack, (x ^ (1 << bit)) - (1 << bit) if (x >> bit) & 1 else x)
                    else:
                        self._push(stack, x)
                    ctx.gas_left -= self.GAS_LOW

                # ---- 0x10-0x1D 比较/位运算 ----
                elif op == 0x10:  # LT
                    a, b = self._pop2(stack); self._push(stack, 1 if a < b else 0); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x11:  # GT
                    a, b = self._pop2(stack); self._push(stack, 1 if a > b else 0); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x12:  # SLT
                    a, b = self._pop2(stack); self._push(stack, 1 if _signed(a) < _signed(b) else 0); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x13:  # SGT
                    a, b = self._pop2(stack); self._push(stack, 1 if _signed(a) > _signed(b) else 0); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x14:  # EQ
                    a, b = self._pop2(stack); self._push(stack, 1 if a == b else 0); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x15:  # ISZERO
                    a = self._pop(stack); self._push(stack, 1 if a == 0 else 0); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x16:  # AND
                    a, b = self._pop2(stack); self._push(stack, a & b); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x17:  # OR
                    a, b = self._pop2(stack); self._push(stack, a | b); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x18:  # XOR
                    a, b = self._pop2(stack); self._push(stack, a ^ b); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x19:  # NOT
                    a = self._pop(stack); self._push(stack, ~a); ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x1A:  # BYTE：x=次顶，i=栈顶
                    x, i = self._pop2(stack)
                    self._push(stack, (x >> (248 - 8 * i)) & 0xFF if i < 32 else 0)
                    ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x1B:  # SHL：x=次顶，shift=栈顶
                    x, shift = self._pop2(stack)
                    self._push(stack, (x << shift) & UINT256_MAX if shift < 256 else 0)
                    ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x1C:  # SHR：x=次顶，shift=栈顶
                    x, shift = self._pop2(stack)
                    self._push(stack, x >> shift if shift < 256 else 0)
                    ctx.gas_left -= self.GAS_VERYLOW
                elif op == 0x1D:  # SAR：x=次顶，shift=栈顶
                    x, shift = self._pop2(stack)
                    sx = _signed(x)
                    if shift >= 256:
                        self._push(stack, 0 if sx >= 0 else UINT256_MAX)
                    else:
                        self._push(stack, self._u(sx >> shift))
                    ctx.gas_left -= self.GAS_VERYLOW

                # ---- 0x20 SHA3：栈 [size, offset]，offset 栈顶 ----
                elif op == 0x20:
                    size, off = self._pop2(stack)
                    mem = self._mem_expand(mem, off, size)
                    ctx.gas_left -= self.GAS_SHA3 + self.GAS_SHA3_WORD * ((size + 31) // 32) + self._mem_cost(mem, off, size)
                    h = _bytes_to_int(keccak256(bytes(mem[off:off + size])))
                    self._push(stack, h)
                    if getattr(self, "_trace_sha3", False):
                        print(f"[SHA3] mem[{off}..{off + size}] = {bytes(mem[off:off + size]).hex()[:40]}.. -> {hex(h)[:16]}")

                # ---- 0x30-0x48 环境信息 ----
                elif op == 0x30:  # ADDRESS
                    self._push(stack, _bytes_to_int(bytes.fromhex(ctx.address[2:])))
                elif op == 0x31:  # BALANCE
                    a = self._pop(stack)
                    self._push(stack, self._balance_wei("0x" + a.to_bytes(20, "big").hex()))
                    ctx.gas_left -= self.GAS_BALANCE
                elif op == 0x32:  # ORIGIN
                    self._push(stack, _bytes_to_int(bytes.fromhex(ctx.origin[2:])))
                elif op == 0x33:  # CALLER
                    self._push(stack, _bytes_to_int(bytes.fromhex(ctx.caller[2:])))
                elif op == 0x34:  # CALLVALUE
                    self._push(stack, ctx.value)
                elif op == 0x35:  # CALLDATALOAD
                    i = self._pop(stack)
                    chunk = ctx.data[i:i + 32]
                    self._push(stack, _bytes_to_int(chunk.ljust(32, b"\x00")))
                elif op == 0x36:  # CALLDATASIZE
                    self._push(stack, len(ctx.data))
                elif op == 0x37:  # CALLDATACOPY
                    moff, doff, size = self._pop(stack), self._pop(stack), self._pop(stack)
                    mem = self._mem_expand(mem, moff, size)
                    ctx.gas_left -= self.GAS_COPY * ((size + 31) // 32) + self._mem_cost(mem, moff, size)
                    mem[moff:moff + size] = ctx.data[doff:doff + size].ljust(size, b"\x00")
                elif op == 0x38:  # CODESIZE
                    self._push(stack, len(code))
                elif op == 0x39:  # CODECOPY
                    moff, coff, size = self._pop(stack), self._pop(stack), self._pop(stack)
                    mem = self._mem_expand(mem, moff, size)
                    ctx.gas_left -= self.GAS_COPY * ((size + 31) // 32) + self._mem_cost(mem, moff, size)
                    mem[moff:moff + size] = code[coff:coff + size].ljust(size, b"\x00")
                elif op == 0x3A:  # GASPRICE
                    self._push(stack, GAS_WEI)
                elif op == 0x3B:  # EXTCODESIZE
                    a = self._pop(stack)
                    self._push(stack, len(self._code("0x" + a.to_bytes(20, "big").hex())))
                    ctx.gas_left -= self.GAS_EXTCODE
                elif op == 0x3C:  # EXTCODECOPY
                    a, moff, coff, size = self._pop(stack), self._pop(stack), self._pop(stack), self._pop(stack)
                    ext = self._code("0x" + a.to_bytes(20, "big").hex())
                    mem = self._mem_expand(mem, moff, size)
                    ctx.gas_left -= self.GAS_COPY * ((size + 31) // 32) + self.GAS_EXTCODE
                    mem[moff:moff + size] = ext[coff:coff + size].ljust(size, b"\x00")
                elif op == 0x3D:  # RETURNDATASIZE
                    self._push(stack, len(ctx.return_data))
                elif op == 0x3E:  # RETURNDATACOPY
                    moff, roff, size = self._pop(stack), self._pop(stack), self._pop(stack)
                    if roff + size > len(ctx.return_data):
                        raise EvmExecutionError("return data out of bounds")
                    mem = self._mem_expand(mem, moff, size)
                    ctx.gas_left -= self.GAS_COPY * ((size + 31) // 32)
                    mem[moff:moff + size] = ctx.return_data[roff:roff + size]
                elif op == 0x3F:  # EXTCODEHASH
                    a = self._pop(stack)
                    ext = self._code("0x" + a.to_bytes(20, "big").hex())
                    self._push(stack, _bytes_to_int(keccak256(ext)) if ext else 0)
                    ctx.gas_left -= self.GAS_EXTCODE

                elif op == 0x40:  # BLOCKHASH
                    self._pop(stack); self._push(stack, 0)
                elif op == 0x41:  # COINBASE
                    self._push(stack, _bytes_to_int(bytes.fromhex((ctx.coinbase or "0x" + "00" * 20)[2:].rjust(40, "0"))))
                elif op == 0x42:  # TIMESTAMP
                    self._push(stack, ctx.block_time)
                elif op == 0x43:  # NUMBER
                    self._push(stack, ctx.block_height)
                elif op == 0x44:  # PREVRANDAO/DIFFICULTY
                    self._push(stack, 0)
                elif op == 0x45:  # GASLIMIT
                    self._push(stack, 30_000_000)
                elif op == 0x46:  # CHAINID
                    self._push(stack, ctx.chain_id)
                elif op == 0x47:  # SELFBALANCE
                    self._push(stack, self._balance_wei(ctx.address))
                    ctx.gas_left -= self.GAS_BALANCE
                elif op == 0x48:  # BASEFEE
                    self._push(stack, GAS_WEI)

                # ---- 0x50-0x5B 栈/内存/存储/跳转 ----
                elif op == 0x50:  # POP
                    self._pop(stack); ctx.gas_left -= self.GAS_BASE
                elif op == 0x51:  # MLOAD
                    off = self._pop(stack)
                    mem = self._mem_expand(mem, off, 32)
                    ctx.gas_left -= self.GAS_VERYLOW + self._mem_cost(mem, off, 32)
                    self._push(stack, _bytes_to_int(bytes(mem[off:off + 32])))
                elif op == 0x52:  # MSTORE：栈 [value, offset]，offset 栈顶
                    val, off = self._pop2(stack)
                    mem = self._mem_expand(mem, off, 32)
                    ctx.gas_left -= self.GAS_VERYLOW + self._mem_cost(mem, off, 32)
                    mem[off:off + 32] = self._u(val).to_bytes(32, "big")
                elif op == 0x53:  # MSTORE8
                    val, off = self._pop2(stack)
                    mem = self._mem_expand(mem, off, 1)
                    ctx.gas_left -= self.GAS_VERYLOW + self._mem_cost(mem, off, 1)
                    mem[off] = val & 0xFF
                elif op == 0x54:  # SLOAD
                    slot = self._pop(stack)
                    base_storage.setdefault(slot, 0)
                    self._push(stack, base_storage[slot])
                    ctx.gas_left -= self.GAS_SLOAD
                elif op == 0x55:  # SSTORE：标准 EVM 先弹 value，再弹 key/slot
                    val = self._pop(stack)
                    slot = self._pop(stack)
                    if len(base_storage) >= self.MAX_STORAGE_KEYS and slot not in base_storage:
                        raise EvmExecutionError("storage limit exceeded")
                    old = base_storage.get(slot, 0)
                    base_storage[slot] = self._u(val)
                    storage_delta[slot] = base_storage[slot]
                    ctx.gas_left -= self.GAS_SSTORE_SET if old == 0 else self.GAS_SSTORE_RESET
                elif op == 0x56:  # JUMP
                    dest = self._pop(stack)
                    if dest >= len(code) or code[dest] != 0x5B:
                        raise EvmExecutionError("bad jump destination")
                    pc = dest
                    ctx.gas_left -= self.GAS_MID
                elif op == 0x57:  # JUMPI：栈 [cond, dest]，dest 栈顶
                    cond, dest = self._pop2(stack)
                    if cond:
                        if dest >= len(code) or code[dest] != 0x5B:
                            raise EvmExecutionError("bad jump destination")
                        pc = dest
                        if getattr(self, "_trace_jump", False):
                            print(f"[JUMPI] -> {dest} stack={[hex(x)[:8] for x in stack]}")
                    ctx.gas_left -= self.GAS_HIGH
                elif op == 0x58:  # PC
                    self._push(stack, pc - 1); ctx.gas_left -= self.GAS_BASE
                elif op == 0x59:  # MSIZE
                    self._push(stack, len(mem)); ctx.gas_left -= self.GAS_BASE
                elif op == 0x5A:  # GAS
                    self._push(stack, ctx.gas_left); ctx.gas_left -= self.GAS_BASE
                elif op == 0x5B:  # JUMPDEST
                    ctx.gas_left -= self.GAS_JUMPDEST

                # ---- 0x5F PUSH0 / 0x60-0x7F PUSH1-32 ----
                elif 0x60 <= op <= 0x7F:
                    n = op - 0x5F
                    if pc + n > len(code):
                        raise EvmExecutionError("push out of bounds")
                    self._push(stack, _bytes_to_int(code[pc:pc + n]))
                    pc += n
                    ctx.gas_left -= self.GAS_VERYLOW

                # ---- 0x80-0x8F DUP1-16 ----
                elif 0x80 <= op <= 0x8F:
                    self._dup(stack, op - 0x7F)
                    ctx.gas_left -= self.GAS_VERYLOW
                # ---- 0x90-0x9F SWAP1-16 ----
                elif 0x90 <= op <= 0x9F:
                    self._swap(stack, op - 0x8F)
                    ctx.gas_left -= self.GAS_VERYLOW

                # ---- 0xA0-0xA4 LOG0-4：栈（顶->底）topics... size offset ----
                elif 0xA0 <= op <= 0xA4:
                    nt = op - 0xA0
                    topics = [self._pop(stack) for _ in range(nt)]
                    size = self._pop(stack)
                    off = self._pop(stack)
                    mem = self._mem_expand(mem, off, size)
                    ctx.gas_left -= (self.GAS_LOG_BASE + self.GAS_LOG_TOPIC * nt
                                     + self.GAS_LOG_DATA * size + self._mem_cost(mem, off, size))
                    events.append({
                        "address": ctx.address,
                        "topics": ["0x" + t.to_bytes(32, "big").hex() for t in topics],
                        "data": "0x" + bytes(mem[off:off + size]).hex(),
                    })

                # ---- 0xF0 CREATE ----
                elif op == 0xF0:
                    value, off, size = self._pop(stack), self._pop(stack), self._pop(stack)
                    mem = self._mem_expand(mem, off, size)
                    ctx.gas_left -= self.GAS_CREATE + self._mem_cost(mem, off, size)
                    init_code = bytes(mem[off:off + size])
                    new_addr = create_address(ctx.address, ctx.nonce)
                    self._deploy_evm_contract(new_addr, init_code, ctx.caller, value)
                    ctx.nonce += 1
                    self._push(stack, _bytes_to_int(bytes.fromhex(new_addr[2:])))
                elif op == 0xF5:  # CREATE2
                    value, off, size, salt = self._pop(stack), self._pop(stack), self._pop(stack), self._pop(stack)
                    mem = self._mem_expand(mem, off, size)
                    ctx.gas_left -= self.GAS_CREATE + self._mem_cost(mem, off, size)
                    init_code = bytes(mem[off:off + size])
                    new_addr = create2_address(ctx.address, salt, init_code)
                    self._deploy_evm_contract(new_addr, init_code, ctx.caller, value)
                    self._push(stack, _bytes_to_int(bytes.fromhex(new_addr[2:])))

                # ---- 0xF1 CALL / 0xF2 CALLCODE / 0xF4 DELEGATECALL / 0xFA STATICCALL ----
                elif op in (0xF1, 0xF2, 0xF4, 0xFA):
                    if op == 0xF1:
                        gas, to, value, aoff, asize, roff, rsize = self._pop7(stack)
                        static = False
                    elif op == 0xF2:
                        gas, to, value, aoff, asize, roff, rsize = self._pop7(stack)
                        static = False
                    elif op == 0xF4:
                        gas, to, aoff, asize, roff, rsize = self._pop6(stack)
                        value = ctx.value
                        static = False
                    else:  # STATICCALL
                        gas, to, aoff, asize, roff, rsize = self._pop6(stack)
                        value = 0
                        static = True
                    mem = self._mem_expand(mem, aoff, asize)
                    mem = self._mem_expand(mem, roff, rsize)
                    ctx.gas_left -= self.GAS_CALL + (self.GAS_CALL_VALUE if value else 0)
                    call_data = bytes(mem[aoff:aoff + asize])
                    to_addr = "0x" + to.to_bytes(20, "big").hex()
                    # 值转移（value wei -> NOVA）
                    if value and not static:
                        self._set_balance_wei(ctx.address, self._balance_wei(ctx.address) - value)
                        self._set_balance_wei(to_addr, self._balance_wei(to_addr) + value)
                    ret = self._call_sub(to_addr, ctx.caller if op == 0xF1 else ctx.address,
                                         ctx.origin, value, call_data, min(gas, ctx.gas_left),
                                         ctx.block_height, ctx.block_time, ctx.coinbase, static=static)
                    ctx.gas_left -= ret.get("gas_used", 0)
                    ctx.return_data = ret.get("return_data", b"")
                    ok = 1 if ret.get("success") else 0
                    mem[roff:roff + rsize] = ctx.return_data[:rsize].ljust(rsize, b"\x00")
                    self._push(stack, ok)

                # ---- 0xF3 RETURN / 0xFD REVERT：栈 [size, offset]，offset 栈顶 ----
                elif op == 0xF3:
                    size, off = self._pop2(stack)
                    mem = self._mem_expand(mem, off, size)
                    ctx.gas_left -= self.GAS_RETURN
                    return_data = bytes(mem[off:off + size])
                    success = True
                    break
                elif op == 0xFD:
                    size, off = self._pop2(stack)
                    mem = self._mem_expand(mem, off, size)
                    raise EvmRevert(bytes(mem[off:off + size]))

                # ---- 0xFF SELFDESTRUCT ----
                elif op == 0xFF:
                    beneficiary = self._pop(stack)
                    baddr = "0x" + beneficiary.to_bytes(20, "big").hex()
                    bal = self._balance_wei(ctx.address)
                    self._set_balance_wei(baddr, self._balance_wei(baddr) + bal)
                    self._set_balance_wei(ctx.address, 0)
                    ctx.gas_left -= self.GAS_SELFDESTRUCT
                    break
                else:
                    raise EvmExecutionError(f"unsupported opcode 0x{op:02x}")

            if success:
                # 提交存储增量
                for slot, val in storage_delta.items():
                    self.store.evm_storage.setdefault(address, {})[slot] = val
        except EvmRevert as e:
            return_data = e.data
            success = False
        except EvmExecutionError as e:
            success = False
            self._last_error = str(e)
        except Exception as e:
            success = False
            self._last_error = f"{type(e).__name__}: {e}"
        return {
            "success": success,
            "gas_used": max_gas - max(ctx.gas_left, 0),
            "return_data": return_data,
            "storage_delta": storage_delta,
            "events": events,
            "steps": steps,
        }

    def _pop7(self, stack):
        g, t, v, ao, az, ro, rz = self._pop(stack), self._pop(stack), self._pop(stack), self._pop(stack), self._pop(stack), self._pop(stack), self._pop(stack)
        return g, t, v, ao, az, ro, rz

    def _pop6(self, stack):
        g, t, ao, az, ro, rz = self._pop(stack), self._pop(stack), self._pop(stack), self._pop(stack), self._pop(stack), self._pop(stack)
        return g, t, ao, az, ro, rz

    def _deploy_evm_contract(self, addr, init_code, creator, value):
        """CREATE 语义：init_code 执行后取 runtime code（RETURN 数据）。"""
        if not init_code:
            raise EvmExecutionError("empty init code")
        # 初始余额
        self._set_balance_wei(addr, self._balance_wei(addr) + value)
        # 保存 runtime code 前先执行 init（捕获 RETURN 数据）
        res = self.run_init(addr, init_code, creator=creator)
        if not res.get("success"):
            raise EvmExecutionError("create failed")
        runtime = res.get("return_data", b"")
        self.store.evm_contracts[addr] = {
            "bytecode": "0x" + runtime.hex(),
            "creator": creator,
            "ts": int(__import__("time").time()),
        }

    def run_init(self, addr, init_code, creator="", value=0, caller=""):
        """执行 init_code 的临时上下文（返回 RETURN 数据）。"""
        saved = self.store.evm_contracts.get(addr)
        self.store.evm_contracts[addr] = {"bytecode": "0x" + init_code.hex(), "creator": creator, "ts": 0}
        res = self.run(addr, caller=caller or creator, origin=caller or creator, value=value)
        if saved:
            self.store.evm_contracts[addr] = saved
        elif addr in self.store.evm_contracts:
            del self.store.evm_contracts[addr]
        return res

    def _call_sub(self, to_addr, caller, origin, value, data, gas, bh, bt, cb, static=False):
        """子调用：目标为 EVM 合约则递归执行，否则视为外部账户（成功空返回）。"""
        if to_addr in self.store.evm_contracts:
            return self.run(to_addr, caller=caller, origin=origin, value=value, data=data,
                            gas_limit=max(gas, 1), block_height=bh, block_time=bt, coinbase=cb)
        return {"success": True, "gas_used": 0, "return_data": b"", "storage_delta": {}, "events": [], "steps": 0}


def create2_address(sender_hex: str, salt: int, init_code: bytes) -> str:
    """CREATE2 语义：keccak(0xff ++ sender ++ salt ++ keccak(init_code))[-20:]。"""
    s = bytes.fromhex(sender_hex[2:])
    h = keccak256(b"\xff" + s + salt.to_bytes(32, "big") + keccak256(init_code))
    return "0x" + h[-20:].hex()


def wei_to_nova(wei: int) -> float:
    return round(wei / WEI_SCALE, 8)


def nova_to_wei(nova: float) -> int:
    return int(round(float(nova) * WEI_SCALE))


def address_to_bytes(addr: str) -> bytes:
    """0x+40 hex 或 0x+64 hex 转 bytes（截断/填充到地址语义）。"""
    h = addr[2:] if addr.startswith("0x") else addr
    return bytes.fromhex(h)


def checksum_address(addr: str) -> str:
    """EIP-55 校验和地址（MetaMask 展示用）。"""
    h = addr[2:].lower()
    kh = keccak256(h.encode()).hex()
    out = "".join(c.upper() if int(kh[i], 16) >= 8 else c for i, c in enumerate(h))
    return "0x" + out
