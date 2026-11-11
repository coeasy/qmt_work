"""巡检引擎（scripts/qmt_iface_check.py）的判据回归。

这里锁的是 2026-11-11 三次定时巡检暴露出来的**判据问题**，不是普通功能测试：

* R1 单次缺失不得称为「漂移」——08:33 曾据此误判 58600 方法面漂移，
     而 10:04/10:24/10:26/11:04 四次采样全部 stable_present。
* R2 客户端重启（pid 变化）必须视为新的授权会话。
* 安全守卫：任何下单/撤单命令必须在发帧前被断言拦下。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import qmt_iface_check as eng  # noqa: E402


# ---------------------------------------------------------------------------
# 安全守卫
# ---------------------------------------------------------------------------
def test_guard_rejects_passorder_before_sending():
    with pytest.raises(RuntimeError):
        eng._guard("callFormula", [eng.REQ_ID, "passorder", b"{}"])


def test_guard_rejects_forbidden_word_anywhere_in_params():
    with pytest.raises(RuntimeError):
        eng._guard("getInstrumentDetail", ["600000.SH", "撤单"])


def test_guard_rejects_method_outside_readonly_whitelist():
    with pytest.raises(RuntimeError):
        eng._guard("order_stock", ["600000.SH", 100])


def test_guard_allows_readonly_pull():
    eng._guard("getFullTick", ["600000.SH"])
    eng._guard("callFormula", [eng.REQ_ID, "gettradedetail", b"{}"])


# ---------------------------------------------------------------------------
# R1：单次缺失 ≠ 漂移
# ---------------------------------------------------------------------------
def test_single_missing_is_transient_not_drift():
    """这正是 08:33 那次误判的形态：历史全 True，只有最近一次 False。"""
    label = eng.classify_presence([True, True, True, True, False])
    assert label == "transient_missing"
    assert eng.drift_claim_allowed(label) is False


def test_all_present_is_stable_present():
    assert eng.classify_presence([True, True, True]) == "stable_present"
    assert eng.drift_claim_allowed("stable_present") is False


def test_all_absent_is_stable_absent_not_drift():
    """getFullTick 在 58600 上四次都 False —— 这是稳定不存在，不是漂移。"""
    label = eng.classify_presence([False, False, False, False])
    assert label == "stable_absent"
    assert eng.drift_claim_allowed(label) is False


def test_only_flapping_allows_drift_claim():
    label = eng.classify_presence([True, False, True])
    assert label == "flapping"
    assert eng.drift_claim_allowed(label) is True


def test_first_sample_is_not_a_stability_claim():
    assert eng.classify_presence([True]) == "first_sample"
    assert eng.classify_presence([]) == "unknown"


# ---------------------------------------------------------------------------
# R1 落地：序列只收「真正采过该方法的轮次」
# ---------------------------------------------------------------------------
def test_stability_series_skips_missions_that_did_not_sample():
    """preopen 不采方法面；它不能以 None 的形式冒充一次「读了但读不到」。"""
    history = [
        {"file": "a.json", "sampled_at": "2026-11-11 10:04:00",
         "surface": {"getMarketData_1d": True}},
        {"file": "b.json", "sampled_at": "2026-11-11 10:24:00",
         "surface": {}},                       # 没采该轴
        {"file": "c.json", "sampled_at": "2026-11-11 10:26:00",
         "surface": {"getMarketData_1d": True}},
    ]
    rep = eng.stability_report(history, {"getMarketData_1d": True})
    rec = rep["methods"]["getMarketData_1d"]
    assert rec["series"] == [True, True, True]
    assert rec["label"] == "stable_present"


def test_stability_report_marks_drift_only_when_flapping():
    history = [
        {"file": "a.json", "sampled_at": "1", "surface": {"getMarketData_1d": True}},
        {"file": "b.json", "sampled_at": "2", "surface": {"getMarketData_1d": False}},
    ]
    rep = eng.stability_report(history, {"getMarketData_1d": True})
    rec = rep["methods"]["getMarketData_1d"]
    assert rec["label"] == "flapping"
    assert rec["drift_claim_allowed"] is True


# ---------------------------------------------------------------------------
# R2：客户端重启 = 新授权会话
# ---------------------------------------------------------------------------
def test_session_boundary_detects_client_restart():
    history = [{"file": "a.json", "sampled_at": "1", "client_pid": 24240}]
    rec = eng.session_boundary(history, 3624)
    assert rec["client_restarted"] is True
    assert rec["previous_pid"] == 24240


def test_session_boundary_same_pid_is_same_session():
    history = [{"file": "a.json", "sampled_at": "1", "client_pid": 3624}]
    rec = eng.session_boundary(history, 3624)
    assert rec["client_restarted"] is False


def test_session_boundary_without_history_is_first_sample():
    rec = eng.session_boundary([], 3624)
    assert rec["state"] == "first_sample"


# ---------------------------------------------------------------------------
# R3：mission 声明覆盖的轴
# ---------------------------------------------------------------------------
def test_missions_cover_distinct_axes():
    pre, mkt, trd = (set(eng.MISSION_AXES[k]) for k in ("preopen", "market", "trade"))
    assert "surface" not in pre, "开盘前不该扫方法面（此时 worker 可能未就绪）"
    assert "surface" in mkt and "bus" in trd
    assert "formula_realtime" in mkt
    assert "listening_inventory" in trd


def test_every_declared_axis_has_a_state_mapping():
    """新增轴必须同步 _axis_state，否则统计会静默变 skipped。"""
    for axis in eng.ALL_AXES:
        assert axis in eng.MISSION_AXES["preopen"] + eng.MISSION_AXES["market"] \
            + eng.MISSION_AXES["trade"], f"轴 {axis} 不属于任何 mission"


def test_axes_union_is_fully_covered():
    union = set()
    for axes in eng.MISSION_AXES.values():
        union |= set(axes)
    assert union == set(eng.ALL_AXES)


# ---------------------------------------------------------------------------
# 异常也必须落盘
# ---------------------------------------------------------------------------
def test_emergency_doc_is_not_silent():
    doc = eng._emergency_doc("market", RuntimeError("boom"))
    assert doc["failed_preconditions"], "探针异常不得静默退出"
    assert doc["error"].startswith("RuntimeError")
    assert doc["verdicts"]["market_can_use_now"]["state"] == "skipped"


def test_out_path_carries_mission():
    p = eng._out_path("trade")
    assert p.name.endswith("_trade.json")
    assert p.parent.name == "output"
