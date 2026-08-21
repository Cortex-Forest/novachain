# syntax=docker/dockerfile:1
# ============================================================
#  Nova Chain 节点镜像（抗量子版）
#  ------------------------------------------------------------
#  构建:        docker build -t spurtniwa/nova:latest .
#  跳过抗量子:  docker build --build-arg NOVA_OQS=0 -t spurtniwa/nova:latest .
#               （构建更快，但签名回退 Ed25519，非抗量子，仅测试用）
#  运行:        docker run -d -p 8080:8080 -p 9000:9000 -v nova_data:/data \
#                 spurtniwa/nova:latest
# ============================================================

FROM python:3.14-slim

LABEL org.opencontainers.image.title="Nova Chain Node" \
      org.opencontainers.image.description="Nova Chain 抗量子创作者公链节点（CRYSTALS-Dilithium5）" \
      org.opencontainers.image.source="https://github.com/spurtniwa/nova" \
      org.opencontainers.image.version="0.11"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NOVA_DATA_DIR=/data

# 编译工具：liboqs-python 首次 import 时需要现场编译 liboqs（git + cmake + C 编译器）。
# 这些依赖在构建期安装并在镜像内保留，保证运行时无需联网/编译。
# 如需进一步瘦身，可在构建 liboqs 后拆分为多阶段，把编译工具剔除出最终镜像。
ARG NOVA_OQS=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖分层缓存：先装依赖，源码变更不影响这一层
COPY requirements.txt ./
RUN pip install -r requirements.txt
RUN if [ "$NOVA_OQS" = "1" ]; then \
        pip install liboqs-python==0.16.0 \
        && python -c "import oqs; s = oqs.Signature('Dilithium5'); s.free(); print('liboqs OK, version', oqs.get_liboqs_version())"; \
    fi

# 拷贝源码（genesis.json 等运行时文件一并带入）
COPY . .

# 入口脚本：首次启动自动生成自签名 TLS 证书
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 非 root 运行（降低容器逃逸风险）
RUN useradd -m -u 10001 nova && mkdir -p /data && chown -R nova:nova /data
USER nova

# 链状态 / TLS 证书 / 聊天索引 持久化目录
VOLUME ["/data"]

# P2P 与 RPC 端口
EXPOSE 9000 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=4).status==200 else 1)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["--host", "0.0.0.0", "--p2p", "9000", "--rpc", "8080", "--state", "/data/chain_state.json", "--cert", "/data/cert.pem", "--key", "/data/key.pem"]
