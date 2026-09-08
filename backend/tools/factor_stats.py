"""因子研究基础统计（P1-7 / M13，2026-09-08 从 factor_research.py 拆分）。

纯 Python 确定性统计工具，被 factor_ic / factor_backtest 共享；
无内部依赖，便于单测与复用。
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

# ============================================================================
# 基础统计（纯 Python，确定性，可单测）
# ============================================================================

def _clean_pairs(a: Sequence[Optional[float]], b: Sequence[Optional[float]]
                 ) -> Tuple[List[float], List[float]]:
    """成对剔除 None / NaN，返回对齐的 (xs, ys)。"""
    xs, ys = [], []
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        if isinstance(x, float) and math.isnan(x):
            continue
        if isinstance(y, float) and math.isnan(y):
            continue
        xs.append(float(x))
        ys.append(float(y))
    return xs, ys


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    xs, ys = _clean_pairs(xs, ys)
    n = len(xs)
    if n < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _rank_avg(xs: List[float]) -> List[float]:
    """平均秩（处理并列）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    xs, ys = _clean_pairs(xs, ys)
    if len(xs) < 3:
        return None
    rx, ry = _rank_avg(xs), _rank_avg(ys)
    return _pearson(rx, ry)


