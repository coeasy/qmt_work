"""paper 模式查询端点分流 + 连接三态闸门 —— 2026-10-12 / 10-14 修复的守护测试。

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
5. 引擎未注入：503 明确失败，绝不静默假装成交（零 mock）；
6. **连接三态闸门**（2026-10-14 实测）：``broker_id`` 非空时按
   missing/offline/online 三态 × 执行模式分流 ——
   paper 遇到「已注册未连接」必须放行（撮合在本地，不需要券商），
   遇到「不存在」必须拒绝（失败绝不包 code=0）；
   paper 市价单无桥时走注入的 ``quote_fn`` 取价（与 /market/quotes 同源），
   取不到则挂起→确认→如实拒绝，绝不臆造价格。
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

    async def call_locked(self, fn, *args):
        """写操作串行入口（真实 XTQuantBridge 用 asyncio.Lock 包住）。
        测试里直接执行即可 —— 单线程 asyncio 本身就是串行的。
        """
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


class _PassRisk:
    """放行一切的最小风控。

    live 路径的 Mandatory Risk 要求 checker 非 None（``ExecutionService`` 里
    ``checker = risk or self._risk``，两者皆 None 即拒单），故 live 用例必须显式
    注入一个；paper 路径不经过它。
    """

    def check_order(self, *args, **kwargs):
        return True, ""


class _FakeCfg:
    """ConnectionConfig 的最小复刻（能力门禁要读 cfg.conn_id / cfg.name）。"""

    def __init__(self, conn_id: str, name: str = ""):
        self.conn_id = conn_id
        self.name = name or conn_id


class _FakeConn:
    def __init__(self, conn_id: str, connected: bool, adapter=None):
        self.cfg = _FakeCfg(conn_id)
        self.conn_id = conn_id
        self.connected = connected
        self.adapter = adapter


class TriStateManager:
    """带**连接三态**的 manager（真实 BrokerManager.connection_state 的最小复刻）。

    用于 2026-10-14 的连接闸门测试：必须能表达「已注册但未连接」这一态，
    SpyManager（恒返回桥）表达不了。
    """

    def __init__(self, conns: dict[str, bool], bridge: SpyBridge | None = None,
                 adapters: dict | None = None):
        self._conns = {cid: _FakeConn(cid, ok, (adapters or {}).get(cid))
                       for cid, ok in conns.items()}
        self._bridge = bridge

    def bridge(self, conn_id=None):
        if conn_id:
            c = self._conns.get(conn_id)
            return self._bridge if (c is not None and c.connected) else None
        return self._bridge

    def active_bridge(self):
        return self._bridge

    def all_connections(self):
        return list(self._conns.values())

    def connection_state(self, conn_id: str) -> str:
        c = self._conns.get(conn_id)
        if c is None:
            return "missing"
        return "online" if c.connected else "offline"


# ==================================== 装配 ====================================

class Harness:
    """真实路由 + 真实 SignalRouter + 真实 PaperEngine；只换最外层券商。"""

    def __init__(self, tmp_path: Path, *, paper_engine=True, manager=None,
                 quote_fn=None, risk=None, conns=None, adapters=None):
        self.tmp = tmp_path
        self.db = DB(tmp_path / "paper_mode.db")
        self.events: list[dict] = []
        self.gateway = SpyGateway()
        self.bridge = SpyBridge(self.gateway)
        if manager is not None:
            self.manager = manager
        elif conns is not None:
            # conns: {conn_id: connected}。用**同一个** SpyGateway 建三态 manager，
            # 这样 live 下单后照旧读 harness.gateway.orders 断言「真的报了券商」。
            # adapters: {conn_id: adapter} —— 能力门禁测试要给指定连接挂上
            # 「会报 can_submit=False」的适配器（真实大 QMT 桥的 connector_probe）。
            self.manager = TriStateManager(conns, self.bridge, adapters=adapters)
        else:
            self.manager = SpyManager(self.bridge)
        self.paper = (PaperEngine(initial_capital=1_000_000.0).init(self.db)
                      if paper_engine else None)
        self.router = SignalRouter(self.manager, risk=risk, db=self.db,
                                   wal=None, notifier=None,
                                   on_event=lambda e, p=None: self.events.append(
                                       e if isinstance(e, dict) else {"type": e, "data": p}),
                                   paper_engine=self.paper, quote_fn=quote_fn)
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
            # ★ 2026-10-14：现价/市值/盈亏必须来自**真实行情盯市**，不得拿
            #   成交价冒充。PaperEngine 的 last_prices 在无行情推送时停留在
            #   submit_order 写入的成交价（1.739）且自称 marking="live"，
            #   于是持仓页利润恒为 0 —— 把成交价伪装成实时行情。
            #   契约（docstring 承诺「拿不到行情则留空」）：
            #     有行情 → price=真实价、market_value=price×volume、marking="live"
            #     无行情 → price/market_value/profit/profit_pct 全 None、
            #              marking="cost"（诚实标注，不冒充）
            #   断言刻意不写具体行情值（测试环境不应依赖外部网络快照），
            #   只锁「自洽」与「不冒充」两条。
            mv = p["market_value"]
            if p["price"] is not None:
                assert p["marking"] == "live", p
                assert mv == pytest.approx(p["price"] * 100), p
                # 成本 1.739、成交价 1.739 ⇒ 盈亏只反映「成交后至今」的真实变动
                # （apply_pnl 统一 round 到 2 位，断言同精度）
                assert p["profit"] == pytest.approx(
                    round((p["price"] - 1.739) * 100, 2)), p
                assert p["profit_pct"] == pytest.approx(
                    round((p["price"] - 1.739) / 1.739 * 100, 2), abs=0.01), p
            else:
                assert mv is None and p["profit"] is None, p
                assert p["profit_pct"] is None, p
                assert p["marking"] == "cost", p

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


# ==================== 持仓键归一化回归（2026-10-14 下单流程实测） ====================

def test_paper_position_key_merges_bare_and_suffixed_code():
    """同一标的的「裸码」与「带后缀码」必须合并成**一条**持仓。

    实测复现（2026-10-14 本地下单流程，dev 库真实数据）：

        paper_positions: [('000001.SZ', 100.0, 11.85),
                          ('000001',    100.0, 50.0)]   ← 同一只平安银行裂成两条

    后果：
    ① 前端持仓页显示两只同名股票（其中一条 name 还是裸码）；
    ② 持仓集中度风控把一只股票算成两只 —— 单票占比分母虚高、分子被拆，
       该拦的超限单因此放行（假绿灯）；
    ③ T+1 批次记在两处，卖出时可卖量对不上。

    规则：数字段相同即同一标的，沿用已入库的键；两者都全新时用传入原样，
    **绝不猜测交易所**（``000001`` 既可能是上证指数也可能是平安银行）。
    """
    from engines.paper_engine import PaperEngine

    eng = PaperEngine.__new__(PaperEngine)   # 不起库，只测键归并逻辑
    eng.positions = {"000001.SZ": {"code": "000001.SZ", "name": "平安银行",
                                   "volume": 100, "avg_cost": 11.85,
                                   "side": "long"}}
    eng._lots = {}
    eng.last_prices = {}

    # 裸码下单 → 命中已入库的带后缀键
    assert eng._resolve_key("000001") == "000001.SZ"
    # 带后缀下单 → 同样命中（不新建键）
    assert eng._resolve_key("000001.SZ") == "000001.SZ"
    # 不同标的 → 原样返回（不误并）
    assert eng._resolve_key("000002") == "000002"
    assert eng._resolve_key("600519.SH") == "600519.SH"
    # 无数字段的输入 → 原样返回（不炸）
    assert eng._resolve_key("ABC") == "ABC"


def test_paper_submit_order_merges_position_key():
    """端到端：先带后缀买入、再以裸码加仓 ⇒ 仍只有一条持仓、成本被摊薄。"""
    from engines.paper_engine import PaperEngine

    eng = PaperEngine.__new__(PaperEngine)
    eng.positions = {}
    eng._lots = {}
    eng.last_prices = {}
    eng.cash = 1_000_000.0
    eng.commission_rate = 0.00025
    eng.min_commission = 5.0
    eng.stamp_tax_rate = 0.001
    eng.db = None
    eng._seq = 0
    eng._lock = __import__("threading").Lock()
    eng._check_limit_band = lambda code, price: "ok"
    eng._save_cash = lambda: None
    eng._save_position = lambda code: None

    eng.submit_order("000001.SZ", "buy", 10.0, 100, name="平安银行")
    assert list(eng.positions) == ["000001.SZ"], list(eng.positions)

    # 同一标的换写法（裸码）加仓：不得裂成第二条
    eng.submit_order("000001", "buy", 20.0, 100, name="平安银行")
    assert list(eng.positions) == ["000001.SZ"], list(eng.positions)
    pos = eng.positions["000001.SZ"]
    assert pos["volume"] == 200
    assert abs(pos["avg_cost"] - 15.0) < 1e-9, pos["avg_cost"]   # (10+20)/2 摊薄
    # T+1 批次记在同一键下
    assert sum(v for (_d, v) in eng._lots["000001.SZ"]) == 200


# ============ 连接三态闸门（2026-10-14 本地下单流程实测） ============

def test_paper_order_with_registered_offline_conn_id_is_allowed(tmp_path):
    """paper + **已注册但未连接**的 conn_id ⇒ 必须正常成交。

    实测复现（2026-10-14 本机，dev 库真实数据）—— 前端 OrderForm 的
    ``onSubmit`` 恒传 ``conn_id: activeConn``，于是用户只要在连接管理里选中过
    一条**连不上**的券商记录，此后每一笔模拟盘下单都返回：

        POST /trade/order {"conn_id": "6d0f83c154bc", ...}
        → 503「未连接任何券商客户端（连接不存在或未注册：6d0f83c154bc）」

    而不带 conn_id 的同一笔单 ⇒ PAPER-2 filled。根因是未知连接闸门对 live/paper
    统一用 ``bridge() is None`` 判死，而 ``bridge()`` 把「不存在」与「已注册未连接」
    **都返回 None**；paper 撮合全程在本地 PaperEngine，压根不经过该连接。

    后果：模拟盘这条链路从界面**完全不可达**（用户被引导去连券商，而模拟盘
    不需要券商）—— 这就是「下单流程没打通」的直接体感。
    """
    harness = Harness(tmp_path, conns={"offline-conn": False})
    try:
        async def scenario():
            async with harness.client() as c:
                res = _ok(await c.post("/api/v1/trade/order", json={
                    "code": "513090", "direction": "buy", "volume": 100,
                    "price": 1.739, "conn_id": "offline-conn"}))
                assert res["ok"] is True and res["mode"] == "paper", res
                assert res["order_id"].startswith("PAPER-"), res
                assert res["conn_id"] == "offline-conn", res
                # 成交必须落到账本（不是「放行了但没成交」）
                pos = _ok(await c.get("/api/v1/trade/positions"))
                assert len(pos) == 1 and pos[0]["volume"] == 100, pos

        harness.run(scenario())
    finally:
        harness.close()


def test_paper_order_with_missing_conn_id_is_rejected(tmp_path):
    """paper + **不存在**的 conn_id ⇒ 明确拒绝（「失败绝不包 code=0」不放宽）。"""
    harness = Harness(tmp_path, conns={})
    try:
        async def scenario():
            async with harness.client() as c:
                body = _data(await c.post("/api/v1/trade/order", json={
                    "code": "513090", "direction": "buy", "volume": 100,
                    "price": 1.739, "conn_id": "no-such-conn"}))
                assert body["code"] == 503, body
                assert "连接不存在" in body["message"], body
                # 拒绝不得落账
                assert _ok(await c.get("/api/v1/trade/positions")) == []

        harness.run(scenario())
    finally:
        harness.close()


def test_live_order_with_offline_conn_id_is_rejected(tmp_path):
    """live + 已注册未连接 ⇒ 拒绝，且文案说「未连接」而不是「不存在」。

    ``bridge()`` 对两态都返回 None，报错文案若仍写「不存在或未注册」就会把
    「你选了条没连上的连接」说成「这条连接没添加过」—— 用户照这句话去添加，
    添加完还是同一条记录，依旧连不上，陷入无效循环。
    """
    harness = Harness(tmp_path,
                      conns={"online-conn": True, "offline-conn": False},
                      risk=_PassRisk())
    harness.router.mode = "live"
    try:
        async def scenario():
            async with harness.client() as c:
                body = _data(await c.post("/api/v1/trade/order", json={
                    "code": "513090", "direction": "buy", "volume": 100,
                    "price": 1.739, "conn_id": "offline-conn"}))
                assert body["code"] == 503, body
                assert "连接未连接" in body["message"], body
                assert "不存在" not in body["message"], body
                # live + 已连接连接 ⇒ 闸门放行（后续走真实券商路径）
                res = _ok(await c.post("/api/v1/trade/order", json={
                    "code": "513090", "direction": "buy", "volume": 100,
                    "price": 1.739, "conn_id": "online-conn"}))
                assert res["ok"] is True, res
                assert harness.gateway.orders, "live 单必须真的报给券商"

        harness.run(scenario())
    finally:
        harness.close()


def test_paper_market_order_uses_quote_fn_without_bridge(tmp_path):
    """paper 市价单无券商桥 ⇒ 走注入的 quote_fn（与 /market/quotes 同源取价）。

    前端 OrderForm 的「最新价」来自 ``POST /market/quotes``，该端点缓存未命中时
    走 DataSourceManager，**不需要券商**。而 ``_est_price_async`` 此前只认券商桥
    ⇒ 同一标的「界面显示着真实行情价、后端却报取不到行情」：用户按界面价格下
    市价单，换来一句「未连接券商」—— 前后端取价口径分叉造成的断链。
    """
    seen: list[str] = []

    async def _quote(code: str) -> float:
        seen.append(code)
        return 1.75

    # 无任何连接、也无活跃桥（bridge=None）⇒ 只能靠 quote_fn 取价
    harness = Harness(tmp_path, manager=TriStateManager({}, None), quote_fn=_quote)
    try:
        async def scenario():
            async with harness.client() as c:
                res = _ok(await c.post("/api/v1/trade/order", json={
                    "code": "513090", "direction": "buy", "volume": 100,
                    "price": 0, "price_type": "market"}))
                assert res["ok"] is True, res
                # 以兜底价成交，而不是 0、也不是挂起等确认
                assert res["price"] == pytest.approx(1.75), res

        harness.run(scenario())
        assert seen == ["513090"], seen
    finally:
        harness.close()


def test_paper_market_order_without_quote_fn_still_rejects_honestly(tmp_path):
    """无兜底也无桥 ⇒ 市价单先挂起等人工确认，确认后如实拒绝。

    零 mock 铁律：拿不到价就**绝不臆造一个价去成交**。手动单按 P0-4 设计先
    挂起（``pending_price_unknown``），用户确认时仍取不到价 ⇒ 明确 400 + 归因，
    且不落任何委托。
    """
    harness = Harness(tmp_path, manager=TriStateManager({}, None))
    try:
        async def scenario():
            async with harness.client() as c:
                res = _ok(await c.post("/api/v1/trade/order", json={
                    "code": "513090", "direction": "buy", "volume": 100,
                    "price": 0, "price_type": "market"}))
                assert res["pending_confirmation"] is True, res
                assert res["pending_price_unknown"] is True, res
                token = res["confirm_token"]

                body = _data(await c.post("/api/v1/signal/confirm",
                                          json={"confirm_token": token}))
                assert body["code"] == 400, body
                assert "无法以未知价成交" in body["message"], body
                assert _ok(await c.get("/api/v1/trade/orders")) == []

        harness.run(scenario())
    finally:
        harness.close()


class _ProbeAdapter:
    """只实现 ``connector_probe`` 的适配器桩（能力门禁的唯一信息源）。"""

    def __init__(self, probe: dict):
        self._probe = probe

    def connector_probe(self):
        return self._probe


def test_live_order_capability_gap_is_actionable_and_not_misattributed(tmp_path):
    """★ 2026-10-15 真机取证：桥已连通但 agent 拿不到 passorder（独立进程形态）。

    本机真实状态：``agent_status.json`` 的 ``runtime_mode=standalone_process``、
    ``injected=[]``、``trade_surface.can_submit=false``，而桥 ``is_connected()``
    为真、界面一条绿灯。旧链路里这笔 live 单会穿过连接闸门，用户最终看到的是
    风控那句「账户快照未就绪：总资产仍为演示值 1000000」——既没解释为什么、
    也没指出下一步；路由层还会再前缀一句「未连接任何券商客户端：请到连接管理」
    （与事实相反：桥连着呢）。

    现在：下单**前**的能力门禁把真实原因和下一步说清楚，且订单一笔都不会
    报给券商（门禁先于执行）。三个归因断言分别锁「说清了 / 没谎报 / 没退化」。
    """
    probe = {"can_submit": False,
             "root_cause": "agent 已就绪但一个终端注入函数都没有（只转发了 76 个 "
                           "xtdata 行情接口）——当前以独立进程运行，下单 / 撤单 / "
                           "账户查询均不可用。需让本策略以「公式策略」形态被 QMT "
                           "in-process 挂载（同系统自带策略），"
                           "而不是以外部 Python 脚本策略运行"}
    harness = Harness(tmp_path, risk=_PassRisk(), conns={"bridge-conn": True},
                      adapters={"bridge-conn": _ProbeAdapter(probe)})
    harness.router.mode = "live"
    try:
        async def scenario():
            async with harness.client() as c:
                body = _data(await c.post("/api/v1/trade/order", json={
                    "code": "513090", "direction": "buy", "volume": 100,
                    "price": 1.739, "conn_id": "bridge-conn"}))
                assert body["code"] == 503, body
                msg = body["message"]
                # ① 说清了：能力缺口 + 真实原因 + 可执行下一步
                assert "不具备下单能力" in msg, msg
                assert "独立进程" in msg and "公式策略" in msg, msg
                # ② 没谎报：不得把「已连接」说成「未连接任何券商客户端」
                #    （路由层旧前缀，2026-10-15 移除）
                assert "未连接任何券商客户端" not in msg, msg
                # ③ 没退化：不得退化成风控那句既无原因也无下一步的文案
                assert "账户快照未就绪" not in msg, msg
                # ④ 门禁先于执行：一笔都没报给券商
                assert harness.gateway.orders == [], harness.gateway.orders

        harness.run(scenario())
    finally:
        harness.close()


def test_live_order_capability_unknown_does_not_block(tmp_path):
    """适配器不报能力（无 connector_probe）⇒ 门禁放行，不新增误伤闸门。

    零 mock 契约的另一面：能力门禁是「把**已知**缺口说清楚」，不是「猜一个
    缺口把正常通道挡住」。拿不到探针信息时一律放行，让真实下单去撞真实错误。
    """
    harness = Harness(tmp_path, risk=_PassRisk(), conns={"plain-conn": True})
    harness.router.mode = "live"
    try:
        async def scenario():
            async with harness.client() as c:
                res = _ok(await c.post("/api/v1/trade/order", json={
                    "code": "513090", "direction": "buy", "volume": 100,
                    "price": 1.739, "conn_id": "plain-conn"}))
                assert res["ok"] is True, res
                assert harness.gateway.orders, "无能力信息的连接不得被门禁误伤"

        harness.run(scenario())
    finally:
        harness.close()
