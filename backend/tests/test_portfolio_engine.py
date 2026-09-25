"""组合回测引擎（P1-3）单测：日期对齐 + 共享现金再平衡 + 换手/成本/归因。

★ 本文件守的是**恒等式**与**拒绝行为**，不是「跑通不崩」：
  - 净值恒等式   ``final_equity == final_cash + Σ 持仓市值``
  - 归因恒等式   ``Σ 分标的 total_pnl == Δequity``
  - 对齐恒等式   面板长度 == 各标的交易日**交集**；不重叠必须**报错**而不是填充
  - 参数拒绝     Σw>1 / 未知 rebalance / 缺标的 / weekly 遇不可解析日期
  - 再平衡语义   ``daily`` 必须把涨多的那只卖出去补跌的那只（袖套模式做不到）
  每条都能被一条断言证伪（变异验证见 ``output/_portfolio_mutation_check.py``）。

行情全部是合成 K 线或本地冷仓真实历史，**不连券商**。
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from tools import portfolio_engine as PE

BACKEND = Path(__file__).resolve().parents[1]
COLD_DB = BACKEND / "data" / "bars_cold.db"


# ============================================================================
# 合成数据
# ============================================================================
def mk_daily(n=200, *, drift=0.001, seed=7.0, start=date(2024, 1, 2),
             base=100.0, flat=False):
    """确定性日线：日期真实（可解析、可算周/月），价格温和趋势 + 噪声。"""
    bars = []
    price = base
    d = start
    for i in range(n):
        noise = (0.0 if flat else
                 (math.sin(i * 0.7 + seed) * 0.5 + math.cos(i * 0.31 + seed) * 0.3))
        close = base if flat else price * (1 + drift) + noise
        o = price
        h = max(o, close) * 1.002
        lo = min(o, close) * 0.998
        v = 1_000_000 + int(abs(noise) * 100_000)
        bars.append({"time": d.isoformat(), "open": round(o, 2), "high": round(h, 2),
                     "low": round(lo, 2), "close": round(close, 2),
                     "volume": v, "amount": v * close})
        price = close
        d = d + timedelta(days=1)
    return bars


def _seq_signals(seqs):
    """按调用顺序依次返回给定信号 —— 引擎对 symbols 的顺序遍历是确定的。

    为什么需要它：本文件要验的是**组合机制**（权重/现金/再平衡/成本），
    信号本身已由 tools.indicators 的测试覆盖。用固定信号才能构造出
    「一只涨一只平」这类可判定的场景。
    """
    it = iter(seqs)

    def fake(strategy, closes, params):  # noqa: ARG001
        return next(it)

    return fake


def _last_close(bars):
    return bars[-1]["close"]


# ============================================================================
# 1. 对齐
# ============================================================================
def test_align_panel_uses_intersection_and_reports_excluded():
    a = mk_daily(200, seed=1)
    b = mk_daily(200, seed=2, start=date(2024, 3, 1))  # 晚两个月
    panel = PE.align_panel({"A.SH": a, "B.SH": b}, min_overlap=30)
    da = {x["time"] for x in a}
    db = {x["time"] for x in b}
    assert panel["dates"] == sorted(da & db)          # ★ 交集，不是并集
    assert len(panel["dates"]) == len(da & db)
    info = panel["alignment"]["per_symbol"]
    assert info["A.SH"]["n_excluded_nonoverlap"] == len(da - db)
    assert info["B.SH"]["n_excluded_nonoverlap"] == len(db - da)
    assert info["A.SH"]["excluded_range"] is not None


def test_align_panel_rejects_non_overlapping():
    """零重叠必须报错 —— 填充一根编造的 bar 比少跑一段更危险。"""
    a = mk_daily(200, seed=1)
    b = mk_daily(200, seed=2, start=date(2025, 6, 1))
    with pytest.raises(ValueError) as ei:
        PE.align_panel({"A.SH": a, "B.SH": b}, min_overlap=30)
    assert "共同交易日" in str(ei.value)


def test_align_panel_keeps_intraday_distinct():
    """★ 分钟线不能被压成一天：同一天不同时刻必须是不同的时间轴键。"""
    bars = [{"time": "2024-01-05 09:31:00", "close": 10.0, "volume": 1},
            {"time": "2024-01-05 09:32:00", "close": 10.5, "volume": 1},
            {"time": "2024-01-05 09:33:00", "close": 11.0, "volume": 1}]
    panel = PE.align_panel({"A.SH": bars}, min_overlap=2)
    assert len(panel["dates"]) == 3, f"分钟线被压成一天：{panel['dates']}"
    assert panel["bars"]["A.SH"][-1]["close"] == 11.0
    assert panel["alignment"]["per_symbol"]["A.SH"]["n_collapsed_same_key"] == 0


def test_align_panel_reports_collapsed_same_key():
    """同键重复（归档里同一 dt 出现两次）保留最后一根，但**必须报出数量**。"""
    bars = [{"time": "2024-01-05", "close": 10.0},
            {"time": "2024-01-05", "close": 12.0},
            {"time": "2024-01-08", "close": 13.0}]
    panel = PE.align_panel({"A.SH": bars}, min_overlap=2)
    assert panel["alignment"]["per_symbol"]["A.SH"]["n_collapsed_same_key"] == 1
    assert panel["bars"]["A.SH"][0]["close"] == 12.0


def test_align_panel_drops_bars_without_close_or_date():
    bars = [{"time": "2024-01-05", "close": 10.0},
            {"time": "2024-01-08"},                 # 无 close
            {"close": 11.0},                        # 无日期
            {"time": "2024-01-09", "close": None}]  # close=None
    panel = PE.align_panel({"A.SH": bars}, min_overlap=1)
    info = panel["alignment"]["per_symbol"]["A.SH"]
    assert info["n_dropped_unusable"] == 3
    assert len(panel["dates"]) == 1


# ============================================================================
# 2. 引擎恒等式
# ============================================================================
def test_engine_equity_identity_cash_plus_market_value():
    k = {"A.SH": mk_daily(150, seed=7), "B.SH": mk_daily(150, seed=3, drift=0.0005)}
    out = PE.run_portfolio_engine(["A.SH", "B.SH"], k, [0.6, 0.4], "ma_cross",
                                  {"fast": 5, "slow": 20}, 1_000_000.0,
                                  rebalance="daily")
    lhs = out["final_cash"] + sum(out["final_positions"][s] * _last_close(k[s])
                                  for s in k)
    assert abs(lhs - out["final_equity"]) < 0.01, (
        f"净值恒等式不成立：cash+Σmv={lhs} vs final_equity={out['final_equity']}")


def test_engine_attribution_identity_sums_to_equity_change():
    k = {"A.SH": mk_daily(150, seed=7), "B.SH": mk_daily(150, seed=3, drift=0.0005)}
    out = PE.run_portfolio_engine(["A.SH", "B.SH"], k, None, "ma_cross",
                                  {"fast": 5, "slow": 20}, 1_000_000.0,
                                  rebalance="signal")
    tot = sum(v["total_pnl"] for v in out["attribution"].values())
    delta = out["final_equity"] - out["initial_capital"]
    assert abs(tot - delta) < 1.0, (
        f"归因恒等式不成立：Σpnl={tot} vs Δequity={delta}（差额 {tot - delta}）")


def test_engine_cost_breakdown_adds_up():
    k = {"A.SH": mk_daily(150, seed=7)}
    out = PE.run_portfolio_engine(["A.SH"], k, None, "ma_cross",
                                  {"fast": 5, "slow": 20}, 1_000_000.0)
    c = out["cost"]
    assert abs(c["total"] - (c["commission"] + c["stamp_tax"] + c["slippage"])) < 0.02
    if out["trade_count"]:
        assert c["total"] > 0 and out["turnover"]["total_notional"] > 0


def test_engine_all_trades_are_whole_lots():
    k = {"A.SH": mk_daily(150, seed=7), "B.SH": mk_daily(150, seed=3)}
    out = PE.run_portfolio_engine(["A.SH", "B.SH"], k, None, "ma_cross",
                                  {"fast": 5, "slow": 20}, 1_000_000.0,
                                  rebalance="daily")
    assert out["trade_count"] > 0
    for t in out["trades"]:
        assert t["qty"] > 0 and t["qty"] % 100 == 0, t


# ============================================================================
# 3. 参数拒绝
# ============================================================================
def test_engine_rejects_weight_sum_over_one():
    k = {"A.SH": mk_daily(150, seed=1), "B.SH": mk_daily(150, seed=2)}
    with pytest.raises(ValueError) as ei:
        PE.run_portfolio_engine(["A.SH", "B.SH"], k, [1.2, 0.5], "ma_cross",
                                {"fast": 5, "slow": 20}, 1_000_000.0)
    assert "杠杆" in str(ei.value)


def test_engine_accepts_weight_sum_below_one_as_cash():
    """Σw<1 是合法意图（留现金），**不得**被悄悄归一化到 1。"""
    k = {"A.SH": mk_daily(150, seed=1), "B.SH": mk_daily(150, seed=2)}
    out = PE.run_portfolio_engine(["A.SH", "B.SH"], k, [0.3, 0.3], "ma_cross",
                                  {"fast": 5, "slow": 20}, 1_000_000.0,
                                  rebalance="none")
    assert out["weights"] == [0.3, 0.3]
    invested = sum(out["final_positions"][s] * _last_close(k[s]) for s in k)
    assert invested <= 0.6 * out["initial_capital"] + 1.0


def test_engine_rejects_missing_symbol():
    k = {"A.SH": mk_daily(150, seed=1), "ZZ.SH": []}
    with pytest.raises(ValueError) as ei:
        PE.run_portfolio_engine(["A.SH", "ZZ.SH"], k, None, "ma_cross",
                                {"fast": 5, "slow": 20}, 1_000_000.0)
    assert "ZZ.SH" in str(ei.value)


def test_engine_rejects_unknown_rebalance():
    k = {"A.SH": mk_daily(150, seed=1)}
    with pytest.raises(ValueError) as ei:
        PE.run_portfolio_engine(["A.SH"], k, None, "ma_cross",
                                {"fast": 5, "slow": 20}, 1_000_000.0,
                                rebalance="every-full-moon")
    assert "rebalance" in str(ei.value)


def test_engine_weekly_requires_parseable_dates():
    """★ 不可解析日期 + weekly ⇒ 报错，绝不静默降级成「每根都调」。"""
    bars = [{"time": f"bar-{i}", "close": 100.0 + i, "volume": 1_000_000}
            for i in range(40)]
    with pytest.raises(ValueError) as ei:
        PE.run_portfolio_engine(["A.SH"], {"A.SH": bars}, None, "ma_cross",
                                {"fast": 5, "slow": 20}, 100_000.0, rebalance="weekly")
    assert "无法解析" in str(ei.value)


def test_engine_rejects_non_sequence_weights():
    """weights 传成标量（例如把 initial_capital 抄到了 weights 位）要报清楚。"""
    k = {"A.SH": mk_daily(150, seed=1)}
    with pytest.raises(ValueError) as ei:
        PE.run_portfolio_engine(["A.SH"], k, 1_000_000.0, "ma_cross",
                                {"fast": 5, "slow": 20}, 1_000_000.0)
    assert "数值序列" in str(ei.value)


# ============================================================================
# 4. 再平衡语义（共享现金 vs 袖套）
# ============================================================================
def test_engine_rebalance_none_trades_only_on_first_bar(monkeypatch):
    k = {"A.SH": mk_daily(120, seed=1), "B.SH": mk_daily(120, seed=2)}
    monkeypatch.setattr(PE, "signals_for", _seq_signals([[1] * 120, [1] * 120]))
    out = PE.run_portfolio_engine(["A.SH", "B.SH"], k, None, "s", {},
                                  1_000_000.0, rebalance="none")
    assert out["trade_count"] > 0
    assert {t["date"] for t in out["trades"]} == {out["dates"][0]}
    assert out["turnover"]["n_rebalances"] == 1


def test_engine_daily_rebalance_sells_winner_to_buy_loser(monkeypatch):
    """★ 共享现金的核心可证伪点：涨多的那只必须被**卖出**去补另一只。

    袖套模式（factor_backtest）永远不会发生这件事 —— 每只标的各管各的钱。
    """
    k = {"WIN.SH": mk_daily(120, drift=0.004, seed=1, flat=False),
         "FLAT.SH": mk_daily(120, flat=True)}
    monkeypatch.setattr(PE, "signals_for", _seq_signals([[1] * 120, [1] * 120]))
    out = PE.run_portfolio_engine(["WIN.SH", "FLAT.SH"], k, [0.5, 0.5], "s", {},
                                  1_000_000.0, rebalance="daily")
    first = out["dates"][0]
    later_sells = [t for t in out["trades"]
                   if t["side"] == "sell" and t["code"] == "WIN.SH" and t["date"] != first]
    assert later_sells, "涨多的标的从未被卖出 ⇒ 权重没有被维持（不是共享现金组合）"
    assert out["turnover"]["n_rebalances"] > 5
    # 末态权重应被拉回目标附近（整手取整 ⇒ 给 3% 容差）
    mv = {s: out["final_positions"][s] * _last_close(k[s]) for s in k}
    total = out["final_equity"]
    assert abs(mv["WIN.SH"] / total - 0.5) < 0.03, mv
    assert abs(mv["FLAT.SH"] / total - 0.5) < 0.03, mv


def test_engine_signal_rebalance_fires_on_signal_change(monkeypatch):
    k = {"A.SH": mk_daily(120, seed=1)}
    sig = [0] * 60 + [1] * 60
    monkeypatch.setattr(PE, "signals_for", _seq_signals([sig]))
    out = PE.run_portfolio_engine(["A.SH"], k, None, "s", {}, 1_000_000.0,
                                  rebalance="signal")
    buys = [t for t in out["trades"] if t["side"] == "buy"]
    assert buys, "信号翻多后应当建仓"
    assert buys[0]["date"] == out["dates"][60]


def test_engine_liquidate_at_end_closes_everything(monkeypatch):
    k = {"A.SH": mk_daily(120, seed=1), "B.SH": mk_daily(120, seed=2)}
    monkeypatch.setattr(PE, "signals_for", _seq_signals([[1] * 120, [1] * 120]))
    out = PE.run_portfolio_engine(["A.SH", "B.SH"], k, None, "s", {},
                                  1_000_000.0, rebalance="none", liquidate_at_end=True)
    assert out["final_positions"] == {"A.SH": 0, "B.SH": 0}
    assert any(t.get("reason") == "liquidate" for t in out["trades"])
    assert abs(out["final_equity"] - out["final_cash"]) < 0.01


def test_engine_default_does_not_liquidate():
    """默认不强平：组合净值已含持仓市值，强平会污染换手/成本统计。"""
    k = {"A.SH": mk_daily(120, seed=1)}
    out = PE.run_portfolio_engine(["A.SH"], k, None, "ma_cross",
                                  {"fast": 5, "slow": 20}, 1_000_000.0)
    assert not any(t.get("reason") == "liquidate" for t in out["trades"])


# ============================================================================
# 5. 诊断（0 成交必须说清成因）
# ============================================================================
def test_engine_diagnostics_no_signal(monkeypatch):
    k = {"A.SH": mk_daily(120, seed=1)}
    monkeypatch.setattr(PE, "signals_for", _seq_signals([[0] * 120]))
    out = PE.run_portfolio_engine(["A.SH"], k, None, "s", {}, 1_000_000.0)
    assert out["trade_count"] == 0
    assert "未产生任何持仓信号" in out["diagnostics"]


def test_engine_diagnostics_insufficient_capital(monkeypatch):
    """★ 「本金不够买 1 手」必须与「策略没信号」分开说 —— 否则会被误读成策略不赚钱。"""
    k = {"A.SH": mk_daily(120, seed=1, base=100.0)}
    monkeypatch.setattr(PE, "signals_for", _seq_signals([[1] * 120]))
    out = PE.run_portfolio_engine(["A.SH"], k, None, "s", {}, 1_000.0)
    assert out["trade_count"] == 0
    assert "资金不足" in out["diagnostics"]


# ============================================================================
# 6. 真实本地历史数据（bars_cold.db，不连券商）
# ============================================================================
def _cold_codes(n=3):
    if not COLD_DB.exists():
        pytest.skip(f"缺少冷仓 {COLD_DB}")
    conn = sqlite3.connect(f"file:{COLD_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT code, COUNT(1) c FROM kline_archive WHERE period='1d' "
            "GROUP BY code HAVING c >= 200 ORDER BY code").fetchall()
    finally:
        conn.close()
    if len(rows) < n:
        pytest.skip("冷仓可用标的不足")
    return [r[0] for r in rows[:n]]


def test_load_bars_from_cold_db_real_history():
    codes = _cold_codes(3)
    data = PE.load_bars_from_cold_db(COLD_DB, codes, period="1d")
    assert set(data) == set(codes)
    for c in codes:
        assert len(data[c]) >= 200
        b = data[c][0]
        assert b["close"] > 0 and b["time"].isdigit() and len(b["time"]) == 8


def test_engine_runs_on_real_cold_db_data():
    codes = _cold_codes(3)
    data = PE.load_bars_from_cold_db(COLD_DB, codes, period="1d")
    out = PE.run_portfolio_engine(codes, data, None, "ma_cross",
                                  {"fast": 5, "slow": 20}, 1_000_000.0,
                                  rebalance="monthly")
    assert out["n_bars"] >= 30
    assert set(out["attribution"]) == set(codes)
    # 真实交易日：日期应是 8 位数字
    assert all(d.isdigit() and len(d) == 8 for d in out["dates"])
    # 恒等式在真实数据上同样成立
    lhs = out["final_cash"] + sum(out["final_positions"][s] * _last_close(data[s])
                                  for s in codes)
    assert abs(lhs - out["final_equity"]) < 0.02
    tot = sum(v["total_pnl"] for v in out["attribution"].values())
    assert abs(tot - (out["final_equity"] - out["initial_capital"])) < 1.0


def test_engine_real_data_alignment_is_intersection():
    codes = _cold_codes(3)
    data = PE.load_bars_from_cold_db(COLD_DB, codes, period="1d")
    panel = PE.align_panel(data, min_overlap=30)
    expect = set.intersection(*[{b["time"] for b in data[c]} for c in codes])
    assert set(panel["dates"]) == expect


# ============================================================================
# 7. 旧袖套接口的日期对齐修复（factor_backtest.run_portfolio_backtest）
# ============================================================================
def test_legacy_portfolio_backtest_aligns_dates_not_indices():
    """★ 旧实现按**索引**加总：两只标的区间不同时会把不同日期混在一起。

    修复后：输出 ``alignment``；组合净值长度 == 交易日**交集**（不是原始根数）；
    且各标的指标是**在对齐切片上**算出来的 —— 后者是行为级判据，单看长度不够。
    """
    from tools.backtest import run_backtest_vectorized
    from tools.factor_research import run_portfolio_backtest

    a = mk_daily(200, seed=1)
    b = mk_daily(200, seed=2, start=date(2024, 3, 1))
    expect = {x["time"] for x in a} & {x["time"] for x in b}
    out = run_portfolio_backtest(["A.SH", "B.SH"], {"A.SH": a, "B.SH": b}, None,
                                 "ma_cross", {"fast": 5, "slow": 20}, 1_000_000.0)
    assert out["alignment"]["n_common_dates"] == len(expect)
    assert out["alignment"]["per_symbol"]["A.SH"]["n_excluded_nonoverlap"] == len(
        {x["time"] for x in a} - expect)
    # 曲线长度必须恰好是「交集根数」（<200 时不该出现 200 点）
    assert len(out["portfolio_equity_curve"]) == min(200, len(expect))

    # ★ 行为级判据：把 A 单独喂**对齐切片**重算，收益必须与组合里的 A 分项一致。
    a_map = {x["time"]: x for x in a}
    aligned_a = [a_map[d] for d in sorted(expect)]
    ref = run_backtest_vectorized("A.SH", aligned_a, "ma_cross",
                                  {"fast": 5, "slow": 20}, 1_000_000.0)
    assert abs(out["per_symbol_metrics"]["A.SH"]["total_return"]
               - ref["metrics"]["total_return"]) < 1e-9, (
        "A 的分项指标不是在交集切片上算的 ⇒ 日期对齐没生效")


def test_legacy_portfolio_backtest_rejects_disjoint_ranges():
    from tools.factor_research import run_portfolio_backtest
    from xtquant_client.base import BrokerNotConnectedError

    a = mk_daily(200, seed=1)
    b = mk_daily(200, seed=2, start=date(2025, 6, 1))
    with pytest.raises(BrokerNotConnectedError):
        run_portfolio_backtest(["A.SH", "B.SH"], {"A.SH": a, "B.SH": b}, None,
                               "ma_cross", {"fast": 5, "slow": 20}, 1_000_000.0)


def test_legacy_portfolio_backtest_unchanged_for_equal_length():
    """等长输入下既有契约（Σ w_i × 袖套收益）必须**保持不变**。"""
    from tools.factor_research import run_portfolio_backtest

    a = mk_daily(200, seed=1)
    b = mk_daily(200, seed=2)
    out = run_portfolio_backtest(["A.SH", "B.SH"], {"A.SH": a, "B.SH": b}, [0.7, 0.3],
                                 "ma_cross", {"fast": 5, "slow": 20}, 500_000.0)
    assert abs(out["weights"][0] - 0.7) < 1e-6
    assert out["alignment"]["n_common_dates"] == 200
    per = out["per_symbol_metrics"]
    expect = sum(out["weights"][i] * per[s]["total_return"]
                 for i, s in enumerate(out["symbols"]))
    assert abs(out["portfolio_metrics"]["total_return"] - expect) < 1e-3


# ============================================================================
# 8. 路由：engine 分发（默认 sleeve 行为不变）
# ============================================================================
@pytest.fixture()
def research_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import tools.factor_research as FR
    from app.routes.research import router

    async def fake_fetch(symbol, period="1d", count=250, broker_id=None):  # noqa: ARG001
        # 两只标的给**不同根数**：路由层也必须走对齐，而不是按索引相加
        n = 200 if symbol.startswith("A") else 160
        return {"bars": mk_daily(n, seed=1 if symbol.startswith("A") else 2),
                "source": "test_synthetic"}

    monkeypatch.setattr(FR, "fetch_kline_cached", fake_fetch)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_route_default_engine_is_sleeve(research_client):
    r = research_client.post("/research/portfolio-backtest",
                             json={"symbols": "A.SH,B.SH", "strategy": "ma_cross",
                                   "count": 200})
    body = r.json()
    assert body["code"] == 0, body
    assert "portfolio_metrics" in body["data"]
    assert body["data"].get("engine") != "portfolio_shared_cash"
    # 不同根数 ⇒ 对齐后共同交易日 = 160
    assert body["data"]["alignment"]["n_common_dates"] == 160


def test_route_shared_cash_engine(research_client):
    r = research_client.post("/research/portfolio-backtest",
                             json={"symbols": "A.SH,B.SH", "strategy": "ma_cross",
                                   "engine": "shared_cash", "rebalance": "daily",
                                   "count": 200})
    body = r.json()
    assert body["code"] == 0, body
    d = body["data"]
    assert d["engine"] == "portfolio_shared_cash"
    for key in ("equity_curve", "turnover", "cost", "attribution", "final_positions"):
        assert key in d, f"shared_cash 引擎缺少 {key}"
    assert set(d["attribution"]) == {"A.SH", "B.SH"}


def test_route_unknown_engine_rejected(research_client):
    """★ 未知 engine 必须报 400，不静默回退到默认口径。"""
    r = research_client.post("/research/portfolio-backtest",
                             json={"symbols": "A.SH,B.SH", "engine": "magic"})
    body = r.json()
    assert body["code"] == 400, body
    assert "engine" in body["message"]
