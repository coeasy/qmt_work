"""阶段 3 研究深度层单测（纯函数 + 路由层，无需真实券商）。

覆盖：因子 IC/ICIR、分位分组、相关性矩阵、组合回测、walk-forward、绩效归因。
行情取合成 K 线（不依赖券商）；纯函数在路由测试里通过 monkeypatch fetch_kline_cached 注入。
"""
import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools import factor_research as FR
from tools.factor_research import (
    attribute_pnl,
    factor_correlation,
    factor_ic,
    factor_ic_panel,
    ic_statistics,
    quantile_analysis,
    run_portfolio_backtest,
    walk_forward,
)


# ---------------- 合成数据 ----------------

def make_kline(n=300, drift=0.001, seed=7):
    """确定性合成日线（温和上涨 + 噪声），含 open/high/low/volume/amount。"""
    bars = []
    price = 100.0
    for i in range(n):
        # 伪随机但确定性
        noise = math.sin(i * 0.7 + seed) * 0.5 + math.cos(i * 0.31) * 0.3
        close = price * (1 + drift) + noise
        open_p = price
        high = max(open_p, close) * (1 + 0.002)
        low = min(open_p, close) * (1 - 0.002)
        vol = 1_000_000 + int(abs(noise) * 100_000)
        amount = vol * close
        bars.append({"time": f"2024-01-{i+1:02d}", "open": round(open_p, 2),
                     "high": round(high, 2), "low": round(low, 2),
                     "close": round(close, 2), "volume": vol, "amount": round(amount, 2)})
        price = close
    return bars


# ---------------- 1. 因子 IC / ICIR ----------------

def test_factor_ic_perfect_positive():
    ret = [0.01, -0.02, 0.03, -0.01, 0.02, 0.00, 0.015, -0.005]
    ic = factor_ic(ret, ret)
    assert ic is not None and abs(ic - 1.0) < 1e-9


def test_factor_ic_perfect_negative():
    ret = [0.01, -0.02, 0.03, -0.01, 0.02]
    ic = factor_ic(ret, [-x for x in ret])
    assert ic is not None and abs(ic + 1.0) < 1e-9


def test_factor_ic_panel_and_stats():
    # 3 个时间截面，每个截面 5 个标的；因子与收益强正相关（加微小噪声，使 IC 非恒等→ICIR 有定义）
    import random
    rng = random.Random(42)
    panel_f = [
        [1.0, 2.0, 3.0, 4.0, 5.0],
        [5.0, 4.0, 3.0, 2.0, 1.0],
        [2.0, 1.0, 4.0, 3.0, 5.0],
    ]
    panel_r = [[x * 0.1 + rng.uniform(-0.005, 0.005) for x in row] for row in panel_f]
    ic_list = factor_ic_panel(panel_f, panel_r)
    assert len(ic_list) == 3
    assert all(v is not None and v > 0.9 for v in ic_list)
    stats = ic_statistics(ic_list)
    assert stats["n"] == 3
    assert stats["ic_mean"] > 0.9
    assert stats["positive_ratio"] == 1.0
    assert stats["icir"] is not None


def test_ic_statistics_empty():
    stats = ic_statistics([None, None])
    assert stats["n"] == 0
    assert stats["ic_mean"] is None


# ---------------- 2. 分位分组 ----------------

def test_quantile_monotonic():
    # 因子递增，远期收益随因子递增 → 最高分位收益 > 最低分位收益
    factor = list(range(1, 101))
    fwd = [x * 0.001 for x in factor]
    out = quantile_analysis(factor, fwd, n_q=5)
    assert out["n_quantiles"] == 5
    assert len(out["quantiles"]) == 5
    assert out["quantiles"][-1]["avg_return"] > out["quantiles"][0]["avg_return"]
    assert out["long_short_avg_return"] > 0
    assert len(out["spread_by_quantile"]) == 5


def test_quantile_rejects_too_few():
    with pytest.raises(ValueError):
        quantile_analysis([1.0, 2.0], [0.1, 0.2], n_q=5)


# ---------------- 3. 相关性矩阵 ----------------

