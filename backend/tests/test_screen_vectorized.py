"""选股向量化 / 指标去重（D-J §J.10）：同一 (指标, 参数) 一次求值只算一次。

同时验证 evaluate 与「逐只实现」语义一致（矩阵求值 == 逐只实现，本引擎逐只但共享缓存）。
"""
from app.screener.engine import evaluate_scan
from app.screener import conditions as cond_mod
from _phase4_support import make_bar


def test_evaluate_scan_basic():
    bars = [make_bar(10, time_="t1"), make_bar(20, time_="t2")]
    cond = {"field": {"name": "close", "op": "gt", "value": 10, "window": -1}}
    res, scanned, _ = evaluate_scan(["A"], {"A": bars}, cond)
    assert len(res) == 1 and res[0]["code"] == "A"
    assert scanned == 1


def test_evaluate_scan_no_hit():
    bars = [make_bar(10, time_="t1"), make_bar(5, time_="t2")]
    cond = {"field": {"name": "close", "op": "gt", "value": 10, "window": -1}}
    res, scanned, _ = evaluate_scan(["B"], {"B": bars}, cond)
    # scanned = **已评估**标的数（≠ 命中数）：未命中仍计入「已扫描」，
    # 前端头部「命中 N 只（扫描 M 只）」依赖此语义。
    assert res == [] and scanned == 1


def test_evaluate_scan_scanned_counts_evaluated_not_hits():
    """scanned 与命中数解耦：全部未命中时 scanned 仍等于已评估的标的数。"""
    bars = [make_bar(10, time_="t1"), make_bar(20, time_="t2")]
    hit_cond = {"field": {"name": "close", "op": "gt", "value": 15, "window": -1}}
    res, scanned, _ = evaluate_scan(["A", "B"], {"A": bars, "B": bars}, hit_cond)
    assert len(res) == 2 and scanned == 2

    miss_cond = {"field": {"name": "close", "op": "gt", "value": 100, "window": -1}}
    res2, scanned2, _ = evaluate_scan(["A", "B"], {"A": bars, "B": bars}, miss_cond)
    assert res2 == [] and scanned2 == 2


def test_indicator_computed_once_per_unique_spec():
    """两个叶子都引用 MA(20) → calc 只调用一次（去重）。"""
    orig = cond_mod.calc
    calls = []

    def counting(name, bars, **kw):
        calls.append((name, tuple(sorted(kw.items()))))
        return orig(name, bars, **kw)

    cond_mod.calc = counting
    try:
        bars = [make_bar(10, time_=f"t{i}") for i in range(30)]
        # 两个叶子同指 MA(20)
        cond = {"and": [
            {"indicator": {"name": "ma", "params": {"win": 20}, "output": "ma",
                          "op": "gt", "value": 0, "window": -1}},
            {"indicator": {"name": "ma", "params": {"win": 20}, "output": "ma",
                          "op": "lt", "value": 100, "window": -1}},
        ]}
        evaluate_scan(["A"], {"A": bars}, cond)
    finally:
        cond_mod.calc = orig
    ma_calls = [c for c in calls if c[0] == "ma"]
    assert len(ma_calls) == 1, ma_calls


def test_min_price_prefilter():
    bars = [make_bar(10, time_="t1"), make_bar(20, time_="t2")]
    cond = {"field": {"name": "close", "op": "gt", "value": 0, "window": -1}}
    res, _, _ = evaluate_scan(["A"], {"A": bars}, cond, min_price=100)
    assert res == []
