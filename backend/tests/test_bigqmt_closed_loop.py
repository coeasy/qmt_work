"""策略内闭环：计划解析 / 时间窗 / 风控闸门 / 意图生成 / 回传行。

这是「外部指挥 → 内部自治」的语义转换点，也是最容易出静默错误的地方：

* 计划非法必须**响亮失败**（按半个计划下单 = 拿真钱试错）；
* 权重合计 > 1 必须拒绝（否则是隐性加杠杆）；
* 风控闸门裁剪要**留原因**（否则用户只看到「少下了几笔」，查不出为什么）；
* ``emit()`` 与 ``LogTail`` 是跨进程契约，必须能原样往返。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_bigqmt.closed_loop import (  # noqa: E402
    PREFIX, Guard, PlanError, decide, emit, hhmm_of, in_window, parse_plan,
)
from connectors.bigqmt_log_tail import LogTail  # noqa: E402

PLAN = {
    "id": "p-20261102-01",
    "strat": "etf_rot",
    "window": ["09:35", "14:50"],
    "rule": {"type": "rebalance",
             "target": [{"code": "513100.SH", "w": 0.5}, {"code": "511990.SH", "w": 0.5}]},
    "guard": {"max_orders": 4, "max_turnover": 0.3, "stop_dd": 0.06},
}


def test_parse_plan_ok():
    plan = parse_plan(PLAN)
    assert plan["id"] == "p-20261102-01"
    assert plan["window"] == ["09:35", "14:50"]
    assert plan["rule"]["target"][0]["code"] == "513100.SH"
    assert plan["guard"]["max_orders"] == 4


def test_parse_plan_defaults():
    plan = parse_plan({"id": "p", "strat": "s", "rule": {"target": [{"code": "A", "w": 0.1}]}})
    assert plan["window"] == ["09:30", "14:57"]
    assert plan["guard"]["max_orders"] == 8
    assert plan["guard"]["max_turnover"] == 0.5


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p.pop("id"), "id"),
    (lambda p: p.pop("strat"), "strat"),
    (lambda p: p.update(window=["09:35"]), "window"),
    (lambda p: p.update(window=["15:00", "09:00"]), "开始晚于结束"),
    (lambda p: p.update(window=["25:00", "26:00"]), "时间格式"),
    (lambda p: p.update(rule={"type": "rebalance", "target": []}), "target"),
    (lambda p: p.update(rule={"type": "magic", "target": [{"code": "A", "w": 0.1}]}), "rule.type"),
    (lambda p: p.update(rule={"type": "rebalance", "target": [{"code": "", "w": 0.1}]}), "code"),
    (lambda p: p.update(rule={"type": "rebalance",
                              "target": [{"code": "A", "w": 0.8}, {"code": "B", "w": 0.5}]}), "权重合计"),
    (lambda p: p.update(rule={"type": "rebalance", "target": [{"code": "A", "w": -0.1}]}), "越界"),
])
def test_parse_plan_rejects_bad_input(mutate, needle):
    raw = json.loads(json.dumps(PLAN))
    mutate(raw)
    with pytest.raises(PlanError) as exc:
        parse_plan(raw)
    assert needle in str(exc.value)


def test_in_window_boundaries():
    plan = parse_plan(PLAN)
    assert in_window(plan, "09:35") is True
    assert in_window(plan, "14:50") is True
    assert in_window(plan, "09:34") is False
    assert in_window(plan, "14:51") is False


def test_hhmm_of():
    import time
    stamp = time.mktime((2026, 11, 2, 9, 5, 0, 0, 0, -1))
    assert hhmm_of(stamp) == "09:05"


def test_guard_max_orders_keeps_reason():
    guard = Guard({"max_orders": 2, "max_turnover": 1.0}, nav=100000)
    intents = [{"code": c, "value": 1000} for c in ("A", "B", "C")]
    allowed, dropped = guard.filter(intents)
    assert [i["code"] for i in allowed] == ["A", "B"]
    assert dropped == [{"code": "C", "reason": "max_orders"}]
    assert guard.orders_sent == 2


def test_guard_max_turnover():
    guard = Guard({"max_orders": 10, "max_turnover": 0.05}, nav=100000)
    allowed, dropped = guard.filter([{"code": "A", "value": 3000},
                                     {"code": "B", "value": 3000}])
    assert [i["code"] for i in allowed] == ["A"]
    assert "max_turnover" in dropped[0]["reason"]


def test_guard_stop_dd_halts_and_blocks():
    guard = Guard({"max_orders": 10, "max_turnover": 1.0, "stop_dd": 0.05}, nav=100000)
    assert guard.mark_nav(100000) is False
    assert guard.mark_nav(94000) is True  # -6% >= 5%
    allowed, dropped = guard.filter([{"code": "A", "value": 1000}])
    assert allowed == []
    assert dropped[0]["reason"].startswith("halted")


def test_guard_reset_day():
    guard = Guard({"max_orders": 1, "max_turnover": 1.0}, nav=100000)
    guard.filter([{"code": "A", "value": 1000}])
    assert guard.orders_sent == 1
    guard.reset_day(nav=100000)
    assert guard.orders_sent == 0 and guard.turnover == 0.0 and not guard.halted


def test_decide_buys_and_sells_with_lot_rounding():
    plan = parse_plan(PLAN)
    positions = {"513100.SH": {"volume": 200000}}   # 800k > 目标 500k ⇒ 应卖出
    prices = {"513100.SH": 4.0, "511990.SH": 100.0}
    intents = decide(plan, positions, nav=1_000_000, prices=prices)
    by_code = {i["code"]: i for i in intents}
    assert by_code["513100.SH"]["side"] == "SELL"
    assert by_code["513100.SH"]["volume"] == 75000  # (800k-500k)/4
    assert by_code["513100.SH"]["volume"] % 100 == 0
    assert by_code["511990.SH"]["side"] == "BUY"
    assert by_code["511990.SH"]["volume"] == 5000  # 500000/100
    # 大额优先
    assert intents[0]["value"] >= intents[-1]["value"]


def test_decide_skips_missing_price():
    """没价格就用 0 价算 ⇒ 股数无穷大。必须跳过，而不是下天量单。"""
    plan = parse_plan(PLAN)
    intents = decide(plan, {}, nav=1_000_000, prices={"513100.SH": 4.0})
    assert [i["code"] for i in intents] == ["513100.SH"]


def test_decide_requires_nav():
    with pytest.raises(PlanError):
        decide(parse_plan(PLAN), {}, nav=0, prices={"513100.SH": 4.0})


def test_emit_roundtrips_through_log_tail():
    """跨进程契约：策略打印的行，后端解析器必须原样读回。"""
    line = emit("ORDER", {"signal_id": "s1", "code": "600519.SH"}, seq=7, boot_id="b9")
    assert line.startswith(PREFIX)
    rec = LogTail().feed_line(line)
    assert rec is not None
    assert (rec.kind, rec.seq, rec.boot_id) == ("ORDER", 7, "b9")
    assert rec.data["signal_id"] == "s1"


def test_emit_truncates_oversize_but_stays_valid_json():
    line = emit("RESULT", {"pad": "x" * 20000}, seq=1, boot_id="b")
    assert len(line) <= 8192
    payload = json.loads(line[len(PREFIX):])
    assert payload["d"]["truncated"] is True
    assert LogTail().feed_line(line) is not None
