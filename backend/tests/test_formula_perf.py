"""公式执行效率优化（2026-10-04 P0-A/B/C）的语义与契约钉子。

三块被钉死的行为：
1. ``registry.calc(raw=True)`` 与默认 list 路径**逐值等价**（NaN → None 语义一致），
   且默认路径（REST/MCP JSON 契约）行为完全不变；
2. 条件求值的字段整列缓存：同一 (bars, 字段) 一次求值只构建一次（P0-B）；
3. ``scan_async`` 结果级 TTL 缓存：命中带 ``cached_result``、参数变化 miss、
   TTL=0 禁用、过期淘汰（P0-C）——**不同 store 身份绝不串结果**。
"""
import asyncio

import pytest

from _phase4_support import make_bar


# ==================== P0-A：raw 路径语义等价 ====================
@pytest.mark.parametrize("name,params", [
    ("ma", {"period": 5}),
    ("ema", {"period": 5}),
    ("rsi", {"period": 14}),
    ("roc", {"period": 5}),
    ("boll", {"period": 10, "m": 2.0}),
    ("kdj", {}),
])
def test_calc_raw_matches_list_path(name, params):
    """raw=True 的 numpy 输出与默认 list 输出逐值等价（NaN ⇔ None 对齐）。"""
    import math

    from app.indicators import calc
    bars = [make_bar(10 + i * 0.37, time_=f"t{i}") for i in range(40)]
    as_list = calc(name, bars, **params)["outputs"]
    as_raw = calc(name, bars, raw=True, **params)["outputs"]
    assert set(as_list) == set(as_raw)
    for col in as_list:
        lv, rv = as_list[col], as_raw[col]
        assert len(lv) == len(rv)
        for a, b in zip(lv, rv):
            if a is None:
                assert b != b or b is None, (name, col)   # None ⇔ NaN
            else:
                assert b is not None and b == b
                assert math.isclose(a, float(b), rel_tol=1e-12, abs_tol=1e-12)


def test_calc_default_path_unchanged():
    """默认路径（JSON 契约）不返回 numpy：元素是 float/None。"""
    import numpy as np

    from app.indicators import calc
    bars = [make_bar(10 + i, time_=f"t{i}") for i in range(30)]
    out = calc("ma", bars, period=5)["outputs"]["ma"]
    assert not isinstance(out, np.ndarray)
    assert all(v is None or isinstance(v, float) for v in out)


def test_raw_nan_window_no_match():
    """raw numpy 路径下窗口 NaN → 不命中（与 list 路径 None → 不命中一致）。"""
    from app.screener.conditions import evaluate
    # window=0（首根）ma 不足窗口必为 NaN/None → 无论阈值多低都不命中
    cond = {"indicator": {"name": "ma", "params": {"win": 20}, "output": "ma",
                          "op": "gt", "value": -1e9, "window": 0}}
    bars = [make_bar(10, time_=f"t{i}") for i in range(30)]
    assert evaluate(cond, bars)[0] is False


# ==================== P0-B：字段整列缓存 ====================
def test_field_series_built_once_per_evaluate(monkeypatch):
    """同一 (bars, 字段) 被多个叶子引用 → _field_series 只调用一次。"""
    from app.screener import conditions as cond_mod
    from app.screener.conditions import evaluate

    calls = []
    orig = cond_mod._field_series

    def counting(bars, name):
        calls.append(name)
        return orig(bars, name)

    monkeypatch.setattr(cond_mod, "_field_series", counting)
    cond = {"and": [
        {"field": {"name": "close", "op": "gt", "value": 0, "window": -1}},
        {"compare": {
            "op": "gt",
            "left": {"kind": "field", "name": "close", "window": -1},
            "right": {"kind": "field", "name": "close", "window": -2},
        }},
    ]}
    bars = [make_bar(10 + i, time_=f"t{i}") for i in range(5)]
    evaluate(cond, bars)
    assert calls.count("close") == 1, calls


# ==================== P0-C：结果级 TTL 缓存 ====================
@pytest.fixture()
def cache_off_guard():
    """防其它测试残留缓存污染；用例内自行控制 TTL。"""
    from app.screener import engine as eng
    eng._RESULT_CACHE.clear()
    yield eng
    eng._RESULT_CACHE.clear()


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    from datasource.local_store import LocalStore
    db = DB(tmp_path / "test_cache.db")
    st = LocalStore(db)
    st.upsert_stock_list([{"code": "UP.T", "name": "上涨股", "category": "主板"}])
    st.upsert_bars("UP.T", [
        {"time": f"2026{i:03d}", "open": 100 + i, "high": 101 + i,
         "low": 99 + i, "close": 100 + i, "volume": 1000}
        for i in range(30)], adjust="qfq")
    yield st
    db._conn.close()


