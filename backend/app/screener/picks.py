"""选股结果落库（V11 R14 §5.3 E）。

## 为什么必须落库

选股结果此前**只存在于作业返回的 JSON 里**：

- 手动选股：关掉页面就没了，第二天想看「昨天选出来什么」只能重跑（而重跑用的是
  **今天的**行情，结果自然不同）；
- 定时选股（``system.classic_screen``）：跑完之后用户只能去「任务运行时」翻一个
  巨大的 JSON 结果字段 —— 翻不到就等于没有。

于是「每天收盘后自动选股」这条链路事实上是**跑给日志看的**。

## 为什么是两张表

``screen_runs``（每次运行一行，**命中 0 只也写**）+ ``screen_picks``（命中行）。

只建 picks 表的话，「今天跑过但没选出票」与「从来没跑过」在界面上完全一样 ——
这正是本项目反复踩到的「空结果不可见」问题。运行表还额外承载三个**可信度元数据**：

- ``scanned == 0``：股票池为空或批量取数为空 ⇒ 不是「行情不好」，而是同步没跑成功；
- ``bar_date``：命中所依据的日线**截至哪一天**。空/陈旧时前端要显式标注 ——
  「非空 ≠ 够新」，券商本地历史可能只到一年前却非空；
- ``degraded_*``：这次选股用的是降级数据源，结果的可信度要跟着降。

## 接口约定

- :func:`save_run` 写一次运行（运行行 + 全部命中行），返回运行摘要（含 ``run_id``）；
- :func:`list_runs` / :func:`latest_run` 读运行元数据（**不**带命中行）；
- :func:`picks_of_run` 读某次运行的命中行。

写入是**追加**（每次生成新 ``run_id``），不做 upsert —— 用户需要看到「跑过几次、
每次结果如何」的演变。
"""
from __future__ import annotations

import json
import logging
import uuid

import core.db as db_mod
from core.clock import now_iso

log = logging.getLogger("qmt_work.screener.picks")

#: 单次运行最多落库多少条命中（防止某个策略在全市场命中上万条把库撑爆）。
#: 超出部分不落库，但在运行表里如实记 ``truncated``。
MAX_PICKS_PER_RUN = 2000

#: 命中行里已经单独成列的字段，不再进 detail_json
_COLUMN_FIELDS = {"code", "name", "strategy", "close", "change_pct", "score", "reason"}

_RUN_COLS = ("run_id", "source", "job_id", "strategies", "hits", "scanned",
             "bar_date", "degraded", "degraded_reason", "truncated", "created_at")


def _new_run_id() -> str:
    return f"{now_iso().replace(':', '').replace('-', '')[:15]}-{uuid.uuid4().hex[:8]}"


