"""闭环通道胶水：日志 → 意图终态 + 事件流。

三个模块单看都对，接不上就等于没做。本文件盯的就是「接上之后」的四件事：

* RESULT 必须把 ``PENDING`` 推进到终态（否则意图永远挂着，下一版计划部署被阻断）；
* 重复回执（日志重读）不得二次推进（幂等）；
* ORDER/TRADE 必须变成事件，且 **RESULT 不能被当成事件** 混进事件流；
* 没有锚点的回执要计数留痕，不能瞎猜归属。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_bigqmt.closed_loop import emit  # noqa: E402
from connectors.bigqmt_closed_loop_channel import ClosedLoopChannel  # noqa: E402
from connectors.bigqmt_log_tail import LogTail  # noqa: E402
from connectors.transports import EmbeddedTransport  # noqa: E402


def _channel():
    transport = EmbeddedTransport("bundle-1")
    return ClosedLoopChannel(transport), transport


def _plan(transport, sid="s1"):
    from connectors.transport import WireRequest
    import asyncio
    resp = asyncio.run(transport.invoke(WireRequest(op="SUBMIT_PLAN", signal_id=sid)))
    assert resp.ok
    return sid


def test_result_closes_intent():
    channel, transport = _channel()
    sid = _plan(transport)
    assert transport.intent(sid).state == "PENDING"

    line = emit("RESULT", {"signal_id": sid, "ok": True, "result": {"executed": 2}},
                seq=1, boot_id="b1")
    channel.consume([LogTail().feed_line(line)])

    intent = transport.intent(sid)
    assert intent.state == "DONE"
    assert intent.result == {"executed": 2}
    assert channel.resolved == 1


def test_failed_result_marks_failed():
    channel, transport = _channel()
    sid = _plan(transport)
    line = emit("RESULT", {"signal_id": sid, "ok": False, "error": "柜台拒单"},
                seq=1, boot_id="b1")
    channel.consume([LogTail().feed_line(line)])
    intent = transport.intent(sid)
    assert intent.state == "FAILED"
    assert "拒单" in intent.error


def test_duplicate_result_is_idempotent():
    channel, transport = _channel()
    sid = _plan(transport)
    line = emit("RESULT", {"signal_id": sid, "ok": True}, seq=1, boot_id="b1")
    rec = LogTail().feed_line(line)
    channel.consume([rec])
    channel.consume([rec])  # 同一批被重放
    assert channel.resolved == 1
    assert transport.intent(sid).state == "DONE"


def test_order_becomes_event_and_result_does_not():
    channel, transport = _channel()
    sid = _plan(transport)
    order = LogTail().feed_line(emit("ORDER", {"order_id": "o1", "order_status": "50"},
                                     seq=1, boot_id="b1"))
    result = LogTail().feed_line(emit("RESULT", {"signal_id": sid, "ok": True},
                                      seq=2, boot_id="b1"))
    events = channel.consume([order, result])
    assert [e.kind for e in events] == ["order"]
    assert transport.intent(sid).state == "DONE"


def test_result_without_anchor_counted():
    channel, transport = _channel()
    rec = LogTail().feed_line(emit("RESULT", {"ok": True}, seq=1, boot_id="b1"))
    channel.consume([rec])
    assert channel.unmatched == 1
    assert channel.resolved == 0


def test_result_for_unknown_intent_does_not_crash():
    channel, transport = _channel()
    rec = LogTail().feed_line(emit("RESULT", {"signal_id": "ghost", "ok": True},
                                   seq=1, boot_id="b1"))
    assert channel.consume([rec]) == []
    assert channel.resolved == 0


def test_on_event_callback():
    channel, transport = _channel()
    seen = []
    channel.on_event = seen.append
    rec = LogTail().feed_line(emit("TRADE", {"traded_id": "t1"}, seq=1, boot_id="b1"))
    channel.consume([rec])
    assert len(seen) == 1 and seen[0].kind == "trade"


def test_poll_reads_file_and_advances_cursor(tmp_path):
    log = tmp_path / "XtClient.log"
    log.write_bytes(b"noise\n")
    channel, transport = _channel()
    channel.log_path = str(log)
    channel.tail = LogTail(str(log))
    sid = _plan(transport)

    with open(log, "ab") as fh:
        fh.write((emit("RESULT", {"signal_id": sid, "ok": True}, seq=1, boot_id="b1")
                  + "\n").encode("utf-8"))
    assert len(channel.poll()) == 0
    assert transport.intent(sid).state == "DONE"
    # 再 poll 一次不应重复推进
    assert channel.poll() == []
    assert channel.resolved == 1


def test_missing_log_is_not_an_error():
    channel, transport = _channel()
    channel.log_path = "Z:/definitely/not/here/XtClient.log"
    channel.tail = LogTail(channel.log_path)
    assert channel.poll() == []


def test_stats_shape():
    channel, transport = _channel()
    sid = _plan(transport)
    channel.consume([channel.tail.feed_line(
        emit("RESULT", {"signal_id": sid, "ok": True}, seq=1, boot_id="b1"))])
    stats = channel.stats
    assert stats["resolved"] == 1
    assert stats["parsed"] == 1
    assert "unmatched_results" in stats
