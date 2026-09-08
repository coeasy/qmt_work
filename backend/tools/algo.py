"""兼容 shim：AlgoEngine 已迁移至 engines/algo.py（P1-6 / M8，2026-09-08）。

新代码请直接 `from engines.algo import ...`；本文件仅为存量引用保留。
"""
from engines.algo import *  # noqa: F401,F403
from engines.algo import AlgoEngine, register_algo_tools  # noqa: F401
from engines.algo import _engine  # noqa: F401  (私有 API，测试与内部调用兼容)