def bars_last_date(bars_map: dict) -> str:
    """这一批 K 线的**数据截至日**（``YYYYMMDD``）。

    ★ 「非空 ≠ 够新」：券商本地历史可能只到一年前却非空，所以选股结果必须带上
    它到底基于哪一天的日线。取全池的**最大值**（最新）—— 池子里最新的那只票更新到
    哪天，就是这次选股的实际参照日。解析不了返回 ``""``（**不猜成今天**）。

    唯一实现：手动选股（``routes/screen.py``）与定时选股
    （``runtime/system_jobs.py``）共用，避免两处口径分叉。
    """
    from core.clock import bar_date

    latest = ""
    for bars in (bars_map or {}).values():
        if not bars:
            continue
        last = bars[-1]
        d = bar_date(last.get("time") if hasattr(last, "get") else None)
        if d and d > latest:
            latest = d
    return latest


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def save_run(
    *,
    results: dict[str, list[dict]],
    scanned: int,
    source: str = "manual",
    job_id: str = "",
    bar_date: str = "",
    degraded: bool = False,
    degraded_reason: str = "",
    names: dict[str, str] | None = None,
) -> dict:
    """把一次选股运行落库（运行行 + 命中行），返回运行摘要（含 ``run_id``）。

    ``results``：``{策略id: [命中行...]}``，行结构与 ``screener.classic.run_classic``
    一致（``code/close/change_pct/reason/...``）。
    ``names``：代码 → 中文名（可选；券商返回常只有代码，名称为空不影响选股）。

    ★ 运行行**无论如何都会写**（含命中 0 只）：漏掉它就等于把「跑过但没选中」
    伪装成「从没跑过」。
    """
    run_id = _new_run_id()
    created = now_iso()
    per_strategy: dict[str, int] = {}
    rows: list[tuple] = []
    total = 0
    for sid, items in (results or {}).items():
        hits = list(items or [])
        per_strategy[sid] = len(hits)
        total += len(hits)
        for r in hits:
            if not isinstance(r, dict):
                continue
            if len(rows) >= MAX_PICKS_PER_RUN:
                break
            code = str(r.get("code") or "")
            if not code:
                continue
            detail = {k: v for k, v in r.items()
                      if k not in _COLUMN_FIELDS and not isinstance(v, (dict, list))}
            rows.append((
                run_id, sid, code,
                str(r.get("name") or (names or {}).get(code) or ""),
                _f(r.get("close")), _f(r.get("change_pct")), _f(r.get("score")),
                str(r.get("reason") or ""),
                json.dumps(detail, ensure_ascii=False),
                created,
            ))
    truncated = total > len(rows)
    summary = {"run_id": run_id, "saved": 0, "total_hits": total,
               "per_strategy": per_strategy, "truncated": truncated, "error": ""}
    db = None
    try:
        db = db_mod.get_db()
    except Exception as exc:  # noqa: BLE001
        # 落库是「让结果看得见」的增强项，失败不得影响选股本身的结果返回。
        log.warning("screen_picks 落库跳过（DB 未就绪）：%s", exc)
        summary["error"] = str(exc)
        return summary
    try:
        db.execute(
            "INSERT OR REPLACE INTO screen_runs (run_id, source, job_id, strategies,"
            " hits, scanned, bar_date, degraded, degraded_reason, truncated, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, source, job_id, ",".join(str(s) for s in (results or {}).keys()),
             total, int(scanned or 0), str(bar_date or ""),
             1 if degraded else 0, str(degraded_reason or ""), 1 if truncated else 0, created),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("screen_runs 落库失败（选股结果仍返回）：%s", exc)
        summary["error"] = str(exc)
        return summary
    if rows:
        try:
            db.executemany(
                "INSERT INTO screen_picks (run_id, strategy, code, name, close,"
                " change_pct, score, reason, detail_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            summary["saved"] = len(rows)
        except Exception as exc:  # noqa: BLE001
            log.warning("screen_picks 落库失败（运行记录已写，选股结果仍返回）：%s", exc)
            summary["error"] = str(exc)
            return summary
    log.info("screen_picks 已落库：run=%s source=%s hits=%d scanned=%d",
             run_id, source, total, scanned)
    return summary


def _run_row(row: dict) -> dict:
    return {
        "run_id": row.get("run_id", ""),
        "source": row.get("source", ""),
        "job_id": row.get("job_id", ""),
        "strategies": [s for s in str(row.get("strategies") or "").split(",") if s],
        "hits": int(row.get("hits") or 0),
        "scanned": int(row.get("scanned") or 0),
        "bar_date": row.get("bar_date", ""),
        "degraded": bool(row.get("degraded")),
        "degraded_reason": row.get("degraded_reason", ""),
        "truncated": bool(row.get("truncated")),
        "created_at": row.get("created_at", ""),
    }


def list_runs(limit: int = 20, source: str = "") -> list[dict]:
    """最近若干次选股运行（按时间倒序）。**不返回命中行**，避免列表接口变大 JSON。"""
    sql = f"SELECT {', '.join(_RUN_COLS)} FROM screen_runs"
    params: list = []
    if source:
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, int(limit or 20)))
    try:
        return [_run_row(r) for r in db_mod.get_db().query(sql, tuple(params))]
    except Exception as exc:  # noqa: BLE001
        log.warning("读取选股运行列表失败：%s", exc)
        return []


def latest_run(source: str = "") -> dict | None:
    runs = list_runs(limit=1, source=source)
    return runs[0] if runs else None


def picks_of_run(run_id: str = "", *, strategy: str = "", limit: int = 500,
                 source: str = "") -> dict:
    """某次运行的命中行。``run_id`` 为空时取**最近一次**运行。

    返回 ``{run, picks, available_runs}``：``run`` 为空说明库里还没有任何选股记录
    （前端必须显示「尚未跑过」而不是空表格 —— 空表格会被读成「今天没选出票」）。
    """
    available = list_runs(limit=20)
    run = None
    if run_id:
        run = next((r for r in available if r["run_id"] == run_id), None)
    if run is None:
        run = latest_run(source=source) if source else (available[0] if available else None)
    if run is None:
        return {"run": None, "picks": [], "available_runs": []}
    sql = ("SELECT strategy, code, name, close, change_pct, score, reason,"
           " detail_json FROM screen_picks WHERE run_id = ?")
    params: list = [run["run_id"]]
    if strategy:
        sql += " AND strategy = ?"
        params.append(strategy)
    sql += " ORDER BY strategy, change_pct DESC, close DESC LIMIT ?"
    params.append(max(1, int(limit or 500)))
    try:
        raw = db_mod.get_db().query(sql, tuple(params))
    except Exception as exc:  # noqa: BLE001
        log.warning("读取选股命中失败：%s", exc)
        raw = []
    picks = []
    for r in raw:
        try:
            detail = json.loads(r.get("detail_json") or "{}")
        except (TypeError, ValueError):
            detail = {}
        picks.append({
            "strategy": r.get("strategy", ""),
            "code": r.get("code", ""),
            "name": r.get("name", ""),
            "close": r.get("close"),
            "change_pct": r.get("change_pct"),
            "score": r.get("score"),
            "reason": r.get("reason", ""),
            "detail": detail,
        })
    return {"run": run, "picks": picks, "available_runs": available}
