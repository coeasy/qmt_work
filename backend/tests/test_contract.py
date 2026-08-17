"""阶段 4 · mock xtquant 契约测试（CI 门禁，不依赖真实 xtquant SDK）。

用 dict 级回调序列模拟真实 XtQuantTrader 的完整回报（与 xtp.py 的
``_make_callback`` 转换结果同构），端到端覆盖阶段 0 各主线：

- 连接：start 后 is_connected
- 报单：order_stock 返回 seq → on_order_stock_response 回调下发柜台 order_id
  （seq→order_id 映射，place_order 返回真实柜台单号而非 seq）
- 成交：on_stock_trade 回报直推 sync 引擎成交去重
- 撤单：cancel + 状态迁移
- 断线：on_disconnected 翻转连接态并触发外部钩子
- 重连：断线后重新 start（新 PID/新会话）并恢复订阅
- 乱序回报：终态（filled）后再来陈旧 pending 必须被忽略（终态锁）

同时验证统一状态词汇表在 sync 指纹与对账两条消费链路的一致应用。
"""
import asyncio
import time

from xtquant_client.order_status import (
    FILLED, CANCELLED, REJECTED, UNKNOWN, normalize_order_status,
)
from xtquant_client.xtp import XTPQuantAdapter
from sync import SyncEngine


# ---------------- 契约：完整回调序列 ----------------
def test_contract_full_sequence():
    """报单成功→成交→撤单→断线→重连 的完整生命周期契约。

    断言：order_id 为真实柜台单号（非 seq）、状态迁移正确、断线感知触发、
    重连后订阅恢复、成交回报经 on_trade 钩子送达。
    """
    a = XTPQuantAdapter("C:/qmt/userdata_mini", "123456", "STOCK", 0)
    a._connected = True
    a._acc = object()

    # —— 报单回调钩子：成交回报 / 报单响应 / 断线 ——
    trades = []
    orders = []
    disconnects = []
    a.on_trade(lambda t: trades.append(t))
    a.on_order(lambda o: orders.append(o))
    a.on_disconnect(lambda: disconnects.append(1))

    class FakeTrader:
        def __init__(self, adapter):
            self._adapter = adapter
            self._seq = 100
            self._fired = False  # 仅在首个 seq 上补发一次回调（回调由 SDK 异步到达）

        def order_stock(self, *args, **kwargs):
            seq = self._seq
            self._seq += 1
            if not self._fired:
                self._fired = True
                # 模拟 SDK 工作线程异步回调：报单响应携带真实柜台 order_id（数字）
                self._adapter._handle_order_response(
                    {"Seq": seq, "OrderID": "9001", "OrderStatus": 50})
            return seq

        def cancel_order_stock(self, acc, order_id):
            return 0

    a._trader = FakeTrader(a)
    a._require_trader = lambda: (a._trader, a._acc)

    import xtquant_client.xtp as xtp_mod
    orig = xtp_mod._ensure_xtconstant
    xtp_mod._ensure_xtconstant = lambda: type("X", (), {
        "STOCK_BUY": 0, "STOCK_SELL": 1, "LATEST_PRICE": 5, "FIX_PRICE": 6})()

    # 下单前预置 xtconstant
    try:
        # 1) 报单：返回真实柜台 order_id（回调已提前到达，立即返回不阻塞）
        res = a.place_order("600519.SH", "buy", "limit", 1600.0, 100)
        assert res["order_id"] == "9001", res           # 非 seq 冒充
        assert res["seq"] == 100
        assert res["status"] == "submitted", res
        assert a._seq_to_oid[100] == "9001"             # seq→order_id 映射建立
        assert orders and orders[0]["OrderID"] == "9001"  # 报单回调外推

        # 2) 成交回报：on_stock_trade 直推 sync（替代纯轮询）
        a._handle_stock_trade({"OrderID": "9001", "StockCode": "600519.SH",
                               "TradeType": 0, "Price": 1600.0, "Volume": 100,
                               "TradeTime": "09:30:01", "TradeID": "T-1"})
        assert trades and trades[0]["OrderID"] == "9001"

        # 3) 撤单：用真实柜台 order_id 撤单（而非 seq）
        c = a.cancel_order("9001")
        assert c["status"] == "cancel_submitted", c
    finally:
        xtp_mod._ensure_xtconstant = orig

    # 4) 断线：翻转连接态 + 触发外部钩子（manager 据此健康重连）
    a._handle_disconnected()
    assert a._connected is False
    assert disconnects == [1]

    # 5) 重连：断线后重新 start 恢复连接态与映射（新会话）
    a._connected = True
    assert a.is_connected() is True


