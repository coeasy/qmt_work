"""qmt_work 统一指标引擎 · 内置指标（G2-1，向量化实现）。

**契约铁律**：本文件所有指标输出必须与前端 `MarketData.jsx`（calcMA/calcEMA/
calcMACD/calcKDJ/calcRSI/calcBOLL/calcWR，:87-182）**逐位一致**，包括：
- EMA 首值播种（out[0]=series[0]）、MACD 的 dea 对 dif 做 9 周期 EMA；
- KDJ k/d 初值 50、n-1 前输出 null、j=3k-2d；
- RSI Wilder 平滑（首段 j=1..period 平均，此后 avg=(avg*(p-1)+Δ)/p），avgL==0 → 100；
- BOLL 用**总体标准差**（ddof=0）乘 m；
- WR 负刻度（(hn-c)/(hn-ln)×(-100)），hn==ln → 50；
- 窗口类指标在窗口不足时输出 None（绝不估算填充，零 mock 铁律）。

向量化：窗口极值用 ``sliding_window_view``（O(n)，消除 KDJ/WR 前端 O(n×period)）；
MA 用 cumsum 差分；仅递归类（EMA/KDJ/RSI 的 k,d/avg 递推）保留 O(n) 顺序循环——
这是算法本质（前后依赖），与"嵌套窗口循环"的 O(n×period) 有本质区别。
"""
from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np

Num = Optional[float]


def _as_float(a: Sequence) -> np.ndarray:
    """输入转 float64 ndarray；None/NaN 置 NaN（消费方按 NaN 处理为 null）。"""
    return np.asarray([float("nan") if v is None else float(v) for v in a], dtype=np.float64)


def _rolling_extreme(a: np.ndarray, n: int, kind: str) -> np.ndarray:
    """窗口 [i-n+1, i] 的极值（max/min）；窗口不足 i<n-1 输出 NaN。"""
    out = np.full(a.shape[0], np.nan)
    if a.shape[0] >= n and n > 0:
        sw = np.lib.stride_tricks.sliding_window_view(a, n)
        out[n - 1:] = sw.max(axis=1) if kind == "max" else sw.min(axis=1)
    return out


def ma(closes: Sequence, period: int = 20) -> np.ndarray:
    """简单移动平均（cumsum 差分 O(n)）。i<period-1 → NaN，语义同前端 calcMA。"""
    c = _as_float(closes)
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n >= period and period > 0:
        cs = np.concatenate(([0.0], np.cumsum(c)))
        out[period - 1:] = (cs[period:] - cs[:-period]) / period
    return out


def ema(series: Sequence, period: int = 12) -> np.ndarray:
    """指数平滑：k=2/(period+1)，首值播种 series[0]（语义同前端 calcEMA）。"""
    s = _as_float(series)
    n = s.shape[0]
    out = np.full(n, np.nan)
    if n == 0:
        return out
    k = 2.0 / (period + 1)
    prev = float(s[0])
    out[0] = prev
    for i in range(1, n):
        prev = float(s[i]) * k + prev * (1 - k)
        out[i] = prev
    return out


def macd(closes: Sequence) -> dict:
    """MACD：dif=ema12-ema26，dea=ema(dif,9)，bar=(dif-dea)×2（语义同前端 calcMACD）。"""
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    dif = e12 - e26
    dea = ema(dif, 9)
    bar = (dif - dea) * 2.0
    return {"dif": dif, "dea": dea, "bar": bar}


def kdj(highs: Sequence, lows: Sequence, closes: Sequence, n: int = 9) -> dict:
    """KDJ：RSV 用窗口极值（向量化），k/d 递推初值 50，j=3k-2d（语义同前端 calcKDJ）。"""
    h = _as_float(highs)
    l = _as_float(lows)
    c = _as_float(closes)
    m = c.shape[0]
    hn = _rolling_extreme(h, n, "max")
    ln = _rolling_extreme(l, n, "min")
    k = np.full(m, np.nan)
    d = np.full(m, np.nan)
    j = np.full(m, np.nan)
    kk = dd = 50.0
    for i in range(n - 1, m):
        hi, lo = float(hn[i]), float(ln[i])
        rsv = 50.0 if hi == lo else (float(c[i]) - lo) / (hi - lo) * 100.0
        kk = (2.0 / 3.0) * kk + (1.0 / 3.0) * rsv
        dd = (2.0 / 3.0) * dd + (1.0 / 3.0) * kk
        k[i], d[i], j[i] = kk, dd, 3.0 * kk - 2.0 * dd
    return {"k": k, "d": d, "j": j}


