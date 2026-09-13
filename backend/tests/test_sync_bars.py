"""G1-5 日线窗口同步测试（BarsSyncer）。

抓取器注入假实现（测试密闭、零网络）；同步正确性由本地仓主键幂等保证。
注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import asyncio
from datetime import date

import pytest

from app.sync.bars import BarsSyncer, SyncSummary, weekday_calendar


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    from datasource.local_store import LocalStore
    db = DB(tmp_path / "test_sync.db")
    yield LocalStore(db)
    db._conn.close()


async def _fetch_ok(code, period, adjust, count):
    return [{"time": f"2026082{i}", "open": 1, "high": 2, "low": 0.5,
             "close": 1.5, "volume": 100} for i in range(1, 4)]


# ---- 交易日历（启发式） ------------------------------------------------------
def test_weekday_calendar():
    days = weekday_calendar(date(2026, 8, 30), count=5)   # 08-30 是周日
    assert days == ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]


def test_weekday_calendar_excludes_weekends():
    days = weekday_calendar(date(2026, 8, 30), count=3)
    assert "2026-08-29" not in days and "2026-08-30" not in days


# ---- 单标的 ----------------------------------------------------------------
def test_sync_one_writes_bars(store):
    s = BarsSyncer(store=store, fetch_bars=_fetch_ok)   # 默认 adjust="qfq"
    out = asyncio.run(s.sync_one("600519.SH"))
    assert out.ok is True
    assert out.bars_written == 3
    assert store.count_bars("600519.SH", adjust="qfq") == 3


async def _fetch_none(code, period, adjust, count):
    return None


def test_sync_one_source_empty(store):
    s = BarsSyncer(store=store, fetch_bars=_fetch_none)
    out = asyncio.run(s.sync_one("000001.SZ"))
    assert out.ok is False
    assert out.error


def test_sync_one_fetch_exception(store):
    async def _boom(code, period, adjust, count):
        raise RuntimeError("远端超时")

    s = BarsSyncer(store=store, fetch_bars=_boom)
    out = asyncio.run(s.sync_one("X"))
    assert out.ok is False
    assert "超时" in out.error


# ---- 批量 ------------------------------------------------------------------
def test_sync_many_summary(store):
    async def _mixed(code, period, adjust, count):
        return None if code == "BAD" else await _fetch_ok(code, period, adjust, count)

    s = BarsSyncer(store=store, fetch_bars=_mixed)
    summary = asyncio.run(s.sync_many(["600519.SH", "000001.SZ", "BAD", "000002.SZ"]))
    assert isinstance(summary, SyncSummary)
    assert summary.total == 4
    assert summary.ok == 3
    assert summary.failed == 1
    assert summary.bars_written == 9
    assert summary.errors[0]["code"] == "BAD"
    assert summary.elapsed_ms >= 0
    # 同步完成元数据已落
    assert store.get_meta("last_sync_at") == summary.finished


def test_sync_many_idempotent(store):
    s = BarsSyncer(store=store, fetch_bars=_fetch_ok)
    asyncio.run(s.sync_many(["600519.SH", "000001.SZ"]))
    asyncio.run(s.sync_many(["600519.SH", "000001.SZ"]))
    # REPLACE 幂等：同一批同步两轮不膨胀（复权维度 qfq）
    assert store.count_bars("600519.SH", adjust="qfq") == 3
    assert store.count_bars("000001.SZ", adjust="qfq") == 3


def test_sync_many_concurrency_bounded(store):
    state = {"active": 0, "peak": 0}

    async def _slow(code, period, adjust, count):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.02)
        state["active"] -= 1
        return await _fetch_ok(code, period, adjust, count)

    s = BarsSyncer(store=store, fetch_bars=_slow, concurrency=3)
    codes = [f"60000{i}.SH" for i in range(1, 9)]
    asyncio.run(s.sync_many(codes))
    assert state["peak"] <= 3, f"并发峰值 {state['peak']} 超闸门 3"
    assert state["peak"] >= 1


# ---------------- 交易日历（G1-5b：内置节假日表） ----------------
def test_calendar_excludes_holidays():
    from datetime import date as _d

    from app.sync.calendar import is_trading_day
    # 2025-10-01 国庆休市（周四，工作日但为节假日）
    assert is_trading_day(_d(2025, 10, 1)) is False
    # 2025-10-09 节后首个交易日（周四）
    assert is_trading_day(_d(2025, 10, 9)) is True
    # 调休补班周末（2025-09-28 周日为交易日）
    assert is_trading_day(_d(2025, 9, 28)) is True


def test_weekday_calendar_now_holiday_accurate():
    # weekday_calendar 已升级为真实日历：国庆周不再返回休市日
    days = weekday_calendar(date(2025, 10, 9), count=3)
    assert days == ["2025-10-09"] or "2025-10-01" not in days
    assert all(d not in ("2025-10-01", "2025-10-02", "2025-10-03") for d in days)


def test_exchange_calendar_port_declares_coverage():
    from app.sync.calendar import exchange_calendar

    assert exchange_calendar.coverage(date(2026, 8, 30)).exact is True
    assert exchange_calendar.coverage(date(2028, 1, 3)).exact is False


# ---------------- 溯源为真（P3-4：禁止用 "auto" 冒充真实来源） ----------------
def _bar(dt="20260901"):
    return {"time": dt, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100}


def _providers(store, code):
    rows = store._db.query(
        "SELECT DISTINCT provider_id FROM local_bars WHERE code=?", (code,))
    return sorted(r["provider_id"] for r in rows)


def test_sync_records_real_provider_id(store):
    """抓取器返回 (bars, source) 时必须把真实来源写进 provider_id。"""

    async def _fetch(code, period, adjust, count):
        return [_bar()], "eltdx"

    s = BarsSyncer(store=store, fetch_bars=_fetch, provider_id="auto")
    out = asyncio.run(s.sync_one("600519.SH"))
    assert out.ok is True
    assert _providers(store, "600519.SH") == ["eltdx"]


def test_sync_never_fabricates_auto_provider(store):
    """拿不到来源信息且配置为 auto 时，溯源记空值 —— 绝不伪造 "auto"。"""

    async def _fetch(code, period, adjust, count):
        return [_bar()]

    s = BarsSyncer(store=store, fetch_bars=_fetch, provider_id="auto")
    out = asyncio.run(s.sync_one("600002.SZ"))
    assert out.ok is True
    assert _providers(store, "600002.SZ") == [""]


def test_sync_uses_configured_provider_when_source_unknown(store):
    """调用方显式指定的数据源在无真实来源名时作为溯源兜底。"""

    async def _fetch(code, period, adjust, count):
        return [_bar()]

    s = BarsSyncer(store=store, fetch_bars=_fetch, provider_id="baostock")
    asyncio.run(s.sync_one("600003.SH"))
    assert _providers(store, "600003.SH") == ["baostock"]


def test_real_source_wins_over_configured_label(store):
    """真实命中来源优先于调用方标签（不得因标签而说谎）。"""

    async def _fetch(code, period, adjust, count):
        return [_bar()], "akshare"

    s = BarsSyncer(store=store, fetch_bars=_fetch, provider_id="baostock")
    asyncio.run(s.sync_one("600004.SH"))
    assert _providers(store, "600004.SH") == ["akshare"]


def test_default_fetch_passes_configured_source(monkeypatch, store):
    """configured provider 必须真正驱动取数（eod 备用源续跑此前是空操作）。"""
    seen = {}

    class _Hub:
        async def get_kline(self, code, period, count, source="auto", adjust=None):
            seen["source"] = source
            seen["adjust"] = adjust
            return [_bar()], "baostock"

    monkeypatch.setattr("app.sync.bars.get_hub", lambda: _Hub())
    s = BarsSyncer(store=store, provider_id="baostock", adjust="qfq")
    out = asyncio.run(s.sync_one("600005.SH"))
    assert out.ok is True
    assert seen["source"] == "baostock"
    assert seen["adjust"] == "qfq"
    assert _providers(store, "600005.SH") == ["baostock"]
