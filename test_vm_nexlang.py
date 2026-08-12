"""Unit tests for core.transaction, core.vm and nexlang_compiler."""
import hashlib

from core.transaction import Tx, canonical_amount
from core.vm import NexusVM, deploy_address
from nexlang_compiler import NexLangCompiler


# ---------------------------------------------------------------------------
# canonical_amount / Tx
# ---------------------------------------------------------------------------

def test_canonical_amount_invalid_inputs():
    assert canonical_amount("abc") == ""
    assert canonical_amount(None) == ""
    assert canonical_amount({}) == ""


def test_canonical_amount_normalization():
    assert canonical_amount(0.1 + 0.2) == "0.3"  # 8 位小数格式化
    assert canonical_amount(1.00000000) == "1"
    assert canonical_amount(0.000001) == "0.000001"


def test_tx_roundtrip_preserves_fields():
    tx = Tx("alice", "bob", 1.5, ["p1", "p2"], "memo", "pk", "sig", timestamp=12345.0)
    d = tx.to_dict()
    r = Tx.from_dict(d)
    assert r.sender == "alice"
    assert r.receiver == "bob"
    assert r.amount == 1.5
    assert r.parents == ["p1", "p2"]
    assert r.data == "memo"
    assert r.sender_public_key == "pk"
    assert r.signature == "sig"
    assert r.timestamp == 12345.0
    assert r.txid == tx.txid


def test_tx_from_dict_defaults():
    r = Tx.from_dict({"sender": "a", "receiver": "b", "amount": 1})
    assert r.parents == []
    assert r.data == ""
    assert r.sender_public_key == ""
    assert r.signature == ""
    assert r.timestamp is not None
    assert r.txid is not None


def test_tx_explicit_txid_preserved():
    tx = Tx("a", "b", 1, txid="fixed-id")
    assert tx.txid == "fixed-id"


def test_tx_txid_deterministic():
    tx1 = Tx("a", "b", 1, [], "", timestamp=1000.0)
    tx2 = Tx("a", "b", 1, [], "", timestamp=1000.0)
    assert tx1.txid == tx2.txid
    tx3 = Tx("a", "b", 2, [], "", timestamp=1000.0)
    assert tx1.txid != tx3.txid


def test_tx_signing_data_is_canonical():
    tx = Tx("alice", "bob", 1.5, ["p"], "d", "pk", timestamp=1000.0)
    assert tx.signing_data() == "alicebob1.51000.0['p']dpk"


# ---------------------------------------------------------------------------
# core.vm
# ---------------------------------------------------------------------------

def test_vm_execute():
    assert NexusVM.execute("code", "msg") == "VM执行: msg"


def test_deploy_address_deterministic():
    a1 = deploy_address("bytecode")
    a2 = deploy_address("bytecode")
    assert a1 == a2
    assert a1.startswith("0x")
    assert len(a1) == 42  # 0x + 40 hex
    assert deploy_address("other") != a1
    expected = "0x" + hashlib.sha3_256(b"bytecode").hexdigest()[:40]
    assert a1 == expected


# ---------------------------------------------------------------------------
# nexlang_compiler
# ---------------------------------------------------------------------------

def test_compile_empty_and_comments():
    c = NexLangCompiler()
    assert c.compile("") == []
    assert c.compile("// 注释\n// 另一行") == []


def test_compile_top_level_let_send_return():
    c = NexLangCompiler()
    code = c.compile("""
        let x = 5;
        send(0xabc, 10);
        return x;
    """)
    # let x=5 → PUSH 5, STORE slot0
    # send(0xabc, 10); → PUSH 10, PUSH slot0(0xabc 非数字非槽位→无), SEND
    # return x; → PUSH slot0, RET
    assert code == [0x01, 5, 0x02, 0, 0x01, 10, 0x04, 0x01, 0, 0x05]
    assert c.storage_slots == {"x": 0}


def test_compile_function_body_and_labels():
    c = NexLangCompiler()
    code = c.compile("""
        on_transfer(to, amt) {
            send(to, 10);
        }
        query balance() {
            return 1;
        }
    """)
    assert code == [0x01, 10, 0x04, 0x01, 1, 0x05]
    assert c.labels == {"on_transfer": 0, "balance": 3}


def test_compile_query_without_body():
    c = NexLangCompiler()
    code = c.compile("query balance()")
    assert code == []
    assert c.labels["balance"] == 0


def test_compile_expression_operators():
    c = NexLangCompiler()
    code = c.compile("""
        let a = 2 * 3;
        let b = 10 - 4;
        let d = 12 / 3;
        return a;
    """)
    assert code[0:3] == [0x01, 2, 0x01]
    assert code[3:5] == [3, 0x03]            # 2*3 → 0x03
    assert code[5:9] == [0x02, 0, 0x01, 10]  # STORE slot0, PUSH 10 (b)
    assert code == [1, 2, 1, 3, 3, 2, 0, 1, 10, 1, 4, 3, 2, 1, 1, 12, 1, 3, 3, 2, 2, 1, 0, 5]


def test_compile_unknown_lines_skipped():
    c = NexLangCompiler()
    code = c.compile("""
        unknown_statement foo bar
        let x = 7;
        // 注释
    """)
    assert code == [0x01, 7, 0x02, 0]


def test_compile_storage_slot_reuse():
    c = NexLangCompiler()
    c.compile("let x = 1;\nlet y = 2;\nlet x = 3;")
    assert c.storage_slots == {"x": 0, "y": 1}
    assert c.next_slot == 2