"""paper 模式查询端点分流 —— 2026-10-12 修复的守护测试。

## 缺陷回顾（实测复现，非推测）

``/trade/order`` 早在 2026-09-20 就按执行模式分流（只有 live 才要求券商），
但四个**查询**端点没有：``/trade/positions``、``/trade/orders``、
``/trade/deals``、``/account/status`` 一律 ``_need()`` 无条件捅券商。
本机实测（2026-10-12，paper 模式、大QMT 桥 connected、agent 无注入函数）：

    POST /trade/order  → 200 PAPER-9 filled        ← 模拟盘成交成功
    GET  /trade/orders → 503「缺少券商 SDK：未捕获到 get_trade_detail_data」

即「**单成交了却永远查不回来**」：Trade 页三个页签在 ``catch {}`` 静默兜底下
恒为空，用户刚下的模拟盘委托/持仓/成交一笔都看不到。归因不是券商问题
（桥是连着的），是路由层把模拟盘查询指向了券商。

## 本文件锁死什么

1. paper 模式：四个查询端点返回**本地 PaperEngine 账本**（契约形状），
   且**绝不触碰券商**（spy 断言 get_orders/get_deals/get_positions 零调用）；
2. 契约形状：order_id 与 ``submit_order`` 返回的 ``PAPER-{id}`` 逐字符一致
   （撤单路径的 ``is_paper_order_id`` 依赖该前缀）、status 恒 filled、
   deal_id==order_id、持仓行带 cost/price/profit 契约字段；
3. dry_run 模式：503 + 模式归因（预演不产生任何委托/持仓/成交），
   不返回空列表冒充「查过了」；
4. live 模式：券商路径**原样不变**（防修复过度伤到实盘查询）；
5. 引擎未注入：503 明确失败，绝不静默假装成交（零 mock）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.middleware.envelope import EnvelopeMiddleware  # noqa: E402
from app.routes import router as api_router  # noqa: E402
from core.config import settings  # noqa: E402
from core.context import AppContext, set_active_context  # noqa: E402
from core.db import DB  # noqa: E402
from engines.paper_engine import PaperEngine, is_paper_order_id  # noqa: E402
from gateway.signal_router import SignalRouter  # noqa: E402

import gateway.idempotency as idem  # noqa: E402


# ============================ 假券商（live 路径唯一被替换的外层） ============================

class SpyGateway:
    """记录调用的最小券商适配器（live 模式查询必须走它）。"""

    def __init__(self):
        self.orders: list[dict] = []
        self.calls: list[str] = []

    def is_connected(self) -> bool:
        return True

    def get_quote(self, code: str):
        self.calls.append("get_quote")
        return {"code": code, "last": 12.0, "volume": 1_000}

    def place_order(self, code, direction, price_type, price, volume,
                    strategy="", remark=""):
        oid = f"OID-{len(self.orders) + 1}"
        self.orders.append({"order_id": oid, "code": code, "direction": direction,
                            "price_type": price_type, "price": price,
                            "volume": volume, "status": "filled", "dealt": volume})
        return {"order_id": oid, "ok": True}

    def cancel_order(self, order_id):
        return {"ok": True, "order_id": order_id}

    def get_orders(self):
        self.calls.append("get_orders")
        return list(self.orders)

    def get_deals(self):
        self.calls.append("get_deals")
        return [{"order_id": o["order_id"], "code": o["code"],
                 "direction": o["direction"], "price": o["price"],
                 "volume": o["volume"], "seq": 1} for o in self.orders]

    def get_positions(self, symbol=None):
        self.calls.append("get_positions")
        rows = [{"code": "600519.SH", "name": "贵州茅台", "volume": 100,
                 "avail": 100, "cost": 1500.0, "market_value": 150_000.0}]
        if symbol:
            return [r for r in rows if r["code"] == symbol]
        return rows

    def get_cash(self):
        self.calls.append("get_cash")
        return {"cash": 500_000.0, "assets": 650_000.0}


class SpyBridge:
    def __init__(self, gateway: SpyGateway):
        self.gateway = gateway

    async def call(self, fn, *args):
        return fn(*args)


class SpyManager:
    def __init__(self, bridge: SpyBridge):
        self._bridge = bridge

    def bridge(self, conn_id=None):
        return self._bridge

    def active_bridge(self):
        return self._bridge

    def all_connections(self):
        return []


# ==================================== 装配 ====================================

class Harness:
    """真实路由 + 真实 SignalRouter + 真实 PaperEngine；只换最外层券商。"""

    def __init__(self, tmp_path: Path, *, paper_engine=True):
        self.tmp = tmp_path
        self.db = DB(tmp_path / "paper_mode.db")
        self.events: list[dict] = []
        self.gateway = SpyGateway()
        self.bridge = SpyBridge(self.gateway)
        self.manager = SpyManager(self.bridge)
        self.paper = (PaperEngine(initial_capital=1_000_000.0).init(self.db)
                      if paper_engine else None)
        self.router = SignalRouter(self.manager, risk=None, db=self.db,
                                   wal=None, notifier=None,
                                   on_event=lambda e, p=None: self.events.append(
                                       e if isinstance(e, dict) else {"type": e, "data": p}),
                                   paper_engine=self.paper)
        self.router.mode = "paper"

        self.ctx = AppContext()
        self.ctx.update(
            db=self.db, broker_manager=self.manager, bridge=self.bridge,
            signal_router=self.router, lifecycle_ready=True,
            started_at=0.0,
            phase_status={p: "ready" for p in ("db", "engines", "watchdogs",
                                               "replay", "misc")},
        )
        self.app = FastAPI()
        self.app.add_middleware(EnvelopeMiddleware)
        self.app.include_router(api_router)
        set_active_context(self.ctx)

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app),
                           base_url="http://paper.test",
                           headers={"X-API-Key": settings.api_key})

    def run(self, coro):
        return asyncio.run(coro)

    def close(self):
        set_active_context(AppContext())
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(autouse=True)
def _clean_globals():
    """幂等缓存与持仓现价缓存都是模块级全局：不清会跨用例污染。"""
    import app.services.positions as pos_svc
    with idem._lock:
        idem._cache.clear()
        idem._inflight.clear()
    pos_svc.clear_price_cache()
    yield
    with idem._lock:
        idem._cache.clear()
        idem._inflight.clear()
    pos_svc.clear_price_cache()


@pytest.fixture()
def h(tmp_path):
    harness = Harness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture()
def h_no_engine(tmp_path):
    """paper 模式但引擎未注入（bootstrap 失败/极简装配的形态）。"""
    harness = Harness(tmp_path, paper_engine=False)
    try:
        yield harness
    finally:
        harness.close()


def _data(resp) -> dict:
    body = resp.json()
    assert isinstance(body, dict) and "code" in body, f"非信封响应：{body}"
    return body


def _ok(resp):
    body = _data(resp)
    assert body["code"] == 0, f"期望 code=0，实际 {body}"
    return body.get("data")


# ============================ paper：账本查询，零券商触碰 ============================

def test_paper_order_then_queries_roundtrip(h):
    """paper 下单 → 三个查询端点原样读回，且全程不碰券商。

    这正是修复前的断链形态：POST 200 filled，GET 全部 503 捅券商。
    """
    async def scenario():
        async with h.client() as c:
            placed = _ok(await c.post("/api/v1/trade/order", json={
                "code": "513090", "direction": "buy", "volume": 100,
                "price": 1.739, "price_type": "limit",
                "remark": "paper-mode-query-test"}))
            assert placed["ok"] is True and placed["mode"] == "paper", placed
            order_id = placed["order_id"]
            assert order_id.startswith("PAPER-"), order_id

            orders = _ok(await c.get("/api/v1/trade/orders"))
            assert isinstance(orders, list) and len(orders) == 1, orders
            row = orders[0]
            # ★ 委托号必须与 submit_order 返回逐字符一致（撤单前缀判据依赖它）
            assert row["order_id"] == order_id
            assert row["status"] == "filled" and row["filled"] == 100
            assert row["side"] == "buy" and row["code"] == "513090"
            assert row["price"] == 1.739 and row["volume"] == 100

            deals = _ok(await c.get("/api/v1/trade/deals"))
            assert len(deals) == 1, deals
            d = deals[0]
            assert d["deal_id"] == order_id and d["order_id"] == order_id
            assert d["amount"] == pytest.approx(173.9)

            positions = _ok(await c.get("/api/v1/trade/positions"))
            assert len(positions) == 1, positions
            p = positions[0]
            assert p["code"] == "513090" and p["volume"] == 100
            assert p["avail"] == 100            # 模拟盘无资金冻结 ⇒ 全额可卖
            assert p["cost"] == pytest.approx(1.739)
            assert p["market_value"] == pytest.approx(173.9)

    h.run(scenario())
    # ★ 核心断言：paper 查询**零券商调用**（修复前四个端点全部捅券商）
    assert h.gateway.calls == [], f"paper 查询不应触碰券商，实际调用了 {h.gateway.calls}"


def test_paper_positions_symbol_filter(h):
    """symbol 过滤：精确代码与去交易所后缀的裸代码都应命中。"""
    async def scenario():
        async with h.client() as c:
            await c.post("/api/v1/trade/order", json={
                "code": "513090", "direction": "buy", "volume": 100, "price": 1.739})
            await c.post("/api/v1/trade/order", json={
                "code": "600519.SH", "direction": "buy", "volume": 100, "price": 10.0})
            exact = _ok(await c.get("/api/v1/trade/positions", params={"symbol": "513090"}))
            assert [p["code"] for p in exact] == ["513090"], exact
            # 查裸代码命中带后缀持仓（与用户输入习惯一致）
            bare = _ok(await c.get("/api/v1/trade/positions", params={"symbol": "600519"}))
            assert [p["code"] for p in bare] == ["600519.SH"], bare
            miss = _ok(await c.get("/api/v1/trade/positions", params={"symbol": "000001"}))
            assert miss == [], miss

    h.run(scenario())


def test_paper_account_status_marks_source(h):
    """paper 模式 /account/status 返回账本且显式标注来源（connected=False）。"""
    async def scenario():
        async with h.client() as c:
            await c.post("/api/v1/trade/order", json={
                "code": "513090", "direction": "buy", "volume": 100, "price": 1.739})
            st = _ok(await c.get("/api/v1/account/status"))
            # 模拟持仓不得冒充券商账户：connected=False + mode 标注
            assert st["connected"] is False and st["mode"] == "paper", st
            assert st["position_count"] == 1 and len(st["positions"]) == 1
            assert st["cash"] == pytest.approx(1_000_000 - 173.9 - 5.0)
            assert st["assets"] == pytest.approx(1_000_000 - 173.9 - 5.0 + 173.9)

    h.run(scenario())
    assert "get_cash" not in h.gateway.calls


def test_paper_order_id_recognized_by_cancel_path(h):
    """查询返回的 order_id 必须被 is_paper_order_id 识别（撤单 503 归因依赖它）。"""
    async def scenario():
        async with h.client() as c:
            placed = _ok(await c.post("/api/v1/trade/order", json={
                "code": "513090", "direction": "buy", "volume": 100, "price": 1.739}))
            orders = _ok(await c.get("/api/v1/trade/orders"))
            assert orders and is_paper_order_id(orders[0]["order_id"])
            assert is_paper_order_id(placed["order_id"])

    h.run(scenario())


def test_paper_engine_missing_is_503_not_fake_success(h_no_engine):
    """引擎未注入：明确 503，绝不返回空列表冒充「查过了、没有委托」。"""
    async def scenario():
        async with h_no_engine.client() as c:
            for path in ("/api/v1/trade/orders", "/api/v1/trade/deals",
                         "/api/v1/trade/positions", "/api/v1/account/status"):
                body = _data(await c.get(path))
                assert body["code"] == 503, f"{path} 应 503，实际 {body}"
                assert "模拟盘引擎未初始化" in body["message"], body

    h_no_engine.run(scenario())


# ============================ dry_run：模式归因 503 ============================

def test_dry_run_queries_are_503_with_mode_attribution(h):
    """预演模式不产生任何数据：503 + 说明为什么没有，而不是空列表。"""
    h.router.mode = "dry_run"

    async def scenario():
        async with h.client() as c:
            for path in ("/api/v1/trade/orders", "/api/v1/trade/deals",
                         "/api/v1/trade/positions", "/api/v1/account/status"):
                body = _data(await c.get(path))
                assert body["code"] == 503, f"{path} 应 503，实际 {body}"
                assert "预演" in body["message"], body

    h.run(scenario())


# ============================ live：券商路径原样不变 ============================

def test_live_mode_queries_still_hit_broker(h):
    """live 模式查询必须仍走券商（防修复过度伤到实盘路径）。"""
    h.router.mode = "live"

    async def scenario():
        async with h.client() as c:
            orders = _ok(await c.get("/api/v1/trade/orders"))
            deals = _ok(await c.get("/api/v1/trade/deals"))
            positions = _ok(await c.get("/api/v1/trade/positions"))
            st = _ok(await c.get("/api/v1/account/status"))
            # 券商返回原样透传（未下过单 ⇒ 委托/成交为空列表，不是 503）
            assert orders == [] and deals == []
            # 持仓走券商行 + 现价补算（quote 12.0）——live 路径未被 paper 分支改写
            assert len(positions) == 1 and positions[0]["code"] == "600519.SH"
            assert positions[0]["avail"] == 100 and positions[0]["cost"] == 1500.0
            assert positions[0]["price"] == 12.0
            assert st["connected"] is True and st["cash"] == 500_000.0

    h.run(scenario())
    assert "get_orders" in h.gateway.calls
    assert "get_deals" in h.gateway.calls
    assert "get_positions" in h.gateway.calls
    assert "get_cash" in h.gateway.calls


# ==================== profit_pct 口径回归（R2 P1-4） ====================

def test_paper_positions_profit_pct_is_percent_not_ratio():
    """``profit_pct`` 契约口径是**百分数**（5.0 表示 5%），不是分数。

    PaperEngine 内部用分数（``profit_ratio = upnl / cost``，0.05 = 5%），
    实盘路径 positions.py 用 ``(price-cost)/cost*100`` 产出百分数，
    前端 format.ts 的 fmtPct 直接当百分数渲染。paper 账本映射若直接透传
    分数，同一字段两种口径差 **100 倍** —— 模拟盘显示 +0.05%、实盘 +5.00%。
    """
    from app.services.paper_ledger import paper_positions
    from types import SimpleNamespace

    eng = SimpleNamespace(get_positions=lambda: [{
        "code": "513090", "volume": 100, "avg_cost": 10.00,
        "last_price": 10.50, "market_value": 1050.0,
        "unrealized_pnl": 50.0, "profit_ratio": 0.05,
    }])
    (p,) = paper_positions(eng)
    # 0.05 × 100 = 5.0（百分数），不是 0.05（分数）
    assert p["profit_pct"] == 5.0, p["profit_pct"]
    assert p["profit"] == 50.0

    # 成本缺失 ⇒ profit_ratio 为 None ⇒ 保持 None（"无盈亏可言"≠"零盈亏"）
    eng2 = SimpleNamespace(get_positions=lambda: [{
        "code": "513090", "volume": 100, "avg_cost": 0.0,
        "last_price": 10.50, "market_value": 1050.0,
        "unrealized_pnl": 0.0, "profit_ratio": None,
    }])
    assert paper_positions(eng2)[0]["profit_pct"] is None
