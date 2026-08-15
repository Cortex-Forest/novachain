# -*- coding: utf-8 -*-
"""算力网络端到端演示（提示词 1-5）。

流程：
1. 算力节点注册 + 超级节点自动资格 + 规格公开可查（提示词 1）；
2. 五类任务参考价与需求规格（提示词 1 / 5）；
3. 抢单模式：双节点一致结果自动结算 + 1% 手续费回流激励池（提示词 2）；
4. 竞价模式：节点报价、发起者选标（提示词 2）；
5. 结果不一致 → 第三方节点仲裁（提示词 2 / 4）；
6. 争议冻结 + 社区 3 票仲裁，预算退回发起者（提示词 2）；
7. 5% 随机抽查：命中后第三方审计，发现错误罚没双倍质押、信誉分清零（提示词 4）；
8. 激励池按存储 40% / 算力 60% 分配 + 信誉加成（提示词 5）；
9. AI 服务接入、自动定价与 70/20/10 分账（提示词 3）。

用法：
  python scripts/compute_network_demo.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.compute import TASK_TYPES, AUDIT_RATE
from core.crypto import QuantumWallet
from core.transaction import Tx
from nova_node import NovaNode


def _node():
    return NovaNode(host="127.0.0.1", p2p=9972, rpc=8324, use_tls=False, state_file=None)


def _fund(node, addr, amt=100000.0):
    node.balances[addr] = amt


def _signed_tx(w, op, amount=0.0, **kw):
    data = json.dumps(dict(op=op, **kw), ensure_ascii=False)
    ts = int(time.time())
    tx = Tx(w.address, w.address, amount, [], data, w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    return tx


def _apply(node, tx):
    assert node.validate_tx(tx), "校验失败: " + tx.data[:120]
    node.apply_tx(tx)


def _stake_validator(node, w, amt=1000.0):
    ts = int(time.time())
    tx = Tx(w.address, w.address, amt, [], "nova:stake", w.public_key_hex(), "", timestamp=ts)
    tx.signature = w.sign(tx.signing_data())
    _apply(node, tx)


def _register(node, w, cpu=8, gpu="RTX 4090", vram=24, ram=64, storage=512,
              region="cn-east", lat=30):
    _apply(node, _signed_tx(w, "nova:compute:register", cpu_cores=cpu, gpu_model=gpu,
                            gpu_vram_gb=vram, ram_gb=ram, storage_gb=storage,
                            region=region, latency_ms=lat))


def _stake(node, w, amt=200.0):
    _apply(node, _signed_tx(w, "nova:compute:stake", amount=amt))


def _publish(node, creator, spec, bounty, task_type, mode="grab", min_nodes=2,
             hours=24, acceptance=""):
    _apply(node, _signed_tx(creator, "nova:compute:publish", amount=bounty, spec=spec,
                            task_type=task_type, mode=mode, min_nodes=min_nodes,
                            expires_in=hours * 3600, acceptance=acceptance))
    return sorted(node.store.compute_tasks,
                  key=lambda t: node.store.compute_tasks[t]["created_at"])[-1]


def main():
    node = _node()
    cm = node.compute_market
    print("=" * 68)
    print("Nova 算力网络 · 端到端演示（提示词 1-5）")
    print("=" * 68)

    # ---------- 1. 节点注册与质押 ----------
    creator, w1, w2, w3, w4, w5, w6 = (QuantumWallet() for _ in range(7))
    for w in (creator, w1, w2, w3, w4, w5, w6):
        _fund(node, w.address)
    _register(node, w1, cpu=32, gpu="A100 x2", vram=160, ram=256, storage=4096, lat=12)
    _register(node, w2, cpu=16, gpu="RTX 4090", vram=24, ram=128, storage=2048, lat=24)
    _register(node, w3, cpu=8, gpu="T4", vram=16, ram=64, storage=512, lat=40)
    _register(node, w4, cpu=4, gpu="", vram=0, ram=32, storage=256, lat=55)
    _register(node, w5, cpu=16, gpu="A100", vram=80, ram=128, storage=2048, lat=20)
    _register(node, w6, cpu=8, gpu="RTX 4080", vram=16, ram=64, storage=512, lat=35)
    _stake(node, w1, 1200)
    _stake(node, w2, 600)
    _stake(node, w3, 200)
    _stake(node, w4, 100)
    _stake(node, w5, 400)
    _stake(node, w6, 200)
    print("[1] 节点注册与质押")
    for w in (w1, w2, w3, w4, w5, w6):
        v = cm.node_view(w.address)
        print("    - %s: %s核 %s %sGB显存 / 质押 %s NOVA / 信誉 %s（%s）"
              % (w.address[:10], v["spec"]["cpu_cores"],
                 v["spec"]["gpu_model"] or "无GPU", v["spec"]["gpu_vram_gb"],
                 v["stake"], v["reputation"]["score"], v["reputation"]["tier"]))
    validator = QuantumWallet()
    _fund(node, validator.address)
    _stake_validator(node, validator, 1000)
    print("    - 超级节点（验证者质押）自动具备算力资格: %s"
          % cm.is_qualified_node(validator.address))

    # ---------- 2. 参考价 ----------
    print("[2] 任务类型与参考价")
    for k, t in TASK_TYPES.items():
        print("    - %-16s %-8s 参考价 %s-%s NOVA"
              % (t["name"], k, t["price_min"], t["price_max"]))

    # ---------- 3. 抢单 + 双节点一致结算 ----------
    tid = _publish(node, creator, "生成一首 3 分钟流行歌曲（BPM 120）", 20.0, "ai_music",
                   acceptance="时长 3 分钟，采样率 44.1kHz")
    pool0 = node.balances.get(node.economy.VALIDATOR_POOL, 0.0)
    for w in (w1, w2):
        _apply(node, _signed_tx(w, "nova:compute:accept", task_id=tid))
    r1 = "aa" * 32
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid, result_hash=r1))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid, result_hash=r1))
    task = node.store.compute_tasks[tid]
    fee = task["fee"]
    pool1 = node.balances.get(node.economy.VALIDATOR_POOL, 0.0)
    print("[3] 抢单任务「%s」→ %s" % (task["spec"][:18], task["status"]))
    print("    双节点结果一致自动结算：每节点 %.4f NOVA（含信誉加成），手续费 %.4f NOVA 回流激励池"
          % (task["shares"].get(w1.address, 0), fee))
    assert abs((pool1 - pool0) - fee) < 1e-6 and task["status"] == "completed"

    # ---------- 4. 竞价模式 ----------
    tid2 = _publish(node, creator, "生成 4K 城市夜景图像", 15.0, "ai_image", mode="bid", min_nodes=2)
    _apply(node, _signed_tx(w2, "nova:compute:bid", task_id=tid2, price=8.0))
    _apply(node, _signed_tx(w3, "nova:compute:bid", task_id=tid2, price=6.5))
    _apply(node, _signed_tx(creator, "nova:compute:award", task_id=tid2,
                            workers=[w2.address, w3.address]))
    t2 = node.store.compute_tasks[tid2]
    print("[4] 竞价任务（w2 报 8 / w3 报 6.5）→ 发起者选标 w2+w3 → %s，执行节点 %s"
          % (t2["status"], ",".join(a[:8] for a in t2["assigned"])))

    # ---------- 5. 第三方仲裁 ----------
    tid3 = _publish(node, creator, "视频转码 4K→1080p", 30.0, "video_transcode", min_nodes=2)
    for w in (w1, w2):
        _apply(node, _signed_tx(w, "nova:compute:accept", task_id=tid3))
    _apply(node, _signed_tx(w1, "nova:compute:submit", task_id=tid3, result_hash="c1" * 32))
    _apply(node, _signed_tx(w2, "nova:compute:submit", task_id=tid3, result_hash="c2" * 32))
    assert node.store.compute_tasks[tid3]["status"] == "arbitrating"
    _apply(node, _signed_tx(w3, "nova:compute:arbitrate", task_id=tid3, result_hash="c1" * 32))
    t3 = node.store.compute_tasks[tid3]
    print("[5] 双节点结果不一致 → 第三方仲裁（w3 判定 c1 正确）→ %s，仲裁节点 %s"
          % (t3["status"], t3["arbiter"][:10]))

    # ---------- 6. 争议 + 社区仲裁 ----------
    tid4 = _publish(node, creator, "清洗 10000 条用户数据", 10.0, "data_clean", min_nodes=2)
    for w in (w3, w4):
        _apply(node, _signed_tx(w, "nova:compute:accept", task_id=tid4))
    r4 = "dd" * 32
    _apply(node, _signed_tx(w3, "nova:compute:submit", task_id=tid4, result_hash=r4))
    _apply(node, _signed_tx(w4, "nova:compute:submit", task_id=tid4, result_hash=r4))
    _apply(node, _signed_tx(creator, "nova:compute:dispute", task_id=tid4, reason="结果质量不达标"))
    assert node.store.compute_tasks[tid4]["status"] == "disputed"
    voters = []
    for _ in range(3):
        v = QuantumWallet()
        _fund(node, v.address)
        _stake_validator(node, v, 1000)
        voters.append(v)
    for v in voters:
        _apply(node, _signed_tx(v, "nova:compute:vote", task_id=tid4, support="uphold"))
    assert cm._settle_disputes() == 1
    t4 = node.store.compute_tasks[tid4]
    print("[6] 发起者 24h 内异议 → 预算冻结 → 社区 3 票支持 → %s，预算 %.2f NOVA 退回发起者"
          % (t4["status"], t4["bounty"]))

    # ---------- 7. 随机抽查 ----------
    # 注意：w2 在仲裁中被判负（wrong -10 + 投诉 -10），信誉 33 < 40 已降级为轻量节点，
    # 只能接 data_clean 类任务 —— 正是信誉机制在起作用。
    print("    [信誉机制] w2 仲裁判负后信誉降至 %s，已降级为轻量节点（仅 data_clean 可接）"
          % node.compute_market.compute_reputation(w2.address)["score"])
    tid5 = _publish(node, creator, "图像超分 4x", 8.0, "ai_image", min_nodes=2)
    for w in (w5, w6):
        _apply(node, _signed_tx(w, "nova:compute:accept", task_id=tid5))
    r5 = "ee" * 32
    _apply(node, _signed_tx(w5, "nova:compute:submit", task_id=tid5, result_hash=r5))
    _apply(node, _signed_tx(w6, "nova:compute:submit", task_id=tid5, result_hash=r5))
    stake_before = node.store.compute_stakes[w5.address]
    t0 = time.time()
    hit = None
    for i in range(10000):
        d = time.strftime("%Y-%m-%d", time.localtime(t0 + i * 86400))
        if cm._audit_roll(tid5, d) < int(AUDIT_RATE * 100):
            hit = t0 + i * 86400
            break
    assert hit is not None
    assert cm._run_audits(hit) == 1
    auditor = node.store.compute_audits[tid5]["auditor"]
    auditor_w = next(w for w in (w1, w2, w3, w4) if w.address == auditor)
    _apply(node, _signed_tx(auditor_w, "nova:compute:audit",
                            task_id=tid5, result_hash="00" * 32))
    t5 = node.store.compute_tasks[tid5]
    print("[7] 5%% 随机抽查命中 → 审计节点 %s 判定错误 → audit_failed=%s，w5 质押 %.2f → %.2f NOVA（罚没双倍），信誉清零"
          % (auditor[:10], t5.get("audit_failed"), stake_before,
             node.store.compute_stakes.get(w5.address, 0.0)))
    assert node.compute_market.compute_reputation(w5.address)["score"] == 0.0

    # ---------- 8. 激励池 ----------
    node.balances[node.economy.VALIDATOR_POOL] = 1000.0
    res = cm.settle_incentive_epoch()
    print("[8] 激励池结算：算力 %s 节点 / 存储 %s 节点，共支出 %.2f NOVA（存储 40%% / 算力 60%%）"
          % (res["compute_nodes"], res["storage_nodes"], res["paid"]))
    for w in (w2, w3, w4, w5, w6):
        inc = cm.node_income(w.address)
        print("    - %s 收益统计：任务 %.2f + 加成 %.2f + 出块 %.2f + 审计 %.2f = %.2f NOVA"
              % (w.address[:10], inc["task_reward"], inc["rep_bonus"],
                 inc["block_reward"], inc["audit_reward"], inc["total"]))

    # ---------- 9. AI 服务 + 分账 ----------
    human, ai, fan = QuantumWallet(), QuantumWallet(), QuantumWallet()
    for w in (human, ai, fan):
        _fund(node, w.address)
    _apply(node, _signed_tx(ai, "nova:ai:register", name="Nova 音乐精灵",
                            owner=human.address, daily_budget=100.0))
    _apply(node, _signed_tx(ai, "nova:ai:svc:register", service_type="suno",
                            name="Suno 音乐生成", model="suno-v4",
                            endpoint_hash="sha256:suno-v4"))
    _apply(node, _signed_tx(ai, "nova:ai:muso:config", enabled=True, schedule="daily",
                            hour=10, weekday=0, budget=50.0))
    _apply(node, _signed_tx(ai, "nova:ai:work:create", title="星轨心跳",
                            cid="bafy" + "a" * 50, task_type="ai_music", meta="Suno v4"))
    wid = list(node.store.ai_works)[0]
    work = node.store.ai_works[wid]
    print("[9] AI 音乐人：服务登记 + 每日循环配置 + 作品「%s」自动定价 %.2f NOVA"
          % (work["title"], work["price"]))
    _apply(node, _signed_tx(fan, "nova:ai:work:buy", wid=wid, amount=work["price"]))
    fund = node.ai_service.fund_view()
    print("    购买分账：创作者 70%% / 算力 20%% / 基金 10%%；成长基金余额 %.2f NOVA（收入 %.2f / 支出 %.2f）"
          % (fund["balance"], fund["income_total"], fund["expense_total"]))

    print("=" * 68)
    print("演示完成：节点 %s / 任务 %s / 抽查 %s / 罚没 %.2f NOVA"
          % (len(node.store.compute_nodes), len(node.store.compute_tasks),
             len(node.store.compute_audits), node.store.compute_slashed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
