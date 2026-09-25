"""回测作业记录的只读仓储。

``app/routes/backtest.py`` 曾直接拼 ``SELECT * FROM backtest_jobs``。
注意本模块**只是记录层**：作业的**调度真源**是 ``ctx.backtest_queue``（内存队列），
这里的表是「重启后仍能查到历史作业」的持久化副本。

★ 因此 ``get_job`` 的调用方必须先问队列、再回落到本表 —— 顺序反了会把
「队列里正在跑、但还没落库」的作业报成 404。路由层保持这个顺序。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["TABLE", "list_jobs", "get_job"]

TABLE = "backtest_jobs"


def list_jobs(db: Any, limit: int = 50) -> List[Dict[str, Any]]:
    """按创建时间倒序取最近的作业记录。"""
    return db.query(
        f"SELECT * FROM {TABLE} ORDER BY created_at DESC LIMIT ?", (limit,)
    )


def get_job(db: Any, job_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取作业记录，不存在返回 None。"""
    return db.query_one(f"SELECT * FROM {TABLE} WHERE id=?", (job_id,))
