"""阶段 6：杂项 - 行情缓存定时 + 资金流采集 + 数据源预热。

- 行情缓存定时维护（runtime_config 热控）
- 资金流自动采集（交易时段每 5 分钟）
- 多源行情补充源预热（best-effort 后台）
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from core.state import state

log = logging.getLogger("qmt_work.bootstrap.misc")


async def setup(app: FastAPI) -> dict:
    from gateway.market_sync import MarketSync
    # V10 A3：注入 job_runtime 与 EOD 同步 JobSpec 工厂（消除 gateway→app 反向依赖）
    job_runtime = None
    sync_job_factory = None
    try:
        from app.runtime.jobs import JobSpec, get_runtime, sync_runner
        job_runtime = get_runtime()
        sync_job_factory = lambda params: JobSpec(
            kind="sync", name=params.get("name", "EOD 全市场同步"),
            runner=sync_runner(params), priority=2, params=params)
    except Exception as exc:  # noqa: BLE001
        log.warning("EOD job_runtime 注入失败（将降级为 no-op）：%s", exc)
    state.market_sync = MarketSync(
        state, state.runtime_config,
        job_runtime=job_runtime, sync_job_factory=sync_job_factory)
    await state.market_sync.start()
    log.info("market sync ready: enabled=%s sync_time=%s",
             state.market_sync.enabled, state.market_sync.sync_time)

    try:
        import asyncio

        from datasource.registry import get_hub
        asyncio.create_task(get_hub().warmup_all())
        log.info("行情数据源预热任务已提交（后台）")
    except Exception as exc:  # noqa: BLE001
        log.warning("行情数据源预热任务提交失败：%s", exc)

    try:
        from app.routes.market import start_moneyflow_collector
        start_moneyflow_collector()
    except Exception as exc:  # noqa: BLE001
        log.warning("资金流自动采集启动失败：%s", exc)

    # ---- V9 Phase 7：Durable Scheduler + EOD（选股数据每天自动到位） ----
    try:
        from app.runtime.jobs import get_runtime
        from app.runtime.schedules import ScheduleRunner, ScheduleStore
        from app.runtime.system_jobs import register_all
        runtime = get_runtime()
        runtime.attach_db(state.db)          # 启动补跑（catch-up）+ durable ledger
        runtime.start_reaper(interval=30.0)  # P1-20 租约收割
        register_all()                       # 8 个 system.* JobKind
        store = ScheduleStore(state.db)
        from app.runtime.eod import ensure_default_schedule
        ensure_default_schedule(state.db)    # F9 修复：默认 18:30 EOD，enabled
        runner = ScheduleRunner(store, runtime, tick_seconds=30.0)
        runner.start()  # 同步方法（内部 ensure_future 后台循环），不可 await
        state.schedule_runner = runner
        log.info("durable scheduler ready: %d schedules",
                 len(store.list(enabled_only=True)))
    except Exception as exc:  # noqa: BLE001
        log.warning("durable scheduler 启动失败（不影响其他能力）：%s", exc)

    log.info("qmt_work started (real broker mode)")
    return {}


__all__ = ["setup"]
