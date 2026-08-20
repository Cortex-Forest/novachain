# -*- coding: utf-8 -*-
"""Solidity 示例合约集 + 开发工具链模板（v0.11，G5）：
① 微型 EVM 汇编器（助记符 -> 真实字节码），生成可执行示例合约：
   - SimpleStorage（基础存储回归）
   - ERC20Nova（ERC-20 标准：totalSupply/balanceOf/transfer/approve/allowance/transferFrom/mint）
② Solidity 源码示例（音乐 NFT / 盲盒 / 订阅）——可由 Remix/Hardhat/Foundry 编译后部署
③ Hardhat / Foundry / 网络配置模板 + 水龙头说明

所有字节码可由本机 EVM 解释器真实执行（无需外部编译器）。
"""
# ---------------------------------------------------------------------------
# ① 微型 EVM 汇编器
# ---------------------------------------------------------------------------
_OP = {
    "STOP": 0x00, "ADD": 0x01, "MUL": 0x02, "SUB": 0x03, "DIV": 0x04, "MOD": 0x06,
    "EXP": 0x0A, "SIGNEXTEND": 0x0B, "LT": 0x10, "GT": 0x11, "EQ": 0x14, "ISZERO": 0x15,
    "AND": 0x16, "OR": 0x17, "XOR": 0x18, "NOT": 0x19, "BYTE": 0x1A, "SHL": 0x1B,
    "SHR": 0x1C, "SAR": 0x1D, "SHA3": 0x20, "ADDRESS": 0x30, "BALANCE": 0x31,
    "ORIGIN": 0x32, "CALLER": 0x33, "CALLVALUE": 0x34, "CALLDATALOAD": 0x35,
    "CALLDATASIZE": 0x36, "CODESIZE": 0x38, "GASPRICE": 0x3A, "EXTCODESIZE": 0x3B,
    "TIMESTAMP": 0x42, "NUMBER": 0x43, "CHAINID": 0x46, "SELFBALANCE": 0x47,
    "POP": 0x50, "MLOAD": 0x51, "MSTORE": 0x52, "MSTORE8": 0x53, "SLOAD": 0x54,
    "SSTORE": 0x55, "JUMP": 0x56, "JUMPI": 0x57, "PC": 0x58, "MSIZE": 0x59, "GAS": 0x5A,
    "JUMPDEST": 0x5B, "LOG0": 0xA0, "LOG1": 0xA1, "LOG2": 0xA2, "LOG3": 0xA3, "LOG4": 0xA4,
    "CREATE": 0xF0, "CALL": 0xF1, "RETURN": 0xF3, "DELEGATECALL": 0xF4,
    "REVERT": 0xFD, "INVALID": 0xFE, "SELFDESTRUCT": 0xFF,
}


def _push_bytes(n, val):
    val = int(val, 16) if isinstance(val, str) else val
    return bytes([0x5F + n]) + (val & ((1 << (8 * n)) - 1)).to_bytes(n, "big")


def compile_asm(src: str) -> str:
    """助记符汇编 -> 0x 前缀字节码 hex（支持 label 与 @label 跳转）。"""
    return _compile_runtime(src)


# ---------------------------------------------------------------------------
# SimpleStorage：set(uint256) / get()
# ---------------------------------------------------------------------------
def simple_storage_bytecode() -> str:
    """部署 init code -> runtime。
    函数：0x60fe47b1 set(uint256)、0x6d4ce63c get()。"""
    src = """
    ; ---- dispatcher ----
    dispatch:
      PUSH1 0x04
      CALLDATASIZE
      GT
      PUSH1 @revert
      JUMPI
      PUSH1 0x00
      CALLDATALOAD
      PUSH1 0xE0
      SHR
      DUP1
      PUSH4 0x60fe47b1
      EQ
      PUSH1 @set_handler
      JUMPI
      DUP1
      PUSH4 0x6d4ce63c
      EQ
      PUSH1 @get_handler
      JUMPI
    revert:
      JUMPDEST
      PUSH1 0x00
      PUSH1 0x00
      REVERT
    set_handler:
      JUMPDEST
      POP
      ; SSTORE(0, calldata[4:36])
      PUSH1 0x00
      PUSH1 0x04
      CALLDATALOAD
      SSTORE
      STOP
    get_handler:
      JUMPDEST
      POP
      PUSH1 0x00
      SLOAD
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      PUSH1 0x00
      RETURN
    """
    return _init_wrap(bytes.fromhex(_compile_runtime(src)[2:]))


