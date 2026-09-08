"""兼容 shim：迁移账本已下沉至 core/db_migrations.py（P1-2 延续 / M1，2026-09-08）。

新代码请直接 `from core.db_migrations import ...`；本文件仅为存量引用保留。
"""
from core.db_migrations import EXTRA_COLUMNS, MIGRATIONS  # noqa: F401
