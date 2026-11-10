"""P0-1 回归：订单「结果未知」必须是独立状态，不能与拒单混为一谈。

| 场景 | 期望 | 若写错会怎样 |
|------|------|--------------|
| 柜台超时 / 无委托号 | ``intent_unknown``，继续留在未决列表 | 记为 failed → 用户重试 → **重复下单** |
| 柜台明确拒单 | ``intent_failed``，从未决列表消失 | 记为 unknown → 对账反复查一笔不存在的单 |
| 未连接 / 无 SDK | ``intent_failed``（请求根本没发出） | 记为 unknown → 永远待核对 |
| 同 ``client_order_id`` 重试 | 返回原结果，``duplicated=True`` | 再下一单 → 双倍仓位 |
| 不带 key 的人工重复下单 | **允许** | 误拦用户真实意图 |
"""
import asyncio

import gateway.idempotency as idem
import pytest

from gateway.signal_router import SignalRouter
from gateway.wal import (
    INTENT_PENDING_OPS,
    WAL_OP_INTENT,
    WAL_OP_INTENT_FAILED,
    WAL_OP_INTENT_UNKNOWN,
    WAL_OP_RESULT,
    WAL,
)
from xtquant_client.base import BrokerNotConnectedError


@pytest.fixture(autouse=True)
def _clean_idempotency():
    with idem._lock:
        idem._cache.clear()
        idem._inflight.clear()
    yield
    with idem._lock:
        idem._cache.clear()
        idem._inflight.clear()


class _Bridge:
    """可配置返回/抛出的最小 bridge。"""

    def __init__(self, result=None, exc=None):
        self.gateway = self
        self.result = result if result is not None else {"order_id": "OID-1", "ok": True}
        self.exc = exc
        self.calls = 0

    def get_quote(self, code):
        return {"last": 10.0}

    def place_order(self, *args, **kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return dict(self.result)

    async def call(self, fn, *args):
        return fn(*args)

    async def call_locked(self, fn, *args):
        return fn(*args)


class _Mgr:
    def __init__(self, bridge):
        self._bridge = bridge

    def bridge(self, conn_id=None):
        return self._bridge


class _AllowRisk:
    def check_order(self, code, price, volume, side, price_type="limit",
                    require_account=False, **kwargs):
        return True, ""


def _router(bridge, wal):
    r = SignalRouter(_Mgr(bridge), risk=_AllowRisk(), wal=wal)
    r.mode = "live"
    r.threshold = 1e9
    return r


def _ops(wal):
    return [rec.get("op") for rec in wal.all_records()]


# ---------------------------- WAL 语义 --------------------------------------

def test_unknown_stays_unresolved(tmp_path):
    """超时/无委托号 → intent_unknown，且必须继续留在未决列表。"""
    wal = WAL(tmp_path / "wal.jsonl")
    r = _router(_Bridge(result={"order_id": "", "ok": False}), wal)

    out = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert out["status"] == "unknown", out
    assert out["retryable"] is False, "未知结果不可提示用户重试"
    assert WAL_OP_INTENT_UNKNOWN in _ops(wal)
    pending = wal.unresolved_intents()
    assert len(pending) == 1, "未知单必须可被对账发现"
    assert pending[0]["payload"]["code"] == "600519.SH"


def test_rejected_is_resolved(tmp_path):
    """明确拒单 → intent_failed，从未决列表消失。"""
    wal = WAL(tmp_path / "wal.jsonl")
    r = _router(_Bridge(result={"order_id": "", "status": "rejected", "ok": False}), wal)

    out = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert out["status"] == "rejected", out
    assert out["retryable"] is True, "确定拒单允许用户修正后重试"
    assert WAL_OP_INTENT_FAILED in _ops(wal)
    assert wal.unresolved_intents() == []


def test_timeout_exception_is_unknown(tmp_path):
    """超时异常 → unknown（请求可能已到柜台），绝不能记成 failed。"""
    wal = WAL(tmp_path / "wal.jsonl")
    r = _router(_Bridge(exc=TimeoutError("read timeout")), wal)

    out = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert out["status"] == "unknown", out
    assert WAL_OP_INTENT_FAILED not in _ops(wal)
    assert len(wal.unresolved_intents()) == 1


def test_not_connected_is_rejected(tmp_path):
    """未连接券商 = 请求根本没发出，属于确定失败。"""
    wal = WAL(tmp_path / "wal.jsonl")
    r = _router(_Bridge(exc=BrokerNotConnectedError("未连接")), wal)

    out = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert out["status"] == "rejected", out
    assert out["broker_unavailable"] is True
    assert wal.unresolved_intents() == []


def test_order_pairs_unknown(tmp_path):
    """后续拿到 order 记录时，unknown 必须被配对、不再未决。"""
    wal = WAL(tmp_path / "wal.jsonl")
    wal.append(WAL_OP_INTENT, "order", "i-1",
               {"intent_id": "i-1", "client_order_id": "c-1", "code": "600519.SH"})
    wal.append(WAL_OP_INTENT_UNKNOWN, "order", "i-1",
               {"intent_id": "i-1", "client_order_id": "c-1"})
    assert len(wal.unresolved_intents()) == 1

    wal.append(WAL_OP_RESULT, "order", "OID-9", {"intent_id": "i-1"})

    assert wal.unresolved_intents() == []


def test_pending_ops_cover_both(tmp_path):
    assert INTENT_PENDING_OPS == (WAL_OP_INTENT, WAL_OP_INTENT_UNKNOWN)


# ---------------------------- 幂等短路 --------------------------------------

def test_same_key_short_circuits(tmp_path):
    """同一 client_order_id 重试：返回原结果，不再调用柜台。"""
    wal = WAL(tmp_path / "wal.jsonl")
    bridge = _Bridge(result={"order_id": "", "ok": False})
    r = _router(bridge, wal)

    first = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit",
                                 source="manual", client_order_id="c-1"))
    second = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit",
                                  source="manual", client_order_id="c-1"))

    assert first["status"] == "unknown"
    assert second.get("duplicated") is True, second
    assert second["intent_id"] == first["intent_id"]
    assert bridge.calls == 1, "命中未决单时不得再次调用柜台"
    assert len([op for op in _ops(wal) if op == WAL_OP_INTENT]) == 1


def test_different_key_allowed(tmp_path):
    """不同 key 的人工重复下单是真实意图，必须允许。"""
    wal = WAL(tmp_path / "wal.jsonl")
    bridge = _Bridge(result={"order_id": "OID-1", "ok": True})
    r = _router(bridge, wal)

    asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))
    asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit", source="manual"))

    assert bridge.calls == 2, "不带幂等键的人工单不应被拦截"


def test_key_after_success_does_not_block(tmp_path):
    """已成功的 key 不应被当成未决单拦截（历史单不是 pending）。"""
    wal = WAL(tmp_path / "wal.jsonl")
    bridge = _Bridge(result={"order_id": "OID-1", "ok": True})
    r = _router(bridge, wal)

    asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit",
                         source="manual", client_order_id="c-1"))
    again = asyncio.run(r.submit("600519.SH", "buy", 100, 10.0, "limit",
                                 source="manual", client_order_id="c-1"))

    # 已成功的 key 可能命中 single_flight 缓存（duplicated=True）——
    # 关键是**没有再次调用柜台**，而不是标志位本身。
    assert again.get("order_id") == "OID-1"
    assert bridge.calls == 1, "已配对的历史单不能触发第二次下单"
    assert wal.unresolved_intents() == []
