# BUILD_AND_TOOLCHAIN.md — Phase 1

## 环境
- Python: 3.14.6 (C:\Users\Administrator\AppData\Local\Programs\Python\Python314)
- 测试环境: `.venv-test`（pytest 9.1.1, aiohttp 3.14.3）
- oqs（CRYSTALS-Dilithium5）未安装 → 运行时回退 Ed25519（README 已声明，/api/status 如实返回）
- 无编译步骤；语言：Python（asyncio/aiohttp），前端为静态 JS（范围外）

## 构建/测试命令
`.\\.venv-test\\Scripts\\python.exe -m pytest -q`

## 结果
- 261 passed, 5 warnings, 16.32s
- 警告：5 × aiohttp NotAppKeyWarning（`explorer/server.py:85-86` 使用字符串键）— 非安全问题
- 编译器：`nexlang_compiler.py`（Python 实现，无外部依赖）

## 无编译器警告可记录；构建门通过。
