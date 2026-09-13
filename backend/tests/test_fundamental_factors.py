"""基本面因子字段级溯源（D-J §J.6 / F11）：字段不可得写 None + missing_codes，绝不填 0。

同源一致性：一次请求只用单一源供给全部字段。
"""
import asyncio
from app.screener.fundamentals import fetch_fundamentals, FUNDAMENTAL_FIELDS
from _phase4_support import FakeManager, FakePlugin, REG_ALL, force_deps


def _run(coro):
    return asyncio.run(coro)


def test_field_level_values_and_provenance():
    mgr = FakeManager(REG_ALL, {"akshare": FakePlugin({"pe": 12.5, "pb": 1.3, "roe": 0.15})})
    with force_deps():
        res = _run(fetch_fundamentals(["600000.SH", "000001.SZ"],
                                     policy_str="auto",
                                     fields=["pe", "pb", "roe"], hub=mgr))
    assert res["fields"]["pe"]["600000.SH"] == 12.5
    assert res["fields"]["pb"]["000001.SZ"] == 1.3
    assert res["provenance"]["pe"] == "akshare"
    # 同源一致性：各字段 provenance 一致
    assert res["provenance"]["pb"] == res["provenance"]["pe"]


def test_missing_field_never_zero():
    # 源只提供 pe，pb/roe 缺失 → None + 入 missing_codes，绝不填 0
    mgr = FakeManager(REG_ALL, {"akshare": FakePlugin({"pe": 9.0})})
    with force_deps():
        res = _run(fetch_fundamentals(["600000.SH"], fields=["pe", "pb", "roe"], hub=mgr))
    assert res["fields"]["pe"]["600000.SH"] == 9.0
    assert res["fields"]["pb"].get("600000.SH") is None
    assert "600000.SH" in res["missing_codes"]["pb"]
    # 确保没有任何字段被填成 0
    for fld, vals in res["fields"].items():
        for code, v in vals.items():
            assert v != 0 or fld in res["missing_codes"], f"{fld} 不应填 0"


def test_empty_codes_no_error():
    mgr = FakeManager(REG_ALL, {"akshare": FakePlugin({"pe": 1.0})})
    with force_deps():
        res = _run(fetch_fundamentals([], fields=["pe"], hub=mgr))
    assert res["fields"]["pe"] == {}


def test_fundamental_fields_constant():
    assert "dividend_yield" in FUNDAMENTAL_FIELDS
    assert "mktcap" in FUNDAMENTAL_FIELDS
