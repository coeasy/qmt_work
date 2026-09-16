"""统一技术指标库（纯 Python + numpy，无 native 依赖）。

本项目 backtest（tools/backtest.py）和 strategy_runtime（tools/strategy_runtime.py）
之前各自实现 SMA/EMA/MACD/RSI，逻辑重复且差异微妙。本模块提供统一实现。

设计原则：
- **不依赖 xtquant**：纯 numpy 数组运算，宿主进程和桥接子进程都能用。
- **API 稳定**：所有函数返回 numpy 数组或浮点数，调用方按 shape 自取。
- **NaN 友好**：暖机期（前 n-1 个值）用 NaN 填充，对下游 backtest 信号判断友好。
- **性能优先**：用 numpy 向量化，单千点 K 线计算 < 1ms。

历史背景：
- 2026-09-07 重构：R2，从 backtest.py + strategy_runtime.py 抽取统一实现。
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np

# ============================================================================
# 基础指标
# ============================================================================

def sma(values: np.ndarray | List[float], n: int) -> np.ndarray:
    """简单移动平均：前 n-1 个位置返回 NaN。

    Args:
        values: 输入序列（价格/成交量等）
        n: 窗口大小（必须 >= 1）

    Returns:
        np.ndarray: 与输入等长，dtype=float
    """
    v = np.asarray(values, dtype=float)
    if n < 1 or len(v) == 0:
        return np.full_like(v, np.nan, dtype=float)
    if len(v) < n:
        return np.full_like(v, np.nan, dtype=float)
    # cumsum 滑动窗口
    csum = np.cumsum(np.insert(v, 0, 0.0))
    out = (csum[n:] - csum[:-n]) / n
    return np.concatenate([np.full(n - 1, np.nan), out])


def ema(values: np.ndarray | List[float], n: int) -> np.ndarray:
    """指数移动平均：k = 2/(n+1)，首值用 values[0]。

    Args:
        values: 输入序列
        n: 窗口大小

    Returns:
        np.ndarray: 与输入等长
    """
    v = np.asarray(values, dtype=float)
    if n < 1 or len(v) == 0:
        return np.full_like(v, np.nan, dtype=float)
    k = 2.0 / (n + 1)
    out = np.empty_like(v, dtype=float)
    out[0] = v[0]
    for i in range(1, len(v)):
        out[i] = v[i] * k + out[i - 1] * (1 - k)
    return out


def macd(closes: np.ndarray | List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD 指标：返回 (DIF, DEA, HIST) 三条线。

    DIF = EMA(close, fast) - EMA(close, slow)
    DEA = EMA(DIF, signal)
    HIST = 2 * (DIF - DEA)   # 常见定义；backtest 用 DIF-DEA（不乘2）以保持金叉死叉阈值一致性
    """
    v = np.asarray(closes, dtype=float)
    dif = ema(v, fast) - ema(v, slow)
    dea = ema(dif, signal)
    hist = dif - dea
    return dif, dea, hist


