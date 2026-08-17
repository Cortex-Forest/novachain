# IN_SCOPE_COVERAGE_MATRIX.md — Phase 2C

| 文件 | 模块/职责 | 外部入口 | 关键状态 | 依赖 | Phase3 轮次 | Phase6 轮次 | PoC/战役 | 最终状态 |
|------|-----------|----------|----------|------|-------------|-------------|----------|----------|
| nova_node.py | 节点核心：validate/apply/RPC | 全部 /api/* | balances, dag, stakes, 全模块 store | core/* network/* | P1-P10 | D1-D20 | poc_bridge_dict | covered_no_issue（桥故障另有 finding） |
| core/transaction.py | Tx/金额规范化 | — | txid, signing_data | — | P11 | D21 | — | covered_no_issue（float 已知 TM-012） |
| core/blockchain.py | Block | — | block.hash | — | P11 | D21 | — | covered_no_issue |
| core/crypto.py | Ed25519/Dilithium/P-256 ECIES | verify_quantum_tx | — | oqs(可选)/cryptography | P12 | D22-D23 | — | covered_no_issue（s<L 已知 TM-011） |
| core/consensus.py | PoS/checkpoint | adopt_block/elect | chain, epoch_stakes, pos_missed | crypto/blockchain | P13-P15 | D24-D26 | — | known_or_duplicate（TM-003 时间戳） |
| core/economy.py | 经济参数/奖励/空投 | block_reward/early_airdrop/check_unlock | balances, locked | — | P16 | D27 | — | covered_no_issue |
| core/vm.py | NexusVM | run | storage, events | — | P17 | D28 | — | covered_no_issue（SEND 未接账本=功能缺口） |
| nexlang_compiler.py | NexLang 编译器 | compile | bytecode | — | P17 | D28 | — | covered_no_issue |
| core/storage.py | StateStore | snapshot/restore | 全状态 | — | P18 | D29 | — | covered_no_issue |
| core/storage_network.py | 存储网络 pin/claim/proof/order | RPC 包装 | storage_claims/seals | economy | P19-P20 | D30-D33 | poc_storage | phase8_finding F-02 |
| core/storage_incentive.py | 存储激励 | RPC 包装 | inc_files/inc_nodes | economy | P21-P22 | D34-D37 | poc_storage | phase8_finding F-02 |
| core/compute.py | 算力市场 | RPC 包装 | compute_tasks | economy | P23 | D38-D40 | — | covered_no_issue |
| core/ai_service.py | AI 服务/基金 | RPC 包装 | ai_works, ai_fund | compute/socialfi | P24 | D41-D42 | — | covered_no_issue |
| core/socialfi.py | SocialFi 10 类 | rpc_socialfi_action | fan_tokens, markets, text_assets, bonds | storage_network | P25-P28 | D43-D50 | — | covered_no_issue（market oracle 自设=设计风险） |
| core/arbitration.py | 社区仲裁 | RPC 包装 | arb_cases | socialfi | P29-P30 | D51-D55 | — | covered_no_issue |
| core/chat.py | E2E 信箱 | rpc_chat_* | inbox, pubkeys | — | P31 | D56 | — | known_or_duplicate（TM-008 读取无鉴权） |
| core/oracle.py | VRF/价格/AI 验证 | rpc_oracle_* | oracle_feeds/sources | economy | P32-P34 | D57-D60 | poc_oracle | phase8_finding F-03/F-04 |
| core/bridge.py | 跨链桥 | rpc_bridge_* | bridge_nodes/assets/deposits | oracle | P35-P37 | D61-D65 | poc_bridge | phase8_finding F-01/F-05 |
| core/dex.py | DEX AMM | rpc_dex_* | dex_pairs/lp/farm | bridge | P38 | D66-D68 | — | covered_no_issue |
| core/governance.py | 治理 | rpc_gov_* | gov_proposals/delegations | bridge/dex | P39-P40 | D69-D72 | poc_gov | phase8_finding F-06 |
| core/did.py | DID/声誉 | rpc_did_* | did_profiles/reputation | — | P41 | D73 | — | covered_no_issue |
| core/subscription.py | 订阅 | rpc_sub_* | sub_creators/subscriptions | — | P42 | D74 | — | covered_no_issue |
| network/rpc.py | 路由/CORS | — | — | — | P43 | D75 | — | known_or_duplicate（TM-009 CORS） |
| network/p2p.py | P2P 分帧/快照 | — | connections/peers | — | P44 | D76 | — | covered_no_issue（P0-2 已修复；TLS 已知 TM-013） |
| network/security.py | 限流/重放/防作弊 | — | request_log, processed_txids | — | P45 | D77 | — | covered_no_issue（P1-8 部分修复） |
| agent/*.py | Agent 运行时 | gateway/guardrail | — | core | P46 | D78-D80 | — | covered_no_issue |
| explorer/*.py | 索引器/GraphQL | /graphql /api/* | db | — | P47 | D81-D82 | — | covered_no_issue（只读） |
| scripts/storage_monitor.py 等 | 运维脚本 | — | — | — | P48 | D83 | — | covered_no_issue |
| run_network.py / run_local_node.py / sign_tx.py / cert_gen.py | 启动/签名/证书 | — | — | — | P49 | D84 | — | covered_no_issue |
| genesis.json / requirements.txt / pytest.ini | 配置 | — | — | — | P50 | D85 | — | covered_no_issue（创世池为 0 备注） |

（Phase 6 余下 D86-D100 覆盖跨模块交互、经济极端、不变量回归，见 06-deep-dive 文件。）
