"""qmt_work 统一指标引擎 · 注册表与调度（G2-1）。

**单一真源**：指标定义（元数据 + 向量化实现）只在本包存在一次，经 G2-2 自动暴露
为 REST + MCP 后，前端 K 线/选股条件/策略信号全部消费后端结果，不再各自实现
（消除 R2：后端 19 因子 vs 前端 7 指标的"双份实现"）。

元数据（G2-1）：``name`` / ``label`` / ``category`` / ``description`` /
``params``（名称/类型/默认/下限）/ ``outputs`` / ``formula``（公式说明，为
G2-4 公式 DSL 阶段二预留声明式基础）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from app.indicators import builtin

log = logging.getLogger("qmt_work.indicators.registry")

PARAM_INT = "int"
PARAM_FLOAT = "float"


@dataclass(frozen=True)
class IndicatorParam:
    """指标参数声明。"""

    name: str
    type: str = PARAM_INT          # int / float
    default: Any = None
    min: Optional[float] = None
    desc: str = ""


@dataclass(frozen=True)
class IndicatorSpec:
    """指标声明式元数据 + 实现绑定。"""

    name: str
    label: str
    category: str                  # trend / momentum / volatility / volume / other
    description: str
    params: List[IndicatorParam] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    fn: Optional[Callable] = None  # 向量化实现（builtin.*）
    formula: str = ""              # 公式说明（DSL 阶段二的元数据基础）

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "category": self.category,
            "description": self.description,
            "params": [{"name": p.name, "type": p.type, "default": p.default,
                        "min": p.min, "desc": p.desc} for p in self.params],
            "outputs": list(self.outputs),
            "formula": self.formula,
        }


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
_INDICATORS: Dict[str, IndicatorSpec] = {}


def register(spec: IndicatorSpec) -> IndicatorSpec:
    if spec.name in _INDICATORS:
        raise ValueError(f"指标重复注册：{spec.name}")
    _INDICATORS[spec.name] = spec
    return spec


def get_indicator(name: str) -> IndicatorSpec:
    try:
        return _INDICATORS[name]
    except KeyError as exc:
        raise KeyError(f"未知指标：{name}（可用：{', '.join(sorted(_INDICATORS))}）") from exc


def list_indicators() -> List[dict]:
    return [spec.to_dict() for spec in sorted(_INDICATORS.values(), key=lambda s: s.name)]


# ---------------------------------------------------------------------------
# 内置指标注册
# ---------------------------------------------------------------------------
register(IndicatorSpec(
    name="ma", label="MA 简单均线", category="trend",
    description="简单移动平均，i<period-1 输出 null（对齐前端 calcMA）。",
    params=[IndicatorParam("period", PARAM_INT, 20, 1, "窗口周期")],
    outputs=["ma"], fn=builtin.ma, formula="MA(n) = mean(C[i-n+1..i])",
))
register(IndicatorSpec(
    name="ema", label="EMA 指数均线", category="trend",
    description="指数平滑，k=2/(period+1)，首值播种（对齐前端 calcEMA）。",
    params=[IndicatorParam("period", PARAM_INT, 12, 1, "平滑周期")],
    outputs=["ema"], fn=builtin.ema, formula="EMA = C[i]*k + EMA[i-1]*(1-k)",
))
register(IndicatorSpec(
    name="macd", label="MACD 指数平滑异同", category="trend",
    description="dif=ema12-ema26，dea=ema(dif,9)，bar=(dif-dea)*2（对齐前端 calcMACD）。",
    params=[], outputs=["dif", "dea", "bar"], fn=builtin.macd,
    formula="MACD = (ema12 - ema26, ema(dif,9), (dif-dea)*2)",
))
register(IndicatorSpec(
    name="kdj", label="KDJ 随机指标", category="momentum",
    description="RSV 窗口极值 + k/d 递推（初值 50），j=3k-2d（对齐前端 calcKDJ）。",
    params=[IndicatorParam("n", PARAM_INT, 9, 1, "窗口周期")],
    outputs=["k", "d", "j"], fn=builtin.kdj,
    formula="RSV=(C-Ln)/(Hn-Ln)*100; K=(2K+RSV)/3; D=(2D+K)/3; J=3K-2D",
))
register(IndicatorSpec(
    name="rsi", label="RSI 相对强弱", category="momentum",
    description="Wilder 平滑，avgL==0 → 100（对齐前端 calcRSI）。",
    params=[IndicatorParam("period", PARAM_INT, 14, 1, "平滑周期")],
    outputs=["rsi"], fn=builtin.rsi,
    formula="RSI = 100 - 100/(1+avgG/avgL)，avg=(avg*(p-1)+Δ)/p",
))
register(IndicatorSpec(
    name="boll", label="BOLL 布林带", category="volatility",
    description="mid=MA(n)，带=总体标准差×m（对齐前端 calcBOLL）。",
    params=[IndicatorParam("n", PARAM_INT, 20, 1, "窗口周期"),
            IndicatorParam("m", PARAM_FLOAT, 2.0, 0.1, "标准差倍数")],
    outputs=["upper", "mid", "lower"], fn=builtin.boll,
    formula="MID=MA(n); UP/DN=MID±m*std(C[n])",
))
register(IndicatorSpec(
    name="wr", label="WR 威廉指标", category="momentum",
    description="负刻度，(hn-c)/(hn-ln)*(-100)，hn==ln → 50（对齐前端 calcWR）。",
    params=[IndicatorParam("n", PARAM_INT, 14, 1, "窗口周期")],
    outputs=["wr"], fn=builtin.wr,
    formula="WR = (Hn-C)/(Hn-Ln)*(-100)",
))


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
def _to_list(a: np.ndarray) -> list:
    """numpy 数组 → list，NaN → None（JSON 友好，绝不伪造）。"""
    return [None if (v != v) else float(v) for v in a]  # NaN 自不等


def _resolve_params(spec: IndicatorSpec, params: Dict[str, Any]) -> Dict[str, Any]:
    """参数解析：缺省取默认，非法类型报错，低于下限钳制（显式告知而非静默）。"""
    resolved: Dict[str, Any] = {}
    for p in spec.params:
        if p.name not in params or params[p.name] is None:
            resolved[p.name] = p.default
            continue
        raw = params[p.name]
        try:
            val = int(raw) if p.type == PARAM_INT else float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"指标 {spec.name} 参数 {p.name} 非法：{raw!r}") from exc
        if p.min is not None and val < p.min:
            raise ValueError(f"指标 {spec.name} 参数 {p.name} 低于下限 {p.min}：{val}")
        resolved[p.name] = val
    return resolved


def _col(bars: list, key: str) -> list:
    """从 Bar 模型或 dict 列表取列（两态兼容）。"""
    if not bars:
        return []
    first = bars[0]
    getter = (lambda b: getattr(b, key, None)) if hasattr(first, key) else (lambda b: b.get(key))
    return [getter(b) for b in bars]


def calc(name: str, bars: list, **params: Any) -> dict:
    """计算指标：输入 bars（Bar 模型或 dict，需含 open/high/low/close/volume），
    返回 {name, params, outputs:{列名: list}}，NaN 一律转 null。"""
    spec = get_indicator(name)
    resolved = _resolve_params(spec, params)
    closes = _col(bars, "close")
    if not closes:
        raise ValueError(f"指标 {name} 需要非空 K 线输入")
    if spec.fn is None:
        raise ValueError(f"指标 {name} 未绑定实现")
    if name in ("kdj", "wr"):
        raw = spec.fn(_col(bars, "high"), _col(bars, "low"), closes, **resolved)
    else:
        raw = spec.fn(closes, **resolved)
    outputs = {k: _to_list(v) for k, v in raw.items()} if isinstance(raw, dict) \
        else {"ma" if name == "ma" else ("ema" if name == "ema" else "rsi"): _to_list(raw)}
    return {"name": name, "params": resolved, "outputs": outputs}


__all__ = [
    "IndicatorParam", "IndicatorSpec", "register", "get_indicator",
    "list_indicators", "calc",
]
