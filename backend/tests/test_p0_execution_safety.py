"""P0 交易正确性回归（方案 §7 阶段 1 续）：WAL 写前日志 / 风控单点 / 引擎幂等 /
webhook 默认安全 / watchdog 撤单留痕 / 停机关库。

每条断言都对应一个「改坏了会真金白银亏钱」的缺陷，并遵循项目惯例：
先确认新代码下通过，再回退修复确认它**确实会失败**（因果闭环）。

| # | 缺陷 | 后果 |
|---|------|------|
| P0-1 | WAL 写后日志 | 下单成功但崩溃 → 委托永久失联，对账查不到 |
| P0-2 | 风控执行两次 | 频率/日额度翻倍消耗，正常单被提前误拦 |
| P0-3 | 引擎无幂等 | 重启重复触发即重复下单 |
| P0-7 | webhook 空密钥 | 未配置 secret 即可实盘下单 |
| P0-8 | watchdog 撤单无痕 | 崩溃后无法重放撤单 |
| P0-10 | 停机不关库 | 残留句柄 / WAL 未 checkpoint |
"""
import asyncio

import gateway.idempotency as idem
import pytest

from gateway.execution import ExecutionService
from gateway.order_watchdog import OrderWatchdog
from gateway.signal_router import SignalRouter
from gateway.wal import WAL

# ---- 幂等缓存是模块级全局：每个用例前清空，避免跨用例污染 -------------------


@pytest.fixture(autouse=True)
def _clean_idempotency():
    with idem._lock:
        idem._cache.clear()
        idem._inflight.clear()
    yield
    with idem._lock:
        idem._cache.clear()
        idem._inflight.clear()


# ---- 桩件 ------------------------------------------------------------------


class _Bridge:
    """最小可用 bridge：place_order 时**同步**抓取 WAL 快照（验证写前日志）。"""

    def __init__(self, wal=None):
        self.gateway = self
        self.wal = wal
        self.orders: list = []
        self.wal_at_place: list | None = None

    def get_quote(self, code):
        return {"last": 10.0}

    def place_order(self, *args, **kwargs):
        if self.wal is not None:
            self.wal_at_place = list(self.wal.all_records())
        self.orders.append((args, kwargs))
        return {"order_id": f"OID-{len(self.orders)}", "ok": True}

    async def call(self, fn, *args):
        return fn(*args)

    async def call_locked(self, fn, *args):
        return fn(*args)


class _Mgr:
    def __init__(self, bridge):
        self._bridge = bridge

    def bridge(self, conn_id=None):
        return self._bridge


class _CountingRisk:
    """放行一切，但**统计被校验次数**（P0-2 的核心观测点）。"""

    def __init__(self):
        self.calls = 0

    def check_order(self, code, price, volume, side, price_type="limit",
                    require_account=False, **kwargs):
        self.calls += 1
        return True, ""


class _DenyRisk:
    def check_order(self, code, price, volume, side, price_type="limit",
                    require_account=False, **kwargs):
        return False, "风控拒绝（测试）"


def _router(wal=None, risk=None, **kw):
    bridge = _Bridge(wal=wal)
    r = SignalRouter(_Mgr(bridge), risk=risk or _CountingRisk(), wal=wal, **kw)
    r.mode = "live"
    r.threshold = 1e9          # 本案不测二次确认，避免挂起干扰
    return r, bridge


# =========================== P0-1：WAL 写前日志 ============================


def test_wal_intent_is_written_before_order_is_placed(tmp_path):
    """核心断言：调用柜台 place_order 的那一刻，intent 必须已在 WAL 中。

    这是「写前」与「写后」的唯一区别 —— 下单后进程崩溃时，只有写前日志能
    让对账发现这笔委托的存在。
    """
    wal = WAL(tmp_path / "wal.jsonl")
    r, bridge = _router(wal=wal)

    out = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert out.get("ok") is True, out
    assert bridge.wal_at_place is not None, "place_order 未被调用"
    kinds = [rec.get("op") for rec in bridge.wal_at_place]
    assert "intent" in kinds, (
        "下单时 WAL 里还没有 intent —— 说明仍是「写后日志」，崩溃窗口内委托会失联")


