# syntax=docker/dockerfile:1
# ============================================================
#  Nova Chain 节点镜像（抗量子版）
#  ------------------------------------------------------------
#  构建:        docker build -t ghcr.io/cortex-forest/novachain:latest .
#  跳过抗量子:  docker build --build-arg NOVA_OQS=0 -t ghcr.io/cortex-forest/novachain:latest .
#               （构建更快，但签名回退 Ed25519，非抗量子，仅测试用）
#  运行:        docker run -d -p 8080:8080 -p 9000:9000 -v nova_data:/data \
#                 ghcr.io/cortex-forest/novachain:latest
# ============================================================

FROM python:3.14-slim

LABEL org.opencontainers.image.title="Nova Chain Node" \
      org.opencontainers.image.description="Nova Chain 抗量子创作者公链节点（CRYSTALS-Dilithium5）" \
      org.opencontainers.image.source="https://github.com/Cortex-Forest/novachain" \
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
        build-essential cmake git ca-certificates libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖分层缓存：先装依赖，源码变更不影响这一层
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 显式预编译并安装 liboqs 系统库（版本与 liboqs-python 匹配）。
# 比 liboqs-python 的「运行时自动下载编译」更可靠（官方 Dockerfile 同款做法）。
RUN if [ "$NOVA_OQS" = "1" ]; then \
        git clone --depth 1 --branch 0.16.0 https://github.com/open-quantum-safe/liboqs /tmp/liboqs \
        && cmake -S /tmp/liboqs -B /tmp/liboqs/build \
             -DBUILD_SHARED_LIBS=ON \
             -DOQS_BUILD_ONLY_LIB=ON \
             -DCMAKE_BUILD_TYPE=Release \
             -DCMAKE_INSTALL_PREFIX=/usr/local \
        && cmake --build /tmp/liboqs/build --parallel 4 \
        && cmake --build /tmp/liboqs/build --target install \
        && ldconfig \
        && rm -rf /tmp/liboqs; \
    fi

# 让 liboqs-python 直接找到系统 liboqs：
#   - OQS_INSTALL_PATH=/usr/local -> 直接从 /usr/local/lib/liboqs.so 加载
#   - ldconfig 已更新缓存，find_library 可兜底
ENV LD_LIBRARY_PATH=/usr/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} \
    OQS_INSTALL_PATH=/usr/local

# 安装 liboqs-python 并验证 Dilithium5 可用
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