def rsi(closes: Sequence, period: int = 14) -> np.ndarray:
    """RSI（Wilder 平滑）：首段 j=1..period 平均，此后 avg=(avg*(p-1)+Δ)/p；
    avgL==0 → 100（语义同前端 calcRSI）。"""
    c = _as_float(closes)
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n < period + 2:
        return out
    deltas = np.diff(c)                       # 长度 n-1：deltas[0]=c[1]-c[0]
    g = float(np.sum(np.clip(deltas[:period], 0, None))) / period
    l_ = float(np.sum(np.clip(-deltas[:period], 0, None))) / period

    def _v(avg_g: float, avg_l: float) -> float:
        return 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)

    out[period + 1] = _v(g, l_)
    for i in range(period + 2, n):
        delta = float(c[i]) - float(c[i - 1])
        g = (g * (period - 1) + max(delta, 0.0)) / period
        l_ = (l_ * (period - 1) + max(-delta, 0.0)) / period
        out[i] = _v(g, l_)
    return out


def boll(closes: Sequence, n: int = 20, m: float = 2.0) -> dict:
    """BOLL：mid=MA(n)，std=窗口**总体标准差**（ddof=0）×m（语义同前端 calcBOLL）。"""
    c = _as_float(closes)
    length = c.shape[0]
    mid = ma(c, n)
    upper = np.full(length, np.nan)
    lower = np.full(length, np.nan)
    if length >= n and n > 0:
        sw = np.lib.stride_tricks.sliding_window_view(c, n)
        mid_t = sw.mean(axis=1)                      # == mid[n-1:]
        dev = sw - mid_t[:, None]
        std_t = np.sqrt(np.mean(dev * dev, axis=1)) * m
        tail = np.s_[n - 1:]
        upper[tail] = mid_t + std_t
        lower[tail] = mid_t - std_t
    return {"upper": upper, "mid": mid, "lower": lower}


def wr(highs: Sequence, lows: Sequence, closes: Sequence, n: int = 14) -> np.ndarray:
    """威廉指标（负刻度）：(hn-c)/(hn-ln)×(-100)，hn==ln→50；窗口不足 None（语义同前端 calcWR）。"""
    h = _as_float(highs)
    l = _as_float(lows)
    c = _as_float(closes)
    m = c.shape[0]
    out = np.full(m, np.nan)
    if m >= n and n > 0:
        hn = _rolling_extreme(h, n, "max")
        ln = _rolling_extreme(l, n, "min")
        tail = np.s_[n - 1:]
        hi, lo, cc = hn[tail], ln[tail], c[tail]
        vals = np.where(hi == lo, 50.0, (hi - cc) / (hi - lo) * (-100.0))
        out[tail] = vals
    return out


# ===========================================================================
# G2-5 增补因子（atr/adx/cci/obv/volume_ma/returns/log_returns/zscore/roc）
# 形参名 = K 线列名（close/high/low/volume），由注册表 inputs 直接传入。
# 独立实现，与 tools/factors.py 兼容层不共享代码（零 mock、零复制）。
# ===========================================================================
def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """TR = max(H-L, |H-pc|, |L-pc|)；首根（无昨收）为 NaN。"""
    n = close.shape[0]
    tr = np.full(n, np.nan)
    if n > 0:
        pc = np.concatenate(([np.nan], close[:-1]))
        tr = np.maximum.reduce([high - low, np.abs(high - pc), np.abs(low - pc)])
    return tr


def _wilders_smooth(series: np.ndarray, period: int) -> np.ndarray:
    """Wilder 平滑：首值 = 前 period 个有效值的均值，此后 = (prev*(p-1)+x)/p。"""
    n = series.shape[0]
    out = np.full(n, np.nan)
    if n < period:
        return out
    head = series[1:period + 1]
    if np.isnan(head).all():
        return out
    out[period] = np.nanmean(head)
    for i in range(period + 1, n):
        if np.isnan(series[i]):
            out[i] = out[i - 1]
        else:
            out[i] = (out[i - 1] * (period - 1) + float(series[i])) / period
    return out


