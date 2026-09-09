"""G7 条件选股 · 本地仓全市场向量化扫描引擎 + 动态板块。

数据源：**本地数据仓**（G1-4，LocalStore）——全市场扫描不走逐股远程接口，
每标的一次性拉本地 K 线（含复权维度）后经统一指标引擎（G2）计算、条件求值。
本地仓为空 → 明确引导先运行同步任务（零 mock，绝不返回假数据）。
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
    progress_cb: Optional[Callable[[int, int], None]] = None,   # (done, total) 每 50 只回调
) -> Dict[str, Any]:
    """全市场条件扫描（本地仓）。

    返回 {count, total_scanned, elapsed_ms, sort_by, conditions, results}；
    results 每项 {code, name, close, change_pct, volume, score, total_conditions}。
    """
    st = store or get_store()
    t0 = time.perf_counter()
    if sort_by not in _SORT_KEYS:
        raise ValueError(f"sort_by 非法：{sort_by}（可选 {sorted(_SORT_KEYS)}）")

    codes = [r["code"] for r in st.get_stock_list()]
    if not codes:
        raise RuntimeError("本地数据仓为空：请先运行同步任务（python -m app.sync.bars）再选股")
    if max_codes and max_codes > 0:
        codes = codes[: max_codes]
    names = _stock_name_map(st)

    results: List[dict] = []
    scanned = 0
    total = len(codes)
    for idx, code in enumerate(codes):
        if progress_cb and idx % 50 == 0:
            progress_cb(idx, total)
        bars = st.get_bars(code, period=period, adjust=adjust, limit=250)
        if not bars:
            continue
        scanned += 1
        try:
            hit, score, total = evaluate(conditions, bars)
        except ValueError as exc:
            raise ValueError(f"条件求值失败（{code}）：{exc}") from exc
        if not hit:
            continue
        last = bars[-1]
        close = float(last.close)
        if min_price is not None and close < min_price:
            continue
        if max_price is not None and close > max_price:
            continue
        pre = float(bars[-2].close) if len(bars) > 1 else close
        change_pct = round((close / pre - 1.0) * 100.0, 2) if pre else 0.0
        results.append({
            "code": code,
            "name": names.get(code, ""),
            "close": round(close, 4),
            "change_pct": change_pct,
            "volume": float(last.volume or 0.0),
            "score": score,
            "total_conditions": total,
        })

    results.sort(
        key=lambda r: r[sort_by],
        reverse=bool(sort_desc),
    )
    if limit and limit > 0:
        results = results[: int(limit)]
    return {
        "count": len(results),
        "total_scanned": scanned,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "sort_by": sort_by,
        "conditions": conditions,
        "results": results,
    }


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


__all__ = ["scan", "save_as_board", "list_saved_boards"]
