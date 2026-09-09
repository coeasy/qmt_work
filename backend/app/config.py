"""兼容 shim：配置已下沉至 core.config（P1-2 / M1，2026-09-08）。

新代码请直接 `from core.config import ...`；本文件仅为存量引用保留。
"""
from core.config import (  # noqa: F401  # noqa: F401
    BASE_DIR,
    Settings,
    config_file,
    ensure_config_file,
    exe_dir,
    settings,
)
