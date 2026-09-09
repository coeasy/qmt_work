"""兼容 shim：数据库单例已下沉至 core/db.py（P1-2 延续 / M1，2026-09-08）。

下沉原因：xtquant_client/manager.py 与 tools/analysis.py 需持久化数据，
若保留在 app 包会造成「核心适配器 → 应用装配层」的反向依赖。

新代码请直接 `from core.db import ...`；本文件仅为存量引用保留。
"""
from core.db import *  # noqa: F401,F403
from core.db import DB, _db, _RWLock, audit_chain_hash, get_db, init_db  # noqa: F401  # noqa: F401
