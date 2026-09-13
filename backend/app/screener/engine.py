"""G7 条件选股 · 多源扫描引擎（Phase 4 强化版）。

设计要点（D-J）：
- 引擎只依赖「能力端口 + Source Policy 解析」，代码内**不得出现任何 provider 名字**（§J.1）；
- 取数统一走 ``app.data.bars_provider.BarsProvider``（本地 canonical + 在线能力链）；
- 指标需求去重（§J.10）：同一 (指标, 参数) 在一次求值内只计算一次；
- 结果全链路溯源（§J.10）：provenance / degraded / fallback_tried / provider_policy_version；
- 无数据源且无本地数据 → 抛 RuntimeError，由路由层转 503（零 mock，绝不返回空列表冒充「无符合标的」）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from app.screener.conditions import evaluate
from datasource.local_store import LocalStore, get_store

log = logging.getLogger("qmt_work.screener.engine")

_SORT_KEYS = {"score", "change_pct", "close", "volume"}


def _stock_name_map(store: LocalStore) -> Dict[str, str]:
    return {r["code"]: r["name"] for r in store.get_stock_list()}


def _is_st(name: str) -> bool:
    n = (name or "").upper()
    return n.startswith("ST") or n.startswith("*ST") or " 退" in name or "退市" in name


def _prefilter_codes(codes: List[str], names: Dict[str, str],
                     prefilter: Optional[dict]) -> tuple[List[str], dict]:
    """过滤前置（修 P1-32）：在取数前用可本地判定的维度（ST/退市）削减扫描量。

    返回 (过滤后代码, prefilter 报告)。停牌/流动性等需在线数据的维度，返回是否尝试及结果，
    不强行在线（避免选股被单源失败击穿）。
    """
    meta: dict = {"applied": [], "excluded_st": 0, "excluded_suspended": 0}
    if not prefilter:
        return codes, meta
    out = list(codes)
    if prefilter.get("exclude_st"):
        before = len(out)
        out = [c for c in out if not _is_st(names.get(c, ""))]
        meta["excluded_st"] = before - len(out)
        meta["applied"].append("exclude_st")
    if prefilter.get("exclude_suspended") and isinstance(prefilter.get("suspended"), (set, list)):
        sus = set(prefilter["suspended"])
        before = len(out)
        out = [c for c in out if c not in sus]
        meta["excluded_suspended"] = before - len(out)
        meta["applied"].append("exclude_suspended")
    return out, meta


def evaluate_scan(
    codes: List[str],
    bars_map: Dict[str, list],
    conditions: dict,
    *,
    names: Optional[Dict[str, str]] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    prefilter: Optional[dict] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> tuple[List[dict], int, int]:
    """对给定代码集合做条件求值（同步、纯 CPU），返回 (results, scanned, elapsed_ms)。

    ``scanned`` = 已**实际评估**的标的数（含未命中）；命中数由 ``results`` / ``count``
    表达。两者语义不同，调用方（``total_scanned`` 字段）依赖前者。

    指标去重、价格区间过滤、涨跌额计算均在此完成；同源一致性由调用方（BarsProvider）保证。
    """
    t0 = time.perf_counter()
    names = names or {}
    results: List[dict] = []
    scanned = 0
    total = len(codes)
    # 价格区间前置（无需指标计算，先剔除明显不符者，省去后续计算）
    price_lo = min_price
    price_hi = max_price
    for idx, code in enumerate(codes):
        if progress_cb and idx % 50 == 0:
            progress_cb(idx, total)
        bars = bars_map.get(code) or []
        if not bars:
            continue
        # 价格区间前置过滤
        last = bars[-1]
        close = float(getattr(last, "close", None) or 0.0)
        if close <= 0:
            continue
        if price_lo is not None and close < price_lo:
            continue
        if price_hi is not None and close > price_hi:
            continue
        # total_scanned = **已实际评估**的标的数（含未命中）；命中数由 results/count 表达。
        # 旧实现把 scanned 仅在命中时自增，使 total_scanned 变成命中数（与字段名及
        # tests/test_screener.py 契约相反）。
        scanned += 1
        try:
            hit, score, tot = evaluate(conditions, bars)
        except ValueError as exc:
            raise ValueError(f"条件求值失败（{code}）：{exc}") from exc
        if not hit:
            continue
        pre = float(getattr(bars[-2], "close", None) or 0.0) if len(bars) > 1 else close
        change_pct = round((close / pre - 1.0) * 100.0, 2) if pre else 0.0
        results.append({
            "code": code,
            "name": names.get(code, ""),
            "close": round(close, 4),
            "change_pct": change_pct,
            "volume": float(getattr(last, "volume", None) or 0.0),
            "score": score,
            "total_conditions": tot,
        })
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return results, scanned, elapsed_ms


async def scan_async(
    store: Optional[LocalStore],
    conditions: dict,
    *,
    limit: int = 100,
    sort_by: str = "score",
    sort_desc: bool = True,
    adjust: str = "qfq",
    period: str = "1d",
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    max_codes: int = 0,
    source_policy: str = "auto",
    universe: Any = None,
    prefilter: Optional[dict] = None,
    fields: Optional[List[str]] = None,
    offline: bool = False,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """多源条件选股（异步编排：解析股票池 → 取数 → 求值 → 溯源）。

    异常（无数据源 / 空池）交由路由层转 503；绝不返回空列表冒充「无符合标的」。
    """
    from app.data.bars_provider import BarsProvider
    from app.screener.fundamentals import fetch_fundamentals
    from app.screener.universe import UniverseSpec, resolve_universe

    st = store or get_store()
    if sort_by not in _SORT_KEYS:
        raise ValueError(f"sort_by 非法：{sort_by}（可选 {sorted(_SORT_KEYS)}）")

    spec = UniverseSpec.parse(universe)
    uni = await resolve_universe(spec, policy_str=source_policy, store=st)
    codes = uni["codes"]
    if not codes:
        raise RuntimeError(
            f"选股股票池为空：{uni.get('degraded_reason') or 'universe_empty'}"
            "（请检查 universe 参数或先运行同步任务）")
    if max_codes and max_codes > 0:
        codes = codes[: max_codes]

    codes, pre_meta = _prefilter_codes(codes, uni["names"], prefilter)

    bp = BarsProvider(store=st)
    bars_map, report = await bp.get_bars_batch(
        codes, period=period, adjust=adjust, policy_str=source_policy, offline=offline)
    # 全空（既无在线源数据又无本地数据）→ 503 引导
    if report.provider_used == "local" and report.count_local == 0 and not offline:
        raise RuntimeError(
            "no_data_source_and_no_local_data：请先连接数据源或运行同步任务")

    results, scanned, elapsed_ms = evaluate_scan(
        codes, bars_map, conditions, names=uni["names"],
        min_price=min_price, max_price=max_price, prefilter=prefilter,
        progress_cb=progress_cb)

    results.sort(key=lambda r: r[sort_by], reverse=bool(sort_desc))
    if limit and limit > 0:
        results = results[: int(limit)]

    fund = None
    if fields and results:
        fund = await fetch_fundamentals(
            [r["code"] for r in results], policy_str=source_policy, fields=fields)

    provenance = report.to_provenance()
    provenance.update({
        "universe": spec.kind,
        "universe_provider": uni.get("provider_used"),
        "universe_degraded": uni.get("degraded", False),
        "prefilter": pre_meta,
        "screening_mode": "offline" if offline else "online",
    })
    return {
        "count": len(results),
        "total_scanned": scanned,
        "elapsed_ms": elapsed_ms,
        "sort_by": sort_by,
        "conditions": conditions,
        "results": results,
        "provenance": provenance,
        "degraded": report.degraded,
        "degraded_reason": report.degraded_reason,
        "fallback_tried": report.fallback_tried,
        "provider_policy_version": "chain.v1",
        "dataset_snapshot_id": None,
        "fundamentals": fund,
    }


# ---------------------------------------------------------------------------
# 兼容层：本地仓同步扫描（旧调用方 / 纯本地场景）
# ---------------------------------------------------------------------------
def scan(
    store: Optional[LocalStore],
    conditions: dict,
    *,
    limit: int = 100,
    sort_by: str = "score",
    sort_desc: bool = True,
    adjust: str = "qfq",
    period: str = "1d",
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    max_codes: int = 0,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """本地仓同步扫描（只走本地 canonical，等同 ``scan_async(offline=True)`` 的同步版）。

    保留旧签名以兼容既有调用；多源能力请使用 ``scan_async``。
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        # 已有事件循环：直接跑异步编排（offline），不阻塞。
        return loop.run_until_complete(
            scan_async(store, conditions, limit=limit, sort_by=sort_by,
                       sort_desc=sort_desc, adjust=adjust, period=period,
                       min_price=min_price, max_price=max_price,
                       max_codes=max_codes, source_policy="local_only",
                       offline=True, progress_cb=progress_cb))
    return asyncio.run(
        scan_async(store, conditions, limit=limit, sort_by=sort_by,
                   sort_desc=sort_desc, adjust=adjust, period=period,
                   min_price=min_price, max_price=max_price,
                   max_codes=max_codes, source_policy="local_only",
                   offline=True, progress_cb=progress_cb))


