import asyncio, json, ssl

class P2PNetwork:
    def __init__(self, node, host, port, use_tls, cert_file, key_file):
        self.node = node
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.cert_file = cert_file
        self.key_file = key_file
        self.server = None

    def _create_ssl_context(self):
        if not self.use_tls: return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_file, self.key_file)
        return ctx

    async def start_server(self):
        ssl_ctx = self._create_ssl_context()
        self.server = await asyncio.start_server(self._handle_connection, self.host, self.port, ssl=ssl_ctx)
        mode = "TLS 1.3" if ssl_ctx else "明文"
        print(f"[P2P] Nova 监听 {self.host}:{self.port} ({mode})")

    async def _handle_connection(self, reader, writer):
        peer = None
        try:
            data = await reader.read(16384)
            if data:
                msg = json.loads(data.decode())
                if msg.get("type") == "hello":
                    peer = msg["node_id"]
                    self.node.peers.add(peer)
                    writer.write(json.dumps({"type":"hello","node_id":self.node.node_id}).encode())
                    await writer.drain()
                    asyncio.create_task(self._listen(reader, peer))
                else:
                    await self.node.process_message(msg, peer)
        except: pass

    async def connect_to_peer(self, addr):
        try:
            h, p = addr.split(":")
            r, w = await asyncio.open_connection(h, int(p))
            w.write(json.dumps({"type":"hello","node_id":self.node.node_id}).encode())
            await w.drain()
            self.node.peers.add(addr)
            asyncio.create_task(self._listen(r, addr))
        except: pass

    async def _listen(self, reader, peer):
        try:
            while True:
                data = await reader.read(16384)
                if not data: break
                await self.node.process_message(json.loads(data.decode()), peer)
        except: pass
        finally:
            if peer in self.node.peers: self.node.peers.remove(peer)

    async def gossip(self, msg, exclude=None):
        exclude = exclude or []
        for p in list(self.node.peers):
            if p in exclude: continue
            try:
                h, port = p.split(":")
                _, w = await asyncio.open_connection(h, int(port))
                w.write(json.dumps(msg).encode())
                await w.drain()
                w.close()
            except: pass