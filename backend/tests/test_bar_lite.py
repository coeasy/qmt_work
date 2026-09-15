"""R4 性能杠杆 1：``BarLite`` 轻量视图的等价性 / 契约 / 成本护栏。

背景（2026-09-15 实测，117 万行真实库）：全市场选股要为 6982 只 × ~168 根
≈ 117 万根 K 线各构造一个对象，逐行 Pydantic 校验是本地取数侧最大单项成本
（实测 ~4.9s）。``lite=True`` 改构造 ``BarLite``（``__slots__`` 轻量视图，
字段集与 ``Bar`` 一致但跳过校验），实测 ~1.9s（约 3 倍快）。

本文件钉住四件事：
1. **字段集不得漂移** —— ``Bar`` 增删字段而 ``BarLite`` 没跟上即红；
2. **取值逐行等价** —— 含多源 canonical 选主、latest-N、``amount`` 为 NULL；
3. **契约不变** —— ``get_bars_batch`` 默认仍返回 ``Bar``；
4. **成本护栏** —— ``lite=True`` 时**一次 ``Bar`` 都不构造**（结构性断言，
   与机器快慢无关，不会 flaky）。
"""
import asyncio

import pytest

import datasource.local_store as local_store
import datasource.registry as regmod
import app.screener.engine as engine_mod
from datasource.local_store import LocalStore
from datasource.models import Bar, BarLite
from app.screener.engine import evaluate_scan
from _phase4_support import REG_ALL, FakeStore, fake_reg_manager, force_deps, make_bar

_FIELDS = ("time", "open", "high", "low", "close", "volume", "amount",
           "volume_unit")


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    db = DB(tmp_path / "test_bar_lite.db")
    yield LocalStore(db)
    db._conn.close()


def _bars(n=4, start=20260901):
    return [Bar(time=str(start + i), open=10 + i, high=11 + i, low=9 + i,
                close=10.5 + i, volume=1000 * (i + 1), amount=10500 * (i + 1))
            for i in range(n)]


def _seed(store, codes, n=4):
    for c in codes:
        store.upsert_bars(c, _bars(n), adjust="qfq")


def _seed_multi_source(store, code):
    """同一 (code, dt) 写多 provider / 多档质量 + 一根 amount 为 NULL 的 K 线。"""
    def bar(t, close, amount):
        return Bar(time=t, open=close, high=close + 1, low=close - 1,
                   close=close, volume=100, amount=amount)

    store.upsert_bars(code, [bar("20260901", 12.0, 1200.0)], adjust="qfq",
                      provider_id="src_z", quality_state="validated")
    store.upsert_bars(code, [bar("20260901", 10.0, 1000.0)], adjust="qfq",
                      provider_id="src_a", quality_state="unknown")
    store.upsert_bars(code, [bar("20260902", 21.0, 2100.0)], adjust="qfq",
                      provider_id="src_b", quality_state="unknown")
    store.upsert_bars(code, [bar("20260902", 20.0, 2000.0)], adjust="qfq",
                      provider_id="src_a", quality_state="unknown")
    # amount 为 NULL：契约允许（Bar.amount 是 Optional），两条路径都必须保留 None
    store.upsert_bars(code, [bar("20260903", 31.0, None)], adjust="qfq",
                      provider_id="src_a", quality_state="unknown")


# ---------------------------------------------------------------------------
# 1. 结构等价：字段集不得漂移
# ---------------------------------------------------------------------------
def test_bar_lite_field_parity():
    """``BarLite`` 的字段集必须与 ``Bar`` 完全一致（防单侧漂移）。

    ``Bar`` 增删字段而 ``BarLite`` 没跟上，会在全市场选股里表现为「属性静默缺失」
    （指标算出 None、条件恒不命中），极难定位——故用结构断言提前拦住。
    """
    assert set(BarLite.__slots__) == set(Bar.model_fields), (
        f"BarLite 与 Bar 字段集不一致："
        f"仅 Bar 有 {set(Bar.model_fields) - set(BarLite.__slots__)}；"
        f"仅 BarLite 有 {set(BarLite.__slots__) - set(Bar.model_fields)}")
    assert tuple(BarLite.__slots__) == _FIELDS, "BarLite 字段顺序被改动"
    # 顺序也要与 Bar 一致：位置实参构造依赖它
    assert tuple(BarLite.__slots__) == tuple(Bar.model_fields), "字段顺序与 Bar 不一致"


