"""Phase 3 ExecutionService contract tests."""
import asyncio

from gateway.execution import ExecutionService


class _Risk:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.prices = []

    def check_order(self, code, price, volume, direction, price_type="limit"):
        self.prices.append(price)
        return self.allowed, "blocked" if not self.allowed else ""


class _Gateway:
    def __init__(self, quote=None):
        self.quote = quote
        self.calls = []

    def get_quote(self, code):
        return self.quote

    def place_order(self, *args):
        self.calls.append(args)
        return {"code": 0, "order_id": "O1", "status": "submitted"}

    def cancel_order(self, order_id):
        return {"code": 0, "order_id": order_id}


class _Bridge:
    def __init__(self, quote=None):
        self.gateway = _Gateway(quote)

    async def call(self, fn, *args):
        return fn(*args)

    async def call_locked(self, fn, *args):
        return fn(*args)


def test_market_order_risk_uses_real_quote():
    async def run():
        risk = _Risk()
        bridge = _Bridge({"last": 12.5})
        result = await ExecutionService(risk=risk).place_order(
            bridge, "600519.SH", "buy", 100, 0, "market")
        assert result["ok"] is True
        assert risk.prices == [12.5]
        assert bridge.gateway.calls[0][3] == 0

    asyncio.run(run())


def test_market_order_without_quote_is_rejected_without_gateway_order():
    async def run():
        risk = _Risk()
        bridge = _Bridge(None)
        result = await ExecutionService(risk=risk).place_order(
            bridge, "600519.SH", "buy", 100, 0, "market")
        assert result["ok"] is False
        assert "真实最新价" in result["reason"]
        assert bridge.gateway.calls == []
        assert risk.prices == []

    asyncio.run(run())


def test_risk_rejection_prevents_gateway_order():
    async def run():
        risk = _Risk(allowed=False)
        bridge = _Bridge()
        result = await ExecutionService(risk=risk).place_order(
            bridge, "600519.SH", "buy", 100, 10, "limit")
        assert result == {"ok": False, "reason": "blocked"}
        assert bridge.gateway.calls == []

    asyncio.run(run())


def test_cancel_result_has_unified_success_flag():
    async def run():
        bridge = _Bridge()
        result = await ExecutionService().cancel_order(bridge, "O1")
        assert result["ok"] is True

    asyncio.run(run())
