"""V11 R7 · 指标「同一语义多份实现」一致性护栏。

**背景**：RSI 在仓库里曾有 **3 份实现**，两两不同，且**全部在产线可达**：

- ``/api/v1/indicators/calc``、``/api/v1/screener/*`` 的指标叶子 → ``app.indicators.builtin.rsi``；
- ``/api/v1/factors/compute``（前端「因子研究」面板） → ``tools.factors._rsi``。

同一根 K 线在两处显示不同 RSI。更糟的是 ``tests/test_indicators.py::_ref_rsi``
把 ``builtin.rsi`` 的错位**照抄**了一遍，形成**循环护栏**
（断言「实现 == 实现的副本」），所以分歧长期零预警。

**本文件的两条纪律**：

1. **独立参考**：参考实现用 pandas ``ewm(alpha=1/n, adjust=False)`` + 显式播种
   （与主实现完全不同的计算路径），**绝不照抄被测实现**；
2. **多入口一致**：所有对外入口在同一输入下必须给出同一输出。

RSI 权威定义 = **标准 Wilder**：前 ``n`` 个涨跌幅简单均值播种 → 首个有效值在
**下标 ``n``**；递推 ``avg = (avg*(n-1) + Δ) / n``；``avg_loss == 0`` 时
有涨幅 → 100、全平 → 50；暖机期为 NaN。
"""
from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest

from app.indicators import builtin
from tools import factors as F
from tools.indicators import ema as ema_impl
from tools.indicators import rsi as rsi_impl


# ============================ 确定性序列 ====================================
def _prices(n: int = 120):
    """与 ``tests/test_indicators.py::prices`` 同源（``random.Random(42)``）。"""
    rng = random.Random(42)
    return [100.0 + i * 0.5 + rng.uniform(-3, 3) for i in range(n)]


# ============================ 独立参考实现 ==================================
def _wilder_rsi_ref(closes, n: int = 14):
    """标准 Wilder RSI —— **独立路径**参考实现（pandas ewm + 显式播种）。

    与 ``tools.indicators.rsi`` 的差异在于计算路径：本函数把播种值当作
    ewm 序列的首个观测，之后交给 pandas 的 ``ewm(alpha=1/n, adjust=False)``
    递推；被测实现是手写 for 循环。两条路径若一致，说明递推与播种都正确。

    返回 ``list[float | None]``（None 表示暖机期）。
    """
    s = pd.Series(list(closes), dtype="float64")
    L = len(s)
    out: list = [None] * L
    if n < 1 or L < n + 1:
        return out
    d = s.diff()
    g = d.clip(lower=0.0)
    ls = (-d).clip(lower=0.0)
    ag_seed = float(g.iloc[1:n + 1].mean())
    al_seed = float(ls.iloc[1:n + 1].mean())
    # 把播种值接到序列头部，交给 ewm 递推（等价于 Wilder 平滑）
    gx = pd.concat([pd.Series([ag_seed]), g.iloc[n + 1:].reset_index(drop=True)],
                   ignore_index=True)
    lx = pd.concat([pd.Series([al_seed]), ls.iloc[n + 1:].reset_index(drop=True)],
                   ignore_index=True)
    ag = gx.ewm(alpha=1.0 / n, adjust=False).mean().tolist()
    al = lx.ewm(alpha=1.0 / n, adjust=False).mean().tolist()
    for k, (a, b) in enumerate(zip(ag, al)):
        if b == 0:
            out[n + k] = 100.0 if a > 0 else 50.0
        else:
            out[n + k] = 100.0 - 100.0 / (1.0 + a / b)
    return out


def _as_nan(vals):
    return np.asarray([np.nan if v is None else float(v) for v in vals], dtype=float)


# ============================ 1. 数值正确性 =================================
@pytest.mark.parametrize("period", [6, 14, 21])
def test_rsi_matches_independent_reference(period):
    """``tools.indicators.rsi`` == 独立参考（两条不同计算路径）。"""
    closes = _prices()
    got = _as_nan(rsi_impl(closes, period))
    ref = _as_nan(_wilder_rsi_ref(closes, period))
    assert np.allclose(got, ref, equal_nan=True, rtol=1e-9, atol=1e-9)


