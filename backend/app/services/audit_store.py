"""审计日志的只读仓储。

``app/routes/audit.py`` 曾直接拼 ``SELECT ... FROM audit_log``（含列清单常量与
两条按 ``action`` 分叉的语句）。列清单是**安全敏感**的：审计表里有
``params_json``（已脱敏）与 ``prev_hash`` / ``hash``（hash 链），
一旦某个调用点用 ``SELECT *`` 就会把将来的新增敏感列一并带出去。
把列清单收敛到这里，是「哪些列可以出网」的唯一出处。

写路径不在这里 —— 审计的写入统一走 ``DB.audit()``（它负责 hash 链衔接），
本模块**只读**。
"""
from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["TABLE", "LIST_COLUMNS", "list_entries"]

TABLE = "audit_log"

#: 允许出网的列（唯一出处）。刻意不含任何后续新增列，避免 `SELECT *` 漂移。
LIST_COLUMNS = (
    "id, actor, action, target, params_json, result, ip, created_at, "
    "prev_hash, hash"
)


def list_entries(db: Any, action: str, limit: int) -> List[Dict[str, Any]]:
    """按 id 倒序取审计记录；``action`` 非空时只取该动作。"""
    if action:
        return db.query(
            f"SELECT {LIST_COLUMNS} FROM {TABLE} WHERE action=? ORDER BY id DESC LIMIT ?",
            (action, limit),
        )
    return db.query(
        f"SELECT {LIST_COLUMNS} FROM {TABLE} ORDER BY id DESC LIMIT ?", (limit,)
    )
