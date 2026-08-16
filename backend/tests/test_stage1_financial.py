"""阶段 1（A 股金融正确性）回归测试。

覆盖：板块涨跌停幅度、涨跌停价、整手、T+1 账本、可参数化撮合内核
（执行时点 / 涨跌停不可成交 / 成交量容量）、严谨指标（年化/alpha/beta/IR/Calmar）、
VWAP 真实分布降级、回测引擎集成 + 样本内/外切分。
"""
import math

from tools import ashare, metrics
from tools.ashare import (
    T1Ledger, board_limit_pct, is_limit_down, is_limit_up, limit_price,
    round_lot,
)
from tools.matching import MatchingConfig, simulate
from tools.backtest import run_backtest_engine


# ---------------- 合成行情 ----------------
def _kline(closes, opens=None, highs=None, lows=None, vols=None):
    n = len(closes)
    opens = opens or closes
    highs = highs or [max(c, o) for c, o in zip(closes, opens)]
    lows = lows or [min(c, o) for c, o in zip(closes, opens)]
    vols = vols or [1_000_000] * n
    return [{"time": f"2024-01-{i+1:02d}", "open": opens[i], "high": highs[i],
             "low": lows[i], "close": closes[i], "volume": vols[i]}
            for i in range(n)]


# ================= ashare 规则 =================
def test_board_limit_pct():
    assert board_limit_pct("600000.SH") == 0.10          # 主板
    assert board_limit_pct("300750.SZ") == 0.20          # 创业板
    assert board_limit_pct("688981.SH") == 0.20          # 科创板
    assert board_limit_pct("830799.BJ") == 0.30          # 北交所
    assert board_limit_pct("600000.SH", is_st=True) == 0.05  # ST


def test_limit_price_rounding():
    # 10.00 涨停 = 11.00（主板 10%）
    assert limit_price(10.0, "up", code="600000.SH") == 11.0
    # 33.33 * 1.20 = 39.996 -> 40.00（创业板 20%，四舍五入到分）
    assert limit_price(33.33, "up", code="300750.SZ") == 40.0
    # 跌停
    assert limit_price(10.0, "down", code="600000.SH") == 9.0


def test_is_limit_up_down():
    assert is_limit_up("600000.SH", 11.0, 10.0)
    assert not is_limit_up("600000.SH", 10.9, 10.0)
    assert is_limit_down("600000.SH", 9.0, 10.0)
    # 无昨收无法判定
    assert not is_limit_up("600000.SH", 11.0, None)


def test_round_lot():
    assert round_lot(12345, 100) == 12300
    assert round_lot(50, 100) == 0
    assert ashare.is_valid_lot(100) and not ashare.is_valid_lot(150)


def test_t1_ledger():
    led = T1Ledger()
    led.buy(1000, idx=5)           # T 日(idx5)买入
    assert led.sellable(5) == 0    # 当日不可卖
    assert led.sellable(6) == 1000 # T+1(idx6)可卖
    used = led.consume(400, idx=6)
    assert used == 400
    assert led.position() == 600
    assert led.sellable(6) == 600


# ================= 撮合内核 =================
def test_matching_close_vs_next_open():
    closes = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5]  # 单调上涨
    opens = [c + 0.3 for c in closes]               # 高开：次根开盘价高于当根收盘
    k = _kline(closes, opens)
    # 从第 2 根起持有（信号 0,1,1,1,1,1）
    sig = [0, 1, 1, 1, 1, 1]
    cfg_close = MatchingConfig(execution_timing="close")
    cfg_open = MatchingConfig(execution_timing="next_open")
    r_close = simulate(closes, sig, k, cfg_close, 100_000)
    r_open = simulate(closes, sig, k, cfg_open, 100_000)
    # next_open 在更高价位建仓（更晚成交且高开），成本应高于 close 口径
    close_buy = [t for t in r_close["trades"] if t["side"] == "buy"][0]
    open_buy = [t for t in r_open["trades"] if t["side"] == "buy"][0]
    assert open_buy["price"] > close_buy["price"]


def test_matching_enforce_limit_blocks_buy():
    # 第 1 根较第 0 根涨 10%（涨停），且信号要求买入
    closes = [10.0, 11.0]
    k = _kline(closes)
    sig = [0, 1]
    cfg_on = MatchingConfig(execution_timing="close", enforce_limit=True,
                            code="600000.SH")
    cfg_off = MatchingConfig(execution_timing="close", enforce_limit=False,
                             code="600000.SH")
    r_on = simulate(closes, sig, k, cfg_on, 100_000)
    r_off = simulate(closes, sig, k, cfg_off, 100_000)
    assert [t for t in r_on["trades"] if t["side"] == "buy"] == []   # 涨停买不进
    assert [t for t in r_off["trades"] if t["side"] == "buy"] != []  # 关闭约束可买


def test_matching_volume_capacity_spreads_fills():
    # 长上涨 + 极低容量上限 -> 买入分多根完成
    closes = [float(10 + i * 0.1) for i in range(60)]
    k = _kline(closes, vols=[2000] * 60)   # 每根量小
    sig = [0] + [1] * 59
    cfg = MatchingConfig(execution_timing="close",
                         max_participation_pct=0.1)  # 单根最多 10% 成交量
    r = simulate(closes, sig, k, cfg, 1_000_000)
    buys = [t for t in r["trades"] if t["side"] == "buy"]
    assert len(buys) > 1                 # 容量约束下分多根建仓
    # 每根买入量不超过该根成交量 * 10%
    for t in buys:
        assert t["qty"] <= 200           # 2000*0.1=200