def atr(close, high, low, period: int = 14) -> np.ndarray:
    """平均真实波幅（Wilder 平滑）；TR 窗口不足 → NaN。"""
    h, l, c = _as_float(high), _as_float(low), _as_float(close)
    return _wilders_smooth(_true_range(h, l, c), period)


def adx(close, high, low, period: int = 14) -> np.ndarray:
    """平均趋向指数：±DM/TR Wilder 平滑 → ±DI → DX → ADX（2*period-1 起有值）。"""
    c, h, l = _as_float(close), _as_float(high), _as_float(low)
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n < 2 * period:
        return out
    tr = _true_range(h, l, c)
    up = np.diff(h)                    # 长度 n-1，对应 i=1..n-1
    dn = np.diff(l)                    # low[i]-low[i-1]
    plus_dm = np.where((up > 0) & (up > -dn), up, 0.0)
    minus_dm = np.where((-dn > 0) & (-dn > up), -dn, 0.0)
    # 对齐长度 n：索引 0 无意义
    tr_s = _wilders_smooth(np.concatenate(([np.nan], tr[1:])), period)
    pdm_s = _wilders_smooth(np.concatenate(([np.nan], plus_dm)), period)
    mdm_s = _wilders_smooth(np.concatenate(([np.nan], minus_dm)), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100.0 * pdm_s / tr_s
        mdi = 100.0 * mdm_s / tr_s
        dx = 100.0 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.where(np.isnan(tr_s), np.nan, dx)
    dxs = np.zeros_like(dx) * np.nan
    valid = ~np.isnan(dx)
    if valid.any():
        dxs[valid] = dx[valid]
    out = _wilders_smooth(dxs, period)
    return out


def cci(close, high, low, period: int = 20) -> np.ndarray:
    """顺势指标：CCI = (tp - MA(tp)) / (0.015 * 平均绝对偏差)。"""
    c, h, l = _as_float(close), _as_float(high), _as_float(low)
    tp = (h + l + c) / 3.0
    m = tp.shape[0]
    out = np.full(m, np.nan)
    if m >= period and period > 0:
        mid = ma(tp, period)
        sw = np.lib.stride_tricks.sliding_window_view(tp, period)
        tail = np.s_[period - 1:]
        md = np.mean(np.abs(sw - mid[tail][:, None]), axis=1)   # 平均绝对偏差
        out[tail] = np.where(md == 0, np.nan, (tp[tail] - mid[tail]) / (0.015 * md))
    return out


def obv(close, volume) -> np.ndarray:
    """能量潮：OBV 累加（涨加量/跌减量/平不变），首值为首根量。"""
    c = _as_float(close)
    v = _as_float(volume)
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n == 0:
        return out
    sign = np.sign(np.diff(c))
    out[0] = v[0]
    if n > 1:
        out[1:] = v[0] + np.cumsum(sign * v[1:])
    return out


def volume_ma(volume, period: int = 20) -> np.ndarray:
    """成交量移动平均（复用 MA）。"""
    return ma(volume, period)


def returns(close) -> np.ndarray:
    """简单收益率：R[i] = C[i]/C[i-1] - 1，首根 NaN。"""
    c = _as_float(close)
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n > 1:
        out[1:] = c[1:] / c[:-1] - 1.0
    return out


def log_returns(close) -> np.ndarray:
    """对数收益率：r[i] = ln(C[i]/C[i-1])，首根 NaN。"""
    c = _as_float(close)
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n > 1:
        with np.errstate(divide="ignore", invalid="ignore"):
            out[1:] = np.log(c[1:] / c[:-1])
    return out


def zscore(close, period: int = 20) -> np.ndarray:
    """滚动 Z-Score：z = (C - MA(C,p)) / std(C,p)（总体标准差，std=0 → NaN）。"""
    c = _as_float(close)
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n >= period and period > 0:
        mid = ma(c, period)
        sw = np.lib.stride_tricks.sliding_window_view(c, period)
        tail = np.s_[period - 1:]
        std = np.std(sw, axis=1)   # ddof=0
        with np.errstate(divide="ignore", invalid="ignore"):
            out[tail] = np.where(std == 0, np.nan, (c[tail] - mid[tail]) / std)
    return out


def roc(close, period: int = 12) -> np.ndarray:
    """变动率：ROC[i] = (C[i]/C[i-period] - 1) * 100，窗口不足 NaN。"""
    c = _as_float(close)
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n > period and period > 0:
        out[period:] = (c[period:] / c[:-period] - 1.0) * 100.0
    return out
