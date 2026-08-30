"""G10 分析输出测试（组合聚合 + 统一导出）。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import csv
import io
import json

import pytest

from app.analysis.portfolio import Position, _vol, aggregate
from app.export import to_csv, to_excel, to_json


# ============================ 组合聚合 ======================================
def test_aggregate_basic():
    pos = [
        Position(code="600519.SH", name="茅台", sector="白酒", qty=100, price=1600, cost=1200),
        Position(code="000001.SZ", name="平安", sector="银行", qty=1000, price=12, cost=10),
    ]
    out = aggregate(pos)
    assert out["total_market_value"] == 160000 + 12000
    assert out["total_cost"] == 120000 + 10000
    assert out["total_pnl"] == 42000
    assert out["position_count"] == 2
    assert out["items"][0]["weight"] == pytest.approx(round(160000 / 172000, 4), abs=1e-9)
    assert out["items"][0]["pnl_pct"] == pytest.approx(33.33, rel=1e-2)
    # 行业暴露：白酒市值权重
    sector = {s["sector"]: s for s in out["sector_exposure"]}
    assert sector["白酒"]["weight"] == pytest.approx(round(160000 / 172000, 4), abs=1e-9)
    # 无 K 线 → 风险贡献为 None（不估算）
    assert out["risk_contribution"] is None


def test_aggregate_empty_sector_default():
    pos = [Position(code="A", qty=1, price=10, cost=5)]
    out = aggregate(pos)
    assert out["sector_exposure"][0]["sector"] == "未分类"


def test_aggregate_risk_contribution():
    """有 K 线 → 风险贡献按 w_i*σ_i 归一化，波动更大的标的贡献更高。"""
    bars_low = [{"time": f"2026{i:03d}", "open": 10, "high": 11, "low": 9,
                 "close": 10 + (0.1 if i % 2 else -0.1), "volume": 100} for i in range(60)]
    bars_high = [{"time": f"2026{i:03d}", "open": 10, "high": 12, "low": 8,
                  "close": 10 + (2.0 if i % 2 else -2.0), "volume": 100} for i in range(60)]
    pos = [Position(code="L", name="低波", qty=100, price=10, cost=9),
           Position(code="H", name="高波", qty=100, price=10, cost=9)]
    out = aggregate(pos, bars_by_code={"L": bars_low, "H": bars_high})
    rc = {r["code"]: r for r in out["risk_contribution"]}
    assert rc["H"]["contribution_pct"] > rc["L"]["contribution_pct"]
    total = sum(r["contribution_pct"] for r in out["risk_contribution"])
    assert total == pytest.approx(100.0, rel=1e-2)


def test_vol_insufficient_data():
    assert _vol([{"time": "x", "close": 1}]) is None


# ============================ 统一导出 ======================================
def test_to_csv_columns_and_order():
    rows = [{"code": "600519.SH", "close": 1600.5, "name": "茅台"},
            {"code": "000001.SZ", "close": 12.0, "name": "平安"}]
    cols = [{"key": "code", "label": "代码"}, {"key": "close", "label": "收盘"}]
    text = to_csv(rows, cols)
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["代码", "收盘"]           # 标签行 + 列序固定
    assert parsed[1] == ["600519.SH", "1600.5"]
    assert len(parsed) == 3


def test_to_csv_missing_value_empty():
    text = to_csv([{"a": 1}], [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    assert "1," in text and text.rstrip().endswith(",")


def test_to_json_roundtrip():
    text = to_json([{"code": "600519.SH", "close": 1600.5}])
    assert json.loads(text) == [{"code": "600519.SH", "close": 1600.5}]


def test_to_excel_writes_file(tmp_path):
    rows = [{"code": "A", "v": 1}, {"code": "B", "v": 2}]
    cols = [{"key": "code", "label": "代码"}, {"key": "v", "label": "值"}]
    p = tmp_path / "out.xlsx"
    to_excel(rows, cols, str(p))
    assert p.exists() and p.stat().st_size > 0
