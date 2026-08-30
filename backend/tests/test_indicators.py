"""G2-1 指标引擎契约测试（内置指标 vs 前端 JS 语义逐位一致）。

核心：**参考镜像**——把 `MarketData.jsx:87-182` 的 7 个 JS 函数逐行翻译为纯
Python 参考实现，断言向量化实现与参考镜像在确定性数据上逐点一致（null 位置 +
数值容差 1e-9）。这是「前后端同一指标同参数结果完全一致」的契约锚点。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import random

import numpy as np
import pytest

from app.indicators import builtin, calc, get_indicator, list_indicators


# ============================ 参考镜像（JS 逐行翻译） =========================
def _ref_ma(closes, period):
    out = []
    s = 0.0
    for i in range(len(closes)):
        s += closes[i]
        if i >= period:
            s -= closes[i - period]
        out.append(None if i < period - 1 else s / period)
    return out


def _ref_ema(series, period):
    out = []
    k = 2 / (period + 1)
    prev = None
    for x in series:
        if x is None:
            out.append(None)
            continue
        if prev is None:
            prev = x
            out.append(x)
            continue
        prev = x * k + prev * (1 - k)
        out.append(prev)
    return out


def _ref_macd(closes):
    e12 = _ref_ema(closes, 12)
    e26 = _ref_ema(closes, 26)
    dif = [a - b if (a is not None and b is not None) else None
           for a, b in zip(e12, e26)]
    dea_raw = _ref_ema([v for v in dif if v is not None], 9)
    dea = []
    idx = 0
    for v in dif:
        if v is None:
            dea.append(None)
            continue
        dea.append(dea_raw[idx])
        idx += 1
    bar = [(d - e) * 2 if (d is not None and e is not None) else None
           for d, e in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "bar": bar}


def _ref_kdj(highs, lows, closes, n=9):
    k_arr, d_arr, j_arr = [], [], []
    k = d = 50.0
    for i in range(len(closes)):
        if i < n - 1:
            k_arr.append(None); d_arr.append(None); j_arr.append(None)
            continue
        hn = max(highs[i - n + 1:i + 1])
        ln = min(lows[i - n + 1:i + 1])
        rsv = 50 if hn == ln else (closes[i] - ln) / (hn - ln) * 100
        k = (2 / 3) * k + (1 / 3) * rsv
        d = (2 / 3) * d + (1 / 3) * k
        k_arr.append(k); d_arr.append(d); j_arr.append(3 * k - 2 * d)
    return {"k": k_arr, "d": d_arr, "j": j_arr}


def _ref_rsi(closes, period=14):
    out = []
    avg_g = avg_l = 0.0
    for i in range(len(closes)):
        if i <= period:
            out.append(None)
            continue
        if i == period + 1:
            g = l = 0.0
            for j in range(1, period + 1):
                d = closes[j] - closes[j - 1]
                if d > 0:
                    g += d
                else:
                    l -= d
            avg_g = g / period
            avg_l = l / period
        else:
            d = closes[i] - closes[i - 1]
            avg_g = (avg_g * (period - 1) + (d if d > 0 else 0)) / period
            avg_l = (avg_l * (period - 1) + (-d if d < 0 else 0)) / period
        out.append(100 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    return out


def _ref_boll(closes, n=20, m=2):
    mid = _ref_ma(closes, n)
    std = []
    for i in range(len(closes)):
        if mid[i] is None:
            std.append(None)
            continue
        s = 0.0
        c = 0
        for j in range(i - n + 1, i + 1):
            if closes[j] is not None:
                s += (closes[j] - mid[i]) ** 2
                c += 1
        std.append((s / c) ** 0.5 * m if c > 0 else None)
    upper = [v + st if (v is not None and st is not None) else None
             for v, st in zip(mid, std)]
    lower = [v - st if (v is not None and st is not None) else None
             for v, st in zip(mid, std)]
    return {"upper": upper, "mid": mid, "lower": lower}


def _ref_wr(highs, lows, closes, n=14):
    out = []
    for i in range(len(closes)):
        if i < n - 1:
            out.append(None)
            continue
        hn = max(highs[i - n + 1:i + 1])
        ln = min(lows[i - n + 1:i + 1])
        out.append(50 if hn == ln else (hn - closes[i]) / (hn - ln) * (-100))
    return out


# ============================ 数据与断言工具 ================================
@pytest.fixture()
def prices():
    rng = random.Random(42)
    closes = [100.0 + i * 0.5 + rng.uniform(-3, 3) for i in range(120)]
    highs = [c + rng.uniform(0, 2) for c in closes]
    lows = [c - rng.uniform(0, 2) for c in closes]
    return {"closes": closes, "highs": highs, "lows": lows}


def _assert_equal(builtin_arr, ref_arr):
    """逐点一致：None 位置完全对齐 + 数值容差 1e-9。"""
    assert len(builtin_arr) == len(ref_arr), (len(builtin_arr), len(ref_arr))
    b = np.asarray([np.nan if v is None else float(v) for v in builtin_arr])
    r = np.asarray([np.nan if v is None else float(v) for v in ref_arr])
    assert np.allclose(b, r, equal_nan=True, rtol=1e-9, atol=1e-9)


def _bars(d):
    return [{"open": h, "high": h, "low": l, "close": c, "volume": 1000}
            for h, l, c in zip(d["highs"], d["lows"], d["closes"])]


# ============================ 已知值锁定 ====================================
def test_known_ma():
    out = builtin.ma([1, 2, 3, 4, 5], 3)
    assert np.isnan(out[:2]).all()                  # 窗口不足 → NaN（契约层转 null）
    assert out[2] == pytest.approx(2.0)
    assert out[4] == pytest.approx(4.0)


def test_known_ema_seed():
    out = builtin.ema([1, 2, 3, 4], 3)
    assert out[0] == pytest.approx(1.0)         # 首值播种
    assert out[1] == pytest.approx(2 * 0.5 + 1 * 0.5)   # k=2/(3+1)=0.5
    assert out[3] == pytest.approx(3.125)


def test_known_rsi_all_up_is_100():
    up = [100 + i for i in range(30)]
    out = builtin.rsi(up, 14)
    assert all(v == pytest.approx(100.0) for v in out[16:])  # avgL==0 → 100


def test_known_wr_scale():
    # hn=10, ln=0, close=2.5 → (10-2.5)/10*(-100) = -75
    assert builtin.wr([10, 10], [0, 0], [1, 2.5], 2)[1] == pytest.approx(-75.0)


# ============================ 参考镜像契约（默认参数） =======================
def test_ma_matches_reference(prices):
    _assert_equal(builtin.ma(prices["closes"], 20), _ref_ma(prices["closes"], 20))


def test_ema_matches_reference(prices):
    _assert_equal(builtin.ema(prices["closes"], 12), _ref_ema(prices["closes"], 12))


def test_macd_matches_reference(prices):
    b = builtin.macd(prices["closes"])
    r = _ref_macd(prices["closes"])
    for key in ("dif", "dea", "bar"):
        _assert_equal(b[key], r[key])


def test_kdj_matches_reference(prices):
    b = builtin.kdj(prices["highs"], prices["lows"], prices["closes"], 9)
    r = _ref_kdj(prices["highs"], prices["lows"], prices["closes"], 9)
    for key in ("k", "d", "j"):
        _assert_equal(b[key], r[key])


def test_rsi_matches_reference(prices):
    _assert_equal(builtin.rsi(prices["closes"], 14), _ref_rsi(prices["closes"], 14))


def test_boll_matches_reference(prices):
    b = builtin.boll(prices["closes"], 20, 2.0)
    r = _ref_boll(prices["closes"], 20, 2.0)
    for key in ("upper", "mid", "lower"):
        _assert_equal(b[key], r[key])


def test_wr_matches_reference(prices):
    _assert_equal(builtin.wr(prices["highs"], prices["lows"], prices["closes"], 14),
                  _ref_wr(prices["highs"], prices["lows"], prices["closes"], 14))


# ============================ 自定义参数契约 ================================
def test_custom_params_match_reference(prices):
    _assert_equal(builtin.ma(prices["closes"], 5), _ref_ma(prices["closes"], 5))
    _assert_equal(builtin.ema(prices["closes"], 30), _ref_ema(prices["closes"], 30))
    _assert_equal(builtin.rsi(prices["closes"], 6), _ref_rsi(prices["closes"], 6))
    _assert_equal(builtin.wr(prices["highs"], prices["lows"], prices["closes"], 10),
                  _ref_wr(prices["highs"], prices["lows"], prices["closes"], 10))
    b = builtin.kdj(prices["highs"], prices["lows"], prices["closes"], 5)
    r = _ref_kdj(prices["highs"], prices["lows"], prices["closes"], 5)
    for key in ("k", "d", "j"):
        _assert_equal(b[key], r[key])
    b = builtin.boll(prices["closes"], 10, 1.5)
    r = _ref_boll(prices["closes"], 10, 1.5)
    for key in ("upper", "mid", "lower"):
        _assert_equal(b[key], r[key])


# ============================ 注册表与调度 ==================================
def test_registry_has_sixteen_indicators():
    inds = list_indicators()
    assert len(inds) == 16
    assert {i["name"] for i in inds} == {
        "ma", "ema", "macd", "kdj", "rsi", "boll", "wr",
        "atr", "adx", "cci", "obv", "volume_ma",
        "returns", "log_returns", "zscore", "roc",
    }
    for i in inds:
        assert {"name", "label", "category", "params", "outputs", "inputs",
                "formula"} <= set(i)


def test_calc_dispatch_dict_bars(prices):
    res = calc("kdj", _bars(prices), n=9)
    assert res["name"] == "kdj"
    assert res["params"] == {"n": 9}
    assert set(res["outputs"]) == {"k", "d", "j"}
    assert res["outputs"]["k"][0] is None      # 窗口不足 → null
    assert res["outputs"]["k"][8] is not None


def test_calc_dispatch_bar_models(prices):
    from app.datasource.models import Bar
    bars = [Bar(time=f"2026082{i % 9 + 1}", open=h, high=h, low=l, close=c, volume=1000)
            for i, (h, l, c) in enumerate(zip(prices["highs"], prices["lows"],
                                              prices["closes"]))]
    res = calc("rsi", bars, period=14)
    assert res["name"] == "rsi"
    assert res["params"] == {"period": 14}
    assert res["outputs"]["rsi"][0] is None


def test_calc_default_params(prices):
    res = calc("boll", _bars(prices))
    assert res["params"] == {"n": 20, "m": 2.0}


def test_calc_unknown_indicator(prices):
    with pytest.raises(KeyError):
        calc("no_such", _bars(prices))


def test_calc_param_validation(prices):
    with pytest.raises(ValueError):
        calc("ma", _bars(prices), period=0)          # 低于下限
    with pytest.raises(ValueError):
        calc("rsi", _bars(prices), period="abc")     # 非法类型


def test_calc_empty_bars():
    with pytest.raises(ValueError):
        calc("ma", [])


def test_get_indicator_metadata():
    spec = get_indicator("kdj")
    assert spec.category == "momentum"
    assert spec.outputs == ["k", "d", "j"]
    assert spec.params[0].name == "n" and spec.params[0].default == 9


# ============================ G2-5 增补因子 ================================
def test_atr_known_flat_bars():
    """全部同价 → TR=0，ATR 预热后为 0。"""
    bars = [{"open": 10, "high": 11, "low": 9, "close": 10, "volume": 100}] * 30
    out = builtin.atr([b["close"] for b in bars], [b["high"] for b in bars],
                      [b["low"] for b in bars], 14)
    assert np.isnan(out[:14]).all()
    assert np.allclose(out[14:], 2.0)   # TR = H-L = 2 恒定


def test_atr_dispatch_via_calc(prices):
    res = calc("atr", _bars(prices), period=14)
    assert res["name"] == "atr"
    assert res["params"] == {"period": 14}
    assert res["outputs"]["atr"][0] is None


def test_returns_log_returns():
    c = [100.0, 110.0, 121.0]
    r = builtin.returns(c)
    assert r[0] != r[0]              # 首根 NaN
    assert r[1] == pytest.approx(0.1)
    assert r[2] == pytest.approx(0.1)
    lr = builtin.log_returns(c)
    assert lr[1] == pytest.approx(0.0953101798, rel=1e-6)


def test_roc_known():
    c = [100.0, 110.0, 90.0, 120.0]
    out = builtin.roc(c, 2)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx((90 / 100 - 1) * 100)
    assert out[3] == pytest.approx((120 / 110 - 1) * 100)


def test_obv_known():
    closes = [10, 11, 11, 9]
    vols = [100, 200, 300, 400]
    out = builtin.obv(closes, vols)
    assert out[0] == 100
    assert out[1] == pytest.approx(300)      # 涨 +200
    assert out[2] == pytest.approx(300)      # 平 +0
    assert out[3] == pytest.approx(-100)     # 跌 -400


def test_volume_ma_known():
    out = builtin.volume_ma([1, 2, 3, 4, 5], 3)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(2.0)


def test_zscore_zero_std_nan():
    """std=0 → NaN（不伪造）。"""
    out = builtin.zscore([5, 5, 5, 5, 5, 5], 3)
    assert np.isnan(out[2:]).all()


def test_calc_new_factor_with_bar_models(prices):
    from app.datasource.models import Bar
    bars = [Bar(time=f"2026082{i % 9 + 1}", open=h, high=h, low=l, close=c, volume=1000)
            for i, (h, l, c) in enumerate(zip(prices["highs"], prices["lows"],
                                              prices["closes"]))]
    for name in ("cci", "adx", "obv", "volume_ma", "returns", "log_returns", "zscore", "roc"):
        res = calc(name, bars)
        assert res["name"] == name
        assert res["outputs"]
