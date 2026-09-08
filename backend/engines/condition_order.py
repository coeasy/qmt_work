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
from xtquant_client.order_status import (  # P1-5：调用统一状态词汇表做终态核销
    is_active,
    normalize_order_status,
)

log = logging.getLogger("qmt_work")

_TRIGGER_TYPES = ("gte", "lte")


def _safe_int(v, default: int = 0) -> int:
    """把任意值安全转 int：None/空串/非数字 → default。用于屏蔽历史脏数据（DB 里
    retry_count/intraday_retry 等以空串存储）导致 'invalid literal for int() with base 10'
    崩溃。condition retry 链路的计数一律经本函数读取。"""
    if v is None:
        return default
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if not s:
        return default
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


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


def _tomorrow() -> str:
    """次日日期（YYYY-MM-DD），用于「拒单次日重试」。"""
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


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
        # 阶段 2：拒单/异常进「重试」队列（cid -> order，含 retry_date/retry_count）
        self._retry_queue: dict[str, dict] = {}
        self._retry_limit = 3
        # P1-5 分级重试：当日盘中重试（间隔 30s，上限 5 次）用尽后转次日（上限 _retry_limit）
        self._intraday_interval = 30.0
        self._intraday_retry_limit = 5

    def _wal_append(self, op: str, oid: str, payload: dict):
        if self._wal is not None:
            from gateway.wal import WAL
            if isinstance(self._wal, WAL):
                self._wal.append(op, "condition", oid, payload)

    # ---------------- 生命周期 ----------------
    def load_from_db(self) -> None:
        """恢复未完成且未到期的条件单（重启后继续监控；A3 跨日续作）。

        阶段 2：`status='triggered'` 的记录是「上次触发但下单未完成/未知结果」（崩溃或异常
        中断），同样载入并进「次日重试」队列待恢复，绝不丢弃。
        """
        if self._db is None:
            return
        try:
            rows = self._db.query(
                "SELECT * FROM condition_orders WHERE status IN ('pending','triggered')")
            expired_now = 0
            for r in rows:
                d = dict(r)
                # A3：启动时清理已到期但仍是 pending/triggered 的条件单
                if _is_expired(d.get("expire_at") or ""):
                    d["status"] = "expired"
                    d["expired_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    self._db.execute(
                        "UPDATE condition_orders SET status=?, expired_at=? WHERE id=?",
                        ("expired", d["expired_at"], d["id"]))
                    self._retry_queue.pop(d["id"], None)
                    expired_now += 1
                    continue
                self._orders[d["id"]] = d
                if d.get("status") == "triggered":
                    # 上次触发未完成 → 进次日重试队列。显式归一化来自 DB 的空串计数
                    # （历史脏数据以 '' 存储），屏蔽 setdefault 对「已存在空串 key」不生效的坑。
                    d["retry_count"] = _safe_int(d.get("retry_count", 0))
                    d["intraday_retry"] = _safe_int(d.get("intraday_retry", 0))
                    if not d.get("retry_date"):
                        d["retry_date"] = _tomorrow()
                    self._retry_queue[d["id"]] = d
            if rows:
                log.info("condition orders restored: %d (retry-queued: %d, expired-on-startup: %d)",
                         len(self._orders), len(self._retry_queue), expired_now)
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
            # P1-5：重试/核销字段，新建即初始化，保证 _schedule_retry/_settle_submitted 直接读写
            "retry_count": 0, "retry_date": "", "intraday_retry": 0,
            "next_retry_at": "", "settle_status": "",
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
        if o["status"] in ("pending", "triggered"):
            o["status"] = "canceled"
            self._retry_queue.pop(cid, None)
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
                "valid_days, expire_at, last_check_date, expired_at, retry_date, retry_count, "
                "intraday_retry, next_retry_at, settle_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["id"], o["code"], o["side"], o["trigger_type"], o["trigger_price"],
                 o["price_type"], o["price"], o["volume"], o["status"], o["order_id"],
                 o["remark"], o["created_at"], o["triggered_at"],
                 o.get("valid_days", 0), o.get("expire_at", ""),
                 o.get("last_check_date", ""), o.get("expired_at", ""),
                 o.get("retry_date", ""), o.get("retry_count", 0),
                 o.get("intraday_retry", 0), o.get("next_retry_at", ""),
                 o.get("settle_status", "")))
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
                # P1-5：已受理（submitted）订单对账核销 —— 查询券商当日委托，
                # 若已到终态（filled/canceled/part_filled/rejected）则回写条件单并推事件。
                await self._settle_submitted(b)
                # 重试队列（盘中等时重试 + 次日重试）
                if self._retry_queue:
                    for cid, o in list(self._retry_queue.items()):
                        if b is None:
                            continue
                        if _is_expired(o.get("expire_at") or ""):
                            await self._expire(o)
                            continue
                        # 当日盘中重试按 next_retry_at 判定是否到点；次日重试按 retry_date 判定
                        nra = o.get("next_retry_at") or ""
                        if nra:
                            try:
                                if datetime.now() < datetime.fromisoformat(nra):
                                    continue
                            except Exception:  # noqa: BLE001
                                pass
                        elif today < (o.get("retry_date") or ""):
                            continue
                        try:
                            await self._fire(b, o, 0.0, is_retry=True)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("condition retry %s failed: %s", cid, exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("condition loop error: %s", exc)
            interval = float(self._cfg.get("interval", 2.0))
            try:
                from core.state import state
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
        self._retry_queue.pop(o["id"], None)
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

    async def _fire(self, b, o: dict, last_price: float, is_retry: bool = False) -> None:
        """触发条件单下单（阶段 2 加固）。

        - **触发用最新价校验**：下单前重拉一次最新行情，确认触发条件仍成立（lte 止损 /
          gte 突破），避免瞬时脉冲误触发；条件已回退则不成交。
        - **捕获全异常并保留 triggered 记录待恢复**：下单抛任意异常（非仅 BrokerError）
          都不让记录丢失，进「次日重试」队列。
        - **拒单进次日重试队列**：下单被拒（ok=False）也进重试队列，达上限才标 failed。
        """
        # 触发用最新价校验：重新拉最新行情确认条件仍成立
        fresh = 0
        try:
            q = await b.call(b.gateway.get_quote, o["code"])
            fresh = q.get("last") or 0
        except Exception:  # noqa: BLE001
            pass
        if fresh > 0:
            hit = (fresh >= o["trigger_price"]) if o["trigger_type"] == "gte" \
                else (0 < fresh <= o["trigger_price"]) if o["trigger_type"] == "lte" else False
            if not hit:
                if is_retry:
                    self._schedule_retry(o, "触发价已回退，重试时条件不再成立")
                else:
                    # 首次触发校验不通过：保持 pending 继续监控，不误下单
                    o["status"] = "pending"
                    self._persist(o)
                    log.info("condition %s trigger re-check miss (fresh=%s)，保持监控",
                             o["id"], fresh)
                return
            last_price = fresh
        # 首次触发：状态 → triggered 并持久化（保留 triggered 记录待恢复）
        if not is_retry:
            o["status"] = "triggered"
            o["triggered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._persist(o)
            self._wal_append("trigger", o["id"], self._view(o))
            self._emit({"type": "condition_triggered", "data": self._view(o)})
        # 阶段 0-B（F6）：统一经 SignalRouter.submit()，由它完成风控 + 幂等 + 审计 + 真实下单。
        try:
            from core.state import state
            sr = state.signal_router
            price = o["price"] if (o["price_type"] == "limit" and o["price"] > 0) else last_price
            if sr is None:
                raise RuntimeError("信号路由器未初始化")
            res = await sr.submit(o["code"], o["side"], o["volume"], price, o["price_type"],
                                  source="condition", broker_id="",
                                  remark=o["remark"] or "条件单触发", auto_confirm=True)
        except Exception as exc:  # noqa: BLE001 —— 捕获全异常（含超时/网络/引擎异常）
            self._schedule_retry(o, f"下单异常：{exc}")
            return
        if not res.get("ok"):
            self._schedule_retry(o, f"下单被拒：{res.get('reason', '')}")
            return
        o["order_id"] = res.get("order_id", "")
        # P1-5：拿到 order_id 仅代表「已受理」，未成交不可标 filled。
        # 置 submitted 并写 WAL（entity=condition, op=order），OrderReconciler 依据该记录
        # 与券商当日委托对账；_settle_submitted 把终态回写条件单。
        o["status"] = "submitted"
        o["intraday_retry"] = 0
        o["next_retry_at"] = ""
        o.setdefault("settle_status", "")
        self._retry_queue.pop(o["id"], None)
        self._persist(o)
        self._wal_append("order", o["id"], {"order_id": o["order_id"], "status": "submitted"})
        self._emit({"type": "condition_order", "data": self._view(o)})
        self._audit("condition.triggered", o, f"order_id={o['order_id']}（已受理，待成交）")

    # ---------------- P1-5：submitted 订单终态核销 ----------------
    async def _settle_submitted(self, b) -> None:
        """已受理（submitted）订单对账核销。

        拉取券商当日委托，捞出 order_id 对应的现行行；若已到终态
        （filled / cancelled / rejected / part_filled）则把条件单回写为终态并推事件，
        避免"已受理"停留在半途、撤单/部成不被反映。
        """
        subs = [o for o in self._orders.values()
                if o.get("status") == "submitted" and o.get("order_id")]
        if not subs:
            return
        rows: dict[str, dict] = {}
        try:
            rr = await b.call_locked(b.gateway.get_orders)
            for r in rr or []:
                oid = str(r.get("order_id") or r.get("id") or "")
                if oid:
                    rows[oid] = r
        except Exception as exc:  # noqa: BLE001
            log.debug("settle get_orders failed: %s", exc)
            return
        for o in subs:
            oid = o.get("order_id", "")
            row = rows.get(oid)
            if not row:
                continue
            status = normalize_order_status(row.get("status") or row.get("order_status"))
            if is_active(status):
                # 仍在挂单（pending/partial），不核销，继续跟踪
                continue
            o["status"] = status
            o["settle_status"] = status
            self._persist(o)
            self._wal_append("settle", o["id"],
                             {"order_id": oid, "status": status, "settle_status": status})
            self._emit({"type": "condition_settled", "data": self._view(o)})
            self._audit("condition.settled", o, f"order_id={oid} status={status}")
            log.info("condition %s 核销为终态: %s", o["id"], status)

    def _schedule_retry(self, o: dict, reason: str) -> None:
        """拒单/异常 → 分级重试（P1-5）。

        - 当日盘中重试：间隔 ``_intraday_interval``（30s），上限 ``_intraday_retry_limit``（5）
          次，止损/突破单避免风险敞口拖到次日（新增 ``intraday_retry`` 计数）；
        - 盘中次数用尽转次日重试：``retry_count`` 累计跨日重试次数（语义不变），
          上限 ``_retry_limit``（3）；
        - 达跨日上限则标 ``failed`` 不再重试。
        """
        intraday = _safe_int(o.get("intraday_retry", 0))
        o["remark"] = reason
        if intraday < self._intraday_retry_limit:
            o["intraday_retry"] = intraday + 1
            o["status"] = "triggered"          # 保留触发记录待恢复
            o["next_retry_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + self._intraday_interval))
            o["retry_date"] = _today()         # 盘中重试仍属当日
            self._retry_queue[o["id"]] = o
            log.info("condition %s 当日盘中重试(%d/%d，%ds后): %s",
                     o["id"], intraday + 1, self._intraday_retry_limit,
                     self._intraday_interval, reason)
        elif _safe_int(o.get("retry_count", 0)) + 1 >= self._retry_limit:
            o["status"] = "failed"
            self._retry_queue.pop(o["id"], None)
            log.warning("condition %s 达跨日重试上限(%d)，标记 failed: %s",
                        o["id"], self._retry_limit, reason)
        else:
            # 当日盘中 5 次用尽 → 转次日重试，重置盘中计数
            o["intraday_retry"] = 0
            o["retry_count"] = _safe_int(o.get("retry_count", 0)) + 1
            o["next_retry_at"] = ""
            o["status"] = "triggered"
            o["retry_date"] = _tomorrow()      # 次日重试
            self._retry_queue[o["id"]] = o
            log.info("condition %s 进次日重试队列(%d/%d): %s",
                     o["id"], o["retry_count"], self._retry_limit, reason)
        self._persist(o)
        self._wal_append("error", o["id"], {"status": o["status"], "error": reason,
                                            "retry_count": o.get("retry_count", 0),
                                            "intraday_retry": o.get("intraday_retry", 0),
                                            "retry_date": o.get("retry_date", ""),
                                            "next_retry_at": o.get("next_retry_at", "")})
        self._emit({"type": "condition_failed", "data": self._view(o)})
        self._audit("condition.failed", o, reason)

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
    from core.state import state
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
