"""P0-1 回归：未决 intent 必须进入对账，并能被柜台委托认领。

之前 ``OrderReconciler`` 只读 ``order/submit/create``，``intent`` 被完全跳过——
崩溃窗口内的委托永远不会被发现，启动日志却显示「无待核销委托」（假阴性）。
"""
import asyncio

from gateway.reconcile import OrderReconciler
from gateway.wal import WAL_OP_INTENT, WAL_OP_INTENT_FAILED, WAL_OP_INTENT_UNKNOWN, WAL


class _ReconBridge:
    def __init__(self, orders=None, deals=None):
        self.gateway = self
        self._orders = orders or []
        self._deals = deals or []

    def get_orders(self):
        return list(self._orders)

    def get_deals(self):
        return list(self._deals)

    async def call_locked(self, fn, *args):
        return fn(*args)


class _Mgr:
    def __init__(self, bridge):
        self._bridge = bridge

    def bridge(self, conn_id=None):
        return self._bridge


def _reconciler(tmp_path, orders=None):
    wal = WAL(tmp_path / "wal.jsonl")
    r = OrderReconciler(_Mgr(_ReconBridge(orders=orders)), wal=wal)
    r.missing_rounds = 3
    return r, wal


def _write_intent(wal, iid="i-1", cid="c-1", code="600519.SH"):
    wal.append(WAL_OP_INTENT, "order", iid, {
        "intent_id": iid, "client_order_id": cid, "code": code,
        "side": "buy", "volume": 100, "price": 10.0,
    })


def test_pending_includes_intent(tmp_path):
    """仅写 intent 的 WAL 也必须出现在待核销集合里。"""
    r, wal = _reconciler(tmp_path)
    _write_intent(wal)

    pending = r._pending_from_wal()

    assert "intent:i-1" in pending, pending
    assert pending["intent:i-1"]["kind"] == "intent"


def test_pending_excludes_paired_intent(tmp_path):
    """已有 order 配对的 intent 不再待核销。"""
    r, wal = _reconciler(tmp_path)
    _write_intent(wal)
    wal.append("order", "order", "OID-1", {"intent_id": "i-1"})

    pending = r._pending_from_wal()
    # order 本身仍待核销（直到 reconciled），但 intent 条目必须消失；
    # 否则同一笔委托会被核对两次。
    assert "intent:i-1" not in pending
    assert "OID-1" in pending


def test_reconcile_claims_by_client_id(tmp_path):
    """柜台委托带 client_order_id → 认领成功，补写 order 配对。"""
    r, wal = _reconciler(tmp_path, orders=[{
        "order_id": "OID-1", "client_order_id": "c-1",
        "status": "filled", "volume": 100, "traded_volume": 100,
    }])
    _write_intent(wal)

    out = asyncio.run(r.reconcile())

    assert out["checked"] == 1, out
    assert wal.unresolved_intents() == [], "认领后不应再是未决"
    ops = [rec.get("op") for rec in wal.all_records()]
    assert "order" in ops


def test_reconcile_marks_failed_after_rounds(tmp_path):
    """连续 3 轮查不到 → intent_failed，不能永远悬着。"""
    r, wal = _reconciler(tmp_path)
    _write_intent(wal)

    first = asyncio.run(r.reconcile())
    second = asyncio.run(r.reconcile())
    third = asyncio.run(r.reconcile())

    assert first["open"] == 1, first
    assert second["open"] == 1, second
    assert third["unknown"] == 1, third
    ops = [rec.get("op") for rec in wal.all_records()]
    assert WAL_OP_INTENT_FAILED in ops
    assert wal.unresolved_intents() == []


def test_unknown_intent_also_reconciled(tmp_path):
    """intent_unknown 与 intent 一样进入对账（不能只处理初始 intent）。"""
    r, wal = _reconciler(tmp_path)
    _write_intent(wal)
    wal.append(WAL_OP_INTENT_UNKNOWN, "order", "i-1",
               {"intent_id": "i-1", "client_order_id": "c-1"})

    pending = r._pending_from_wal()

    assert "intent:i-1" in pending
    assert pending["intent:i-1"]["kind"] == "intent"