def test_scan_cache_hit_then_miss_on_param_change(store, cache_off_guard, monkeypatch):
    """同参数二刷 → cached_result=True；任一参数变化 → miss（重新计算）。"""
    eng = cache_off_guard
    monkeypatch.setattr(eng, "_screen_cache_ttl", lambda: 60)
    cond = {"field": {"name": "close", "op": "gt", "value": 110, "window": -1}}

    first = asyncio.run(eng.scan_async(store, cond, universe="all",
                                       source_policy="local_only", offline=True))
    assert first.get("cached_result") is None          # 首算不带缓存标记

    second = asyncio.run(eng.scan_async(store, cond, universe="all",
                                        source_policy="local_only", offline=True))
    assert second["cached_result"] is True
    assert second["cache_age_ms"] >= 0
    assert second["results"] == first["results"]

    changed = asyncio.run(eng.scan_async(store, cond, universe="all",
                                         source_policy="local_only", offline=True,
                                         min_price=500))   # 参数变化
    assert changed.get("cached_result") is None
    assert changed["count"] == 0


def test_scan_cache_disabled_when_ttl_zero(store, cache_off_guard, monkeypatch):
    """QMT_SCREEN_CACHE_TTL=0（显式禁用）→ 永远新鲜计算。"""
    eng = cache_off_guard
    monkeypatch.setattr(eng, "_screen_cache_ttl", lambda: 0)
    cond = {"field": {"name": "close", "op": "gt", "value": 0, "window": -1}}
    for _ in range(2):
        out = asyncio.run(eng.scan_async(store, cond, universe="all",
                                         source_policy="local_only", offline=True))
        assert out.get("cached_result") is None


def test_scan_cache_expired_entry_evicted(store, cache_off_guard, monkeypatch):
    """过期条目 → 淘汰并重新计算（age 超过 TTL 不返回）。"""
    eng = cache_off_guard
    monkeypatch.setattr(eng, "_screen_cache_ttl", lambda: 60)
    cond = {"field": {"name": "close", "op": "gt", "value": 0, "window": -1}}
    first = asyncio.run(eng.scan_async(store, cond, universe="all",
                                       source_policy="local_only", offline=True))
    # 把缓存时间戳拨回 61s 前 → 过期
    (key, (_, snap)), = eng._RESULT_CACHE.items()
    eng._RESULT_CACHE[key] = (eng.time.monotonic() - 61.0, snap)
    second = asyncio.run(eng.scan_async(store, cond, universe="all",
                                        source_policy="local_only", offline=True))
    assert second.get("cached_result") is None
    assert second["results"] == first["results"]


def test_scan_cache_never_crosses_stores(tmp_path, cache_off_guard, monkeypatch):
    """不同 LocalStore（不同 db 文件）同参数 → 绝不串缓存（store 身份入 key）。"""
    from core.db import DB
    from datasource.local_store import LocalStore
    eng = cache_off_guard
    monkeypatch.setattr(eng, "_screen_cache_ttl", lambda: 60)

    def _mk(name, close):
        db = DB(tmp_path / name)
        st = LocalStore(db)
        st.upsert_stock_list([{"code": "UP.T", "name": "x", "category": "主板"}])
        st.upsert_bars("UP.T", [
            {"time": f"2026{i:03d}", "open": close, "high": close + 1,
             "low": close - 1, "close": close, "volume": 1000}
            for i in range(30)], adjust="qfq")
        return st, db

    st_a, db_a = _mk("a.db", 100.0)
    st_b, db_b = _mk("b.db", 200.0)
    cond = {"field": {"name": "close", "op": "gt", "value": 0, "window": -1}}
    r_a = asyncio.run(eng.scan_async(st_a, cond, universe="all",
                                     source_policy="local_only", offline=True))
    r_b = asyncio.run(eng.scan_async(st_b, cond, universe="all",
                                     source_policy="local_only", offline=True))
    assert r_a.get("cached_result") is None
    assert r_b.get("cached_result") is None            # b 库不得命中 a 库的结果
    assert r_a["results"][0]["close"] < r_b["results"][0]["close"]
    db_a._conn.close()
    db_b._conn.close()