def test_matching_lot_enforced():
    closes = [10.0, 11.0, 12.0, 13.0]
    k = _kline(closes)
    sig = [0, 1, 1, 1]
    cfg = MatchingConfig(execution_timing="close", min_lot=100)
    r = simulate(closes, sig, k, cfg, 100_000)
    for t in r["trades"]:
        assert t["qty"] % 100 == 0


# ================= 指标严谨性 =================
def test_annualization_factor():
    assert metrics.annualization_factor("1d") == 252
    assert metrics.annualization_factor("5m") == 252 * 48
    assert metrics.annualization_factor("1m") == 252 * 240
    assert metrics.annualization_factor("1w") == 52


def test_metrics_annual_uses_bar_freq():
    # 252 个日收益 0.001 -> 年化应等于 (1.001)^252-1
    rets = [0.001] * 252
    eq = [100_000.0]
    for r in rets:
        eq.append(eq[-1] * (1 + r))
    m = metrics.compute_metrics(eq, period="1d")
    expected = (1.001 ** 252) - 1
    assert abs(m["annual_return"] - expected) < 1e-3
    assert m["annualization"] == 252


def test_metrics_alpha_beta_ir():
    # 组合与基准完全同步 -> beta≈1, alpha≈0, IR 有限
    rets = [0.01, -0.005, 0.02, 0.0, -0.01, 0.015]
    eq = [100_000.0]
    for r in rets:
        eq.append(eq[-1] * (1 + r))
    bench = [r * 0.8 for r in rets]   # 基准波动更小
    m = metrics.compute_metrics(eq, trades=[], period="1d", benchmark_rets=bench)
    assert "beta" in m and "alpha" in m and "information_ratio" in m
    # 基准振幅为组合的 0.8 倍 -> 组合相对基准 beta≈1/0.8=1.25
    assert abs(m["beta"] - 1.25) < 0.05
    assert abs(m["alpha"]) < 0.05   # CAPM alpha 应近 0


def test_metrics_calmar_present():
    # 单调上涨 -> 无回撤 -> calmar 应 >= 0
    eq = [100_000 * (1.001 ** i) for i in range(100)]
    m = metrics.compute_metrics(eq, period="1d")
    assert "calmar" in m
    assert m["max_drawdown"] == 0.0
    assert m["calmar"] >= 0


# ================= VWAP 真实分布降级 =================
def test_vwap_plan_profile_source():
    from tools.algo import AlgoEngine
    # 提供真实分时量分布 -> source=profile
    plan, src = AlgoEngine._plan_vwap(10_000, 5, [100, 200, 300, 200, 100])
    assert src == "profile"
    assert sum(plan) == 10_000
    # 无 profile -> 降级 heuristic_utype，且两端重于中间
    plan2, src2 = AlgoEngine._plan_vwap(10_000, 5, None)
    assert src2 == "heuristic_utype"
    assert plan2[0] > plan2[2] and plan2[-1] > plan2[2]


# ================= 回测引擎集成 =================
def test_backtest_engine_runs():
    closes = [float(10 + math.sin(i / 5.0)) + 10 for i in range(120)]
    k = _kline(closes)
    res = run_backtest_engine("600000.SH", k, "ma_cross",
                              {"fast": 5, "slow": 20}, 100_000.0)
    assert "metrics" in res and res["metrics"]
    assert res["engine"] == "legacy"
    assert res["metrics"]["trade_count"] >= 0


def test_backtest_train_test_split():
    closes = [float(10 + math.sin(i / 5.0)) + 10 for i in range(200)]
    k = _kline(closes)
    res = run_backtest_engine("600000.SH", k, "ma_cross",
                              {"fast": 5, "slow": 20}, 100_000.0,
                              train_ratio=0.6)
    assert "train_test" in res
    assert "train" in res["train_test"] and "test" in res["train_test"]
    # 样本内长度应短于全样本
    assert res["train_test"]["train"]["trade_count"] <= res["trade_count"]


def test_backtest_vectorized_parity():
    closes = [float(10 + math.sin(i / 5.0)) + 10 for i in range(120)]
    k = _kline(closes)
    a = run_backtest_engine("600000.SH", k, "ma_cross", {"fast": 5, "slow": 20}, 100_000.0)
    b = __import__("tools.backtest", fromlist=["run_backtest_vectorized"]).run_backtest_vectorized(
        "600000.SH", k, "ma_cross", {"fast": 5, "slow": 20}, 100_000.0)
    # 双内核在同一撮合参数下成交数应一致（信号语义已对齐）
    assert a["trade_count"] == b["trade_count"]


# ================= 模拟盘 A 股规则（整手 + T+1） =================
def test_paper_t1_and_lot():
    from paper.paper_engine import PaperEngine, _today_date
    import paper.paper_engine as pe

    eng = PaperEngine(initial_capital=1_000_000.0)
    # 控制「今天」：买入日为 D1
    pe._today_date = lambda: "2024-01-01"
    eng.submit_order("600000.SH", "buy", 10.0, 1000)
    # 整手校验：150 股非法
    try:
        eng.submit_order("600000.SH", "buy", 10.0, 150)
        assert False, "应拒绝非整手"
    except ValueError:
        pass
    # 当日买入不可卖（T+1）
    try:
        eng.submit_order("600000.SH", "sell", 11.0, 500)
        assert False, "应拒绝 T+1 当日卖出"
    except ValueError:
        pass
    # 次日可卖
    pe._today_date = lambda: "2024-01-02"
    r = eng.submit_order("600000.SH", "sell", 11.0, 500)
    assert r["status"] == "filled"
    assert r["volume"] == 500
    # 盯市来源标注
    acc = eng.get_account()
    assert acc["marking_source"] in ("live", "frozen")
    pe._today_date = _today_date  # 还原