def test_bar_lite_is_slots_class_not_pydantic():
    """``BarLite`` 必须是纯 ``__slots__`` 类、不继承 Pydantic —— 这是提速的**机制**。

    若有人图省事把它改成 ``BaseModel`` 子类，性能优化会静默失效（且功能测试全绿）。
    """
    from pydantic import BaseModel

    assert not issubclass(BarLite, BaseModel)
    assert not hasattr(BarLite, "model_fields")
    obj = BarLite("20260901", 1.0, 2.0, 0.5, 1.5)
    assert not hasattr(obj, "__dict__"), "BarLite 实例带 __dict__，已不是轻量视图"
    assert (obj.time, obj.open, obj.high, obj.low, obj.close) == \
        ("20260901", 1.0, 2.0, 0.5, 1.5)
    assert obj.volume is None and obj.amount is None and obj.volume_unit is None


# ---------------------------------------------------------------------------
# 2. 契约不变：默认仍返回 Bar
# ---------------------------------------------------------------------------
def test_get_bars_batch_default_returns_bar(store):
    codes = ["600000.SH", "600001.SH"]
    _seed(store, codes, n=3)
    out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)
    for c in codes:
        assert len(out[c]) == 3, c
        assert all(isinstance(b, Bar) for b in out[c]), c
        assert not any(isinstance(b, BarLite) for b in out[c]), c


def test_get_bars_batch_lite_returns_bar_lite(store):
    codes = ["600000.SH", "600001.SH"]
    _seed(store, codes, n=3)
    out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250,
                               lite=True)
    for c in codes:
        assert len(out[c]) == 3, c
        assert all(isinstance(b, BarLite) for b in out[c]), c


# ---------------------------------------------------------------------------
# 3. 取值逐行等价（多源选主 / latest-N / amount 为 NULL）
# ---------------------------------------------------------------------------
def test_get_bars_batch_lite_values_identical_to_full(store):
    """``lite=True`` 与默认路径必须**逐行等值**（含选主结果与 NULL 保留）。"""
    codes = [f"60000{i}.SH" for i in range(4)]
    for c in codes:
        _seed_multi_source(store, c)

    full = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)
    lite = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250,
                                lite=True)

    assert set(full) == set(lite)
    for c in codes:
        assert len(full[c]) == len(lite[c]) == 3, c
        for a, b in zip(full[c], lite[c]):
            assert (a.time, a.open, a.high, a.low, a.close, a.volume, a.amount,
                    a.volume_unit) == \
                   (b.time, b.open, b.high, b.low, b.close, b.volume, b.amount,
                    b.volume_unit), c

    # 选主三级键仍生效（数值即证据）：validated 胜 / provider 名升序胜 / 空 provider 胜
    closes = [b.close for b in lite[codes[0]]]
    assert closes == [12.0, 20.0, 31.0], closes
    # NULL amount 原样保留，不被估算填充（零 mock 铁律）
    assert lite[codes[0]][-1].amount is None
    assert full[codes[0]][-1].amount is None


def test_get_bars_batch_lite_latest_n_matches_get_bars(store):
    """latest-N 语义在 lite 路径下不变，且与逐只 ``get_bars`` 一致。"""
    codes = [f"60000{i}.SH" for i in range(3)]
    _seed(store, codes, n=10)
    lite = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=3,
                                lite=True)
    for c in codes:
        per = store.get_bars(c, period="1d", adjust="qfq", limit=3)
        assert len(lite[c]) == 3
        assert [b.time for b in lite[c]] == [b.time for b in per], c
        assert [b.close for b in lite[c]] == [b.close for b in per], c


