"""K 线交易日格式归一 + 「有数据但全是陈的」不得报成功（V11 R13）。

为什么需要它（2026-09-18 实测）
------------------------------
全市场日线同步报告 ``total=5224 ok=5153 bars_written=1630066``，界面显示
「已完成」。但直接查库：

- ``broker`` provider：5093 只股票的**最后一根停在 20250418**（一年多前）；
- ``tencent`` provider：49 只到 2026-09-18，是活的；
- 两个 provider **零重叠**（那 49 只是 broker 返空才降级的新股）。

两个独立缺陷叠在一起：

**① dt 形状混存。** 写入侧原样落库各数据源的原始形状 —— 券商 ``"20260825"``、
腾讯 ``"2026-09-18"``（163 万根里 3894 根带横线）。后果不是「看着不整齐」：

- 字典序错乱：``'2026-09-18' < '20260825'``（``'-'`` 0x2D < ``'0'`` 0x30），
  ``MAX(dt)`` 永远取不到真正的最后一根；
- 主键 ``(code, period, adjust, dt, provider_id)`` 对**同一天**产出两行，
  跨源对账把它们当成两个不同交易日而恒对不上；
- ``WHERE dt BETWEEN ? AND ?`` 对另一种形状整体失效；
- 消费方按 ``"%Y%m%d"`` 硬解析直接抛 ``ValueError``。

**② 非空 ≠ 够新。** 源链是「第一个**非空**即返回」，而券商本地历史只下载到
2025-04-18 却照样非空 ⇒ **永不降级**到在线源。这是继「空转报成功」
（total=0）、「对账假空态」之后**第三种假成功**：写了一堆历史数据却宣称完成。

锁住的修复：
1. :func:`core.clock.bar_date` 是交易日格式的**唯一入口**；
2. ``upsert_bars`` 写入前归一，解析不出的行整行丢弃；
3. 迁移 v25 收敛**存量**脏形状；
4. ``get_kline(min_date=)`` 让源链在「非空但陈旧」时继续降级；
5. 同步结果带 ``stale`` / ``as_of``，全部陈旧时任务**必须失败**。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


# ---------------------------------------------------------------------------
# ① 归一化入口
# ---------------------------------------------------------------------------
def test_bar_date_normalizes_every_known_shape():
    """各数据源的实际形状都收敛到 YYYYMMDD。"""
    from core.clock import bar_date

    assert bar_date("20260825") == "20260825"          # 券商/QMT
    assert bar_date("2026-09-18") == "20260918"        # 腾讯在线源
    assert bar_date("2026/09/18") == "20260918"        # 东财风格
    assert bar_date("2026-09-18T15:00:00") == "20260918"
    assert bar_date(datetime(2026, 9, 18)) == "20260918"
    assert bar_date(20260918) == "20260918"            # 数字入参


def test_bar_date_rejects_impossible_dates():
    """非法日期返回 '' —— 绝不「修」成一个看起来合理的别的日子。"""
    from core.clock import bar_date

    assert bar_date("20260231") == ""      # 2 月没有 31 号
    assert bar_date("20261332") == ""      # 13 月
    assert bar_date("") == ""
    assert bar_date(None) == ""
    assert bar_date("not a date") == ""


def test_bar_date_is_ordering_stable():
    """★ 归一后字典序 == 时间序；**混存形状时这一点会失效**。

    实测线上就是这么翻车的：``'2026-09-18'`` 与 ``'20260825'`` 混在 dt 列里，
    按原始字符串排出来 ``'2026-09-18' < '20260825'``（``'-'`` 0x2D < ``'0'`` 0x30），
    于是 ``MAX(dt)`` 拿到的是一年多前的日期、界面显示的数据却「看着是新的」。
    """
    from core.clock import bar_date

    pairs = [("2026-09-18", "20260825"), ("20260825", "20250418"),
             ("20260101", "2025-12-31"), ("2026-09-18", "20250418")]
    for newer, older in pairs:
        assert bar_date(newer) > bar_date(older), (newer, older)

    # 反证：不归一化时，第一对的原始字符串比较给出**相反**的结论
    assert "2026-09-18" < "20260825", "前提：裸字符串比较确实是错的"
    assert bar_date("2026-09-18") > bar_date("20260825")


# ---------------------------------------------------------------------------
# ② 写入侧归一
# ---------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    """独立临时库的 LocalStore（用完还原全局 DB）。

    ★ 必须每次新建库文件：固定路径会跨次复用，上一次的残留行会让
    「写入了几行」这类断言莫名其妙地多出数据。
    """
    from core import db as db_mod
    from datasource.local_store import LocalStore

    prev = db_mod._db
    db_mod.init_db(tmp_path / "bardate.db")
    try:
        yield LocalStore(db_mod.get_db())
    finally:
        db_mod._db = prev


def test_upsert_bars_normalizes_dt(store):
    """★ 各源原样形状不许直接落库，必须收敛到 YYYYMMDD。"""
    store.upsert_bars("600519.SH", [
        {"time": "2026-09-18", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
         "volume": 10, "amount": 15},
        {"time": "20260825", "open": 1, "high": 2, "low": 0.5, "close": 1.4,
         "volume": 10, "amount": 14},
    ], period="1d", adjust="qfq", provider_id="tencent")

    dts = sorted(r["dt"] for r in store._db.query(
        "SELECT dt FROM local_bars WHERE code='600519.SH'"))
    assert dts == ["20260825", "20260918"], f"dt 未归一：{dts}"


def test_upsert_bars_drops_unparsable_time(store):
    """日期解析不出的行整行丢弃 —— 坏 dt 不许进主键。"""
    n = store.upsert_bars("000001.SZ", [
        {"time": "2026-02-31", "close": 1},     # 不存在的日期
        {"time": "garbage", "close": 2},
        {"time": "20260918", "close": 3},
    ], provider_id="x")
    assert n == 1, f"应只写入 1 行，实际 {n}"
    rows = store._db.query("SELECT dt FROM local_bars WHERE code='000001.SZ'")
    assert [r["dt"] for r in rows] == ["20260918"]


def test_get_bars_range_filters_across_shapes(store):
    """★ 区间参数也归一：传 'YYYY-MM-DD' 去比较 'YYYYMMDD' 会静默筛不到。"""
    def _b(t: str, c: float):
        return {"time": t, "open": c, "high": c + 1, "low": c - 1,
                "close": c, "volume": 10, "amount": c * 10}

    store.upsert_bars("600000.SH", [_b("20260901", 1), _b("20260910", 2),
                                    _b("20260918", 3)], provider_id="broker")
    got = store.get_bars("600000.SH", start="2026-09-10", end="2026-09-18")
    assert [b.time for b in got] == ["20260910", "20260918"], [b.time for b in got]


# ---------------------------------------------------------------------------
# ③ 存量迁移
# ---------------------------------------------------------------------------
def test_migration_v25_compacts_dashed_dates(store):
    """迁移把存量的 'YYYY-MM-DD' 收敛成 'YYYYMMDD'。"""
    from core.db_migrations import MIGRATIONS

    sql = dict(MIGRATIONS).get(25)
    assert sql, "缺少迁移 v25"

    db = store._db
    db.execute("DELETE FROM local_bars")
    # 绕过写入侧归一，直接造出历史脏形状（与线上存量同形）
    db.execute(
        "INSERT INTO local_bars (code, period, adjust, dt, provider_id, close) "
        "VALUES ('688783.SH','1d','qfq','2026-09-18','tencent',29.46),"
        "       ('688783.SH','1d','qfq','2025-10-28','tencent',20.0),"
        "       ('600519.SH','1d','qfq','20260825','broker',1304.0)")
    db.execute(sql)

    rows = {r["code"]: r["dt"] for r in db.query(
        "SELECT code, dt FROM local_bars ORDER BY dt")}
    assert rows["688783.SH"] == "20260918", rows
    assert rows["600519.SH"] == "20260825", rows
    # 归一化后 max(dt) 才拿得到真正的最后一根
    assert db.query("SELECT MAX(dt) m FROM local_bars")[0]["m"] == "20260918"


# ---------------------------------------------------------------------------
# ④ 源链新鲜度降级
# ---------------------------------------------------------------------------
def test_bars_last_date_does_not_assume_ordering():
    """不假设 bars 已升序 —— 直接取 bars[-1] 会拿错。"""
    from datasource.registry import bars_last_date

    assert bars_last_date([{"time": "2026-09-18"}, {"time": "20250418"}]) == "20260918"
    assert bars_last_date([{"time": "20250418"}, {"time": "2026-09-18"}]) == "20260918"
    assert bars_last_date([]) == ""
    assert bars_last_date([{"time": "bad"}]) == ""


def _mk_hub(plugins, chain):
    """构造一个只测「降级选择」的 DataSourceManager。

    熔断 / 许可证 / 能力解析全部旁路掉：本组用例关心的是
    「非空但陈旧时要不要继续往下试」，不是熔断器本身。
    """
    from datasource.registry import DataSourceManager

    m = DataSourceManager.__new__(DataSourceManager)
    m._plugins = plugins
    m._broker_factory = None
    m._declared = {}
    m._auto_chain_override = None
    m._commercial_mode = False
    m._breakers = {}
    m._resolve_sources = lambda s, c: list(chain)
    m._validate_source = lambda s: s

    async def _call(name, coro, timeout=None):
        return await coro

    m._call_source = _call  # type: ignore[assignment]
    return m


def test_get_kline_degrades_when_first_source_is_stale():
    """★ 非空但陈旧 ⇒ 继续降级，而不是停在第一个非空源上。"""
    calls: list[str] = []

    class _StaleSrc:
        async def get_kline(self, code, period, count, adjust=None):
            calls.append("qmt")
            return [{"time": "20250418", "close": 1.0}]

    class _FreshSrc:
        async def get_kline(self, code, period, count, adjust=None):
            calls.append("tencent")
            return [{"time": "2026-09-18", "close": 2.0}]

    m = _mk_hub({"qmt": _StaleSrc(), "tencent": _FreshSrc()},
                ["qmt", "tencent"])

    async def main():
        return await m.get_kline("600519.SH", min_date="20260909")

    bars, src = asyncio.run(main())
    assert src == "tencent", f"应降级到在线源，实际 {src}"
    assert calls == ["qmt", "tencent"]


def test_get_kline_returns_closest_when_all_stale():
    """全链都陈旧时返回**最接近**的一份（有数据优于无数据），但不谎报新鲜。"""
    from datasource.registry import DataSourceManager

    class _A:
        async def get_kline(self, code, period, count, adjust=None):
            return [{"time": "20250418", "close": 1.0}]

    class _B:
        async def get_kline(self, code, period, count, adjust=None):
            return [{"time": "20250601", "close": 2.0}]

    m = _mk_hub({"qmt": _A(), "tencent": _B()}, ["qmt", "tencent"])

    async def main():
        return await m.get_kline("600519.SH", min_date="20260909")

    bars, src = asyncio.run(main())
    assert src == "tencent", src
    assert bars[0]["time"] == "20250601"


def test_get_kline_without_min_date_keeps_old_behavior():
    """不传 min_date 时行为与改造前一致（第一个非空即返回）。"""
    from datasource.registry import DataSourceManager

    class _StaleSrc:
        async def get_kline(self, code, period, count, adjust=None):
            return [{"time": "20250418", "close": 1.0}]

    m = _mk_hub({"qmt": _StaleSrc()}, ["qmt"])

    async def main():
        return await m.get_kline("600519.SH")

    bars, src = asyncio.run(main())
    assert src == "qmt" and bars[0]["time"] == "20250418"


# ---------------------------------------------------------------------------
# ⑤ 同步结果标注 + 任务不得假成功
# ---------------------------------------------------------------------------
def _mk_syncer(store, *, stale_days=10):
    from app.sync.bars import BarsSyncer
    return BarsSyncer(store=store, stale_days=stale_days)


def test_sync_one_marks_stale_with_as_of(store):
    """拿到数据 ≠ 数据够新：as_of / stale 必须如实标注。"""
    syncer = _mk_syncer(store)

    async def _fetch(code, period, adjust, count):
        return [{"time": "2026-09-18", "close": 1}], "tencent"

    syncer._fetch = _fetch  # type: ignore[assignment]
    out = asyncio.run(syncer.sync_one("600519.SH"))
    assert out.ok and not out.stale
    assert out.as_of == "20260918"


def test_sync_one_flags_year_old_data_as_stale(store):
    """★ 券商本地只到 20250418 ⇒ 必须标 stale，不能当成成功的数据更新。"""
    syncer = _mk_syncer(store)

    async def _fetch(code, period, adjust, count):
        return [{"time": "20250418", "close": 1}], "broker"

    syncer._fetch = _fetch  # type: ignore[assignment]
    out = asyncio.run(syncer.sync_one("600519.SH"))
    assert out.ok, "写入本身是成功的"
    assert out.stale is True, "一年多前的数据必须标陈旧"
    assert out.as_of == "20250418"


def test_stale_days_zero_disables_the_gate(store):
    """stale_days=0 关闭门槛（恢复旧行为），供离线同步历史数据的场景使用。"""
    syncer = _mk_syncer(store, stale_days=0)
    assert syncer._min_date() == ""
    out = asyncio.run(syncer.sync_one("600519.SH")) if False else None

    async def _fetch(code, period, adjust, count):
        return [{"time": "20250418", "close": 1}], "broker"

    syncer._fetch = _fetch  # type: ignore[assignment]
    out = asyncio.run(syncer.sync_one("600519.SH"))
    assert out.ok and out.stale is False


def test_sync_many_aggregates_stale_count(store):
    """汇总里要有 stale / as_of_max —— 光看 ok 看不出数据是陈的。"""
    syncer = _mk_syncer(store)

    async def _fetch(code, period, adjust, count):
        if code == "600519.SH":
            return [{"time": "2026-09-18", "close": 1}], "tencent"
        return [{"time": "20250418", "close": 1}], "broker"

    syncer._fetch = _fetch  # type: ignore[assignment]
    summary = asyncio.run(syncer.sync_many(["600519.SH", "600000.SH", "000001.SZ"]))
    d = summary.to_dict()
    assert d["ok"] == 3
    assert d["stale"] == 2, f"应有 2 只陈旧，实际 {d['stale']}"
    assert d["as_of_max"] == "20260918"


def test_sync_runner_fails_when_everything_is_stale():
    """★★ 全部陈旧必须抛错 —— 写了 163 万根历史数据却报「已完成」是撒谎。"""
    from app.runtime.jobs import sync_runner

    class _Summary:
        failed = 0
        bars_written = 1630066
        finished = "x"

        def to_dict(self):
            return {"total": 5153, "ok": 5153, "failed": 0,
                    "bars_written": 1630066, "stale": 5153,
                    "as_of_max": "20250418"}

    class _Syncer:
        @staticmethod
        async def sync_stock_list(limit=None, progress_cb=None):
            return _Summary()

    import app.sync.bars as bars_mod
    orig = bars_mod.BarsSyncer
    bars_mod.BarsSyncer = lambda *a, **k: _Syncer()
    try:
        runner = sync_runner({})
        job: dict = {"report": lambda p, m: None}
        with pytest.raises(RuntimeError, match="数据全部陈旧"):
            asyncio.run(runner(job))
    finally:
        bars_mod.BarsSyncer = orig


def test_sync_runner_honors_explicit_empty_adjust():
    """★ ``adjust=""``（不复权）必须真的生效 —— 不能被 ``or`` 兜底成 qfq。

    ``str(params.get("adjust") or "qfq")`` 里**空字符串是 falsy**，显式传
    ``""`` 会被静默改成 ``"qfq"``。差别不是口味问题：qfq 链只含真做复权的源
    （新浪只回不复权，按设计不在该链内），``""`` 的不复权链**含新浪** ——
    实测传 ``""`` 却仍旧走 qfq 时，新浪一次都没被调用，数据照样追不上。
    """
    from app.runtime.jobs import sync_runner

    seen: dict = {}

    class _Syncer:
        def __init__(self, **kw):
            seen.update(kw)

        @staticmethod
        async def sync_stock_list(limit=None, progress_cb=None):
            class _S:
                failed = 0
                bars_written = 1
                finished = "x"

                def to_dict(self):
                    return {"total": 1, "ok": 1, "failed": 0,
                            "bars_written": 1, "stale": 0, "as_of_max": "20260918"}
            return _S()

    import app.sync.bars as bars_mod
    orig = bars_mod.BarsSyncer

    def _factory(**kw):
        return _Syncer(**kw)

    bars_mod.BarsSyncer = _factory
    try:
        asyncio.run(sync_runner({"adjust": ""})({"report": lambda p, m: None}))
        assert seen.get("adjust") == "", f"空字符串被吞成 {seen.get('adjust')!r}"
        seen.clear()
        asyncio.run(sync_runner({})({"report": lambda p, m: None}))
        assert seen.get("adjust") == "qfq", "未传时仍应默认 qfq"
        seen.clear()
        asyncio.run(sync_runner({"adjust": "hfq"})({"report": lambda p, m: None}))
        assert seen.get("adjust") == "hfq"
    finally:
        bars_mod.BarsSyncer = orig


def test_sync_runner_fails_on_overwhelming_stale_ratio():
    """★ 98% 陈旧却显示 done —— 界面根本看不出数据没追上，必须判失败。

    实测（2026-09-19 全市场 5224 只）：在线数据源被限流后降级链路整体失效，
    ``stale=5095 / ok=5104``（98.4%），任务依然 ``status=done``。
    只有零星几只新鲜 ⇒ 在线源其实已不可用。
    """
    from app.runtime.jobs import sync_runner

    class _Summary:
        failed = 120
        bars_written = 610172
        finished = "x"

        def to_dict(self):
            return {"total": 5224, "ok": 5104, "failed": 120,
                    "bars_written": 610172, "stale": 5095,
                    "as_of_max": "20260918"}

    class _Syncer:
        @staticmethod
        async def sync_stock_list(limit=None, progress_cb=None):
            return _Summary()

    import app.sync.bars as bars_mod
    orig = bars_mod.BarsSyncer
    bars_mod.BarsSyncer = lambda *a, **k: _Syncer()
    try:
        runner = sync_runner({})
        job: dict = {"report": lambda p, m: None}
        with pytest.raises(RuntimeError, match="超过 .* 阈值"):
            asyncio.run(runner(job))
    finally:
        bars_mod.BarsSyncer = orig


def test_sync_runner_surfaces_partial_staleness_but_succeeds():
    """部分陈旧不算失败，但必须在结果里显形（否则又是一个静默陷阱）。"""
    from app.runtime.jobs import sync_runner

    class _Summary:
        failed = 0
        bars_written = 100
        finished = "x"

        def to_dict(self):
            return {"total": 10, "ok": 10, "failed": 0, "bars_written": 100,
                    "stale": 2, "as_of_max": "20260918"}

    class _Syncer:
        @staticmethod
        async def sync_stock_list(limit=None, progress_cb=None):
            return _Summary()

    import app.sync.bars as bars_mod
    orig = bars_mod.BarsSyncer
    bars_mod.BarsSyncer = lambda *a, **k: _Syncer()
    try:
        runner = sync_runner({})
        seen: dict = {}
        job: dict = {"report": lambda p, m: seen.__setitem__("msg", m)}
        res = asyncio.run(runner(job))
    finally:
        bars_mod.BarsSyncer = orig

    assert res["stale"] == 2
    assert "陈旧" in res["stale_warning"]
    assert "陈旧" in seen.get("msg", ""), "陈旧必须出现在任务进度文案里"
