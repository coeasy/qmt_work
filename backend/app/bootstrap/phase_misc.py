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


def _job_failure_alert(job: dict) -> None:
    """定时任务失败的告警出口（由 JobRuntime 回调）。

    ★ 为什么要这一层：定时任务的典型失效是「某天起一直失败但没人看日志」——
    日线不再更新、选股结果一直是旧的，界面却一切正常。日志不是告警。
    这里把失败同时送给两处：
      1) WS 事件（前端事件日志/通知中心可见）；
      2) 已有的 notifier（出站通知，如 webhook / 系统通知）。
    任一不可用都只记日志，绝不影响任务本身。
    """
    kind = job.get("kind", "")
    title = f"定时任务失败：{job.get('name') or kind}"
    detail = f"{job.get('error') or job.get('message') or '未知错误'}（job {job.get('id', '')}）"
    try:
        from core.emit import emit_event
        emit_event(None, {"type": "system",
                          "data": {"event": "job_failed", "kind": kind,
                                   "job_id": job.get("id", ""), "title": title,
                                   "message": detail}})
    except Exception as exc:  # noqa: BLE001
        log.warning("任务失败事件推送异常（已忽略）：%s", exc)
    try:
        notifier = getattr(state, "notifier", None)
        if notifier is not None:
            import asyncio
            coro = notifier.notify("job.failed", title, detail,
                                   {"kind": kind, "job_id": job.get("id", "")})
            if asyncio.iscoroutine(coro):
                asyncio.ensure_future(coro)
    except Exception as exc:  # noqa: BLE001
        log.warning("任务失败通知异常（已忽略）：%s", exc)


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
        # 任务失败接入告警：否则「日线同步每天失败」这种事只会躺在日志里，
        # 用户看到的是「数据停在三天前」却不知道为什么。
        runtime.set_failure_hook(_job_failure_alert)
        runtime.start_reaper(interval=30.0)  # P1-20 租约收割
        # 9 个 system.* JobKind（含 system.classic_screen）+ 播种默认调度
        # （15:30 日线更新 → 16:00 经典选股 → 18:30 EOD，见 ensure_default_schedules）
        register_all()
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
