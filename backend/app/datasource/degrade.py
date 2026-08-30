"""qmt_work 降级策略（G1-6）：远程失败 → 本地数据仓兜底，**降级 ≠ 造假**。

铁律（与「零 mock」并列）：
- 远程源彻底不可用时，**允许**返回本地数据仓中的历史数据，但必须：
  1. ``stale=True``（显式标记陈旧）；
  2. ``as_of`` 数据截至时间（取本地最新一根 K 线日期）；
  3. ``source="local:sqlite"``（标明来源，不得冒充远程源）；
  4. ``warnings`` 携带降级原因，前端据此展示「数据截至 X，未取得最新行情」。
- 本地仓也无数据时返回 ``None``，由路由照常 ``503``——绝不伪造「最新」。

试点：``/market/kline`` 已在远程无源分支接入本地兜底；其余端点后续子批次按同
模式接入（G1-6 全端点）。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from app.datasource.local_store import LocalStore, get_store
from app.datasource.result import DataResult

log = logging.getLogger("qmt_work.datasource.degrade")

_LOCAL_SOURCE = "local:sqlite"


def local_bars(
    code: str,
    period: str = "1d",
    adjust: str = "",
    store: Optional[LocalStore] = None,
    limit: int = 500,
) -> Optional[DataResult]:
    """本地 K 线兜底：有数据 → 标 stale 的 ``DataResult``；无数据 → None。

    ``as_of`` 取本地最近一根 K 线日期（数据真实截至时间，非当前时间——避免把
    陈旧数据伪装成"刚刚取到"）。
    """
    st = store or get_store()
    bars = st.get_bars(code, period=period, adjust=adjust, limit=limit)
    if not bars:
        return None
    as_of = st.latest_dt(code, period=period, adjust=adjust)
    return DataResult.from_source(
        [b.model_dump() for b in bars],
        source=_LOCAL_SOURCE,
        stale=True,
        as_of=as_of,
        warnings=[f"远程行情源不可用，返回本地数据仓数据（截至 {as_of}）。"],
    )


def local_stock_list(store: Optional[LocalStore] = None) -> Optional[DataResult]:
    """本地股票列表兜底（同上）。as_of 优先取最近同步时间，无则用行级落库时间。"""
    st = store or get_store()
    items = st.get_stock_list()
    if not items:
        return None
    as_of = st.get_meta("last_sync_at") or st.last_updated("local_stock_list")
    return DataResult.from_source(
        items,
        source=_LOCAL_SOURCE,
        stale=True,
        as_of=as_of,
        warnings=[f"远程行情源不可用，返回本地股票列表（同步于 {as_of or '未知'}）。"],
    )


def local_boards(kind: str, store: Optional[LocalStore] = None) -> Optional[DataResult]:
    """本地板块榜兜底（同上）。"""
    st = store or get_store()
    items = st.get_boards(kind)
    if not items:
        return None
    as_of = st.get_meta("last_sync_at") or st.last_updated("local_boards", "kind=?", (kind,))
    return DataResult.from_source(
        items,
        source=_LOCAL_SOURCE,
        stale=True,
        as_of=as_of,
        warnings=[f"远程行情源不可用，返回本地板块数据（同步于 {as_of or '未知'}）。"],
    )


def envelope(data: dict, dres: DataResult) -> dict:
    """把降级元数据并入 ok 信封：统一 ``stale`` / ``source`` / ``as_of`` / ``warning``。

    ``warning`` 取第一条 warnings（前端单行提示），完整列表保留在 ``warnings``。
    """
    out = dict(data)
    out["stale"] = dres.stale
    out["source"] = dres.source
    out["as_of"] = dres.as_of
    if dres.warnings:
        out["warning"] = dres.warnings[0]
        out["warnings"] = list(dres.warnings)
    return out


__all__ = ["local_bars", "local_stock_list", "local_boards", "envelope"]