def test_wal_intent_is_paired_with_result_on_success(tmp_path):
    """下单成功后，结果记录携带 intent_id 与 intent 配对 → 不算未完成。"""
    wal = WAL(tmp_path / "wal.jsonl")
    r, _bridge = _router(wal=wal)

    out = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert out.get("intent_id"), "结果应回带 intent_id 便于前端/审计追踪"
    assert wal.unresolved_intents() == [], "已成功下单，不应留下未完成 intent"


def test_crash_window_leaves_unresolved_intent(tmp_path):
    """模拟崩溃：柜台下单时进程被 kill —— 对账必须能发现这笔未完成 intent。"""
    wal = WAL(tmp_path / "wal.jsonl")

    class _CrashingBridge(_Bridge):
        def place_order(self, *args, **kwargs):
            if self.wal is not None:
                self.wal_at_place = list(self.wal.all_records())
            raise SystemExit("simulated kill -9 after intent, before result")

    bridge = _CrashingBridge(wal=wal)
    r = SignalRouter(_Mgr(bridge), risk=_CountingRisk(), wal=wal)
    r.mode = "live"
    r.threshold = 1e9

    with pytest.raises(SystemExit):
        asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    pending = wal.unresolved_intents()
    assert len(pending) == 1, "崩溃窗口内的委托必须可被对账发现"
    assert pending[0]["payload"]["code"] == "600519.SH"
    assert pending[0]["payload"]["volume"] == 100


def test_rejected_order_does_not_leave_unresolved_intent(tmp_path):
    """被柜台/风控拒单是**确定结果**，不得被误报成「悬而未决」。"""
    wal = WAL(tmp_path / "wal.jsonl")

    class _RejectBridge(_Bridge):
        def place_order(self, *args, **kwargs):
            if self.wal is not None:
                self.wal_at_place = list(self.wal.all_records())
            return {"order_id": "", "status": "rejected", "ok": False}

    bridge = _RejectBridge(wal=wal)
    r = SignalRouter(_Mgr(bridge), risk=_CountingRisk(), wal=wal)
    r.mode = "live"
    r.threshold = 1e9

    out = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert out.get("ok") is False
    assert wal.unresolved_intents() == [], "拒单已终结 intent，不应残留"


# =========================== P0-2：风控单点执行 ============================


def test_risk_is_checked_exactly_once_on_router_path():
    """核心断言：经 SignalRouter 下单，风控只执行 1 次（此前 route + execution 各一次）。"""
    risk = _CountingRisk()
    r, bridge = _router(risk=risk)

    asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert len(bridge.orders) == 1
    assert risk.calls == 1, (
        f"风控被执行了 {risk.calls} 次 —— 频率窗口与日额度会被翻倍消耗，"
        "正常单提前被误拦")


def test_execution_service_still_enforces_risk_when_called_directly():
    """直连 ExecutionService（MCP/批量等路径）必须仍然强制风控，不得被削弱。"""
    bridge = _Bridge()
    svc = ExecutionService(risk=_DenyRisk(), db=None)

    out = asyncio.run(svc.place_order(bridge, "600519.SH", "buy", 100, 10.0, "limit"))

    assert out["ok"] is False
    assert bridge.orders == [], "风控拒绝后不得向柜台报单"


def test_risk_rejection_blocks_order_on_router_path():
    """风控拒绝时，SignalRouter 不得把委托送到柜台。"""
    r, bridge = _router(risk=_DenyRisk())

    out = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert out.get("ok") is False
    assert bridge.orders == []


# =========================== P0-3：引擎路径强制幂等 =========================


def test_engine_repeated_signal_places_only_one_order():
    """同一引擎信号在时间窗内重复投递 → 只产生 1 笔委托。"""
    r, bridge = _router()
    r._auto_idem_window = 30.0

    outs = [asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit",
                                 source="condition")) for _ in range(3)]

    assert len(bridge.orders) == 1, "引擎重复投递产生了重复委托"
    assert outs[1].get("duplicated") is True
    assert outs[2].get("duplicated") is True