def rsi(closes: np.ndarray | List[float], n: int = 14) -> np.ndarray:
    """RSI 相对强弱指数（**标准 Wilder 定义**）：返回 [0, 100] 区间数组，暖机期 NaN。

    算法（与 TradingView / 教科书一致）：
    - 播种：前 ``n`` 个涨跌幅的**简单均值** → 首个有效值在**下标 ``n``**；
    - 递推：``avg = (avg * (n-1) + Δ) / n``（Wilder 平滑）；
    - ``avg_loss == 0``：有涨幅 → **100**；全平 → **50**（无信息，取中性值）；
    - 暖机期（下标 < ``n``）为 NaN。

    ★ V11 R7 修正（2026-09-15 实测）：此前本函数用**滚动窗口简单均值**（``np.mean(gains[i-n:i])``），
    却声称「使用 Wilder 平滑（与简单移动平均等价）」—— 该等价说法**不成立**。
    同时仓库里还有另外两份 RSI（``app.indicators.builtin.rsi``、
    ``tools.factors._rsi``），**三份两两不同**，且**全部在产线可达**：

    - ``/api/v1/indicators/calc``、``/api/v1/screener/*`` 的指标叶子 → ``builtin.rsi``；
    - ``/api/v1/factors/compute``（前端「因子研究」面板） → ``factors._rsi``。

    同一根 K 线在「行情叠加」与「因子研究」两处显示**不同的 RSI**。
    下表为 ``backend/tests/test_indicators.py::prices`` 固定序列
    （``random.Random(42)``，120 根）上 ``RSI(14)`` 的实测值，可用
    ``backend/output/probe_rsi_old.py`` 复现：

    ==================  ==========================  ==========  =============
    实现                规则                        首个有效下标  idx25 值
    ==================  ==========================  ==========  =============
    标准 Wilder         播种 + 递推                 **14**      **58.295751**
    本函数（旧）        滚动窗口简单均值            14          58.864162
    ``builtin``（旧）   首值错写在下标 ``n+1``，     15 ❌       58.499036
                        且递推**跳过 delta[n]**
    ``factors``（旧）   ``ewm`` 无播种              1 ❌        47.188055
    ==================  ==========================  ==========  =============

    即同一时刻 RSI 读数最大相差 **11.7**（47.19 vs 58.86）—— 远超任何合理容差。

    现本函数为**唯一实现**，``builtin.rsi`` 与 ``factors._rsi`` 均委托到此处；
    一致性由 ``backend/tests/test_indicator_unity.py`` 锁死（含**独立参考实现**，
    用 pandas ``ewm(alpha=1/n, adjust=False)`` + 显式播种，而非照抄本函数）。
    """
    v = np.asarray(closes, dtype=float)
    out = np.full(v.shape[0], np.nan, dtype=float)
    if n < 1 or v.shape[0] < n + 1:
        return out
    d = np.diff(v)
    g = np.clip(d, 0.0, None)
    l = np.clip(-d, 0.0, None)          # noqa: E741 — 与 g 对称，沿用数学记号
    ag = float(g[:n].mean())
    al = float(l[:n].mean())
    for i in range(n, v.shape[0]):
        if i > n:                       # 下标 n 处直接用播种值，不重复递推
            ag = (ag * (n - 1) + float(g[i - 1])) / n
            al = (al * (n - 1) + float(l[i - 1])) / n
        if al == 0:
            out[i] = 100.0 if ag > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ag / al)
    return out


# ============================================================================
# 信号生成
# ============================================================================

def ma_cross_signals(closes: np.ndarray | List[float], fast: int = 5, slow: int = 20) -> np.ndarray:
    """MA 金叉死叉仓位信号：1=持仓，0=空仓。

    暖机期（任一 SMA 仍为 NaN）保持前一根仓位不变（默认空仓）。
    """
    v = np.asarray(closes, dtype=float)
    n = len(v)
    sig = np.zeros(n, dtype=int)
    if n == 0:
        return sig
    fast_ma = sma(v, fast)
    slow_ma = sma(v, slow)
    for i in range(1, n):
        if math.isnan(fast_ma[i]) or math.isnan(slow_ma[i]):
            sig[i] = sig[i - 1]
            continue
        prev_f = fast_ma[i - 1]
        prev_s = slow_ma[i - 1]
        prev_known = not (math.isnan(prev_f) or math.isnan(prev_s))
        if fast_ma[i] > slow_ma[i] and (not prev_known or prev_f <= prev_s):
            sig[i] = 1
        elif fast_ma[i] < slow_ma[i] and (not prev_known or prev_f >= prev_s):
            sig[i] = 0
        else:
            sig[i] = sig[i - 1]
    return sig


