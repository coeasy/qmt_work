"""tools.factors 与 /factors 路由的单测（无需券商连接）。"""
import math

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.factors import router
from tools import factors as F


# 最小应用：仅挂载 factors 路由，保持测试快速隔离
app = FastAPI()
app.include_router(router)
client = TestClient(app)


# ---------------- 工具层单测 ----------------

def test_list_factors_nonempty():
    fs = F.list_factors()
    assert isinstance(fs, list) and len(fs) >= 10
    names = {f["name"] for f in fs}
    for expect in ("sma", "ema", "rsi", "macd", "bollinger", "atr", "adx",
                   "cci", "kdj", "obv", "volume_ma", "returns", "log_returns",
                   "zscore", "roc"):
        assert expect in names


def test_unknown_factor_raises():
    with pytest.raises(ValueError):
        F.compute_factor("not_a_factor", [1, 2, 3])


def test_sma_known_series():
    # 1..5 简单移动平均，period=2 -> [None, 1.5, 2.5, 3.5, 4.5]
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = F.compute_factor("sma", series, period=2)
    assert out[0] is None
    assert out[1] == pytest.approx(1.5)
    assert out[4] == pytest.approx(4.5)
    # 长度对齐
    assert len(out) == len(series)


def test_ema_known_series():
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = F.compute_factor("ema", series, period=2)
    # 首值等于输入（adjust=False 下 ewm 第一点 = x0）
    assert out[0] == pytest.approx(1.0)
    # adjust=False 下 span=2 的 EWM，末值 = 365/81
    assert out[-1] == pytest.approx(4.5061728395, abs=1e-6)
    assert len(out) == len(series)


def test_rsi_monotonic_up_is_high():
    # 单调递增序列 RSI 应接近 100（无下跌）
    series = [float(i) for i in range(1, 31)]
    out = F.compute_factor("rsi", series, period=14)
    assert out[-1] is not None
    assert out[-1] == pytest.approx(100.0, abs=1e-6)


def test_macd_shape_and_alignment():
    series = [float(100 + i + (i % 3)) for i in range(60)]
    out = F.compute_factor("macd", series)
    assert set(out.keys()) == {"macd", "signal", "hist"}
    assert len(out["macd"]) == len(series)


def test_bollinger_bands_ordering():
    series = [float(100 + 2 * math.sin(i / 3.0)) for i in range(40)]
    out = F.compute_factor("bollinger", series, period=20, num_std=2.0)
    # 在有效区，上轨 >= 中轨 >= 下轨
    mid = out["mid"][-1]
    up = out["upper"][-1]
    lo = out["lower"][-1]
    assert up >= mid >= lo


def test_atr_requires_high_low():
    # 缺少 high/low 应抛 ValueError
    with pytest.raises(ValueError):
        F.compute_factor("atr", [1.0, 2.0, 3.0])


def test_atr_computes():
    close = [10.0, 10.5, 10.2, 10.8, 11.0]
    high = [10.2, 10.8, 10.5, 11.0, 11.3]
    low = [9.8, 10.3, 10.0, 10.5, 10.7]
    out = F.compute_factor("atr", close, high=high, low=low, period=3)
    assert len(out) == len(close)
    # ATR 非负
    assert all((v is None or v >= 0) for v in out)


def test_compute_many():
    series = [float(i) for i in range(1, 31)]
    out = F.compute_many(["sma", "ema", "rsi"], series)
    assert set(out.keys()) == {"sma", "ema", "rsi"}
    assert len(out["sma"]) == len(series)


def test_from_kline_helper():
    kline = [{"close": 1.0}, {"close": 2.0}, {"close": 3.0}, {"high": 5.0, "low": 1.0, "close": 4.0}]
    assert F.from_kline(kline) == [1.0, 2.0, 3.0, 4.0]
    assert F.from_kline(kline, "high") == [None, None, None, 5.0]


# ---------------- 路由层单测 ----------------

def test_get_factors_endpoint():
    r = client.get("/factors")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    names = {f["name"] for f in body["data"]}
    assert "sma" in names and "rsi" in names


def test_compute_endpoint_ok():
    r = client.post("/factors/compute",
                    json={"name": "sma", "values": [1, 2, 3, 4, 5], "params": {"period": 2}})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["name"] == "sma"
    assert body["data"]["values"][1] == pytest.approx(1.5)


def test_compute_endpoint_unknown():
    r = client.post("/factors/compute",
                    json={"name": "nope", "values": [1, 2, 3]})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 400


def test_compute_many_endpoint():
    r = client.post("/factors/compute/many",
                    json={"names": ["sma", "ema"], "values": [1, 2, 3, 4, 5]})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert "sma" in data and "ema" in data


def test_from_kline_no_broker_returns_503():
    # 未连接券商，fetch_kline_cached 会抛 BrokerNotConnectedError -> 503
    r = client.post("/factors/from-kline",
                    json={"symbol": "600519.SH", "names": ["sma"]})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 503
