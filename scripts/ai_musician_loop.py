# -*- coding: utf-8 -*-
"""AI 音乐人离线圈子（提示词 3）——Suno 生成 → IPFS 上传 → 内容合约自动上架。

链上约定：
- 音乐人循环配置（nova:ai:muso:config）写入链上：enabled / schedule(daily|weekly) /
  hour / weekday / budget；维护循环按配置把 due 置位（nova_node._run_daily_maintenance）。
- 本脚本到点（/api/ai/status 的 muso.due = true）后执行：
    1. SunoClient.generate(prompt)  → (标题, 音频字节)   （默认 Mock，可接真实 Suno API）
    2. IPFSUploader.upload(bytes)   → IPFS CID           （默认本地文件仓库，可接 ipfshttpclient/CLI）
    3. 广播 nova:ai:work:create     → 作品自动上架（售价由链上 suggest_price 自动定价）
- 作品销售按合约分账：创作者 70% / 算力节点 20% / AI 成长基金 10%。

用法：
  # 内存演示：跑一轮完整流程（注册 AI 创作者 → 配置循环 → 生成 → 上架 → 购买分账）
  python scripts/ai_musician_loop.py --demo --once
  python scripts/ai_musician_loop.py --demo --loop --interval 60

  # 对接本地节点（真实广播，需节点已注册该 AI 创作者）
  python scripts/ai_musician_loop.py --rpc http://127.0.0.1:8080 \
      --ai-addr 0x... --ai-priv <32字节hex> --once
"""
import argparse
import json
import os
import struct
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from core.crypto import QuantumWallet
    from core.transaction import Tx
except Exception as e:  # pragma: no cover
    print(f"[AI-MUSO] 无法导入 Nova 核心模块: {e}")
    sys.exit(1)

# 演示用提示词池
PROMPTS = [
    "流行电子，BPM 120，女声，副歌抓耳",
    "慵懒爵士钢琴，夜晚咖啡馆氛围",
    "宇宙环境音 + 缓拍，适合冥想",
    "赛博朋克合成器浪潮，高速追击感",
    "原声吉他民谣，星夜与远航主题",
]
TITLE_PREFIX = ["星轨", "量子", "月面", "深空", "霓虹"]


def canonical_amount(n: float) -> str:
    return ("%.8f" % n).rstrip("0").rstrip(".") or "0"


class SunoClient:
    """Suno API 客户端。默认 Mock 生成一段 8 秒 44.1kHz 16bit 单声道 WAV，
    便于离线演示；设置 --suno-url / --suno-key 后走真实 HTTP 调用。"""

    def __init__(self, url=None, key=None):
        self.url = url
        self.key = key
        self._i = 0

    def generate(self, prompt: str):
        self._i += 1
        if self.url:
            req = urllib.request.Request(
                self.url.rstrip("/") + "/generate",
                data=json.dumps({"prompt": prompt}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + (self.key or "")},
                method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            return data.get("title", "Suno 生成曲"), bytes(data.get("audio", []))
        # Mock：8 秒正弦扫频 WAV（16bit mono 44.1kHz）
        title = TITLE_PREFIX[self._i % len(TITLE_PREFIX)] + "回响"
        sr = 44100
        frames = bytearray()
        n = sr * 8
        for t in range(n):
            f = 220 + 440 * (t / n)
            v = int(12000 * ((t % sr) / sr) * 0.5 + 12000 * 0.4)
            frames += struct.pack("<h", v if t % 4410 else v // 4)
        wav = (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
               + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
               + b"data" + struct.pack("<I", len(frames)) + bytes(frames))
        return title, wav


class IPFSUploader:
    """IPFS 上传。默认写入本地文件仓库（store 目录下 <cid>.bin），
    便于无 IPFS 环境演示；可传入 ipfshttpclient 或 ipfs CLI 实现。"""

    def __init__(self, store_dir: str, api_url: str = None, binary: str = None):
        self.store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)
        self.api_url = api_url
        self.binary = binary or "ipfs"

    def upload(self, data: bytes) -> str:
        if self.api_url:
            import urllib.parse
            url = self.api_url.rstrip("/") + "/api/v0/add"
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode()).get("Hash", "bafy-err")
        import hashlib
        h = hashlib.sha256(data).hexdigest()
        cid = "bafy" + h[:44]
        with open(os.path.join(self.store_dir, cid + ".bin"), "wb") as f:
            f.write(data)
        return cid


# ---------------------------------------------------------------------------
# 内存演示节点
# ---------------------------------------------------------------------------
def _build_demo_node():
    from nova_node import NovaNode
    node = NovaNode(host="127.0.0.1", p2p=9973, rpc=8325, use_tls=False, state_file=None)
    return node


def _signed_tx(w, op, amount=0.0, **kw):
    data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _apply(node, tx):
    assert node.validate_tx(tx), "校验失败: " + tx.data[:120]
    node.apply_tx(tx)


