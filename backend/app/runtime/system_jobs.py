"""V9 Phase 7：system.* JobKind 注册表（9 个真实 runner 工厂 + 默认调度）。

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
    system.classic_screen      经典策略选股（app/screener/classic.py，复刻 Sequoia-X）

另外 ``register_all`` 会播种两条默认调度（见 ``DEFAULT_SCHEDULES``），
让「定时更新日线 / 定时自动选股」开箱即用——用户不必自己写 cron。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("qmt_work.runtime.system_jobs")

SYSTEM_JOB_KINDS: tuple[str, ...] = (
    "system.eod", "system.sync_bars", "system.sync_fundamentals",
    "system.refresh_universe", "system.reconcile_bars",
    "system.rolling_repair", "system.coverage_report", "system.publish_snapshot",
    "system.classic_screen",
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


# ------------------------------------------------------ classic_screen
def _classic_screen_runner(params: dict) -> Runner:
    """定时经典策略选股（复刻 Sequoia-X 的「收盘后自动跑策略」）。

    与手动选股共用同一条链路：解析股票池 → BarsProvider 批量取日线 →
    ``screener.classic.run_classic`` 求值。差别只是由调度触发、结果落在作业里。
    """
    async def _run(job: dict) -> dict:
        import asyncio as _asyncio

        from app.data.bars_provider import BarsProvider
        from app.screener.classic import STRATEGY_IDS, run_classic
        from app.screener.universe import UniverseSpec, resolve_universe

        strategies = params.get("strategies") or params.get("strategy") or []
        if isinstance(strategies, str):
            strategies = [strategies]
        strategies = [s for s in strategies if s] or list(STRATEGY_IDS)
        # 写错的策略名必须让作业**失败并留下原因**，而不是每天跑出「0 命中」——
        # 后者用户会当成「行情不好」，永远发现不了配置是错的。
        unknown = [s for s in strategies if s not in STRATEGY_IDS]
        if unknown:
            raise RuntimeError(
                f"未知经典策略：{', '.join(unknown)}（可选 {', '.join(STRATEGY_IDS)}）")

        limit = int(params.get("limit") or 50)
        period = str(params.get("period") or "1d")
        adjust = str(params.get("adjust") or "qfq")
        policy = str(params.get("source_policy") or "auto")

        uni = await resolve_universe(
            UniverseSpec(kind=str(params.get("universe") or "all")), policy_str=policy)
        codes = uni["codes"]
        if not codes:
            raise RuntimeError("股票池为空——请先运行日线同步任务或连接券商数据源")
        max_codes = int(params.get("max_codes") or 0)
        if max_codes and max_codes > 0:
            codes = codes[:max_codes]

        bp = BarsProvider()
        bars_map, report = await bp.get_bars_batch(
            codes, period=period, adjust=adjust, policy_str=policy,
            offline=bool(params.get("offline")), lite=True)

        results: dict[str, list] = {}
        for sid in strategies:
            # 全池逐只形态识别是纯 CPU ⇒ 必须移出事件循环
            results[sid] = await _asyncio.to_thread(
                run_classic, bars_map, sid, params.get("classic_params"), limit)

        return {
            "strategies": strategies,
            "scanned": len(bars_map),
            "total_hits": sum(len(v) for v in results.values()),
            "results": results,
            "provider": report.provider_used,
            "degraded": report.degraded,
            "degraded_reason": report.degraded_reason or "",
        }
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
    "system.classic_screen": _classic_screen_runner,
}


def runner_for(kind: str) -> Runner | None:
    """按 kind 取 runner 工厂；非 system kind 返回 None（调用方回退内置）。"""
    factory = _FACTORIES.get(kind)
    return factory


# ------------------------------------------------------ 默认调度（易用性）
# 为什么需要：``register_all`` 只注册 runner 工厂，**不建任何调度** ——
# 用户想要「每天自动更新日线 / 自动选股」必须自己填 cron，门槛很高
# （Sequoia-X 就是靠 crontab 在收盘后跑，这里把它内置成开箱即用）。
#
# 幂等：用**固定 schedule_id** 播种，已存在即跳过，绝不覆盖用户改过的配置。
# 时间排布刻意构成一条链：15:30 先把当日日线落库 → 16:00 才有数据可选股 →
# 18:30 再跑 EOD 全流程对账/快照（既有的 ensure_default_schedule）。
# 若把选股排在日线更新之前，它会拿昨天的 K 线跑，选出的是「昨天的结果」。
DEFAULT_SCHEDULES: tuple[dict, ...] = (
    {
        "id": "sch-default-sync-bars",
        "kind": "system.sync_bars",
        "cron": "30 15 * * 1-5",          # 每交易日 15:30（收盘 15:00 之后）
        "name": "收盘后更新日线数据",
        # concurrency / lookback 用**实测验证过**的值（2026-09-19 全市场 5224 只）：
        # - concurrency=4：券商补下载走的是本地 RPC，4 并发下全市场 7 分钟跑完
        #   （ok=5221 / stale=4）。默认 8 在全市场量级未经实测，且在线源在
        #   并发 32 时曾集体超时触发熔断 —— 批量同步宁慢勿炸。
        # - lookback=120：足够覆盖全部内置策略（最长 high_tight_flag 的 60 日
        #   回看 + MA20 + RPS 20 日），而默认 320 会把耗时翻近一倍。
        #   窗口增量是幂等合并，缩短回看**不会**丢历史（旧数据不删除）。
        "params": {"period": "1d", "adjust": "qfq",
                   "concurrency": 4, "lookback": 120},
    },
    {
        "id": "sch-default-classic-screen",
        "kind": "system.classic_screen",
        "cron": "0 16 * * 1-5",           # 每交易日 16:00（日线更新之后）
        "name": "收盘后经典策略选股",
        "params": {"strategies": ["turtle_trade", "ma_volume"], "limit": 50},
    },
)


def ensure_default_schedules(enabled: bool = True) -> list[dict]:
    """播种默认调度（幂等）。返回本次新建的调度；失败只记日志，不影响启动。"""
    from app.runtime.schedules import ScheduleStore

    created: list[dict] = []
    try:
        store = ScheduleStore(_db())
        for spec in DEFAULT_SCHEDULES:
            try:
                if store.get(spec["id"]):
                    continue            # 已存在（含用户改过的）⇒ 绝不覆盖
                created.append(store.create(
                    spec["kind"], spec["cron"], name=spec["name"],
                    params=spec["params"], schedule_id=spec["id"],
                    enabled=enabled))
                log.info("已播种默认调度：%s（%s %s）",
                         spec["name"], spec["cron"], spec["kind"])
            except Exception as exc:  # noqa: BLE001 — 单条失败不影响其余
                log.warning("默认调度 %s 创建失败：%s", spec["id"], exc)
    except Exception as exc:  # noqa: BLE001 — 调度播种失败绝不能阻断启动
        log.warning("默认调度播种失败（已跳过，不影响启动）：%s", exc)
    return created


def register_all(seed_schedules: bool = True) -> None:
    """把 system.* 工厂注册进 JobRuntime（main 启动时调用一次）。

    ``seed_schedules=True`` 时顺带播种默认调度（收盘后日线更新 + 经典策略选股），
    让「定时更新日线 / 定时自动选股」开箱即用；幂等且失败不阻断启动。
    """
    from app.runtime.jobs import register_runner_factory
    for kind, factory in _FACTORIES.items():
        register_runner_factory(kind, factory)
    if seed_schedules:
        ensure_default_schedules()


__all__ = [
    "SYSTEM_JOB_KINDS", "runner_for", "register_all",
    "DEFAULT_SCHEDULES", "ensure_default_schedules",
]
