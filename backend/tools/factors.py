"""因子 / 指标库（纯 pandas/numpy 实现，可选 pandas_ta 扩展）。

所有指标为纯函数，绝不产生或伪造行情数据 —— 输入什么算什么。
- pandas / numpy 可用时优先使用；缺失时退化为纯 Python 循环实现。
- pandas_ta 可用时通过 `_PANDAS_TA` 提供扩展指标，但核心指标不依赖它。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

try:
    import pandas_ta as ta  # 可选扩展集；导入失败不影响核心指标
except ImportError:  # pragma: no cover
    ta = None

_PANDAS_TA = ta is not None


# ---------------- 输入归一化 ----------------

def _to_floats(values) -> List[float]:
    """接受 list / tuple / pd.Series / np.ndarray，返回 list[float]。"""
    if pd is not None and isinstance(values, pd.Series):
        return [None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)
                for v in values.tolist()]
    if np is not None and isinstance(values, np.ndarray):
        out = []
        for v in values.tolist():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                out.append(None)
            else:
                out.append(float(v))
        return out
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out.append(None)
        else:
            out.append(float(v))
    return out


def _to_pd_series(values) -> "pd.Series":
    """返回 pandas Series（要求 pd 可用）；缺失值以 NaN 表示。"""
    if pd is None:  # pragma: no cover
        raise RuntimeError("pandas 不可用，无法使用向量化指标")
    arr = _to_floats(values)
    return pd.Series([math.nan if v is None else v for v in arr], dtype="float64")


def _none_to_nan(values: List[float]) -> List[float]:
    return [math.nan if v is None else v for v in values]


def _nan_to_none(values) -> List[Optional[float]]:
    """把 NaN 还原为 None，便于 JSON 序列化。"""
    out = []
    for v in values:
        if v is None:
            out.append(None)
        elif isinstance(v, float) and math.isnan(v):
            out.append(None)
        else:
            out.append(float(v))
    return out


# ---------------- 单指标实现 ----------------

def _sma(values, period: int = 20):
    s = _to_pd_series(values)
    r = s.rolling(period, min_periods=period).mean()
    return _nan_to_none(r.tolist())


def _ema(values, period: int = 20):
    s = _to_pd_series(values)
    r = s.ewm(span=period, adjust=False).mean()
    return _nan_to_none(r.tolist())


def _rsi(values, period: int = 14):
    s = _to_pd_series(values)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, math.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)  # 无下跌时 RSI=100
    return _nan_to_none(rsi.tolist())


def _macd(values, fast: int = 12, slow: int = 26, signal: int = 9):
    s = _to_pd_series(values)
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return {
        "macd": _nan_to_none(macd_line.tolist()),
        "signal": _nan_to_none(signal_line.tolist()),
        "hist": _nan_to_none(hist.tolist()),
    }


def _bollinger(values, period: int = 20, num_std: float = 2.0):
    s = _to_pd_series(values)
    mid = s.rolling(period, min_periods=period).mean()
    std = s.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return {
        "mid": _nan_to_none(mid.tolist()),
        "upper": _nan_to_none(upper.tolist()),
        "lower": _nan_to_none(lower.tolist()),
    }


def _atr(high, low, close, period: int = 14):
    h = _to_pd_series(high)
    l = _to_pd_series(low)
    c = _to_pd_series(close)
    prev_close = c.shift(1)
    tr = pd.concat([
        (h - l).abs(),
        (h - prev_close).abs(),
        (l - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return _nan_to_none(atr.tolist())


def _adx(high, low, close, period: int = 14):
    h = _to_pd_series(high)
    l = _to_pd_series(low)
    c = _to_pd_series(close)
    up = h.diff()
    down = -l.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = pd.concat([
        (h - l).abs(),
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, math.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, math.nan))
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, math.nan) * 100
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return {
        "adx": _nan_to_none(adx.tolist()),
        "plus_di": _nan_to_none(plus_di.tolist()),
        "minus_di": _nan_to_none(minus_di.tolist()),
    }


def _cci(high, low, close, period: int = 20):
    h = _to_pd_series(high)
    l = _to_pd_series(low)
    c = _to_pd_series(close)
    tp = (h + l + c) / 3
    ma = tp.rolling(period, min_periods=period).mean()
    md = tp.rolling(period, min_periods=period).apply(lambda x: float(np.abs(x - x.mean()).mean()), raw=True) \
        if np is not None else tp.rolling(period, min_periods=period).apply(
            lambda x: sum(abs(v - sum(x) / len(x)) for v in x) / len(x))
    cci = (tp - ma) / (0.015 * md.replace(0, math.nan))
    return _nan_to_none(cci.tolist())


def _kdj(high, low, close, n: int = 9, m1: int = 3, m2: int = 3):
    h = _to_pd_series(high)
    l = _to_pd_series(low)
    c = _to_pd_series(close)
    hh = h.rolling(n, min_periods=n).max()
    ll = l.rolling(n, min_periods=n).min()
    rsv = (c - ll) / (hh - ll).replace(0, math.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return {
        "k": _nan_to_none(k.tolist()),
        "d": _nan_to_none(d.tolist()),
        "j": _nan_to_none(j.tolist()),
    }


def _obv(close, volume):
    c = _to_pd_series(close)
    v = _to_pd_series(volume)
    sign = c.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    obv = (sign * v).cumsum()
    return _nan_to_none(obv.tolist())


def _volume_ma(volume, period: int = 20):
    v = _to_pd_series(volume)
    r = v.rolling(period, min_periods=period).mean()
    return _nan_to_none(r.tolist())


def _returns(values):
    s = _to_pd_series(values)
    r = s.pct_change()
    return _nan_to_none(r.tolist())


def _log_returns(values):
    s = _to_pd_series(values)
    r = s.apply(lambda x: math.log(x) if x and x > 0 else math.nan).diff()
    return _nan_to_none(r.tolist())


def _zscore(values, period: int = 20):
    s = _to_pd_series(values)
    mean = s.rolling(period, min_periods=period).mean()
    std = s.rolling(period, min_periods=period).std(ddof=0)
    z = (s - mean) / std.replace(0, math.nan)
    return _nan_to_none(z.tolist())


def _roc(values, period: int = 12):
    s = _to_pd_series(values)
    r = s.pct_change(period) * 100
    return _nan_to_none(r.tolist())


# ---------------- 注册表 ----------------

# 单序列指标（仅 close / 单序列输入）直接返回 list
# 多序列 / 多输出指标（需要 high/low/volume 或返回 dict）单独处理
_FACTOR_DEFS = [
    ("sma", "简单移动平均线", {"period": 20}, lambda v, **p: _sma(v, **p)),
    ("ema", "指数移动平均线", {"period": 20}, lambda v, **p: _ema(v, **p)),
    ("rsi", "相对强弱指标", {"period": 14}, lambda v, **p: _rsi(v, **p)),
    ("bollinger", "布林带（中/上/下轨）", {"period": 20, "num_std": 2.0},
     lambda v, **p: _bollinger(v, **p)),
    ("macd", "指数平滑异同移动平均（DIF/DEA/柱）", {"fast": 12, "slow": 26, "signal": 9},
     lambda v, **p: _macd(v, **p)),
    ("atr", "平均真实波幅（需 high/low/close）", {"period": 14},
     lambda v, **p: _atr(p.get("high"), p.get("low"), v, **_strip(p))),
    ("adx", "平均趋向指数（需 high/low/close）", {"period": 14},
     lambda v, **p: _adx(p.get("high"), p.get("low"), v, **_strip(p))),
    ("cci", "顺势指标（需 high/low/close）", {"period": 20},
     lambda v, **p: _cci(p.get("high"), p.get("low"), v, **_strip(p))),
    ("kdj", "随机指标 KDJ（需 high/low/close）", {"n": 9, "m1": 3, "m2": 3},
     lambda v, **p: _kdj(p.get("high"), p.get("low"), v, **_strip(p))),
    ("obv", "能量潮（需 volume）", {}, lambda v, **p: _obv(v, p.get("volume"))),
    ("volume_ma", "成交量移动平均（需 volume）", {"period": 20},
     lambda v, **p: _volume_ma(p.get("volume"), **_strip(p))),
    ("returns", "简单收益率", {}, lambda v, **p: _returns(v)),
    ("log_returns", "对数收益率", {}, lambda v, **p: _log_returns(v)),
    ("zscore", "滚动 Z-Score", {"period": 20}, lambda v, **p: _zscore(v, **p)),
    ("roc", "变动率", {"period": 12}, lambda v, **p: _roc(v, **p)),
]

# 需要额外序列输入的因子（用于校验 / from-kline 时取字段）
_EXTRA_FIELDS = {
    "atr": ["high", "low"],
    "adx": ["high", "low"],
    "cci": ["high", "low"],
    "kdj": ["high", "low"],
    "obv": ["volume"],
    "volume_ma": ["volume"],
}


# 去掉传给底层函数的非指标参数（high/low/volume 已在闭包里取出）
def _strip(params: dict) -> dict:
    return {k: v for k, v in params.items() if k in ("period", "num_std", "fast", "slow",
                                                      "signal", "n", "m1", "m2")}


def _registry() -> Dict[str, dict]:
    reg = {}
    for name, desc, params, fn in _FACTOR_DEFS:
        reg[name] = {"name": name, "description": desc, "params": params, "callable": fn}
    return reg


FACTORS: Dict[str, dict] = _registry()


# ---------------- 公开 API ----------------

def list_factors() -> List[dict]:
    """返回所有可用指标的描述与默认参数（不含 callable）。"""
    return [{"name": m["name"], "description": m["description"], "params": m["params"]}
            for m in FACTORS.values()]


def compute_factor(name: str, series, **params) -> Any:
    """计算单个指标。

    series: 收盘序列（list / pd.Series）；部分指标还需在 params 中提供
    high/low/volume。返回对齐到输入长度的 list，或 dict（多输出指标）。
    未知指标名抛 ValueError。
    """
    if name not in FACTORS:
        raise ValueError(f"未知指标: {name}")
    extra = _EXTRA_FIELDS.get(name, [])
    for fld in extra:
        if fld not in params:
            raise ValueError(f"指标 {name} 需要参数: {', '.join(extra)}")
    return FACTORS[name]["callable"](series, **params)


def compute_many(names: List[str], closes: list, **shared) -> Dict[str, Any]:
    """一次计算多个指标（共享 closes 及 high/low/volume 等字段）。"""
    out: Dict[str, Any] = {}
    for name in names:
        params = {k: v for k, v in shared.items() if k in ("high", "low", "volume")}
        params.update({k: v for k, v in shared.items()
                       if k in FACTORS.get(name, {}).get("params", {})})
        out[name] = compute_factor(name, closes, **params)
    return out


def from_kline(kline: List[dict], field: str = "close") -> List[float]:
    """从 K 线列表抽取某字段序列（默认 close）。"""
    out = []
    for b in kline or []:
        v = b.get(field)
        out.append(None if v is None else float(v))
    return out
