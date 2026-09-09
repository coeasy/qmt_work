"""兼容 shim：state 已下沉至 core.state（P1-2 / M1，2026-09-08）。

新代码请直接 `from core.state import ...`；本文件仅为存量引用保留。
"""
from core.state import MSG_NO_BROKER, MSG_NO_BROKER_EXC, AppState, state  # noqa: F401
