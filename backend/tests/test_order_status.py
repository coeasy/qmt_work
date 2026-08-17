"""阶段 0-A 回归测试：订单状态统一词汇 + xtp 报单回调闭环 + 对账 F11/F12。

不依赖真实 xtquant SDK（CI/托管 venv 均无 xtquant），通过 dict 级回调与 monkeypatch
覆盖：seq→柜台 order_id 映射、-1 显式报错、断线翻转、对账「查无此单」连续轮次保护。
"""
import asyncio
import threading
import time

import pytest

from xtquant_client.base import BrokerSDKError
from xtquant_client.order_status import (
    normalize_order_status, is_active, is_terminal,
    PENDING, PARTIAL, FILLED, CANCELLED, REJECTED, UNKNOWN,
)
from xtquant_client.xtp import XTPQuantAdapter
from gateway.order_watchdog import collect_stale
from gateway.reconcile import OrderReconciler


# ---------------- 统一状态词汇 ----------------
def test_normalize_int_codes():
    assert normalize_order_status(56) == FILLED      # fully_dealt
    assert normalize_order_status(55) == PARTIAL     # part_deal
    assert normalize_order_status(54) == CANCELLED
    assert normalize_order_status(57) == REJECTED
    assert normalize_order_status(48) == PENDING


def test_normalize_raw_strings():
    # 方案 F12 点名漏覆盖的 xtp 实际小写英文
    assert normalize_order_status("fully_dealt") == FILLED
    assert normalize_order_status("part_deal") == PARTIAL
    assert normalize_order_status("unreported") == PENDING
    assert normalize_order_status("已撤单") == CANCELLED
    assert normalize_order_status("废单") == REJECTED


def test_normalize_unknown_explicit():
    # 未知形态显式返回 unknown，绝不静默映射
    assert normalize_order_status("some_garbage_xyz") == UNKNOWN
    assert normalize_order_status(None) == UNKNOWN
    assert normalize_order_status("") == UNKNOWN


def test_is_active_terminal():
    assert is_active("pending") and is_active("partial")
    assert not is_active("filled") and not is_active("cancelled")
    assert is_terminal("filled") and is_terminal("rejected")
    assert not is_terminal("reported")


# ---------------- xtp 报单回调闭环 ----------------
def _make_adapter():
    a = XTPQuantAdapter("C:/qmt/userdata_mini", "123456", "STOCK", 0)
    return a


def test_seq_to_oid_mapping():
    a = _make_adapter()
    a._handle_order_response({"Seq": 1, "OrderID": "X1", "OrderStatus": 56})
    assert a._seq_to_oid[1] == "X1"
    assert a._oid_to_seq["X1"] == 1
    # 已解析则等待立即返回，不阻塞
    assert a._wait_order_response(1) == "X1"


def test_disconnect_flips_state():
    a = _make_adapter()
    a._connected = True
    fired = []
    a.on_disconnect(lambda: fired.append(1))
    a._handle_disconnected()
    assert a._connected is False
    assert fired == [1]


def test_place_order_negative_one_raises():
    a = _make_adapter()

    class FakeTrader:
        def order_stock(self, *args, **kwargs):
            return -1  # 柜台失败

    a._require_trader = lambda: (FakeTrader(), object())
    import xtquant_client.xtp as xtp_mod
    orig = xtp_mod._ensure_xtconstant
    xtp_mod._ensure_xtconstant = lambda: type("X", (), {
        "STOCK_BUY": 0, "STOCK_SELL": 1, "LATEST_PRICE": 5, "FIX_PRICE": 6})()
    try:
        with pytest.raises(BrokerSDKError):
            a.place_order("600000", "buy", "limit", 10.0, 100)
    finally:
        xtp_mod._ensure_xtconstant = orig


def test_place_order_success_uses_counter_oid():
    a = _make_adapter()
    # 预置映射：回调已提前到达，place_order 不应阻塞
    a._seq_to_oid[7] = "O7"

    class FakeTrader:
        def order_stock(self, *args, **kwargs):
            return 7

    a._require_trader = lambda: (FakeTrader(), object())
    import xtquant_client.xtp as xtp_mod
    orig = xtp_mod._ensure_xtconstant
    xtp_mod._ensure_xtconstant = lambda: type("X", (), {
        "STOCK_BUY": 0, "STOCK_SELL": 1, "LATEST_PRICE": 5, "FIX_PRICE": 6})()
    try:
        res = a.place_order("600000", "buy", "limit", 10.0, 100)
    finally:
        xtp_mod._ensure_xtconstant = orig
    assert res["order_id"] == "O7"
    assert res["seq"] == 7
    assert res["status"] == "submitted"


