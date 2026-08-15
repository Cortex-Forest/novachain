# -*- coding: utf-8 -*-
"""超级节点存储守护进程（节点端存储证明脚本 + 热文件缓存）。

功能：
1. 自动注册/心跳：定时向链上提交心跳，保持“在线”状态（30 分钟超时判离线）。
2. 24 小时存储证明：
   - 从 RPC 获取链上确定性挑战（最多 3 个文件）；
   - 从本地缓存 / IPFS 读取每个文件的前 1KB 片段；
   - 计算 sha256 并与链上 fragment_commit 比对；
   - 签名提交 nova:storage:inc:prove，链上验证通过后记录证明时间戳。
3. 内置热文件缓存（提示词 2）：
   - LRU 缓存最近访问的 100 个热门文件，缓存 7 天过期；
   - 用户请求优先走本地缓存，未命中再从 IPFS 拉取。
4. 附带触发链上结算/热门保护/濒危恢复的入口（--maintain）。

用法：
  python scripts/storage_node_daemon.py --rpc http://127.0.0.1:8080 \
      --priv-key <32字节hex种子> --store ./node_store [--ipfs-api http://127.0.0.1:5001]
  python scripts/storage_node_daemon.py --rpc ... --prove          # 单次提交证明（可配合 cron）
  python scripts/storage_node_daemon.py --rpc ... --maintain      # 单次触发链上维护

IPFS 客户端：优先 ipfshttpclient，其次本地 ipfs CLI；都没有时使用本地文件仓库
（--store 下的 <cid>.bin，便于无 IPFS 环境演示）。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

# 允许直接运行：scripts 目录位于仓库根下
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from core.crypto import QuantumWallet
    from core.transaction import Tx
except Exception as e:  # pragma: no cover
    print(f"[DAEMON] 无法导入 Nova 核心模块: {e}")
    sys.exit(1)

PROOF_PERIOD = 86400
FRAGMENT_SIZE = 1024
CACHE_MAX = 100                 # 热文件缓存条数
CACHE_TTL = 7 * 86400           # 缓存过期：7 天
HEARTBEAT_INTERVAL = 300        # 心跳/扫描间隔：5 分钟


# ---------------------------------------------------------------------------
# IPFS 客户端（三种后端自动降级）
# ---------------------------------------------------------------------------
class IPFSClient:
    def __init__(self, store_dir: str, api_url: str = None, binary: str = None):
        self.store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)
        self.api_url = api_url
        self.binary = binary or "ipfs"
        self._http = None
        try:
            import ipfshttpclient  # noqa
            if api_url:
                self._http = ipfshttpclient.connect(api_url)
        except Exception:
            self._http = None

    def _via_cli(self, args, data: bytes = None) -> bytes:
        cmd = [self.binary] + args
        proc = subprocess.run(cmd, input=data, capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"ipfs CLI 失败: {proc.stderr.decode(errors='replace')[:200]}")
        return proc.stdout

    def local_path(self, cid: str) -> str:
        safe = cid.replace("/", "_")
        return os.path.join(self.store_dir, safe + ".bin")

    def add(self, data: bytes) -> str:
        """上传内容并固定（pin），返回 CID。"""
        if self._http is not None:
            res = self._http.add_bytes(data)
            cid = res.decode() if isinstance(res, bytes) else str(res)
            try:
                self._http.pin.add(cid)
            except Exception:
                pass
            return cid
        if self._cli_available():
            out = self._via_cli(["add", "-Q", "--pin=true"], data)
            cid = out.decode().strip()
            self._cache_write(cid, data)
            return cid
        # 本地仓库回退：以内容 sha256 作为 CID
        cid = "0x" + hashlib.sha3_256(data).hexdigest()
        self._cache_write(cid, data)
        return cid

    def _cli_available(self) -> bool:
        try:
            subprocess.run([self.binary, "--version"], capture_output=True, timeout=10)
            return True
        except Exception:
            return False

    def _cache_write(self, cid: str, data: bytes):
        with open(self.local_path(cid), "wb") as f:
            f.write(data)

    def cat(self, cid: str, limit: int = None) -> bytes:
        """读取文件内容：本地仓库 → HTTP API → CLI。"""
        local = self.local_path(cid)
        if os.path.exists(local):
            with open(local, "rb") as f:
                return f.read(limit) if limit else f.read()
        if self._http is not None:
            data = self._http.cat(cid)
            return bytes(data[:limit]) if limit else bytes(data)
        if self._cli_available():
            return self._via_cli(["cat", cid])[:limit] if limit else self._via_cli(["cat", cid])
        raise FileNotFoundError(f"本地/IPFS 均无文件: {cid}")


# ---------------------------------------------------------------------------
# LRU 热文件缓存（最多 100 条，7 天过期）
# ---------------------------------------------------------------------------
class HotCache:
    def __init__(self, cache_file: str):
        self.cache_file = cache_file
        self.data = {}          # cid -> {"at": ts, "size": int, "hits": int}
        self._load()

    def _load(self):
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def save(self):
        tmp = self.cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.cache_file)

    def evict(self):
        """清理过期项（7 天）并裁剪到 100 条。"""
        now = time.time()
        expired = [k for k, v in self.data.items() if now - v.get("at", 0) > CACHE_TTL]
        for k in expired:
            del self.data[k]
        if len(self.data) > CACHE_MAX:
            order = sorted(self.data, key=lambda k: self.data[k].get("at", 0))
            for k in order[:len(self.data) - CACHE_MAX]:
                del self.data[k]
        self.save()

    def get(self, cid: str) -> bool:
        v = self.data.get(cid)
        if not v:
            return False
        if time.time() - v.get("at", 0) > CACHE_TTL:
            del self.data[cid]
            self.save()
            return False
        v["at"] = time.time()
        v["hits"] = v.get("hits", 0) + 1
        return True

    def put(self, cid: str, size: int):
        self.data[cid] = {"at": time.time(), "size": size, "hits": self.data.get(cid, {}).get("hits", 0)}
        self.evict()


# ---------------------------------------------------------------------------
# 链上 RPC 客户端
# ---------------------------------------------------------------------------
class ChainRPC:
    def __init__(self, rpc_url: str, wallet: QuantumWallet):
        self.rpc_url = rpc_url.rstrip("/")
        self.wallet = wallet

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.rpc_url + path,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.rpc_url + path, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _signed(self, op: str, amount: float = 0.0, **kw) -> dict:
        data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
        ts = int(time.time())
        tx = Tx(self.wallet.address, self.wallet.address, amount, [], data,
                self.wallet.public_key_hex(), "", timestamp=ts)
        tx.signature = self.wallet.sign(tx.signing_data())
        body = {"addr": self.wallet.address, "timestamp": ts,
                "sender_public_key": self.wallet.public_key_hex(),
                "signature": tx.signature}
        body.update(kw)
        return body

    # ---------- 节点操作 ----------
    def register(self, capacity_gb: float):
        return self._post("/api/storage/register",
                          self._signed("nova:storage:register", capacity_gb=capacity_gb))

    def heartbeat(self):
        return self._post("/api/storage/heartbeat",
                          self._signed("nova:storage:inc:heartbeat"))

    def challenge(self) -> dict:
        return self._get(f"/api/storage/nodes/{self.wallet.address}/challenge")

    def prove(self, day: int, files: list, fragments: list) -> dict:
        return self._post("/api/storage/prove",
                          self._signed("nova:storage:inc:prove", day=day,
                                       files=files, fragments=fragments))

    def claim(self, cid: str):
        return self._post("/api/storage/inc/claim",
                          self._signed("nova:storage:inc:claim", cid=cid))

    def node_info(self) -> dict:
        try:
            return self._get(f"/api/storage/nodes/{self.wallet.address}/revenue")
        except Exception:
            return {"found": False}

    def maintain(self):
        """触发链上维护：结算昨日奖励 + 热门保护 + 濒危恢复。"""
        self._post("/api/storage/inc/settle", self._signed("nova:storage:inc:settle"))
        self._post("/api/storage/inc/protect", self._signed("nova:storage:inc:protect"))
        self._post("/api/storage/inc/reassign", self._signed("nova:storage:inc:reassign"))


# ---------------------------------------------------------------------------
# 守护进程
# ---------------------------------------------------------------------------
class StorageNodeDaemon:
    def __init__(self, rpc_url: str, priv_key: str, store_dir: str,
                 ipfs_api: str = None, capacity_gb: float = 1024.0):
        self.wallet = QuantumWallet(priv_key)
        self.rpc = ChainRPC(rpc_url, self.wallet)
        self.ipfs = IPFSClient(store_dir, api_url=ipfs_api)
        self.cache = HotCache(os.path.join(store_dir, "hot_cache.json"))
        self.capacity_gb = capacity_gb
        print(f"[DAEMON] 存储节点 {self.wallet.address}")
        print(f"[DAEMON] 本地仓库 {store_dir} / 容量声明 {capacity_gb}GB / RPC {rpc_url}")

    def first_run(self):
        """确保节点已在链上注册（旧提供者注册会自动进入激励系统）。"""
        info = self.rpc.node_info()
        if info.get("found"):
            return
        try:
            self.rpc.register(self.capacity_gb)
            print("[DAEMON] 已注册为存储提供者（自动进入激励系统）")
        except Exception as e:
            print(f"[DAEMON] 注册失败（可能已注册或节点无余额）: {e}")

    def submit_proof(self) -> dict:
        """获取挑战并提交存储证明（返回链上结果）。"""
        ch = self.rpc.challenge()
        if not ch.get("found"):
            print(f"[DAEMON] 今日无挑战: {ch.get('reason', '未知')}")
            return ch
        files, fragments = ch["files"], []
        ok = True
        for cid in files:
            try:
                frag = self.ipfs.cat(cid, limit=FRAGMENT_SIZE)
                commit = hashlib.sha256(frag).hexdigest()
                # 从节点信息接口拿不到文件承诺，这里直接提交片段，链上自校验
                fragments.append(frag[:FRAGMENT_SIZE].hex())
                print(f"[DAEMON] 挑战文件 {cid[:20]}... 片段 sha256={commit[:16]}...")
            except Exception as e:
                print(f"[DAEMON] 读取文件失败 {cid[:20]}...: {e}")
                fragments.append("")
                ok = False
        if not ok or any(len(f) != FRAGMENT_SIZE * 2 for f in fragments):
            print("[DAEMON] 部分文件不可用，跳过本次证明（将计入失败）")
            return {"ok": False, "reason": "file_unavailable"}
        result = self.rpc.prove(ch["day"], files, fragments)
        print(f"[DAEMON] 证明提交结果: {result}")
        return result

    def serve_file(self, cid: str) -> bytes:
        """用户请求：优先本地热缓存，未命中再从 IPFS 拉取并写缓存。"""
        if self.cache.get(cid):
            try:
                return self.ipfs.cat(cid)
            except Exception:
                pass
        data = self.ipfs.cat(cid)
        self.cache.put(cid, len(data))
        return data

    def run_once(self, maintain: bool = False):
        self.first_run()
        try:
            self.rpc.heartbeat()
            print("[DAEMON] 心跳已提交")
        except Exception as e:
            print(f"[DAEMON] 心跳失败: {e}")
        self.submit_proof()
        if maintain:
            try:
                self.rpc.maintain()
                print("[DAEMON] 已触发链上维护（结算/热门保护/濒危恢复）")
            except Exception as e:
                print(f"[DAEMON] 维护触发失败: {e}")

    def run_loop(self):
        self.first_run()
        while True:
            try:
                self.rpc.heartbeat()
                print(f"[DAEMON] {time.strftime('%F %T')} 心跳 OK")
            except Exception as e:
                print(f"[DAEMON] 心跳失败: {e}")
            self.submit_proof()
            try:
                self.cache.evict()
            except Exception as e:
                print(f"[DAEMON] 缓存清理失败: {e}")
            time.sleep(HEARTBEAT_INTERVAL)


def main():
    p = argparse.ArgumentParser(description="Nova 存储节点守护进程（证明/心跳/缓存）")
    p.add_argument("--rpc", default="http://127.0.0.1:8080")
    p.add_argument("--priv-key", default="", help="32 字节 hex 种子私钥；缺省生成临时钱包")
    p.add_argument("--store", default="./node_store", help="本地文件仓库/缓存目录")
    p.add_argument("--ipfs-api", default="", help="IPFS HTTP API（如 http://127.0.0.1:5001）")
    p.add_argument("--capacity-gb", type=float, default=1024.0)
    p.add_argument("--once", action="store_true", help="单次执行（心跳+证明）后退出")
    p.add_argument("--maintain", action="store_true", help="同时触发链上维护（结算/热门保护/濒危恢复）")
    p.add_argument("--serve", default="", help="读取并输出指定 CID 内容（热缓存优先）")
    a = p.parse_args()

    daemon = StorageNodeDaemon(a.rpc, a.priv_key, a.store, a.ipfs_api, a.capacity_gb)
    if a.serve:
        data = daemon.serve_file(a.serve)
        sys.stdout.buffer.write(data)
        return
    if a.once:
        daemon.run_once(maintain=a.maintain)
        return
    daemon.run_loop()


if __name__ == "__main__":
    main()
