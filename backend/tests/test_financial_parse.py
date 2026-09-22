"""券商财务数据（``get_financial``）解析朝向的契约测试。

为什么需要它
------------
实测（2026-09-19，本机 xtquant）：``get_stock_financial`` 返回的 DataFrame
**行是指标、列是报告期**（与「行=报告期」的直觉相反）。原实现一律取
``frame.iloc[-1]`` 当「最新一期」，于是把**最后一个指标名**当成了报告期：

    GET /api/v1/market/analysis?code=600519.SH
    → "valuation": {"report_time": "PARENT_NET", "eps": null, "bps": null,
                    "roe": null, "pe": null, "pb": null}

``PARENT_NET`` 就是 ``PARENT_NETPROFIT`` 被 ``[:10]`` 截断的结果。估值维度因此
**永远 unavailable**，界面上「基本面 / 估值」永久空白 —— 但错误原因是「解析错了」
而不是「没有数据」，用户完全无从判断。

锁定四条不变量：

1. **报告期绝不能是指标名**（``report_time`` 必须像日期，不能命中 ``fields``）；
2. 转置朝向与常规朝向都必须解析出 ``EPS`` / ``BPS`` / ``ROE``；
3. 旧 SDK 的 dict 形态继续可用；
4. 真的没有数据 / 接口缺失时给出**可操作**的文案，不伪造数值、不静默吞错。
"""
from __future__ import annotations

import pandas as pd
import pytest

from xtquant_client.base import BrokerNotConnectedError
from xtquant_client.xtp.instrument import InstrumentMixin

_CODE = "600519.SH"
_FIELDS = ["EPS", "BPS", "OPERATE_INCOME", "TOTAL_OPERATE_INCOME",
           "PARENT_NETPROFIT", "TOTAL_OPERATE_EXPENSE", "ROE", "CAPITAL",
           "TOTAL_OPERATE_INCOME_YOY", "PARENT_NETPROFIT_YOY"]


class _Adapter(InstrumentMixin):
    """最小适配器：只喂 ``_xtdata``，其余能力不参与本测试。"""

    def __init__(self, frame=None, *, exc=None, methods=("get_stock_financial",)):
        self._frame = frame
        self._exc = exc
        self._methods = set(methods)

        outer = self

        class _Xtd:
            def __getattr__(self, name):  # noqa: ANN001
                if name not in outer._methods:
                    raise AttributeError(name)

                def _call(*_a, **_kw):
                    if outer._exc is not None:
                        raise outer._exc
                    return {_CODE: outer._frame}

                return _call

        self._xtdata = _Xtd()


def _transposed_frame():
    """行=指标，列=报告期（实测形态）。"""
    return pd.DataFrame(
        {"2024-12-31": [2.0] * len(_FIELDS), "2025-03-31": [1.0] * len(_FIELDS)},
        index=_FIELDS,
    )


def _normal_frame():
    """行=报告期，列=指标（直觉形态，必须继续兼容）。"""
    return pd.DataFrame(
        [{f: 1.5 for f in _FIELDS}, {f: 2.5 for f in _FIELDS}],
        index=["2024-12-31", "2025-03-31"],
    )


def test_transposed_frame_is_parsed_by_column():
    """★ 实测形态：取最后一个**报告期列**，而不是最后一行。"""
    out = _Adapter(_transposed_frame()).get_financial(_CODE)
    assert out["report_time"] == "2025-03-31"
    assert out["EPS"] == 1.0
    assert out["BPS"] == 1.0
    assert out["ROE"] == 1.0


def test_report_time_is_never_a_field_name():
    """回归锁定：报告期不能命中指标名（原先返回被截断的 ``PARENT_NET``）。"""
    out = _Adapter(_transposed_frame()).get_financial(_CODE)
    assert out["report_time"] not in _FIELDS
    assert out["report_time"] != "PARENT_NET"
    assert not any(out["report_time"] == f[:10] for f in _FIELDS)


