"""G2-2 指标 REST 端点测试（目录 + 服务端计算 + G3 auto-safe 签名）。

fetch_kline_cached 打桩（测试密闭、零网络）；路由返回 ok/err 信封 {code,message,data}。
注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import asyncio
import inspect

import pytest

from app.routes.indicators import market_indicators, market_indicators_calc

_KLINE = [{"time": f"2026082{i}", "open": 10 + i, "high": 12 + i,
           "low": 9 + i, "close": 11 + i, "volume": 1000} for i in range(1, 40)]


@pytest.fixture()
def fake_kline(monkeypatch):
    async def _fake(code, period="1d", count=250, broker_id=None, force=False,
                    source="auto", adjust=None):
        return {"bars": _KLINE, "source": "eltdx", "cached_at": "2026-08-30T10:00:00"}
    monkeypatch.setattr("tools.fetch_kline_cached", _fake)


def test_list_indicators():
    res = asyncio.run(market_indicators())
    assert res["code"] == 0
    data = res["data"]
    assert data["count"] == 16
    names = {i["name"] for i in data["items"]}
    assert names == {"ma", "ema", "macd", "kdj", "rsi", "boll", "wr",
                     "atr", "adx", "cci", "obv", "volume_ma",
                     "returns", "log_returns", "zscore", "roc"}


def test_calc_kdj(fake_kline):
    res = asyncio.run(market_indicators_calc(name="kdj", code="600519.SH", n=9))
    assert res["code"] == 0
    d = res["data"]
    assert d["name"] == "kdj"
    assert d["params"] == {"n": 9}
    assert d["count"] == len(_KLINE)
    assert d["source"] == "eltdx"
    assert set(d["outputs"]) == {"k", "d", "j"}
    assert d["outputs"]["k"][0] is None          # 窗口不足 → null
    assert d["meta"]["category"] == "momentum"


def test_calc_win_maps_to_period(fake_kline):
    """win 路由参数 → ma/ema/rsi 的 period 指标参数。"""
    res = asyncio.run(market_indicators_calc(name="rsi", code="600519.SH", win=14))
    assert res["code"] == 0
    assert res["data"]["params"] == {"period": 14}


def test_calc_boll_with_n_m(fake_kline):
    res = asyncio.run(market_indicators_calc(name="boll", code="600519.SH", n=20, m=1.5))
    assert res["code"] == 0
    assert res["data"]["params"] == {"n": 20, "m": 1.5}
    assert set(res["data"]["outputs"]) == {"upper", "mid", "lower"}


def test_calc_unknown_name():
    res = asyncio.run(market_indicators_calc(name="nope", code="600519.SH"))
    assert res["code"] == 400


def test_calc_missing_code():
    res = asyncio.run(market_indicators_calc(name="ma"))
    assert res["code"] == 400


def test_calc_param_below_min(fake_kline):
    res = asyncio.run(market_indicators_calc(name="kdj", code="600519.SH", n=0))
    assert res["code"] == 400


def test_calc_source_empty(monkeypatch):
    async def _empty(code, period="1d", count=250, broker_id=None, force=False,
                     source="auto", adjust=None):
        return {"bars": [], "source": None, "cached_at": None}
    monkeypatch.setattr("tools.fetch_kline_cached", _empty)
    res = asyncio.run(market_indicators_calc(name="ma", code="600519.SH"))
    assert res["code"] == 503


# ---- G3 auto-safe 签名门禁 ------------------------------------------------
def test_calc_signature_auto_safe():
    """签名无 **kwargs / Request：capabilities 才会标记 auto_safe 并自动暴露 MCP。"""
    sig = inspect.signature(market_indicators_calc)
    for p in sig.parameters.values():
        assert p.kind not in (inspect.Parameter.VAR_POSITIONAL,
                              inspect.Parameter.VAR_KEYWORD), p.name
