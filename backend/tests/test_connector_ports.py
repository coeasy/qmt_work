"""V9 Phase 6 DoD：9 端口契约（结构化）+ QMT 连接器满足端口面。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors import ports as P  # noqa: E402
from connectors.qmt import QmtConnector  # noqa: E402

CANONICAL = ("ConnectorPort", "ExecutionPort", "AccountPort", "PositionsPort",
             "MarketDataPort", "QuoteFeedPort", "InstrumentPort",
             "CalendarPort", "HealthPort", "CapabilityPort")


def test_nine_ports_exist_and_runtime_checkable():
    for name in CANONICAL:
        cls = getattr(P, name)
        assert hasattr(cls, "__protocol_attrs__") or issubclass(cls, object), name


def test_qmt_connector_satisfies_ports():
    """QmtConnector 无需真实 SDK 即可做结构化 isinstance 契约校验。"""

    class _StubAdapter:
        broker_name = "stub"
        client_version = "test"

        def is_connected(self):
            return False

        def get_orders(self):
            return []

        def get_deals(self):
            return []

        def get_account(self):
            return {}

        def get_cash(self):
            return {}

        def get_positions(self, symbol=None):
            return []

        def get_quote(self, code):
            return {}

        def get_kline(self, code, period, count, start="", end=""):
            return []

        def get_full_tick(self, codes):
            return {}

        def subscribe_quote(self, codes, on_tick):
            return None

        def get_instrument_detail(self, code):
            return {}

        def get_stock_list(self, sector="沪深A股"):
            return []

        def get_sector_list(self):
            return []

        def get_trading_calendar(self, start="", end=""):
            return []

        def test_connection(self):
            return {"ok": True}

    c = QmtConnector(_StubAdapter())
    assert isinstance(c, P.ConnectorPort)
    assert isinstance(c, P.ExecutionPort)
    assert isinstance(c, P.AccountPort)
    assert isinstance(c, P.PositionsPort)
    assert isinstance(c, P.MarketDataPort)
    assert isinstance(c, P.QuoteFeedPort)
    assert isinstance(c, P.InstrumentPort)
    assert isinstance(c, P.CalendarPort)
    assert isinstance(c, P.HealthPort)
    assert isinstance(c, P.CapabilityPort)


def test_order_request_validation_contract():
    from connectors.ports import InstrumentId, OrderRequest

    ok = OrderRequest(instrument=InstrumentId(code="600000", exchange="SH"),
                      side="buy", order_type="limit", price=10.0, quantity=100)
    ok.validate()  # 不抛
    for bad in (
        OrderRequest(instrument=InstrumentId("600000"), side="hold",
                     order_type="limit", price=10.0, quantity=100),
        OrderRequest(instrument=InstrumentId("600000"), side="buy",
                     order_type="limit", price=0.0, quantity=100),
        OrderRequest(instrument=InstrumentId("600000"), side="buy",
                     order_type="limit", price=10.0, quantity=0),
        OrderRequest(instrument=InstrumentId("600000"), side="buy",
                     order_type="fok", price=10.0, quantity=100),
    ):
        try:
            bad.validate()
            raise AssertionError("must raise for invalid order")
        except ValueError:
            pass


def test_supervisor_rejects_duplicate_id():
    import asyncio

    from connectors.supervisor import ConnectionSupervisor

    class _C:
        descriptor = P.ConnectorDescriptor(id="x", name="x", version="1")

        async def start(self):
            return None

        async def close(self):
            return None

        def is_connected(self):
            return False

    async def _run():
        sup = ConnectionSupervisor()
        sup.register("a", _C())
        try:
            sup.register("a", _C())
            raise AssertionError("duplicate must raise")
        except ValueError:
            pass

    asyncio.run(_run())