# ---------------------------------------------------------------------------
# 4. 成本护栏（结构性，与机器快慢无关）
# ---------------------------------------------------------------------------
def test_get_bars_batch_lite_never_constructs_bar(store, monkeypatch):
    """``lite=True`` 时**一次 ``Bar`` 都不得构造** —— 这正是提速的来源。

    断言「构造次数」而非「耗时」：不受机器负载影响，不会 flaky。同时反向验证
    默认路径确实走 ``Bar``（证明探针有效，而非恒为 0）。
    """
    codes = [f"60000{i}.SH" for i in range(5)]
    _seed(store, codes, n=4)

    made = {"n": 0}

    class _SpyBar(Bar):
        def __init__(self, **kw):
            made["n"] += 1
            super().__init__(**kw)

    monkeypatch.setattr(local_store, "Bar", _SpyBar)

    store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250, lite=True)
    assert made["n"] == 0, (
        f"lite 路径仍构造了 {made['n']} 个 Bar —— Pydantic 校验未绕开，优化失效")

    store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)
    assert made["n"] == len(codes) * 4, (
        f"默认路径构造 {made['n']} 个 Bar，期望 {len(codes) * 4} —— 探针未生效")


def test_get_bars_batch_lite_constructs_bar_lite(store, monkeypatch):
    """对称护栏：``lite=True`` 必须真的构造 ``BarLite``（不是两条路都返回空）。"""
    codes = ["600000.SH"]
    _seed(store, codes, n=3)

    made = {"n": 0}
    orig_init = BarLite.__init__

    def spy(self, *a, **kw):
        made["n"] += 1
        return orig_init(self, *a, **kw)

    monkeypatch.setattr(BarLite, "__init__", spy)
    out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250,
                               lite=True)
    assert len(out["600000.SH"]) == 3
    assert made["n"] == 3, f"构造 {made['n']} 个 BarLite，期望 3"


# ---------------------------------------------------------------------------
# 5. 消费方等价：选股求值对两种类型结果一致
# ---------------------------------------------------------------------------
def _cond():
    return {"and": [{"field": {"name": "close", "op": "gt", "value": 10}},
                    {"indicator": {"name": "ma", "params": {"win": 3},
                                   "output": "ma", "op": "lt", "value": 100}}]}


def test_evaluate_scan_identical_for_bar_and_bar_lite():
    """同一批数据用 ``Bar`` / ``BarLite`` 喂 ``evaluate_scan``，结果必须完全一致。

    这覆盖**在线路径**：``BarsProvider`` 在线取回的是 ``Bar``（由 hub 构造），
    本地路径是 ``BarLite`` —— 引擎必须对两种类型都能工作（鸭子类型）。
    """
    codes = [f"60000{i}.SH" for i in range(6)]
    cond = _cond()
    as_bar = {c: _bars(6) for c in codes}
    as_lite = {c: [BarLite(**{k: getattr(b, k) for k in _FIELDS}) for b in bs]
               for c, bs in as_bar.items()}

    r1, s1, _ = evaluate_scan(codes, as_bar, cond)
    r2, s2, _ = evaluate_scan(codes, as_lite, cond)
    assert s1 == s2 == len(codes)
    assert [r["code"] for r in r1] == [r["code"] for r in r2]
    assert [r["close"] for r in r1] == [r["close"] for r in r2]
    assert [r["score"] for r in r1] == [r["score"] for r in r2]
    assert [r["change_pct"] for r in r1] == [r["change_pct"] for r in r2]


# ---------------------------------------------------------------------------
# 6. 端到端：scan_async 必须向本地仓要 lite
# ---------------------------------------------------------------------------
def test_scan_async_requests_lite_from_local_store(monkeypatch):
    """``scan_async`` 必须显式传 ``lite=True`` —— 防止优化被无声改回默认。

    断言「传了什么参数」而非「跑了多久」，与机器快慢无关。
    """
    seen = {}

    class _RecordingStore(FakeStore):
        def get_bars_batch(self, codes, period="1d", adjust="", limit=250,
                           lite=False):
            seen["lite"] = lite
            return {c: [make_bar(15, time_=f"t{i}") for i in range(20)]
                    for c in codes}

    with force_deps():
        store = _RecordingStore(
            stock_list=[{"code": "600000.SH", "name": "浦发"}])
        orig_gm = regmod.get_manager
        regmod.get_manager = lambda: fake_reg_manager(REG_ALL)
        try:
            out = asyncio.run(engine_mod.scan_async(
                store, _cond(), source_policy="local_only", universe="all",
                offline=True))
        finally:
            regmod.get_manager = orig_gm

    assert seen.get("lite") is True, (
        "scan_async 未向本地仓请求 lite —— 全市场选股的 Bar 构造成本又回来了")
    assert out["provenance"]["provider_used"] == "local"