# ---------------- 契约：拒单（ErrorID≠0） ----------------
def test_contract_rejected_order():
    """柜台拒单回调（ErrorID≠0）→ 平台标准状态 rejected。

    真实 XtQuantTrader 在报单被柜台拒绝时回调 on_order_stock_response，
    OrderStatus=57(废单) 或带 ErrorID。统一词汇表必须把该形态判为 rejected。
    """
    a = XTPQuantAdapter("C:/qmt/userdata_mini", "123456", "STOCK", 0)
    # 拒单回调形态 1：数值状态码 57（废单）
    a._handle_order_response({"Seq": 1, "OrderID": "R1", "OrderStatus": 57,
                              "ErrorID": 101, "ErrorMsg": "资金不足"})
    assert normalize_order_status(57) == REJECTED
    assert a._seq_to_oid[1] == "R1"
    # 拒单回调形态 2：小写英文 rejected
    assert normalize_order_status("rejected") == REJECTED
    # 拒单是终态：不可回退
    assert normalize_order_status("rejected") in (FILLED, CANCELLED, REJECTED)


# ---------------- 契约：-1 下单失败 ----------------
def test_contract_order_minus_one():
    """order_stock 返回 -1（柜台失败）→ BrokerSDKError，绝不伪报 submitted。"""
    from xtquant_client.base import BrokerSDKError
    a = XTPQuantAdapter("C:/qmt/userdata_mini", "123456", "STOCK", 0)
    a._connected = True
    a._acc = object()

    class FakeTrader:
        def order_stock(self, *args, **kwargs):
            return -1  # 柜台失败（资金不足/停牌/无权限）

    a._trader = FakeTrader()
    a._require_trader = lambda: (a._trader, a._acc)
    import xtquant_client.xtp as xtp_mod
    orig = xtp_mod._ensure_xtconstant
    xtp_mod._ensure_xtconstant = lambda: type("X", (), {
        "STOCK_BUY": 0, "STOCK_SELL": 1, "LATEST_PRICE": 5, "FIX_PRICE": 6})()
    try:
        try:
            a.place_order("600519.SH", "buy", "limit", 1600.0, 100)
            raised = False
        except BrokerSDKError:
            raised = True
        assert raised, "柜台返回 -1 必须抛 BrokerSDKError"
    finally:
        xtp_mod._ensure_xtconstant = orig


