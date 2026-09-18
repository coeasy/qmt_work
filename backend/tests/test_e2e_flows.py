"""阶段 3 贯通验收：方案 §5 的 10 条核心流程 E2E（此前为 0，本轮补齐）。

与既有单测的区别（也是它才有验收价值的原因）：

- **走真实 HTTP 路由层**：请求经 FastAPI 路由 → 真实 SignalRouter / AlgoEngine /
  ConditionEngine / LimitUpMonitor / ExecutionService / WAL / Reconciler，
  只把最外层的**券商柜台**换成可控假桥（真实柜台需要 QMT 客户端与实盘账户，
  不可能在 CI 里驱动，且会让「验收」依赖外部状态）。
- **断言「链路」而非只断言返回值**：每条流程都验证
  「API 入口 → 后端链路 → 事件回推」三段，缺一段即判定该流程是断的。
- **流程清单是契约**：`tests/contracts/e2e_flows.json` 是唯一真源，
  本文件按它逐条跑，前端 `flowEntry.test.ts` 按它逐条断言前端入口存在。

运行（必须逐文件跑，见项目记忆）：
    cd backend && ./runtimes/cp311/python.exe -m pytest tests/test_e2e_flows.py -q
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
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
from core.state import state as app_state  # noqa: E402
from engines.algo import AlgoEngine  # noqa: E402
from engines.condition_order import ConditionOrderEngine  # noqa: E402
from engines.limitup import LimitUpMonitor  # noqa: E402
from gateway.alert_engine import AlertEngine  # noqa: E402
from gateway.reconcile import OrderReconciler  # noqa: E402
from gateway.signal_router import SignalRouter  # noqa: E402
from gateway.wal import WAL  # noqa: E402
from sync import SyncEngine  # noqa: E402

import gateway.idempotency as idem  # noqa: E402

MANIFEST = json.loads(
    (Path(__file__).parent / "contracts" / "e2e_flows.json").read_text(encoding="utf-8"))


# ============================ 假柜台（唯一被替换的外层） ============================

class FakeGateway:
    """最小可用的券商适配器。返回形态与真实 XTQuant 适配器一致。"""

    def __init__(self, last: float = 12.0):
        self.last = last
        self.orders: list[dict] = []
        self.cancelled: list[str] = []
        self.subscribed: list[str] = []

    def is_connected(self) -> bool:
        return True

    def get_quote(self, code: str):
        return {"code": code, "last": self.last, "volume": 1_000_000}

    def place_order(self, code, direction, price_type, price, volume, strategy="", remark=""):
        oid = f"OID-{len(self.orders) + 1}"
        self.orders.append({"order_id": oid, "code": code, "direction": direction,
                            "price_type": price_type, "price": price, "volume": volume,
                            "status": "filled", "dealt": volume})
        return {"order_id": oid, "ok": True}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"ok": True, "order_id": order_id}

    def get_orders(self):
        return list(self.orders)

    def get_deals(self):
        return [{"order_id": o["order_id"], "code": o["code"], "volume": o["volume"],
                 "price": o["price"], "side": o["direction"]} for o in self.orders]

    def get_positions(self, symbol=None):
        # 字段名严格对齐真实适配器契约（xtquant_client/xtp/account.py:55）：
        # code / name / volume / avail / cost / market_value —— 不含现价与浮动盈亏，
        # 现价/盈亏由 /trade/positions 按行情补算（见 test_positions_price_and_pnl_enrichment）。
        rows = [{"code": "600519.SH", "name": "贵州茅台", "volume": 100,
                 "avail": 100, "cost": 1500.0, "market_value": 150_000.0}]
        if symbol:
            return [r for r in rows if r["code"] == symbol]
        return rows

    def get_cash(self):
        return {"cash": 500_000.0, "assets": 650_000.0}

    def subscribe_quote(self, codes, cb):
        self.subscribed.extend(codes)
        return {"ok": True}


class FakeBridge:
    def __init__(self, gateway: FakeGateway):
        self.gateway = gateway

    async def call(self, fn, *args):
        return fn(*args)

    async def call_locked(self, fn, *args):
        return fn(*args)

    def enqueue(self, evt):  # SyncEngine 订阅回调
        return None


class FakeConnCfg:
    def __init__(self, conn_id: str, name: str = "", account_id: str = ""):
        self.conn_id = conn_id
        self.name = name or conn_id
        self.broker_id = ""
        self.client_path = ""
        self.client_mode = "auto"
        self.account_id = account_id
        self.account_type = "STOCK"
        self.session_id = 0
        self.active = False


class FakeConn:
    def __init__(self, conn_id: str):
        self.cfg = FakeConnCfg(conn_id)
        self.connected = True
        # 账户快照轮次需要 conn.bridge / conn.adapter（真实 BrokerManager 由连接建立时装配）
        self.bridge = None
        self.adapter = None


class FakeManager:
    def __init__(self, bridge: FakeBridge):
        self._bridge = bridge
        self._conns: dict[str, FakeConn] = {}

    def bridge(self, conn_id=None):
        return self._bridge

    def active_bridge(self):
        return self._bridge

    def all_connections(self):
        return list(self._conns.values())

    def add_connection(self, cfg, autoconnect=True):
        # ★ 镜像真实 BrokerManager._build：空 conn_id 就地生成 uuid
        # （manager.py:141 `if not cfg.conn_id: cfg.conn_id = uuid.uuid4().hex[:12]`）。
        # 替身若省掉这一步，两条连接都会落在 "" 这个键上
        # 互相覆盖 —— 那是替身自己造出来的假象，
        # 会让「重复提交不堆条目」这类断言变成假绿/假红。
        if not cfg.conn_id:
            cfg.conn_id = uuid.uuid4().hex[:12]
        c = FakeConn(cfg.conn_id or f"C{len(self._conns) + 1}")
        c.cfg = cfg          # 保留路由写入的完整 ConnectionConfig（含 account_id）
        c.connected = bool(autoconnect)
        c.bridge = self._bridge
        c.adapter = self._bridge.gateway
        self._conns[c.cfg.conn_id] = c
        return c

    def connect(self, conn_id):
        """返回形态与真实 BrokerManager 一致：dict（路由会 res.get("connected")）。"""
        c = self._conns.get(conn_id)
        if c is None:
            raise KeyError(conn_id)
        c.connected = True
        return {"conn_id": conn_id, "connected": True,
                "account_id": c.cfg.account_id, "name": c.cfg.name}

    def disconnect(self, conn_id):
        c = self._conns.get(conn_id)
        if c is None:
            raise KeyError(conn_id)
        c.connected = False
        return {"conn_id": conn_id, "connected": False}

    def status_list(self):
        return [{"conn_id": c.cfg.conn_id, "name": c.cfg.name,
                 "connected": c.connected, "active": True}
                for c in self._conns.values()]

    def find_by_identity(self, broker_id: str, client_path: str,
                         account_id: str):
        """按「券商 + 客户端路径 + 资金账号」定位已有连接（镜像真实判据）。

        归一化只保留一份实现（`xtquant_client/manager.py::_norm_path`）。
        替身若自己另写一套比较逻辑，测的就是替身而不是产品：
        「路径写法不同仍认成同一条」这条断言会因替身写错而假失败。
        """
        from xtquant_client.manager import _norm_path
        want = (broker_id or "", _norm_path(client_path),
                str(account_id or ""))
        for conn in self._conns.values():
            c = conn.cfg
            if (c.broker_id or "", _norm_path(c.client_path),
                    str(c.account_id or "")) == want:
                return conn
        return None

    def activate(self, conn_id: str):
        """镜像真实 `BrokerManager.activate`：重新点亮持久意图并拉起，**不新建**。

        路由的复用分支在 `autoconnect=True` 时会调它；替身缺它就是
        AttributeError —— 那会把「产品漏洞」与「替身缺口」混为一谈。
        """
        c = self._conns.get(conn_id)
        if c is None:
            raise KeyError(f"未知连接：{conn_id}")
        c.cfg.active = True
        c.connected = True
        return c


class FakeRisk:
    """可切换放行/拒绝的风控（确定性优先于真实性：本文件验的是链路不是风控规则）。"""

    def __init__(self):
        self.allow = True
        self.reason = "风控拒绝（E2E）"
        self.calls = 0

    def check_order(self, code, price, volume, side, price_type="limit",
                    require_account=False, **kwargs):
        self.calls += 1
        return (True, "") if self.allow else (False, self.reason)

    def precheck_order(self, code, price, volume, side, price_type="limit",
                       require_account=False, **kwargs):
        return (True, "") if self.allow else (False, self.reason)


class FakeNotifier:
    """通知通道假实现。真实链路是 `await notifier.notify(...)` —— 必须是协程。"""

    def __init__(self):
        self.sent: list[tuple] = []

    async def notify(self, *args, **kwargs):
        self.sent.append(args)
        return None


class FakeHub:
    """数据源 Hub 假实现（检索/K线/单只行情）——真实源需要网络与 TDX。"""

    def __init__(self, last: float = 12.0):
        self.last = last

    async def search_stocks(self, q, limit=20):
        return [{"code": "600519.SH", "name": "贵州茅台"}] if q else []

    async def search_boards(self, q, limit=8):
        return []

    async def get_quote(self, code, source="auto", conn_id=None):
        return {"code": code, "last": self.last, "source": "e2e-fake"}


# ==================================== 装配 ====================================

class Harness:
    """每条流程的运行环境：真实链路 + 假柜台。"""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.db = DB(tmp_path / "e2e.db")
        self.wal = WAL(tmp_path / "wal.jsonl")
        self.events: list[dict] = []
        self.router_calls: list[dict] = []

        self.gateway = FakeGateway()
        self.bridge = FakeBridge(self.gateway)
        self.manager = FakeManager(self.bridge)
        self.risk = FakeRisk()

        def _on_event(evt):
            self.events.append(evt)

        self.router = SignalRouter(self.manager, risk=self.risk, db=self.db,
                                   wal=self.wal, notifier=FakeNotifier(),
                                   on_event=_on_event)
        self.router.mode = "live"
        self.router.threshold = 100_000.0
        # 记录每次进路由的来源与幂等键：P0-3 的验收观测点。
        # 包 `route` 而非 `submit`：webhook 等入口直调 route，包 submit 会漏记。
        orig_route = self.router.route

        async def _recording_route(sig, auto_confirm=False, idempotency_key=""):
            self.router_calls.append({"source": sig.source, "key": idempotency_key,
                                      "code": sig.code, "volume": sig.volume})
            return await orig_route(sig, auto_confirm=auto_confirm,
                                    idempotency_key=idempotency_key)

        self.router.route = _recording_route  # type: ignore[method-assign]

        self.sync_engine = SyncEngine(self.manager, self.db, notifier=FakeNotifier())
        self.algo_engine = AlgoEngine(self.manager, risk=self.risk, on_event=_on_event,
                                      wal=self.wal, notifier=FakeNotifier())
        self.condition_engine = ConditionOrderEngine(self.manager, risk=self.risk, db=self.db,
                                                on_event=_on_event, wal=self.wal,
                                                notifier=FakeNotifier())
        self.limitup = LimitUpMonitor(self.manager, risk=self.risk, on_event=_on_event,
                                      wal=self.wal)
        self.reconciler = OrderReconciler(self.manager, wal=self.wal, db=self.db,
                                          on_event=_on_event)
        self.notifier = FakeNotifier()
        self.alert_engine = AlertEngine(self.db, self.notifier, on_event=_on_event)

        self.ctx = AppContext()
        self.ctx.update(
            db=self.db, broker_manager=self.manager, bridge=self.bridge,
            risk=self.risk, signal_router=self.router, wal=self.wal,
            sync_engine=self.sync_engine, algo_engine=self.algo_engine,
            condition_engine=self.condition_engine, limitup_monitor=self.limitup,
            reconciler=self.reconciler, notifier=self.notifier,
            alert_engine=self.alert_engine, lifecycle_ready=True,
            # 冷启动就绪信号：真实环境由 6 阶段 bootstrap 写入，此处显式补齐
            # （本文件验的是「就绪之后的贯通」，不是 bootstrap 本身）
            started_at=time.time(),
            phase_status={p: "ready" for p in ("db", "engines", "watchdogs",
                                               "replay", "misc")},
            ws_manager=object(), backtest_queue=object(),
        )
        self.app = FastAPI()
        self.app.add_middleware(EnvelopeMiddleware)
        self.app.include_router(api_router)

    # ---- 进程级状态注入（批量/执行服务从 core.state 取依赖）----
    def bind_state(self):
        self._saved = {k: getattr(app_state, k, None)
                       for k in ("db", "broker_manager", "bridge", "risk",
                                 "signal_router", "wal", "sync_engine")}
        app_state.db = self.db
        app_state.broker_manager = self.manager
        app_state.bridge = self.bridge
        app_state.risk = self.risk
        app_state.signal_router = self.router
        app_state.wal = self.wal
        app_state.sync_engine = self.sync_engine
        set_active_context(self.ctx)

    def unbind_state(self):
        for k, v in getattr(self, "_saved", {}).items():
            setattr(app_state, k, v)
        set_active_context(AppContext())

    # ---- 客户端 / 协程驱动 ----
    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app),
                           base_url="http://e2e.test",
                           headers={"X-API-Key": settings.api_key})

    def run(self, coro):
        return asyncio.run(coro)

    def event_types(self) -> list[str]:
        return [e.get("type", "") for e in self.events]


@pytest.fixture(autouse=True)
def _clean_idempotency():
    """幂等缓存是模块级全局：每条流程前清空，避免跨流程污染。"""
    with idem._lock:
        idem._cache.clear()
        idem._inflight.clear()
    # 持仓现价短 TTL 缓存同为模块级全局：不清会让上一条流程的行情喂给下一条
    # （实测表现为「注入 1688 却读到上条流程券商价 12」的伪失败）。
    import app.services.positions as pos_svc
    pos_svc.clear_price_cache()
    yield
    with idem._lock:
        idem._cache.clear()
        idem._inflight.clear()
    pos_svc.clear_price_cache()


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    # 数据源（检索/K线/单只行情）换成确定性假实现：真实源需要网络与 TDX，
    # 与本文件要验的「前后端贯通」无关，且会让结果随外部状态波动。
    import app.routes.market as market_routes
    import tools

    hub = FakeHub()

    monkeypatch.setattr(market_routes, "get_hub", lambda: hub, raising=False)

    async def _fake_kline(code, period, count, broker_id=None, force=False,
                          source="auto", adjust=None):
        return {"bars": [{"time": "2026-09-10", "open": 10.0, "high": 11.0,
                          "low": 9.5, "close": 10.5, "volume": 1000}],
                "source": "e2e-fake", "cached_at": None, "note": None}

    monkeypatch.setattr(tools, "fetch_kline_cached", _fake_kline, raising=False)
    harness.bind_state()
    try:
        yield harness
    finally:
        harness.unbind_state()
        try:
            harness.db.close()
        except Exception:  # noqa: BLE001
            pass


def _data(resp) -> dict:
    body = resp.json()
    assert isinstance(body, dict) and "code" in body, f"非信封响应：{body}"
    return body


def _ok(resp, expect: int = 0):
    body = _data(resp)
    assert body["code"] == expect, f"期望 code={expect}，实际 {body}"
    return body.get("data")


# ============================== 流程 1：冷启动→连接→行情 ==============================

def test_flow1_cold_start_connect_and_quotes(h):
    async def scenario():
        async with h.client() as c:
            live = _ok(await c.get("/api/v1/live"))
            ready = _ok(await c.get("/api/v1/ready"))
            assert live and ready is not None, "探活端点必须可用（/live 与 /ready）"

            profiles = _ok(await c.get("/api/v1/brokers/profiles"))
            assert isinstance(profiles, list) and profiles, "券商档案为空则无法引导连接"

            created = _ok(await c.post("/api/v1/brokers", json={
                "conn_id": "C1", "name": "E2E 测试账户", "broker_id": "",
                "client_path": "", "autoconnect": False}))
            assert created["conn_id"] == "C1"

            conn = _ok(await c.post("/api/v1/brokers/C1/connect"))
            assert conn, "连接后应返回连接状态"

            brokers = _ok(await c.get("/api/v1/brokers"))
            assert any(b.get("conn_id") == "C1" and b.get("connected")
                       for b in brokers), f"连接未出现在列表：{brokers}"

            # 实时行情：SyncEngine 收到行情事件 → /market/quotes 命中缓存（零新增网络调用）
            await h.sync_engine.on_event({"type": "quote", "data": {
                "code": "600519.SH", "last": 1688.0, "name": "贵州茅台"}})
            q = _ok(await c.post("/api/v1/market/quotes",
                                 json={"codes": ["600519.SH"]}))
            assert q["served"] == 1 and q["items"][0]["last"] == 1688.0, (
                "行情未贯通：SyncEngine 缓存 → /market/quotes 断链")
            assert h.sync_engine.latest_quotes["600519.SH"]["last"] == 1688.0

    h.run(scenario())


# ==================== 流程 1b：重复添加连接必须幂等（不得堆重复条目） ====================

def test_flow1b_add_broker_is_idempotent(h):
    """★ 连点同一个「检测到的本地客户端」不得堆出重复连接。

    注：**本用例不新增契约流程**。`tests/contracts/e2e_flows.json` 按方案 §5
    冻结为 10 条（`test_manifest_matches_real_endpoints` 把守）；本例用的
    `POST /api/v1/brokers` 已在 flow1 的 endpoints 里，它只是对该端点**幂等性**的补强。
    若需新增流程，应改方案 §5 并同步后端 MANIFEST / 前端 flowEntry.test.ts，
    而不是直接往本文件里加一个函数。

    实测反馈：「本地连接时点击同一个链接，会在下方出现多次相同客户端、相同交易账号
    id 的连接；也没办法直接连接，点击连接还报错。」
    —— 根因是 POST /brokers 每次都走 `_build()` 生成新 conn_id，从不查重。
    两条指向同一 QMT userdata 目录的连接会互相抢占，第二条必然连不上，
    于是「连点 → 多一条 → 点连接报错」。

    修法：`add_broker` 复用 `BrokerManager.find_by_identity`（与启动自动连接
    `phase_broker._auto_connect_active` 同一判据），命中即复用并返回 `reused=True`。

    判据必须选在**能区分修好没修好**的观测面上：断言连接列表条数与 conn_id 稳定，
    而不是「请求返回 200」—— 修复前请求同样返回 200，只是列表里多了一条。
    """
    async def scenario():
        async with h.client() as c:
            body = {"broker_id": "", "client_path": r"P:\qmt\userdata_mini",
                    "account_id": "1234567", "autoconnect": False}
            first = _ok(await c.post("/api/v1/brokers", json=body))
            assert first["reused"] is False, f"首次添加不应是复用：{first}"

            second = _ok(await c.post("/api/v1/brokers", json=body))
            assert second["reused"] is True, f"重复添加必须复用而非新建：{second}"
            assert second["conn_id"] == first["conn_id"]

            # Windows 路径大小写 / 斜杠 / 尾斜杠不敏感：写法不同仍是同一个客户端
            third = _ok(await c.post("/api/v1/brokers", json={
                **body, "client_path": "p:/QMT/userdata_mini/"}))
            assert third["reused"] is True, f"路径写法不同也必须认成同一条：{third}"
            assert third["conn_id"] == first["conn_id"]

            # 换了资金账号 = 另一条连接（去重不能过度，否则用户无法加第二个账号）
            other = _ok(await c.post("/api/v1/brokers", json={
                **body, "account_id": "7654321"}))
            assert other["reused"] is False
            assert other["conn_id"] != first["conn_id"]

            brokers = _ok(await c.get("/api/v1/brokers"))
            assert len(brokers) == 2, f"3 次同身份提交 + 1 次换账号 ⇒ 应恰好 2 条：{brokers}"

    h.run(scenario())


# ========================= 流程 2：检索→自选股→K 线→联动 =========================

def test_flow2_search_subscribe_kline(h):
    async def scenario():
        async with h.client() as c:
            rows = _ok(await c.get("/api/v1/market/search", params={"q": "茅台"}))
            assert rows and rows[0]["code"] == "600519.SH", "检索链路断"

            sub = _ok(await c.post("/api/v1/sync/subscribe",
                                   json={"codes": ["600519.SH", "000001.SZ"]}))
            assert sub["subscribed"] == ["000001.SZ", "600519.SH"], sub
            # 订阅聚合：重复订阅同一标的不应重复下发（旧前端「24 Tab → 24 订阅」）
            before = len(h.bridge.gateway.subscribed)
            _ok(await c.post("/api/v1/sync/subscribe", json={"codes": ["600519.SH"]}))
            assert len(h.bridge.gateway.subscribed) == before, "重复订阅未聚合"

            kline = _ok(await c.get("/api/v1/market/kline", params={
                "code": "600519.SH", "period": "1d", "count": 10}))
            assert kline["bars"], "K 线返回空（前端图表会空白）"
            assert kline["count"] == len(kline["bars"])

            quote = _ok(await c.get("/api/v1/market/quote",
                                    params={"code": "600519.SH"}))
            assert quote, "单只行情端点断"

    h.run(scenario())


# ===================== 流程 3：手动下单全流程（含二次确认） =====================

def test_flow3_manual_order_with_confirmation(h):
    async def scenario():
        async with h.client() as c:
            pre = _ok(await c.post("/api/v1/trade/precheck", json={
                "code": "600519.SH", "direction": "buy", "volume": 200, "price": 1000.0}))
            assert pre["allowed"] is True, pre

            # 大额（20 万 ≥ 阈值 10 万）→ 必须挂起等二次确认（P0-4）
            big = _ok(await c.post("/api/v1/trade/order", json={
                "code": "600519.SH", "direction": "buy", "volume": 200, "price": 1000.0}))
            assert big.get("pending_confirmation") is True, f"大额单未挂起：{big}"
            token = big["confirm_token"]
            assert "signal_pending" in h.event_types(), "挂起未回推事件（前端弹窗不会出）"
            assert h.bridge.gateway.orders == [], "挂起期间不得向柜台报单"

            confirmed = _ok(await c.post("/api/v1/signal/confirm",
                                         json={"confirm_token": token}))
            assert confirmed.get("ok") is True and confirmed.get("order_id"), confirmed
            assert confirmed.get("confirmed") is True
            assert len(h.bridge.gateway.orders) == 1, "确认后未真实下单"
            assert "signal_live" in h.event_types(), "成交未回推事件"
            assert h.wal.unresolved_intents() == [], "WAL 存在未完成 intent（写前日志未配对）"

            # 小额单：不挂起，直接下单
            small = _ok(await c.post("/api/v1/trade/order", json={
                "code": "000001.SZ", "direction": "buy", "volume": 100, "price": 10.0}))
            assert small.get("pending_confirmation") is not True, small
            assert len(h.bridge.gateway.orders) == 2

            orders = _ok(await c.get("/api/v1/trade/orders"))
            assert len(orders) == 2, "委托未进入查询链路（对账会查无此单）"

    h.run(scenario())


# ============================ 流程 4：算法交易 TWAP ============================

def test_flow4_algo_twap_slices_and_control(h):
    async def scenario():
        async with h.client() as c:
            job = _ok(await c.post("/api/v1/algo/submit", json={
                "code": "600519.SH", "direction": "buy", "volume": 100,
                "algo": "twap", "slices": 1, "duration": 10,
                "price_type": "limit", "limit_price": 10.0}))
            aid = job["algo_id"]

            # 等首片真实下发（后台任务在本 loop 内推进）
            placed = 0
            for _ in range(50):
                if h.bridge.gateway.orders:
                    placed = len(h.bridge.gateway.orders)
                    break
                await asyncio.sleep(0.05)
            assert placed == 1, "算法单未产生柜台委托（拆单链路断）"
            await h.algo_engine.stop()

            # P0-3：拆片必须带显式幂等键（含分片序号），否则 TWAP 各片会被吞成一单
            slice_calls = [x for x in h.router_calls if x["key"].startswith(f"algo:{aid}")]
            assert slice_calls, f"算法单未带显式幂等键：{h.router_calls}"
            assert slice_calls[0]["key"] == f"algo:{aid}:1", slice_calls[0]

            # 同片重复下发 → 幂等去重（不得重复成交）
            await h.algo_engine._place_slice(aid, 1, 100)
            assert len(h.bridge.gateway.orders) == 1, "同分片重复投递未去重"

            listed = _ok(await c.get("/api/v1/algo"))
            assert any(j["algo_id"] == aid for j in listed), "算法单未出现在列表"

            # 暂停 / 恢复 / 取消状态机
            job2 = _ok(await c.post("/api/v1/algo/submit", json={
                "code": "000001.SZ", "direction": "buy", "volume": 10000,
                "algo": "twap", "slices": 5, "duration": 600,
                "price_type": "limit", "limit_price": 10.0}))
            aid2 = job2["algo_id"]
            status = "pending"
            for _ in range(50):
                rows = _ok(await c.get("/api/v1/algo"))
                status = next(j["status"] for j in rows if j["algo_id"] == aid2)
                if status == "running":
                    break
                await asyncio.sleep(0.05)
            assert status == "running", f"算法单未进入 running：{status}"

            paused = _ok(await c.post(f"/api/v1/algo/{aid2}/pause"))
            assert paused["status"] == "paused", paused
            resumed = _ok(await c.post(f"/api/v1/algo/{aid2}/resume"))
            assert resumed["status"] == "running", resumed
            canceled = _ok(await c.post(f"/api/v1/algo/{aid2}/cancel"))
            assert canceled["status"] == "canceled", canceled
            await h.algo_engine.stop()

    h.run(scenario())


# ======================= 流程 5：条件单触发 / 涨停监控 =======================

def test_flow5_condition_trigger_and_limitup(h):
    async def scenario():
        async with h.client() as c:
            created = _ok(await c.post("/api/v1/trade/conditions", json={
                "code": "600519.SH", "side": "buy", "trigger_type": "gte",
                "trigger_price": 10.0, "volume": 100, "price_type": "limit",
                "price": 10.5}))
            cid = created["id"]
            conds = _ok(await c.get("/api/v1/trade/conditions"))
            assert any(o["id"] == cid and o["status"] == "pending" for o in conds["orders"])

            # 行情越过触发价 → 引擎下单（驱动 _loop 里同一个 _fire，避免等真实轮询周期）
            order = h.condition_engine._orders[cid]
            await h.condition_engine._fire(h.bridge, order, 12.0)
            assert len(h.bridge.gateway.orders) == 1, "条件单触发后未下单"
            assert any(x["source"] == "condition" for x in h.router_calls), (
                "条件单未走统一信号路由（风控/幂等/WAL 会被绕过）")
            assert "condition_triggered" in h.event_types(), "触发未回推事件"
            # 触发后状态经核销推进（triggered → submitted），触发时间必须留痕
            assert order["triggered_at"], "触发时间未留痕（崩溃后无法判断已触发）"
            assert order["status"] in ("triggered", "submitted"), order["status"]

            # 涨停监控：加池 → 启动 → 停止
            _ok(await c.post("/api/v1/limitup/pool", json={"code": "600519.SH"}))
            started = _ok(await c.post("/api/v1/limitup/start", json={
                "limit_pct": 0.1, "cutoff": "10:00", "min_rise": 0.03,
                "buy_volume": 0, "do_trade": False, "interval": 2.0}))
            assert started["running"] is True, started
            st = _ok(await c.get("/api/v1/limitup/status"))
            assert any(p["code"] == "600519.SH" for p in st["pool"]), st
            stopped = _ok(await c.post("/api/v1/limitup/stop"))
            assert stopped["running"] is False, stopped

    h.run(scenario())


# ========================= 流程 6：多账户批量下单 / 撤单 =========================

def test_flow6_batch_order_and_cancel(h):
    async def scenario():
        async with h.client() as c:
            _ok(await c.post("/api/v1/brokers", json={
                "conn_id": "C1", "name": "批量账户1", "autoconnect": True}))
            summary = _ok(await c.post("/api/v1/account/batch/order", json={
                "orders": [{"conn_id": "C1", "code": "600519.SH", "direction": "buy",
                            "volume": 100, "price": 10.0},
                           {"conn_id": "C1", "code": "000001.SZ", "direction": "buy",
                            "volume": 200, "price": 8.0}]}))
            assert summary["total"] == 2 and summary["ok"] == 2, summary
            assert len(h.bridge.gateway.orders) == 2, "批量子单未进入柜台"
            assert all(x["source"] == "batch" for x in h.router_calls), (
                "批量未走统一信号路由（会形成独立执行旁路）")
            # 批量幂等键必须带 batch_id + 序号
            assert all(x["key"].startswith("batch:") for x in h.router_calls), h.router_calls

            oid = h.bridge.gateway.orders[0]["order_id"]
            res = _ok(await c.post("/api/v1/account/batch/cancel", json={
                "items": [{"conn_id": "C1", "order_id": oid}]}))
            assert res["ok"] == 1 and res["results"][0]["status"] == "canceled", res
            assert oid in h.bridge.gateway.cancelled, "撤单未到柜台"

            acc = _ok(await c.get("/api/v1/account/status"))
            assert acc["connected"] is True and acc["assets"] > 0, acc

    h.run(scenario())


# ========================= 流程 7：风控拦截 → 实时提示 =========================

def test_flow7_risk_block_emits_event(h):
    async def scenario():
        h.risk.allow = False
        async with h.client() as c:
            pre = _ok(await c.post("/api/v1/trade/precheck", json={
                "code": "600519.SH", "direction": "buy", "volume": 100, "price": 10.0}))
            assert pre["allowed"] is False, "预检与风控判定不一致（前端会放行后被拒）"

            body = _data(await c.post("/api/v1/trade/order", json={
                "code": "600519.SH", "direction": "buy", "volume": 100, "price": 10.0}))
            assert body["code"] == 400, f"风控拦截必须是业务失败，不能是 code=0：{body}"
            assert "风控" in (body.get("message") or ""), body
            assert h.bridge.gateway.orders == [], "风控拦截后仍向柜台报单"
            blocked = [e for e in h.events if e.get("type") == "risk.blocked"]
            assert blocked and blocked[0]["data"]["reason"], "未回推 risk.blocked（前端无提示）"

    h.run(scenario())


# ====================== 流程 8：委托/成交/持仓 → 对账核销 ======================

def test_flow8_orders_deals_positions_reconcile(h):
    async def scenario():
        async with h.client() as c:
            _ok(await c.post("/api/v1/trade/order", json={
                "code": "600519.SH", "direction": "buy", "volume": 100, "price": 10.0}))
            orders = _ok(await c.get("/api/v1/trade/orders"))
            deals = _ok(await c.get("/api/v1/trade/deals"))
            positions = _ok(await c.get("/api/v1/trade/positions"))
            assert len(orders) == 1 and len(deals) == 1, "委托/成交链路断"
            assert positions and positions[0]["code"] == "600519.SH"

            stats = _ok(await c.get("/api/v1/wal/stats"))
            assert stats["records"] >= 2, f"WAL 未记录 intent+result：{stats}"

            rec = _ok(await c.post("/api/v1/reconcile", json={}))
            assert rec["checked"] >= 1, f"对账未覆盖已下委托：{rec}"
            assert rec["mismatched"] == 0, f"对账出现数量差异：{rec}"
            last = _ok(await c.get("/api/v1/reconcile/last"))
            assert last["checked"] == rec["checked"], "对账结果未留存（前端读 last 会空白）"
            assert "reconcile" in h.event_types(), "对账未回推事件"

    h.run(scenario())


# ===================== 流程 9：告警规则 → 通知 / 出站 Webhook =====================

def test_flow9_alerts_and_webhooks(h):
    async def scenario():
        async with h.client() as c:
            saved = _ok(await c.post("/api/v1/alerts/rules", json={
                "name": "E2E 告警", "event": "system.test", "channel": "*",
                "enabled": True, "cooldown_seconds": 0}))
            rid = saved["id"]
            rules = _ok(await c.get("/api/v1/alerts/rules"))
            assert any(r["id"] == rid for r in rules), "告警规则未落库（页面列表会空）"

            fired = _ok(await c.post("/api/v1/alerts/test",
                                     json={"event": "system.test", "payload": {"x": 1}}))
            assert fired["fired"] is True, fired
            assert any(e.get("type") == "alert" for e in h.events), "告警未回推事件"
            assert h.notifier.sent, "告警未进通知通道"
            history = _ok(await c.get("/api/v1/alerts/history"))
            assert isinstance(history, list), "告警历史端点断"

            from gateway.webhook_out import WebhookOut
            wh = WebhookOut(h.db)
            h.ctx.webhook_out = wh
            try:
                sid = _ok(await c.post("/api/v1/webhooks", json={
                    "name": "E2E 出站", "url": "http://127.0.0.1:9/hook",
                    "events": ["system.test"], "enabled": True}))
                assert sid, "出站 webhook 未保存"
                deliveries = _ok(await c.get("/api/v1/webhooks/deliveries"))
                assert isinstance(deliveries, list), "投递记录端点断"
            finally:
                await wh.close()

    h.run(scenario())


# =================== 流程 10：外部信号入站 → 路由 → 执行 ===================

def test_flow10_inbound_webhook_signal(h, monkeypatch):
    async def scenario():
        async with h.client() as c:
            mode = _ok(await c.get("/api/v1/signal/mode"))
            assert mode["mode"] == "live", mode

            body = {"code": "600519.SH", "side": "buy", "volume": 100, "price": 10.0,
                    "source": "webhook"}
            raw = json.dumps(body).encode()

            # P0-7：未配置密钥 → 默认拒绝（否则任何人都能实盘下单）
            monkeypatch.setattr(settings, "webhook_secret", "", raising=False)
            monkeypatch.setattr(settings, "webhook_allow_insecure", False, raising=False)
            denied = _data(await c.post("/api/v1/signal/webhook", content=raw))
            assert denied["code"] == 401, f"未配置密钥却放行了入站信号：{denied}"

            # 配置密钥后：错误签名拒绝、正确签名放行并经统一路由下单
            monkeypatch.setattr(settings, "webhook_secret", "s3cr3t", raising=False)
            bad = _data(await c.post("/api/v1/signal/webhook", content=raw,
                                     headers={"x-signature": "deadbeef"}))
            assert bad["code"] == 401, "错误签名被放行"

            good = hmac.new(b"s3cr3t", raw, hashlib.sha256).hexdigest()
            accepted = _ok(await c.post("/api/v1/signal/webhook", content=raw,
                                        headers={"x-signature": good}))
            assert accepted.get("ok") is True, accepted
            assert len(h.bridge.gateway.orders) == 1, "入站信号未进入执行链路"
            assert h.router_calls and h.router_calls[-1]["source"] == "webhook", h.router_calls
            assert "signal_live" in h.event_types(), "入站信号未回推事件"

            orders = _ok(await c.get("/api/v1/trade/orders"))
            assert len(orders) == 1

    h.run(scenario())


# ============ 持仓行情补算（现价 / 盈亏 / 盈亏比）：/trade/positions ============

def test_sync_engine_subscribes_held_positions(h):
    """持仓盯市：SyncEngine 把持仓代码并入行情订阅（现价/盈亏零打源可读）。

    回归背景：持仓页此前靠「请求时逐只打行情源」取现价，而 broker quote 的
    合约详情富化会回落到不可达的公共源（实测单次 5.6s），必然超时 → 三列恒空。
    改为持仓代码随账户快照轮次自动订阅，现价直接命中实时缓存且随 tick 刷新。
    """
    eng = h.sync_engine
    pos = h.gateway.get_positions()
    new = eng.subscribe_positions(pos)
    assert new == ["600519.SH"], new
    assert "600519.SH" in eng._subscribed_codes, "未登记已订阅集合 → 下轮会重复下发"
    assert "600519.SH" in h.gateway.subscribed, "未真正下发到券商"

    # 幂等：每轮快照无脑调用，不得重复下发
    assert eng.subscribe_positions(pos) == [], "重复订阅未去重"
    # 脏数据容错：None / 非 dict / 无 code 一律跳过，不得抛错
    assert eng.subscribe_positions([None, "x", {}, {"code": ""}]) == []


def test_account_snapshot_loop_subscribes_positions(h):
    """接线验收：账户快照轮次必须真的调用持仓订阅（否则功能存在但永不生效）。

    只测 `subscribe_positions` 本身不足以防回归——它被从快照循环里摘掉时，
    单测依旧全绿，而持仓页的现价/盈亏会静默回到恒空状态。
    """
    async def scenario():
        cfg = FakeConnCfg("C9", name="快照账户", account_id="A9")
        cfg.active = True
        h.manager.add_connection(cfg)

        eng = h.sync_engine
        await eng.start_account_snapshots(interval=5.0)
        try:
            for _ in range(60):
                if "600519.SH" in eng._subscribed_codes:
                    break
                await asyncio.sleep(0.05)
            assert "600519.SH" in eng._subscribed_codes, (
                "快照轮次未订阅持仓 → 持仓页现价/盈亏恒空")
            assert "600519.SH" in h.gateway.subscribed, "未真正下发到券商"
        finally:
            eng._account_task.cancel()
            try:
                await eng._account_task
            except asyncio.CancelledError:
                pass

    h.run(scenario())


def test_positions_price_and_pnl_enrichment(h):
    """券商只回 成本/数量，接口须按真实行情补出 现价/盈亏/盈亏比（拿不到行情则留空）。

    回归背景：前端持仓表「现价 / 盈亏 / 盈亏比」三列此前恒为「--」——因为
    /trade/positions 直接透传券商字段，而券商 get_positions 契约
    （xtquant_client/xtp/account.py:55）本就不含这三个字段。
    """
    async def scenario():
        async with h.client() as c:
            # 行情经 SyncEngine 注入 → 命中订阅缓存（零网络调用），补算路径确定性可验
            await h.sync_engine.on_event({"type": "quote", "data": {
                "code": "600519.SH", "last": 1688.0, "name": "贵州茅台"}})
            positions = _ok(await c.get("/api/v1/trade/positions"))
            row = next(p for p in positions if p["code"] == "600519.SH")
            assert row["price"] == 1688.0, f"现价未补算：{row}"
            assert row["profit"] == round((1688.0 - 1500.0) * 100, 2), f"盈亏错误：{row}"
            assert row["profit_pct"] == round((1688.0 - 1500.0) / 1500.0 * 100, 2), \
                f"盈亏比错误：{row}"

    h.run(scenario())


def test_account_status_positions_carry_pnl(h):
    """/account/status 的 positions 必须与 /trade/positions 同口径带上盈亏。

    仪表盘「持仓盈亏」是对 `/account/status` 的 positions 求和 `p.profit`
    （见 frontend-next/src/domains/Dashboard.tsx）。两页读的是同一份券商持仓，
    补算若只做在其中一个端点，同一时刻两页就会各说各话（实测：持仓页 -10.4、
    仪表盘 0）——故两者必须共用 app.services.positions 的同一实现。
    """
    async def scenario():
        async with h.client() as c:
            await h.sync_engine.on_event({"type": "quote", "data": {
                "code": "600519.SH", "last": 1688.0, "name": "贵州茅台"}})
            acct = _ok(await c.get("/api/v1/account/status"))
            row = next(p for p in acct["positions"] if p["code"] == "600519.SH")
            assert row["price"] == 1688.0, f"现价未补算：{row}"
            assert row["profit"] == round((1688.0 - 1500.0) * 100, 2), row
            # 仪表盘口径：对 positions 求和
            total = sum(p.get("profit") or 0 for p in acct["positions"])
            assert total == row["profit"], f"仪表盘聚合口径对不上：{total} != {row['profit']}"

    h.run(scenario())


def test_positions_price_from_broker_snapshot_when_hub_broken(h, monkeypatch):
    """hub 不可用时，现价仍须由**券商直连快照**补出（持仓页能否显示现价的关键）。

    hub 的 broker 路径会做「合约详情富化」，详情为空壳时回落到公共源
    （实测 sina 403 经代理耗时约 5.6s），持仓页等不起。故把 hub 直接打坏：
    只剩券商直连这一条路，仍必须出价——否则线上表现为「三列恒空」。
    """
    import app.services.positions as pos_svc

    def _boom():
        raise RuntimeError("hub 不可用（测试注入）")

    monkeypatch.setattr(pos_svc, "get_manager", _boom, raising=False)

    async def scenario():
        async with h.client() as c:
            positions = _ok(await c.get("/api/v1/trade/positions"))
            row = next(p for p in positions if p["code"] == "600519.SH")
            assert row["price"] == h.gateway.last, f"券商直连快照未补价：{row}"
            assert row["profit"] == round((h.gateway.last - 1500.0) * 100, 2), row
            assert row["profit_pct"] == round(
                (h.gateway.last - 1500.0) / 1500.0 * 100, 2), row

    h.run(scenario())


def test_positions_quote_failure_is_negatively_cached(monkeypatch):
    """打源失败必须进负缓存：否则持仓页每次轮询都白等一个超时（实测 2.5s）。"""
    import app.services.positions as pos_svc

    calls = {"n": 0}

    class _DeadHub:
        async def get_quote(self, code, source="auto"):
            calls["n"] += 1
            return None

    monkeypatch.setattr(pos_svc, "get_manager", lambda: _DeadHub(), raising=False)
    rows = [{"code": "600519.SH", "volume": 100, "cost": 1500.0}]

    async def scenario():
        ctx = AppContext()      # sync_engine 为 None → 缓存冷，必然走打源
        await pos_svc._fill_position_prices(rows, ctx, bridge=None)
        assert calls["n"] == 1, f"首次未打源：{calls}"
        await pos_svc._fill_position_prices(rows, ctx, bridge=None)
        assert calls["n"] == 1, f"负缓存未生效，第二次仍在打源：{calls}"
        assert "price" not in rows[0], "拿不到行情却写入了现价（伪造）"

    asyncio.run(scenario())


def test_positions_pnl_cost_field_fallback():
    """成本字段多版本兼容：cost 缺失时回落 avg_cost/cost_price，并归一化到 cost。

    归一化是必需的——前端持仓「成本」列只读 cost；若适配器回 avg_cost 而不同步，
    成本列与盈亏列会一起空掉。
    """
    from app.services.positions import apply_pnl as _apply_pnl

    rows = [
        {"code": "A.SH", "volume": 100, "avg_cost": 10.0, "price": 12.0},
        {"code": "B.SH", "volume": 100, "cost_price": 20.0, "price": 18.0},
        {"code": "C.SH", "volume": 100, "price": 5.0},          # 无成本 → 不伪造盈亏
        {"code": "D.SH", "volume": 100, "cost": 0.0, "price": 5.0},  # 成本 0 → 不伪造
    ]
    _apply_pnl(rows)
    assert rows[0]["cost"] == 10.0 and rows[0]["profit"] == 200.0
    assert rows[0]["profit_pct"] == 20.0
    assert rows[1]["cost"] == 20.0 and rows[1]["profit"] == -200.0
    assert rows[1]["profit_pct"] == -10.0
    assert rows[2].get("profit") is None and rows[2].get("profit_pct") is None
    assert rows[3].get("profit") is None and rows[3].get("profit_pct") is None
    # 市值兜底：券商未给时按 现价×数量 补
    assert rows[2]["market_value"] == 500.0


# ==================== 元验收：流程清单本身必须是完整的契约 ====================

def test_manifest_matches_real_endpoints():
    """清单里的端点必须真实存在（防止流程文档与实现各说各话）。"""
    endpoints = json.loads(
        (Path(__file__).parent / "contracts" / "rest_endpoints.json").read_text(encoding="utf-8"))
    missing = [ep for f in MANIFEST["flows"] for ep in f["endpoints"] if ep not in endpoints]
    assert missing == [], f"流程清单引用了不存在的端点：{missing}"
    assert len(MANIFEST["flows"]) == 10, "方案 §5 是 10 条流程，不得增删"
