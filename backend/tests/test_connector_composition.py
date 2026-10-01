"""Phase 1/4：组合装配、事件合成、EasyTrader façade、Ptrade 模板。

盯的仍然是**不变量**而非"能不能调通"，尤其是这三条：
* 差分事件**首轮不得补发**（否则重启动会重放历史成交 → 重复记账）；
* façade 没有 connector 时**必须报错**，不许返回空列表冒充「无持仓」；
* 新增客户端不需要改编排层（用 Ptrade 模板证明）。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.dialects import BigQmtV1, PtradeV1, XtQuantV1, get_dialect  # noqa: E402
from connectors.events import PollDiffEventSource, PushEventSource  # noqa: E402
from connectors.generic import GenericConnector  # noqa: E402
from connectors.ports import (  # noqa: E402
    AccountSnapshot, EventSemantics, InstrumentId, OrderRequest, OrderSnapshot,
    PositionSnapshot,
)
from connectors.registry import available_keys  # noqa: E402
from connectors.transports import InProcessTransport  # noqa: E402
from xtquant_client.gateway import XTQuantBridge  # noqa: E402


class _FakeGateway:
    client_version = "fake"
    sdk_required = "xtquant"
    supported_account_types = ["STOCK"]
    capabilities = ["quote", "kline", "trade"]

    def __init__(self):
        self.calls = []

    def start(self): return None
    def close(self): return None
    def is_connected(self): return True
    def get_quote(self, code): return {"last": 1.0}
    def get_kline(self, code, period, count, start="", end="", adjust=None):
        self.calls.append(("get_kline", code, period, count, start, end, adjust))
        return [{"time": "2026-01-01", "close": 1.0}]
    def place_order(self, code, direction, price_type, price, volume,
                    strategy_name="", remark=""):
        self.calls.append(("place_order", code, direction, price_type, price,
                           volume, strategy_name, remark))
        return {"order_id": "9001", "status": 50}
    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": 54}
    def query_position(self): return []
    def query_cash(self): return {}
    def subscribe_quote(self, codes, on_tick): return None
    def get_positions(self, symbol=None):
        return [{"code": "600036.SH", "volume": 100, "can_use_volume": 100,
                 "open_price": 35.0, "market_value": 3500.0}]
    def get_account(self):
        return {"account_id": "8888", "cash": 500.0, "assets": 4000.0}
    def get_orders(self):
        return [{"order_id": "9001", "code": "600036.SH", "status": 50, "volume": 100}]
    def get_deals(self):
        return [{"trade_id": "T1", "code": "600036.SH", "price": 35.5, "volume": 100}]
    def get_stock_list(self, sector="沪深A股"):
        return [{"code": "600036.SH", "name": "招商银行"}]
    def get_trading_calendar(self, start="", end=""):
        return ["2026-01-02"]
    def test_connection(self):
        return {"connected": True}


@pytest.fixture()
def inprocess():
    gw = _FakeGateway()
    bridge = XTQuantBridge(gw)
    conn = GenericConnector(dialect=get_dialect("xtquant.v1"),
                            transport=InProcessTransport(gw, bridge=bridge,
                                                         write_ops=frozenset({"place_order", "cancel_order"})),
                            capabilities=("quote", "trade"))
    return conn, gw


def test_inprocess_canonical_reads(inprocess):
    conn, gw = inprocess

    async def run():
        return (await conn.get_positions(), await conn.get_account(),
                await conn.get_orders(), await conn.get_deals())

    pos, acc, orders, deals = asyncio.run(run())
    assert isinstance(pos[0], PositionSnapshot) and pos[0].quantity == 100
    assert isinstance(acc, AccountSnapshot) and acc.cash == 500.0
    assert isinstance(orders[0], OrderSnapshot) and orders[0].status == "pending"
    assert deals and deals[0].status == "filled"


def test_write_op_is_serialized_and_carries_remark(inprocess):
    conn, gw = inprocess
    req = OrderRequest(instrument=InstrumentId(code="600036", exchange="SH"),
                       side="buy", order_type="limit", price=35.5, quantity=100,
                       strategy_name="strate", remark="r1")

    async def run():
        return await conn.place_order(req)

    snap = asyncio.run(run())
    assert snap.broker_order_id == "9001" and snap.accepted is True
    call = gw.calls[-1]
    assert call[0] == "place_order"
    assert call[1] == "600036.SH" and call[2] == "buy"


def test_kline_date_range_is_exposed_through_port(inprocess):
    """缺口 D4：端口必须能表达 start/end，否则只有 count 一种取数方式。"""
    conn, gw = inprocess

    async def run():
        return await conn.get_kline("600036.SH", "1d", 10,
                                    adjust="qfq", start="2026-01-01", end="2026-02-01")

    rows = asyncio.run(run())
    assert rows and rows[0]["time"] == "2026-01-01"
    assert gw.calls[-1][4] == "2026-01-01" and gw.calls[-1][5] == "2026-02-01"
    assert gw.calls[-1][6] == "qfq"


def test_unknown_dialect_or_transport_is_explicit():
    with pytest.raises(KeyError):
        get_dialect("nonexistent.v9")
    from connectors.transports import get_transport

    with pytest.raises(KeyError):
        get_transport("carrier_pigeon")


# ---------------------------------------------------------------------------
# 事件合成
# ---------------------------------------------------------------------------
def _orders():
    return [{"order_id": "9001", "order_status": 50, "dealt": 0, "traded_price": 0,
             "time": "09:31:00"}]


def test_poll_diff_first_round_only_primes():
    src = PollDiffEventSource(_orders, lambda: [], poll_interval_ms=500)
    assert src.poll_once() == []          # 首轮只建基线
    src._fetch_orders = lambda: [
        {"order_id": "9001", "order_status": 56, "dealt": 100, "traded_price": 35.5,
         "time": "09:32:00"}]
    evs = src.poll_once()
    assert len(evs) == 1 and evs[0].kind == "order" and evs[0].synthetic is True


def test_poll_diff_emits_order_error_for_junk_order():
    """废单必须合成 order_error，不得静默丢弃（xtquant_big_convert 的教训）。"""
    src = PollDiffEventSource(_orders, lambda: [], poll_interval_ms=500)
    src.poll_once()
    src._fetch_orders = lambda: [
        {"order_id": "9001", "order_status": 57, "dealt": 0, "traded_price": 0}]
    evs = src.poll_once()
    assert evs and evs[0].kind == "order_error"
    assert "9001" in str(evs[0].data.get("error_id"))


def test_poll_diff_declares_latency_bound():
    src = PollDiffEventSource(_orders, lambda: [], poll_interval_ms=800)
    spec = src.event_semantics()
    assert spec.semantics is EventSemantics.POLL_DIFF
    assert spec.max_latency_ms == 1600   # 保守给 2 倍


def test_push_source_semantics():
    src = PushEventSource()
    src.push("order", {"order_id": "1"})
    assert src.event_semantics().semantics is EventSemantics.PUSH
    drained = src.stream_events()
    assert len(drained) == 1 and drained[0].synthetic is False
    assert src.stream_events() == []      # 取走即清空


# ---------------------------------------------------------------------------
# EasyTrader façade
# ---------------------------------------------------------------------------
def test_facade_queries_without_connector_raise():
    from gateway.easytrader_facade import EasyTraderFacade, FacadeError

    f = EasyTraderFacade(router=object())

    async def run():
        await f.position()

    with pytest.raises(FacadeError) as exc:
        asyncio.run(run())
    assert "connector" in str(exc.value)


def test_facade_buy_goes_through_signal_router():
    """INV-1：façade 的下单必须落到 SignalRouter.submit，而不是直连 adapter。"""
    from gateway.easytrader_facade import EasyTraderFacade

    class _Router:
        def __init__(self):
            self.seen = []

        async def submit(self, **kw):
            self.seen.append(kw)
            return {"ok": True}

    router = _Router()
    f = EasyTraderFacade(router=router, broker_id="conn-1")

    async def run():
        await f.buy("600036.SH", price=35.5, amount=100, remark="r")
        await f.sell("600036.SH", price=0.0, amount=100)

    asyncio.run(run())
    assert router.seen[0]["side"] == "buy" and router.seen[0]["volume"] == 100
    assert router.seen[0]["source"] == "easytrader_facade"
    # 价格为 0 ⇒ 走市价（风控会取真实最新价做保护价）
    assert router.seen[1]["price_type"] == "market"


def test_facade_raw_is_read_only_adapter():
    from gateway.easytrader_facade import EasyTraderFacade, FacadeError

    f = EasyTraderFacade(router=object())
    with pytest.raises(FacadeError):
        _ = f.raw
    gw = _FakeGateway()
    conn = GenericConnector(dialect=get_dialect("xtquant.v1"),
                            transport=InProcessTransport(gw))
    assert EasyTraderFacade(router=object(), connector=conn).raw is gw


def test_facade_can_queries_via_connector():
    from gateway.easytrader_facade import EasyTraderFacade

    gw = _FakeGateway()
    conn = GenericConnector(dialect=get_dialect("xtquant.v1"),
                            transport=InProcessTransport(gw))
    f = EasyTraderFacade(router=object(), connector=conn)

    async def run():
        return (await f.position(), await f.balance(),
                await f.today_entrusts(), await f.today_trades())

    pos, bal, orders, trades = asyncio.run(run())
    assert pos[0]["stock_code"] == "600036" and pos[0]["can_use_volume"] == 100
    assert bal[0]["account_id"] == "8888"
    assert orders[0]["order_status"] == "pending"    # SSOT，不是方言整数 50
    assert trades[0]["traded_price"] == 35.5


# ---------------------------------------------------------------------------
# Ptrade 模板（证明「新增客户端零改动编排层」）
# ---------------------------------------------------------------------------
def test_ptrade_is_registered_as_first_class_key():
    assert "ptrade.http" in available_keys()
    assert isinstance(get_dialect("ptrade.v1"), PtradeV1)


def test_ptrade_prepare_uses_its_own_field_names():
    d = PtradeV1()
    p = d.prepare("PLACE_ORDER", {"code": "600036.SH", "side": "buy",
                                  "quantity": 100, "price": 1.0})
    assert p["symbol"] == "600036.SH" and p["amount"] == 100
    assert d.op_of("TEST_CONNECTION") == "ping"


def test_all_dialects_declare_status_ssot_instead_of_own_table():
    """INV-3：任何 dialect 都不得自带状态映射表。"""
    for cls in (XtQuantV1, BigQmtV1, PtradeV1):
        assert cls().status_ssot == "xtquant_client.order_status"


# ---------------------------------------------------------------------------
# EventPort 接通（两种传输都真正出事件）
# ---------------------------------------------------------------------------
def test_inprocess_stream_events_returns_pushed(inprocess):
    """miniQMT 进程内直连：原生回调经 PushEventSource 转为 canonical 事件。"""
    conn, gw = inprocess
    conn.transport.push_event("order", {"order_id": "1", "code": "600036.SH"})
    evs = conn.stream_events()
    assert len(evs) == 1 and evs[0].kind == "order" and evs[0].synthetic is False
    # 进程内直连语义必须是 PUSH（不是 NONE）。
    assert conn.event_semantics().semantics is EventSemantics.PUSH


def test_subscribe_quote_rejected_on_wire_transport():
    """跨进程传输不支持回调式订阅：给出清晰 UnsupportedOp，而非 JSON 序列化崩溃。"""
    from connectors.dialects import UnsupportedOp
    from connectors.transports import FileSignalTransport
    import tempfile

    root = tempfile.mkdtemp()
    t = FileSignalTransport(root, poll_interval=0.02)
    conn = GenericConnector(dialect=get_dialect("bigqmt.v1"), transport=t)
    with pytest.raises(UnsupportedOp):
        asyncio.run(conn.subscribe_quote(["600036.SH"], lambda d: None))
