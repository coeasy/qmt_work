"""兼容 shim：StrategyRuntime 已迁移至 engines/strategy_runtime.py（P1-6 / M8，2026-09-08）。

新代码请直接 `from engines.strategy_runtime import ...`；本文件仅为存量引用保留。
"""
from engines.strategy_runtime import *  # noqa: F401,F403
from engines.strategy_runtime import (  # noqa: F401
    StrategyRuntime,  # noqa: F401
    _ema,
    _last_signal_for,
    _ma_signal,
    _macd_signal,
    _rsi,
    _rsi_signal,
)
