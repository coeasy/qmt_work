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
    from app.datasource.local_store import LocalStore
    from app.db import DB
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