def test_place_order_waits_for_callback():
    a = _make_adapter()

    class FakeTrader:
        def __init__(self, adapter):
            self._adapter = adapter

        def order_stock(self, *args, **kwargs):
            seq = 9
            # 模拟回调在另一线程稍后到达
            threading.Timer(0.05, lambda: self._adapter._handle_order_response(
                {"Seq": 9, "OrderID": "O9", "OrderStatus": 50})).start()
            return seq

    a._require_trader = lambda: (FakeTrader(a), object())
    import xtquant_client.xtp as xtp_mod
    orig = xtp_mod._ensure_xtconstant
    xtp_mod._ensure_xtconstant = lambda: type("X", (), {
        "STOCK_BUY": 0, "STOCK_SELL": 1, "LATEST_PRICE": 5, "FIX_PRICE": 6})()
    try:
        res = a.place_order("600000", "buy", "limit", 10.0, 100)
    finally:
        xtp_mod._ensure_xtconstant = orig
    # 回调到达后拿到真实柜台 order_id
    assert res["order_id"] == "O9"
    assert res["status"] == "submitted"


def test_close_clears_state():
    a = _make_adapter()
    a._trader = object()
    a._acc = object()
    a._seq_to_oid[1] = "X1"
    a._oid_to_seq["X1"] = 1
    a.close()
    assert a._trader is None and a._acc is None
    assert a._seq_to_oid == {} and a._oid_to_seq == {}


# ---------------- watchdog is_active ----------------
def test_collect_stale_uses_active_vocab():
    first_seen = {}
    # xtp 真实返回 "reported"(已报) 与 "part_deal"(部成) 必须判为活跃
    orders = [
        {"order_id": "1", "status": "reported"},
        {"order_id": "2", "status": "part_deal"},
        {"order_id": "3", "status": "fully_dealt"},   # 已成交 → 不超时撤
        {"order_id": "4", "status": "canceled"},       # 已撤 → 不超时撤
    ]
    now = time.time()
    stale = collect_stale(orders, first_seen, now, timeout=1000.0)
    # 超时阈值很大，首轮不应有 stale（first_seen 刚记）
    assert stale == []
    assert set(first_seen) == {"1", "2"}
    # 把时间推过 timeout，活跃单变 stale
    stale2 = collect_stale(orders, first_seen, now + 2000.0, timeout=1000.0)
    assert {o["order_id"] for o in stale2} == {"1", "2"}


# ---------------- reconcile F11/F12 ----------------
class _FakeWal:
    def __init__(self, records):
        self._records = records

    def all_records(self):
        return self._records


class _FakeManager:
    def bridge(self, conn_id=None):
        return None  # 无券商连接 → 券商快照为空


def test_reconcile_missing_not_immediately_stale():
    """F11：查无此单不能立即核销，需连续 missing_rounds 轮。"""
    wal = _FakeWal([{
        "op": "order", "entity": "order", "entity_id": "O1",
        "ts": 0, "payload": {"order_id": "O1", "code": "600000", "volume": 100},
    }])
    r = OrderReconciler(_FakeManager(), wal=wal, db=None)
    r.missing_rounds = 3
    # 第 1、2 轮：券商查不到，但仍跟踪（open），不核销
    s1 = asyncio.run(r.reconcile())
    s2 = asyncio.run(r.reconcile())
    assert s1["open"] == 1 and s2["open"] == 1
    assert s1["stale"] == 0 and s2["stale"] == 0
    # 第 3 轮达到阈值 → 标 stale
    s3 = asyncio.run(r.reconcile())
    assert s3["stale"] == 1


def test_reconcile_condition_entity_included():
    """条件单 entity=condition 也要纳入对账（原代码漏对账）。"""
    wal = _FakeWal([{
        "op": "create", "entity": "condition", "entity_id": "C1",
        "ts": 0, "payload": {"order_id": "C1", "code": "600000", "volume": 100},
    }])
    r = OrderReconciler(_FakeManager(), wal=wal, db=None)
    r.missing_rounds = 1  # 一轮即阈值
    s = asyncio.run(r.reconcile())
    # 至少被纳入待核销集合（checked=1），而非被 entity 过滤丢弃
    assert s["checked"] == 1
