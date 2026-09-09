"""兼容 shim：LimitUpMonitor 已迁移至 engines/limitup.py（P1-6 / M8，2026-09-08）。

新代码请直接 `from engines.limitup import ...`；本文件仅为存量引用保留。
"""
from engines.limitup import *  # noqa: F401,F403
from engines.limitup import (  # noqa: F401  # noqa: F401
    _INDEXES,
    _LIMIT_SEEN_MAX,
    _LIMIT_SEEN_TTL,
    LimitUpMonitor,
    _board_of,
    _limit_factor,
    _prune_limit_first_seen,
    register_limitup_tools,
)
