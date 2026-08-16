# -*- coding: utf-8 -*-
"""Nova 链区块链浏览器索引器包。

独立服务：
- 从 Nova 节点增量拉取区块/交易/合约，解析后写入 SQLite 或 PostgreSQL；
- 提供 REST + GraphQL 查询接口（常用查询 1 分钟缓存）；
- 支持按地址、交易 ID、合约地址、区块高度搜索。
"""

__version__ = "1.0.0"
