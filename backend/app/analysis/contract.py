"""G10-3 分析脚本契约：先契约后脚本（避免 Fincept 4000 野生脚本覆辙）。

契约（输入/输出）：
- 输入：``DataResult``（G1-3 统一容器：results/source/as_of/stale）+ 参数 dict。
- 输出：``AnalysisOutput`` = {metrics: dict, chart_spec: dict, alerts: list[str]}。

注册即经 G3 暴露：``GET /market/analysis/scripts`` 列出已注册脚本（auto-safe →
MCP tool）；``POST /market/analysis/run`` 执行（写域）。新增分析能力只需实现
一个符合契约的脚本并 register()，无需改路由。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from datasource.result import DataResult

log = logging.getLogger("qmt_work.analysis.contract")

#: 脚本实现签名：async (data: DataResult, **params) -> AnalysisOutput
ScriptFn = Callable[..., "AnalysisOutput"]


@dataclass
class AnalysisOutput:
    """分析脚本统一输出。chart_spec 为前端渲染器的图表规范（G9-4 消费）。"""

    metrics: Dict[str, Any] = field(default_factory=dict)
    chart_spec: Dict[str, Any] = field(default_factory=dict)
    alerts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"metrics": self.metrics, "chart_spec": self.chart_spec,
                "alerts": self.alerts}


@dataclass(frozen=True)
class AnalysisScript:
    """分析脚本声明（契约元数据 + 实现）。"""

    name: str
    title: str
    category: str                 # portfolio / risk / report / screen ...
    description: str
    fn: ScriptFn
    params: List[dict] = field(default_factory=list)   # [{name,type,default,desc}]

    def to_dict(self) -> dict:
        return {"name": self.name, "title": self.title, "category": self.category,
                "description": self.description, "params": self.params}


_SCRIPTS: Dict[str, AnalysisScript] = {}


def register(script: AnalysisScript) -> AnalysisScript:
    if script.name in _SCRIPTS:
        raise ValueError(f"分析脚本重复注册：{script.name}")
    _SCRIPTS[script.name] = script
    return script


def get_script(name: str) -> AnalysisScript:
    try:
        return _SCRIPTS[name]
    except KeyError as exc:
        raise KeyError(f"未知分析脚本：{name}（可用：{', '.join(sorted(_SCRIPTS))}）") from exc


def list_scripts() -> List[dict]:
    return [s.to_dict() for s in sorted(_SCRIPTS.values(), key=lambda s: s.name)]


async def run(name: str, data: DataResult, **params: Any) -> dict:
    """执行脚本：输入 DataResult → AnalysisOutput dict（兼容同步/异步实现）。"""
    script = get_script(name)
    out = script.fn(data, **params)
    if hasattr(out, "__await__"):       # 异步实现
        out = await out
    if not isinstance(out, AnalysisOutput):
        raise ValueError(f"脚本 {name} 未返回 AnalysisOutput")
    return out.to_dict()


# ---------------------------------------------------------------------------
# 内置示例脚本：组合概览（输入为持仓聚合 DataResult）
# ---------------------------------------------------------------------------
register(AnalysisScript(
    name="portfolio_summary",
    title="组合概览",
    category="portfolio",
    description="输入组合聚合 DataResult（results 含 total_market_value/total_pnl/"
                "sector_exposure），输出关键指标 + 告警。",
    params=[],
    fn=lambda data, **kw: _portfolio_summary(data),
))


def _portfolio_summary(data: DataResult) -> AnalysisOutput:
    r = data.results or {}
    alerts = []
    pnl = r.get("total_pnl")
    if pnl is not None and pnl < 0:
        alerts.append(f"组合浮亏 {abs(pnl):,.2f}，注意回撤")
    if r.get("position_count", 0) == 0:
        alerts.append("组合为空")
    # 行业集中度：最大行业权重 > 50% 提示
    sector = r.get("sector_exposure") or []
    if sector and sector[0].get("weight", 0) > 0.5:
        alerts.append(f"行业集中度偏高：{sector[0]['sector']} 权重 "
                      f"{sector[0]['weight'] * 100:.1f}%")
    return AnalysisOutput(
        metrics={
            "total_market_value": r.get("total_market_value"),
            "total_pnl": pnl,
            "total_pnl_pct": r.get("total_pnl_pct"),
            "position_count": r.get("position_count"),
            "top_sector_weight": sector[0].get("weight") if sector else None,
        },
        chart_spec={
            "type": "pie",
            "title": "行业暴露",
            "data": [{"name": s["sector"], "value": s["market_value"]}
                     for s in sector],
        },
        alerts=alerts,
    )


__all__ = ["AnalysisOutput", "AnalysisScript", "register", "get_script",
           "list_scripts", "run"]