def test_manual_orders_are_not_deduplicated():
    """人工单不自动生成幂等键：连下两笔相同委托是用户真实意图，不应被吞。"""
    r, bridge = _router()
    r._auto_idem_window = 30.0

    for _ in range(2):
        asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert len(bridge.orders) == 2, "人工重复下单被误去重"


def test_explicit_key_with_slice_index_keeps_every_slice():
    """算法单显式键含分片序号：TWAP 各片参数相同也必须各自成单（不被吞）。"""
    r, bridge = _router()
    r._auto_idem_window = 30.0

    for idx in range(4):
        asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit",
                             source="algo_twap", auto_confirm=True,
                             idempotency_key=f"algo:A1:{idx}"))

    assert len(bridge.orders) == 4, "拆单被幂等误吞 —— TWAP/VWAP 会严重欠量"
    # 同片重复提交仍须去重
    asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit",
                         source="algo_twap", auto_confirm=True,
                         idempotency_key="algo:A1:0"))
    assert len(bridge.orders) == 4, "同一分片重复提交未去重"


def test_unknown_source_is_never_auto_deduplicated():
    """白名单外的来源不去重（宁可漏去重，也绝不误吞正常下单）。"""
    r, bridge = _router()
    r._auto_idem_window = 30.0

    for _ in range(2):
        asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="some_new_engine"))

    assert len(bridge.orders) == 2


# =========================== P0-7：webhook 默认安全 =========================


