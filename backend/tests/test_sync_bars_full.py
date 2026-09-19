"""全量日线回补（V11 §5.3 P0-3 III）测试。

数据源 hub 与抓取器全部注入假实现（零网络、零真实券商）。这里钉死四件事：

1. **「全量」必须真的按日期区间翻页**。拿不到区间就如实退化为单次大 count，
   并把 ``paged=False`` 报出来 —— 绝不允许「假装翻了 12 年、其实一直拿最近
   N 根」（那会让用户以为历史已补齐，实际一根没多）。
2. **断点续传的游标是「数据」而不是「上次跑到第几个」**。后者只在正常退出时
   才写对，崩一次就白跑；「库里最早一根是哪天」本身就是事实。
3. ``skipped_complete`` / ``paged`` / ``as_of_min`` 必须出现在汇总里 ——
   否则「跳过了 4000 只」「历史没补齐」在界面上完全看不见。
4. 非法 ``mode`` **绝不静默升级成 full**（那是几小时的作业）。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import asyncio
from datetime import date

import pytest

import app.sync.bars as bars_mod
from app.sync.bars import BarsSyncer, _bars_first_date, _dedupe_bars


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    from datasource.local_store import LocalStore
    db = DB(tmp_path / "test_full.db")
    yield LocalStore(db)
    db._conn.close()


def _bar(dt: str) -> dict:
    return {"time": dt, "open": 1.0, "high": 2.0, "low": 0.5,
            "close": 1.5, "volume": 100}


async def _never(code, period, adjust, count):
    """全量路径下**不该**被调用到（调用即说明退化，另有断言）。"""
    raise AssertionError(f"退化路径被意外调用：{code} count={count}")


class _RangeHub:
    """假 hub：只有 ``years`` 里的年份有数据，其余返回空页。"""

    def __init__(self, years=(), support=True):
        self.years = set(years)
        self.support = support
        self.calls: list = []

    async def get_kline_range(self, code, period="1d", *, adjust=None,
                              start="", end="", count=5000, source="auto"):
        self.calls.append(str(start))
        if not self.support:
            # 链上没有源声明 supports_kline_range ⇒ 与真实实现一致地返 (None, None)
            return None, None
        year = int(str(start)[:4])
        if year not in self.years:
            return [], "broker"
        return [_bar(f"{year}0315"), _bar(f"{year}0615")], "broker"


def _patch_hub(monkeypatch, hub):
    monkeypatch.setattr(bars_mod, "get_hub", lambda: hub)


# ---------------------------------------------------------------------------
# ① 逐年翻页
# ---------------------------------------------------------------------------
def test_full_pages_year_by_year(store, monkeypatch):
    """从今年往前逐页取，遇到空页即停，多年结果合并去重。"""
    cur = date.today().year
    hub = _RangeHub(years={cur, cur - 1, cur - 2})
    _patch_hub(monkeypatch, hub)

    s = BarsSyncer(store=store, fetch_bars=_never, mode="full")
    out = asyncio.run(s.sync_one("600519.SH"))

    assert out.ok is True
    assert out.paged is True
    # 三年 × 每年 2 根
    assert out.bars_written == 6
    # 最早一根 = 三年前那一年的第一根（不是 bars[0]，见 ③）
    assert out.as_of_min == f"{cur - 2}0315"
    assert out.as_of == f"{cur}0615"
    # 第 4 次调用拿到空页（早于上市日）⇒ 停止，不再白翻
    assert hub.calls == [f"{cur}0101", f"{cur - 1}0101",
                         f"{cur - 2}0101", f"{cur - 3}0101"]


def test_full_stops_at_full_years_cap(store, monkeypatch):
    """源永远有数据时，页数受 ``full_years`` 封顶（不会无限向前翻）。"""
    cur = date.today().year
    hub = _RangeHub(years={cur - i for i in range(50)})
    _patch_hub(monkeypatch, hub)

    s = BarsSyncer(store=store, fetch_bars=_never, mode="full", full_years=3)
    out = asyncio.run(s.sync_one("600519.SH"))

    assert out.ok is True
    assert len(hub.calls) == 3
    assert out.as_of_min == f"{cur - 2}0315"


# ---------------------------------------------------------------------------
# ② 没有支持区间的源 ⇒ 如实退化
# ---------------------------------------------------------------------------
def test_full_degrades_to_big_count_without_range_source(store, monkeypatch):
    """纯在线源环境：没有源支持区间 ⇒ 退化为单次大 count，且 ``paged=False``。"""
    hub = _RangeHub(support=False)
    _patch_hub(monkeypatch, hub)
    seen: list = []

    async def _fetch(code, period, adjust, count):
        seen.append(count)
        return [_bar("20260815")]

    s = BarsSyncer(store=store, fetch_bars=_fetch, mode="full")
    out = asyncio.run(s.sync_one("600519.SH"))

    assert out.ok is True
    assert out.paged is False                      # ★ 必须说出来
    assert seen == [BarsSyncer.FULL_COUNT_DEFAULT]  # 大窗口，而不是「假装翻页」
    assert len(hub.calls) == 1                      # 试过一次就放弃，不空翻 12 年


def test_summary_flags_unpaged_full(store, monkeypatch):
    """汇总里必须能看出「mode=full 但没翻成页」—— 这是最危险的假成功。"""
    hub = _RangeHub(support=False)
    _patch_hub(monkeypatch, hub)

    async def _fetch(code, period, adjust, count):
        return [_bar("20260815")]

    s = BarsSyncer(store=store, fetch_bars=_fetch, mode="full")
    summary = asyncio.run(s.sync_many(["600519.SH", "000001.SZ"]))

    assert summary.mode == "full"
    assert summary.paged is False
    assert summary.to_dict()["paged"] is False
    assert summary.to_dict()["mode"] == "full"


def test_summary_reports_paged_full(store, monkeypatch):
    cur = date.today().year
    _patch_hub(monkeypatch, _RangeHub(years={cur, cur - 1}))

    s = BarsSyncer(store=store, fetch_bars=_never, mode="full")
    summary = asyncio.run(s.sync_many(["600519.SH"]))

    assert summary.paged is True
    assert summary.as_of_min == f"{cur - 1}0315"


# ---------------------------------------------------------------------------
# ③ 工具函数：不假设顺序、去重
# ---------------------------------------------------------------------------
def test_bars_first_date_takes_min_not_first():
    """不假设源按升序返回 —— 直接取 bars[0] 会拿错。"""
    bars = [_bar("20260615"), _bar("20240101"), _bar("20250301")]
    assert _bars_first_date(bars) == "20240101"
    assert _bars_first_date([]) == ""
    assert _bars_first_date([{"time": "not-a-date"}]) == ""


def test_dedupe_bars_sorted_and_last_wins():
    bars = [_bar("20250101"), _bar("20240101"), _bar("20250101")]
    bars[2]["close"] = 9.9
    out = _dedupe_bars(bars)
    assert [b["time"] for b in out] == ["20240101", "20250101"]
    assert out[1]["close"] == 9.9      # 同日后者覆盖前者


# ---------------------------------------------------------------------------
# ④ 断点续传：数据即游标
# ---------------------------------------------------------------------------
class _FakeStore:
    def __init__(self, have=None, boom=False):
        self.have = dict(have or {})
        self.boom = boom
        self.seen_period = None
        self.seen_adjust = None

    def earliest_dt_map(self, codes, period="1d", adjust=""):
        if self.boom:
            raise RuntimeError("db locked")
        self.seen_period, self.seen_adjust = period, adjust
        return {c: self.have[c] for c in codes if c in self.have}


def test_filter_backfilled_uses_earliest_date_as_cursor():
    s = BarsSyncer(store=_FakeStore(), mode="full", full_years=12)
    target = s._target_start()
    y = int(target[:4])

    st = _FakeStore({"A": f"{y - 1}0101",   # 早于目标起点 ⇒ 已补齐，跳过
                     "B": f"{y}0101",       # 正好等于目标起点 ⇒ 已达标，跳过
                     "C": f"{y}0601"})      # 晚于目标起点 ⇒ 仍需回补
    s = BarsSyncer(store=st, mode="full", full_years=12)
    need, skipped = s._filter_backfilled(["A", "B", "C", "D"])

    assert need == ["C", "D"]     # D 本地无记录 ⇒ 必须回补
    assert skipped == 2
    assert (st.seen_period, st.seen_adjust) == ("1d", "qfq")


def test_filter_backfilled_reruns_everything_on_read_failure():
    """读本地最早日期失败 ⇒ 全量重跑。宁可多做功，不可漏补。"""
    s = BarsSyncer(store=_FakeStore(boom=True), mode="full")
    need, skipped = s._filter_backfilled(["A", "B"])
    assert need == ["A", "B"]
    assert skipped == 0


def test_target_start_is_full_years_back():
    s = BarsSyncer(store=_FakeStore(), mode="full", full_years=12)
    cur = date.today().year
    assert s._target_start() == f"{cur - 11}0101"


def test_sync_stock_list_full_skips_backfilled(store, monkeypatch):
    """全量模式下 ``sync_stock_list`` 必须先筛掉已补齐的标的，并把跳过数带出去。"""
    import datasource.registry as reg

    class _Hub:
        async def get_stock_list(self, source="auto"):
            return [{"code": "600000.SH", "name": "浦发"}, {"code": "600519.SH", "name": "茅台"}]

    hub = _Hub()
    monkeypatch.setattr(reg, "get_hub", lambda: hub)
    monkeypatch.setattr(bars_mod, "get_hub", lambda: hub)

    # 600000.SH 本地历史已覆盖到目标起点之前 ⇒ 会被跳过
    target = BarsSyncer(store=store, mode="full")._target_start()
    store.upsert_bars("600000.SH", [_bar(f"{int(target[:4]) - 1}1231")],
                      period="1d", adjust="qfq")

    async def _fetch(code, period, adjust, count):
        return [_bar("20260815")]

    s = BarsSyncer(store=store, fetch_bars=_fetch, mode="full")
    summary = asyncio.run(s.sync_stock_list())

    assert summary.skipped_complete == 1
    assert summary.total == 1
    assert summary.ok == 1
    assert summary.to_dict()["skipped_complete"] == 1


# ---------------------------------------------------------------------------
# ⑤ 增量模式不受影响
# ---------------------------------------------------------------------------
def test_incremental_does_not_page_or_filter(store, monkeypatch):
    hub = _RangeHub(support=False)
    _patch_hub(monkeypatch, hub)

    async def _fetch(code, period, adjust, count):
        assert count == 320          # 用的是 lookback，不是 FULL_COUNT
        return [_bar("20260815")]

    s = BarsSyncer(store=store, fetch_bars=_fetch, lookback=320)
    summary = asyncio.run(s.sync_many(["600519.SH"]))

    assert hub.calls == []           # 增量模式压根不碰区间接口
    assert summary.mode == "incremental"
    assert summary.paged is False
    assert summary.skipped_complete == 0


def test_invalid_mode_falls_back_to_incremental(store):
    """非法 mode 绝不静默变全量 —— 那是几小时的作业，不能猜。"""
    for bad in ("", "   ", "FULLLL", "1", "true", None):
        assert BarsSyncer(store=store, mode=bad)._mode == "incremental"
    for good in ("full", "FULL", " Full "):
        assert BarsSyncer(store=store, mode=good)._mode == "full"
