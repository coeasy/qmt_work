"""V9 Phase 7：system.* JobKind 注册表（8 个真实 runner 工厂）。

调度器（ScheduleRunner）与 REST 均可按 kind 提交；全部委托既有真实服务
（BarsSyncer / DatasetSnapshotStore / quality / screener），零 mock：
    system.eod                 EOD 全流程管线（app/runtime/eod.py）
    system.sync_bars           全市场日线同步（复用 sync_runner）
    system.sync_fundamentals   基本面因子预取（选股加速缓存）
    system.refresh_universe    股票池刷新（本地列表重建）
    system.reconcile_bars      跨源对账（quality.reconcile_bars）
    system.rolling_repair      缺失区间滚动修复（按链补数）
    system.coverage_report     覆盖率 + 各源贡献占比报表
    system.publish_snapshot    Dataset Snapshot 发布
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("qmt_work.runtime.system_jobs")

SYSTEM_JOB_KINDS: tuple[str, ...] = (
    "system.eod", "system.sync_bars", "system.sync_fundamentals",
    "system.refresh_universe", "system.reconcile_bars",
    "system.rolling_repair", "system.coverage_report", "system.publish_snapshot",
)

Runner = Callable[[dict], Any]


def _db():
    from core.db import get_db
    return get_db()


# ---------------------------------------------------------------- sync_bars
def _sync_bars_runner(params: dict) -> Runner:
    from app.runtime.jobs import sync_runner
    return sync_runner(params)


# ---------------------------------------------------------- fundamentals
def _sync_fundamentals_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from app.screener.fundamentals import FUNDAMENTAL_FIELDS, fetch_fundamentals
        from app.screener.universe import UniverseSpec, resolve_universe

        fields = list(params.get("fields") or FUNDAMENTAL_FIELDS)
        codes = list(params.get("codes") or [])
        if not codes:
            uni = await resolve_universe(
                UniverseSpec(kind=str(params.get("universe") or "all")),
                policy_str=str(params.get("source_policy") or "auto"))
            codes = uni["codes"]
            if not codes:
                raise RuntimeError(
                    "股票池为空（数据源不可用且本地无股票列表）——请先同步数据或连接券商")
        limit = int(params.get("limit") or 0)
        if limit:
            codes = codes[:limit]

        async def _chunked():
            out: dict[str, Any] = {}
            missing: list[str] = []
            size = int(params.get("chunk") or 200)
            for i in range(0, len(codes), size):
                part = codes[i:i + size]
                res = await fetch_fundamentals(
                    part, policy_str=str(params.get("source_policy") or "auto"),
                    fields=fields)
                for f, m in res["fields"].items():
                    out.setdefault(f, {}).update(m)
                missing.extend(res["missing_codes"])
                job["report"](int((i + size) / max(1, len(codes)) * 100),
                              f"基本面 {min(i + size, len(codes))}/{len(codes)}")
            return {"fields": out, "missing_codes": missing,
                    "codes": len(codes), "source_policy": params.get("source_policy")}

        return await _chunked()
    return _run


# ------------------------------------------------------------- universe
def _refresh_universe_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from app.screener.universe import UniverseSpec, resolve_universe
        kind = str(params.get("universe") or "all")
        uni = await resolve_universe(
            UniverseSpec(kind=kind),
            policy_str=str(params.get("source_policy") or "auto"))
        job["report"](60, f"universe {kind}: {len(uni['codes'])} codes")
        if not uni["codes"]:
            raise RuntimeError(
                "股票池为空：数据源不可用且本地无股票列表（503 语义，不伪造）")
        return {"kind": kind, "codes": len(uni["codes"]),
                "provider_used": uni.get("provider_used"),
                "degraded": uni.get("degraded")}
    return _run


# --------------------------------------------------------- reconcile
def _reconcile_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        import asyncio

        from datasource.quality import reconcile_bars
        stats = await asyncio.to_thread(
            reconcile_bars, _db(),
            period=str(params.get("period") or "1d"),
            adjust=str(params.get("adjust") or "qfq"),
            lookback_days=int(params.get("lookback_days") or 10),
            price_diff_pct=float(params.get("price_diff_pct") or 0.005))
        job["report"](100, f"对账完成：conflicts={stats['conflicts']}")
        return stats
    return _run


# ----------------------------------------------------- rolling repair
def _rolling_repair_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from app.sync.bars import BarsSyncer
        lookback = int(params.get("lookback") or 30)

        def _cb(done: int, total: int, code: str) -> None:
            job["report"](int(done / total * 100) if total else 100,
                          f"修复 {done}/{total}（{code}）")

        syncer = BarsSyncer(
            concurrency=int(params.get("concurrency") or 4),
            lookback=lookback,
            provider_id=str(params.get("provider_id") or "auto"),
            batch_id=str(params.get("batch_id") or "") or None)
        summary = await syncer.sync_stock_list(
            limit=int(params.get("limit") or 0) or None, progress_cb=_cb)
        result = summary.to_dict()
        result["mode"] = "rolling_repair"
        return result
    return _run


# ---------------------------------------------------- coverage report
def _coverage_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        import asyncio

        from datasource.quality import coverage_report
        rep = await asyncio.to_thread(
            coverage_report, _db(),
            period=str(params.get("period") or "1d"),
            adjust=str(params.get("adjust") or "qfq"),
            lookback_days=int(params.get("lookback_days") or 10))
        job["report"](100, "coverage report done")
        return rep
    return _run


# ------------------------------------------------- publish snapshot
def _publish_snapshot_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from datasource.snapshots import DatasetSnapshotStore
        store = DatasetSnapshotStore(_db())
        snap = store.publish_local_bars(
            str(params.get("dataset") or "cn_equity_daily"),
            str(params.get("batch_id") or ""),
            str(params.get("provider_id") or "auto"),
            str(params.get("batch_id") or ""),
            quality_state=str(params.get("quality_state") or "provisional"),
            calendar_version=str(params.get("calendar_version") or ""),
            adjustment_version=str(params.get("adjustment_version") or "qfq"),
            manifest=dict(params.get("manifest") or {}))
        finality = str(params.get("finality") or "")
        if finality:
            from datasource.quality import apply_finality
            apply_finality(_db(), snap["id"], finality)
            snap["finality"] = finality
        return snap
    return _run


# ------------------------------------------------------------- EOD
def _eod_runner(params: dict) -> Runner:
    async def _run(job: dict) -> dict:
        from app.runtime.eod import run_eod_pipeline
        merged = dict(params)
        merged["_job"] = job
        return await run_eod_pipeline(merged)
    return _run


_FACTORIES: dict[str, Callable[[dict], Runner]] = {
    "system.eod": _eod_runner,
    "system.sync_bars": _sync_bars_runner,
    "system.sync_fundamentals": _sync_fundamentals_runner,
    "system.refresh_universe": _refresh_universe_runner,
    "system.reconcile_bars": _reconcile_runner,
    "system.rolling_repair": _rolling_repair_runner,
    "system.coverage_report": _coverage_runner,
    "system.publish_snapshot": _publish_snapshot_runner,
}


def runner_for(kind: str) -> Runner | None:
    """按 kind 取 runner 工厂；非 system kind 返回 None（调用方回退内置）。"""
    factory = _FACTORIES.get(kind)
    return factory


def register_all() -> None:
    """把 system.* 工厂注册进 JobRuntime（main 启动时调用一次）。"""
    from app.runtime.jobs import register_runner_factory
    for kind, factory in _FACTORIES.items():
        register_runner_factory(kind, factory)


__all__ = ["SYSTEM_JOB_KINDS", "runner_for", "register_all"]
