"""冷热分层同步（PLAN_COLD_HOT_SYNC）测试。

零网络、零真实券商：抓源桩 / 假 hub 全部注入。

钉死五件事：
1. ``_build_plans`` 自动判定四类场景（新股 / 老股冷齐全 / 老股缺冷且陈 / 老股缺冷且新）。
2. ``zone="hot"`` 只写 ``local_bars``、**绝不**碰冷仓。
3. ``zone="cold"`` 只写冷仓 ``kline_archive``、**绝不**写热表，并打 ``cold_complete`` 标记。
4. 冷补齐完成后二次同步 ``skipped_cold>0`` 且冷仓行数不再增长。
5. 冷仓数据经 ``KlineCache.get`` 端到端可见（图表/回测透明合并冷历史）。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import asyncio
from datetime import date, timedelta

import app.sync.bars as bars_mod
from app.sync.bars import BarsSyncer, SyncPlan

import pytest


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    from datasource.local_store import LocalStore
    db = DB(tmp_path / "test_coldhot.db")
    yield LocalStore(db)
    db._conn.close()


@pytest.fixture()
def cold(tmp_path):
    from datasource.cold_store import init_cold_store, reset_cold_store
    reset_cold_store()
    cs = init_cold_store(tmp_path / "cold.db")
    yield cs
    reset_cold_store()


def _bar(dt: str) -> dict:
    return {"time": dt, "open": 1.0, "high": 2.0, "low": 0.5,
            "close": 1.5, "volume": 100}


async def _fetch_ok(code, period, adjust, count):
    return [_bar("20260815"), _bar("20260816"), _bar("20260817")]


class _ColdHub:
    """假 hub：既提供股票列表，又支持按年份区间翻页取历史（冷补齐用）。"""

    def __init__(self, years=()):
        self.years = set(years)

    async def get_stock_list(self, source="auto"):
        return [{"code": "600519.SH", "name": "茅台"}]

    def kline_range_supported(self, source, *, adjust=None, period=None):
        return True

    async def get_kline_range(self, code, period="1d", *, adjust=None,
                              start="", end="", count=5000, source="auto"):
        year = int(str(start)[:4])
        if year not in self.years:
            return [], "broker"
        return [_bar(f"{year}0315"), _bar(f"{year}0615")], "broker"


def _patch_hub(monkeypatch, hub):
    monkeypatch.setattr(bars_mod, "get_hub", lambda: hub)


# ---------------------------------------------------------------------------
# ① 自动判定四种场景
# ---------------------------------------------------------------------------
def test_build_plans_four_scenarios(store, cold, monkeypatch):
    # 「近期」按同步器自身的「预期交易日」取，确保落入「已最新」区间
    # （单纯 today-10 仍早于 expected_bar_date，会被判陈旧而触发热同步，
    # 那是正确的增量行为，只是不符合本场景假设）。
    s0 = BarsSyncer(store=store, zone="auto", skip_fresh=True)
    recent = s0._fresh_target() or (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    old = "20200101"

    # A 新股：只有近期一根（无冷区间）
    store.upsert_bars("A.SH", [_bar(recent)], period="1d", adjust="qfq")
    # B 老股冷齐全：最早一根很老（有冷区间）+ 最近一根，且已标记 cold_complete
    store.upsert_bars("B.SH", [_bar(old), _bar(recent)], period="1d", adjust="qfq")
    store.set_meta("cold_complete:1d:qfq:B.SH", "1")
    # C 老股缺冷且陈旧：只有一根很老的（热也陈旧）
    store.upsert_bars("C.SH", [_bar(old)], period="1d", adjust="qfq")
    # D 老股缺冷但热新鲜：最早很老（有冷区间）+ 最近一根（热已最新）
    store.upsert_bars("D.SH", [_bar(old), _bar(recent)], period="1d", adjust="qfq")

    s = BarsSyncer(store=store, zone="auto", skip_fresh=True)
    plans, skipped_fresh, skipped_cold = s._build_plans(["A.SH", "B.SH", "C.SH", "D.SH"])

    # A 新股：无冷区间 ⇒ 热已最新且冷不参与 ⇒ 双双跳过
    assert plans["A.SH"] == SyncPlan(hot=False, cold=False)
    # B 老股冷齐全：cold_complete 被尊重 ⇒ 即便有冷区间也不补
    assert plans["B.SH"].cold is False
    # C 老股缺冷且陈：冷缺失 ⇒ 需补冷（热的陈旧另算）
    assert plans["C.SH"].cold is True
    # D 老股缺冷但热新鲜：热已最新 ⇒ 只补冷
    assert plans["D.SH"] == SyncPlan(hot=False, cold=True)
    # 冷补齐跳过计数：A、B 已无需冷（共 2 只）
    assert skipped_cold == 2
    # 热跳过计数：A、B、D 热已最新（共 3 只）
    assert skipped_fresh == 3


# ---------------------------------------------------------------------------
# ② zone=hot：只写热表，不碰冷仓
# ---------------------------------------------------------------------------
def test_zone_hot_only_writes_local_bars(store, cold, monkeypatch):
    _patch_hub(monkeypatch, _ColdHub(years={date.today().year}))
    s = BarsSyncer(store=store, fetch_bars=_fetch_ok, zone="hot")
    summary = asyncio.run(s.sync_stock_list())

    assert summary.ok == 1
    assert store.count_bars("600519.SH", adjust="qfq") == 3
    # ★ 冷仓一行都不该被写（zone=hot 严格不碰冷历史）
    assert cold.count() == 0
    assert summary.cold_written == 0


# ---------------------------------------------------------------------------
# ③ zone=cold：只写冷仓，不写热表，并打 complete 标记
# ---------------------------------------------------------------------------
def test_zone_cold_only_writes_cold_and_marks_complete(store, cold, monkeypatch):
    cur = date.today().year
    _patch_hub(monkeypatch, _ColdHub(years={cur, cur - 1, cur - 2}))

    s = BarsSyncer(store=store, zone="cold")
    summary = asyncio.run(s.sync_stock_list())

    # 冷仓应有 3 年 × 2 根 = 6 行历史
    assert cold.count() == 6
    assert summary.cold_written == 6
    # ★ 热表一行都不该被写（zone=cold 严格不碰热表）
    assert store.count_bars("600519.SH", adjust="qfq") == 0
    # ★ cold_complete 标记已落（后续日常同步据此跳过）
    assert store.get_meta("cold_complete:1d:qfq:600519.SH") == "1"


# ---------------------------------------------------------------------------
# ④ 冷补齐完成后二次同步：跳过且冷仓不增长
# ---------------------------------------------------------------------------
def test_cold_complete_second_sync_skips(store, cold, monkeypatch):
    cur = date.today().year
    _patch_hub(monkeypatch, _ColdHub(years={cur, cur - 1, cur - 2}))

    s1 = BarsSyncer(store=store, zone="cold")
    asyncio.run(s1.sync_stock_list())
    assert cold.count() == 6

    # 二次同步：全部已 complete ⇒ 冷跳过数 > 0，冷仓行数不再增长
    s2 = BarsSyncer(store=store, zone="cold")
    summary2 = asyncio.run(s2.sync_stock_list())

    assert summary2.skipped_complete >= 1
    assert summary2.cold_written == 0
    assert cold.count() == 6


# ---------------------------------------------------------------------------
# ⑤ 端到端：冷仓数据经 KlineCache.get 透明可见
# ---------------------------------------------------------------------------
def test_kline_cache_sees_cold_end_to_end(store, cold, monkeypatch, tmp_path):
    from core.db import DB
    from gateway.kline_cache import KlineCache

    cur = date.today().year
    _patch_hub(monkeypatch, _ColdHub(years={cur, cur - 1, cur - 2}))

    s = BarsSyncer(store=store, zone="cold")
    asyncio.run(s.sync_stock_list())

    # 构造一个**只有空热表**的 KlineCache（热表无数据，全部应来自冷仓）
    kc_db = DB(tmp_path / "kc.db")
    kc_db._conn.execute(
        "CREATE TABLE IF NOT EXISTS kline_cache ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, period TEXT, dt TEXT, "
        "open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL, "
        "fetched_at REAL, adjust TEXT)")
    kc = KlineCache(kc_db, cold=cold)

    rows = kc.get("600519.SH", "1d", 250, "qfq")
    # 冷仓 6 根全部经 get 合并返回（热表为空，纯冷路径）
    assert len(rows) == 6
    times = sorted(r["time"] for r in rows)
    assert times[0] == f"{cur - 2}0315"   # 最早一根来自最老年份的冷数据
    assert times[-1] == f"{cur}0615"
    kc_db._conn.close()


# ---------------------------------------------------------------------------
# ⑥ 冷仓日期归一（R2 P1-1）：dt 必须是 YYYYMMDD，跨源一致
# ---------------------------------------------------------------------------
def test_cold_dt_normalized_to_yyyymmdd(store, monkeypatch):
    """冷仓行 ``dt`` 恒为 ``YYYYMMDD``，不随源返回形状漂移。

    源链三形状混入（QMT ``"20260105"`` / 腾讯 ``"2026-01-05"`` /
    东财 ``datetime`` 或 ``"2026/01/05"``）。若冷仓原样透传，
    ``kline_archive.dt`` 会混存两种格式，于是：

    1. ``MIN(dt)`` 取到字典序最小值而非真实最早日 —— 字符串
       ``'2026-01-05'`` 与 ``'20150101'`` 相比，``'-'``(0x2D) < ``'0'``(0x30)，
       断点续传的 ``_covered`` 判定（``best <= cold_target``）会误判；
    2. 同一天出现两行（跨源对账认为「两个交易日」）；
    3. ``WHERE dt BETWEEN start AND end`` 区间过滤对另一种格式整体失效。

    热路径（bars.py）已在入库前归一；冷路径必须同样归一，否则
    ``KlineCache.get`` 合并热 + 冷时会同日重复且顺序错乱。
    """
    from datetime import datetime

    s = BarsSyncer(store=store)
    bars = [
        {"time": "20260105", "open": 10, "high": 10.5, "low": 9.5,
         "close": 10.2, "volume": 1000, "amount": 10200.0},
        {"time": "2026-01-06", "open": 10, "high": 10.5, "low": 9.5,
         "close": 10.2, "volume": 1000, "amount": 10200.0},
        {"time": datetime(2026, 1, 7), "open": 10, "high": 10.5, "low": 9.5,
         "close": 10.2, "volume": 1000, "amount": 10200.0},
        {"time": "2026/01/08", "open": 10, "high": 10.5, "low": 9.5,
         "close": 10.2, "volume": 1000, "amount": 10200.0},
        {"time": "", "open": 1, "high": 1, "low": 1, "close": 1,
         "volume": 0, "amount": 0.0},          # 坏日期 ⇒ 跳过
    ]
    rows = s._normalize_for_cold("600519.SH", bars)
    assert len(rows) == 4                     # 空日期被跳过
    dts = sorted(r[2] for r in rows)
    assert dts == ["20260105", "20260106", "20260107", "20260108"], dts
    # 全部为 8 位纯数字（可被 BETWEEN 区间过滤与字典序比较正确处理）
    assert all(len(d) == 8 and d.isdigit() for d in dts)
