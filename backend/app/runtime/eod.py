"""V9 Phase 7：EOD 数据治理管线（选股数据每天自动到位的保障）。

步骤（每步独立 try/except，失败记录 degraded 而非中断整批）：
    calendar → universe → bars_sync(主源+备用源) → quality/coverage
    → reconcile → snapshot publish(finality) → screener refresh 信号

默认调度（F9 修复）：``Asia/Shanghai 18:30``（收盘后），``enabled=True``，
cron ``30 18 * * 1-5``（工作日）。由 ``ensure_default_schedule()`` 在启动时
幂等创建（schedules 表，misfire=coalesce）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("qmt_work.runtime.eod")

DEFAULT_EOD_CRON = "30 18 * * 1-5"
DEFAULT_EOD_NAME = "system.eod"


async def run_eod_pipeline(params: dict) -> dict:
    """EOD 全流程 runner（供 system.eod JobKind / 手动触发）。

    params: {lookback, limit, adjust, price_diff_pct, finality, batch_id}
    """
    from core.db import get_db
    from datasource.quality import coverage_report, reconcile_bars

    job = params.get("_job")   # JobRuntime 注入（submit 路径外的直调可为 None）
    started = time.time()

    def _report(pct: int, msg: str) -> None:
        if isinstance(job, dict) and callable(job.get("report")):
            job["report"](pct, msg)

    steps: dict[str, Any] = {}
    degraded: list[str] = []
    db = get_db()

    # 1) calendar（可选：活跃券商提供；无券商 → 降级不阻断）
    try:
        from core.context import active_context
        bm = active_context().broker_manager
        bridge = bm.bridge(None) if bm is not None else None
        if bridge is not None:
            cal = bridge.get_trading_calendar("", "")
            steps["calendar"] = {"days": len(cal or []), "source": "broker"}
        else:
            degraded.append("calendar: 无券商连接，使用本地兜底")
            steps["calendar"] = {"days": 0, "source": "fallback"}
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"calendar: {exc}")
        steps["calendar"] = {"days": 0, "source": "error"}

    _report(5, "calendar done")

    # 2) universe
    universe_codes: list[str] = []
    try:
        from datasource.local_store import get_store
        store = get_store()
        rows = store.get_stock_list() if hasattr(store, "get_stock_list") else []
        universe_codes = [r["code"] if isinstance(r, dict) else str(r) for r in rows]
        steps["universe"] = {"codes": len(universe_codes)}
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"universe: {exc}")
        steps["universe"] = {"codes": 0, "error": str(exc)}
    _report(10, f"universe {len(universe_codes)} codes")

    # 3) bars sync（主源 + 备用源续跑：BarsSyncer 内部走 provider 链；
    #    主源整批失败不中断 —— 失败部分用备用 provider 再补一轮）
    sync_summary = {}
    try:
        from app.sync.bars import BarsSyncer

        def _cb(done: int, total: int, code: str) -> None:
            pct = 10 + int(done / max(1, total) * 55)
            _report(pct, f"sync {done}/{total}（{code}）")

        primary = BarsSyncer(
            concurrency=int(params.get("concurrency") or 8),
            lookback=int(params.get("lookback") or 320),
            provider_id=str(params.get("primary_provider") or "auto"),
            batch_id=str(params.get("batch_id") or "") or None)
        summary = await primary.sync_stock_list(
            limit=int(params.get("limit") or 0) or None, progress_cb=_cb)
        sync_summary = summary.to_dict()
        if summary.failed and str(params.get("fallback_provider") or ""):
            fallback = BarsSyncer(
                concurrency=int(params.get("concurrency") or 8),
                lookback=int(params.get("lookback") or 320),
                provider_id=str(params["fallback_provider"]),
                batch_id=str(params.get("batch_id") or "") or None)
            summary2 = await fallback.sync_stock_list(
                limit=int(params.get("limit") or 0) or None, progress_cb=_cb)
            sync_summary["fallback"] = summary2.to_dict()
            sync_summary["fallback"]["retried_codes"] = len(summary.failed)
            degraded.append(
                f"sync: 主源失败 {len(summary.failed)} 只，已按备用源续跑")
        steps["bars_sync"] = sync_summary
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"bars_sync: {exc}")
        steps["bars_sync"] = {"error": str(exc)}
    _report(70, "bars sync done")

    # 4) reconcile（跨源对账；冲突保留全部 raw）
    try:
        steps["reconcile"] = reconcile_bars(
            db, lookback_days=int(params.get("lookback_days") or 10),
            price_diff_pct=float(params.get("price_diff_pct") or 0.005))
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"reconcile: {exc}")
    _report(80, "reconcile done")

    # 5) coverage report
    try:
        steps["coverage"] = coverage_report(db, universe=universe_codes)
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"coverage: {exc}")
    _report(90, "coverage done")

    # 6) snapshot publish（finality 默认 provisional；显式确认后可置 final）
    try:
        from datasource.snapshots import DatasetSnapshotStore
        quality = "provisional"
        if isinstance(steps.get("bars_sync"), dict):
            written = int(steps["bars_sync"].get("bars_written") or 0)
            failed = int(steps["bars_sync"].get("failed_count") or 0)
            quality = "final" if (failed == 0 and written > 0 and not degraded) else (
                "provisional" if written > 0 else "invalid")
        snap = DatasetSnapshotStore(db).publish_local_bars(
            "cn_equity_daily", str(params.get("batch_id") or ""),
            str(params.get("primary_provider") or "auto"),
            str(params.get("batch_id") or ""),
            quality_state=quality,
            calendar_version=str(steps.get("calendar", {}).get("days", "")),
            adjustment_version=str(params.get("adjust") or "qfq"),
            manifest={"eod": steps, "degraded": degraded})
        steps["snapshot"] = {"id": snap.get("id"), "quality_state": quality}
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"snapshot: {exc}")

    _report(100, "EOD done")
    result = {
        "steps": steps, "degraded": degraded,
        "duration_ms": int((time.time() - started) * 1000),
    }
    # 选股刷新信号：EOD 完成即选股可用（下一步由前端/调度消费最新 snapshot）
    return result


def ensure_default_schedule(db) -> str | None:
    """幂等创建默认 EOD 调度（18:30 工作日，F9 修复 enabled=True）。

    返回 schedule id；schedules 表不存在（旧库未迁移）时返回 None。
    """
    try:
        from app.runtime.schedules import ScheduleStore
        store = ScheduleStore(db)
        for sch in store.list():
            if sch["kind"] == DEFAULT_EOD_NAME:
                return sch["id"]
        sid = store.create(
            kind=DEFAULT_EOD_NAME, cron=DEFAULT_EOD_CRON,
            name="EOD 数据治理（收盘后）", misfire_policy="coalesce",
            enabled=True,
            params={"lookback": 320, "adjust": "qfq", "lookback_days": 10})
        log.info("default EOD schedule created: %s (cron %s)",
                 sid.get("id") if isinstance(sid, dict) else sid, DEFAULT_EOD_CRON)
        return sid["id"] if isinstance(sid, dict) else sid
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_default_schedule failed: %s", exc)
        return None


__all__ = ["run_eod_pipeline", "ensure_default_schedule",
           "DEFAULT_EOD_CRON", "DEFAULT_EOD_NAME"]