def _compile_runtime(src) -> str:
    """compile_asm 支持 @label 跳转的完整实现。"""
    lines = [ln.strip() for ln in src.splitlines()
             if ln.strip() and not ln.strip().startswith(";")]
    instrs = []
    labels = {}
    for ln in lines:
        if ln.endswith(":"):
            labels[ln[:-1]] = len(instrs)
            continue
        parts = ln.split()
        mnem = parts[0].upper()
        if mnem.startswith("PUSH") and mnem[4:].isdigit():
            instrs.append(("PUSH", int(mnem[4:]), parts[1] if len(parts) > 1 else None))
        elif mnem.startswith("DUP") and mnem[3:].isdigit():
            instrs.append(("DUP", int(mnem[3:]), None))
        elif mnem.startswith("SWAP") and mnem[4:].isdigit():
            instrs.append(("SWAP", int(mnem[4:]), None))
        else:
            instrs.append(("OP", mnem, parts[1] if len(parts) > 1 else None))
    def _push_width(m, a):
        """label 跳转目标偏移可能 >255，自动升宽到 PUSH2（保证可表示）。"""
        if a and a.startswith("@"):
            return max(m, 2)
        return m
    # 计算每条指令长度
    def instr_len(kind, mnem, arg):
        if kind == "PUSH":
            return 1 + _push_width(mnem, arg)
        return 1
    # 标签解析为字节偏移
    off = 0
    label_off = {}
    for idx, (kind, m, a) in enumerate(instrs):
        label_off[idx] = off
        off += instr_len(kind, m, a)
    byte_off = {}
    for name, idx in labels.items():
        byte_off[name] = label_off[idx]
    out = bytearray()
    for kind, m, a in instrs:
        if kind == "PUSH":
            n = _push_width(m, a)
            if a and a.startswith("@"):
                val = byte_off[a[1:]]
            else:
                val = int(a, 16) if a else 0
            out += _push_bytes(n, val)
        elif kind == "DUP":
            out.append(0x7F + m)
        elif kind == "SWAP":
            out.append(0x8F + m)
        else:
            out.append(_OP[m])
    return "0x" + out.hex()


def _runtime_from_src(src) -> str:
    return _compile_runtime(src)


def _init_wrap(runtime: bytes, ctor: bytes = b"") -> str:
    """把 runtime 包装为部署 init code（可前置 constructor 字节）：
    [ctor] PUSH size PUSH offset PUSH 0 CODECOPY PUSH size PUSH 0 RETURN。"""
    n = len(runtime)
    ctor = ctor or b""
    size_w = 2 if n > 0xFF else 1
    # offset 宽度与 header 长度互相依赖，迭代收敛（offset <= 255 用 PUSH1，否则 PUSH2）
    off_w = 2
    for _ in range(2):
        base = (1 + size_w) + (1 + off_w) + 2 + 1 + (1 + size_w) + 2 + 1
        off = len(ctor) + base
        want = 1 if off <= 0xFF else 2
        if want == off_w:
            break
        off_w = want
    else:
        off_w = 2
        base = (1 + size_w) + (1 + off_w) + 2 + 1 + (1 + size_w) + 2 + 1
        off = len(ctor) + base
    hdr = (ctor
           + _push_bytes(size_w, n) + _push_bytes(off_w, off)
           + _push_bytes(1, 0) + bytes([0x39])
           + _push_bytes(size_w, n) + _push_bytes(1, 0) + bytes([0xF3]))
    return "0x" + (hdr + runtime).hex()


def _wrap_returns(code: str) -> str:
    return _init_wrap(bytes.fromhex(code[2:]))


