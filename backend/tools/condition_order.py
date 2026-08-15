"""条件单 / 止损单引擎（工业级交易闭环）。

- 条件触发：trigger_type=gte（价格 ≥ 触发价，如突破买入/止损卖出）/ lte（价格 ≤ 触发价）
- 触发后经统一风控下单（place_order），状态持久化 condition_orders 表（重启恢复 pending）
- 轮询真实行情（get_quote，默认 2s）；事件 + 审计
- A3 跨日续作与到期：valid_days 指定有效期（0=仅当日），到期自动失效并通知；
  跨自然日 pending 条件单继续监控（不会因换日而丢失）
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta

from xtquant_client.base import BrokerError

log = logging.getLogger("qmt_work")

_TRIGGER_TYPES = ("gte", "lte")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _end_of_day(dt: str = "") -> str:
    """返回给定日期（或今天）的 23:59:59 本地时间 ISO 串。"""
    base = datetime.fromisoformat(dt) if dt else datetime.now()
    return base.replace(hour=23, minute=59, second=59).strftime("%Y-%m-%dT%H:%M:%S")


def _compute_expire(valid_days: int) -> str:
    """计算到期时间：valid_days<=0 当日 23:59:59；>0 则从今天起 N 天后 23:59:59。"""
    now = datetime.now()
    if valid_days and valid_days > 0:
        target = now + timedelta(days=int(valid_days))
    else:
        target = now
    return _end_of_day(target.strftime("%Y-%m-%dT00:00:00"))


def _is_expired(expire_at: str) -> bool:
    if not expire_at:
        return False
    try:
        return datetime.now() > datetime.fromisoformat(expire_at)
    except Exception:  # noqa: BLE001
        return False


class ConditionOrderEngine:
    """条件单引擎（事件循环内轮询真实行情触发）。"""

    def __init__(self, manager, risk=None, db=None, on_event=None, wal=None, notifier=None):
        self._manager = manager
        self._risk = risk
        self._db = db
        self._on_event = on_event
        self._wal = wal
        self._notifier = notifier      # A3 到期通知
        self._orders: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._cfg: dict = {"interval": 2.0}

    def _wal_append(self, op: str, oid: str, payload: dict):
        if self._wal is not None:
            from gateway.wal import WAL
            if isinstance(self._wal, WAL):
                self._wal.append(op, "condition", oid, payload)

    # ---------------- 生命周期 ----------------
    def load_from_db(self) -> None:
        """恢复未完成且未到期的条件单（重启后继续监控；A3 跨日续作）。"""
        if self._db is None:
            return
        try:
            rows = self._db.query("SELECT * FROM condition_orders WHERE status='pending'")
            expired_now = 0
            for r in rows:
                d = dict(r)
                # A3：启动时清理已到期但仍是 pending 的条件单
                if _is_expired(d.get("expire_at") or ""):
                    d["status"] = "expired"
                    d["expired_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    self._db.execute(
                        "UPDATE condition_orders SET status=?, expired_at=? WHERE id=?",
                        ("expired", d["expired_at"], d["id"]))
                    expired_now += 1
                    continue
                self._orders[d["id"]] = d
            if rows:
                log.info("condition orders restored: %d (expired-on-startup: %d)",
                         len(self._orders), expired_now)
        except Exception as exc:  # noqa: BLE001
            log.warning("condition orders restore failed: %s", exc)

    async def start(self, interval: float = 2.0) -> dict:
        await self.stop()
        self._cfg["interval"] = max(0.5, float(interval))
        self._task = asyncio.create_task(self._loop())
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

    def status(self) -> dict:
        pending = [o for o in self._orders.values() if o.get("status") == "pending"]
        return {"running": self._task is not None and not self._task.done(),
                "interval": self._cfg.get("interval", 2.0),
                "total": len(self._orders), "pending": len(pending),
                "orders": [self._view(o) for o in self._orders.values()]}

    @staticmethod
    def _view(o: dict) -> dict:
        return {k: v for k, v in o.items()}

    # ---------------- 提交/取消 ----------------
    def submit(self, code: str, side: str, trigger_type: str, trigger_price: float,
               volume: int, price_type: str = "market", price: float = 0.0,
               remark: str = "", valid_days: int = 0) -> dict:
        code = (code or "").strip().upper()
        side = (side or "").lower()
        trigger_type = (trigger_type or "").lower()
        if not code:
            raise ValueError("代码不能为空")
        if side not in ("buy", "sell"):
            raise ValueError("side 须为 buy/sell")
        if trigger_type not in _TRIGGER_TYPES:
            raise ValueError(f"trigger_type 须为 {_TRIGGER_TYPES}")
        if float(trigger_price) <= 0:
            raise ValueError("trigger_price 必须为正")
        volume = int(volume)
        if volume <= 0 or volume % 100 != 0:
            raise ValueError("volume 须为 100 的整数倍")
        try:
            valid_days = int(valid_days)
        except (TypeError, ValueError):
            valid_days = 0
        cid = uuid.uuid4().hex[:12]
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        order = {
            "id": cid, "code": code, "side": side, "trigger_type": trigger_type,
            "trigger_price": float(trigger_price), "price_type": price_type,
            "price": float(price or 0), "volume": volume,
            "status": "pending", "order_id": "", "remark": remark,
            "created_at": created_at, "triggered_at": "",
            "valid_days": valid_days, "expire_at": _compute_expire(valid_days),
            "last_check_date": _today(), "expired_at": "",
        }
        self._orders[cid] = order
        self._persist(order)
        self._wal_append("create", cid, self._view(order))
        self._emit({"type": "condition_created", "data": self._view(order)})
        return {"id": cid, "status": "pending", "expire_at": order["expire_at"]}

    def cancel(self, cid: str) -> dict:
        o = self._orders.get(cid)
        if not o:
            raise KeyError(f"未知条件单：{cid}")
        if o["status"] == "pending":
            o["status"] = "canceled"
            self._persist(o)
            self._wal_append("cancel", cid, {"status": "canceled"})
        return {"id": cid, "status": o["status"]}

    def _persist(self, o: dict) -> None:
        if self._db is None:
            return
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO condition_orders "
                "(id, code, side, trigger_type, trigger_price, price_type, price, "
                "volume, status, order_id, remark, created_at, triggered_at, "
                "valid_days, expire_at, last_check_date, expired_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["id"], o["code"], o["side"], o["trigger_type"], o["trigger_price"],
                 o["price_type"], o["price"], o["volume"], o["status"], o["order_id"],
                 o["remark"], o["created_at"], o["triggered_at"],
                 o.get("valid_days", 0), o.get("expire_at", ""),
                 o.get("last_check_date", ""), o.get("expired_at", "")))
        except Exception as exc:  # noqa: BLE001
            log.warning("condition order persist failed: %s", exc)

    # ---------------- 核心循环 ----------------
    async def _loop(self):
        while True:
            try:
                b = self._manager.active_bridge()
                today = _today()
                pending = [o for o in self._orders.values() if o["status"] == "pending"]
                for o in pending:
                    # A3：跨日续作 —— 记录当日检查日期
                    if o.get("last_check_date") != today:
                        o["last_check_date"] = today
                        self._persist(o)
                    # A3：到期自动失效
                    if _is_expired(o.get("expire_at") or ""):
                        await self._expire(o)
                        continue
                    if b is None:
                        continue
                    try:
                        q = await b.call(b.gateway.get_quote, o["code"])
                        last = q.get("last") or 0
                        hit = (last >= o["trigger_price"]) if o["trigger_type"] == "gte" \
                            else (0 < last <= o["trigger_price"]) if o["trigger_type"] == "lte" else False
                        if hit:
                            await self._fire(b, o, last)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("condition check %s failed: %s", o["code"], exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("condition loop error: %s", exc)
            interval = float(self._cfg.get("interval", 2.0))
            try:
                from app.state import state
                if state.runtime_config is not None:
                    interval = state.runtime_config.condition_interval
            except Exception:  # noqa: BLE001
                pass
            # 盘中按业务间隔轮询；非交易时段降频探活
            from gateway.trading_session import default_session
            await asyncio.sleep(default_session.sleep_seconds(interval, 30.0))

    async def _expire(self, o: dict) -> None:
        """A3：条件单到期失效，写库 + 推送 + 通知。"""
        o["status"] = "expired"
        o["expired_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._persist(o)
        self._wal_append("expire", o["id"], {"status": "expired", "expired_at": o["expired_at"]})
        self._emit({"type": "condition_expired", "data": self._view(o)})
        self._audit("condition.expired", o, f"expire_at={o.get('expire_at')}")
        if self._notifier:
            try:
                await self._notifier.notify(
                    "condition.expired", "条件单到期失效",
                    f"{o['code']} {o['side']} {o['volume']} 条件单已到期未触发（有效期至 {o.get('expire_at')}）",
                    {"id": o["id"], "code": o["code"], "side": o["side"],
                     "volume": o["volume"], "trigger_price": o["trigger_price"]})
            except Exception:  # noqa: BLE001
                pass

    async def _fire(self, b, o: dict, last_price: float) -> None:
        o["status"] = "triggered"
        o["triggered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._persist(o)
        self._wal_append("trigger", o["id"], self._view(o))
        self._emit({"type": "condition_triggered", "data": self._view(o)})
        # 过风控
        risk = self._risk
        if risk is not None:
            ok, reason = risk.check_order(o["code"], o["trigger_price"], o["volume"], o["side"])
            if not ok:
                o["status"] = "failed"
                o["remark"] = f"风控拒绝：{reason}"
                self._persist(o)
                self._emit({"type": "condition_failed", "data": self._view(o)})
                self._audit("condition.rejected", o, reason)
                return
        try:
            price = o["price"] if (o["price_type"] == "limit" and o["price"] > 0) else last_price
            res = await b.call(b.gateway.place_order, o["code"], o["side"],
                               o["price_type"], price, o["volume"], "condition", o["remark"])
            o["order_id"] = res.get("order_id", "")
            self._persist(o)
            self._wal_append("order", o["id"], {"order_id": o["order_id"], "status": "filled"})
            self._emit({"type": "condition_order", "data": self._view(o)})
            self._audit("condition.triggered", o, f"order_id={o['order_id']}")
        except BrokerError as exc:
            o["status"] = "failed"
            o["remark"] = str(exc)
            self._persist(o)
            self._wal_append("error", o["id"], {"status": "failed", "error": str(exc)})
            self._emit({"type": "condition_failed", "data": self._view(o)})
            self._audit("condition.failed", o, str(exc))

    def _audit(self, action: str, o: dict, result: str) -> None:
        if self._db is not None:
            try:
                self._db.audit("condition", action, o["code"],
                               {k: o.get(k) for k in ("id", "side", "trigger_type",
                                                      "trigger_price", "volume")}, result)
            except Exception:  # noqa: BLE001
                pass

    def _emit(self, event: dict) -> None:
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001
                pass


def _engine():
    from app.state import state
    if state.condition_engine is None:
        raise BrokerError("条件单引擎未初始化")
    return state.condition_engine


def register_condition_tools(mcp):
    @mcp.tool()
    async def condition_submit(code: str, side: str, trigger_type: str,
                               trigger_price: float, volume: int,
                               price_type: str = "market", price: float = 0.0,
                               remark: str = "", valid_days: int = 0) -> dict:
        """提交条件单/止损单：价格达到 trigger_price（gte≥ / lte≤）时自动下单（过风控）。

        valid_days：有效期天数（0=仅当日有效；>0=跨日续作，到期自动失效并通知）。
        """
        return _engine().submit(code, side, trigger_type, trigger_price, volume,
                                price_type, price, remark, valid_days=valid_days)

    @mcp.tool()
    async def condition_cancel(condition_id: str) -> dict:
        """取消挂起中的条件单。"""
        return _engine().cancel(condition_id)

    @mcp.tool()
    async def condition_list() -> list[dict]:
        """列出全部条件单及状态。"""
        return _engine().status().get("orders", [])
