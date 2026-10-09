"""日线同步的**写入裁剪**与**区间能力探测**（2026-10-10 数据下载链路优化）。

钉死四件事：

1. **增量同步不再重写整个 lookback 窗口**。实测本机 ``local_bars`` 395 万行 /
   ``app.db`` 1.56GB —— 每天把 7740 只 × 320 根整窗口 ``INSERT OR REPLACE`` 一遍，
   99% 的行与库里逐字节相同。裁剪后只写「本地最新之后 + 尾部修正窗口」。
2. **没新数据时写 0 根，但必须记为 ``bars_unchanged``**。否则「一切正常」与
   「源挂了」在汇总里长得一模一样（written 都是 0）。
3. **全量回补不受裁剪影响**（它要补的就是历史区间），且**前导空年不再被误判成
   「没有源支持区间」** —— 年初 / 退市股的历史因此才补得上。
4. **失败明细有上限**，全市场集体失败时不会把几千条 dict 塞进 job 结果。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import asyncio
from datetime import date

import pytest

import app.sync.bars as bars_mod
from app.sync.bars import BarsSyncer


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    from datasource.local_store import LocalStore
    db = DB(tmp_path / "test_trim.db")
    yield LocalStore(db)
    db._conn.close()


def _bar(dt: str, close: float = 1.5) -> dict:
    return {"time": dt, "open": 1.0, "high": 2.0, "low": 0.5,
            "close": close, "volume": 100}


def _bars(*dts: str) -> list:
    return [_bar(d) for d in dts]


# ---------------------------------------------------------------------------
# ① 增量裁剪
# ---------------------------------------------------------------------------
def test_incremental_only_writes_newer_plus_tail(store):
    """本地最新 20260105 ⇒ 只写 01-06/01-07 两根 + 尾部修正窗口 2 根。"""
    store.upsert_bars("600519.SH", _bars("20260101", "20260102", "20260103",
                                         "20260104", "20260105"),
                      period="1d", adjust="qfq", provider_id="eltdx")

    async def _fetch(code, period, adjust, count):
        return _bars("20260101", "20260102", "20260103", "20260104", "20260105",
                     "20260106", "20260107")

    s = BarsSyncer(store=store, fetch_bars=_fetch, rewrite_tail=2)
    out = asyncio.run(s.sync_many(["600519.SH"]))

    assert out.ok == 1
    # 2 根新的 + 2 根尾窗口；前面 3 根（01-01~01-03）被裁掉
    assert out.bars_written == 4, out.to_dict()
    assert out.bars_unchanged == 3, out.to_dict()
    # 库里确实只多了两根（01-06/01-07），且旧数据没被删
    rows = store.get_bars("600519.SH", period="1d", adjust="qfq", limit=100)
    assert [r.time for r in rows] == ["20260101", "20260102", "20260103",
                                      "20260104", "20260105", "20260106",
                                      "20260107"]


def test_trim_writes_nothing_but_counts_unchanged_when_no_new_data(store):
    """源没有比本地更新的数据 ⇒ 一行不写，但如实记 ``bars_unchanged``。"""
    store.upsert_bars("600519.SH", _bars("20260105", "20260106"),
                      period="1d", adjust="qfq", provider_id="eltdx")

    async def _fetch(code, period, adjust, count):
        return _bars("20260105", "20260106")

    s = BarsSyncer(store=store, fetch_bars=_fetch)
    out = asyncio.run(s.sync_many(["600519.SH"]))

    assert out.ok == 1
    assert out.bars_written == 0
    assert out.bars_unchanged == 2      # ★ 不是「失败」，是「本来就一样」
    assert out.to_dict()["bars_unchanged"] == 2


def test_trim_disabled_rewrites_everything(store):
    """``trim_new=False`` 保持改造前行为（整窗口重写）—— 强制修正历史时用。"""
    store.upsert_bars("600519.SH", _bars("20260105"),
                      period="1d", adjust="qfq", provider_id="eltdx")

    async def _fetch(code, period, adjust, count):
        return _bars("20260105", "20260106")

    s = BarsSyncer(store=store, fetch_bars=_fetch, trim_new=False)
    out = asyncio.run(s.sync_many(["600519.SH"]))

    assert out.bars_written == 2
    assert out.bars_unchanged == 0


def test_full_mode_never_trims(store):
    """全量回补**绝不**裁剪：它要写的正是历史区间，裁了就成了「假成功」。"""
    store.upsert_bars("600519.SH", [_bar("20140101")],
                      period="1d", adjust="qfq", provider_id="broker")

    async def _fetch(code, period, adjust, count):
        return _bars("20140101", "20150101", "20160101")

    s = BarsSyncer(store=store, fetch_bars=_fetch, mode="full")
    out = asyncio.run(s.sync_many(["600519.SH"]))

    assert out.mode == "full"
    assert out.bars_written == 3
    assert out.bars_unchanged == 0


def test_as_of_min_uses_untrimmed_bars(store):
    """``as_of_min`` 回答「源这次给到了多早」，不该被裁剪窗口改写。"""
    store.upsert_bars("600519.SH", _bars("20260105"),
                      period="1d", adjust="qfq", provider_id="eltdx")

    async def _fetch(code, period, adjust, count):
        return _bars("20230101", "20260105", "20260106")

    s = BarsSyncer(store=store, fetch_bars=_fetch, rewrite_tail=1)
    out = asyncio.run(s.sync_many(["600519.SH"]))

    assert out.as_of_min == "20230101"      # 源的最早一根，不是窗口起点
    assert out.bars_written == 2            # 01-05(尾窗) + 01-06(新)


# ---------------------------------------------------------------------------
# ② 区间能力探测（前导空年）
# ---------------------------------------------------------------------------
class _ProbeHub:
    """带 ``kline_range_supported`` 的真实形状 hub：只有 ``years`` 里的年有数据。"""

    def __init__(self, years=(), support=True):
        self.years = set(years)
        self.support = support
        self.calls: list = []

    def kline_range_supported(self, source="auto", adjust=None, period="1d"):
        return self.support

    async def get_kline_range(self, code, period="1d", *, adjust=None,
                              start="", end="", count=5000, source="auto"):
        self.calls.append(str(start))
        year = int(str(start)[:4])
        if year not in self.years:
            return [], "broker"
        return [_bar(f"{year}0315"), _bar(f"{year}0615")], "broker"


def test_unsupported_range_costs_zero_rpc(store, monkeypatch):
    """探测说「不支持」⇒ 一次无效 RPC 都不发，直接退化。"""
    hub = _ProbeHub(support=False)
    monkeypatch.setattr(bars_mod, "get_hub", lambda: hub)

    async def _fetch(code, period, adjust, count):
        return [_bar("20260815")]

    s = BarsSyncer(store=store, fetch_bars=_fetch, mode="full")
    out = asyncio.run(s.sync_one("600519.SH"))

    assert out.paged is False
    assert out.ok is True
    assert hub.calls == []          # ★ 旧实现会先白打一页


def test_leading_empty_years_no_longer_degrade(store, monkeypatch):
    """★ 退市 / 年初场景：前几年都没数据也**不能**判成「没有源支持区间」。

    改造前：第一页（今年）为空 ⇒ 要么退化、要么 break ⇒ 历史一根不补，
    而汇总还写着 ok —— 这是「假成功」家族的新成员。
    """
    cur = date.today().year
    # 只有 4 年前有数据（退市股）：前 3 年都是前导空年
    hub = _ProbeHub(years={cur - 3})
    monkeypatch.setattr(bars_mod, "get_hub", lambda: hub)

    async def _never(code, period, adjust, count):
        raise AssertionError(f"不该退化：{code}")

    s = BarsSyncer(store=store, fetch_bars=_never, mode="full", full_years=12)
    out = asyncio.run(s.sync_one("600519.SH"))

    assert out.ok is True
    assert out.paged is True
    assert out.bars_written == 2
    assert out.as_of_min == f"{cur - 3}0315"


def test_leading_empty_years_capped(store, monkeypatch):
    """前导空年有上限：源永远空时不会一路翻到 1990 年。"""
    hub = _ProbeHub(years=set())
    monkeypatch.setattr(bars_mod, "get_hub", lambda: hub)

    s = BarsSyncer(store=store, fetch_bars=lambda *a: [], mode="full")
    out = asyncio.run(s.sync_one("600519.SH"))

    # 容忍 N 个前导空年 + 再试第 N+1 页 ⇒ 共 N+1 次请求后放弃
    assert len(hub.calls) == BarsSyncer.FULL_LEAD_EMPTY_MAX + 1
    assert out.paged is True
    assert out.ok is False          # 真的没数据，如实失败


def test_legacy_hub_without_probe_keeps_old_behaviour(store, monkeypatch):
    """老 hub / 注入桩没有探测方法 ⇒ 走改造前判据（首页空即退化），行为不变。"""

    class _LegacyHub:
        def __init__(self):
            self.calls = []

        async def get_kline_range(self, code, period="1d", *, adjust=None,
                                  start="", end="", count=5000, source="auto"):
            self.calls.append(str(start))
            return None, None       # 与「无源支持」不可区分

    hub = _LegacyHub()
    monkeypatch.setattr(bars_mod, "get_hub", lambda: hub)

    async def _fetch(code, period, adjust, count):
        return [_bar("20260815")]

    s = BarsSyncer(store=store, fetch_bars=_fetch, mode="full")
    out = asyncio.run(s.sync_one("600519.SH"))

    assert out.paged is False
    assert hub.calls == [f"{date.today().year}0101"]   # 试一页即放弃（旧行为）


# ---------------------------------------------------------------------------
# ③ 失败明细截断
# ---------------------------------------------------------------------------
def test_errors_are_capped_with_omitted_count(store):
    """全市场集体失败：明细只留前 N 条，其余只记数（不把 job 结果撑爆）。"""
    codes = [f"{600000 + i}.SH" for i in range(230)]

    async def _fetch(code, period, adjust, count):
        return None                 # 全部失败

    s = BarsSyncer(store=store, fetch_bars=_fetch)
    out = asyncio.run(s.sync_many(codes))

    assert out.failed == 230
    assert len(out.errors) == bars_mod.ERRORS_MAX
    assert out.errors_truncated == 230 - bars_mod.ERRORS_MAX
    d = out.to_dict()
    assert d["errors_truncated"] == 30
    assert len(d["errors"]) == bars_mod.ERRORS_MAX
