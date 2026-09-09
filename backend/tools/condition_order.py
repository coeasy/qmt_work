"""兼容 shim：ConditionOrderEngine 已迁移至 engines/condition_order.py（P1-6 / M8，2026-09-08）。

新代码请直接 `from engines.condition_order import ...`；本文件仅为存量引用保留。
"""
from engines.condition_order import *  # noqa: F401,F403
from engines.condition_order import (  # noqa: F401
    ConditionOrderEngine,
    _engine,  # noqa: F401
    register_condition_tools,
)
