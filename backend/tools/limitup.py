"""涨停监控 / 打板助手（借鉴 123quant/QMT-QuantLimit 核心逻辑，真实行情实现）。

- 股票池管理：添加/移除监控代码
- 轮询真实行情（get_full_tick）判断涨停：
    last >= 涨停价(昨收*(1+limit_pct))  &&  时间 <= 截止  &&  近 N tick 涨幅 >= min_rise
- 触发后：WS 广播事件 + 可选自动涨停价限价买入（过风控金额/数量检查）
"""
import asyncio
import logging
from collections import deque
from datetime import datetime

from xtquant_client.base import BrokerError

log = logging.getLogger("qmt_work")


class LimitUpMonitor:
    """打板监控器（事件循环内轮询，无假数据）。"""

    def __init__(self, manager, risk=None, on_event=None, wal=None):
        self._manager = manager
        self._risk = risk
        self._on_event = on_event
        self._wal = wal
        self._pool: dict[str, str] = {}          # code -> name(占位)
        self._ticks: dict[str, deque] = {}       # code -> 最近 last 序列
        self._triggered: set[str] = set()
        self._events: deque = deque(maxlen=200)
        self._cfg: dict = {}
        self._task: asyncio.Task | None = None

    def _wal_append(self, op: str, entity_id: str, payload: dict):
        if self._wal is not None:
            from gateway.wal import WAL
            if isinstance(self._wal, WAL):
                self._wal.append(op, "limitup", entity_id, payload)

    # ---------------- 状态 ----------------
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "interval": self._cfg.get("interval", 2.0),
            "limit_pct": self._cfg.get("limit_pct", 0.1),
            "cutoff": self._cfg.get("cutoff", "10:00"),
            "min_rise": self._cfg.get("min_rise", 0.03),
            "buy_volume": self._cfg.get("buy_volume", 0),
            "do_trade": bool(self._cfg.get("do_trade", False)),
            "pool": [{"code": c, "name": n} for c, n in self._pool.items()],
            "total_triggered": len(self._triggered),
            "events": list(self._events)[-50:],
        }

    # ---------------- 股票池 ----------------
    def add(self, code: str, name: str = "") -> dict:
        code = (code or "").strip().upper()
        if not code:
            raise ValueError("代码不能为空")
        self._pool[code] = name or code
        self._ticks.setdefault(code, deque(maxlen=25))
        return {"code": code, "name": self._pool[code]}

    def remove(self, code: str) -> None:
        code = (code or "").strip().upper()
        self._pool.pop(code, None)
        self._ticks.pop(code, None)
        self._triggered.discard(code)

    def clear(self) -> None:
        self._pool.clear()
        self._ticks.clear()
        self._triggered.clear()

    def reset_triggered(self) -> None:
        """清空当日触发记录（新交易日/重新打板前调用）。"""
        self._triggered.clear()

    # ---------------- 启停 ----------------
    async def start(self, cfg: dict | None = None) -> dict:
        await self.stop()
        self._cfg = dict(cfg or {})
        if not self._pool:
            raise ValueError("股票池为空，请先添加监控代码")
        self._triggered.clear()
        self._task = asyncio.create_task(self._loop())
        log.info("limitup monitor started: pool=%s cfg=%s", len(self._pool), self._cfg)
        return self.status()

    async def stop(self) -> dict:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        return self.status()

    # ---------------- 核心循环 ----------------
    async def _loop(self):
        while True:
            try:
                b = self._manager.active_bridge()
                if b is not None and self._pool:
                    ticks = await b.call(b.gateway.get_full_tick, list(self._pool.keys()))
                    in_window = datetime.now().strftime("%H:%M") <= self._cfg.get("cutoff", "10:00")
                    for code, q in (ticks or {}).items():
                        self._check(code, q, in_window)
            except Exception as exc:  # noqa: BLE001
                log.warning("limitup loop error: %s", exc)
            # 盘中高频轮询；非交易时段降频探活（省资源、减券商压力）
            from gateway.trading_session import default_session
            iv = default_session.sleep_seconds(
                float(self._cfg.get("interval", 2.0)), 30.0)
            await asyncio.sleep(iv)

    def _check(self, code: str, q: dict, in_window: bool) -> None:
        last = q.get("last") or 0
        lc = q.get("lastClose") or 0
        if not last or not lc:
            return
        pct = float(self._cfg.get("limit_pct", 0.1))
        limit = round(lc * (1 + pct), 2)
        if last < limit - 1e-9:
            return
        buf = self._ticks.setdefault(code, deque(maxlen=25))
        buf.append(last)
        min_rise = float(self._cfg.get("min_rise", 0.03))
        rise_ok = len(buf) >= 25 and (buf[-1] / buf[0] - 1) >= min_rise
        if not (in_window and rise_ok):
            return
        if code in self._triggered:
            return
        self._triggered.add(code)
        event = {"code": code, "price": last, "limit": limit,
                 "ts": datetime.now().isoformat(timespec="seconds")}
        self._events.append(event)
        self._wal_append("trigger", code, event)
        self._emit({"type": "limitup", "data": event})
        if self._cfg.get("do_trade") and int(self._cfg.get("buy_volume", 0)) > 0:
            asyncio.create_task(self._auto_buy(code, limit))

    async def _auto_buy(self, code: str, limit_price: float) -> None:
        b = self._manager.active_bridge()
        if b is None:
            return
        vol = (int(self._cfg.get("buy_volume", 0)) // 100) * 100
        if vol <= 0:
            return
        try:
            risk = self._risk
            if risk is not None:
                cash = (await b.call(b.gateway.get_cash)).get("cash", 0) or 0
                if limit_price * vol > min(float(getattr(risk, "max_amount", 1e9)), float(cash)):
                    self._emit({"type": "limitup_order", "data": {
                        "code": code, "error": "金额超风控上限，未下单"}})
                    self._audit("limitup.buy_rejected", code,
                                {"limit_price": limit_price, "volume": vol},
                                "金额超风控上限")
                    return
            res = await b.call(b.gateway.place_order, code, "buy", "limit",
                               float(limit_price), vol, "limitup", "打板自动买入")
            self._emit({"type": "limitup_order", "data": {
                "code": code, "order_id": res.get("order_id"),
                "price": limit_price, "volume": vol}})
            self._wal_append("order", f"{code}:{res.get('order_id')}",
                             {"code": code, "side": "buy", "price": limit_price,
                              "volume": vol, "order_id": res.get("order_id")})
            self._audit("limitup.buy", code,
                        {"limit_price": limit_price, "volume": vol},
                        f"order_id={res.get('order_id')}")
        except BrokerError as exc:
            self._emit({"type": "limitup_order", "data": {"code": code, "error": str(exc)}})
            self._wal_append("error", code, {"error": str(exc), "price": limit_price, "volume": vol})
            self._audit("limitup.buy_failed", code,
                        {"limit_price": limit_price, "volume": vol}, str(exc))

    @staticmethod
    def _audit(action: str, target: str, params: dict, result: str) -> None:
        from app.state import state
        if state.db is not None:
            try:
                state.db.audit("limitup", action, target, params, result)
            except Exception:  # noqa: BLE001
                pass

    def _emit(self, event: dict) -> None:
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001
                pass


def _monitor():
    from app.state import state
    if state.limitup_monitor is None:
        raise BrokerError("涨停监控未初始化")
    return state.limitup_monitor


def register_limitup_tools(mcp):
    @mcp.tool()
    async def limitup_pool_add(code: str) -> dict:
        """添加代码到涨停监控股票池。"""
        return _monitor().add(code)

    @mcp.tool()
    async def limitup_pool_remove(code: str) -> str:
        """从涨停监控股票池移除代码。"""
        _monitor().remove(code)
        return "ok"

    @mcp.tool()
    async def limitup_start(limit_pct: float = 0.1, cutoff: str = "10:00",
                            min_rise: float = 0.03, buy_volume: int = 0,
                            do_trade: bool = False, interval: float = 2.0) -> dict:
        """启动涨停监控：last>=涨停价 且 时间<=cutoff 且 近25个tick涨幅>=min_rise 时触发；
        buy_volume>0 且 do_trade=True 时自动涨停价限价买入。"""
        return await _monitor().start({
            "limit_pct": limit_pct, "cutoff": cutoff, "min_rise": min_rise,
            "buy_volume": buy_volume, "do_trade": do_trade, "interval": interval})

    @mcp.tool()
    async def limitup_stop() -> dict:
        """停止涨停监控。"""
        return await _monitor().stop()

    @mcp.tool()
    async def limitup_status() -> dict:
        """涨停监控状态（运行中/参数/股票池/已触发事件）。"""
        return _monitor().status()
