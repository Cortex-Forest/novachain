# Nova 链（抗量子版）

全球首个全抗量子创作者公链。采用 NIST 认证的 CRYSTALS-Dilithium5 签名算法。
8100 万 NOVA，零团队预留，全人类共有。

## 安装依赖
pip install oqs aiohttp pyopenssl

## 生成 TLS 证书
python cert_gen.py

## 本地测试（单机三节点，不使用 TLS）
python run_network.py

## 生产启动（启用 TLS）
python nova_node.py --host 0.0.0.0 --p2p 9000 --rpc 8080

## 生成抗量子钱包
python -c "from core.crypto import QuantumWallet; w = QuantumWallet(); print('地址:', w.address); print('私钥:', w.private_key_hex())"

## 安全特性
- CRYSTALS-Dilithium5 抗量子签名（NIST PQC 标准）
- TLS 1.3 加密通信
- RPC 频率限制（100次/秒/IP）
- 交易去重防重放
- 数据大小限制（100KB/交易，100KB/合约）
- 质押防女巫（最低100 NOVA）
- 签到防作弊（IP限制 + 设备指纹 + 20小时间隔）

## 经济参数
- 总量：8100万 NOVA（锁死，永不增发）
- 手续费：0.000001 NOVA/笔（固定，100%回流激励池）
- 出块奖励：0.5 NOVA起，每9个月减半，共9次，之后恒定 ~0.00097
- 质押：100-10000 NOVA，按比例分配奖励，7天冷静期

## 三层奖励
| 类型 | 初始奖励 | 减半条件 | 最低值 |
|------|---------|----------|--------|
| 部署合约 | 5 NOVA | 每5万合约 | 0.01 |
| 推荐奖励 | 1 NOVA | 每10万人 | 0.01 |
| 合约调用分红 | 0.1 NOVA | 每50万次 | 0.001 |
| 轻节点验证 | =出块奖励 | 同减半 | ~0.00097 |

## 早期激励
- 前81位超级节点矿工：注册即空投100 NOVA（锁定3年，之后逐月解锁10%）
- 前8100位轻节点签到者：首次签到即空投100 NOVA（锁定3年）
- 保持9个月在线：矿工额外1000 NOVA，轻节点额外100 NOVA
- 奖励发放时间：链上线满12个月

## 预售
- 仅收 USDT（BSC BEP-20）
- 9阶段阶梯定价：0.00001 → 0.001 (+9999%) → 每阶段×3 → 2.187
- 预售接收地址：0x6a5C3f17af93f690847208E68722afeaE7108bc5
- 必须先通过网页绑定 Nova 地址与 BSC 地址

## 文件结构