"""G10-3 分析脚本契约 + G4 数据面策略表 + G9-4 图表规范测试。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import asyncio

import pytest

from app.analysis.contract import (
    AnalysisOutput,
    AnalysisScript,
    get_script,
    list_scripts,
    register,
    run,
)
from app.datasource.result import DataResult
from app.routes.analysis import market_analysis_run, market_analysis_scripts_list
from app.routes.datahub import datahub_policies
from app.routes.indicators import market_chart_spec


def test_contract_has_portfolio_summary():
    names = {s["name"] for s in list_scripts()}
    assert "portfolio_summary" in names
    spec = get_script("portfolio_summary")
    assert spec.category == "portfolio"


def test_contract_run_portfolio():
    dres = DataResult.from_source({
        "total_market_value": 172000,
        "total_pnl": -42000,
        "position_count": 2,
        "sector_exposure": [{"sector": "白酒", "market_value": 160000, "weight": 0.93}],
    }, source="api")
    out = asyncio.run(run("portfolio_summary", dres))
    assert out["metrics"]["position_count"] == 2
    assert any("浮亏" in a for a in out["alerts"])          # pnl<0 → 告警
    assert any("集中度" in a for a in out["alerts"])        # 权重>0.5 → 告警
    assert out["chart_spec"]["type"] == "pie"


def test_contract_unknown_script():
    with pytest.raises(KeyError):
        asyncio.run(run("nope", DataResult.from_source({}, source="api")))


def test_contract_requires_analysis_output():
    async def _bad(_data, **_kw):
        return {"metrics": {}}
    register(AnalysisScript(name="_bad_script", title="bad", category="test",
                            description="x", fn=_bad))
    with pytest.raises(ValueError):
        asyncio.run(run("_bad_script", DataResult.from_source({}, source="api")))


def test_contract_route_list():
    res = asyncio.run(market_analysis_scripts_list())
    assert res["code"] == 0
    assert res["data"]["count"] >= 1


def test_contract_route_run():
    body = {
        "name": "portfolio_summary",
        "data": {"results": {"total_pnl": -100, "position_count": 1,
                             "sector_exposure": []}, "source": "api"},
    }
    res = asyncio.run(market_analysis_run(body))
    assert res["code"] == 0
    assert res["data"]["metrics"]["total_pnl"] == -100


def test_contract_route_run_missing_name():
    res = asyncio.run(market_analysis_run({"data": {}}))
    assert res["code"] == 400


# ============================ G4 数据面策略表 ==============================
def test_datahub_policies_route():
    res = asyncio.run(datahub_policies())
    assert res["code"] == 0
    topics = res["data"]["topics"]
    assert "market:boards" in topics
    p = topics["market:boards"]
    for key in ("ttl_ms", "min_interval_ms", "coalesce_within_ms", "priority", "stale_ok"):
        assert key in p
    assert res["data"]["default"]["ttl_ms"] == 30000


# ============================ G9-4 图表规范 =================================
def test_chart_spec_route():
    res = asyncio.run(market_chart_spec())
    assert res["code"] == 0
    spec = res["data"]
    assert spec["candlestick"]["up"] == "#ef4d56"
    assert spec["main"]["default"] == "ma"
    assert spec["sub"]["default"] == "macd"
    assert {o["indicator"] for o in spec["sub"]["options"]} == {"macd", "kdj", "rsi", "wr"}
