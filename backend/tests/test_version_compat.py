"""多版本 QMT 客户端兼容回归测试（阶段 0-F / 深度检查）。

直接对照本机已安装的广发 QMT 客户端（旧版 xttrader SDK）的真实 API 表面构造
Fake 对象，验证平台在「与新版 API 命名不同」的旧客户端下不崩溃、且数据不被取错：

- 旧 SDK 对象属性全小写（XtOrder: order_id/stock_code/order_volume/
  traded_volume/order_status/price/order_time；XtTrade: traded_price/
  traded_time），而非 CamelCase；退化路径（无 query_stock_deals）用「已成交
  订单」近似，订单对象仅能取到 order_time（无 traded_time）；
- 旧 SDK 回调名为 on_order_stock_async_response(response) 与
  on_cancel_error(cancel_error)（1 个对象参数），与新增的
  on_order_stock_response(order) / on_cancel_error(oid, eid, emsg) 不同；
- 旧 SDK 只有 query_stock_asset（无 query_asset）、无 query_stock_deals；
- 旧 SDK get_trading_calendar 首参为 market（必填）；
- 旧 SDK get_l2_transaction 首参为 field_list（不是 stock_code）。
"""
from types import SimpleNamespace

from xtquant_client.xtp import (
    XTPQuantAdapter,
    _direction_from_order_type,
    _pick,
)


# ---------- 旧 SDK（xttrader）对象复刻（属性名取自真实 xttype.py） ----------
class FakeXtOrder:
    def __init__(self, oid, code, otype, vol, dealt, status, price, order_time=""):
        self.order_id = oid
        self.stock_code = code
        self.order_type = otype
        self.order_volume = vol
        self.traded_volume = dealt
        self.order_status = status
        self.price = price
        self.order_time = order_time


class FakeXtTrade:
    def __init__(self, oid, code, otype, price, vol, t):
        self.order_id = oid
        self.stock_code = code
        self.order_type = otype
        self.traded_price = price
        self.traded_volume = vol
        self.traded_time = t


class FakeXtOrderResponse:
    """on_order_stock_async_response 的 response：只有 seq / order_id / error_msg。"""
    def __init__(self, seq, order_id, error_msg=""):
        self.seq = seq
        self.order_id = order_id
        self.error_msg = error_msg


class FakeXtCancelError:
    def __init__(self, order_id, error_id, error_msg):
        self.order_id = order_id
        self.error_id = error_id
        self.error_msg = error_msg


class FakeXtOrderError:
    def __init__(self, order_id, error_id, error_msg):
        self.order_id = order_id
        self.error_id = error_id
        self.error_msg = error_msg


class FakeXtAsset:
    def __init__(self, cash, frozen, mv, total):
        self.cash = cash
        self.frozen_cash = frozen
        self.market_value = mv
        self.total_asset = total


class FakeXtPosition:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeTrader:
    """旧 SDK trader：只有 query_stock_asset（无 query_asset），无 query_stock_deals。"""
    def __init__(self, orders, trades, asset, positions=None):
        self._orders = orders
        self._trades = trades
        self._asset = asset
        self._positions = positions or []
        self.order_calls: list[dict] = []
        self.cancel_calls: list[tuple] = []

    def query_stock_orders(self, acc):
        return list(self._orders)

    def query_stock_positions(self, acc):
        return list(self._positions)

    def query_stock_asset(self, acc):
        return self._asset

    def order_stock(self, account, stock_code, order_type, order_volume,
                    price_type, price, strategy_name='', order_remark=''):
        """模拟旧版 order_stock 签名，捕获调用参数供测试断言。"""
        self.order_calls.append({
            "account": account,
            "stock_code": stock_code,
            "order_type": order_type,
            "order_volume": order_volume,
            "price_type": price_type,
            "price": price,
            "strategy_name": strategy_name,
            "order_remark": order_remark,
        })
        return len(self.order_calls)  # seq

    def cancel_order_stock(self, account, order_id):
        self.cancel_calls.append((account, order_id))
        return 0
    # 故意不提供 query_asset / query_stock_deals —— 强制走兼容兜底


# ---------- 回调基类（同时含新旧两套方法名，模拟「合并」API） ----------
class FakeCallbackBase:
    def on_disconnected(self):
        pass

    def on_order_stock_response(self, order):
        pass

    def on_order_stock_async_response(self, response):
        pass

    def on_order_error(self, order_error):
        pass

    def on_cancel_error(self, *args):
        pass

    def on_stock_trade(self, trade):
        pass


