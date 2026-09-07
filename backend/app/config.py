"""兼容 shim：配置已下沉至 core.config（P1-2 / M1，2026-09-08）。

新代码请直接 `from core.config import ...`；本文件仅为存量引用保留。
"""
from core.config import BASE_DIR, Settings, ensure_config_file, exe_dir  # noqa: F401
from core.config import config_file, settings  # noqa: F401
