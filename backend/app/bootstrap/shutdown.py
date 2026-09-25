"""优雅停机（逆序关闭）。

启动顺序：db → broker → engines → watchdogs → replay → misc
停机顺序：misc → replay → watchdogs → engines → broker → db

历史：原 main.py 的 finally 块散落 12+ 行停机代码，R1 统一到本模块。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from core.config import settings
from core.state import state

log = logging.getLogger("qmt_work.bootstrap.shutdown")


async def shutdown(app: FastAPI) -> None:
    """逆序关闭所有引擎/服务。"""
    # 1. 广播关闭事件，前端据此进入离线态
    if state.ws_manager is not None:
        try:
            await state.ws_manager.broadcast(
                "system", {"event": "shutdown",
                           "message": "服务正在关闭，稍后将自动重连"})
        except (asyncio.CancelledError, ConnectionError, RuntimeError) as exc:
            # 关停期广播失败不阻断其他清理
            log.debug("ws shutdown 广播失败（已忽略）：%s", exc)

    # 2. 系统广播
    _system_task = getattr(app.state, "_system_task", None)
    if _system_task is not None:
        _system_task.cancel()
        try:
            await _system_task
        except (asyncio.CancelledError, RuntimeError) as exc:
            # 系统广播 task 关停本身可预期被取消
            log.debug("system broadcast task 关停异常（已忽略）：%s", exc)

    # 2b. 后台常驻协程（R26）——必须在任何 DB 关闭动作**之前**取消。
    #      这三者都是 create_task 出来的永不退出的循环，且**都会写库**：
    #        · 任务运行时派发器（空转 0.05s/轮）+ 租约收割器（30s/轮）+ 在飞作业；
    #        · 资金流自动采集（交易日每 5 分钟写 moneyflow_cache）。
    #      此前它们没有任何停机路径，句柄被丢弃后在 ``db.close()`` 之后仍可能触发写入。
    try:
        from app.runtime.jobs import get_runtime
        await get_runtime().stop()
    except Exception as exc:  # noqa: BLE001
        log.warning("jobs runtime stop failed: %s", exc)
    try:
        from app.services.market.kline_io import stop_moneyflow_collector
        await stop_moneyflow_collector()
    except Exception as exc:  # noqa: BLE001
        log.warning("moneyflow collector stop failed: %s", exc)

    # 2c. WS 连接与心跳任务（R26）——广播完关闭通知后再收尾。
    #      `WSManager._hb_tasks` 只在「客户端主动断开」时取消，正常停机时它仍挂在
    #      事件循环上（"Task was destroyed but it is pending"），已建立的连接也收不到
    #      关闭帧。放在 DB 关闭之前：close() 里没有库操作，但与其余收尾动作同属
    #      「先停后台，再关资源」的顺序。
    if state.ws_manager is not None:
        try:
            await state.ws_manager.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("ws manager close failed: %s", exc)

    # 3. DB 备份
    db_backup = state.db_backup
    if db_backup is not None and settings.db_backup_enabled:
        await db_backup.stop()

    # 4. 对账器
    if state.reconciler:
        await state.reconciler.stop()

    # 5. 健康监控
    if state.health_monitor:
        await state.health_monitor.stop()

    # 6. 泵守护
    _pump_task = getattr(app.state, "_pump_task", None)
    if _pump_task is not None:
        _pump_task.cancel()
        try:
            await _pump_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # 7. 涨停监控
    if state.limitup_monitor:
        await state.limitup_monitor.stop()

    # 8. 条件单
    if state.condition_engine:
        await state.condition_engine.stop()

    # 9. 行情缓存定时
    if getattr(state, "market_sync", None) is not None:
        await state.market_sync.stop()

    # 9b. V9 Phase 7：Durable Scheduler
    if getattr(state, "schedule_runner", None) is not None:
        try:
            await state.schedule_runner.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("schedule_runner stop failed: %s", exc)

    # 10. 订单超时守护
    if state.order_watchdog:
        await state.order_watchdog.stop()

    # 11. 通知
    if state.notifier:
        await state.notifier.close()

    # 12. Webhook out
    if state.webhook_out:
        await state.webhook_out.close()

    # 13. WAL
    if state.wal:
        state.wal.close()

    # 14. 回测队列
    if state.backtest_queue:
        await state.backtest_queue.close()

    # 15. 同步引擎
    if state.sync_engine:
        await state.sync_engine.stop()

    # 15b. 后台券商任务（R24/R25）——必须在 bridge.stop() **之前**取消。
    #      两类任务都会「自己把连接拉起来」：
    #        · `phase_broker._bg_start_tasks`：启动预算到点后交还后台继续拉起的连接；
    #        · `app.state._broker_acg`：每 30s 复探本机客户端的自动连接守卫。
    #      不取消的话，它们可能在 `conn.bridge.stop()` 之后重新建连 —— 停机后
    #      留下残留连接与子进程（本项目实测过「只杀主进程会留孤儿」的同类问题）。
    _bg_tasks: list[asyncio.Task] = []
    try:
        from app.bootstrap import phase_broker
        _bg_tasks.extend(t for t in list(phase_broker._bg_start_tasks) if not t.done())
    except Exception as exc:  # noqa: BLE001
        log.debug("读取启动期后台任务失败（已忽略）：%s", exc)
    _acg = getattr(app.state, "_broker_acg", None)
    if _acg is not None and not _acg.done():
        _bg_tasks.append(_acg)
    for _t in _bg_tasks:
        _t.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
        log.info("已取消 %d 个后台券商任务（防止停机后重连）", len(_bg_tasks))

    # 16. 所有连接 bridge
    for conn in state.broker_manager.all_connections():
        try:
            await conn.bridge.stop()
        except (ConnectionError, RuntimeError, OSError) as exc:
            # bridge 子进程可能在父进程退出前已自杀；不阻断
            log.debug("bridge %s 关停异常（已忽略）：%s", conn.id, exc)

    # 17. DB（启动顺序里 db 是第一个，停机必须最后）——P0-10
    # 此前全靠 GC 隐式回收，SQLite 句柄与未 checkpoint 的 -wal 文件可能残留。
    # 关闭动作本身是同步阻塞 I/O，移出事件循环，避免停机时卡住 loop。
    if state.db is not None:
        try:
            await asyncio.to_thread(state.db.close)
        except Exception as exc:  # noqa: BLE001
            log.warning("db close 失败（已忽略）：%s", exc)

    log.info("qmt_work stopped")


__all__ = ["shutdown"]
