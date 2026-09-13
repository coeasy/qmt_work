"""V9 Phase 7：数据质量 —— 跨源对账（reconcile）与 Data Finality。

- ``reconcile_bars``：对同一 (code, dt) 的多 provider 行做价差对账。
  价差超阈值 → 全部相关 raw 行标记 ``conflict``（**保留全部 raw，不删不覆盖**），
  canonical 归属按 provider 质量序（QMT > eltdx > baostock > akshare > 其他）。
- Data Finality 终态：``provisional / final / revised / invalid``（落在
  dataset_snapshots.quality_state 上；``partial/complete`` 等中间态由同步路径管理）。
"""
from __future__ import annotations

import logging

log = logging.getLogger("qmt_work.datasource.quality")

#: provider 质量序（越小越优）——与默认数据链 QMT→eltdx→baostock→akshare 对齐
PROVIDER_QUALITY_RANK: dict[str, int] = {
    "broker": 0, "qmt": 0, "eltdx": 10, "baostock": 20, "akshare": 30,
}

#: Data Finality 终态（快照可对外承诺的状态）
FINALITY_STATES: tuple[str, ...] = ("provisional", "final", "revised", "invalid")


def _rank(provider_id: str) -> int:
    return PROVIDER_QUALITY_RANK.get((provider_id or "").lower(), 999)


def _cutoff_ymd(lookback_days: int) -> str:
    """Python 侧计算 YYYYMMDD 边界（不依赖 SQLite strftime 修饰符顺序差异）。"""
    from datetime import date, timedelta
    return (date.today() - timedelta(days=int(lookback_days))).strftime("%Y%m%d")


def reconcile_bars(db, *, period: str = "1d", adjust: str = "qfq",
                   lookback_days: int = 10,
                   price_diff_pct: float = 0.005,
                   codes: list[str] | None = None) -> dict:
    """跨源 K 线对账。

    对最近 lookback_days 内同一 (code, dt) 存在 >=2 个 provider 的行：
    - close 价差（相对最优源）> price_diff_pct → 全组标 ``conflict``；
    - 否则把最优源行标 ``final``（canonical），其余标 ``reconciled``（保留）。
    返回统计 {checked, groups, conflicts, canonical_set, providers}。
    """
    rows = db.query(
        "SELECT code, dt, provider_id, close, quality_state FROM local_bars "
        "WHERE period=? AND adjust=? AND dt >= ? "
        "ORDER BY code, dt",
        (period, adjust, _cutoff_ymd(lookback_days)))
    if codes:
        want = set(codes)
        rows = [r for r in rows if r["code"] in want]

    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["code"], r["dt"]), []).append(r)

    stats = {"checked": len(rows), "groups": len(groups), "conflicts": 0,
             "canonical_set": 0, "providers": sorted({(r.get("provider_id") or "")
                                                       for r in rows})}
    for (_code, _dt), grp in groups.items():
        if len(grp) < 2:
            continue
        best = min(grp, key=lambda r: (_rank(r.get("provider_id") or ""),
                                        -(r.get("close") or 0)))
        best_close = best.get("close")
        conflict = False
        for r in grp:
            if r is best or best_close in (None, 0):
                continue
            c = r.get("close")
            if c is None or best_close in (None, 0):
                continue
            if abs(float(c) - float(best_close)) / abs(float(best_close)) > price_diff_pct:
                conflict = True
                break
        if conflict:
            stats["conflicts"] += 1
            for r in grp:
                if r.get("quality_state") != "conflict":
                    db.execute("UPDATE local_bars SET quality_state='conflict' "
                               "WHERE code=? AND dt=? AND provider_id=? AND period=? AND adjust=?",
                               (r["code"], r["dt"], r.get("provider_id") or "",
                                period, adjust))
        else:
            stats["canonical_set"] += 1
            if best.get("quality_state") != "final":
                db.execute("UPDATE local_bars SET quality_state='final' "
                           "WHERE code=? AND dt=? AND provider_id=? AND period=? AND adjust=?",
                           (best["code"], best["dt"], best.get("provider_id") or "",
                            period, adjust))
    return stats


def apply_finality(db, snapshot_id: str, finality: str) -> dict:
    """给 dataset snapshot 打终态。非法终态抛 ValueError（绝不静默）。"""
    if finality not in FINALITY_STATES:
        raise ValueError(f"invalid finality: {finality}（合法：{FINALITY_STATES}）")
    db.execute("UPDATE dataset_snapshots SET quality_state=? WHERE id=?",
               (finality, snapshot_id))
    return {"snapshot_id": snapshot_id, "finality": finality}


def coverage_report(db, *, period: str = "1d", adjust: str = "qfq",
                    lookback_days: int = 10, universe: list[str] | None = None) -> dict:
    """覆盖率报表：最近 lookback_days 每个交易日在库的 code 数 + provider 占比。"""
    cut = _cutoff_ymd(lookback_days)
    rows = db.query(
        "SELECT dt, COUNT(DISTINCT code) AS n FROM local_bars "
        "WHERE period=? AND adjust=? AND dt >= ? "
        "GROUP BY dt ORDER BY dt DESC LIMIT ?",
        (period, adjust, cut, lookback_days))
    prov_rows = db.query(
        "SELECT COALESCE(provider_id,'') AS p, COUNT(*) AS n FROM local_bars "
        "WHERE period=? AND adjust=? AND dt >= ? "
        "GROUP BY p ORDER BY n DESC",
        (period, adjust, cut))
    total_bars = sum(int(r["n"]) for r in prov_rows) or 1
    return {
        "per_day": [{"dt": r["dt"], "codes": int(r["n"])} for r in rows],
        "provider_share": [
            {"provider_id": r["p"], "bars": int(r["n"]),
             "share": round(int(r["n"]) / total_bars, 4)}
            for r in prov_rows],
        "universe_size": len(universe) if universe else None,
        "lookback_days": lookback_days,
    }


__all__ = ["reconcile_bars", "apply_finality", "coverage_report",
           "FINALITY_STATES", "PROVIDER_QUALITY_RANK"]
