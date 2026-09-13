"""V9 总纲落地回归测试（2026-09-09 实施批）。

覆盖：
1. 执行统一收口：ExecutionService 强制风控（risk=None 拒绝）；
2. BatchExecutionService：N × OrderIntent 全部经 SignalRouter 统一链路；
3. local_bars 多源隔离（迁移 v23）：provider_id 入主键不互相覆盖，读取按质量选主；
4. Dataset Snapshot 质量 Gate：发布即校验、违例降级 provisional、日历/复权版本落库。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import asyncio
import json

import pytest

from gateway.execution import ExecutionService


# ---- 1. 强制风控（V9 §7 Mandatory Risk） ------------------------------------

class _Risk:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = 0

    def check_order(self, code, price, volume, direction, price_type="limit"):
        self.calls += 1
        return self.allowed, "blocked" if not self.allowed else ""


class _Gateway:
    def __init__(self, quote=None):
        self.quote = quote
        self.orders = []

    def get_quote(self, code):
        return self.quote

    def place_order(self, *args):
        self.orders.append(args)
        return {"code": 0, "order_id": "O1", "status": "submitted"}


class _Bridge:
    def __init__(self, quote=None):
        self.gateway = _Gateway(quote)

    async def call(self, fn, *args):
        return fn(*args)

    async def call_locked(self, fn, *args):
        return fn(*args)


def test_execution_without_risk_is_rejected():
    """风控未初始化时必须拒绝，绝不放行无风控裸单。"""
    async def run():
        bridge = _Bridge({"last": 10.0})
        res = await ExecutionService(risk=None).place_order(
            bridge, "600519.SH", "buy", 100, 10.0, "limit")
        assert res["ok"] is False
        assert "风控" in res["reason"]
        assert bridge.gateway.orders == []

    asyncio.run(run())


# ---- 2. BatchExecutionService（V9 §6.2） ------------------------------------

class _SpyRouter:
    """记录 submit 调用的假统一入口（模拟 SignalRouter.submit 契约）。"""

    def __init__(self, ok=True, mode="live"):
        self.ok = ok
        self.mode = mode
        self.submits = []

    async def submit(self, code, side, volume, price=0.0, price_type="limit",
                     source="", broker_id="", remark="", idempotency_key="",
                     auto_confirm=False, payload=None):
        self.submits.append({"code": code, "side": side, "volume": volume,
                             "source": source, "broker_id": broker_id,
                             "idempotency_key": idempotency_key,
                             "auto_confirm": auto_confirm, "price_type": price_type})
        if not self.ok:
            return {"ok": False, "reason": "risk blocked", "mode": self.mode}
        return {"ok": True, "mode": self.mode, "order_id": f"OID-{len(self.submits)}"}


def test_batch_routes_every_order_through_signal_router():
    from gateway.batch_execution import BatchExecutionService
    router = _SpyRouter()
    svc = BatchExecutionService(router)
    orders = [
        {"conn_id": "c1", "code": "600519.SH", "direction": "buy", "volume": 100,
         "price": 10.0, "price_type": "limit"},
        {"conn_id": "c2", "code": "000001.SZ", "direction": "sell", "volume": 200,
         "price": 0, "price_type": "market"},
    ]
    summary = asyncio.run(svc.submit_batch(orders, batch_id="b42"))
    assert summary["total"] == 2 and summary["ok"] == 2
    assert len(router.submits) == 2
    for s in router.submits:
        assert s["source"] == "batch"
        assert s["auto_confirm"] is True          # 与引擎单一致跳过 TOTP 挂起
        assert s["idempotency_key"].startswith("batch:b42:")
    assert summary["results"][0]["order_id"] == "OID-1"
    assert summary["results"][0]["mode"] == "live"


def test_batch_rejects_invalid_intent_without_router_call():
    from gateway.batch_execution import BatchExecutionService
    router = _SpyRouter()
    svc = BatchExecutionService(router)
    summary = asyncio.run(svc.submit_batch(
        [{"conn_id": "c1", "code": "", "direction": "buy", "volume": 0}], batch_id="b1"))
    assert summary["ok"] == 0
    assert router.submits == []
    assert "参数非法" in summary["results"][0]["detail"]


def test_batch_surfaces_router_rejection():
    from gateway.batch_execution import BatchExecutionService
    router = _SpyRouter(ok=False)
    svc = BatchExecutionService(router)
    summary = asyncio.run(svc.submit_batch(
        [{"conn_id": "c1", "code": "600519.SH", "direction": "buy", "volume": 100,
          "price": 10.0}], batch_id="b1"))
    assert summary["ok"] == 0
    assert "risk blocked" in summary["results"][0]["detail"]


# ---- 3. local_bars 多源隔离（迁移 v23） --------------------------------------

@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    db = DB(tmp_path / "test_v23.db")
    from datasource.local_store import LocalStore
    yield LocalStore(db)
    db._conn.close()


def _bar(t, close=10.0):
    from datasource.models import Bar
    return Bar(time=t, open=close, high=close + 1, low=close - 1,
               close=close, volume=100, amount=close * 100)


def test_multi_source_bars_do_not_overwrite(store):
    """两个 provider 写同一 (code,period,adjust,dt) → 都保留，不互相覆盖。"""
    store.upsert_bars("600519.SH", [_bar("20260901", 10.0)], provider_id="src_a")
    store.upsert_bars("600519.SH", [_bar("20260901", 11.0)], provider_id="src_b")
    rows = store._db.query(
        "SELECT provider_id, close FROM local_bars WHERE code='600519.SH'")
    assert {(r["provider_id"], r["close"]) for r in rows} == {("src_a", 10.0), ("src_b", 11.0)}
    # 读取按质量选主：同质量时具名 provider 优先，每 dt 只出一行
    bars = store.get_bars("600519.SH")
    assert len(bars) == 1
    # 覆盖度按交易日计（DISTINCT dt），不因多源膨胀
    assert store.count_bars("600519.SH") == 1


def test_same_source_resync_overwrites_own_rows(store):
    """同源重同步 → REPLACE 自己的行（幂等），不产生重复。"""
    store.upsert_bars("600519.SH", [_bar("20260901", 10.0)], provider_id="src_a")
    store.upsert_bars("600519.SH", [_bar("20260901", 10.5)], provider_id="src_a")
    rows = store._db.query(
        "SELECT close FROM local_bars WHERE code='600519.SH' AND provider_id='src_a'")
    assert len(rows) == 1 and rows[0]["close"] == 10.5


def test_quality_priority_selects_validated_row(store):
    """质量选主：validated 优先于 unknown，即使 provider 名排序靠后。"""
    store.upsert_bars("600519.SH", [_bar("20260901", 10.0)], provider_id="src_a",
                      quality_state="unknown")
    store.upsert_bars("600519.SH", [_bar("20260901", 12.0)], provider_id="src_z",
                      quality_state="validated")
    bars = store.get_bars("600519.SH")
    assert len(bars) == 1 and bars[0].close == 12.0


# ---- 4. Dataset Snapshot 质量 Gate（V9 §11 / §10.4） -------------------------

@pytest.fixture()
def snap_db(tmp_path):
    from core.db import DB
    db = DB(tmp_path / "test_snap.db")
    yield db
    db._conn.close()


def test_snapshot_quality_gate_downgrades_bad_rows(snap_db):
    from datasource.snapshots import DatasetSnapshotStore
    rows = [
        {"code": "600519.SH", "dt": "20260901", "open": 10, "high": 11, "low": 9,
         "close": 10.5, "volume": 100, "amount": 1050},
        {"code": "600519.SH", "dt": "20260902", "open": 10, "high": 5, "low": 9,
         "close": 10, "volume": -3, "amount": 0},   # high<low + 负 volume
    ]
    snap = DatasetSnapshotStore(snap_db).publish(
        "d1", "v1", "p1", "b1", rows, quality_state="complete",
        calendar_version="cal-1", adjustment_version="qfq")
    # 违例不允许伪装 complete → 降级 provisional，问题清单入 manifest
    assert snap["quality_state"] == "provisional"
    assert snap["calendar_version"] == "cal-1"
    assert snap["adjustment_version"] == "qfq"
    manifest = json.loads(snap["manifest_json"])
    assert manifest["quality_issue_count"] >= 2
    assert any("high<low" in i for i in manifest["quality_issues"])


def test_snapshot_clean_rows_keep_complete(snap_db):
    from datasource.snapshots import DatasetSnapshotStore
    rows = [{"code": "600519.SH", "dt": "20260901", "open": 10, "high": 11,
             "low": 9, "close": 10.5, "volume": 100, "amount": 1050}]
    snap = DatasetSnapshotStore(snap_db).publish(
        "d1", "v1", "p1", "b1", rows, quality_state="complete")
    assert snap["quality_state"] == "complete"
    # 修复 P0-7（原为恒真断言 `... if False else True`）：
    # 干净行发布 complete 时，manifest 不应携带 quality_issues（或为空列表）
    manifest = json.loads(snap["manifest_json"])
    assert not manifest.get("quality_issues"), manifest.get("quality_issues")


def test_quality_issues_rejects_require_quality(snap_db):
    """provisional 快照必须被 require_quality 拒绝（研究/回测消费门禁）。"""
    from datasource.snapshots import DatasetSnapshotStore, require_quality
    rows = [{"code": "x", "dt": "20260901", "open": 10, "high": 5, "low": 9,
             "close": 10, "volume": 0, "amount": 0}]
    snap = DatasetSnapshotStore(snap_db).publish("d", "v", "p", "b", rows,
                                                 quality_state="complete")
    with pytest.raises(ValueError):
        require_quality(snap)
