"""向量化回测引擎与 legacy 纯 Python 引擎的行为一致性测试。

锁定契约：_signals_vectorized 必须与 _signals 逐根信号一致；
run_backtest_vectorized 的指标体系必须与 run_backtest_engine 完全相等
（同一个交易内核 + 同一套成本模型）。
运行：cd backend && python -m pytest tests/test_backtest_vectorized.py -q
"""
import math
import os
import sys
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest import (  # noqa: E402
    _signals, _signals_vectorized, run_backtest_engine,
    run_backtest_vectorized, run_param_sweep,
)

SEED = 20240814


def _synthetic(n=401, seed=SEED):
    random.seed(seed)
    closes = []
    price = 100.0
    for i in range(n):
        price += random.uniform(-2, 2) + math.sin(i / 10.0)
        closes.append(price)
    return closes


CLOSES = _synthetic()


def _kline(closes):
    return [{"close": c, "time": i} for i, c in enumerate(closes)]


CONFIGS = [
    ("ma_cross", {"fast": 5, "slow": 20}),
    ("ma_cross", {"fast": 10, "slow": 30}),
    ("ma_cross", {"fast": 3, "slow": 15}),
    ("macd", {}),
    ("macd", {"fast": 8, "slow": 17, "signal": 5}),
    ("rsi", {}),
    ("rsi", {"period": 7, "buy": 35, "sell": 65}),
    ("rsi", {"period": 21, "buy": 25, "sell": 75}),
]


def test_signals_identical():
    for strat, params in CONFIGS:
        a = _signals(strat, CLOSES, params)
        b = _signals_vectorized(strat, CLOSES, params)
        assert a == b, f"{strat} {params}: signals diverge {sum(1 for x,y in zip(a,b) if x!=y)}/{len(a)}"


def test_backtest_metrics_equal():
    for strat, params in CONFIGS:
        k = _kline(CLOSES)
        m1 = run_backtest_engine("X", k, strat, params, 100_000.0)["metrics"]
        m2 = run_backtest_vectorized("X", k, strat, params, 100_000.0)["metrics"]
        for key in m1:
            if isinstance(m1[key], (int, float)):
                assert abs(float(m1[key]) - float(m2[key])) < 1e-9, (
                    f"{strat} {params}: metric {key} {m1[key]} != {m2[key]}")
        assert m1["trade_count"] == m2["trade_count"]


def test_param_sweep_runs():
    k = _kline(CLOSES)
    res = run_param_sweep("X", k, "ma_cross",
                          {"fast": [5, 10, 20], "slow": [20, 40]},
                          initial_capital=100_000.0)
    assert res["count"] == 6
    assert res["best"] is not None
    assert "params" in res["best"] and "metrics" in res["best"]
    # 网格全部按夏普降序
    sharpe = [g["metrics"].get("sharpe", -99) for g in res["grid"]]
    assert sharpe == sorted(sharpe, reverse=True)


def test_short_kline_rejected():
    short = _kline(CLOSES[:20])
    import pytest
    with pytest.raises(Exception):
        run_backtest_vectorized("X", short, "ma_cross", {}, 100_000.0)