# ---------------------------------------------------------------------------
# ERC20Nova：真实执行的 ERC-20（storage mapping 语义）
# ---------------------------------------------------------------------------
def _str32(s: str) -> int:
    """字符串左对齐到 32 字节的 int 值（PUSH32 用）。"""
    b = s.encode()
    return int.from_bytes(b, "big") << (8 * (32 - len(b)))


_NAME_HEX = "0x" + _str32("NovaToken").to_bytes(32, "big").hex()
_SYM_HEX = "0x" + _str32("NVT").to_bytes(32, "big").hex()


def erc20_bytecode() -> str:
    """极简但真实执行的 ERC-20 runtime。
    storage: slot0 totalSupply / slot1 owner / balances: keccak(addr ++ 2) /
    allowances: keccak(owner ++ keccak(spender ++ 3))。"""
    src = f"""
    ; ---------------- dispatcher ----------------
    dispatch:
      PUSH1 0x04
      CALLDATASIZE
      GT
      PUSH1 @revert
      JUMPI
      PUSH1 0x00
      CALLDATALOAD
      PUSH1 0xE0
      SHR
      DUP1
      PUSH4 0x18160ddd
      EQ
      PUSH1 @totalSupply
      JUMPI
      DUP1
      PUSH4 0x70a08231
      EQ
      PUSH1 @balanceOf
      JUMPI
      DUP1
      PUSH4 0xa9059cbb
      EQ
      PUSH1 @transfer
      JUMPI
      DUP1
      PUSH4 0x095ea7b3
      EQ
      PUSH1 @approve
      JUMPI
      DUP1
      PUSH4 0xdd62ed3e
      EQ
      PUSH1 @allowance
      JUMPI
      DUP1
      PUSH4 0x23b872dd
      EQ
      PUSH1 @transferFrom
      JUMPI
      DUP1
      PUSH4 0x40c10f19
      EQ
      PUSH1 @mint
      JUMPI
      DUP1
      PUSH4 0x06fdde03
      EQ
      PUSH1 @name
      JUMPI
      DUP1
      PUSH4 0x95d89b41
      EQ
      PUSH1 @symbol
      JUMPI
      DUP1
      PUSH4 0x313ce567
      EQ
      PUSH1 @decimals
      JUMPI
    revert:
      JUMPDEST
      PUSH1 0x00
      PUSH1 0x00
      REVERT

    ; ---------------- totalSupply() ----------------
    totalSupply:
      JUMPDEST
      POP
      PUSH1 0x00
      SLOAD
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      PUSH1 0x00
      RETURN

    ; ---------------- balanceOf(address) ----------------
    balanceOf:
      JUMPDEST
      POP
      PUSH1 0x04
      CALLDATALOAD
      PUSH1 0x60
      SHR
      PUSH1 0x00
      MSTORE
      PUSH1 0x02
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      SLOAD
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      PUSH1 0x00
      RETURN

    ; ---------------- transfer(address,uint256) ----------------
    transfer:
      JUMPDEST
      POP
      ; S_sender = keccak(caller ++ 2)
      CALLER
      PUSH1 0x00
      MSTORE
      PUSH1 0x02
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      DUP1
      SLOAD
      PUSH1 0x24
      CALLDATALOAD
      SWAP1
      DUP2
      GT
      ISZERO
      PUSH1 @transfer_fail
      JUMPI
      ; 扣 sender：SSTORE(S, bal - value)，栈 [S, value] 需 DUP2 复制 S
      DUP2
      SLOAD
      SWAP1
      SUB
      SSTORE
      ; 加 to：SSTORE(S_to, bal_to + value)
      PUSH1 0x04
      CALLDATALOAD
      PUSH1 0x60
      SHR
      PUSH1 0x00
      MSTORE
      PUSH1 0x02
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      DUP1
      SLOAD
      PUSH1 0x24
      CALLDATALOAD
      ADD
      SSTORE
      ; return true
      PUSH1 0x01
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      PUSH1 0x00
      RETURN
    transfer_fail:
      JUMPDEST
      PUSH1 0x00
      PUSH1 0x00
      REVERT

    ; ---------------- approve(address,uint256) ----------------
    approve:
      JUMPDEST
      POP
      ; A = keccak(spender ++ keccak(caller ++ 3))
      PUSH1 0x04
      CALLDATALOAD
      PUSH1 0x60
      SHR
      PUSH1 0x00
      MSTORE
      CALLER
      PUSH1 0x20
      MSTORE
      PUSH1 0x03
      PUSH1 0x40
      MSTORE
      PUSH1 0x40
      PUSH1 0x20
      SHA3
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      PUSH1 0x24
      CALLDATALOAD
      SSTORE
      PUSH1 0x01
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      PUSH1 0x00
      RETURN

    ; ---------------- allowance(owner,spender) ----------------
    allowance:
      JUMPDEST
      POP
      PUSH1 0x04
      CALLDATALOAD
      PUSH1 0x60
      SHR
      PUSH1 0x00
      MSTORE
      PUSH1 0x03
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      PUSH1 0x24
      CALLDATALOAD
      PUSH1 0x60
      SHR
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      SLOAD
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      PUSH1 0x00
      RETURN

    ; ---------------- transferFrom(from,to,value) ----------------
    transferFrom:
      JUMPDEST
      POP
      ; A = keccak(caller ++ keccak(from ++ 3))（spender=caller，owner=from）
      PUSH1 0x04
      CALLDATALOAD
      PUSH1 0x60
      SHR
      PUSH1 0x00
      MSTORE
      PUSH1 0x03
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      CALLER
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      DUP1
      SLOAD
      PUSH1 0x44
      CALLDATALOAD
      SWAP1
      DUP2
      GT
      ISZERO
      PUSH1 @fail2
      JUMPI
      ; 扣 allowance：栈 [A, value] 需 DUP2 复制 A
      DUP2
      SLOAD
      SWAP1
      SUB
      SSTORE
      ; 扣 from 余额
      PUSH1 0x04
      CALLDATALOAD
      PUSH1 0x60
      SHR
      PUSH1 0x00
      MSTORE
      PUSH1 0x02
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      DUP1
      SLOAD
      PUSH1 0x44
      CALLDATALOAD
      SWAP1
      DUP2
      GT
      ISZERO
      PUSH1 @fail2
      JUMPI
      DUP2
      SLOAD
      SWAP1
      SUB
      SSTORE
      ; 加 to 余额
      PUSH1 0x24
      CALLDATALOAD
      PUSH1 0x60
      SHR
      PUSH1 0x00
      MSTORE
      PUSH1 0x02
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      DUP1
      SLOAD
      PUSH1 0x44
      CALLDATALOAD
      ADD
      SSTORE
      PUSH1 0x01
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      PUSH1 0x00
      RETURN
    fail2:
      JUMPDEST
      PUSH1 0x00
      PUSH1 0x00
      REVERT

    ; ---------------- mint(address,uint256) ----------------
    mint:
      JUMPDEST
      POP
      PUSH1 0x01
      SLOAD
      CALLER
      EQ
      ISZERO
      PUSH1 @mint_fail
      JUMPI
      ; totalSupply += value
      PUSH1 0x00
      DUP1
      SLOAD
      PUSH1 0x24
      CALLDATALOAD
      ADD
      SSTORE
      ; to balance += value
      PUSH1 0x04
      CALLDATALOAD
      PUSH1 0x60
      SHR
      PUSH1 0x00
      MSTORE
      PUSH1 0x02
      PUSH1 0x20
      MSTORE
      PUSH1 0x40
      PUSH1 0x00
      SHA3
      DUP1
      SLOAD
      PUSH1 0x24
      CALLDATALOAD
      ADD
      SSTORE
      PUSH1 0x01
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      PUSH1 0x00
      RETURN
    mint_fail:
      JUMPDEST
      PUSH1 0x00
      PUSH1 0x00
      REVERT

    ; ---------------- name() ----------------
    name:
      JUMPDEST
      POP
      PUSH1 0x40
      PUSH1 0x00
      MSTORE
      PUSH1 0x09
      PUSH1 0x20
      MSTORE
      PUSH32 {_NAME_HEX}
      PUSH1 0x40
      MSTORE
      PUSH1 0x60
      PUSH1 0x00
      RETURN

    ; ---------------- symbol() ----------------
    symbol:
      JUMPDEST
      POP
      PUSH1 0x40
      PUSH1 0x00
      MSTORE
      PUSH1 0x03
      PUSH1 0x20
      MSTORE
      PUSH32 {_SYM_HEX}
      PUSH1 0x40
      MSTORE
      PUSH1 0x60
      PUSH1 0x00
      RETURN

    ; ---------------- decimals() ----------------
    decimals:
      JUMPDEST
      POP
      PUSH1 0x12
      PUSH1 0x00
      MSTORE
      PUSH1 0x20
      PUSH1 0x00
      RETURN
    """
    # constructor：slot1 = ORIGIN（部署者即 owner，mint 权限校验用）
    # SSTORE(slot, value)：栈 [slot, value]，value 栈顶 -> PUSH1 1 ORIGIN SSTORE
    ctor = bytes.fromhex("60013255")
    return _init_wrap(bytes.fromhex(_runtime_from_src(src)[2:]), ctor=ctor)


