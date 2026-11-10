"""三方对账（意图 × 日志 × 柜台）：**缺一方就如实标缺**，不做「大概率一致」。

为什么不能只对两方（本文件的核心）：
* 只看意图+日志 ⇒ 日志说发了、柜台其实拒了，被当成成交；
* 只看意图+柜台 ⇒ 柜台有一笔同标的同量的单，未必是这一笔（手工单/上一会话）。

★ 最贵的一条：``INTENT_ONLY`` 是 ``UNKNOWN`` 而不是失败。处理顺序永远是
  「先查柜台 → 查到认账 → 查不到才允许重发」，**绝不超时重发**。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.bigqmt_log_tail import AgentRecord  # noqa: E402
from connectors.bigqmt_reconcile import (  # noqa: E402
    STATE_COUNTER_ONLY, STATE_INTENT_ONLY, STATE_LOG_ONLY, STATE_MATCHED,
    STATE_VOLUME_MISMATCH, ReconcileService, extract_cid,
)


def _intent(sid, code="600519.SH", volume=100):
    return {"signal_id": sid, "op": "PLACE", "code": code, "volume": volume}


def _counter(sid, code="600519.SH", volume=100, remark=None):
    return {"code": code, "volume": volume, "remark": remark or ("cid=" + sid)}


def _log(sid, kind="ORDER"):
    return AgentRecord(kind=kind, seq=1, boot_id="b1", ts=1.0,
                       data={"signal_id": sid, "code": "600519.SH"})


def _by_sid(report):
    return {row.signal_id: row for row in report.rows}


def test_extract_cid_priority():
    assert extract_cid({"signal_id": "s1", "remark": "cid=s2"}) == "s1"
    assert extract_cid({"remark": "手工备注 cid=abc_123 尾部"}) == "abc_123"
    assert extract_cid({"remark": "无锚点"}) == ""
    assert extract_cid({}) == ""


def test_three_way_match():
    svc = ReconcileService()
    rep = svc.reconcile([_intent("s1")], [_log("s1")], [_counter("s1")])
    row = _by_sid(rep)["s1"]
    assert row.state == STATE_MATCHED
    assert rep.clean is True
    assert rep.counts[STATE_MATCHED] == 1


def test_counter_without_log_still_matched():
    """日志会轮转/被截断，柜台有单就是有单 —— 按柜台认账，不因日志缺失判失败。"""
    rep = ReconcileService().reconcile([_intent("s1")], [], [_counter("s1")])
    row = _by_sid(rep)["s1"]
    assert row.state == STATE_MATCHED
    assert "日志缺失" in row.detail


def test_log_without_counter_is_log_only():
    rep = ReconcileService().reconcile([_intent("s1")], [_log("s1")], [])
    row = _by_sid(rep)["s1"]
    assert row.state == STATE_LOG_ONLY
    # 不阻断自动部署（可能是被柜台拒了），但要留痕
    assert rep.clean is True


def test_intent_only_is_blocking_unknown():
    rep = ReconcileService().reconcile([_intent("s1")], [], [])
    row = _by_sid(rep)["s1"]
    assert row.state == STATE_INTENT_ONLY
    assert rep.clean is False
    assert rep.unknown_intents == ["s1"]
    assert rep.blocking[0].signal_id == "s1"


def test_volume_mismatch_blocks():
    rep = ReconcileService().reconcile([_intent("s1", volume=100)],
                                       [_log("s1")], [_counter("s1", volume=60)])
    row = _by_sid(rep)["s1"]
    assert row.state == STATE_VOLUME_MISMATCH
    assert row.counter_volume == 60
    assert rep.clean is False


def test_counter_only_reported_not_blocking():
    rep = ReconcileService().reconcile([], [], [_counter("manual-1")])
    row = _by_sid(rep)["manual-1"]
    assert row.state == STATE_COUNTER_ONLY
    assert rep.clean is True


def test_intent_without_anchor_is_listed_not_guessed():
    rep = ReconcileService().reconcile([{"code": "600519.SH", "volume": 100}], [], [])
    assert len(rep.rows) == 1
    assert rep.rows[0].state == STATE_INTENT_ONLY
    assert "无法对账" in rep.rows[0].detail
    assert rep.clean is False


def test_log_record_with_dict_data_is_accepted():
    """日志可能被调用方先转成 dict（落库后回放），两种形态都要认。"""
    rep = ReconcileService().reconcile(
        [_intent("s1")], [{"signal_id": "s1", "code": "600519.SH"}], [_counter("s1")])
    assert _by_sid(rep)["s1"].state == STATE_MATCHED


def test_report_as_dict_is_serializable():
    import json
    rep = ReconcileService().reconcile([_intent("s1")], [], [])
    doc = rep.as_dict()
    assert doc["clean"] is False
    assert doc["unknown_intents"] == ["s1"]
    json.dumps(doc, ensure_ascii=False)  # 不抛即通过


def test_counts_and_input_totals():
    rep = ReconcileService().reconcile(
        [_intent("s1"), _intent("s2")], [_log("s1")], [_counter("s1"), _counter("manual")])
    assert rep.intents == 2
    assert rep.log_records == 1
    assert rep.counter_rows == 2
    assert rep.counts[STATE_MATCHED] == 1
    assert rep.counts[STATE_INTENT_ONLY] == 1
    assert rep.counts[STATE_COUNTER_ONLY] == 1