def macd_signals(closes: np.ndarray | List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> np.ndarray:
    """MACD 金叉死叉仓位信号（基于 HIST 过零）。"""
    v = np.asarray(closes, dtype=float)
    n = len(v)
    sig = np.zeros(n, dtype=int)
    if n == 0:
        return sig
    _, _, hist = macd(v, fast, slow, signal)
    for i in range(1, n):
        h, ph = hist[i], hist[i - 1]
        if math.isnan(h) or math.isnan(ph):
            sig[i] = sig[i - 1]
        elif h > 0 and ph <= 0:
            sig[i] = 1
        elif h < 0 and ph >= 0:
            sig[i] = 0
        else:
            sig[i] = sig[i - 1]
    return sig


def rsi_signals(closes: np.ndarray | List[float], period: int = 14, buy: float = 30, sell: float = 70) -> np.ndarray:
    """RSI 阈值仓位信号：RSI<buy 持仓，RSI>sell 空仓。"""
    v = np.asarray(closes, dtype=float)
    n = len(v)
    sig = np.zeros(n, dtype=int)
    if n == 0:
        return sig
    r = rsi(v, period)
    for i in range(n):
        if math.isnan(r[i]):
            sig[i] = sig[i - 1] if i > 0 else 0
        elif r[i] < buy:
            sig[i] = 1
        elif r[i] > sell:
            sig[i] = 0
        else:
            sig[i] = sig[i - 1] if i > 0 else 0
    return sig


def signals_for(strategy: str, closes: np.ndarray | List[float], params: dict) -> np.ndarray:
    """统一信号生成入口：按 strategy 名称分派。"""
    s = (strategy or "ma_cross").lower()
    if s == "macd":
        return macd_signals(closes,
                            int(params.get("fast", 12)),
                            int(params.get("slow", 26)),
                            int(params.get("signal", 9)))
    if s == "rsi":
        return rsi_signals(closes,
                           int(params.get("period", 14)),
                           float(params.get("buy", 30)),
                           float(params.get("sell", 70)))
    # 默认 ma_cross
    return ma_cross_signals(closes,
                            int(params.get("fast", 5)),
                            int(params.get("slow", 20)))


# ============================================================================
# 单点信号（用于实时 K 线最后一根判定）
# ============================================================================

def ma_cross_last(closes: np.ndarray | List[float], fast: int = 5, slow: int = 20) -> Tuple[str, Optional[float]]:
    """MA 金叉死叉单点判定：返回 ('buy'/'sell'/'hold', fast_ma 或 None)。

    与 strategy_runtime 的 _ma_signal 等价。"""
    v = np.asarray(closes, dtype=float)
    if len(v) < slow + 1:
        return "hold", None
    fast_ma = float(np.mean(v[-fast:]))
    slow_ma = float(np.mean(v[-slow:]))
    prev_fast = float(np.mean(v[-fast - 1:-1]))
    prev_slow = float(np.mean(v[-slow - 1:-1]))
    if prev_fast <= prev_slow and fast_ma > slow_ma:
        return "buy", fast_ma
    if prev_fast >= prev_slow and fast_ma < slow_ma:
        return "sell", fast_ma
    return "hold", fast_ma


def macd_last(closes: np.ndarray | List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[str, Optional[float]]:
    """MACD 单点判定：返回 ('buy'/'sell'/'hold', dif 或 None)。"""
    v = np.asarray(closes, dtype=float)
    need = slow + signal + 2
    if len(v) < need:
        return "hold", None
    dif = ema(v, fast) - ema(v, slow)
    dea = ema(dif, signal)
    cross_up = dea[-2] <= dif[-2] and dea[-1] > dif[-1]
    cross_dn = dea[-2] >= dif[-2] and dea[-1] < dif[-1]
    if cross_up:
        return "buy", float(dif[-1])
    if cross_dn:
        return "sell", float(dif[-1])
    return "hold", float(dif[-1])


def rsi_last(closes: np.ndarray | List[float], period: int = 14, buy: float = 30, sell: float = 70) -> Tuple[str, Optional[float]]:
    """RSI 单点判定：返回 ('buy'/'sell'/'hold', rsi 或 None)。"""
    r = rsi(closes, period)
    if math.isnan(r[-1]):
        return "hold", None
    v = float(r[-1])
    if v < buy:
        return "buy", v
    if v > sell:
        return "sell", v
    return "hold", v


def last_signal_for(strategy: str, closes: np.ndarray | List[float], params: dict) -> Tuple[str, Optional[float]]:
    """统一单点信号入口：按 strategy 名称分派。"""
    s = (strategy or "ma_cross").lower()
    if s == "macd":
        return macd_last(closes,
                          int(params.get("fast", 12)),
                          int(params.get("slow", 26)),
                          int(params.get("signal", 9)))
    if s == "rsi":
        return rsi_last(closes,
                        int(params.get("period", 14)),
                        float(params.get("buy", 30)),
                        float(params.get("sell", 70)))
    return ma_cross_last(closes,
                         int(params.get("fast", 5)),
                         int(params.get("slow", 20)))


__all__ = [
    "sma", "ema", "macd", "rsi",
    "ma_cross_signals", "macd_signals", "rsi_signals", "signals_for",
    "ma_cross_last", "macd_last", "rsi_last", "last_signal_for",
]
