"""数据同步引擎（Phase 2）+ WebSocket 多端一致同步（多券商/多账户）。

- 订阅聚合：多客户端订阅同一标的，只向活跃券商订阅一次，再扇出广播
- 行情事件：券商回调 -> bridge 队列 -> 事件循环 -> 写缓存(market_cache) + 广播
- 账户快照：遍历所有已连接券商账户，定时归档(account_snapshot) + 广播
- WS 协议：连接即发全量快照；订阅/退订消息；断线重连补发
"""
import asyncio
import json
import logging
import time
from collections import deque

from fastapi import WebSocket

log = logging.getLogger("qmt_work.sync")


class SyncEngine:
    def __init__(self, manager, db, quote_bus=None, risk=None, notifier=None,
                 webhook_out=None, runtime_config=None):
        self.manager = manager
        self.db = db
        self.risk = risk                                 # 日亏损熔断需要净值上报（B4）
        self.notifier = notifier
        self.webhook_out = webhook_out                   # B2 事件出站 webhook
        self.runtime_config = runtime_config             # 运行时配置（热更新）
        self.latest_quotes: dict[str, dict] = {}
        self._subscribed_codes: set[str] = set()
        self._client_subscriptions: dict[str, set[str]] = {}
        self._notify_handlers: list = []
        self._account_task: asyncio.Task | None = None
        self._order_fp: dict[str, dict[str, str]] = {}   # account -> {order_id: status}
        self._deal_seen: dict[str, set] = {}             # account -> {deal_key}
        self._fp_date: str = ""                          # 阶段 2：指纹归属交易日（跨日清理）
        self._last_account_bcast: float = 0.0
        self._quote_bus = quote_bus                      # 可选行情总线（内存/Redis）
        self._latency_stats: dict[str, list[float]] = {} # code -> 最近延迟样本
        self._batch_buf: list[dict] = []                 # 行情微批缓冲（100ms 窗口，C2）
        # C1/P2-1：market_cache 批量写盘缓冲（(code, dtype, ts, payload_json) 行）
        self._batch_rows: list[tuple] = []
        self._batch_task: asyncio.Task | None = None
        self._trade_cbs: set[str] = set()          # 已注册实时成交回调的连接（幂等）

    # ---- 订阅聚合（只向活跃券商订阅一次；引用计数零时退订）----
    def client_subscribe(self, client_id: str, codes: list[str]) -> None:
        sub = self._client_subscriptions.setdefault(client_id, set())
        new = set(codes) - sub
        sub.update(codes)
        if new - self._subscribed_codes:
            self._subscribe_to_qmt(sorted(new - self._subscribed_codes))
        if self._quote_bus:
            for c in codes:
                self._quote_bus.add_ref(c)

    def client_unsubscribe(self, client_id: str, codes: list[str]) -> None:
        sub = self._client_subscriptions.get(client_id)
        if sub:
            sub.difference_update(codes)
            if not sub:
                self._client_subscriptions.pop(client_id, None)
        # 引用计数：扫描所有客户端订阅，零引用的 code 从券商退订
        still_used = set()
        for s in self._client_subscriptions.values():
            still_used |= s
        stale = (self._subscribed_codes - still_used) & set(codes)
        if stale:
            self._unsubscribe_from_qmt(sorted(stale))
        if self._quote_bus:
            for c in codes:
                self._quote_bus.dec_ref(c)

    def _subscribe_to_qmt(self, codes: list[str]) -> None:
        b = self.manager.active_bridge()
        if b is None:
            log.warning("subscribe skipped: 无活跃券商连接")
            return
        try:
            b.gateway.subscribe_quote(codes, lambda evt: b.enqueue(evt))
            # 成功后才标记已订阅（C18）：原实现先 update 再调用，失败后仍认为
            # 「已订阅」→ 永不重试，行情流静默丢失。BridgeAdapter 内部另维护
            # 已订阅集合，子进程重启时会自动重新下发，双保险恢复订阅。
            self._subscribed_codes.update(codes)
            log.info("subscribed to broker: %s", codes)
        except Exception as exc:  # noqa: BLE001
            log.warning("subscribe_quote failed %s: %s", codes, exc)

    def _unsubscribe_from_qmt(self, codes: list[str]) -> None:
        for c in codes:
            self._subscribed_codes.discard(c)
        b = self.manager.active_bridge()
        if b is None:
            return
        try:
            fn = getattr(b.gateway, "unsubscribe_quote", None)
            if fn:
                fn(codes)
                log.info("unsubscribed from broker (refcount=0): %s", codes)
        except Exception as exc:  # noqa: BLE001
            log.warning("unsubscribe_quote failed %s: %s", codes, exc)

    # ---- 事件处理（bridge 泵 -> 本方法）----
    _QUOTE_REQUIRED = {"code", "last"}

    async def on_event(self, event: dict) -> None:
        if event.get("type") != "quote":
            return
        data = event.get("data") or {}
        # Schema Guard：缺少必填字段则丢弃（防止脏数据污染缓存/广播）。
        # 注意：last==0 是合法的停牌/盘前状态值，不作为丢弃理由（只拦 None/""）。
        missing = [k for k in self._QUOTE_REQUIRED
                   if data.get(k) in (None, "") or (k != "last" and data.get(k) == 0)]
        if missing:
            log.debug("quote dropped (schema): %s missing %s", data.get("code"), missing)
            return
        code = data.get("code")
        from gateway.metrics import get_metrics
        get_metrics().record_quote()
        # 延迟监控：quote 事件带 source_ts 时计算端到端延迟
        src_ts = data.pop("source_ts", None)
        if src_ts:
            try:
                latency = (time.time() - float(src_ts)) * 1000
                buf = self._latency_stats.setdefault(code, [])
                buf.append(latency)
                if len(buf) > 100:
                    del buf[:len(buf) - 100]
                if latency > 2000:
                    log.warning("quote latency high %s: %.0fms", code, latency)
                get_metrics().record_quote_latency(latency)
                try:
                    from app import state as _state
                    if _state.alert_engine is not None:
                        _state.alert_engine.evaluate_metric("quote_latency", latency, {"code": code})
                except Exception:  # noqa: BLE001
                    pass
            except (TypeError, ValueError):
                pass
        if not code:
            return
        # P3：自动补中文名（来自本地名称缓存，不依赖券商 get_instrument_detail）。
        # 券商 latest_quotes 通常不含 name 或 name 为空；此处用 eltdx 本地名称缓存兜底，
        # 使 WS 推送 / latest_quotes / 前端面板均能直接显示中文名。O(1) 字典查找，高频安全。
        if not data.get("name"):
            try:
                from app.datasource.manager import get_hub
                _nm = get_hub().lookup_name(code)
                if _nm:
                    data["name"] = _nm
            except Exception:  # noqa: BLE001
                pass
        self.latest_quotes[code] = data
        # 行情总线分发（内存 / Redis 多进程共享）
        if self._quote_bus:
            try:
                self._quote_bus.publish(code, data)
            except Exception as exc:  # noqa: BLE001
                log.debug("quote_bus publish failed: %s", exc)
        # C1/P2-1：market_cache 写入放进 100ms 微批缓冲，由 _batch_loop 单事务批量落盘，
        # 不再每 tick 立即 upsert+commit（消除高频订阅多标的时的写盘风暴）。
        self._batch_rows.append((
            code, "quote",
            data.get("ts", time.strftime("%Y-%m-%dT%H:%M:%S")),
            json.dumps(data, ensure_ascii=False),
        ))
        # 行情微批聚合：100ms 窗口内批量广播（C2），降低高频帧数
        self._batch_buf.append(data)

    def latency_stats(self) -> dict:
        """返回各标的行情延迟统计（avg/p50/p99）。"""
        out = {}
        for code, buf in self._latency_stats.items():
            if not buf:
                continue
            s = sorted(buf)
            out[code] = {
                "count": len(s),
                "avg": round(sum(s) / len(s), 1),
                "p50": round(s[len(s) // 2], 1),
                "p99": round(s[int(len(s) * 0.99)], 1),
            }
        return out

    async def _notify(self, event_type: str, payload: dict, codes: list[str] | None = None) -> None:
        for handler in self._notify_handlers:
            try:
                await handler(event_type, payload, codes)
            except Exception as exc:
                log.warning("notify handler error: %s", exc)
        # B2：订单/成交/账户事件同步投递给出站 webhook
        if self.webhook_out is not None and event_type in ("order", "deal", "account", "risk"):
            try:
                await self.webhook_out.dispatch(f"{event_type}.event", payload)
            except Exception as exc:  # noqa: BLE001
                log.debug("webhook out dispatch failed: %s", exc)

    # ---- 账户快照定时归档（遍历所有已连接券商账户）+ 订单/成交实时推送 ----
    async def start_account_snapshots(self, interval: float = 5.0) -> None:
        async def _loop():
            while True:
                try:
                    for conn in self.manager.all_connections():
                        if not conn.connected:
                            continue
                        acc_key = conn.cfg.account_id or conn.cfg.conn_id
                        cash = await conn.bridge.call(conn.adapter.get_cash)
                        pos = await conn.bridge.call(conn.adapter.get_positions)
                        pos_value = sum(p.get("market_value", 0.0) for p in pos)
                        snap = {"cash": cash, "net_value": round((cash.get("assets", 0.0) or 0.0) + pos_value, 2),
                                "positions": pos, "broker": conn.cfg.name,
                                "account_id": conn.cfg.account_id}
                        # 阶段 3：同步 sqlite 移出事件循环——快照写入走线程池，避免卡事件循环
                        await self.db.ainsert("account_snapshot", {
                            "account_id": conn.cfg.account_id or conn.cfg.name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "net_value": snap["net_value"],
                            "positions_json": json.dumps(pos, ensure_ascii=False),
                            "cash_json": json.dumps(cash, ensure_ascii=False),
                        })
                        # B4：净值上报给风控（日亏损熔断），仅活跃连接锚定日初净值
                        if self.risk is not None and conn.cfg.active:
                            try:
                                # 用真实持仓市值 + 总资产喂入风控（接管演示级默认值，来源切 live）
                                self.risk.feed_account_snapshot(
                                    {p.get("code", ""): float(p.get("market_value", 0.0) or 0.0)
                                     for p in (pos or [])},
                                    float((cash.get("assets", 0.0) or 0.0) or snap["net_value"]))
                                reason = self.risk.update_net_value(snap["net_value"])
                                if reason:
                                    log.warning("risk circuit broken: %s", reason)
                                    if self.notifier:
                                        await self.notifier.notify(
                                            "risk.circuit", "风控熔断触发", reason,
                                            {"net_value": snap["net_value"],
                                             "account_id": conn.cfg.account_id})
                                    await self._notify("risk", {
                                        "type": "risk_circuit", "data": self.risk.daily_stats()})
                            except Exception as exc:  # noqa: BLE001
                                log.debug("risk net value update failed: %s", exc)
                        # 订单/成交 diff（实时事件流）
                        try:
                            await self._push_order_deal_events(conn, acc_key)
                        except Exception as exc:
                            log.debug("order/deal diff failed: %s", exc)
                        for handler in self._notify_handlers:
                            await handler("account", {"type": "account_snapshot", "data": snap, "broker": conn.cfg.name})
                except Exception as exc:
                    log.warning("account snapshot failed: %s", exc)
                iv = interval
                if self.runtime_config is not None:
                    iv = self.runtime_config.snapshot_interval
                # 非交易时段放大快照间隔（60s 探活），减少无效券商请求
                from gateway.trading_session import default_session
                await asyncio.sleep(default_session.sleep_seconds(iv, 60.0))

        self._account_task = asyncio.create_task(_loop())

    async def _push_order_deal_events(self, conn, acc_key: str) -> None:
        """对比上次订单/成交快照，新订单/状态变化/新成交 -> WS 事件推送。

        阶段 2 加固：
        - 指纹按交易日清理（order_id 跨日复用不再误判）；
        - 成交去重键加入 seq（同秒同价量部成不再指纹碰撞丢单）；
        - 状态迁移加终态锁（filled/cancelled/rejected 不可回退，拦截乱序 filled→pending）。
        """
        today = time.strftime("%Y-%m-%d")
        if self._fp_date != today:
            self._fp_date = today
            self._order_fp.clear()
            self._deal_seen.clear()
        orders = await conn.bridge.call(conn.adapter.get_orders) or []
        deals = await conn.bridge.call(conn.adapter.get_deals) or []
        fp = self._order_fp.setdefault(acc_key, {})
        from xtquant_client.order_status import is_terminal
        for o in orders:
            oid = str(o.get("order_id", ""))
            if not oid:
                continue
            st = o.get("status", "")
            prev = fp.get(oid)
            if prev is not None and is_terminal(prev):
                # 终态锁：已成交/已撤/废单不可回退——乱序/陈旧回报直接忽略
                if prev != st:
                    log.warning("order %s 已终态(%s)，忽略乱序状态 %s", oid, prev, st)
                continue
            if prev is None:
                await self._notify("order", {"type": "order_event", "data": o,
                                             "broker": conn.cfg.name, "event": "new"})
            elif prev != st:
                await self._notify("order", {"type": "order_event", "data": o,
                                             "broker": conn.cfg.name,
                                             "event": "status", "prev_status": prev})
            fp[oid] = st
        seen = self._deal_seen.setdefault(acc_key, set())
        for d in deals:
            # 成交去重键加入 seq/回报唯一 id，避免同秒同价量部成指纹碰撞丢单
            key = (str(d.get("order_id", "")), str(d.get("seq", "") or d.get("deal_id", "")),
                   str(d.get("price")), str(d.get("volume")), str(d.get("time", "")))
            if key not in seen:
                seen.add(key)
                await self._notify("deal", {"type": "deal_event", "data": d,
                                            "broker": conn.cfg.name})

    def _roll_fp(self) -> None:
        """交易日滚动：跨日清理订单/成交指纹，防止 order_id 跨日复用误判。"""
        today = time.strftime("%Y-%m-%d")
        if self._fp_date != today:
            self._fp_date = today
            self._order_fp.clear()
            self._deal_seen.clear()

    # ---- 阶段 0-A：桥接子进程交易回报实时推送（零轮询延迟）----
    def register_realtime_trade_handlers(self, conn) -> None:
        """为连接注册桥接子进程转发的 on_order/on_trade → 泵 → 实时 WS 推送。幂等。

        与 `_push_order_deal_events` 轮询共享 `_order_fp`/`_deal_seen` 指纹——实时事件与
        轮询 diff 天然互斥（同一 order_id/成交都只推一次），轮询退化为兜底安全网。
        """
        b = conn.bridge
        key = getattr(conn.cfg, "conn_id", "")
        if b is None or not key or key in self._trade_cbs:
            return
        acc_key = conn.cfg.account_id or conn.cfg.conn_id
        broker = conn.cfg.name or conn.cfg.broker_id or acc_key

        def _on_order_evt(o, _a=acc_key, _br=broker, _b=b):
            _b.enqueue({"type": "order", "data": o or {}, "account": _a, "broker": _br})

        def _on_deal_evt(t, _a=acc_key, _br=broker, _b=b):
            _b.enqueue({"type": "deal", "data": t or {}, "account": _a, "broker": _br})

        try:
            conn.adapter.on_order(_on_order_evt)
            conn.adapter.on_trade(_on_deal_evt)
            # partial 对象：同 func+args 判等，ensure_handler 可去重（幂等补注册）
            from functools import partial
            b.ensure_handler("order", partial(self._on_realtime_order, acc_key))
            b.ensure_handler("deal", partial(self._on_realtime_deal, acc_key))
            self._trade_cbs.add(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("realtime trade handlers %s: %s", conn.cfg.conn_id, exc)

    async def _on_realtime_order(self, acc_key: str, event: dict) -> None:
        """桥接子进程实时报单回报 → 推送前端（与轮询共享指纹去重）。"""
        data = event.get("data") or {}
        oid = str(data.get("order_id") or "")
        if not oid:
            return
        st = data.get("status") or data.get("order_status") or ""
        self._roll_fp()
        from xtquant_client.order_status import is_terminal
        fp = self._order_fp.setdefault(acc_key, {})
        prev = fp.get(oid)
        if prev is not None and is_terminal(prev):
            return  # 终态锁：乱序/陈旧回报忽略
        if prev is None:
            await self._notify("order", {"type": "order_event", "data": data,
                                         "broker": event.get("broker", ""), "event": "new"})
        elif prev != st:
            await self._notify("order", {"type": "order_event", "data": data,
                                         "broker": event.get("broker", ""),
                                         "event": "status", "prev_status": prev})
        fp[oid] = st

    async def _on_realtime_deal(self, acc_key: str, event: dict) -> None:
        """桥接子进程实时成交回报 → 推送前端（与轮询共享成交去重键）。"""
        data = event.get("data") or {}
        oid = str(data.get("order_id") or "")
        key = (oid, str(data.get("seq") or data.get("trade_id") or ""),
               str(data.get("price")), str(data.get("volume")),
               str(data.get("trade_time") or ""))
        self._roll_fp()
        seen = self._deal_seen.setdefault(acc_key, set())
        if key in seen:
            return
        seen.add(key)
        await self._notify("deal", {"type": "deal_event", "data": data,
                                    "broker": event.get("broker", "")})

    async def stop(self) -> None:
        if self._account_task:
            self._account_task.cancel()
        if self._batch_task:
            self._batch_task.cancel()

    def start_batch(self) -> None:
        """启动行情微批 flush 循环（100ms 窗口，C2）。"""
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._batch_loop())

    async def _batch_loop(self) -> None:
        prune_tick = 0
        while True:
            window = 0.1
            if self.runtime_config is not None:
                window = self.runtime_config.batch_window
            await asyncio.sleep(window)
            # 取走并清空两个缓冲（广播 + 写盘）
            buf = self._batch_buf
            rows = self._batch_rows
            items = list(buf)
            buf.clear()
            row_items = list(rows)
            rows.clear()
            # C1/P2-1：market_cache 单事务批量落盘（一窗口一次 commit）
            if row_items and self.db is not None:
                try:
                    await self.db.aexecutemany_in_txn(
                        "INSERT OR REPLACE INTO market_cache (code, dtype, ts, payload_json) "
                        "VALUES (?,?,?,?)", row_items)
                except Exception as exc:  # noqa: BLE001
                    log.warning("market_cache batch write failed: %s", exc)
            if not items:
                continue
            await self._notify("quotes", {"items": items})
            # C1/P2-1：周期性收敛 market_cache 数据量（约每 100 个窗口≈10s 一次）
            prune_tick += 1
            if prune_tick % 100 == 0 and self.db is not None:
                try:
                    removed = await self.db.aprune_market_cache(keep=20)
                    if removed:
                        log.info("market_cache pruned %d rows", removed)
                except Exception as exc:  # noqa: BLE001
                    log.debug("market_cache prune failed: %s", exc)

    def on_notify(self, handler) -> None:
        self._notify_handlers.append(handler)


class WSManager:
    """WebSocket 连接管理：账户/行情/告警三通道广播。"""

    def __init__(self, engine: SyncEngine):
        self.engine = engine
        self._sockets: dict[str, WebSocket] = {}
        self._seq: dict[str, int] = {}
        self._recent: deque = deque(maxlen=3000)   # 最近行情环形缓冲（断线补发窗口，C4）
        # 阶段 1（C20）：cid 单调自增，绝不复用——断连重连后若用 len(sockets)+1，
        # 会生成重复 cid 覆盖现有 socket，导致订阅/退订/清理全错位
        self._next_cid = 0

    async def connect(self, ws: WebSocket) -> str:
        await ws.accept()
        self._next_cid += 1
        cid = f"client-{self._next_cid}"
        self._sockets[cid] = ws
        self._seq[cid] = 0
        await self.send_full_snapshot(cid)
        return cid

    def _push_recent(self, items: list[dict]) -> None:
        now = time.time()
        for it in items:
            rec = dict(it)
            rec["_t"] = now
            self._recent.append(rec)

    async def _send_recent_window(self, cid: str, codes: list[str] | None = None,
                                  window: float = 0.0) -> None:
        """断线重连订阅后补发最近 window 秒的行情（C4），避免策略缺口。

        仅补发本客户端订阅的代码；codes 为空则按其当前订阅集过滤。
        window<=0 时取运行时配置 sync.recent_window（默认 30s）。
        """
        if window <= 0:
            window = 30.0
            if self.engine.runtime_config is not None:
                window = self.engine.runtime_config.recent_window
        ws = self._sockets.get(cid)
        if not ws or not self._recent:
            return
        want = set(codes or self.engine._client_subscriptions.get(cid, set()))
        if not want:
            return
        cutoff = time.time() - window
        items = [it for it in self._recent
                 if it.get("_t", 0) >= cutoff and it.get("code") in want]
        if not items:
            return
        try:
            await ws.send_text(json.dumps({"type": "quotes_replay", "seq": self._bump(cid),
                                           "data": {"items": items}}, ensure_ascii=False))
        except Exception:
            self.disconnect(cid)

    def disconnect(self, cid: str) -> None:
        self._sockets.pop(cid, None)
        self.engine.client_unsubscribe(cid, list(self.engine._client_subscriptions.get(cid, set())))

    def client_count(self) -> int:
        return len(self._sockets)

    def _bump(self, cid: str) -> int:
        self._seq[cid] += 1
        return self._seq[cid]

    async def send_full_snapshot(self, cid: str) -> None:
        ws = self._sockets.get(cid)
        if not ws:
            return
        try:
            await ws.send_text(json.dumps({
                "type": "snapshot",
                "seq": self._bump(cid),
                "quotes": self.engine.latest_quotes,
            }, ensure_ascii=False))
        except Exception:
            self.disconnect(cid)

    async def broadcast(self, event_type, payload: dict | None = None,
                        codes: list[str] | None = None) -> None:
        # 兼容两种调用形态（P2-2 事件推送修复）：
        #   - 引擎 on_event=ws_manager.broadcast 时传单个 dict {"type","data"}；
        #   - sync/_notify 等传 (event_type, payload[, codes])。
        if isinstance(event_type, dict):
            codes = payload
            payload = event_type.get("data")
            event_type = event_type.get("type") or "event"
        # 维护最近行情环形缓冲（断线补发窗口，C4）
        if event_type == "quotes":
            self._push_recent(payload.get("items", []) if isinstance(payload, dict) else [])
        elif event_type == "quote" and isinstance(payload, dict) and payload.get("code"):
            self._push_recent([payload])
        for cid, ws in list(self._sockets.items()):
            send_payload = payload
            if event_type == "quotes":
                sub = self.engine._client_subscriptions.get(cid, set())
                items = [it for it in payload.get("items", []) if it.get("code") in sub]
                if not items:
                    continue
                send_payload = {"items": items}
            elif event_type == "quote" and codes is not None:
                sub = self.engine._client_subscriptions.get(cid, set())
                if payload.get("code") not in sub:
                    continue
            try:
                await ws.send_text(json.dumps({"type": event_type, "seq": self._bump(cid),
                                               "data": send_payload}, ensure_ascii=False))
            except Exception:
                self.disconnect(cid)

    async def handle_client_message(self, cid: str, message: str) -> None:
        """客户端协议：{"action":"subscribe"|"unsubscribe","codes":[...]}"""
        try:
            msg = json.loads(message)
        except Exception:
            return
        action = msg.get("action")
        codes = msg.get("codes", [])
        if action == "subscribe":
            self.engine.client_subscribe(cid, codes)
            await self.send_full_snapshot(cid)
            # 补发订阅代码最近 30s 的行情（断线重连缺口，C4）
            await self._send_recent_window(cid, codes)
        elif action == "unsubscribe":
            self.engine.client_unsubscribe(cid, codes)
        elif action == "ping":
            ws = self._sockets.get(cid)
            if ws:
                await ws.send_text(json.dumps({"type": "pong", "seq": self._bump(cid)}))