# ---------------------------------------------------------------------------
# ② Solidity 源码示例
# ---------------------------------------------------------------------------
MUSIC_NFT_SOL = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
// Nova 音乐 NFT：ERC-721 + 版税（OpenSea 可展示）
interface IERC721Receiver {
    function onERC721Received(address,address,uint256,bytes calldata) external returns (bytes4);
}
contract MusicNFT {
    string public name = "NovaMusic";
    string public symbol = "NOVAM";
    mapping(uint256 => address) private _owner;
    mapping(address => uint256) private _balances;
    uint256 private _nextId = 1;
    uint256 public constant ROYALTY_BPS = 500; // 5% 版税
    address public royaltyRecipient;
    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);
    event Royalty(address indexed payer, uint256 tokenId, uint256 amount);

    constructor(address recipient) { royaltyRecipient = recipient; }

    function mint(address to, string calldata /*uri*/) external returns (uint256) {
        uint256 id = _nextId++;
        _owner[id] = to; _balances[to]++;
        emit Transfer(address(0), to, id);
        return id;
    }
    function ownerOf(uint256 id) public view returns (address) { return _owner[id]; }
    function balanceOf(address a) public view returns (uint256) { return _balances[a]; }
    function totalSupply() public view returns (uint256) { return _nextId - 1; }
    function transferFrom(address from, address to, uint256 id) public {
        require(_owner[id] == from && (msg.sender == from || _approvedFor[id] == msg.sender), "not owner");
        _owner[id] = to; _balances[from]--; _balances[to]++;
        emit Transfer(from, to, id);
    }
    mapping(uint256 => address) private _approvedFor;
    function approve(address to, uint256 id) public { _approvedFor[id] = to; }
}
"""

BLIND_BOX_SOL = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
// Nova 盲盒：ERC-1155 + 可验证随机数（可调原生盲盒随机源）
contract BlindBox {
    mapping(uint256 => string) private _uri;
    mapping(uint256 => uint256) private _totalSupply;
    mapping(address => mapping(uint256 => uint256)) private _balance;
    uint256 public nextBoxId = 1;
    bytes32 public seedHash; // 开盒种子承诺（与原生盲盒 commit 一致）
    event TransferSingle(address indexed op, address indexed from, address indexed to, uint256 id, uint256 value);

    constructor(bytes32 hash) { seedHash = hash; }

    function createBox(uint256 supply, string calldata uri) external returns (uint256) {
        uint256 id = nextBoxId++;
        _totalSupply[id] = supply; _uri[id] = uri;
        return id;
    }
    function uri(uint256 id) external view returns (string memory) { return _uri[id]; }
    function balanceOf(address a, uint256 id) external view returns (uint256) { return _balance[a][id]; }
    function totalSupply(uint256 id) external view returns (uint256) { return _totalSupply[id]; }
    function mint(address to, uint256 id, uint256 amount) external {
        _balance[to][id] += amount;
        emit TransferSingle(msg.sender, address(0), to, id, amount);
    }
    function random(uint256 nonce) external view returns (uint256) {
        // 可验证随机（链上确定性）：keccak(seedHash, msg.sender, nonce)
        return uint256(keccak256(abi.encodePacked(seedHash, msg.sender, nonce)));
    }
}
"""

