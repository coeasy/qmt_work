"""API Key 的持久化仓储。

与 ``alerts_store`` 同因：``app/routes/apikeys.py`` 曾直接拼 SQL（9 处，含
``f"UPDATE api_keys SET {','.join(fields)} WHERE id=?"`` 与
``f"DELETE FROM api_keys WHERE id IN ({place})"`` 两类动态串）。
收敛到这里后，路由只做参数校验、编排与审计。

★ 列表查询**刻意只取需要的列**并带 ``substr(key_hash,1,8) AS key_prefix`` ——
``key_hash`` 是密钥的 SHA256，**绝不能整列返回**（即使前端只显示前 8 位，
整串进了响应体就等于把可离线爆破的材料发出去）。这条不变量由本模块唯一承担。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
    "TABLE",
    "LIST_COLUMNS",
    "list_keys",
    "get_id",
    "insert_key",
    "update_fields",
    "set_key_hash",
    "delete_key",
    "delete_keys",
    "find_stale_ids",
]

TABLE = "api_keys"

#: 列表返回的列（唯一出处）。刻意不含完整 ``key_hash``，只给 8 位前缀。
LIST_COLUMNS = (
    "id, name, scopes, rate_limit, status, created_at, "
    "ip_allow, expires_at, grace_until, last_used_at, use_count, "
    "substr(key_hash,1,8) AS key_prefix"
)

#: ``PATCH /api-keys/{kid}`` 允许更新的字段白名单。
#: ★ 白名单在**仓储层**再声明一次：路由已经过滤过一遍，但把「哪些列可改」
#: 放在 SQL 旁边，才不会被将来的新调用点绕过（例如 MCP 工具直接调本模块）。
UPDATABLE_FIELDS = ("name", "scopes", "rate_limit", "status", "ip_allow", "expires_at")


def list_keys(db: Any) -> List[Dict[str, Any]]:
    """列出全部密钥（不含完整 key_hash）。"""
    return db.query(f"SELECT {LIST_COLUMNS} FROM {TABLE} ORDER BY id")


def get_id(db: Any, kid: int) -> Optional[Dict[str, Any]]:
    """按键取 ``{"id": ...}``，不存在返回 None（调用方据此返回 404）。"""
    return db.query_one(f"SELECT id FROM {TABLE} WHERE id=?", (kid,))


def insert_key(db: Any, payload: Dict[str, Any]) -> int:
    """写入一条密钥记录，返回 id。"""
    return int(db.insert(TABLE, payload))


def update_fields(db: Any, kid: int, fields: Dict[str, Any]) -> int:
    """按字段字典更新（键须取自 ``UPDATABLE_FIELDS``），返回受影响行数语义上的 id。

    ★ 字段列表由 ``fields`` 的键程序化生成；空字典直接返回 0，不发出无 ``SET`` 的语句。
    """
    if not fields:
        return 0
    sets = [f"{k}=?" for k in fields]
    db.execute(
        f"UPDATE {TABLE} SET {','.join(sets)} WHERE id=?",
        (*fields.values(), kid),
    )
    return kid


def set_key_hash(db: Any, kid: int, key_hash: str, grace_until: str,
                 created_at: str) -> None:
    """轮换密钥：写入新 hash + 宽限标记 + 创建时间。"""
    db.execute(
        f"UPDATE {TABLE} SET key_hash=?, grace_until=?, created_at=? WHERE id=?",
        (key_hash, grace_until, created_at, kid),
    )


def delete_key(db: Any, kid: int) -> None:
    """删除单条密钥（幂等）。"""
    db.execute(f"DELETE FROM {TABLE} WHERE id=?", (kid,))


def delete_keys(db: Any, ids: List[int]) -> int:
    """批量删除，返回实际下发的 id 数。``ids`` 为空时不发语句。"""
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    db.execute(f"DELETE FROM {TABLE} WHERE id IN ({placeholders})", tuple(ids))
    return len(ids)


def find_stale_ids(db: Any, cutoff: str) -> List[int]:
    """找出「可清理」的密钥 id（``status='active'`` 且已超过 cutoff）。

    ★ 「从未使用」这一支**必须同时满足 ``created_at < cutoff``**。
    旧写法 ``(last_used_at < ? OR last_used_at = '' OR last_used_at IS NULL)``
    少了创建时间条件 ⇒ 一个刚创建、还没被用过的密钥会在**第一次清理时就被删掉**，
    与 docstring 声明的保留条件（「从未使用且 created_at < cutoff」）相反。
    典型后果：新建密钥 → 忘了配到客户端 → 跑一次清理 → 密钥消失且无痕迹可查。
    """
    rows = db.query(
        f"SELECT id, last_used_at, created_at FROM {TABLE} "
        "WHERE status='active' AND ("
        "  (last_used_at IS NOT NULL AND last_used_at <> '' AND last_used_at < ?)"
        "  OR ((last_used_at IS NULL OR last_used_at = '') AND created_at < ?)"
        ")",
        (cutoff, cutoff),
    )
    return [r["id"] for r in rows]
