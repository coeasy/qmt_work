"""账户净值快照（``account_snapshot``）的只读仓储。

``app/routes/account.py::account_pnl`` 曾直接拼三条 SQL（按账户过滤 / 全量 /
取账户清单）。这里把「账户隔离」这条不变量收进一个地方。

★ 为什么值得单独成层：净值曲线是**多账户混算**最容易出错的地方 —— 无条件
``SELECT`` 全表时，曲线形状完全失真而界面毫无提示，用户会以为自己的策略
亏了/赚了。把查询收在这里，将来任何新调用点（MCP 工具、导出、报表）都自动
继承「必须显式声明账户范围」的语义。
"""
from __future__ import annotations

from typing import Any, Dict, List

__all__ = ["TABLE", "pnl_series", "list_account_ids"]

TABLE = "account_snapshot"


def pnl_series(db: Any, account_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    """取最近的净值点（``ts`` + ``net_value``），按 ts 倒序。

    ``account_id`` 为空表示**跨账户**取数；调用方必须据此向界面声明
    ``mixed_accounts``，不得把多账户曲线当成单账户呈现。
    """
    if account_id:
        return db.query(
            f"SELECT ts, net_value FROM {TABLE} WHERE account_id=? "
            "ORDER BY ts DESC LIMIT ?",
            (account_id, limit),
        )
    return db.query(
        f"SELECT ts, net_value FROM {TABLE} ORDER BY ts DESC LIMIT ?", (limit,)
    )


def list_account_ids(db: Any) -> List[str]:
    """列出快照表里出现过的全部账户 id（去重、已剔除空值）。"""
    rows = db.query(f"SELECT DISTINCT account_id FROM {TABLE}")
    return sorted({r["account_id"] for r in rows if r.get("account_id")})
