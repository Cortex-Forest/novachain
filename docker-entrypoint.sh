#!/bin/sh
# Nova Chain 节点容器入口：
#   1. 确保 /data 存在
#   2. 首次启动时自动生成自签名 TLS 证书（10 年有效期，幂等）
#   3. 透传参数启动节点
set -e

DATA_DIR="${NOVA_DATA_DIR:-/data}"
CERT_FILE="${DATA_DIR}/cert.pem"
KEY_FILE="${DATA_DIR}/key.pem"

mkdir -p "$DATA_DIR"

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "[entrypoint] 生成自签名 TLS 证书 -> $DATA_DIR"
    python -c "from cert_gen import generate_self_signed_cert; generate_self_signed_cert('$CERT_FILE', '$KEY_FILE')"
fi

echo "[entrypoint] 启动 Nova 节点: python nova_node.py $*"
exec python nova_node.py "$@"