def test_normal_frame_still_parsed_by_row():
    """常规朝向不得被改坏（取最后一行）。"""
    out = _Adapter(_normal_frame()).get_financial(_CODE)
    assert out["report_time"] == "2025-03-31"
    assert out["EPS"] == 2.5


def test_dict_frame_takes_last_report_period():
    """旧 SDK ``get_financial_data`` 的 {报告期: {指标: 值}} 形态。"""
    frame = {"2024-12-31": {"EPS": 1.0}, "2025-03-31": {"EPS": 3.0}}
    out = _Adapter(frame, methods=("get_financial_data",)).get_financial(_CODE)
    assert out["report_time"] == "2025-03-31"
    assert out["EPS"] == 3.0


def test_empty_frame_reports_actionable_detail():
    out = _Adapter(pd.DataFrame()).get_financial(_CODE)
    assert out["code"] == _CODE
    assert "无财务数据" in out["detail"]


def test_missing_interface_gives_actionable_message():
    """券商终端没有财务接口时，文案必须能指导用户动作（不能只说「失败」）。"""
    adapter = _Adapter(_transposed_frame(), methods=())
    with pytest.raises(BrokerNotConnectedError) as ei:
        adapter.get_financial(_CODE)
    msg = str(ei.value)
    assert "get_stock_financial" in msg and "升级" in msg


def test_sdk_exception_is_wrapped_with_cause():
    """SDK 抛错要带原始原因，不能变成光秃秃的「失败」。"""
    adapter = _Adapter(_transposed_frame(), exc=RuntimeError("权限不足"))
    with pytest.raises(BrokerNotConnectedError) as ei:
        adapter.get_financial(_CODE)
    assert "权限不足" in str(ei.value)


def test_nonnumeric_values_do_not_crash():
    """指标值是 None 时留 None，不抛异常（不能让整页 500）。"""
    frame = pd.DataFrame({"2025-03-31": [None] * len(_FIELDS)}, index=_FIELDS)
    out = _Adapter(frame).get_financial(_CODE)
    assert out["EPS"] is None
    assert out["report_time"] == "2025-03-31"


# =====================================================================
# 2026-09-21 复发（A9）：真实形态是 {代码: {指标: 表}}，不是 {报告期: {指标: 值}}
# =====================================================================
#
# 实测：本机 QMT **只有旧接口 `get_financial_data`**（无 `get_stock_financial`），
# 它返回的**不是** DataFrame，而是：
#
#     {"600519.SH": {"EPS": DataFrame(0,0), "BPS": DataFrame(0,0), ...共 10 个指标}}
#
# 即**键是指标名、每个指标各自一张表**。旧实现把 `items[-1]` 当「最新一期」，
# 于是报告期取到最后一个指标名被截断的结果，10 个指标全 null：
#
#     GET /api/v1/reference/financial?code=600519.SH
#     → {"report_time": "PARENT_NET", "EPS": null, "BPS": null, ...}
#
# 而 `PARENT_NET` = `PARENT_NETPROFIT_YOY`[:10] —— 界面「报告期」栏就会显示这串
# 莫名其妙的字母，用户完全无从判断这是解析错了还是真没数据。
#
# 注意上面 `test_*_frame_*` 三条只覆盖 DataFrame 与「{报告期:{指标:值}}」两种形态，
# **都没覆盖这一种** ⇒ 这就是它能在修复之后再次复发的空隙。本组测试补上这个空隙。


def _field_keyed(tables: dict) -> dict:
    """构造实测形态。

    ⚠️ 只返回**内层** ``{指标: 表/标量}``：``_Adapter`` 自己会把它包成
    ``{代码: frame}``（与真实 SDK 一致）。这里再包一层就会多出一级，
    让 frame 变成 ``{代码: {...}}`` ⇒ 键集与指标名不相交 ⇒ 走错分支。
    """
    return dict(tables)


