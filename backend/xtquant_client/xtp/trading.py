"""交易：下单 / 撤单 / 委托与成交查询 / 回调闭环（TradingMixin，自原 xtp.py 逐行搬移）。"""

import threading
from datetime import datetime

from ..base import BrokerError
from ._common import _dget, _direction_from_order_type, _pick, _shell_attr, log


class TradingMixin:


    # ---------------- 阶段 0-A：报单回调闭环 ----------------
    def on_order(self, cb) -> None:
        """注册报单响应回调（order_id 解析 / 状态更新 → 推 sync 引擎）。"""
        self._on_order_cb = cb

    def on_trade(self, cb) -> None:
        """注册成交回报回调（替代纯轮询，直推 sync 引擎）。"""
        self._on_trade_cb = cb

    def on_disconnect(self, cb) -> None:
        """注册断线回调（触发 manager 健康重连）。"""
        self._on_disconnect_cb = cb

    def _make_callback(self, base):
        """动态构造 XtQuantTraderCallback 子类，把 SDK 对象转 dict 后交 adapter 方法处理。

        动态子类（而非静态子类）是因为 XtQuantTraderCallback 仅在 xtquant 导入后存在；
        adapter 方法接收**纯 dict**，便于在无 SDK 环境下单测。

        多版本兼容：新旧 SDK 的回调方法名与签名都不一致，这里**同时覆盖两套**：
        - 新 SDK：``on_order_stock_response(order)`` + ``on_cancel_error(order_id, error_id, error_msg)``
        - 旧 SDK (xttrader)：``on_order_stock_async_response(response)`` +
          ``on_cancel_error(cancel_error)``（1 个对象参数）+ ``on_order_error(order_error)``
        只要基类定义了对应方法，覆盖后即生效；基类未定义的方法名覆盖无副作用。
        """
        adapter = self

        def _order_as_dict(order) -> dict:
            # 兼容新旧 SDK：旧 xttrader 用全小写（seq/order_id/order_status/error_msg），
            # 新版部分用 CamelCase（Seq/OrderID/OrderStatus/ErrorMsg）。
            return {
                "seq": _pick(order, "Seq", "seq"),
                "order_id": _pick(order, "OrderID", "order_id"),
                "order_status": _pick(order, "OrderStatus", "order_status"),
                "error_id": _pick(order, "ErrorID", "error_id"),
                "error_msg": _pick(order, "ErrorMsg", "error_msg"),
                "stock_code": _pick(order, "StockCode", "stock_code"),
                "order_type": _pick(order, "OrderType", "order_type"),
                "price": _pick(order, "Price", "price"),
                "volume": _pick(order, "Volume", "order_volume", "volume", default=0),
                "traded_volume": _pick(order, "TradedVolume", "traded_volume", default=0),
            }

        def _trade_as_dict(trade) -> dict:
            # 真实成交（XtTrade）用 traded_price/traded_time；旧 SDK 退化路径（见
            # _query_stock_deals）以「已成交订单」近似，订单对象用 price/order_time，
            # 故两者都列入候选（真实成交的 traded_price 优先命中，不会被 price 覆盖）。
            return {
                "order_id": _pick(trade, "OrderID", "order_id"),
                "stock_code": _pick(trade, "StockCode", "stock_code"),
                "trade_type": _pick(trade, "TradeType", "trade_type"),
                "price": _pick(trade, "traded_price", "deal_price", "DealPrice", "price"),
                "volume": _pick(trade, "Volume", "traded_volume", "deal_volume", default=0),
                "trade_time": _pick(trade, "TradeTime", "traded_time", "deal_time", "order_time"),
                "trade_id": _pick(trade, "TradeID", "traded_id", "deal_id"),
            }

        class _Cb(base):
            def on_disconnected(self):
                adapter._handle_disconnected()

            # 新 SDK: on_order_stock_response(order)
            def on_order_stock_response(self, order):
                adapter._handle_order_response(_order_as_dict(order))

            # 旧 SDK (xttrader): on_order_stock_async_response(response)
            def on_order_stock_async_response(self, response):
                adapter._handle_order_response(_order_as_dict(response))

            # 旧 SDK 报单失败回调（XtOrderError 对象）
            def on_order_error(self, order_error):
                adapter._handle_order_error(order_error)

            # 兼容两种签名：旧 SDK on_cancel_error(cancel_error: 对象) /
            # 新 SDK on_cancel_error(order_id, error_id, error_msg)
            def on_cancel_error(self, *args):
                adapter._handle_cancel_error(*args)

            def on_stock_trade(self, trade):
                adapter._handle_stock_trade(_trade_as_dict(trade))

        return _Cb()

    def _handle_disconnected(self) -> None:
        """断线：翻转连接态并通知 manager 触发健康重连。"""
        was = self._connected
        self._connected = False
        with self._map_lock:
            self._seq_to_oid.clear()
            self._oid_to_seq.clear()
            self._pending_resp.clear()
        log.warning("XtQuantTrader 断线（adapter=%s account=%s）",
                    self._account_id, self._account_type)
        if was and self._on_disconnect_cb is not None:
            try:
                self._on_disconnect_cb()
            except Exception:  # noqa: BLE001
                pass

    def _handle_order_response(self, o: dict) -> None:
        """报单响应：建立 seq→柜台 order_id 映射，唤醒等待中的 place_order，并外推。

        键名兼容新旧两套：旧 xttrader 用全小写（seq/order_id/order_status/error_msg），
        新版部分用 CamelCase（Seq/OrderID/OrderStatus/ErrorMsg）。
        """
        try:
            seq = int(_dget(o, "seq", "Seq") or -1)
        except (ValueError, TypeError):
            seq = -1
        oid = _dget(o, "order_id", "OrderID")
        with self._map_lock:
            if oid is not None:
                oid = str(oid)
                self._seq_to_oid[seq] = oid
                self._oid_to_seq[oid] = seq
            pend = self._pending_resp.pop(seq, None)
        log.debug("order response seq=%s order_id=%s status=%s err=%s",
                  seq, oid, _dget(o, "order_status", "OrderStatus"),
                  _dget(o, "error_msg", "ErrorMsg"))
        if pend is not None:
            ev, bucket = pend
            bucket.append(o)
            ev.set()
        if self._on_order_cb is not None:
            try:
                self._on_order_cb(o)
            except Exception:  # noqa: BLE001
                pass

    def _handle_order_error(self, order_error) -> None:
        """旧 SDK 报单失败回调（XtOrderError）：记录以便审计与诊断。"""
        oid = getattr(order_error, "order_id", None)
        eid = getattr(order_error, "error_id", None)
        emsg = getattr(order_error, "error_msg", None)
        log.warning("order error order_id=%s error_id=%s msg=%s", oid, eid, emsg)

    def _handle_cancel_error(self, *args) -> None:
        """撤单失败回调，兼容两种 SDK 签名：

        - 旧 SDK：``on_cancel_error(cancel_error: XtCancelError)`` —— 1 个对象参数，
          从中取 ``order_id`` / ``error_id`` / ``error_msg``；
        - 新 SDK：``on_cancel_error(order_id, error_id, error_msg)`` —— 3 个标量参数。
        """
        oid = eid = emsg = None
        if len(args) == 1:
            ce = args[0]
            if ce is not None:
                oid = getattr(ce, "order_id", None)
                eid = getattr(ce, "error_id", None)
                emsg = getattr(ce, "error_msg", None)
                if oid is None and eid is None and emsg is None and not isinstance(ce, (list, dict)):
                    # 个别版本直接传错误字符串/整型
                    emsg = str(ce)
        elif len(args) >= 3:
            oid, eid, emsg = args[0], args[1], args[2]
        log.warning("cancel error order_id=%s error_id=%s msg=%s", oid, eid, emsg)

    def _handle_stock_trade(self, t: dict) -> None:
        """成交回报：直推 sync 引擎（替代纯轮询）。"""
        if self._on_trade_cb is not None:
            try:
                self._on_trade_cb(t)
            except Exception:  # noqa: BLE001
                pass

    def _wait_order_response(self, seq: int, timeout: float = 10.0) -> str | None:
        """等待 on_order_stock_response 回调解析出柜台真实 order_id。

        返回柜台 order_id（str）；超时未到返回 None（调用方应记作 status=unknown）。
        回调已提前到达时（映射已建立）直接返回，不阻塞。

        并发安全：注册 pending 与回调侧写映射/弹 pending 都在 _map_lock 内，
        消除「回调落在 get(seq) 之后、Event 注册之前」的竞态窗口；等待结束后
        再查一次映射，覆盖「回调已建映射但 event 未触发」的边界。
        """
        with self._map_lock:
            oid = self._seq_to_oid.get(seq)
            if oid is not None:
                return oid
            ev = threading.Event()
            bucket: list = []
            self._pending_resp[seq] = (ev, bucket)
        if ev.wait(timeout):
            with self._map_lock:
                oid = self._seq_to_oid.get(seq)
            if oid is not None:
                return oid
            for o in bucket:
                got = _dget(o, "order_id", "OrderID")
                if got is not None:
                    return str(got)
        return None

    def get_orders(self) -> list[dict]:
        trader, acc = self._require_trader()
        with self._lock:
            orders = trader.query_stock_orders(acc) or []
        out = []
        for o in orders:
            # 多版本属性兼容：旧版全小写（order_id/stock_code/order_volume/
            # traded_volume/order_status），新版部分 CamelCase。
            oid = str(_pick(o, "order_id", "OrderID") or "")
            code = _pick(o, "stock_code", "StockCode")
            otype = _pick(o, "order_type", "OrderType")
            out.append({
                "order_id": oid,
                "code": code,
                "direction": _direction_from_order_type(otype),
                "price": self._f(_pick(o, "price", "Price")),
                "volume": _pick(o, "order_volume", "ordered_volume", "Volume", default=0),
                "dealt": _pick(o, "traded_volume", "deal_volume", "DealVolume", default=0),
                "status": self._order_status(_pick(o, "order_status", "Status", default=-1), oid),
            })
        return out

    def get_deals(self) -> list[dict]:
        trader, acc = self._require_trader()
        with self._lock:
            deals = self._query_stock_deals(trader, acc)
        if not deals:
            return []
        out = []
        for d in deals:
            # 多版本属性兼容（同 get_orders / _trade_as_dict）。
            # 注意退化路径（旧 SDK 无 query_stock_deals）用「已成交订单」近似，
            # 订单对象用 price/order_time，故一并列入候选（真实成交的
            # traded_price/traded_time 优先命中，不会被覆盖）。
            oid = str(_pick(d, "order_id", "OrderID") or "")
            code = _pick(d, "stock_code", "StockCode")
            otype = _pick(d, "order_type", "OrderType")
            out.append({
                "order_id": oid,
                "code": code,
                "direction": _direction_from_order_type(otype),
                "price": self._f(_pick(d, "traded_price", "deal_price", "DealPrice", "price")),
                "volume": _pick(d, "traded_volume", "deal_volume", "DealVolume", default=0),
                "time": _pick(d, "traded_time", "deal_time", "DealTime", "order_time") or "",
                "seq": _pick(d, "seq", "Seq"),
            })
        return out

    def _query_stock_deals(self, trader, acc) -> list:
        """查询成交记录，兼容有无 ``query_stock_trades`` / ``query_stock_deals`` 的 SDK 版本。

        - 新 SDK（迅投新版）：``trader.query_stock_trades(acc)``；
        - 旧 SDK（xttrader）：``query_stock_deals``，再退化为「已成交订单」近似。
          真实成交回报仍以 on_stock_trade 回调为准，不会漏单 / 不会重复计。
        """
        # 关键正确性修复（P0-13）：优先 query_stock_trades（迅投新版成交接口），
        # 回退 query_stock_deals（旧版），最后才退化到「已成交订单」近似，保证
        # 返回真实 trade_id / traded_price / traded_time。
        fn = getattr(trader, "query_stock_trades", None) or getattr(trader, "query_stock_deals", None)
        if fn is not None:
            try:
                return fn(acc) or []
            except Exception:  # noqa: BLE001
                pass
        try:
            orders = trader.query_stock_orders(acc) or []
            return [o for o in orders
                    if int(_pick(o, "traded_volume", "deal_volume", default=0) or 0) > 0]
        except Exception:  # noqa: BLE001
            return []

    # ---------------- 交易 ----------------
    def place_order(self, code: str, direction: str, price_type: str,
                    price: float, volume: int, strategy_name: str = "",
                    remark: str = "") -> dict:
        trader, acc = self._require_trader()
        # 参数防线（纵深防御，风控层可能被旁路/直调 bridge）：
        # 限价单 price<=0 会以「价格=0」送出成交灾难，必须先拦。
        if (price_type or "limit") == "limit" and price <= 0:
            raise BrokerError("限价单必须提供 >0 的委托价")
        if not (isinstance(volume, int) or float(volume).is_integer()) or volume <= 0:
            raise BrokerError("委托数量必须为正整数")
        xtc = _shell_attr("_ensure_xtconstant")()  # monkeypatch 兼容：经壳模块动态查找（见 _common._shell_attr）
        # 阶段 0-A（C9）：信用账户裸常量 CREDIT_BUY/CREDIT_SELL 在部分 xtquant 版本
        # 不存在（AttributeError）。用 getattr 兜底，缺失时回退标准买卖。
        if self._account_type == "CREDIT" and direction == "buy":
            op = getattr(xtc, "CREDIT_BUY", xtc.STOCK_BUY)
        elif self._account_type == "CREDIT" and direction == "sell":
            op = getattr(xtc, "CREDIT_SELL", xtc.STOCK_SELL)
        elif direction == "buy":
            op = xtc.STOCK_BUY
        else:
            op = xtc.STOCK_SELL
        # 价格类型常量：部分旧版可能无 LATEST_PRICE / 用 MARKET_PRICE，getattr 兜底避免
        # AttributeError；最终数字码与新版一致（LATEST_PRICE=5, FIX_PRICE=11）。
        _mkt = getattr(xtc, "LATEST_PRICE", getattr(xtc, "MARKET_PRICE", 5))
        _fix = getattr(xtc, "FIX_PRICE", 11)
        pt = _mkt if (price_type or "limit") == "market" else _fix
        # 注意参数顺序：真实旧版 order_stock(account, stock_code, order_type, order_volume,
        # price_type, price, strategy_name, order_remark)。原代码把 pt 放在 order_volume
        # 位置会导致真实下单时「量=价格类型、价格类型=价格、价格=量」——交易灾难。
        with self._lock:
            seq = trader.order_stock(acc, code, op, int(volume), pt, float(price),
                                     strategy_name or "", remark or "")
        # 阶段 0-A（C2/F2）：order_stock 失败返回 -1（truthy，不能 `if not` 判断），
        # 必须把 -1 显式判为失败并抛错，否则「下单失败被报成功」→ 审计记 ok、WAL 记 pending。
        if seq is None or seq == -1:
            # 语义修正：SDK 已就位（_require_trader 已通过），-1 是**券商柜台拒单**
            # （资金不足 / 非交易时段 / 无交易权限 / 风控拦截），并非「缺少 SDK」。
            # 旧实现抛 BrokerSDKError 会把可操作的拒单原因包装成「请安装 SDK」，
            # 且经桥接跨进程重建后还会二次套娃（见 bridge_client._rebuild_error）。
            raise BrokerError(
                "下单失败：柜台返回 -1（资金不足/标的不在交易时段/无交易权限/风控拦截）")
        seq = int(seq)
        # 阶段 0-A：真实柜台 order_id 仅经 on_order_stock_response 回调下发，
        # 提交后等待回调（带超时）——超时未到则记作 status=unknown（绝不伪报 submitted）。
        oid = self._wait_order_response(seq, timeout=10.0)
        order_id = oid or str(seq)
        status = "unknown" if oid is None else "submitted"
        return {"order_id": order_id, "seq": seq, "code": code, "direction": direction,
                "price_type": price_type, "price": price, "volume": volume,
                "status": status, "ts": datetime.now().isoformat(timespec="seconds")}

    def cancel_order(self, order_id: str) -> dict:
        trader, acc = self._require_trader()
        # order_id 可能是柜台真实 order_id（place_order 返回），直接传入；
        # seq 已通过 _seq_to_oid 映射为真实 id，无需再转换。
        # 关键正确性修复（P0-12）：必须捕获 cancel_order_stock 的返回码
        # （0=成功 / -1=失败），据此返回真实 ok 与 message，否则上层
        # ExecutionService 永远判撤单成功（假成功）。
        with self._lock:
            ret = trader.cancel_order_stock(acc, int(order_id))
        ok = (ret == 0)
        code = int(ret) if isinstance(ret, (int, float)) else (-1 if not ok else 0)
        return {
            "ok": ok,
            "code": code,
            "message": "" if ok else f"撤单失败（柜台返回 {ret!r}）",
            "order_id": str(order_id),
            "status": "cancel_submitted" if ok else "cancel_failed",
        }

    def _order_status(self, st: int, oid: str) -> str:
        # 阶段 0-A（F12）：统一走共享词汇表，消除三处各说各话。
        from ..order_status import normalize_order_status
        return normalize_order_status(st)


__all__ = [
    'TradingMixin',
]