def _new_adapter():
    a = XTPQuantAdapter(client_path="C:/x", account_id="123456",
                        account_type="STOCK", session_id=0)
    a._connected = True
    a._trader = FakeTrader(
        orders=[
            FakeXtOrder("O1", "600000", 23, 100, 0, 50, 10.5),   # 买 部成待报
            FakeXtOrder("O2", "000001", 24, 200, 50, 56, 8.2,
                        order_time="20240102 09:35:00"),   # 卖 全部成交
        ],
        trades=[
            FakeXtTrade("O2", "000001", 24, 8.2, 50, "20240102 09:35:00"),
        ],
        asset=FakeXtAsset(100000.0, 0.0, 1640.0, 101640.0),
    )
    a._acc = SimpleNamespace(account_id="123456")
    return a


# ---------------- 工具函数 ----------------
def test_pick_lowercase_and_camelcase():
    obj = SimpleNamespace(order_id="X9", stock_code="600000")
    # 优先 CamelCase，缺失时回退小写
    assert _pick(obj, "OrderID", "order_id") == "X9"
    obj2 = SimpleNamespace(order_id="X9")
    assert _pick(obj2, "OrderID", "order_id") == "X9"
    # 0 这类合法值不应被误判为缺失
    obj3 = SimpleNamespace(volume=0)
    assert _pick(obj3, "Volume", "volume", default=-1) == 0
    # 全缺失返回 default
    assert _pick(obj3, "nope", default="DFLT") == "DFLT"


def test_direction_from_order_type():
    assert _direction_from_order_type(23) == "buy"      # STOCK_BUY / 担保品买入
    assert _direction_from_order_type(24) == "sell"     # STOCK_SELL
    assert _direction_from_order_type(29) == "buy"      # 买券还券
    assert _direction_from_order_type(31) == "sell"     # 卖券还款
    assert _direction_from_order_type("23") == "buy"
    assert _direction_from_order_type(99) == ""         # 未知不臆测
    assert _direction_from_order_type("garbage") == ""


# ---------------- 回调：同时覆盖新旧两套方法名 ----------------
def test_callback_covers_both_response_names():
    a = _new_adapter()
    cb = a._make_callback(FakeCallbackBase)
    # 必须同时拥有新旧两个响应回调名（旧 SDK 只调 async 版，新 SDK 只调非 async 版）
    assert hasattr(cb, "on_order_stock_response")
    assert hasattr(cb, "on_order_stock_async_response")
    assert hasattr(cb, "on_cancel_error")
    assert hasattr(cb, "on_order_error")
    assert hasattr(cb, "on_stock_trade")


def test_old_api_async_response_builds_seq_mapping():
    """旧 SDK 的 on_order_stock_async_response(response) 必须建立 seq->order_id 映射。"""
    a = _new_adapter()
    cb = a._make_callback(FakeCallbackBase)
    # 模拟旧 SDK 异步回报（小写属性）
    cb.on_order_stock_async_response(FakeXtOrderResponse(seq=7, order_id="REAL_OID"))
    assert a._seq_to_oid.get(7) == "REAL_OID"
    assert a._oid_to_seq.get("REAL_OID") == 7


def test_old_api_cancel_error_single_object_no_crash():
    """旧 SDK on_cancel_error(cancel_error: 对象) 不能因签名不符而 TypeError。"""
    a = _new_adapter()
    cb = a._make_callback(FakeCallbackBase)
    # 1 个对象参数（XtCancelError）→ 不应抛异常
    cb.on_cancel_error(FakeXtCancelError("O9", 1001, "资金不足"))
    # 新 SDK 3 标量参数也应兼容
    cb.on_cancel_error("O9", 1001, "资金不足")


def test_old_api_order_error_handler():
    a = _new_adapter()
    cb = a._make_callback(FakeCallbackBase)
    cb.on_order_error(FakeXtOrderError("O8", 2002, "废单"))  # 不应抛异常


# ---------------- get_orders / get_deals 低版本属性兼容 ----------------
def test_get_orders_lowercase_attrs():
    a = _new_adapter()
    rows = a.get_orders()
    assert len(rows) == 2
    r = {x["order_id"]: x for x in rows}
    o1 = r["O1"]
    assert o1["code"] == "600000"
    assert o1["direction"] == "buy"
    assert o1["volume"] == 100          # 旧版 order_volume 必须被取到（曾为 0）
    assert o1["dealt"] == 0
    assert o1["status"] == "pending"    # order_status=50 -> pending（曾为 -1->unknown）
    o2 = r["O2"]
    assert o2["direction"] == "sell"
    assert o2["volume"] == 200
    assert o2["dealt"] == 50
    assert o2["status"] == "filled"     # order_status=56 -> filled


