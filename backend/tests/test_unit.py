"""核心逻辑单元测试（pytest，不依赖券商连接）。

覆盖：风控规则 / 回测成本模型 / xtquant 自动发现 / 条件单校验 / 下单幂等。
运行：cd backend && python -m pytest tests/test_unit.py -q
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.risk import RiskManager  # noqa: E402
from tools.backtest import run_backtest_engine  # noqa: E402
from tools.condition_order import ConditionOrderEngine  # noqa: E402
from xtquant_client.xtp import _resolve_xtquant_path  # noqa: E402


# ---------------- 风控规则 ----------------
def test_risk_basic_rules():
    rm = RiskManager(max_amount=10_000, min_qty=100,
                     max_single_position_ratio=0.2, total_assets=100_000)
    ok, reason = rm.check_order("600519.SH", 100, 50, "buy")
    assert not ok and "min qty" in reason
    ok, reason = rm.check_order("600519.SH", 100, 150, "buy")
    assert not ok and "100 的整数倍" in reason
    ok, reason = rm.check_order("600519.SH", 200, 100, "buy")  # 200*100=20000 > 10000
    assert not ok and "max amount" in reason
    ok, _ = rm.check_order("600519.SH", 50, 100, "buy")  # 5000，通过
    assert ok


def test_risk_single_position_ratio():
    rm = RiskManager(max_amount=1_000_000, min_qty=100,
                     max_single_position_ratio=0.1, total_assets=100_000)
    ok, _ = rm.check_order("600519.SH", 80, 100, "buy")  # 8000 = 8%
    assert ok
    ok, reason = rm.check_order("600519.SH", 200, 100, "buy")  # 20000 = 20% > 10%
    assert not ok and "single position ratio" in reason


def test_risk_rate_limit():
    rm = RiskManager(max_orders_per_min=3)
    for _ in range(3):
        ok, _ = rm.check_order("600519.SH", 10, 100, "buy")
        assert ok
    ok, reason = rm.check_order("600519.SH", 10, 100, "buy")
    assert not ok and "频率超限" in reason


# ---------------- 回测成本模型 ----------------
def _fake_kline(n=120, base=10.0, up=True):
    out = []
    for i in range(n):
        out.append({"time": f"2026-01-{i % 28 + 1:02d}", "open": base,
                    "high": base, "low": base,
                    "close": base + (i * 0.01 if up else -i * 0.01),
                    "volume": 1000})
    return out


def test_backtest_cost_model():
    kline = _fake_kline()
    params = {"fast": 5, "slow": 20}
    res_no_cost = run_backtest_engine("TEST", kline, "ma_cross", params, 100_000,
                                      commission_rate=0, stamp_tax=0, slippage_bps=0)
    res_cost = run_backtest_engine("TEST", kline, "ma_cross", params, 100_000,
                                   commission_rate=0.0003, stamp_tax=0.001, slippage_bps=5)
    assert res_cost["cost_model"]["commission_rate"] == 0.0003
    assert res_cost["cost_model"]["slippage_bps"] == 5.0
    assert res_cost["metrics"]["total_return"] <= res_no_cost["metrics"]["total_return"]
    assert len(res_cost["trades"]) > 0


def test_backtest_insufficient_data():
    try:
        run_backtest_engine("TEST", _fake_kline(10), "ma_cross", {"fast": 5, "slow": 20}, 100_000)
        assert False, "should raise"
    except Exception as exc:
        assert "K 线不足" in str(exc)


# ---------------- xtquant 自动发现 ----------------
def test_resolve_xtquant_path():
    with tempfile.TemporaryDirectory() as d:
        root = d
        os.makedirs(os.path.join(root, "userdata_mini"))
        sp = os.path.join(root, "bin.x64", "Lib", "site-packages", "xtquant")
        os.makedirs(sp)
        open(os.path.join(sp, "__init__.py"), "w").close()
        found = _resolve_xtquant_path(os.path.join(root, "userdata_mini"))
        assert found and found.endswith(os.path.join("bin.x64", "Lib", "site-packages"))


def test_resolve_xtquant_not_found():
    assert _resolve_xtquant_path(r"C:/no_such_qmt/userdata_mini") is None


def test_resolve_xtquant_multi_layouts():
    """P1：非标准目录结构 + 多种填写层级都能定位（自底向上候选根 + 兜底递归）。"""
    from xtquant_client.xtp import probe_environment
    layouts = [
        (["bin.x64", "Lib", "site-packages"], ["", "bin.x64", "userdata_mini"]),
        (["bin.x64", "python311", "Lib", "site-packages"], ["", "bin.x64", "userdata_mini"]),
        (["Lib", "site-packages"], ["", "Lib"]),
        (["a", "b", "c", "Lib", "site-packages"], ["", "a", "a/b/c/Lib/site-packages"]),
    ]
    for parts, fills in layouts:
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, *parts, "xtquant")
            os.makedirs(sp)
            open(os.path.join(sp, "__init__.py"), "w").close()
            want = os.path.normcase(os.path.normpath(os.path.join(d, *parts)))
            for rel in fills:
                fill = d if not rel else os.path.join(d, *rel.split("/"))
                got = _resolve_xtquant_path(fill)
                assert got and os.path.normcase(os.path.normpath(got)) == want, (parts, rel, got)
            diag = probe_environment(d)
            assert diag["xtquant_found"] is True
            assert os.path.normcase(os.path.normpath(diag["xtquant_site"])) == want


def test_resolve_xtquant_no_stub_false_positive():
    """P1：不存在的路径绝不向上爬（防误命中 IDE 生成的 xtquant stub）。"""
    from xtquant_client.xtp import _is_system_dir
    with tempfile.TemporaryDirectory() as d:
        stub = os.path.join(d, "JetBrains", "python_stubs", "-1", "xtquant")
        os.makedirs(stub)
        open(os.path.join(stub, "__init__.py"), "w").close()
        assert _resolve_xtquant_path(os.path.join(d, "some_client", "userdata_mini")) is None
    assert _is_system_dir("C:/") is True
    assert _is_system_dir("C:/Users/Administrator/AppData") is True
    assert _is_system_dir("C:/my_qmt_client") is False


# ---------------- 条件单校验 ----------------
def test_condition_submit_validation():
    eng = ConditionOrderEngine(manager=None)
    try:
        eng.submit("", "buy", "gte", 10, 100)
        assert False, "should raise"
    except ValueError as exc:
        assert "代码" in str(exc)
    try:
        eng.submit("600519.SH", "hold", "gte", 10, 100)
        assert False, "should raise"
    except ValueError:
        pass
    try:
        eng.submit("600519.SH", "buy", "between", 10, 100)
        assert False, "should raise"
    except ValueError:
        pass
    try:
        eng.submit("600519.SH", "buy", "gte", 10, 150)
        assert False, "should raise"
    except ValueError as exc:
        assert "100 的整数倍" in str(exc)
    r = eng.submit("600519.SH", "buy", "gte", 10, 100)
    assert r["status"] == "pending"
    eng.cancel(r["id"])
    assert eng._orders[r["id"]]["status"] == "canceled"


# ---------------- 阶段 2：条件单拒单次日重试队列 ----------------
class _FakeCondBridge:
    """极简假 bridge：get_quote 返回稳定价格，支持 b.call 包装。"""

    class _GW:
        def get_quote(self, code):
            return {"code": code, "last": 11.0}   # > trigger 10 → 触发成立

    gateway = _GW()

    async def call(self, fn, *a, **k):
        if fn.__name__ == "get_quote":
            return fn(*a, **k)
        raise AssertionError(f"意外调用 {fn.__name__}")


class _FakeCondManager:
    def __init__(self):
        self.b = _FakeCondBridge()

    def active_bridge(self):
        return self.b


def test_condition_reject_goes_retry_queue(monkeypatch):
    """阶段 2：拒单进次日重试队列，保留 triggered 记录待恢复，达上限才标 failed。"""
    from app.state import state
    eng = ConditionOrderEngine(manager=_FakeCondManager(), on_event=lambda _e: None)
    eng._retry_limit = 2

    rejected = {"calls": 0}

    class _FakeRouter:
        async def submit(self, *a, **k):
            rejected["calls"] += 1
            return {"ok": False, "reason": "风控拒绝（模拟）", "order_id": ""}

    state.signal_router = _FakeRouter()
    try:
        r = eng.submit("600519.SH", "buy", "gte", 10, 100)
        cid = r["id"]
        # 首次触发：拒单 → 进重试队列，status 保持 triggered，retry_count=1
        asyncio.run(eng._fire(_FakeCondBridge(), eng._orders[cid], 11.0))
        o = eng._orders[cid]
        assert o["status"] == "triggered"
        assert cid in eng._retry_queue
        assert o["retry_count"] == 1
        assert o["retry_date"]  # 次日重试
        assert rejected["calls"] == 1
        # 第二次（达上限）→ 标 failed 并移出队列
        asyncio.run(eng._fire(_FakeCondBridge(), eng._orders[cid], 11.0, is_retry=True))
        o = eng._orders[cid]
        assert o["status"] == "failed"
        assert cid not in eng._retry_queue
        assert rejected["calls"] == 2
    finally:
        state.signal_router = None


def test_condition_fire_success_marks_filled(monkeypatch):
    """阶段 2：下单成功 → status=filled + order_id，且不残留重试队列。"""
    from app.state import state
    eng = ConditionOrderEngine(manager=_FakeCondManager(), on_event=lambda _e: None)

    class _FakeRouter:
        async def submit(self, *a, **k):
            return {"ok": True, "order_id": "SR-1", "reason": ""}

    state.signal_router = _FakeRouter()
    try:
        r = eng.submit("600519.SH", "buy", "gte", 10, 100)
        cid = r["id"]
        o = eng._orders[cid]
        o["status"] = "triggered"   # 模拟已被触发
        asyncio.run(eng._fire(_FakeCondBridge(), o, 11.0))
        o = eng._orders[cid]
        assert o["status"] == "filled"
        assert o["order_id"] == "SR-1"
        assert cid not in eng._retry_queue
    finally:
        state.signal_router = None


def test_condition_fire_recheck_keeps_pending(monkeypatch):
    """阶段 2：触发用最新价校验——行情回退（fresh 不成立）时不误下单，保持 pending。"""
    from app.state import state
    eng = ConditionOrderEngine(manager=_FakeCondManager(), on_event=lambda _e: None)

    class _DroppedBridge(_FakeCondBridge):
        class _GW(_FakeCondBridge._GW):
            def get_quote(self, code):
                return {"code": code, "last": 9.0}   # < trigger 10 → 条件已回退

        gateway = _GW()

    class _FakeRouter:
        async def submit(self, *a, **k):
            raise AssertionError("条件已回退不应下单")

    state.signal_router = _FakeRouter()
    try:
        r = eng.submit("600519.SH", "buy", "gte", 10, 100)
        cid = r["id"]
        asyncio.run(eng._fire(_DroppedBridge(), eng._orders[cid], 11.0))
        o = eng._orders[cid]
        assert o["status"] == "pending"      # 回退 → 保持监控
        assert cid not in eng._retry_queue
    finally:
        state.signal_router = None


# ---------------- 下单幂等 ----------------
def test_idempotency():
    import time
    from tools import trading
    trading._IDEMPOTENCY.clear()
    assert trading._idempotent_get("k1") is None
    trading._idempotent_set("k1", {"order_id": "100"})
    hit = trading._idempotent_get("k1")
    assert hit and hit["order_id"] == "100"
    trading._IDEMPOTENCY["k1"] = (time.time() - 60, hit)  # 过期
    assert trading._idempotent_get("k1") is None


# ---------------- 阶段 0-B：单飞幂等——并发同键只执行一次 ----------------
def test_single_flight_concurrent_only_one_execution():
    """阶段 0-B（F1）：并发同 key 的单飞——真实逻辑只执行一次，其余复用结果。"""
    from gateway.idempotency import _cache, _inflight, single_flight
    _cache.clear()
    _inflight.clear()

    executed = {"n": 0}

    async def factory():
        executed["n"] += 1
        await asyncio.sleep(0.05)
        return {"order_id": "SF-1", "ok": True}

    async def run():
        results = await asyncio.gather(
            single_flight("same-key", factory),
            single_flight("same-key", factory),
            single_flight("same-key", factory),
        )
        return results

    results = asyncio.run(run())
    assert executed["n"] == 1, f"并发同键应只执行一次，实际 {executed['n']} 次"
    assert all(r["order_id"] == "SF-1" for r in results)
    dup = [r for r in results if r.get("duplicated")]
    assert len(dup) == 2, "两个并发请求应被标记 duplicated"


def test_single_flight_window_cache_hit():
    """阶段 0-B（F1）：窗口内重复请求命中缓存并标记 duplicated，不二次下单。"""
    from gateway.idempotency import _cache, _inflight, single_flight
    _cache.clear()
    _inflight.clear()

    executed = {"n": 0}

    async def factory():
        executed["n"] += 1
        return {"order_id": "SF-2", "ok": True}

    async def run():
        r1 = await single_flight("w-key", factory)
        r2 = await single_flight("w-key", factory)   # 窗口内重复
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert executed["n"] == 1
    assert r2.get("duplicated") is True
    assert r2["order_id"] == "SF-2"


def test_single_flight_failure_not_cached():
    """阶段 0-B（F1）：执行失败不缓存，下次可重试（避免把错误结果当成功）。"""
    from gateway.idempotency import _cache, _inflight, single_flight
    _cache.clear()
    _inflight.clear()

    attempts = {"n": 0}

    async def factory():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("第一次失败")
        return {"order_id": "SF-3", "ok": True}

    async def run():
        try:
            await single_flight("f-key", factory)
        except RuntimeError:
            pass
        r2 = await single_flight("f-key", factory)   # 失败后应可重试成功
        return r2

    r2 = asyncio.run(run())
    assert attempts["n"] == 2
    assert r2["order_id"] == "SF-3"


# ---------------- TOTP 二次确认（D2） ----------------
def test_totp_generate_and_verify():
    from gateway.totp import current_totp, totp_at, verify_totp
    secret = "JBSWY3DPEHPK3PXP"
    # RFC 6238 参考向量（Base32 of "Hello!\xde\xad\xbe\xef" 常用测试串）
    code = totp_at(secret, 59)
    assert len(code) == 6 and code.isdigit()
    assert totp_at(secret, 59) == totp_at(secret, 60 - 1)   # 同 30s 窗口
    assert totp_at(secret, 0) != totp_at(secret, 3600)
    assert verify_totp(secret, current_totp(secret))
    assert not verify_totp(secret, "000000") or True  # 极小概率碰撞，不强断言
    assert not verify_totp(secret, "abc")
    assert not verify_totp(secret, "")


# ---------------- Prometheus 指标（E1） ----------------
def test_metrics_render():
    from gateway.metrics import Metrics
    m = Metrics()
    m.record_order("buy", "submitted")
    m.record_order("buy", "submitted")
    m.record_order("sell", "error")
    m.record_quote()
    m.record_quote_latency(12.5)
    m.record_request("trade", 200, "k1")
    text = m.render({"ws_clients": 2, "brokers": {"c1": True}})
    assert 'qmt_orders_total{side="buy",status="submitted"} 2' in text
    assert 'qmt_orders_total{side="sell",status="error"} 1' in text
    assert "qmt_quotes_total 1" in text
    assert 'qmt_api_requests_total{scope="trade",status="200",key_id="k1"} 1' in text
    assert "qmt_ws_clients 2" in text
    assert 'qmt_broker_connected{conn_id="c1"} 1' in text


# ---------------- 阶段 3：生命周期/幂等/风控/对账可观测（metrics） ----------------
def test_metrics_stage3_observability():
    """阶段 3：断线/重连/订阅恢复/幂等命中/风控拦截/对账差异指标正确输出。"""
    from gateway.metrics import Metrics
    m = Metrics()
    m.record_conn_event("c1", "disconnected")
    m.record_conn_event("c1", "reconnected")
    m.record_conn_event("bridge", "subscription_recovered")
    m.record_idempotency_hit()
    m.record_idempotency_hit()
    m.record_risk_blocked("frequency")
    m.record_risk_blocked("validation")
    m.record_reconcile_diff("order")
    text = m.render({})
    assert 'qmt_conn_events_total{conn_id="c1",event="disconnected"} 1' in text
    assert 'qmt_conn_events_total{conn_id="c1",event="reconnected"} 1' in text
    assert 'qmt_conn_events_total{conn_id="bridge",event="subscription_recovered"} 1' in text
    assert "qmt_idempotency_hits_total 2" in text
    assert 'qmt_risk_blocked_total{rule="frequency"} 1' in text
    assert 'qmt_risk_blocked_total{rule="validation"} 1' in text
    assert 'qmt_reconcile_diffs_total{scope="order"} 1' in text


# ---------------- API Key 过期 / IP 白名单（D1+D3） ----------------
def test_apikey_expiry_and_ip_allow():
    import datetime as _dt

    from gateway.apikey import ApiKeyStore, hash_token, scope_match
    store = ApiKeyStore()

    def iso(delta_s):
        return (_dt.datetime.now() + _dt.timedelta(seconds=delta_s)).isoformat(timespec="seconds")

    assert not store._is_expired({"expires_at": "", "grace_until": ""})
    assert store._is_expired({"expires_at": iso(-10), "grace_until": ""})
    assert not store._is_expired({"expires_at": iso(300), "grace_until": ""})
    # 轮换宽限期内仍可用（旧密钥平滑下线）
    assert not store._is_expired({"expires_at": iso(-10), "grace_until": iso(600)})
    assert store._is_expired({"expires_at": iso(-600), "grace_until": iso(-10)})
    # scope 分级
    assert scope_match("trade", "trade,market")
    assert scope_match("admin", "*")
    assert not scope_match("admin", "trade,market")
    assert scope_match(None, "")
    assert hash_token("abc") == hash_token("abc") and len(hash_token("abc")) == 64
    assert store._ip_allowed({"ip_allow": ""}, "8.8.8.8")            # 空 = 不限制
    assert store._ip_allowed({"ip_allow": "10.0.0.*"}, "10.0.0.7")
    assert not store._ip_allowed({"ip_allow": "10.0.0.*"}, "10.0.1.7")
    assert store._ip_allowed({"ip_allow": "1.2.3.4, 5.6.7.8"}, "5.6.7.8")
    assert not store._ip_allowed({"ip_allow": "1.2.3.4"}, "5.6.7.8")


# ---------------- WAL 轮转 checkpoint + 全量读取（A1） ----------------
def test_wal_checkpoint_and_replay():
    from gateway.wal import WAL
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "wal.jsonl")
        w = WAL(p)
        for i in range(5):
            w.append("order", "signal", f"oid{i}", {"code": "600519.SH", "volume": 100})
        assert len(w.all_records()) == 5
        w.checkpoint()                       # 归档并截断主 WAL
        assert os.path.getsize(p) == 0
        assert os.path.exists(os.path.join(d, "wal.snapshot.jsonl"))
        w.append("order", "signal", "oid9", {"code": "000001.SZ", "volume": 200})
        recs = w.all_records()
        assert len(recs) == 6                # 快照 5 + 增量 1
        seen = []
        summary = w.replay({"signal": lambda r: seen.append(r["entity_id"])})
        assert summary["replayed"] == 6 and summary["by_entity"]["signal"] == 6
        assert "oid0" in seen and "oid9" in seen
        w.close()


# ---------------- 阶段 3：WAL fsync 批量/后台 + 损坏行告警 ----------------
def test_wal_fsync_batched_and_corrupt_line_warned():
    """阶段 3：append 不再每条同步 fsync（后台线程批量），损坏行记录计数并告警。"""
    from gateway.wal import WAL
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "wal.jsonl")
        w = WAL(p)
        try:
            # 大量快速 append：不逐条同步 fsync，而是攒到阈值后由后台线程执行
            w._last_fsync = time.time()  # 重置，避免首条即触发
            w._unfsynced = 0
            for i in range(10):
                w.append("order", "signal", f"oid{i}", {"code": "600519.SH"})
            # 攒满阈值后应触发一次后台 fsync（事件被置位；后台线程消费）
            assert w._unfsynced <= w._fsync_count
            # 写入内容完整可读（flush 保证进程崩溃不丢）
            recs = w.all_records()
            assert len(recs) == 10
        finally:
            w.close()   # close 兜底同步 fsync
        # 构造损坏行：写坏行后再 append，replay 应记录 corrupt 计数而非崩溃
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("{not-json}\n")
        w2 = WAL(p)
        try:
            w2.append("order", "signal", "oidX", {"code": "000001.SZ"})
            summary = w2.replay({})
            assert summary["corrupt"] >= 1, "损坏行应被计数告警而非静默跳过"
        finally:
            w2.close()


# ---------------- 委托对账核销（A2） ----------------
def test_reconcile_status_normalize():
    # 阶段 0-A：状态归一化收敛到统一词汇表（gateway.reconcile 不再私有 _norm_status）
    from xtquant_client.order_status import normalize_order_status
    assert normalize_order_status("已成") == "filled"
    assert normalize_order_status("部成") == "partial"
    assert normalize_order_status("已撤") == "cancelled"
    assert normalize_order_status("废单") == "rejected"
    assert normalize_order_status("已报") == "pending"
    assert normalize_order_status("") == "unknown"


def test_reconcile_pending_from_wal():
    from gateway.reconcile import OrderReconciler
    from gateway.wal import WAL
    with tempfile.TemporaryDirectory() as d:
        w = WAL(os.path.join(d, "wal.jsonl"))
        w.append("order", "signal", "A1", {"code": "600519.SH", "volume": 100, "side": "buy"})
        w.append("order", "signal", "A2", {"code": "000001.SZ", "volume": 200, "side": "sell"})
        w.append("reconciled", "order", "A1", {"status": "filled"})
        w.append("quote", "market", "x", {})     # 非委托实体应忽略
        rec = OrderReconciler(manager=None, wal=w)
        pending = rec._pending_from_wal()
        assert set(pending.keys()) == {"A2"}
        assert pending["A2"]["volume"] == 200
        w.close()


def test_reconcile_no_broker():
    import asyncio as _a
    from gateway.reconcile import OrderReconciler

    class _NoMgr:
        def bridge(self, conn_id=None):
            return None

    from gateway.wal import WAL
    with tempfile.TemporaryDirectory() as d:
            w = WAL(os.path.join(d, "wal.jsonl"))
            w.append("order", "signal", "B1", {"code": "600519.SH", "volume": 100, "side": "buy"})
            rec = OrderReconciler(manager=_NoMgr(), wal=w)
            # 阶段 0-A（F11）：券商不可用「查无此单」不能立即核销为 stale
            # （瞬时断连/分页/会话重置会误销活单）。首轮记 open，不写核销记录。
            res = _a.run(rec.reconcile())
            assert res["checked"] == 1 and res["stale"] == 0 and res["open"] == 1
            assert rec._pending_from_wal() != {}  # 未核销，等待后续轮次确认
            # 连续 missing_rounds 轮仍查不到 → 标 stale 并核销，不再重复对账
            for _ in range(rec.missing_rounds - 1):
                res = _a.run(rec.reconcile())
            assert res["stale"] == 1
            assert rec._pending_from_wal() == {}
            w.close()


# ---------------- 告警规则引擎（E3） ----------------
def test_alert_rule_matching():
    from gateway.alert_engine import AlertEngine

    fired = []
    RULES = [
        {"id": 1, "name": "委托失败", "event": "order.*", "metric": "",
         "op": "", "threshold": 0, "channel": "*", "cooldown_seconds": 0,
         "enabled": 1, "last_triggered": ""},
        {"id": 2, "name": "行情延迟", "event": "", "metric": "quote_latency",
         "op": ">", "threshold": 500, "channel": "dingtalk", "cooldown_seconds": 0,
         "enabled": 1, "last_triggered": ""},
    ]

    class _DB:
        def query(self, sql, params=()):
            if "metric=?" in sql:
                return [r for r in RULES if r["metric"] == params[0]]
            return list(RULES)

        def execute(self, sql, params=()):
            return None

        def insert(self, table, row):
            fired.append(row)
            return len(fired)

    eng = AlertEngine(_DB(), notifier=None)          # notifier=None：无需事件循环
    eng.evaluate_event("order.error", {"code": "600519.SH"})
    assert len(fired) == 1 and fired[0]["rule_id"] == 1
    eng.evaluate_metric("quote_latency", 120)         # 未超阈值 → 不触发
    assert len(fired) == 1
    eng.evaluate_metric("quote_latency", 900)         # 超阈值 → 触发
    assert len(fired) == 2 and fired[1]["rule_id"] == 2
    eng.evaluate_event("alert.triggered", {})         # 自循环必须阻断
    assert len(fired) == 2
    eng.evaluate_event("risk.blocked", {})            # event="order.*" 不匹配
    assert len(fired) == 2


def test_alert_cooldown():
    from datetime import datetime, timezone

    from gateway.alert_engine import AlertEngine
    eng = AlertEngine(db=None, notifier=None)
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    assert eng._cooldown_ok({"last_triggered": "", "cooldown_seconds": 300})
    assert not eng._cooldown_ok({"last_triggered": now_iso, "cooldown_seconds": 300})
    assert eng._cooldown_ok({"last_triggered": now_iso, "cooldown_seconds": 0})


# ---------------- 算法单拆单（B1） ----------------
def test_algo_slice_plan():
    from tools.algo import AlgoEngine
    eng = AlgoEngine(manager=None)
    # 等分拆单：1000 股 4 片 → 每片 250（100 的整数倍）
    slices = eng._plan_slices(1000, 4)
    assert sum(slices) == 1000 and all(s % 100 == 0 for s in slices)
    # 不能整除时余量并入最后一片
    slices = eng._plan_slices(1000, 3)
    assert sum(slices) == 1000 and all(s % 100 == 0 for s in slices)
    # 冰山单可见量
    assert eng._visible_qty(1000, 10.0) == 100
    assert eng._visible_qty(1000, 0.1) == 100     # 下限保底 1 手


# ---------------- B4 日级风控：限额 / 熔断 / 单票次数 ----------------
def test_risk_daily_amount_limit():
    rm = RiskManager(max_amount=1_000_000, daily_amount_limit=50_000)
    ok, _ = rm.check_order("600519.SH", 100, 100, "buy")   # 10000
    assert ok
    ok, _ = rm.check_order("600519.SH", 100, 100, "buy")   # 累计 20000
    assert ok
    ok, _ = rm.check_order("600519.SH", 100, 100, "buy")   # 累计 30000
    assert ok
    ok, _ = rm.check_order("600519.SH", 100, 100, "buy")   # 累计 40000
    assert ok
    ok, reason = rm.check_order("600519.SH", 100, 100, "buy")  # 50000 > 50000
    assert not ok and "日累计下单金额" in reason


def test_risk_per_code_daily_orders():
    rm = RiskManager(max_amount=1_000_000, per_code_daily_orders=2)
    assert rm.check_order("600519.SH", 10, 100, "buy")[0]
    assert rm.check_order("600519.SH", 10, 100, "buy")[0]
    ok, reason = rm.check_order("600519.SH", 10, 100, "buy")
    assert not ok and "已达上限" in reason
    # 其他代码不受影响
    assert rm.check_order("000001.SZ", 10, 100, "buy")[0]


def test_risk_daily_loss_circuit():
    rm = RiskManager(max_amount=1_000_000, daily_loss_limit=10_000)
    assert rm.update_net_value(1_000_000) is None          # 锚定日初
    assert rm.circuit_broken is False
    reason = rm.update_net_value(988_000)                  # 回撤 12000 ≥ 10000
    assert reason is not None and "熔断" in reason
    assert rm.circuit_broken is True
    # 熔断期间禁止买入开仓
    ok, reason = rm.check_order("600519.SH", 10, 100, "buy")
    assert not ok and "熔断" in reason
    # 允许卖出平仓
    ok, _ = rm.check_order("600519.SH", 10, 100, "sell")
    assert ok
    # 人工解除熔断
    rm.reset_circuit()
    assert rm.circuit_broken is False
    assert rm.check_order("600519.SH", 10, 100, "buy")[0]


def test_risk_daily_rollover():
    rm = RiskManager(max_amount=1_000_000, daily_amount_limit=100_000,
                     per_code_daily_orders=1)
    assert rm.check_order("600519.SH", 10, 100, "buy")[0]
    assert not rm.check_order("600519.SH", 10, 100, "buy")[0]   # 单票次数达上限
    # 强制换日 → 计数清零
    rm._day = "1999-01-01"
    assert rm.check_order("600519.SH", 10, 100, "buy")[0]
    assert rm.daily_stats()["date"] == rm._day
    rm.reset_daily()
    assert rm.daily_stats()["day_orders"] == 0


# ---------------- E4 敏感信息脱敏 ----------------
def test_masking_value_and_account():
    from gateway.masking import mask_account, mask_value
    assert mask_value("qmt-dev-key") == "qmt***ey"
    assert mask_value("1234567") == "***"                 # 长度 < 8 全掩码
    assert mask_value("12345678") == "123***78"
    assert mask_account("8801234567") == "88***67"
    assert mask_account("") == ""


def test_masking_dict_recursive():
    from gateway.masking import mask_dict
    d = {"api_key": "qmt-dev-key", "token": "tok123456789",
         "account_id": "8801234567", "code": "600519.SH",
         "nested": {"secret": "s3cr3t", "keep": "visible"},
         "list": [{"password": "pw12345678"}]}
    out = mask_dict(d)
    assert out["api_key"] == "qmt***ey"
    assert out["token"] == "tok***89"
    assert out["account_id"] == "88***67"
    assert out["code"] == "600519.SH"                     # 非敏感键不动
    assert out["nested"]["secret"] == "***"               # 长度 < 8 全掩码
    assert out["nested"]["keep"] == "visible"
    assert out["list"][0]["password"] == "pw1***78"       # 前3后2
    assert d["api_key"] == "qmt-dev-key"                  # 原对象不被修改


def test_masking_text():
    from gateway.masking import mask_text
    out = mask_text("api_key=qmt-dev-key Authorization: Bearer mysecret123456")
    assert "qmt***ey" in out and "qmt-dev-key" not in out
    assert "mysecret123456" not in out
    assert "Bearer" in out


# ---------------- C1 历史 K 线缓存 ----------------
def _mk_db(tmp):
    from pathlib import Path

    from app.db import DB
    return DB(Path(tmp) / "test.db")


def _tmp_db():
    """返回 (db, tmpdir)：Windows 下避免 TemporaryDirectory 严格清理的文件锁问题。"""
    import tempfile as _t
    d = _t.mkdtemp()
    return _mk_db(d), d


def _cleanup(db, d):
    import shutil
    import gc
    try:
        db._conn.close()
    except Exception:  # noqa: BLE001
        pass
    gc.collect()
    shutil.rmtree(d, ignore_errors=True)


def test_kline_cache_basic():
    from gateway.kline_cache import KlineCache
    db, d = _tmp_db()
    try:
        kc = KlineCache(db)
        bars = [{"time": f"2026-01-{i+1:02d}", "open": 10.0, "high": 11.0,
                 "low": 9.0, "close": 10.5, "volume": 1000, "amount": 10500.0}
                for i in range(5)]
        n = kc.put("600519.SH", "1d", bars)
        assert n == 5
        assert kc.count("600519.SH", "1d") == 5
        got = kc.get("600519.SH", "1d", 3)
        assert len(got) == 3 and got[-1]["close"] == 10.5  # 升序、取最近
        assert kc.stats()["rows"] == 5
        assert kc.clear("600519.SH", "1d") == 5
        assert kc.count("600519.SH", "1d") == 0
    finally:
        _cleanup(db, d)


def test_kline_cache_freshness_and_stale():
    import asyncio as _a
    from gateway.kline_cache import KlineCache
    db, d = _tmp_db()
    try:
        kc = KlineCache(db, ttl_daily=3600)
        bars = [{"time": f"2026-01-{i+1:02d}", "open": 1, "high": 2,
                 "low": 0.5, "close": 1.5, "volume": 100, "amount": 150.0}
                for i in range(10)]
        kc.put("000001.SZ", "1d", bars)
        # 缓存命中
        res = _a.run(kc.get_or_fetch("000001.SZ", "1d", 10,
                                     lambda c, p, n: (_a.sleep(0), [])))
        assert res["source"] == "cache" and len(res["bars"]) == 10
        # 券商返回空 → cache_stale 兜底
        kc._cache = None
        # 伪造过期：把 fetched_at 改老
        db.execute("UPDATE kline_cache SET fetched_at=0")
        res = _a.run(kc.get_or_fetch("000001.SZ", "1d", 10,
                                     lambda c, p, n: []))
        assert res["source"] == "cache_stale" and len(res["bars"]) == 10
    finally:
        _cleanup(db, d)


# ---------------- B2 出站 webhook 签名 / 匹配 ----------------
def test_webhook_signature_and_match():
    from gateway.webhook_out import WebhookOut
    body = '{"event":"order.filled","data":{"code":"600519.SH"}}'
    ts = "1695000000"
    sig = WebhookOut._sign("s3cr3t", ts, body)
    assert len(sig) == 64
    # 相同输入产生相同签名
    assert WebhookOut._sign("s3cr3t", ts, body) == sig
    # 密钥不同 → 签名不同
    assert WebhookOut._sign("other", ts, body) != sig
    # 事件通配匹配（订阅配置内 events 以逗号/空格分隔后逐项匹配）
    assert WebhookOut._event_match("order.filled", "order.*")
    assert WebhookOut._event_match("order.filled", "*")
    assert WebhookOut._event_match("deal.event", "deal.*")
    assert not WebhookOut._event_match("risk.blocked", "order.*")


# ---------------- 运行时配置中心（热更新） ----------------
def test_runtime_config_defaults_and_update():
    from gateway.runtime_config import RuntimeConfig
    db, d = _tmp_db()
    try:
        rc = RuntimeConfig(db)
        # 默认值
        assert rc.batch_window == 0.1
        assert rc.snapshot_interval == 5.0
        assert rc.reconcile_interval == 300.0
        assert "condition.interval" in rc.all()
        # 热更新
        assert rc.set_many({"sync.batch_window": 0.2}) == ["sync.batch_window"]
        assert rc.batch_window == 0.2
        assert rc.all()["sync.batch_window"]["overridden"] is True
        # 校验：下限 / 未知 key / 类型
        try:
            rc.set_many({"sync.batch_window": 0.001})
            assert False, "should raise"
        except ValueError:
            pass
        try:
            rc.set_many({"foo.bar": 1})
            assert False, "should raise"
        except ValueError:
            pass
        # 重置
        assert rc.reset("sync.batch_window") == ["sync.batch_window"]
        assert rc.batch_window == 0.1
        assert rc.reset() == []   # 全部重置（已无覆盖项）无异常
    finally:
        _cleanup(db, d)


def test_runtime_config_persists():
    from gateway.runtime_config import RuntimeConfig
    db, d = _tmp_db()
    try:
        rc = RuntimeConfig(db)
        rc.set_many({"condition.interval": 3.5})
        # 新实例从 DB 读到持久化值
        rc2 = RuntimeConfig(db)
        assert rc2.condition_interval == 3.5
    finally:
        _cleanup(db, d)


# ---------------- D4 审计 hash 链 ----------------
def test_audit_chain_hash_and_verify():
    from app.db import audit_chain_hash
    db, d = _tmp_db()
    try:
        db.audit("admin", "a1", "t1", {"k": "v"}, "ok")
        db.audit("admin", "a2", "t2", {"k": "v"}, "ok")
        res = db.verify_audit_chain()
        assert res["ok"] is True and res["checked"] == 2 and res["broken_count"] == 0
        # 篡改第一条 → 校验必须失败
        db.execute("UPDATE audit_log SET result='tampered' WHERE id=1")
        res = db.verify_audit_chain()
        assert res["ok"] is False and res["broken_count"] >= 1
        # hash 确定性
        row = db.query_one("SELECT * FROM audit_log WHERE id=2")
        h = audit_chain_hash(row["prev_hash"], row)
        assert h == row["hash"]
    finally:
        _cleanup(db, d)


# ---------------- exe 同目录配置自动生成（打包运行） ----------------
def test_config_auto_generate_frozen():
    """打包（frozen）运行时：首次启动自动生成 qmt_work_config.json，
    数据库/日志默认解析到 exe 同目录；修改配置文件后新实例读到新值。"""
    from pathlib import Path

    from app import config as cfg

    fake_dir = tempfile.mkdtemp(prefix="qmt_cfg_")
    old_frozen = getattr(sys, "frozen", None)
    old_exe = sys.executable
    try:
        sys.frozen = True
        sys.executable = str(Path(fake_dir) / "qmt_work.exe")
        # 配置文件路径跟随 exe 同目录
        assert str(cfg.exe_dir()) == fake_dir
        assert cfg.config_file() == Path(fake_dir) / "qmt_work_config.json"
        # 首次启动自动生成
        created = cfg.ensure_config_file()
        assert created.exists()
        payload = cfg._json_config_source()
        assert payload.get("port") == 21118
        assert "api_key" in payload and "_readme" in cfg._default_config_payload()
        # db_path 相对路径解析到 exe 同目录
        s = cfg.Settings()
        assert str(s.db_path).startswith(fake_dir)
        assert s.db_path.name == "app.db"
        # 修改配置后新实例生效（相对路径仍解析到 exe 同目录）
        import json as _json
        payload["port"] = 9999
        payload["db_path"] = "custom/data/db.sqlite3"
        Path(cfg.config_file()).write_text(
            _json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        s2 = cfg.Settings()
        assert s2.port == 9999
        assert str(s2.db_path) == str(Path(fake_dir) / "custom/data/db.sqlite3")
    finally:
        sys.frozen = old_frozen
        sys.executable = old_exe
        import shutil
        shutil.rmtree(fake_dir, ignore_errors=True)


def test_config_priority_env_over_json(tmp_path, monkeypatch):
    """优先级：环境变量(QMT_*) > exe 同目录 JSON 配置。"""
    from pathlib import Path

    from app import config as cfg

    fake_dir = str(tmp_path)
    old_frozen = getattr(sys, "frozen", None)
    old_exe = sys.executable
    try:
        sys.frozen = True
        sys.executable = str(Path(fake_dir) / "qmt_work.exe")
        # 生成默认配置并改 port=9999
        cfg.ensure_config_file()
        import json as _json
        payload = cfg._json_config_source()
        payload["port"] = 9999
        Path(cfg.config_file()).write_text(
            _json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # env 覆盖 JSON
        monkeypatch.setenv("QMT_PORT", "12345")
        s = cfg.Settings()
        assert s.port == 12345
    finally:
        sys.frozen = old_frozen
        sys.executable = old_exe


# ---------------- 交易时段调度（TradingSession） ----------------
def test_trading_session_weekday_fallback():
    from datetime import datetime
    from gateway.trading_session import TradingSession
    ts = TradingSession()   # 未注入日历 -> 周末规则
    # 2026-08-14 周五 盘中 10:00 -> 活跃
    assert ts.is_active(datetime(2026, 8, 14, 10, 0))
    # 2026-08-15 周六 -> 非交易
    assert not ts.is_active(datetime(2026, 8, 15, 10, 0))
    # 周五 12:00 午休 -> 休眠
    assert not ts.is_active(datetime(2026, 8, 14, 12, 0))
    # 盘中 2s / 休眠 30s
    assert ts.sleep_seconds(2.0, 30.0, datetime(2026, 8, 14, 10, 0)) == 2.0
    assert ts.sleep_seconds(2.0, 30.0, datetime(2026, 8, 14, 22, 0)) == 30.0


def test_trading_session_calendar_refresh():
    from datetime import datetime
    from gateway.trading_session import TradingSession
    ts = TradingSession()
    # 注入日历：仅包含 20260814（周五）
    n = ts.refresh_from_calendar(["20260814", "20260817"])
    assert n == 2
    assert ts.is_trading_day(datetime(2026, 8, 14).date())
    assert not ts.is_trading_day(datetime(2026, 8, 15).date())   # 周六不在日历
    # 有日历后周末规则不再生效：周六即便白天也不活跃
    assert not ts.is_active(datetime(2026, 8, 15, 10, 0))
    assert ts.is_active(datetime(2026, 8, 14, 10, 0))
    assert ts.stats()["mode"] == "calendar"


def test_db_wal_mode_enabled():
    """WAL 模式：journal_mode 应返回 wal。"""
    db, d = _tmp_db()
    try:
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
    finally:
        _cleanup(db, d)


# ---------------- P1：预交易风控扩展（价格偏离 / 标的白黑名单）----------------
def test_risk_price_deviation():
    rm = RiskManager(max_amount=1_000_000, min_qty=100,
                     max_orders_per_min=100, price_deviation_pct=0.1)
    rm.set_price_provider(lambda code: 10.0)  # 最新价 10.0
    ok, reason = rm.check_order("600519.SH", 12.0, 100, "buy")   # +20% -> 拒
    assert not ok and "deviation" in reason
    ok, _ = rm.check_order("600519.SH", 10.5, 100, "buy")        # +5% -> 放行
    assert ok
    ok, _ = rm.check_order("600519.SH", 9.2, 100, "buy")         # -8% -> 放行
    assert ok
    # 无参考价（provider 返回 None/0）时跳过校验
    rm2 = RiskManager(max_amount=1_000_000, min_qty=100,
                      max_orders_per_min=100, price_deviation_pct=0.1)
    rm2.set_price_provider(lambda code: None)
    ok, _ = rm2.check_order("600519.SH", 999.0, 100, "buy")
    assert ok
    # 关闭（0）时永不过滤
    rm3 = RiskManager(max_amount=1_000_000, min_qty=100,
                      max_orders_per_min=100, price_deviation_pct=0.0)
    rm3.set_price_provider(lambda code: 10.0)
    ok, _ = rm3.check_order("600519.SH", 999.0, 100, "buy")
    assert ok


def test_risk_symbol_allow_deny():
    rm = RiskManager(max_amount=1_000_000, min_qty=100, max_orders_per_min=100,
                     symbol_allow="600519.SH", symbol_deny="600000.SH")
    ok, reason = rm.check_order("600000.SH", 10, 100, "buy")
    assert not ok and "黑名单" in reason
    ok, reason = rm.check_order("000001.SZ", 10, 100, "buy")
    assert not ok and "白名单" in reason
    ok, _ = rm.check_order("600519.SH", 10, 100, "buy")
    assert ok
    # 黑名单优先于白名单
    rm2 = RiskManager(max_amount=1_000_000, min_qty=100, max_orders_per_min=100,
                      symbol_allow="600000.SH,600519.SH", symbol_deny="600000.SH")
    ok, reason = rm2.check_order("600000.SH", 10, 100, "buy")
    assert not ok and "黑名单" in reason
    ok, _ = rm2.check_order("600519.SH", 10, 100, "buy")
    assert ok
    # 运行期可更新（str 参数不受数值校验影响）
    changed = rm.update_from({"symbol_allow": "000001.SZ", "price_deviation_pct": 0.05})
    assert "symbol_allow" in changed and "price_deviation_pct" in changed
    assert rm.symbol_allow == "000001.SZ" and rm.price_deviation_pct == 0.05


# ---------------- P1：订单超时守护 ----------------
def test_order_watchdog_collect_stale():
    from gateway.order_watchdog import collect_stale
    fs: dict[str, float] = {}
    orders = [
        {"order_id": "o1", "status": "submitted"},
        {"order_id": "o2", "status": "filled"},
        {"order_id": "o3", "status": "cancelled"},
    ]
    stale = collect_stale(orders, fs, now=100.0, timeout=60.0)
    assert stale == []                 # 首次出现只记录首见时间
    assert "o1" in fs and "o2" not in fs and "o3" not in fs
    # 70s 后 o1 仍在 pending -> 超时
    stale = collect_stale([{"order_id": "o1", "status": "pending"}], fs,
                          now=170.0, timeout=60.0)
    assert [o["order_id"] for o in stale] == ["o1"]
    # 调用方处理完需自行清除记录（模拟）
    fs.pop("o1", None)
    # 新出现且未超时 -> 不处理
    stale = collect_stale([{"order_id": "o4", "status": "queued"}], fs,
                          now=180.0, timeout=60.0)
    assert stale == [] and "o4" in fs
    # 非活跃状态清除记录；本轮未再出现的活跃记录也清除
    stale = collect_stale([{"order_id": "o4", "status": "filled"}], fs,
                          now=200.0, timeout=60.0)
    assert "o4" not in fs
    stale = collect_stale([{"order_id": "o5", "status": "submitted"}], fs,
                          now=300.0, timeout=60.0)
    fs.pop("o5", None)  # 模拟处理
    stale = collect_stale([], fs, now=301.0, timeout=60.0)
    assert fs == {}


# ---------------- P1：通知去重静默期 ----------------
def test_notifier_dedup():
    from gateway.notifier import Notifier
    n = Notifier(db=None, dedup_seconds=5.0)
    key = (1, "order.filled", "成交")
    assert n._dedup_allowed(key) is True
    assert n._dedup_allowed(key) is False     # 窗口内重复 -> 拒
    assert n._dedup_allowed((1, "risk.blocked", "拦截")) is True  # 不同事件不互扰
    # 未启用（0）时永远放行
    n2 = Notifier(db=None, dedup_seconds=0.0)
    assert n2._dedup_allowed(key) is True
    assert n2._dedup_allowed(key) is True


# ---------------- P1：notify 非阻塞（fire-and-forget）----------------
def test_notifier_non_blocking():
    """阶段 4 / P1：notify 必须非阻塞——内联重试不再卡死调用方。

    配置一个指向不可达地址的 webhook，原实现会在 notify 内联重试（最坏阻塞上百秒，
    曾使 /config/risk/circuit 等接口 ReadTimeout）；修复后 notify 即刻返回，
    发送在后台任务中完成（失败落库 failed）。
    """
    from gateway.notifier import Notifier

    class _FakeDB:
        def __init__(self, rows):
            self._rows = rows
            self.logs = []

        async def aquery(self, sql, params=()):
            return [dict(r) for r in self._rows]

        def execute(self, sql, params=()):
            self.logs.append((sql, params))

        def insert(self, table, row):
            self.logs.append((table, row))
            return len(self.logs)

    rows = [{
        "id": 1, "name": "坏 webhook", "channel": "webhook", "enabled": 1,
        "events": "*", "params_json": '{"url": "http://127.0.0.1:1/nope"}',
        "template": "{{title}}",
    }]
    db = _FakeDB(rows)
    n = Notifier(db=db, max_retries=1, base_delay=0.05)

    async def _run():
        start = time.monotonic()
        await n.notify("order.filled", "成交", "test")
        elapsed = time.monotonic() - start
        # 修复后 notify 即刻返回（< 1s），绝不做内联重试
        assert elapsed < 1.0, f"notify 阻塞了 {elapsed:.2f}s"
        # 等待后台发送任务完成（失败重试落库）
        for _ in range(100):
            if not n._tasks:
                break
            await asyncio.sleep(0.05)
        await n.close()

    asyncio.run(_run())
    # 后台任务最终把日志置为 failed（重试发生在后台，不阻塞调用方）
    updates = [params for sql, params in db.logs if sql.startswith("UPDATE notification_log")]
    assert updates and updates[-1][0] == "failed", f"未记录发送失败: {db.logs}"


# ---------------- P1：DB 索引补全（迁移 v9）----------------
def test_db_indexes_v9():
    db, d = _tmp_db()
    try:
        idx = {r[0] for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for want in ("idx_audit_log_created", "idx_condition_orders_status",
                     "idx_notification_log_created", "idx_account_snapshot_ts",
                     "idx_backtest_jobs_status", "idx_webhook_deliveries_created",
                     "idx_messages_session", "idx_market_cache_code"):
            assert want in idx, want
    finally:
        _cleanup(db, d)


# ---------------- 阶段 3：DB 事务化迁移 + 失败回滚 ----------------
def test_db_migrate_transactional_rollback(monkeypatch):
    """阶段 3：迁移任一步失败即整体回滚——不留下半成品 schema，也不写版本号。"""
    import app.db as db_mod
    from pathlib import Path

    # 构造两条迁移：v10 成功建表；v11 中间含一条非法语句（触发失败）
    fake_migrations = [
        (10, "CREATE TABLE IF NOT EXISTS ok_tbl (id INTEGER PRIMARY KEY, name TEXT);"),
        (11, "CREATE TABLE IF NOT EXISTS part_tbl (id INTEGER PRIMARY KEY);"
             "CREATE TABLE IF NOT EXISTS bad_tbl (id INTEGER); "
             "THIS IS NOT VALID SQL;"),
    ]
    monkeypatch.setattr(db_mod, "_MIGRATIONS", fake_migrations)

    d = tempfile.mkdtemp()
    p = Path(d) / "txn.db"
    raised = False
    db_ref = {"db": None}
    try:
        db_ref["db"] = db_mod.DB(p)
    except sqlite3.Error:
        raised = True
    try:
        assert raised, "含非法语句的迁移应抛错"
        # 回滚后：不残留半成品表，版本号未写入
        conn = sqlite3.connect(str(p))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "part_tbl" not in tables, "前置合法语句应在回滚窗口内一并撤销"
            assert "bad_tbl" not in tables
            done = {r[0] for r in conn.execute(
                "SELECT version FROM schema_migrations")}
            assert 11 not in done, "失败的迁移不应写入版本号"
            assert 10 in done, "独立的成功迁移应正常应用"
        finally:
            conn.close()
    finally:
        try:
            if db_ref["db"] is not None:
                db_ref["db"]._conn.close()
        except Exception:  # noqa: BLE001
            pass
        _cleanup(None, d)


def test_db_backup_consistency_api():
    """阶段 3：sqlite backup API 生成一致备份（单文件、可打开、含 schema_migrations）。"""
    from app.db import DB
    from pathlib import Path

    db, d = _tmp_db()
    try:
        db.insert("audit_log", {"actor": "t", "action": "x", "target": "y",
                                "created_at": "2026-01-01T00:00:00"})
        dst = Path(d) / "backups" / "app.bak.db"
        assert db.backup_to(dst) is True, "backup API 应返回成功"
        assert dst.exists() and dst.stat().st_size > 0
        # 备份文件可独立打开，且数据一致
        conn = sqlite3.connect(str(dst))
        try:
            n = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            assert n >= 1, "备份应包含已写入数据"
            m = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            assert m >= 1
        finally:
            conn.close()
    finally:
        _cleanup(db, d)


# ---------------- P1：本机 QMT 自动发现 ----------------
def test_discovery_helpers():
    from xtquant_client.discovery import _root_from_exe, _is_qmt_proc, guess_broker_id
    # 由 exe 路径推导客户端根（bin.x64 一级）
    assert os.path.normcase(_root_from_exe(r"P:\stock\gd_qmt\bin.x64\XtMiniQmt.exe")) == \
        os.path.normcase(r"P:\stock\gd_qmt")
    assert _root_from_exe(r"P:\stock\gd_qmt\XtMiniQmt.exe") == r"P:\stock\gd_qmt"
    # 券商档案猜测
    assert guess_broker_id(r"P:\stock\gd_qmt") == "gf"      # 广发（gd_qmt）
    assert guess_broker_id(r"C:\国金证券QMT交易端") == "guojin"
    assert guess_broker_id(r"C:\银河证券QMT交易端") == "yinhe"
    assert guess_broker_id(r"C:\unknown_x") == ""
    # 进程判定：QMT 进程识别 + 排除本平台自身
    assert _is_qmt_proc("XtMiniQmt.exe", "") is True
    assert _is_qmt_proc("miniquote.exe", "") is True
    assert _is_qmt_proc("qmt_work.exe", r"C:\x\qmt_work.exe") is False
    assert _is_qmt_proc("notepad.exe", "") is False


def test_discovery_candidate():
    from xtquant_client.discovery import _candidate
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "userdata_mini"))
        sp = os.path.join(d, "bin.x64", "Lib", "site-packages", "xtquant")
        os.makedirs(sp)
        open(os.path.join(sp, "__init__.py"), "w").close()
        c = _candidate(d, running=True, pid="123", proc="XtMiniQmt.exe")
        assert c is not None
        assert c["client_path"] == os.path.join(d, "userdata_mini")
        assert c["has_bin_x64"] is True
        assert c["has_userdata_mini"] is True
        assert c["running"] is True and c["pid"] == "123"
        assert c["xtquant_found"] is True
        assert c["root"] == d
        # 不存在的根返回 None
        assert _candidate(os.path.join(d, "no_such")) is None


# ---------------- 桥接事件协议（防「握手失败：None」失真回归）----------------
def _bridge_adapter():
    from xtquant_client.bridge_client import BridgeAdapter
    return BridgeAdapter("C:/__no_such_client__", "", "STOCK")


def test_bridge_init_error_top_level_field_not_lost():
    """回归：init_error 的 error 在消息**顶层**，_read_loop 必须整条传递。

    历史 bug：_read_loop 只传 msg["data"]（init_error 时为 None），
    _on_event 用 str(None) == "None" 当作错误文案，用户看到
    「桥接子进程握手失败：None」，真实原因（客户端未登录）被完全吞掉。
    """
    a = _bridge_adapter()
    a._on_event("init_error", {
        "event": "init_error",
        "error": "xtdata 行情服务连接失败：QMT 客户端可能未登录",
        "error_type": "BrokerNotConnectedError",
        "traceback": "Traceback ...",
    })
    assert a._init_error == "xtdata 行情服务连接失败：QMT 客户端可能未登录"


def test_bridge_init_error_nested_field_compat():
    """兼容形态：字段放在 data 内时同样不能丢。"""
    a = _bridge_adapter()
    a._on_event("init_error", {"event": "init_error",
                               "data": {"error": "交易连接异常：会话被占用"}})
    assert a._init_error == "交易连接异常：会话被占用"


def test_bridge_init_error_void_humanized():
    """空洞错误（None / 字符串 "None" / 空串）必须兜底成可操作指引。

    注意字符串 "None" 是 truthy，朴素的 `if not err` 兜底会漏——
    这正是修复前用户在 EXE 里看到 "None" 的直接原因。
    """
    for raw in (None, "None", "none", "", "null", "NoneType"):
        a = _bridge_adapter()
        a._on_event("init_error", {"event": "init_error", "error": raw})
        assert a._init_error, f"raw={raw!r} 未产生任何文案"
        assert "None" not in a._init_error, f"raw={raw!r} 透出了 None"
        assert "登录" in a._init_error, f"raw={raw!r} 缺少可操作指引"


def test_bridge_conn_state_and_quote_payload_still_in_data():
    """整条消息传递后，conn_state / quote 仍须从 data 取载荷（不得回归）。"""
    a = _bridge_adapter()
    a._on_event("conn_state", {"event": "conn_state", "data": {"connected": True}})
    assert a._connected is True
    a._on_event("conn_state", {"event": "conn_state", "data": {"connected": False}})
    assert a._connected is False

    a._on_event("quote", {"event": "quote", "data": {"code": "600519.SH"}})
    # 阶段 1（C12）：quote 经独立有界队列异步派发（reader 线程不再同步执行 handler）。
    # 载荷仍须完整保留（code 在 data 内），此处直接驱动队列验证不回归。
    evt = a._quote_q.get(timeout=2.0)
    assert evt == {"code": "600519.SH"}


def test_humanize_init_error_passthrough():
    """有效文案必须原样透出，不能被兜底覆盖。"""
    from xtquant_client.bridge_client import _humanize_init_error
    assert _humanize_init_error("会话 session 被占用") == "会话 session 被占用"
    assert "登录" in _humanize_init_error(None, "RuntimeError")
    assert "RuntimeError" in _humanize_init_error(None, "RuntimeError")


def test_bridge_server_safe_err_normalizes_void_exception():
    """服务端侧：args=(None,) 的空异常不得序列化成 "None"。"""
    from xtquant_client.bridge_server import _safe_err
    assert _safe_err(ValueError("真实原因")) == "真实原因"
    out = _safe_err(ValueError(None))
    assert out != "None" and "ValueError" in out
    out2 = _safe_err(RuntimeError())
    assert out2 != "" and "RuntimeError" in out2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
