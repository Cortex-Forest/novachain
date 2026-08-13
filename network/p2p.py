import asyncio
import json
import ssl

MAX_MSG_BYTES = 64 * 1024 * 1024  # 单条消息上限 64MB（状态快照/大区块）


def _enc(msg):
    return json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"


class P2PNetwork:
    def __init__(self, node, host, port, use_tls, cert_file, key_file):
        self.node = node
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.cert_file = cert_file
        self.key_file = key_file
        self.server = None
        self.connections = set()

    def _create_ssl_context(self):
        if not self.use_tls:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_file, self.key_file)
        return ctx

    def _create_client_ssl_context(self):
        """出站连接的 TLS 客户端上下文（自签名证书，生产环境应改为固定证书校验）。"""
        if not self.use_tls:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _read_message(self, reader):
        """按换行分帧读取一条消息：先读 hello，再进入监听循环。"""
        raw = await reader.readuntil(b"\n")
        return json.loads(raw.strip())

    async def start_server(self):
        ssl_ctx = self._create_ssl_context()
        self.server = await asyncio.start_server(
            self._handle_connection, self.host, self.port, ssl=ssl_ctx, limit=MAX_MSG_BYTES)
        mode = "TLS 1.3" if ssl_ctx else "明文"
        print(f"[P2P] Nova 监听 {self.host}:{self.port} ({mode})")

    async def _handle_connection(self, reader, writer):
        peer = None
        try:
            msg = await self._read_message(reader)
            if msg.get("type") == "hello":
                peer = msg.get("node_id")
                if peer:
                    self.node.peers.add(peer)
                writer.write(_enc({
                    "type": "hello",
                    "node_id": self.node.node_id,
                    "height": self.node.consensus.chain_height(),
                    "dag_count": len(self.node.dag),
                }))
                await writer.drain()
                self.connections.add(writer)
                asyncio.create_task(self._listen(reader, peer, writer))
            else:
                await self.node.process_message(msg, peer, writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, json.JSONDecodeError, ConnectionError):
            pass
        except Exception:
            pass

    async def connect_to_peer(self, addr):
        try:
            h, p = addr.split(":")
            r, w = await asyncio.open_connection(h, int(p), ssl=self._create_client_ssl_context(),
                                                  limit=MAX_MSG_BYTES)
            w.write(_enc({
                "type": "hello",
                "node_id": self.node.node_id,
                "height": self.node.consensus.chain_height(),
                "dag_count": len(self.node.dag),
            }))
            await w.drain()
            self.connections.add(w)
            self.node.peers.add(addr)
            asyncio.create_task(self._listen(r, addr, w))
        except Exception:
            pass

    async def _listen(self, reader, peer, writer):
        try:
            while True:
                msg = await self._read_message(reader)
                await self.node.process_message(msg, peer, writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, json.JSONDecodeError, ConnectionError):
            pass
        except Exception:
            pass
        finally:
            self.connections.discard(writer)
            try:
                writer.close()
            except Exception:
                pass
            if peer in self.node.peers:
                self.node.peers.remove(peer)

    def close_all(self):
        for w in list(self.connections):
            try:
                w.close()
            except Exception:
                pass
        self.connections.clear()
        if self.server:
            self.server.close()

    async def gossip(self, msg, exclude=None):
        exclude = exclude or []
        for p in list(self.node.peers):
            if p in exclude:
                continue
            try:
                h, port = p.split(":")
                _, w = await asyncio.open_connection(h, int(port), ssl=self._create_client_ssl_context(),
                                                     limit=MAX_MSG_BYTES)
                w.write(_enc(msg))
                await w.drain()
                w.close()
            except Exception:
                pass