def test_get_deals_lowercase_attrs_and_fallback():
    """旧 SDK 无 query_stock_deals：get_deals 退化为「已成交订单」近似且不崩溃。"""
    a = _new_adapter()
    rows = a.get_deals()
    # 退化路径返回 traded_volume>0 的订单（O2: 50>0）
    assert len(rows) == 1
    d = rows[0]
    assert d["order_id"] == "O2"
    assert d["code"] == "000001"
    assert d["direction"] == "sell"
    assert d["price"] == 8.2            # 旧版 traded_price 必须被取到（曾为 None）
    assert d["volume"] == 50
    assert d["time"] == "20240102 09:35:00"  # 旧版 traded_time


# ---------------- get_account：query_stock_asset 兜底 ----------------
def test_get_account_via_query_stock_asset():
    a = _new_adapter()
    acc = a.get_account()
    assert acc["cash"] == 100000.0
    assert acc["frozen"] == 0.0
    assert acc["market_value"] == 1640.0
    assert acc["assets"] == 101640.0


# ---------------- get_trading_calendar：market 首参兼容 ----------------
def test_trading_calendar_market_param():
    a = _new_adapter()
    captured = {}

    def fake_cal(market, start_time="", end_time="", tradetimes=False):
        captured["market"] = market
        captured["start_time"] = start_time
        captured["end_time"] = end_time
        return ["20240102", "20240103"]

    a._xtdata = SimpleNamespace(get_trading_calendar=fake_cal)
    out = a.get_trading_calendar("20240101", "20240131")
    assert out == ["20240102", "20240103"]
    # 旧 SDK 首参为 market，必须自动填入 "SH"（绝不可把 start 当 market）
    assert captured["market"] == "SH"
    assert captured["start_time"] == "20240101"
    assert captured["end_time"] == "20240131"


# ---------------- get_l2_transactions：参数顺序兼容 ----------------
def test_l2_transaction_keyword_call():
    a = _new_adapter()
    captured = {}

    def fake_l2(stock_code="", start_time="", end_time="", count=-1, field_list=None):
        captured["stock_code"] = stock_code
        captured["count"] = count
        return {}  # 返回值由 DataFrame 解析（真实 SDK 行为），此处仅校验调用参数

    a._xtdata = SimpleNamespace(get_l2_transaction=fake_l2)
    out = a.get_l2_transactions("600000", 50)
    assert captured["stock_code"] == "600000"   # 绝不可落在 field_list 位置
    assert captured["count"] == 50
    assert out == []  # 返回值由 DataFrame 解析（真实 SDK 行为），此处仅校验调用参数


def test_l2_transaction_positional_fallback_on_typeerror():
    """关键字不被接受时退化为位置参数（首参 field_list=[]，次参 stock_code）。"""
    a = _new_adapter()
    calls = []

    def fake_l2(*args):  # 仅位置参数：不接受关键字 -> 强制触发 TypeError 兜底
        calls.append(args)
        return {}

    a._xtdata = SimpleNamespace(get_l2_transaction=fake_l2)
    a.get_l2_transactions("600000", 30)
    # 关键字调用抛 TypeError 后，兜底以位置参数 ([], code, "", "", count) 调用
    assert len(calls) == 1
    assert calls[0][0] == []            # field_list
    assert calls[0][1] == "600000"       # stock_code 落在正确位置
    assert calls[0][4] == 30            # count


# ---------------- 行情归一化 / 订阅回调：新旧两套载荷结构 ----------------
def test_norm_quote_kline_bar_field_fallback():
    """旧版 K 线 bar（小写字段）归一化：last 用 close、lastClose 用 preClose 兜底。"""
    a = _new_adapter()
    bar = {"close": 10.5, "open": 10.2, "high": 10.8, "low": 10.1,
           "volume": 12345, "amount": 1.3e7}
    q = a._norm_quote("600000", bar)
    assert q["last"] == 10.5        # 曾为 None（K 线 bar 无 lastPrice）
    assert q["lastClose"] is None   # 无 preClose 时保持 None
    # tick 快照（CamelCase）不受影响
    tick = {"lastPrice": 11.0, "lastClose": 10.5, "bidPrice": [10.9, 10.8],
            "askPrice": [11.0, 11.1], "bidVolume": [100, 200], "askVolume": [150, 50]}
    q2 = a._norm_quote("600000", tick)
    assert q2["last"] == 11.0
    assert q2["lastClose"] == 10.5
    assert q2["bid"] == 10.9
    assert q2["ask_vol"] == 150


