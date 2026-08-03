from core.crypto import QuantumWallet, verify_quantum_tx
from core.transaction import Tx


def test_wallet_and_tx_roundtrip():
    wallet = QuantumWallet()
    tx = Tx("alice", "bob", 1, [], "hello", wallet.public_key_hex(), "")
    assert tx.txid
    restored = Tx.from_dict(tx.to_dict())
    assert restored.txid == tx.txid
    assert restored.timestamp == tx.timestamp

    sig = wallet.sign(tx.signing_data())
    assert verify_quantum_tx(tx.signing_data(), sig, wallet.public_key_hex(), wallet.address)


test_wallet_and_tx_roundtrip()
print("smoke-test: ok")