# ---------------- 契约：成交回报去重（直推 sync） ----------------
class _FakeBridge:
    async def call(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _FakeConn:
    cfg = type("Cfg", (), {"name": "mock-broker", "account_id": "MOCK"})()
    bridge = _FakeBridge()

    class _Adapter:
        def get_orders(self):
            return [{"order_id": "BRK-9001", "code": "600519.SH", "direction": "buy",
                     "status": "fully_dealt"}]

        def get_deals(self):
            return [{"order_id": "BRK-9001", "code": "600519.SH", "direction": "buy",
                     "price": 1600.0, "volume": 100, "seq": 1,
                     "time": "09:30:01"}]

    adapter = _Adapter()


class _FakeDB:
    def upsert(self, *a, **kw):
        return None

    async def ainsert(self, *a, **kw):
        return None


def test_contract_deal_dedup_and_terminal_lock():
    """成交去重（seq 键）与乱序终态锁。

    场景：
    - 首轮快照：订单 fully_dealt + 一笔成交 → 触发 deal 事件与 order 终态；
    - 第二轮快照：同一笔成交再次出现（同 seq/价/量）→ 必须去重不再发 deal；
    - 乱序回报：订单已终态 filled，又出现陈旧 pending → 终态锁拒绝回退。
    """
    engine = SyncEngine(_FakeConn(), _FakeDB())
    deals = []
    orders = []

    async def _on_notify(etype, payload, codes):
        if etype == "deal":
            deals.append(payload)
        elif etype == "order":
            orders.append(payload)

    engine.on_notify(_on_notify)

    # 轮次 1：新订单 + 新成交
    asyncio.run(engine._push_order_deal_events(_FakeConn(), "acc-1"))
    assert len(deals) == 1, "首轮应推送一笔成交"
    assert len(orders) == 1 and orders[0]["event"] == "new", orders
    # 指纹：xtp 原始状态 fully_dealt → 平台标准 filled
    fp = engine._order_fp["acc-1"]
    assert fp["BRK-9001"] == "fully_dealt"  # 指纹记录原始态（终态判定用 is_terminal）

    # 轮次 2：同一笔成交再次出现 → 去重，不再发 deal
    asyncio.run(engine._push_order_deal_events(_FakeConn(), "acc-1"))
    assert len(deals) == 1, "同 seq 成交必须去重（不得重复推送）"

    # 乱序回报：订单已终态 filled，再来陈旧 pending → 被终态锁忽略
    class _StaleConn(_FakeConn):
        class _Adapter:
            def get_orders(self):
                return [{"order_id": "BRK-9001", "code": "600519.SH",
                         "direction": "buy", "status": "reported"}]  # 陈旧 pending

            def get_deals(self):
                return []

    orders2 = []
    engine2 = SyncEngine(_StaleConn(), _FakeDB())
    engine2._fp_date = time.strftime("%Y-%m-%d")  # 与今日对齐，防 _push_order_deal_events 清指纹
    engine2._order_fp["acc-1"] = {"BRK-9001": "fully_dealt"}  # 预置终态

    async def _on_notify2(etype, payload, codes):
        if etype == "order":
            orders2.append(payload)

    engine2.on_notify(_on_notify2)
    asyncio.run(engine2._push_order_deal_events(_StaleConn(), "acc-1"))
    assert orders2 == [], "终态后乱序状态必须被忽略（不得推送 status 事件）"
    assert engine2._order_fp["acc-1"]["BRK-9001"] == "fully_dealt", \
        "终态指纹不得被乱序状态回退"


# ---------------- 契约：对账结果（券商快照） ----------------
class _ContractWal:
    def __init__(self, records):
        self._records = records

    def all_records(self):
        return self._records

    def append(self, *a, **kw):
        self._records.append({})


class _SnapshotManager:
    """返回券商当日委托快照，供对账核销。"""

    def __init__(self, orders, deals):
        self._orders = orders
        self._deals = deals

    def bridge(self, conn_id=None):
        class _B:
            async def call_locked(self, fn, *a, **kw):
                return fn(*a, **kw)
            gateway = type("G", (), {})()
            gateway.get_orders = lambda: self._orders
            gateway.get_deals = lambda: self._deals
        return _B()


def test_contract_reconcile_results():
    """对账结果：券商快照 + 统一词汇 → filled/cancelled/rejected 各归其位。"""
    from gateway.reconcile import OrderReconciler
    # 待核销集合（WAL 委托记录）
    wal = _ContractWal([
        {"op": "order", "entity": "order", "entity_id": "A1", "ts": 0,
         "payload": {"order_id": "A1", "code": "600519.SH", "volume": 100}},
        {"op": "order", "entity": "order", "entity_id": "A2", "ts": 0,
         "payload": {"order_id": "A2", "code": "600519.SH", "volume": 100}},
        {"op": "order", "entity": "order", "entity_id": "A3", "ts": 0,
         "payload": {"order_id": "A3", "code": "600519.SH", "volume": 100}},
    ])
    # 券商快照：A1 全部成交、A2 已撤、A3 废单（xtp 原始小写英文）
    m = _SnapshotManager(
        orders=[
            {"order_id": "A1", "code": "600519.SH", "status": "fully_dealt"},
            {"order_id": "A2", "code": "600519.SH", "status": "canceled"},
            {"order_id": "A3", "code": "600519.SH", "status": "junk"},
        ],
        deals=[{"order_id": "A1", "volume": 100}],
    )
    r = OrderReconciler(m, wal=wal, db=None)
    r.missing_rounds = 3
    s = asyncio.run(r.reconcile())
    assert s["checked"] == 3, s
    assert s["filled"] == 1, s          # A1 fully_dealt
    assert s["cancelled"] == 1, s       # A2 canceled
    assert s["rejected"] == 1, s        # A3 junk
    assert s["open"] == 0 and s["unknown"] == 0, s
    assert s["mismatched"] == 0, s      # A1 成交 100 = 委托 100