class _FakeQuoteSink:
    """捕获 subscribe_quote 注册的回调（模拟旧版单 code + callback 签名）。"""
    def __init__(self):
        self.callbacks: dict[str, object] = {}
        self.periods: dict[str, str] = {}

    def subscribe_quote(self, stock_code, period="1d", start_time="",
                        end_time="", count=0, callback=None):
        self.callbacks[stock_code] = callback
        self.periods[stock_code] = period


def test_subscribe_cb_old_list_payload():
    """旧版订阅回调载荷 {code: [bar, ...]}（值直接是 list）必须被推送（曾静默失效）。"""
    a = _new_adapter()
    captured = {}
    sink = _FakeQuoteSink()
    a._xtdata = sink
    a.subscribe_quote(["600000"], lambda evt: captured.update(evt))
    assert "600000" in sink.callbacks
    # 旧版载荷：{code: [bar1, bar2]} —— 值直接是 bar 列表，无 period 嵌套
    sink.callbacks["600000"]({"600000": [
        {"close": 10.0, "open": 9.9, "high": 10.1, "low": 9.8, "volume": 100},
        {"close": 10.2, "open": 10.0, "high": 10.3, "low": 9.9, "volume": 200},
    ]})
    assert captured["type"] == "quote"
    assert captured["data"]["code"] == "600000"
    assert captured["data"]["last"] == 10.2   # 最后一根 bar 的 close 兜底
    assert captured["data"]["volume"] == 200


def test_subscribe_cb_new_dict_payload():
    """新版订阅回调载荷 {code: {period: [bar, ...]}} 必须继续被推送（回归保护）。"""
    a = _new_adapter()
    captured = {}
    sink = _FakeQuoteSink()
    a._xtdata = sink
    a.subscribe_quote(["600000"], lambda evt: captured.update(evt))
    sink.callbacks["600000"]({"600000": {"1m": [
        {"close": 10.5, "volume": 500},
    ]}})
    assert captured["type"] == "quote"
    assert captured["data"]["code"] == "600000"
    assert captured["data"]["last"] == 10.5
    assert captured["data"]["volume"] == 500


# ---------------- place_order：参数顺序（交易安全关键） ----------------
def test_place_order_param_order():
    """order_stock 参数顺序必须与真实旧版 SDK 一致（原 bug：量/价/类型错位）。"""
    import xtquant_client.xtp as xtp_mod
    orig = xtp_mod._ensure_xtconstant
    xtp_mod._ensure_xtconstant = lambda: type("X", (), {
        "STOCK_BUY": 23, "STOCK_SELL": 24,
        "CREDIT_BUY": 23, "CREDIT_SELL": 24,
        "LATEST_PRICE": 5, "FIX_PRICE": 11})()
    try:
        a = _new_adapter()
        # 避免等待回调 10s 超时，只校验 order_stock 调用参数顺序
        a._wait_order_response = lambda seq, timeout=10.0: None
        res = a.place_order("600000", "buy", "limit", 10.5, 300,
                            strategy_name="s1", remark="r1")
        assert len(a._trader.order_calls) == 1
        c = a._trader.order_calls[0]
        assert c["stock_code"] == "600000"
        assert c["order_type"] == 23          # STOCK_BUY
        assert c["order_volume"] == 300
        assert c["price_type"] == 11          # FIX_PRICE
        assert c["price"] == 10.5
        assert c["strategy_name"] == "s1"
        assert c["order_remark"] == "r1"
        assert res["status"] == "unknown"     # 无回调，seq 未映射到真实 order_id
        # 市价单：price_type 应为 LATEST_PRICE=5
        a.place_order("000001", "sell", "market", 9.8, 200)
        c2 = a._trader.order_calls[1]
        assert c2["order_type"] == 24         # STOCK_SELL
        assert c2["price_type"] == 5
        assert c2["price"] == 9.8
        assert c2["order_volume"] == 200
    finally:
        xtp_mod._ensure_xtconstant = orig


