"""Phase F (F2)：OrderReconciler 单元测试 —— 启动对账 / 差异检测 / WAL 核销。

覆盖面（对应 V10 方案 Phase F DoD）：
1. WAL 无待核销 → 空 summary，不触碰券商；
2. 券商返回终态（fully_dealt → filled）→ 核销 + WAL 写 ``reconciled`` 记录，
   且该单从待核销集合中消失；
3. 数量不符（traded != want）→ ``mismatched`` 计数 + detail.mismatch；
4. 仍挂单（pending/partial）→ 不核销，记 open；
5. 查无此单：连续 N 轮内不核销（防瞬时断连误销活单），到轮数后按跨日/成交判 stale；
6. 跨日委托（券商当日委托表已清）→ 直接 stale。

WAL 用真实文件实现（tmp_path），券商侧用 Fake 桥（零真实连接）。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

from gateway.reconcile import OrderReconciler
from gateway.wal import WAL


# ---------------- Fakes ----------------
class FakeGateway:
    def __init__(self, orders, deals):
        self._orders = orders
        self._deals = deals

    def get_orders(self):
        return self._orders

    def get_deals(self):
        return self._deals


class FakeBridge:
    def __init__(self, gateway):
        self.gateway = gateway

    async def call_locked(self, fn):
        return fn()


class FakeManager:
    def __init__(self, bridge):
        self._b = bridge

    def bridge(self, conn_id=None):
        return self._b


def make_reconciler(tmp_path, orders=None, deals=None):
    wal = WAL(tmp_path / "wal.jsonl")
    events = []
    rec = OrderReconciler(FakeManager(FakeBridge(FakeGateway(orders or [], deals or []))),
                          wal=wal, on_event=events.append)
    return rec, wal, events


def seed_order(wal, oid="1001", volume=100, ts=None):
    payload = {"code": "600519.SH", "side": "buy", "volume": volume}
    if ts is not None:
        payload["ts"] = ts
    wal.append("order", "order", oid, payload)


# ---------------- 1. 空集合 ----------------
def test_no_pending_touches_nothing(tmp_path):
    rec, wal, events = make_reconciler(tmp_path)
    out = asyncio.run(rec.reconcile())
    assert out == {"checked": 0, "note": "无待核销委托"}
    assert events == []


# ---------------- 2. 终态核销 + WAL 写回 ----------------
def test_filled_order_written_off(tmp_path):
    rec, wal, events = make_reconciler(
        tmp_path, orders=[{"order_id": "1001", "status": "fully_dealt"}],
        deals=[{"order_id": "1001", "volume": 100}])
    seed_order(wal, "1001", volume=100)
    out = asyncio.run(rec.reconcile())
    assert out["checked"] == 1
    assert out["filled"] == 1
    assert out["mismatched"] == 0
    assert out["details"][0]["status"] == "filled"
    # WAL 核销记录已写回
    recs = wal.all_records()
    assert any(r.get("op") == "reconciled" and str(r.get("entity_id")) == "1001"
               for r in recs)
    # 核销后不再出现在待核销集合
    assert rec._pending_from_wal() == {}
    # WS 事件已发出
    assert events and events[0]["type"] == "reconcile"


# ---------------- 3. 数量差异 ----------------
def test_volume_mismatch_detected(tmp_path):
    rec, wal, _ = make_reconciler(
        tmp_path, orders=[{"order_id": "1002", "status": "fully_dealt"}],
        deals=[{"order_id": "1002", "volume": 60}])
    seed_order(wal, "1002", volume=100)
    out = asyncio.run(rec.reconcile())
    assert out["filled"] == 1
    assert out["mismatched"] == 1
    assert out["details"][0]["mismatch"] is True
    assert out["details"][0]["traded"] == 60


# ---------------- 4. 仍挂单不核销 ----------------
def test_active_order_stays_open(tmp_path):
    rec, wal, _ = make_reconciler(
        tmp_path, orders=[{"order_id": "1003", "status": "reported"}])
    seed_order(wal, "1003", volume=100)
    out = asyncio.run(rec.reconcile())
    assert out["open"] == 1
    assert out["details"] == []          # 未核销，无 detail
    assert rec._pending_from_wal()       # 仍在待核销集合
    assert not any(r.get("op") == "reconciled" for r in wal.all_records())


# ---------------- 5. 查无此单：轮数保护 ----------------
def test_missing_order_rounds_guard(tmp_path):
    rec, wal, _ = make_reconciler(tmp_path, orders=[])  # 券商查不到
    rec.missing_rounds = 2
    seed_order(wal, "1004", volume=100)
    out1 = asyncio.run(rec.reconcile())
    assert out1["open"] == 1, "第 1 轮查无此单不应核销"
    out2 = asyncio.run(rec.reconcile())
    assert out2["stale"] == 1, "第 2 轮（达到 missing_rounds）应核销为 stale"
    assert any(r.get("op") == "reconciled" for r in wal.all_records())


# ---------------- 6. 跨日直接 stale ----------------
def test_cross_day_order_stale(tmp_path):
    rec, wal, _ = make_reconciler(tmp_path, orders=[])
    rec.missing_rounds = 1
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    seed_order(wal, "1005", volume=100, ts=yesterday)
    out = asyncio.run(rec.reconcile())
    assert out["stale"] == 1


# ---------------- 7. WAL 时间戳回退 ----------------
def test_missing_rounds_use_wal_ts_fallback(tmp_path):
    """payload 无 ts 时用 wal_ts 判定跨日（真实场景：旧版本 WAL 记录）。"""
    rec, wal, _ = make_reconciler(tmp_path, orders=[])
    rec.missing_rounds = 1
    seed_order(wal, "1006", volume=100,
               ts=time.time() - 86400 - 60)  # 1 天前（unix 浮点）
    out = asyncio.run(rec.reconcile())
    assert out["stale"] == 1
