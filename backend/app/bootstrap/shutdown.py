"""优雅停机（逆序关闭）。

启动顺序：db → broker → engines → watchdogs → replay → misc
停机顺序：misc → replay → watchdogs → engines → broker → db

历史：原 main.py 的 finally 块散落 12+ 行停机代码，R1 统一到本模块。
"""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.state import state
from fastapi import FastAPI

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

    # 3. DB 备份
    db_backup = getattr(app.state, "_db_backup", None)
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

    # 16. 所有连接 bridge
    for conn in state.broker_manager.all_connections():
        try:
            await conn.bridge.stop()
        except (ConnectionError, RuntimeError, OSError) as exc:
            # bridge 子进程可能在父进程退出前已自杀；不阻断
            log.debug("bridge %s 关停异常（已忽略）：%s", conn.id, exc)

    log.info("qmt_work stopped")


__all__ = ["shutdown"]