def test_field_keyed_dict_with_empty_tables_reports_no_data():
    """★ 实测形态 + 本机真实返回（10 张 0×0 空表）。

    改动前的输出（就是缺陷现场）：
        {"report_time": "PARENT_NET", "EPS": null, "BPS": null, ... 共 10 个 null}
    改动后必须是**可操作文案**：`report_time` 不再出现（无从得知，不编造），
    且给出去哪里解决的指引。
    """
    empty = pd.DataFrame()
    out = _Adapter(_field_keyed({f: empty for f in _FIELDS})).get_financial(_CODE)
    assert out["code"] == _CODE
    # ① 不再透出「看起来像值、实际是字段名」的伪报告期
    assert not out.get("report_time")
    assert out.get("report_time") != "PARENT_NET"
    assert not any(out.get("report_time") == f[:10] for f in _FIELDS)
    # ② 不再返回「一个乱码报告期 + 10 个 null」这种无从判断的载荷
    assert not any(f in out for f in _FIELDS)
    # ③ 文案必须能指导动作
    assert "无财务数据" in out["detail"]
    assert "QMT" in out["detail"] and "权限" in out["detail"]


def test_field_keyed_dict_with_dated_index_is_parsed():
    """指标表的**索引**是报告期（非空表）⇒ 取最新一期与日期。"""
    tables = {
        "EPS": pd.DataFrame({"v": [2.0, 3.5]}, index=["2024-12-31", "2025-03-31"]),
        "BPS": pd.DataFrame({"v": [10.0, 11.0]}, index=["2024-12-31", "2025-03-31"]),
        "ROE": pd.DataFrame({"v": [5.0, 6.0]}, index=["2024-12-31", "2025-03-31"]),
    }
    out = _Adapter(_field_keyed(tables)).get_financial(_CODE)
    assert out["report_time"] == "2025-03-31"
    assert out["EPS"] == 3.5
    assert out["BPS"] == 11.0
    assert out["ROE"] == 6.0


def test_field_keyed_dict_with_dated_columns_is_parsed():
    """指标表的**列**是报告期（另一种版本朝向）也必须解析出来。"""
    tables = {
        "EPS": pd.DataFrame([[2.0, 3.5]], index=["v"], columns=["2024-12-31", "2025-03-31"]),
        "BPS": pd.DataFrame([[10.0, 11.0]], index=["v"], columns=["2024-12-31", "2025-03-31"]),
    }
    out = _Adapter(_field_keyed(tables)).get_financial(_CODE)
    assert out["report_time"] == "2025-03-31"
    assert out["EPS"] == 3.5
    assert out["BPS"] == 11.0


def test_field_keyed_dict_with_scalars_is_parsed():
    """最朴素的形态：``{指标: 标量}``。报告期无从得知 ⇒ 留空，绝不编造。"""
    out = _Adapter(_field_keyed({"EPS": 1.23, "BPS": 8.0})).get_financial(_CODE)
    assert out["EPS"] == 1.23
    assert out["BPS"] == 8.0
    assert out["report_time"] == ""


def test_nan_never_leaks_as_json_nan():
    """NaN 必须转 None。

    ``round(float("nan"), 4)`` 得到 ``nan``，json 编码成裸 ``NaN`` 是**非法 JSON**，
    浏览器 ``JSON.parse`` 直接抛错 ⇒ 整页数据全废。此前 ``float(v)`` 不抛异常，
    所以这条 NaN 从 ``except`` 旁边溜了过去。
    """
    frame = pd.DataFrame({"2025-03-31": [float("nan")] * len(_FIELDS)}, index=_FIELDS)
    out = _Adapter(frame).get_financial(_CODE)
    assert out["EPS"] is None
    assert all(out[f] is None for f in _FIELDS)


def test_report_time_rejects_non_date_labels():
    """报告期只认真日期：``2025-13-45`` 这类假日期一律留空。"""
    from xtquant_client.xtp.instrument import _as_report_date
    assert _as_report_date("2025-03-31") == "2025-03-31"
    assert _as_report_date("20250331") == "2025-03-31"
    assert _as_report_date("PARENT_NETPROFIT_YOY") == ""
    assert _as_report_date("PARENT_NET") == ""
    assert _as_report_date("2025-13-45") == ""
    assert _as_report_date(None) == ""
    assert _as_report_date("") == ""
