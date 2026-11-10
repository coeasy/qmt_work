"""闭环传输（``embedded``）：**不许假装下单成功**。

新规下后端再也无法把指令实时送进 QMT 沙箱。本 transport 的职责不是「换一种送法」，
而是把语义诚实化：只受理计划类 op，其余一律明确拒绝，并给出可读原因。

盯的不变量：
* 实时 op 必须返回 ``ok=False`` + ``ClosedLoopRealtimeUnsupported``（绝不能返回假成功）；
* 意图终态只允许迁移一次（日志重读/策略重发不得重复推进）；
* 过期 ≠ 失败（``EXPIRED`` 之后必须先查柜台，禁止直接重发）；
* registry 里 ``qmt.big.closed`` 必须能装配出 ``EmbeddedTransport``。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.ports import EventSemantics  # noqa: E402
from connectors.registry import (  # noqa: E402
    available_keys, describe, is_retired, resolve, retirement_notice,
)
from connectors.transport import WireRequest  # noqa: E402
from connectors.transports import EmbeddedTransport  # noqa: E402
from connectors.transports.semantics import max_latency_ms, spec_for  # noqa: E402


def _invoke(transport, op, **params):
    return asyncio.run(transport.invoke(WireRequest(op=op, params=params)))


def test_requires_bundle_id():
    with pytest.raises(ValueError):
        EmbeddedTransport("")


def test_plan_op_accepted_as_pending():
    tr = EmbeddedTransport("bundle-20261102")
    resp = _invoke(tr, "SUBMIT_PLAN", plan={"id": "p1"})
    assert resp.ok is True
    assert resp.result["state"] == "PENDING"
    assert resp.result["plan_id"] == resp.signal_id
    assert resp.meta["pending"] is True
    assert tr.pending() == (resp.signal_id,)


def test_realtime_op_rejected_with_reason():
    tr = EmbeddedTransport("bundle-1")
    resp = _invoke(tr, "PLACE_ORDER", code="600519.SH", volume=100)
    assert resp.ok is False
    assert resp.error_type == "ClosedLoopRealtimeUnsupported"
    # 错误信息必须能让用户看懂「为什么不行」，而不是一句「失败」
    assert "闭环" in resp.error
    assert "127.0.0.1" in resp.error
    assert tr.stats["rejected"] == 1
    assert tr.pending() == ()


def test_realtime_supported_flag_is_false():
    """上层据此提前禁用「实时买入」，而不是等超时。"""
    assert EmbeddedTransport("b").realtime_supported is False
    assert EmbeddedTransport("b").supports_callback is False


def test_resolve_is_idempotent():
    tr = EmbeddedTransport("b")
    resp = _invoke(tr, "SUBMIT_PLAN")
    sid = resp.signal_id
    assert tr.resolve(sid, ok=True, result={"executed": 2}) is True
    assert tr.resolve(sid, ok=False, error="late replay") is False
    intent = tr.intent(sid)
    assert intent.state == "DONE"
    assert intent.result == {"executed": 2}


def test_resolve_unknown_signal_returns_false():
    tr = EmbeddedTransport("b")
    assert tr.resolve("nope", ok=True) is False


def test_expire_marks_stale_but_not_failed():
    now = [1000.0]
    tr = EmbeddedTransport("b", clock=lambda: now[0])
    resp = _invoke(tr, "SUBMIT_PLAN")
    now[0] = 1000.0 + 601
    expired = tr.expire(600)
    assert expired == [resp.signal_id]
    assert tr.intent(resp.signal_id).state == "EXPIRED"
    assert tr.stats["EXPIRED"] == 1
    assert tr.stats["FAILED"] == 0


def test_close_rejects_further_plans():
    tr = EmbeddedTransport("b")
    asyncio.run(tr.close())
    resp = _invoke(tr, "SUBMIT_PLAN")
    assert resp.ok is False
    assert resp.error_type == "TransportClosed"
    assert tr.is_alive() is False


def test_snapshot_and_stats_shape():
    tr = EmbeddedTransport("b")
    sid = _invoke(tr, "SUBMIT_PLAN").signal_id
    tr.resolve(sid, ok=True)
    snap = tr.snapshot()
    assert snap and snap[0]["signal_id"] == sid and snap[0]["state"] == "DONE"
    stats = tr.stats
    assert stats["submitted"] == 1 and stats["DONE"] == 1


# ---------------------------------------------------------------------------
# registry 装配
# ---------------------------------------------------------------------------
def test_closed_profile_is_registered():
    assert "qmt.big.closed" in available_keys()
    profile = describe("qmt.big.closed")
    assert profile == {"dialect": "bigqmt.v1", "transport": "embedded"}


def test_resolve_closed_profile_builds_embedded():
    conn = resolve("qmt.big.closed", bundle_id="bundle-x")
    assert isinstance(conn.transport, EmbeddedTransport)
    assert conn.transport.bundle_id == "bundle-x"
    assert conn.descriptor.metadata["transport"] == "embedded"


def test_resolve_closed_requires_bundle_id():
    with pytest.raises(ValueError) as exc:
        resolve("qmt.big.closed")
    assert "bundle_id" in str(exc.value)


def test_retired_profiles_stay_resolvable_but_flagged():
    """老通道保留可用（本地回放/过渡期），但必须能被查询到「为什么不该用」。"""
    assert is_retired("qmt.big.bridge.file") is True
    assert "Forbidden FileIO" in retirement_notice("qmt.big.bridge.file")
    assert is_retired("qmt.big.closed") is False
    assert retirement_notice("qmt.big.closed") == ""


def test_embedded_event_semantics_is_poll_diff():
    spec = spec_for("embedded")
    assert spec.semantics == EventSemantics.POLL_DIFF
    # 上界要覆盖「日志落盘 + 采集间隔」，窄了会误撤未回报的单
    assert max_latency_ms("embedded") == 5000
