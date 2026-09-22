"""阶段 6：杂项 - 行情缓存定时 + 资金流采集 + 数据源预热。

- 行情缓存定时维护（runtime_config 热控）
- 资金流自动采集（交易时段每 5 分钟）
- 多源行情补充源预热（best-effort 后台）
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from core.config import settings
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
    # ★ 同步状态落库回调（V11 §5.3 F）。`gateway` 是顶层包，不能直接 import app
    #   （会重新引入 V10 A3 刚消除的反向依赖）⇒ 在此绑定后注入。
    #   注入失败只影响「重启后还能看到上次同步结果」，不影响同步本身。
    state_recorder = None
    try:
        from app.sync.state import STREAM_MARKET_SYNC, record_run
        state_recorder = lambda **kw: record_run(STREAM_MARKET_SYNC, **kw)
    except Exception as exc:  # noqa: BLE001
        log.warning("同步状态落库回调注入失败（已降级，同步不受影响）：%s", exc)
    state.market_sync = MarketSync(
        state, state.runtime_config,
        job_runtime=job_runtime, sync_job_factory=sync_job_factory,
        state_recorder=state_recorder)
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
        # ★ 启动即校验「相位是否还跟得上自己的 cron」。
        #   调度是**按相位**触发的，任何直接改 cron 的路径（迁移、手工 SQL）留下的
        #   过期相位都会让它按旧时间跑 —— 配置看起来却完全正常。实测迁移 v27 就是
        #   这种情况（同步相位停在 15:30、选股停在 16:00，顺序被倒置）。
        #   这里统一清空不一致的相位，由 runner 按当前 cron 重新对表。
        try:
            fixed = store.normalize_phases()
            if fixed:
                log.warning("已重置 %d 个与 cron 不一致的调度相位：%s",
                            len(fixed), ", ".join(fixed))
        except Exception as exc:  # noqa: BLE001
            log.warning("调度相位校验失败（不影响调度本身）：%s", exc)
        runner = ScheduleRunner(store, runtime, tick_seconds=30.0)
        runner.start()  # 同步方法（内部 ensure_future 后台循环），不可 await
        state.schedule_runner = runner
        log.info("durable scheduler ready: %d schedules",
                 len(store.list(enabled_only=True)))
    except Exception as exc:  # noqa: BLE001
        log.warning("durable scheduler 启动失败（不影响其他能力）：%s", exc)

    # ---- 主库备份：必须是**最后一个**阶段 ----
    # ★ 为什么放这里而不是 watchdogs（2026-09-20 迁来）：启动备份要记录「源库指纹」
    #   用于「主库没变化就跳过」，而本阶段之前还有 replay / misc 前半段在写库 ⇒
    #   在 watchdogs 阶段记下的指纹当场就过期，「客户端反复启停不重复整库复制」
    #   永远不成立（实测：每次启动都白复制一份 1GB+ 的主库）。
    #   放到所有阶段都写完、库已静止之后，指纹才是准的。
    db_backup = None
    if settings.db_backup_enabled:
        from gateway.db_backup import DBBackup
        db_backup = DBBackup(
            settings.db_path, keep=settings.db_backup_keep,
            interval=settings.db_backup_interval, db=state.db,
            max_total_mb=settings.db_backup_max_total_mb,
            min_keep=settings.db_backup_min_keep)
        # 1GB+ 主库的一致性复制要数秒：丢线程，否则整个启动被它拖住
        # （就绪广播、前端首屏全在等它）。
        await asyncio.to_thread(db_backup.backup_once, "startup")
        await db_backup.start()
        # ★ 日志必须同时报「份数」与「体积」两个上限：只报 keep 会让人以为备份占用
        #   有界，而实测 1.14GB 主库 × 10 份 = 11GB（磁盘真被吃满过）。
        log.info("db backup enabled: interval=%.0fs keep=%d max_total=%sMB min_keep=%d",
                 settings.db_backup_interval, settings.db_backup_keep,
                 settings.db_backup_max_total_mb, settings.db_backup_min_keep)
    # 停机阶段与 /config/paths 的占用展示都从上下文取
    state.db_backup = db_backup

    log.info("qmt_work started (real broker mode)")
    return {}


__all__ = ["setup"]