def _webhook_call(body: dict, headers: list | None = None, monkeypatch=None):
    """直接调用 webhook 路由（不经 ASGI 服务器），返回业务信封。"""
    import json

    from fastapi import Request

    from core.config import settings
    from core.context import AppContext, set_active_context

    raw = json.dumps(body).encode()
    scope = {
        "type": "http", "method": "POST", "path": "/api/v1/signal/webhook",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    class _Router:
        def __init__(self):
            self.mode = "live"

        async def route(self, sig, idempotency_key="", auto_confirm=False):
            return {"ok": True, "order_id": "OID-W1", "code": sig.code}

    ctx = AppContext()
    ctx.signal_router = _Router()

    class _DB:
        def audit(self, *a, **k):
            return None

    ctx.db = _DB()
    set_active_context(ctx)
    try:
        from app.routes.signal import signal_webhook
        return asyncio.run(signal_webhook(Request(scope, receive), ctx=ctx))
    finally:
        set_active_context(AppContext())
        _ = settings


def test_webhook_rejects_when_secret_not_configured(monkeypatch):
    """核心断言：未配置 webhook_secret 时默认拒绝，而不是放行实盘下单。"""
    from core.config import settings

    monkeypatch.setattr(settings, "webhook_secret", "", raising=False)
    monkeypatch.setattr(settings, "webhook_allow_insecure", False, raising=False)

    env = _webhook_call({"code": "600519.SH", "side": "buy", "volume": 100, "price": 10.0})

    assert env["code"] == 401, "未配置密钥却放行了未签名请求 —— 任何人都能实盘下单"


def test_webhook_allows_when_insecure_explicitly_enabled(monkeypatch):
    """显式 opt-in 才回到不安全形态（隔离联调环境需要这条逃生通道）。"""
    from core.config import settings

    monkeypatch.setattr(settings, "webhook_secret", "", raising=False)
    monkeypatch.setattr(settings, "webhook_allow_insecure", True, raising=False)

    env = _webhook_call({"code": "600519.SH", "side": "buy", "volume": 100, "price": 10.0})

    assert env["code"] == 0


def test_webhook_rejects_bad_signature_when_secret_configured(monkeypatch):
    """配置了密钥但签名不对 → 401（既有行为不回退）。"""
    import hashlib
    import hmac
    import json

    from core.config import settings

    monkeypatch.setattr(settings, "webhook_secret", "s3cr3t", raising=False)
    body = {"code": "600519.SH", "side": "buy", "volume": 100, "price": 10.0}
    raw = json.dumps(body).encode()
    env = _webhook_call(body, headers=[("x-signature", "deadbeef"),
                                       ("content-length", str(len(raw)))])

    assert env["code"] == 401
    # 正确签名可通过（证明校验逻辑本身没被写死成「一律拒绝」）
    good = hmac.new(b"s3cr3t", raw, hashlib.sha256).hexdigest()
    env2 = _webhook_call(body, headers=[("x-signature", good),
                                        ("content-length", str(len(raw)))])
    assert env2["code"] == 0


# =========================== P0-8：watchdog 撤单留痕 =========================


class _StubConn:
    def __init__(self, conn_id="C1"):
        self.cfg = type("Cfg", (), {"conn_id": conn_id})()
        self.connected = True
        self.bridge = _Bridge()
        self.adapter = self.bridge
        self.cancelled: list[str] = []


class _AuditDB:
    def __init__(self):
        self.rows: list[tuple] = []

    def audit(self, actor, action, target, params, result):
        self.rows.append((actor, action, target, params, result))


def test_watchdog_cancel_is_recorded_in_wal_and_audit(tmp_path):
    """核心断言：自动撤单前写 WAL intent、撤单后写结果，并进审计日志。"""
    wal = WAL(tmp_path / "wal.jsonl")
    db = _AuditDB()

    class _Conn(_StubConn):
        pass

    conn = _Conn()

    # 注意：真实链路是 `await bridge.call(adapter.cancel_order, oid)`，
    # call 内部**同步**执行该函数 —— 桩必须是同步函数，否则永远不会被 await。
    def _fake_cancel(order_id):
        conn.cancelled.append(order_id)
        return {"ok": True}

    conn.adapter.cancel_order = _fake_cancel

    wd = OrderWatchdog(_Mgr(conn.bridge), wal=wal, db=db, notifier=None)
    asyncio.run(wd._handle_stale(conn, {"order_id": "OID-9", "code": "600519.SH"}))

    assert conn.cancelled == ["OID-9"]
    ops = [r.get("op") for r in wal.all_records()]
    assert "cancel_intent" in ops, "撤单前未写 WAL —— 崩溃后这笔撤单永久失联"
    assert "cancel" in ops
    # intent 与 result 配对后不应残留「未完成」
    assert wal.unresolved_intents() == []
    assert any(r[1] == "order.cancel" for r in db.rows), "撤单未进审计日志"


def test_watchdog_records_failure_result(tmp_path):
    """撤单失败也要留痕（result=cancel_failed），否则运维无法感知撤单没成功。"""
    wal = WAL(tmp_path / "wal.jsonl")
    db = _AuditDB()
    conn = _StubConn()

    def _boom(order_id):
        raise RuntimeError("bridge down")

    conn.adapter.cancel_order = _boom

    wd = OrderWatchdog(_Mgr(conn.bridge), wal=wal, db=db)
    asyncio.run(wd._handle_stale(conn, {"order_id": "OID-8", "code": "000001.SZ"}))

    results = [r["payload"].get("result") for r in wal.all_records() if r.get("op") == "cancel"]
    assert results and results[0].startswith("cancel_failed"), "撤单失败未写 WAL 结果"


def test_watchdog_works_without_wal_injected():
    """未注入 WAL/DB 时降级为旧行为（撤单仍然执行），不得因可观测改造而失效。"""
    conn = _StubConn()

    def _fake_cancel(order_id):
        conn.cancelled.append(order_id)
        return {"ok": True}

    conn.adapter.cancel_order = _fake_cancel
    wd = OrderWatchdog(_Mgr(conn.bridge))
    asyncio.run(wd._handle_stale(conn, {"order_id": "OID-7", "code": "600519.SH"}))

    assert conn.cancelled == ["OID-7"]


# =========================== P0-10：停机显式关库 ============================


def test_db_close_is_idempotent_and_checkpoints(tmp_path):
    """close() 幂等：首次 True、重复 False；关闭后 -wal 文件应已被 checkpoint 清零。"""
    from core.db import DB

    path = tmp_path / "t.db"
    db = DB(path)
    db.execute("CREATE TABLE IF NOT EXISTS t (a TEXT)")
    db.insert("t", {"a": "x"})

    wal_file = tmp_path / "t.db-wal"
    assert db.close() is True
    assert db.close() is False, "close() 必须幂等，重复关闭不得抛异常"
    if wal_file.exists():
        assert wal_file.stat().st_size == 0, "-wal 未 checkpoint，残留未合并数据"
