"""告警规则 / 历史的持久化仓储。

为什么单独成层
--------------
``app/routes/alerts.py`` 曾直接拼 SQL（``ctx.db.query`` / ``ctx.db.execute``），
包括 ``f"UPDATE alert_rules SET {','.join(fields)} WHERE id=?"`` 这类**动态拼串**。
路由层的职责是「参数校验 + 编排」，SQL 混在里面有三个具体代价：

1. **无法单测**：要验一条 SQL 的正确性，必须先把整个 FastAPI 路由与上下文搭起来；
2. **无门禁**：拼串写错（漏 ``WHERE``、字段名漂移）不会有任何静态检查发现；
3. **职责漂移**：同一张表的读写散落在路由与引擎里，改表结构要全局 grep。

因此把 SQL 收敛到这里，路由只调函数。门禁
``scripts/check_execution_architecture.py`` 会扫描 ``app/routes/`` 下是否
再出现裸 SQL（``ctx.db.query`` / ``execute`` / ``executemany`` / ``query_one``）。

★ 注意 ``ctx.db.insert`` / ``ctx.db.upsert`` / ``ctx.db.audit`` **不在门禁范围内** ——
它们接收的是「表名 + 字典」而非 SQL 字符串，属于结构化仓储 API，不是裸 SQL。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
    "list_rules",
    "save_rule",
    "delete_rule",
    "delete_rules",
    "list_history",
]

#: 告警规则表名（唯一出处，避免各调用点写死字符串后漂移）
RULE_TABLE = "alert_rules"
#: 告警触发历史表名
HISTORY_TABLE = "alerts_history"


def list_rules(db: Any) -> List[Dict[str, Any]]:
    """按 id 升序列出全部告警规则。"""
    return db.query(f"SELECT * FROM {RULE_TABLE} ORDER BY id")


def save_rule(db: Any, payload: Dict[str, Any], rule_id: Optional[int] = None) -> int:
    """新增或更新一条告警规则，返回规则 id。

    ``rule_id`` 为 None 时走 ``insert``；否则按 ``payload`` 的键整体更新。
    ★ 更新分支的字段列表由 ``payload`` 的键程序化生成，不手工拼写 ——
    手工列表在「payload 新增一个字段」时必然漂移（新增字段静默不落库）。
    """
    if rule_id:
        fields = [f"{k}=?" for k in payload]
        db.execute(
            f"UPDATE {RULE_TABLE} SET {','.join(fields)} WHERE id=?",
            (*payload.values(), int(rule_id)),
        )
        return int(rule_id)
    return int(db.insert(RULE_TABLE, payload))


def delete_rule(db: Any, rule_id: int) -> None:
    """删除单条告警规则（幂等：不存在时不报错）。"""
    db.execute(f"DELETE FROM {RULE_TABLE} WHERE id=?", (rule_id,))


def delete_rules(db: Any, ids: List[int]) -> None:
    """批量删除告警规则。

    ★ 占位符按 ``ids`` 的长度程序化生成；``ids`` 为空时**直接返回**，
    否则会拼出 ``IN ()`` 这种语法错误（调用方已校验非空，这里是防御）。
    """
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    db.execute(f"DELETE FROM {RULE_TABLE} WHERE id IN ({placeholders})", tuple(ids))


def list_history(db: Any, limit: int) -> List[Dict[str, Any]]:
    """按 id 倒序取最近的告警触发历史。"""
    return db.query(f"SELECT * FROM {HISTORY_TABLE} ORDER BY id DESC LIMIT ?", (limit,))
