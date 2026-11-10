"""柜台持仓重建：新规下持仓的唯一真相源。

盯的两条（都是「拿空仓当满仓」的同族事故）：

1. **``None`` ≠ ``[]``**：柜台查询失败返回 None 时，绝不能当成「没有持仓」——
   否则调仓会把整仓重新买一遍。
2. **未重建成功不许交易**：``require()`` 必须在未就绪时抛错，而不是返回空 dict。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_bigqmt.position_source import (  # noqa: E402
    CounterUnavailable, PositionSource, normalize_code,
)


def _row(code="600519", market="SH", volume=100, available=None):
    row = {"m_strInstrumentID": code, "m_strExchangeID": market, "m_nVolume": volume}
    if available is not None:
        row["m_nCanUseVolume"] = available
    return row


def _source(rows):
    calls = []

    def query(account_id, account_type, kind):
        calls.append((account_id, account_type, kind))
        if isinstance(rows, Exception):
            raise rows
        return rows

    return PositionSource(query, "8888"), calls


def test_rebuild_from_counter():
    src, calls = _source([_row(volume=300, available=200)])
    snap = src.rebuild()
    assert snap == {"600519.SH": {"volume": 300, "available": 200}}
    assert src.is_ready() is True
    assert calls == [("8888", "STOCK", "POSITION")]


def test_none_is_failure_not_empty():
    src, _ = _source(None)
    with pytest.raises(CounterUnavailable):
        src.rebuild()
    assert src.is_ready() is False
    assert "None" in src.last_error
    with pytest.raises(CounterUnavailable):
        src.require()


def test_query_exception_wrapped():
    src, _ = _source(RuntimeError("柜台未登录"))
    with pytest.raises(CounterUnavailable) as exc:
        src.rebuild()
    assert "柜台未登录" in str(exc.value)
    assert src.is_ready() is False


def test_empty_list_is_legit_flat():
    src, _ = _source([])
    assert src.rebuild() == {}
    assert src.is_ready() is True
    assert src.require() == {}


def test_rows_without_exchange_are_skipped():
    """没有交易所信息时不猜沪/深：造一个不存在的代码比跳过更危险。"""
    src, _ = _source([{"m_strInstrumentID": "600519", "m_nVolume": 100}, _row()])
    snap = src.rebuild()
    assert list(snap) == ["600519.SH"]


def test_zero_and_negative_volume_dropped():
    src, _ = _source([_row(volume=0), _row(code="000001", market="SZ", volume=-5)])
    assert src.rebuild() == {}


def test_available_clamped_to_volume():
    src, _ = _source([_row(volume=100, available=180)])
    assert src.rebuild()["600519.SH"]["available"] == 100


def test_apply_deal_buy_and_sell():
    src, _ = _source([_row(volume=100)])
    src.rebuild()
    src.apply_deal({"m_strInstrumentID": "600519", "m_strExchangeID": "SH",
                    "m_nVolume": 200, "side": "BUY"})
    assert src.volume_of("600519.SH") == 300
    src.apply_deal({"m_strInstrumentID": "600519", "m_strExchangeID": "SH",
                    "m_nVolume": 300, "side": "SELL"})
    assert src.volume_of("600519.SH") == 0
    assert "600519.SH" not in src.snapshot()


def test_apply_deal_ignores_invalid_rows():
    src, _ = _source([_row()])
    src.rebuild()
    assert src.apply_deal({"m_strInstrumentID": "", "m_nVolume": 10}) is None
    assert src.apply_deal({"m_strInstrumentID": "600519", "m_strExchangeID": "SH",
                           "m_nVolume": 0}) is None
    assert src.volume_of("600519.SH") == 100


def test_snapshot_is_a_copy():
    src, _ = _source([_row(volume=100)])
    snap = src.rebuild()
    snap["600519.SH"]["volume"] = 999
    assert src.volume_of("600519.SH") == 100


def test_normalize_code_forms():
    assert normalize_code({"m_strInstrumentID": "600519", "m_strExchangeID": "SH"}) == "600519.SH"
    assert normalize_code({"code": "000001.SZ"}) == "000001.SZ"
    assert normalize_code({"stock_code": "600000", "exchange_id": "sh"}) == "600000.SH"
    assert normalize_code({}) == ""


def test_constructor_guards():
    with pytest.raises(TypeError):
        PositionSource(None, "8888")
    with pytest.raises(ValueError):
        PositionSource(lambda *a: [], "")
