"""券商连接健康状态机 + 指数退避自动重连。

状态：disconnected -> connecting -> connected | error
- 启动时 / 连接断开后自动进入重连调度
- 指数退避：2^attempt 秒，最大 60s
- 提供 /brokers/{id}/health 查询与 WS 事件推送
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from core.clock import now_iso

if TYPE_CHECKING:
    from xtquant_client.manager import BrokerManager

log = logging.getLogger("qmt_work")


def _brief(exc) -> str:
    """异常 → 单行摘要（供 ``last_error`` 使用）。

    优先用 ``BrokerError.brief``（首行 + 限长）；普通异常退化为同样的规则。
    旧实现是 ``str(exc)[:200]``——对多行诊断会在句中截断，并把换行带进一行式 UI。
    """
    b = getattr(exc, "brief", None)
    if isinstance(b, str) and b:
        return b
    text = str(exc)
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return text[:300]


#: 正常退避上限（秒）：瞬时故障（客户端未启动 / 网络抖动）应较快自愈。
_BACKOFF_CAP = 60
#: 非自愈型失败的退避上限（秒）：券商授权/白名单/权限类根因人工修复前**不可能**成功，
#: 仍按 60s 周期重拉桥接子进程只是白耗（每次都要 spawn 子进程 + 跑满 45s 重试预算，
#: 并在客户端日志里持续刷 ``not allowed``），故降频到 5 分钟，保留自愈能力而不刷屏。
_BACKOFF_CAP_NEEDS_ACTION = 300


class BrokerHealthMonitor:
    def __init__(self, manager: BrokerManager, on_event=None, check_interval: float = 5.0):
        self._manager = manager
        self._on_event = on_event
        self._check_interval = check_interval
        self._task: asyncio.Task | None = None
        self._stopped = False

    async def start(self):
        await self.stop()
        self._stopped = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:  # noqa: BLE001
                pass
        self._task = None

    async def _loop(self):
        while not self._stopped:
            try:
                for conn in self._manager.all_connections():
                    await self._check(conn)
            except Exception as exc:  # noqa: BLE001
                log.warning("broker health loop error: %s", exc)
            interval = self._check_interval
            try:
                from core.state import state
                if state.runtime_config is not None:
                    interval = state.runtime_config.health_check_interval
            except Exception:  # noqa: BLE001
                pass
            # 非交易时段放大健康检查间隔（60s 探活）
            from gateway.trading_session import default_session
            await asyncio.sleep(default_session.sleep_seconds(interval, 60.0))

    async def _check(self, conn):
        try:
            ok = conn.adapter.is_connected()
        except Exception as exc:  # noqa: BLE001
            ok = False
            conn.last_error = _brief(exc)
        conn.connected = ok
        if ok:
            if conn.health_status != "connected":
                conn.health_status = "connected"
                conn.reconnect_attempts = 0
                rt = conn.reconnect_task
                if rt is not None:
                    # C4：无论任务是否已结束，一律清理字段——旧代码仅当循环异常退出
                    # 才清 reconnect_task，重连成功路径不清 → 该字段残留非 None，
                    # 下方 `is None` 判定永远为假，自愈能力随机永久失效。
                    if not rt.done():
                        rt.cancel()
                        try:
                            await rt
                        except Exception:  # noqa: BLE001
                            pass
                    conn.reconnect_task = None
                self._emit(conn.cfg.conn_id, "broker.connected", {"detail": "ok"})
        else:
            # 注意：不把 "needs_action" 降级成 "disconnected" —— 后者会抹掉
            # 「必须人工处理」这一关键状态，用户就再也看不到该去做什么了。
            if conn.health_status in ("connected", "connecting"):
                conn.health_status = "disconnected"
                self._emit(conn.cfg.conn_id, "broker.disconnected",
                           {"last_error": conn.last_error})
            # C4：用 task.done() 判定而非仅 `is None`——重连成功后字段残留非 None 时
            # （旧代码 bug）也能继续创建新的重连任务，保证自愈不失效。
            if conn.cfg.active and (conn.reconnect_task is None or conn.reconnect_task.done()):
                conn.reconnect_task = asyncio.create_task(self._reconnect(conn))

    async def _reconnect(self, conn):
        conn.health_status = "connecting"
        # ★ 2026-09-28（R2）：退避上限**按失败性质分档**。旧实现恒为 min(2^n, 60)，
        #   对「券商授权/白名单/权限」这类非自愈根因会以 60s 周期无限重拉桥接子进程
        #   （每次 spawn + 45s 重试预算 + 客户端日志刷 not allowed），既无意义又掩盖问题。
        #   现在：一旦判定 needs_action，上限提到 300s 并把 health_status 标成
        #   "needs_action"，前端可据此提示「需人工处理」而不是笼统的「错误」。
        cap = _BACKOFF_CAP
        while not self._stopped and conn.cfg.active:
            delay = min(2 ** conn.reconnect_attempts, cap)
            log.info("reconnect %s in %ss (attempt %d, cap=%ss)",
                     conn.cfg.conn_id, delay, conn.reconnect_attempts, cap)
            await asyncio.sleep(delay)
            if self._stopped:
                return
            try:
                await conn.bridge.start()
                if conn.adapter.is_connected():
                    conn.connected = True
                    conn.health_status = "connected"
                    conn.reconnect_attempts = 0
                    conn.last_error = ""
                    self._emit(conn.cfg.conn_id, "broker.connected",
                               {"detail": "reconnected"})
                    # C4 关键：成功路径必须清空 reconnect_task——否则该字段残留
                    # 非 None，_check 的 `is None` 判定永不成立，自愈能力随机永久失效。
                    conn.reconnect_task = None
                    return
                # 子进程起来了但没连上：同样是一次失败，走下面的统一记账
                conn.last_error = conn.last_error or "重连后仍未连接上（adapter.is_connected() 为假）"
            except Exception as exc:  # noqa: BLE001
                conn.last_error = _brief(exc)
                if getattr(exc, "needs_action", False):
                    cap = _BACKOFF_CAP_NEEDS_ACTION
                    conn.health_status = "needs_action"
                log.warning("reconnect %s failed%s: %s", conn.cfg.conn_id,
                            "（需人工处理，已降频）" if cap == _BACKOFF_CAP_NEEDS_ACTION else "",
                            exc)
            else:
                if cap == _BACKOFF_CAP:
                    conn.health_status = "error"
            conn.reconnect_attempts += 1
        conn.reconnect_task = None

    def _emit(self, conn_id: str, event: str, data: dict):
        """推送连接状态事件到 WS 客户端。

        兼容同步/异步回调：on_event 通常为 ws_manager.broadcast(event_type, payload)，
        传入正确参数；若返回协程则调度到事件循环（原实现未 await 且把 dict 当 event_type 传，
        导致 broker.connected/disconnected 事件从未真正推送到前端）。
        唯一实现见 `core/emit.py::emit_event`。

        ★ `event` 必须是**完整事件名常量**（如 ``"broker.connected"``），不是渠道后缀。
        旧实现在这里拼 ``f"broker.{event}"`` —— f-string 构造的事件名**无法被
        `tests/contracts/introspect.py` 静态还原**，于是这两个真实存在、且必然会发生的事件
        从未进入 WS 契约基线。后果不是「少一条记录」而是**契约面与实际不符**：前端一旦
        按规范登记消费策略，`scripts/check_ws_consumption.py` 就会报
        「前端登记但后端基线不存在」——把正确的登记判成错的。
        改为调用点直接传常量全名后，基线可由标准出口 `core.emit.emit_event` 自动派生。
        """
        # 阶段 3：断线/重连可观测——指标 qmt_conn_events_total{event=disconnected/connected/reconnected}
        try:
            from gateway.metrics import get_metrics
            ev = ("reconnected" if data.get("detail") == "reconnected"
                  else event.rsplit(".", 1)[-1])
            get_metrics().record_conn_event(conn_id, ev)
        except Exception:  # noqa: BLE001
            pass
        from core.emit import emit_event
        emit_event(self._on_event, event, {"conn_id": conn_id, **data})

    def status(self, conn_id: str) -> dict | None:
        conn = self._manager._conns.get(conn_id)
        if not conn:
            return None
        return {
            "conn_id": conn_id,
            "status": conn.health_status,
            "connected": conn.connected,
            "active": conn.cfg.active,
            "reconnect_attempts": conn.reconnect_attempts,
            "last_error": conn.last_error,
            "ts": now_iso(),
        }