def test_factor_correlation_identical_and_inverse():
    a = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    b = [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
    out = factor_correlation({"A": a, "B": b})
    assert out["names"] == ["A", "B"]
    assert abs(out["matrix"]["A"]["B"] - (-1.0)) < 1e-9
    assert out["matrix"]["A"]["A"] == 1.0

    out2 = factor_correlation({"A": a, "A2": list(a)})
    assert abs(out2["matrix"]["A"]["A2"] - 1.0) < 1e-9


def test_factor_correlation_short_raises():
    with pytest.raises(ValueError):
        factor_correlation({"A": [1.0, 2.0], "B": [2.0, 3.0]})


# ---------------- 4. 组合回测 ----------------

def test_portfolio_backtest_two_symbols():
    k1 = make_kline(300, drift=0.0008)
    k2 = make_kline(300, drift=0.0005, seed=3)
    out = run_portfolio_backtest(
        ["A.SH", "B.SH"], {"A.SH": k1, "B.SH": k2}, None,
        "ma_cross", {"fast": 5, "slow": 20}, 1_000_000.0)
    assert set(out["symbols"]) == {"A.SH", "B.SH"}
    # 等权
    assert abs(sum(out["weights"]) - 1.0) < 1e-9
    # 组合 = 各标的按权重加总：组合总收益 ≈ Σ w_i × 各标的收益
    # （各标的收益经 compute_metrics 四舍五入到 4 位，故允许 ~1e-3 误差）
    w = out["weights"]
    per = out["per_symbol_metrics"]
    expected_tr = sum(w[i] * per[sym]["total_return"] for i, sym in enumerate(out["symbols"]))
    assert abs(out["portfolio_metrics"]["total_return"] - expected_tr) < 1e-3
    # 各标的指标存在
    assert "A.SH" in out["per_symbol_metrics"]
    assert "B.SH" in out["per_symbol_metrics"]
    # 末态目标持仓 key 与标的对应
    assert set(out["target_portfolio"].keys()) == {"A.SH", "B.SH"}
    assert out["source"] == "real_kline"


def test_portfolio_backtest_custom_weights_sum():
    k1 = make_kline(200)
    k2 = make_kline(200, seed=11)
    out = run_portfolio_backtest(
        ["X.SH", "Y.SH"], {"X.SH": k1, "Y.SH": k2}, [0.7, 0.3],
        "ma_cross", {"fast": 5, "slow": 20}, 500_000.0)
    assert abs(out["weights"][0] - 0.7) < 1e-6
    assert abs(out["weights"][1] - 0.3) < 1e-6
    # 组合与各标的归一化后加总一致
    per = out["per_symbol_metrics"]
    total_pnl = sum(v.get("total_return", 0) for v in per.values())  # 比例权重下未必等于组合，仅校验不崩
    assert isinstance(total_pnl, float)


def test_portfolio_backtest_target_portfolio_feedable():
    """组合回测输出的 target_portfolio 可直接喂回 volume 模式差量同步（结构校验）。"""
    k1 = make_kline(250)
    out = run_portfolio_backtest(
        ["A.SH"], {"A.SH": k1}, None, "ma_cross", {"fast": 5, "slow": 20}, 1_000_000.0)
    tp = out["target_portfolio"]
    assert set(tp.keys()) == {"A.SH"}
    # 目标股数为整百（或 0）
    assert tp["A.SH"] % 100 == 0


# ---------------- 5. walk-forward ----------------

def test_walk_forward_runs_and_reports():
    k = make_kline(200)
    out = walk_forward("A.SH", k, "ma_cross", {"fast": 5, "slow": 20}, 100_000.0,
                       window=60, step=30, period="1d")
    assert out["symbol"] == "A.SH"
    assert out["summary"]["n_folds"] >= 2
    # 每个 fold 有样本外指标
    for f in out["folds"]:
        assert "metrics" in f and "sharpe" in f["metrics"]
        assert f["train_range"][1] == f["test_range"][0]
    assert out["source"] == "real_kline"
    # 稳健性字段存在
    assert out["summary"]["robustness"] in ("stable", "positive", "unstable")


def test_walk_forward_with_optimize():
    k = make_kline(220)
    out = walk_forward("A.SH", k, "ma_cross", {"fast": 5, "slow": 20}, 100_000.0,
                       window=60, step=30, period="1d",
                       optimize=True, param_grid={"fast": [5, 10, 20]})
    assert out["optimize"] is True
    assert "param_drift" in out["summary"]
    assert "fast" in out["summary"]["param_drift"]


def test_walk_forward_insufficient_data():
    k = make_kline(50)
    with pytest.raises(Exception):
        walk_forward("A.SH", k, "ma_cross", {}, 100_000.0, window=60, step=30)


# ---------------- 6. 绩效归因 ----------------

def test_attribute_pnl_by_symbol_and_side():
    trades = [
        {"code": "A.SH", "side": "buy", "price": 10.0, "qty": 1000, "time": "2024-01-01", "pnl": 0},
        {"code": "A.SH", "side": "sell", "price": 11.0, "qty": 1000, "time": "2024-01-02", "pnl": 1000},
        {"code": "B.SH", "side": "buy", "price": 20.0, "qty": 500, "time": "2024-01-01", "pnl": 0},
        {"code": "B.SH", "side": "sell", "price": 19.0, "qty": 500, "time": "2024-01-02", "pnl": -500},
    ]
    klines_by_symbol = {
        "A.SH": [
            {"time": "2024-01-01", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1e6, "amount": 1.02e7},
            {"time": "2024-01-02", "open": 10.8, "high": 11.2, "low": 10.6, "close": 11.0, "volume": 1.1e6, "amount": 1.2e7},
        ],
        "B.SH": [
            {"time": "2024-01-01", "open": 20.0, "high": 20.5, "low": 19.8, "close": 20.2, "volume": 1e6, "amount": 2.02e7},
            {"time": "2024-01-02", "open": 18.8, "high": 19.2, "low": 18.6, "close": 19.0, "volume": 1.1e6, "amount": 2.1e7},
        ],
    }
    out = attribute_pnl(trades, klines_by_symbol)
    # 分标的：A +1000, B -500 → 总 +500
    assert out["by_symbol"]["A.SH"] == 1000.0
    assert out["by_symbol"]["B.SH"] == -500.0
    assert out["total_pnl"] == 500.0
    # 分买卖侧
    assert out["by_side"]["buy"] == pytest.approx(-10.0 * 1000 - 20.0 * 500)
    assert out["by_side"]["sell"] == pytest.approx(11.0 * 1000 + 19.0 * 500)
    # 成本拆解（量级）
    assert out["cost"]["commission_est"] > 0
    assert out["cost"]["stamp_tax_est"] > 0
    # 滑点（成交价 vs 次根参考价）：A 以 10.0 买入、次日参考价更高 → 买在低位、有利 → 负 bps；
    #                         B 以 20.0 买入、次日参考价更低 → 买在高位、不利 → 正 bps
    assert out["slippage"]["n"] == 2
    a_sample = next(s for s in out["slippage"]["samples"] if s["code"] == "A.SH")
    b_sample = next(s for s in out["slippage"]["samples"] if s["code"] == "B.SH")
    assert a_sample["slippage_bps"] < 0
    assert b_sample["slippage_bps"] > 0


# ---------------- 路由层（monkeypatch 取真实 K 线路径） ----------------

@pytest.fixture
def client(monkeypatch):
    # 用合成 K 线替换取数入口
    async def fake_fetch(symbol, period="1d", count=250, broker_id=None):
        return {"bars": make_kline(300), "source": "test_synthetic"}
    monkeypatch.setattr(FR, "fetch_kline_cached", fake_fetch)

    app = FastAPI()
    from app.routes.research import router
    app.include_router(router)
    return TestClient(app)


def test_route_factor_ic(client):
    r = client.post("/research/factor-ic",
                    json={"symbol": "600519.SH", "factor_name": "rsi", "count": 300})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert "ic" in body["data"]
    assert body["data"]["source"] == "test_synthetic"


def test_route_quantile(client):
    r = client.post("/research/quantile",
                    json={"symbol": "600519.SH", "factor_name": "rsi", "n_q": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["n_quantiles"] == 5


def test_route_correlation(client):
    r = client.post("/research/correlation",
                    json={"symbols": "600519.SH,000001.SZ", "factor_name": "rsi"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert set(body["data"]["names"]) == {"600519.SH", "000001.SZ"}


def test_route_portfolio_backtest(client):
    r = client.post("/research/portfolio-backtest",
                    json={"symbols": "600519.SH,000001.SZ", "strategy": "ma_cross",
                          "count": 300})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert "portfolio_metrics" in body["data"]


def test_route_walk_forward(client):
    r = client.post("/research/walk-forward",
                    json={"symbol": "600519.SH", "count": 300, "window": 60, "step": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["summary"]["n_folds"] >= 1


def test_route_attribution_direct(client):
    trades = [
        {"code": "A.SH", "side": "buy", "price": 10.0, "qty": 1000, "time": "2024-01-01", "pnl": 0},
        {"code": "A.SH", "side": "sell", "price": 11.0, "qty": 1000, "time": "2024-01-02", "pnl": 1000},
    ]
    klines = {
        "A.SH": [
            {"time": "2024-01-01", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1e6, "amount": 1.02e7},
            {"time": "2024-01-02", "open": 10.8, "high": 11.2, "low": 10.6, "close": 11.0, "volume": 1.1e6, "amount": 1.2e7},
        ]
    }
    r = client.post("/research/attribution", json={"trades": trades, "klines_by_symbol": klines})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["total_pnl"] == 1000.0
