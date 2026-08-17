"""涨停监控 / 打板助手（借鉴 123quant/QMT-QuantLimit 核心逻辑，真实行情实现）。

- 股票池管理：添加/移除监控代码
- 轮询真实行情（get_full_tick）判断涨停：
    last >= 涨停价(昨收*(1+limit_pct))  &&  时间 <= 截止  &&  近 N tick 涨幅 >= min_rise
- 触发后：WS 广播事件 + 可选自动涨停价限价买入（过风控金额/数量检查）
"""
import asyncio
import logging
import time
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
        self._ticks: dict[str, deque] = {}       # code -> 最近 last 序列（含涨停前）
        self._triggered: set[str] = set()
        self._events: deque = deque(maxlen=200)
        self._cfg: dict = {}
        self._task: asyncio.Task | None = None
        # 阶段 2：_auto_buy 任务托管——stop()/停机时统一取消，避免停机后打板任务失控
        self._tasks: dict[str, asyncio.Task] = {}

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
        # 阶段 2：统一取消托管的打板任务，停机后不再有游离下单
        for t in list(self._tasks.values()):
            if t and not t.done():
                t.cancel()
        if self._tasks:
            try:
                await asyncio.gather(*[t for t in self._tasks.values() if not t.done()],
                                     return_exceptions=True)
            except Exception:  # noqa: BLE001
                pass
        self._tasks.clear()
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
        from gateway.trading_session import default_session
        while True:
            # 阶段 2：交易日 + 盘中时段判定——非交易日/非盘中不判定涨停、不触发打板下单
            if not default_session.is_active():
                await asyncio.sleep(max(float(self._cfg.get("interval", 2.0)), 30.0))
                continue
            try:
                b = self._manager.active_bridge()
                if b is not None and self._pool:
                    ticks = await b.call(b.gateway.get_full_tick, list(self._pool.keys()))
                    in_window = datetime.now().strftime("%H:%M") <= self._cfg.get("cutoff", "10:00")
                    for code, q in (ticks or {}).items():
                        self._check(code, q, in_window)
            except Exception as exc:  # noqa: BLE001
                log.warning("limitup loop error: %s", exc)
            await asyncio.sleep(float(self._cfg.get("interval", 2.0)))

    def _check(self, code: str, q: dict, in_window: bool) -> None:
        last = q.get("last") or 0
        lc = q.get("lastClose") or 0
        if not last or not lc:
            return
        # 阶段 2（F14）：记录**全部** tick（含涨停前的），涨幅基准取自「触发前」序列。
        # 原实现只记录已涨停的 tick，buf 内价格≈涨停价 → rise 恒≈0 < min_rise（默认 3%），
        # 涨停监控在默认配置下永远无法触发。
        buf = self._ticks.setdefault(code, deque(maxlen=25))
        buf.append(last)
        # 涨停幅度：显式配置 > 按板块自动（主板10/创业科创20/北交30/ST5）
        cfg_pct = float(self._cfg.get("limit_pct", 0.0) or 0.0)
        pct = cfg_pct if cfg_pct > 0 else _limit_factor(code, self._pool.get(code, ""))
        limit = round(lc * (1 + pct), 2)
        if last < limit - 1e-9:
            return
        min_rise = float(self._cfg.get("min_rise", 0.03))
        min_ticks = int(self._cfg.get("min_ticks", 10))
        # 涨幅 = 当前价 / 缓冲区最早价 - 1（最早价即涨停前参考点）
        ref = float(buf[0]) if buf else 0.0
        rise = (last / ref - 1) if ref else 0.0
        rise_ok = len(buf) >= min_ticks and rise >= min_rise
        if not (in_window and rise_ok):
            return
        if code in self._triggered:
            return
        self._triggered.add(code)
        event = {"code": code, "price": last, "limit": limit, "rise": round(rise, 4),
                 "ts": datetime.now().isoformat(timespec="seconds")}
        self._events.append(event)
        self._wal_append("trigger", code, event)
        self._emit({"type": "limitup", "data": event})
        if self._cfg.get("do_trade") and int(self._cfg.get("buy_volume", 0)) > 0:
            # 阶段 2：任务托管——登记句柄，stop()/停机统一取消
            t = asyncio.create_task(self._auto_buy(code, limit))
            self._tasks[code] = t
            t.add_done_callback(lambda _t, c=code: self._tasks.pop(c, None))

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
            # 阶段 0-B（F7）：经统一入口提交，携带完整风控（熔断/频率/持仓比例/黑白名单/
            # 日额度），不再直接 gateway.place_order 绕过。auto_confirm=True 跳过人工 TOTP。
            from app.state import state
            res = await state.signal_router.submit(
                code, "buy", vol, float(limit_price), "limit",
                source="limitup", remark="打板自动买入", auto_confirm=True)
            self._emit({"type": "limitup_order", "data": {
                "code": code, "order_id": res.get("order_id"),
                "price": limit_price, "volume": vol}})
            self._wal_append("order", f"{code}:{res.get('order_id')}",
                             {"code": code, "side": "buy", "price": limit_price,
                              "volume": vol, "order_id": res.get("order_id")})
            self._audit("limitup.buy", code,
                        {"limit_price": limit_price, "volume": vol},
                        f"order_id={res.get('order_id')}")
        except Exception as exc:  # noqa: BLE001 —— 捕获全异常（含 BrokerError/超时/引擎异常）
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
    async def limitup_start(limit_pct: float = 0.0, cutoff: str = "10:00",
                            min_rise: float = 0.03, buy_volume: int = 0,
                            do_trade: bool = False, interval: float = 2.0) -> dict:
        """启动涨停监控：last>=涨停价 且 时间<=cutoff 且 近N个tick涨幅>=min_rise 时触发；
        涨停幅度 limit_pct=0（默认）时按板块自动判定（主板10%/创业板科创20%/北交所30%/ST5%）；
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


# ---------------- 涨停板（盘口扫描，真实行情） ----------------
# 记录每只票首次触及涨停的时间（用于展示「涨停时长」）。
_LIMIT_FIRST_SEEN: dict = {}


def _limit_factor(code: str, name: str | None = None) -> float:
    """按代码前缀估算涨停幅度：科创板/创业板 20%，北交所 30%，ST 5%，其余 10%。

    name 用于识别 ST（名称含 ST / *ST），优先于代码前缀。
    """
    if name and "ST" in (name or "").upper():
        return 0.05
    c = (code or "").upper()
    if c.startswith("68") or c.startswith("30"):
        return 0.20
    if c.startswith("8") or c.startswith("4") or c.startswith("92"):
        return 0.30
    return 0.10


async def scan_limit_up(bridge, sector: str = "沪深A股", min_pct: float = 9.5,
                        only_limit: bool = True, limit: int = 200,
                        sort: str = "change") -> list[dict]:
    """扫描板块内最新行情，列出涨停（或接近涨停）个股及最新数据。真实行情，无 mock。

    - 通过 get_sector_stocks 取板块成分，分块 get_full_tick 拉全市场快照；
    - 按昨收*(1+板块幅度) 估算涨停价，last>=涨停价-0.01 视为涨停；
    - 仅对过滤后的小集合逐个查 get_instrument_detail 补名称与精确涨停价；
    - 返回按涨跌幅/成交额排序的列表，含封单量、封单额、涨停时长，便于快速选股交易。
    """
    if bridge is None:
        raise BrokerError("未连接任何券商客户端")
    g = bridge.gateway
    codes = await bridge.call(g.get_sector_stocks, sector) or []
    if not codes:
        return []
    CHUNK = 300
    ticks: dict = {}
    for i in range(0, len(codes), CHUNK):
        sub = await bridge.call(g.get_full_tick, codes[i:i + CHUNK]) or {}
        if sub:
            ticks.update(sub)

    now = time.time()
    rows: list = []
    for code, q in ticks.items():
        last = q.get("last")
        lc = q.get("lastClose")
        if not last or not lc:
            continue
        try:
            last_f = float(last); lc_f = float(lc)
        except (TypeError, ValueError):
            continue
        pct = (last_f - lc_f) / lc_f * 100.0
        if pct < min_pct:
            _LIMIT_FIRST_SEEN.pop((sector, code), None)
            continue
        factor = _limit_factor(code)
        limit_price = round(lc_f * (1 + factor), 2)
        is_limit = last_f >= limit_price - 0.01
        if only_limit and not is_limit:
            _LIMIT_FIRST_SEEN.pop((sector, code), None)
            continue
        key = (sector, code)
        if is_limit:
            if key not in _LIMIT_FIRST_SEEN:
                _LIMIT_FIRST_SEEN[key] = now
            limit_seconds = int(now - _LIMIT_FIRST_SEEN[key])
        else:
            _LIMIT_FIRST_SEEN.pop(key, None)
            limit_seconds = 0
        bid_vol = q.get("bid_vol") or 0
        rows.append({
            "code": code,
            "name": code,
            "last": round(last_f, 2),
            "pre_close": round(lc_f, 2),
            "change_pct": round(pct, 2),
            "limit_price": limit_price,
            "is_limit": is_limit,
            "amount": q.get("amount"),
            "bid_vol": bid_vol,
            "bid_amount": round(bid_vol * limit_price, 0),
            "limit_seconds": limit_seconds,
            "ts": q.get("ts"),
        })
    # 仅对过滤后的小集合补全名称 + 精确涨停价
    for r in rows:
        try:
            d = await bridge.call(g.get_instrument_detail, r["code"]) or {}
            if d.get("name"):
                r["name"] = d["name"]
            if d.get("up_limit_price"):
                r["limit_price"] = round(float(d["up_limit_price"]), 2)
        except Exception:  # noqa: BLE001
            pass
    if sort == "amount":
        rows.sort(key=lambda x: (x.get("amount") or 0), reverse=True)
    else:
        rows.sort(key=lambda x: x["change_pct"], reverse=True)
    return rows[: int(limit)]