SUBSCRIPTION_SOL = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
// Nova 订阅：ERC-20 订阅门控 + 可调原生密文交易
contract Subscription {
    IERC20 public token;
    address public creator;
    uint256 public pricePerMonth;
    mapping(address => uint256) public expiresAt;
    event Subscribed(address indexed user, uint256 expiresAt);

    constructor(IERC20 _token, address _creator, uint256 _price) {
        token = _token; creator = _creator; pricePerMonth = _price;
    }
    function subscribe(uint256 months) external {
        uint256 cost = pricePerMonth * months;
        require(token.transferFrom(msg.sender, creator, cost), "pay failed");
        uint256 nowAt = block.timestamp;
        uint256 base = expiresAt[msg.sender] > nowAt ? expiresAt[msg.sender] : nowAt;
        expiresAt[msg.sender] = base + months * 30 days;
        emit Subscribed(msg.sender, expiresAt[msg.sender]);
    }
    function isSubscribed(address user) external view returns (bool) {
        return expiresAt[user] > block.timestamp;
    }
}
interface IERC20 {
    function transferFrom(address,address,uint256) external returns (bool);
}
"""

HARDHAT_CONFIG = """// hardhat.config.ts —— Nova 网络配置模板（Chain ID 666666）
import { HardhatUserConfig } from "hardhat/config";
import "@nomicfoundation/hardhat-toolbox";

