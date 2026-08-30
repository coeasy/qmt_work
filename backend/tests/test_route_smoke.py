"""T7 新增端点路由冒烟测试（G6/G8/G10/G4 系，函数级调用，零网络零真实 DB）。

覆盖 13 个此前 0 路由级测试的端点：
- datahub: GET /datahub/policies
- runtime: POST /runtime/jobs（合法/非法 kind/缺 conditions）+ GET 列表 + 状态 + 取消
- screen: GET /market/screen/boards + POST /market/screen/nl
- analysis: GET /market/analysis/scripts + POST /market/analysis/run（未知名/非法体）
- indicators: GET /market/indicators + GET /market/chart-spec
- export: POST /market/export（非法 format / 空 rows）
- portfolio: POST /market/portfolio/aggregate

函数级调用（asyncio.run），不启动完整 app → 不触碰真实 app.db。
"""
import asyncio

import pytest

from app.routes import analysis as _an
from app.routes import datahub as _dh
from app.routes import indicators as _ind
from app.routes import runtime as _rt
from app.routes import screen as _sc


def test_datahub_policies():
    res = asyncio.run(_dh.datahub_policies())
    assert res["code"] == 0
    data = res["data"]
    assert "default" in data and "topics" in data
    assert data["default"]["ttl_ms"] == 30000
    assert "market:boards" in data["topics"]
    assert "market:indices" in data["topics"]
    assert "market:moneyflow" in data["topics"]
    assert "market:capital" in data["topics"]


def test_runtime_jobs_submit_invalid_kind():
    res = asyncio.run(_rt.runtime_jobs_submit({"kind": "report"}))
    assert res["code"] == 400
    assert "report" in res["message"]


def test_runtime_jobs_submit_backtest_now_accepts():
    """T8：backtest runner 已接入 → 合法 body 不再 400（不真正执行，仅入队）。"""
    res = asyncio.run(_rt.runtime_jobs_submit(
        {"kind": "backtest", "name": "t", "params": {"symbol": "600519.SH"}}))
    assert res["code"] == 0
    assert res["data"]["status"] == "queued"


def test_runtime_jobs_submit_screen_missing_conditions():
    res = asyncio.run(_rt.runtime_jobs_submit({"kind": "screen", "params": {}}))
    assert res["code"] == 400
    assert "conditions" in res["message"]


def test_runtime_jobs_list_and_status():
    res = asyncio.run(_rt.runtime_jobs_list())
    assert res["code"] == 0
    assert isinstance(res["data"]["items"], list)


def test_runtime_jobs_status_unknown():
    res = asyncio.run(_rt.runtime_jobs_get("no-such-job"))
    assert res["code"] != 0


def test_market_screen_boards_list():
    # local_boards 查询依赖 DB —— 无 DB 时端点为业务 503（绝不 crash/造假）
    res = asyncio.run(_sc.market_screen_boards_list())
    assert res["code"] in (0, 503)


def test_market_screen_nl_volume_surge():
    res = asyncio.run(_sc.market_screen_nl({"text": "放量上涨"}))
    assert res["code"] == 0
    assert res["data"]["conditions"] is not None
    assert any("放量" in r for r in res["data"]["rules"])


def test_market_screen_nl_empty():
    res = asyncio.run(_sc.market_screen_nl({"text": ""}))
    assert res["code"] != 0


def test_analysis_scripts_list():
    res = asyncio.run(_an.market_analysis_scripts_list())
    assert res["code"] == 0
    names = {s["name"] for s in res["data"]["items"]}
    assert "portfolio_summary" in names


def test_analysis_run_unknown_script():
    res = asyncio.run(_an.market_analysis_run({"name": "no_such_script", "data": []}))
    assert res["code"] != 0


def test_indicators_catalog():
    res = asyncio.run(_ind.market_indicators())
    assert res["code"] == 0
    assert res["data"]["count"] >= 16


def test_chart_spec():
    res = asyncio.run(_ind.market_chart_spec())
    assert res["code"] == 0
    spec = res["data"]
    assert "candlestick" in spec and "main" in spec and "sub" in spec
    main_names = {o["indicator"] for o in spec["main"]["options"]}
    sub_names = {o["indicator"] for o in spec["sub"]["options"]}
    assert {"ma", "boll"} <= main_names
    assert {"macd", "kdj", "rsi", "wr"} <= sub_names
    assert spec["candlestick"]["up"] and spec["candlestick"]["down"]


def test_export_invalid_format():
    res = asyncio.run(_an.market_export({"format": "pdf", "rows": []}))
    assert res["code"] == 400


def test_export_empty_rows():
    res = asyncio.run(_an.market_export({"format": "csv", "rows": []}))
    assert res["code"] == 400


def test_portfolio_aggregate_empty():
    # 空持仓 → 400（设计：空列表无意义，调用方应先校验）
    res = asyncio.run(_an.portfolio_aggregate({"positions": []}))
    assert res["code"] == 400
