"""R28 数据集的只读仓储 —— 路由层不再出现裸 SQL。

``app/routes/datasets.py`` 最初把 9 张表的 ``SELECT`` 直接写在路由函数里，
被 ``scripts/check_execution_architecture.py`` 判为「route 层直连 DB」。这条门禁
不是形式主义：SQL 散在路由里意味着同一个数据集的**读法**会出现多份实现 ——
MCP 工具、导出脚本、报表将来各写一遍，表结构一改就得全局搜字符串。

本模块把两件事收在一处：

1. **每张表怎么读**（列什么、按什么排序、``code`` 怎么过滤）；
2. **每张表怎么统计**（行数 / 覆盖区间），且统计口径与读法同源。

★ 诚实约定：统计不到就返回全 0，**绝不**用配置值或上次结果填充 —— 界面上的
「本地 0 行」必须是真的 0 行，否则用户会以为数据已经下载好了。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

__all__ = [
    "count_table",
    "read_local",
]

#: 本模块认识的主库数据集表。``intraday`` 走独立库（``IntradayStore``），
#: 不在此列——它的生命周期与主库不同（全市场分钟线年增数亿行）。
#: 权威名单即 :data:`_COUNT_SQL` 的键集合（需列举时用 ``set(_COUNT_SQL)``）；
#: 曾另设 ``KNOWN_STORES`` 常量但既无人读、又漏了 ``intraday`` 之外的键
#: 一致性（两份名单必然漂移），按「无孤儿逻辑」原则移除。
_log = logging.getLogger("qmt_work.services.dataset_store")

#: 统计口径：store → (SQL, 参数)。``d0``/``d1`` 是覆盖区间端点，语义由表决定
#: （K 线是交易日区间，财务是报告期，参考数据是更新时间）。
_COUNT_SQL: Dict[str, Tuple[str, Tuple]] = {
    "local_bars": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT code) AS codes, "
        "MIN(dt) AS d0, MAX(dt) AS d1 FROM local_bars WHERE period=?",
        ("1d",),
    ),
    "local_ticks": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT code) AS codes, "
        "MIN(dt) AS d0, MAX(dt) AS d1 FROM local_ticks", (),
    ),
    "local_minutes": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT code) AS codes, "
        "MIN(dt) AS d0, MAX(dt) AS d1 FROM local_minutes", (),
    ),
    "local_moneyflow_hist": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT code) AS codes, "
        "MIN(dt) AS d0, MAX(dt) AS d1 FROM local_moneyflow_hist", (),
    ),
    "local_fundamentals": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT code) AS codes, "
        "'' AS d0, MAX(period) AS d1 FROM local_fundamentals", (),
    ),
    "local_stock_list": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT code) AS codes, "
        "'' AS d0, MAX(updated_at) AS d1 FROM local_stock_list", (),
    ),
    "local_boards": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT code) AS codes, "
        "'' AS d0, MAX(updated_at) AS d1 FROM local_boards", (),
    ),
    "local_board_members": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT code) AS codes, "
        "'' AS d0, MAX(updated_at) AS d1 FROM local_board_members", (),
    ),
    "local_capital": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT code) AS codes, "
        "MIN(dt) AS d0, MAX(dt) AS d1 FROM local_capital", (),
    ),
    # 主键是 (market, exchange, trade_date, session)，日期列叫 trade_date
    "exchange_calendar": (
        "SELECT COUNT(*) AS c, COUNT(DISTINCT trade_date) AS codes, "
        "MIN(trade_date) AS d0, MAX(trade_date) AS d1 FROM exchange_calendar", (),
    ),
}


def _empty() -> Dict[str, Any]:
    return {"rows": 0, "codes": 0, "first_dt": "", "last_dt": ""}


def count_table(store: str, period: str = "") -> Dict[str, Any]:
    """统计某张数据集表的真实行数 / 覆盖区间。

    ``store`` 不认识（或表尚未迁移、库被锁）时返回全 0 —— **不抛、不估算**。
    """
    from core.db import get_db

    entry = _COUNT_SQL.get(store)
    if entry is None:
        return _empty()
    sql, args = entry
    if store == "local_bars":
        args = (period or "1d",)
    try:
        rows = get_db().query(sql, args)
    except Exception:  # noqa: BLE001 表未迁移 / 库锁 → 按空处理，不让接口 500
        return _empty()
    r = rows[0] if rows else None
    if not r:
        return _empty()
    return {"rows": int(r["c"] or 0), "codes": int(r["codes"] or 0),
            "first_dt": str(r["d0"] or ""), "last_dt": str(r["d1"] or "")}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def read_local(spec: Any, code: str = "", limit: int = 200,
               start: str = "", end: str = "") -> List[Dict[str, Any]]:
    """按数据集 spec 读**本地已下载**的数据。

    分派顺序：分钟线独立库 → 日线（走 ``LocalStore`` 的对象模型）→ 各主库表。
    返回纯 dict 列表，路由层只负责装信封。
    """
    n = max(1, int(limit or 200))
    store = getattr(spec, "store", "")

    if store == "intraday":
        if not code:
            return []
        from datasource.intraday_store import get_intraday_store
        st = get_intraday_store()
        return st.get_bars(code, getattr(spec, "period", "1m"),
                           getattr(spec, "adjust", ""),
                           limit=n, start=start, end=end)

    if store == "local_bars":
        if not code:
            return []
        # 惰性取 store：只有真正读日线时才需要它，别让其它数据集跟着一起失败
        from datasource.local_store import get_store
        bars = get_store().get_bars(
            code, period=getattr(spec, "period", "1d"),
            adjust=getattr(spec, "adjust", ""),
            limit=n, start=start or None, end=end or None)
        return [b.model_dump() if hasattr(b, "model_dump") else dict(b) for b in bars]

    from core.db import get_db

    if store == "local_stock_list":
        rows = get_db().query(
            "SELECT code,name,category FROM local_stock_list "
            "ORDER BY code LIMIT ?", (n,))
        return [dict(r) for r in rows]

    if store == "local_boards":
        rows = get_db().query(
            "SELECT code,name,last,change_pct,amount FROM local_boards "
            "ORDER BY change_pct DESC LIMIT ?", (n,))
        return [dict(r) for r in rows]

    if store == "local_board_members":
        if code:
            rows = get_db().query(
                "SELECT board_code,board_name,code,name FROM local_board_members "
                "WHERE code=? LIMIT ?", (code, n))
        else:
            rows = get_db().query(
                "SELECT board_code,board_name,code,name FROM local_board_members "
                "LIMIT ?", (n,))
        return [dict(r) for r in rows]

    if store == "local_capital":
        rows = get_db().query(
            "SELECT code,dt,total_shares,float_shares FROM local_capital "
            "ORDER BY code LIMIT ?", (n,))
        return [dict(r) for r in rows]

    if store == "local_fundamentals":
        if not code:
            return []
        rows = get_db().query(
            "SELECT code,period,payload_json,provider_id FROM local_fundamentals "
            "WHERE code=? ORDER BY period DESC LIMIT ?", (code, n))
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            raw = d.pop("payload_json", None)
            try:
                d["payload"] = json.loads(raw or "{}")
            except Exception as exc:  # noqa: BLE001 坏 JSON 就整行不要 payload，不伪造
                from core.errors import swallow
                swallow(exc, why=f"财务 payload_json 解析失败（code={d.get('code')} "
                                 f"period={d.get('period')}）；该行不给 payload，不伪造字段",
                        logger=_log)
            out.append(d)
        return out

    if store == "local_ticks":
        if not code:
            return []
        rows = get_db().query(
            "SELECT dt,tm,price,volume,amount,bs_flag FROM local_ticks "
            "WHERE code=? ORDER BY dt DESC, seq ASC LIMIT ?", (code, n))
        return [dict(r) for r in rows]

    if store == "local_minutes":
        if not code:
            return []
        rows = get_db().query(
            "SELECT dt,tm,price,avg_price,volume,amount FROM local_minutes "
            "WHERE code=? ORDER BY dt DESC, tm ASC LIMIT ?", (code, n))
        return [dict(r) for r in rows]

    if store == "local_moneyflow_hist":
        sql = "SELECT code,dt,main_net,retail_net FROM local_moneyflow_hist"
        args: Tuple = ()
        if code:
            sql += " WHERE code=?"
            args = (code,)
        sql += " ORDER BY dt DESC LIMIT ?"
        rows = get_db().query(sql, args + (n,))
        return [dict(r) for r in rows]

    if store == "exchange_calendar":
        rows = get_db().query(
            "SELECT trade_date AS date, market, exchange, calendar_source AS source "
            "FROM exchange_calendar ORDER BY trade_date DESC LIMIT ?", (n,))
        return [dict(r) for r in rows]

    return []