const config: HardhatUserConfig = {
  solidity: "0.8.24",
  networks: {
    nova: {
      url: "https://你的节点域名/rpc",
      chainId: 666666,
      accounts: [process.env.NOVA_PRIVATE_KEY || ""],
    },
    novaLocal: {
      url: "http://127.0.0.1:8080/rpc",
      chainId: 666666,
      accounts: [process.env.NOVA_PRIVATE_KEY || ""],
    },
  },
};
export default config;
"""

FOUNDRY_TOML = """# foundry.toml —— Nova 网络配置模板
[profile.default]
src = "src"
out = "out"
libs = ["lib"]
solc_version = "0.8.24"
evm_version = "paris"

[rpc_endpoints]
nova = "https://你的节点域名/rpc"
novaLocal = "http://127.0.0.1:8080/rpc"

[etherscan]
nova = { key = "${NOVA_ETHERSCAN_KEY}", url = "https://explorer.yourdomain.com/api" }
"""

NETWORKS_JSON = """{
  "nova": {
    "chainId": 666666,
    "chainIdHex": "0xa23a2",
    "rpcUrl": "https://你的节点域名/rpc",
    "rpcUrlLocal": "http://127.0.0.1:8080/rpc",
    "symbol": "NOVA",
    "decimals": 18,
    "explorer": "https://explorer.yourdomain.com",
    "faucet": "https://你的节点域名/api/faucet/evm"
  }
}
"""

META_MASK_GUIDE = """# 在 MetaMask 中手动添加 Nova 网络
1. MetaMask → 设置 → 网络 → 添加网络
2. 网络名称: Nova
3. 新的 RPC URL: https://你的节点域名/rpc   （本地: http://127.0.0.1:8080/rpc）
4. 链 ID: 666666
5. 货币符号: NOVA
6. 区块浏览器 URL（可选）: https://explorer.yourdomain.com
7. 保存 → 切换到 Nova 网络
"""

EXAMPLES = {
    "MusicNFT.sol": MUSIC_NFT_SOL,
    "BlindBox.sol": BLIND_BOX_SOL,
    "Subscription.sol": SUBSCRIPTION_SOL,
    "hardhat.config.ts": HARDHAT_CONFIG,
    "foundry.toml": FOUNDRY_TOML,
    "networks.json": NETWORKS_JSON,
    "METAMASK_SETUP.md": META_MASK_GUIDE,
}
