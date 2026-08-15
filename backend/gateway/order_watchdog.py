"""订单超时守护（P1）：扫描券商 pending 委托，超时未成交自动撤单 + 告警。

设计要点：
- 不依赖券商侧时间戳（get_orders 不返回 order_time），由守护进程自记「首见时间」：
  某委托首次以 pending/submitted 状态出现时记录 now，持续超时 timeout 秒即撤单。
- 纯函数 collect_stale 负责状态判定与 first_seen 维护（便于单元测试）。
- 未连接券商 / 单次扫描失败不影响其他连接与下一轮。
"""
import asyncio
import logging
import time

log = logging.getLogger("qmt_work.order_watchdog")

# 视为「未成交活跃」的状态集合（小写比较）
_ACTIVE = {"submitted", "pending", "queued", "active", "not_dealt", "submitting"}


def collect_stale(orders: list[dict], first_seen: dict[str, float],
                  now: float, timeout: float) -> list[dict]:
    """纯函数：输入本轮券商委托列表与首见时间表，返回超时需处理的委托列表。

    副作用（维护 first_seen）：
    - 活跃委托首次出现 -> 记 now；持续超过 timeout -> 标记超时
    - 已非活跃（成交/撤单/失败）-> 清除记录
    - 本轮未再出现的活跃记录（单已被撤/已成交但不在本轮返回）-> 清除
    """
    stale: list[dict] = []
    seen: set[str] = set()
    for o in orders:
        oid = str(o.get("order_id") or "")
        status = str(o.get("status") or "").lower()
        if not oid:
            continue
        seen.add(oid)
        if status in _ACTIVE:
            t0 = first_seen.setdefault(oid, now)
            if timeout > 0 and now - t0 >= timeout:
                stale.append(o)
        else:
            first_seen.pop(oid, None)
    for oid in list(first_seen):
        if oid not in seen:
            first_seen.pop(oid, None)
    return stale


class OrderWatchdog:
    """后台任务：周期扫描各已连接券商，处理超时委托。"""

    def __init__(self, manager, timeout: float = 60.0, interval: float = 5.0,
                 enabled: bool = True, on_event=None, notifier=None):
        self.manager = manager
        self.timeout = timeout
        self.interval = interval
        self.enabled = enabled
        self.on_event = on_event      # WS 广播回调（event_type, data）
        self.notifier = notifier
        self._first_seen: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._last_scan_at: float = 0.0
        self._scans = 0
        self._handled = 0

    async def _loop(self):
        while True:
            try:
                await self._scan()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("order watchdog scan failed: %s", exc)
            await asyncio.sleep(self.interval)

    async def _scan(self):
        if not self.enabled or self.timeout <= 0:
            return
        now = time.time()
        self._scans += 1
        self._last_scan_at = now
        for conn in self.manager.all_connections():
            if not conn.connected or conn.bridge is None:
                continue
            try:
                orders = await conn.bridge.call(conn.adapter.get_orders) or []
            except Exception as exc:  # noqa: BLE001
                log.debug("watchdog get_orders %s failed: %s", conn.cfg.conn_id, exc)
                continue
            for o in collect_stale(orders, self._first_seen, now, self.timeout):
                oid = str(o.get("order_id") or "")
                if oid:
                    self._first_seen.pop(oid, None)
                self._handled += 1
                await self._handle_stale(conn.cfg.conn_id, o)

    async def _handle_stale(self, conn_id: str, order: dict):
        oid = str(order.get("order_id") or "")
        code = order.get("code", "")
        reason = (f"订单超时守护：{code} 委托 {oid}（{conn_id}）"
                  f"超过 {self.timeout:.0f}s 未成交，自动撤单")
        result = "cancelled"
        try:
            await conn.bridge.call(conn.adapter.cancel_order, oid)
        except Exception as exc:  # noqa: BLE001
            result = f"cancel_failed: {exc}"
            reason += f"（撤单失败：{exc}）"
        log.warning("%s [%s]", reason, result)
        if self.notifier is not None:
            try:
                await self.notifier.notify(
                    "order.timeout", "委托超时自动撤单", reason,
                    {"order_id": oid, "code": code, "conn_id": conn_id, "result": result})
            except Exception:  # noqa: BLE001
                pass
        if self.on_event is not None:
            try:
                await self.on_event("order.timeout", {
                    "order_id": oid, "code": code, "conn_id": conn_id,
                    "message": reason, "result": result})
            except Exception:  # noqa: BLE001
                pass

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            log.info("order watchdog started: timeout=%.0fs interval=%.0fs",
                     self.timeout, self.interval)

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def stats(self) -> dict:
        return {
            "enabled": self.enabled, "timeout": self.timeout,
            "interval": self.interval, "running": self._task is not None,
            "tracking": len(self._first_seen),
            "scans": self._scans, "handled": self._handled,
        }