# ---------------------------------------------------------------------------
# 动态板块：选股结果存为板块（kind="screen:<name>"，复用 local_boards）
# ---------------------------------------------------------------------------
def save_as_board(
    store: Optional[LocalStore],
    name: str,
    conditions: dict,
    results: List[dict],
) -> dict:
    """把选股结果存为动态板块（本地，随下次同步自动刷新）。"""
    st = store or get_store()
    name = (name or "").strip()
    if not name:
        raise ValueError("板块名称不能为空")
    kind = f"screen:{name}"
    items = [{"code": r["code"], "name": r.get("name", ""),
              "last": r.get("close"), "change_pct": r.get("change_pct"),
              "amount": 0} for r in results]
    n = st.upsert_boards(kind, items)
    # 记录板块的选股条件（供回放/说明）
    st.set_meta(f"board_cond:{kind}", str(conditions))
    return {"kind": kind, "name": name, "count": n, "conditions": conditions}


def list_saved_boards(store: Optional[LocalStore] = None) -> List[dict]:
    """列出已保存的动态板块（kind 前缀 screen: 聚合，含成员数）。"""
    st = store or get_store()
    rows = st._db.query(
        "SELECT kind, COUNT(*) AS n FROM local_boards WHERE kind LIKE 'screen:%' "
        "GROUP BY kind ORDER BY kind")
    return [{"kind": r["kind"], "name": r["kind"][len("screen:"):],
             "count": r["n"]} for r in rows]


__all__ = ["scan", "scan_async", "evaluate_scan", "save_as_board", "list_saved_boards"]