def test_rsi_warmup_and_first_valid_index():
    """标准 Wilder 的首个有效值必须在**下标 n**（不是 n+1）。"""
    closes = _prices()
    out = rsi_impl(closes, 14)
    assert np.isnan(out[:14]).all()
    assert not np.isnan(out[14])


def test_rsi_known_value_locked():
    """锁定实测值，防止再次静默漂移（可用 output/probe_rsi_ref.py 复现）。"""
    out = rsi_impl(_prices(), 14)
    assert out[25] == pytest.approx(58.295751485708756, abs=1e-9)
    assert out[119] == pytest.approx(58.051845, abs=1e-5)


def test_rsi_flat_series_is_50():
    """全平序列：无涨幅也无跌幅 → 50（中性），而非 100 或 NaN。"""
    out = rsi_impl([100.0] * 30, 14)
    assert np.isnan(out[:14]).all()
    assert all(v == pytest.approx(50.0) for v in out[14:])


def test_rsi_all_up_is_100():
    out = rsi_impl([100.0 + i for i in range(30)], 14)
    assert all(v == pytest.approx(100.0) for v in out[14:])


def test_rsi_short_series_all_nan():
    assert np.isnan(rsi_impl([1.0, 2.0, 3.0], 14)).all()


# ============================ 2. 多入口一致 =================================
def test_builtin_rsi_equals_tools_indicators():
    """入口 A（``/indicators/calc``、``/screener``）与权威实现逐点一致。"""
    closes = _prices()
    assert np.allclose(_as_nan(builtin.rsi(closes, 14)), rsi_impl(closes, 14),
                       equal_nan=True, rtol=1e-9, atol=1e-9)


def test_factors_rsi_equals_tools_indicators():
    """入口 B（``/factors/compute``，前端「因子研究」面板）与权威实现逐点一致。"""
    closes = _prices()
    got = _as_nan(F.compute_factor("rsi", closes, period=14))
    assert np.allclose(got, rsi_impl(closes, 14), equal_nan=True, rtol=1e-9, atol=1e-9)


def test_all_rsi_entrypoints_agree():
    """三个入口（含 screener 指标叶子用的 calc）在同一输入下必须同值。"""
    closes = _prices()
    from app.indicators import calc

    bars = [{"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000}
            for c in closes]
    a = rsi_impl(closes, 14)
    b = _as_nan(builtin.rsi(closes, 14))
    c = _as_nan(F.compute_factor("rsi", closes, period=14))
    d = _as_nan(calc("rsi", bars, period=14)["outputs"]["rsi"])
    for other, name in ((b, "builtin"), (c, "factors.compute_factor"), (d, "indicators.calc")):
        assert np.allclose(a, other, equal_nan=True, rtol=1e-9, atol=1e-9), name


# ============================ 3. 顺带锁死 EMA（同为多入口） ==================
def test_ema_entrypoints_agree():
    """EMA 也有两份实现（``tools.indicators.ema`` / ``tools.factors._ema``），锁定一致。"""
    closes = _prices()
    a = ema_impl(closes, 12)
    b = _as_nan(F.compute_factor("ema", closes, period=12))
    assert np.allclose(a, b, equal_nan=True, rtol=1e-9, atol=1e-9)


# ============================ 4. 防回归：禁止重新引入分叉 ====================
def _body_source(fn) -> str:
    """返回函数**函数体**源码（剥掉 docstring），用于检查实现方式。"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    body = tree.body[0].body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]                       # 剥掉 docstring
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_builtin_rsi_delegates_to_single_impl():
    """``builtin.rsi`` 必须是薄委托（函数体里不得再有自己的递推/平滑）。"""
    src = _body_source(builtin.rsi)
    assert "tools.indicators" in src, "builtin.rsi 应委托 tools.indicators.rsi"
    assert "ewm" not in src, "builtin.rsi 不应保留独立平滑实现"
    assert "avg_g" not in src and "avg_l" not in src, "builtin.rsi 不应保留独立递推"


def test_factors_rsi_delegates_to_single_impl():
    """``factors._rsi`` 必须是薄委托。"""
    src = _body_source(F._rsi)
    assert "tools.indicators" in src, "factors._rsi 应委托 tools.indicators.rsi"
    assert "ewm" not in src, "factors._rsi 不应保留 ewm 独立实现"