# ---------------- get_positions：多版本字段兼容 ----------------
def test_get_positions_lowercase_and_camelcase():
    """持仓字段兼容旧版全小写与新版 CamelCase。"""
    a = _new_adapter()
    # 旧版全小写
    p1 = FakeXtPosition(stock_code="600000", volume=1000, can_use_volume=800,
                        open_price=10.0, market_value=12000.0)
    # 新版 CamelCase
    p2 = FakeXtPosition(StockCode="000001", Volume=500, CanUseVolume=400,
                        OpenPrice=8.5, MarketValue=4500.0)
    a._trader._positions = [p1, p2]
    rows = a.get_positions()
    assert len(rows) == 2
    r = {x["code"]: x for x in rows}
    assert r["600000"]["volume"] == 1000
    assert r["600000"]["avail"] == 800
    assert r["600000"]["cost"] == 10.0
    assert r["000001"]["volume"] == 500
    assert r["000001"]["avail"] == 400
    assert r["000001"]["cost"] == 8.5


# ---------------- K 线/订阅周期归一化（60m -> 1h） ----------------
def test_normalize_kline_period_60m_maps_to_1h():
    from xtquant_client.xtp import _normalize_kline_period
    assert _normalize_kline_period("60m") == "1h"
    assert _normalize_kline_period("1m") == "1m"
    assert _normalize_kline_period("1d") == "1d"
    assert _normalize_kline_period("tick") == "tick"
    assert _normalize_kline_period("") == "1d"
    assert _normalize_kline_period(None) == "1d"


class _FakeKlineDF:
    """伪 DataFrame：仅实现 get_kline 用到的 iterrows/len（行是 dict）。"""
    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        return iter(enumerate(self._rows))

    def __len__(self):
        return len(self._rows)


class _FakeKlineSink:
    def __init__(self):
        self.calls: list[dict] = []

    def get_market_data(self, **kwargs):
        self.calls.append(kwargs)
        code = kwargs["stock_list"][0]
        return {code: _FakeKlineDF([{
            "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
            "volume": 1000, "amount": 1e6}])}


def test_get_kline_60m_maps_to_1h():
    """get_kline("60m") 必须归一化为迅投协议周期 "1h"（旧版无 60m）。"""
    a = _new_adapter()
    sink = _FakeKlineSink()
    a._xtdata = sink
    rows = a.get_kline("600000", "60m", 5)
    assert sink.calls and sink.calls[0]["period"] == "1h"
    assert len(rows) == 1
    assert rows[0]["close"] == 10.5


def test_subscribe_quote_60m_maps_to_1h():
    a = _new_adapter()
    sink = _FakeQuoteSink()
    a._xtdata = sink
    a.subscribe_quote(["600000"], lambda evt: None, period="60m")
    assert sink.periods["600000"] == "1h"


# ---------------- seq→oid 映射并发锁 / 竞态 ----------------
def test_wait_order_response_callback_first_no_race():
    """回调先于 wait 注册到达：立即返回真实 oid，不超时、不误判 unknown。"""
    a = _new_adapter()
    a._handle_order_response({"seq": 7, "order_id": "REAL1"})  # 回调先到
    oid = a._wait_order_response(7, timeout=0.2)
    assert oid == "REAL1"


def test_wait_order_response_timeout_returns_none():
    a = _new_adapter()
    oid = a._wait_order_response(9999, timeout=0.05)  # 无回调
    assert oid is None


# ---------------- 占位适配器（未接入 SDK）：结构化错误，绝无假数据 ----------------
def test_placeholder_adapters_structured_error():
    from xtquant_client.adapters.juejin import JuejinAdapter
    from xtquant_client.adapters.ptrade import PTradeAdapter
    from xtquant_client.adapters.ths import ThsAdapter
    from xtquant_client.base import BrokerSDKError

    for cls in (ThsAdapter, PTradeAdapter, JuejinAdapter):
        ad = cls()
        # start 必须抛 SDK 缺失（带安装指引），而不是假成功
        try:
            ad.start()
        except BrokerSDKError as exc:
            assert "安装" in str(exc) or "pip" in str(exc)
        else:
            raise AssertionError(f"{cls.__name__}.start() 不应成功")
        # 查询类方法同样必须抛错，绝不返回假数据
        try:
            ad.get_account()
            raise AssertionError("不应返回假数据")
        except BrokerSDKError:
            pass
        try:
            ad.get_quote("600000")
            raise AssertionError("不应返回假数据")
        except BrokerSDKError:
            pass