def run_demo_once(suno: SunoClient, uploader: IPFSUploader, node=None):
    node = node or _build_demo_node()
    ai = node.ai_service
    human = QuantumWallet()
    ai_w = QuantumWallet()
    fan = QuantumWallet()
    for w in (human, ai_w, fan):
        node.balances[w.address] = 100000.0
    _apply(node, _signed_tx(ai_w, "nova:ai:register", name="Nova 音乐精灵",
                            owner=human.address, daily_budget=100.0))
    _apply(node, _signed_tx(ai_w, "nova:ai:svc:register", service_type="suno",
                            name="Suno 音乐生成", model="suno-v4",
                            endpoint_hash="sha256:suno-v4"))
    _apply(node, _signed_tx(ai_w, "nova:ai:muso:config", enabled=True, schedule="daily",
                            hour=0, weekday=0, budget=50.0))
    # 模拟维护循环到点
    node.store.ai_muso["due"] = True
    assert ai.muso_is_due()
    assert ai.muso_take_due(ai_w.address)
    print("[AI-MUSO] 循环到点（daily 00:00），消费 due，开始创作…")

    prompt = PROMPTS[0]
    title, audio = suno.generate(prompt)
    cid = uploader.upload(audio)
    print(f"[AI-MUSO] Suno 生成「{title}」（{len(audio)} 字节）→ IPFS {cid[:20]}…")

    _apply(node, _signed_tx(ai_w, "nova:ai:work:create", title=title,
                            cid=cid, task_type="ai_music", meta="Suno v4 · " + prompt))
    wid = list(node.store.ai_works)[0]
    work = node.store.ai_works[wid]
    print(f"[AI-MUSO] 作品上架「{work['title']}」售价 {work['price']} NOVA（自动定价）")

    _apply(node, _signed_tx(fan, "nova:ai:work:buy", wid=wid, amount=work["price"]))
    fund = ai.fund_view()
    print(f"[AI-MUSO] 售出分账：创作者 {work['price']*0.70:.4f} / 算力 {work['price']*0.20:.4f} / "
          f"基金 {work['price']*0.10:.4f} NOVA；成长基金余额 {fund['balance']:.4f} NOVA")
    return work


# ---------------------------------------------------------------------------
# RPC 模式：读取链上状态 + 广播上架
# ---------------------------------------------------------------------------
def _rpc_get(rpc: str, path: str) -> dict:
    with urllib.request.urlopen(rpc.rstrip("/") + path, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _rpc_op(rpc: str, priv: str, data: str, amount: float = 0.0) -> dict:
    from core.crypto import QuantumWallet
    w = QuantumWallet()
    w._seed = bytes.fromhex(priv)
    # QuantumWallet 接口：public_key_hex / sign / address 派生
    pub = w.public_key_hex()
    addr = w.address
    ts = int(time.time())
    sig = w.sign(f"{addr}{addr}{canonical_amount(amount)}{ts}[]" + data + pub)
    payload = {"addr": addr, "amount": amount, "data": data, "timestamp": ts,
               "sender_public_key": pub, "signature": sig}
    req = urllib.request.Request(rpc.rstrip("/") + "/api/op",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def run_rpc_once(rpc: str, priv: str, suno: SunoClient, uploader: IPFSUploader):
    status = _rpc_get(rpc, "/api/ai/status")
    muso = status.get("muso") or {}
    if not muso.get("enabled"):
        print("[AI-MUSO] 音乐人循环未启用（链上配置），跳过")
        return None
    if not muso.get("due"):
        print("[AI-MUSO] 未到创作时间（muso.due=false），跳过")
        return None
    prompt = PROMPTS[int(time.time()) % len(PROMPTS)]
    title, audio = suno.generate(prompt)
    cid = uploader.upload(audio)
    data = json.dumps({"op": "nova:ai:work:create", "title": title, "cid": cid,
                       "task_type": "ai_music",
                       "meta": "Suno v4 · " + prompt}, ensure_ascii=False)
    res = _rpc_op(rpc, priv, data)
    print(f"[AI-MUSO] 上架交易已广播：{res.get('txid', '')}（{title} / {cid[:20]}…）")
    if res.get("error"):
        print("[AI-MUSO] 链上拒绝:", res["error"])
        return None
    return res


def main():
    ap = argparse.ArgumentParser(description="AI 音乐人离线圈子")
    ap.add_argument("--demo", action="store_true", help="内存演示节点模式")
    ap.add_argument("--rpc", help="本地节点 RPC 地址（如 http://127.0.0.1:8080）")
    ap.add_argument("--ai-priv", help="AI 创作者私钥（32 字节 hex，RPC 模式必需）")
    ap.add_argument("--store", default="./.ai_muso_store", help="IPFS 本地文件仓库目录")
    ap.add_argument("--suno-url", help="真实 Suno API 地址（缺省为 Mock）")
    ap.add_argument("--suno-key", help="Suno API Key")
    ap.add_argument("--ipfs-api", help="IPFS HTTP API（缺省为本地文件仓库）")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--loop", action="store_true", help="循环模式")
    ap.add_argument("--interval", type=int, default=3600, help="循环间隔秒数")
    args = ap.parse_args()

    suno = SunoClient(args.suno_url, args.suno_key)
    uploader = IPFSUploader(args.store, args.ipfs_api)

    if args.rpc:
        if not args.ai_priv:
            print("RPC 模式需要 --ai-priv（AI 创作者私钥）")
            return 1
        if args.loop and not args.once:
            while True:
                run_rpc_once(args.rpc, args.ai_priv, suno, uploader)
                time.sleep(args.interval)
        else:
            run_rpc_once(args.rpc, args.ai_priv, suno, uploader)
        return 0

    if args.loop and not args.once:
        while True:
            run_demo_once(suno, uploader)
            time.sleep(args.interval)
    else:
        run_demo_once(suno, uploader)
    return 0


if __name__ == "__main__":
    sys.exit(main())
