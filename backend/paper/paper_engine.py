"""兼容 shim：PaperEngine 已迁移至 engines/paper_engine.py（P1-6 / M8，2026-09-08）。

新代码请直接 `from engines.paper_engine import ...`；本文件仅为存量引用保留。
"""
from engines.paper_engine import *  # noqa: F401,F403
from engines.paper_engine import PaperEngine  # noqa: F401
