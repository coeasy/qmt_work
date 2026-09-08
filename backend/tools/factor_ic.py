"""因子有效性分析：IC/ICIR、分层收益、因子相关性矩阵（P1-7 / M13）。

拆分自 factor_research.py；依赖 factor_stats 的纯统计工具。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from .factor_stats import _clean_pairs, _mean, _pearson, _spearman, _stdev

# ============================================================================
# 1. 因子 IC / ICIR
# ============================================================================

def factor_ic(factor: Sequence[float], forward_return: Sequence[float],
              method: str = "pearson") -> Optional[float]:
    """单序列因子 IC：因子值 t 与远期收益 t 的（秩）相关。

    用于「单标的、时间序列」场景：IC 为全样本标量相关；
    若需要 ICIR（多期 IC 的均值/波动），请用 `factor_ic_panel` 产生逐期 IC 序列。
    method: "pearson" | "spearman"。
    """
    if method == "spearman":
        return _spearman(list(factor), list(forward_return))
    return _pearson(list(factor), list(forward_return))


def factor_ic_panel(factor_panel: Sequence[Sequence[float]],
                    return_panel: Sequence[Sequence[float]],
                    method: str = "pearson") -> List[Optional[float]]:
    """截面逐期 IC：每个时间截面 t 上，对全市场因子值与远期收益做（秩）相关。

    factor_panel / return_panel：等长的时间序列，每项是该时刻的截面（各标的）列表。
    返回与输入等长的逐期 IC 列表（截面退化时该期为 None）。
    """
    T = min(len(factor_panel), len(return_panel))
    out: List[Optional[float]] = []
    for t in range(T):
        fa = list(factor_panel[t])
        ra = list(return_panel[t])
        if method == "spearman":
            out.append(_spearman(fa, ra))
        else:
            out.append(_pearson(fa, ra))
    return out


def ic_statistics(ic_list: Sequence[Optional[float]]) -> dict:
    """对逐期 IC 序列汇总：均值 / 波动 / ICIR / 胜率 / t 值 / 显著性。

    ICIR = mean(IC) / std(IC)；positive_ratio = IC>0 占比；
    t_stat = mean / (std/√n)；|t_stat|>1.96 视为显著。
    """
    vals = [x for x in ic_list if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not vals:
        return {"n": 0, "ic_mean": None, "ic_std": None, "icir": None,
                "positive_ratio": None, "t_stat": None, "significant": False}
    m = _mean(vals)
    sd = _stdev(vals)
    n = len(vals)
    icir = (m / sd) if sd > 0 else None
    pos = sum(1 for v in vals if v > 0) / n
    t_stat = (m / (sd / math.sqrt(n))) if sd > 0 else None
    return {
        "n": n,
        "ic_mean": round(m, 4),
        "ic_std": round(sd, 4),
        "icir": round(icir, 3) if icir is not None else None,
        "positive_ratio": round(pos, 3),
        "t_stat": round(t_stat, 3) if t_stat is not None else None,
        "significant": bool(t_stat is not None and abs(t_stat) > 1.96),
    }


# ============================================================================
# 2. 分位（分位数）分组
# ============================================================================

def quantile_analysis(factor: Sequence[float], forward_return: Sequence[float],
                      n_q: int = 5) -> dict:
    """分位分组：按因子值分箱，统计各分位远期收益均值 / 累积，及多空（top-bottom）价差。

    接受「配对样本」(因子值, 远期收益)（单标的时序或多标的截面混合均可）。
    - quantiles: 各分位 min/max/avg_return/count/cum_return
    - long_short_avg_return: 最高分位均值 - 最低分位均值（因子单调性代理）
    返回结构可直接绘图（spread_by_quantile）。
    """
    xs, ys = _clean_pairs(list(factor), list(forward_return))
    n = len(xs)
    if n < n_q * 2:
        raise ValueError(f"样本不足：需 ≥ {n_q * 2} 个配对样本，当前 {n}")
    # 分位边界（等计数组）
    order = sorted(range(n), key=lambda i: xs[i])
    bins: List[List[int]] = [[] for _ in range(n_q)]
    for rank, idx in enumerate(order):
        q = min(n_q - 1, rank * n_q // n)
        bins[q].append(idx)
    quants = []
    spread: List[float] = []
    for q in range(n_q):
        idxs = bins[q]
        rets = [ys[i] for i in idxs]
        avg = _mean(rets)
        cum = 1.0
        for r in rets:
            cum *= (1.0 + r)
        fvals = [xs[i] for i in idxs]
        quants.append({
            "q": q + 1,
            "min": round(min(fvals), 6),
            "max": round(max(fvals), 6),
            "avg_return": round(avg, 6),
            "count": len(idxs),
            "cum_return": round(cum - 1.0, 6),
        })
        spread.append(avg)
    top = quants[-1]["avg_return"]
    bottom = quants[0]["avg_return"]
    # 多空组合逐期收益 = top 组样本收益 - bottom 组样本收益
    ls_samples = [ys[i] for i in bins[-1]] + [-ys[i] for i in bins[0]]
    ls_mean = _mean(ls_samples)
    ls_sd = _stdev(ls_samples)
    return {
        "n_quantiles": n_q,
        "n_samples": n,
        "quantiles": quants,
        "spread_by_quantile": [round(s, 6) for s in spread],
        "long_short_avg_return": round(top - bottom, 6),
        "long_short_sharpe": round(ls_mean / ls_sd * math.sqrt(252), 3) if ls_sd > 0 else None,
    }


# ============================================================================
# 3. 因子相关性矩阵
# ============================================================================

def factor_correlation(factor_dict: Dict[str, List[float]],
                       method: str = "pearson") -> dict:
    """因子相关性矩阵：对一组等长因子序列两两计算（秩）相关。

    factor_dict: {因子名: 序列}；以最短长度对齐。返回 names + matrix。
    """
    names = list(factor_dict.keys())
    seqs = {k: list(v) for k, v in factor_dict.items()}
    L = min(len(v) for v in seqs.values()) if seqs else 0
    if L < 3:
        raise ValueError("因子序列长度不足（需 ≥ 3）")
    aligned = {k: v[:L] for k, v in seqs.items()}
    matrix = {}
    for a in names:
        row = {}
        for b in names:
            if a == b:
                row[b] = 1.0
            else:
                c = _spearman(aligned[a], aligned[b]) if method == "spearman" else _pearson(aligned[a], aligned[b])
                row[b] = round(c, 4) if c is not None else None
        matrix[a] = row
    return {"method": method, "names": names, "matrix": matrix}


