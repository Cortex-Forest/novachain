import sys, json
from core.crypto import QuantumWallet
from core.transaction import Tx

if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("用法: python sign_tx.py <私钥hex> <发送方地址> <接收方地址> <金额> [备注]")
        sys.exit(1)

    priv_key_hex = sys.argv[1]
    sender = sys.argv[2]
    receiver = sys.argv[3]
    amount = float(sys.argv[4])
    memo = sys.argv[5] if len(sys.argv) > 5 else ""

    w = QuantumWallet(bytes.fromhex(priv_key_hex))
    tx = Tx(sender, receiver, amount, [], memo, w.public_key_hex())
    tx.signature = w.sign(tx.signing_data())

    print(json.dumps(tx.to_dict(), indent=2))