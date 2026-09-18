"""日线同步：券商兜底股票池 + 「空转不得报成功」。

为什么需要它（2026-09-19 实测）
------------------------------
``POST /runtime/schedules/sch-default-sync-bars/trigger`` 返回 200，任务
``status=done``、``progress=100``，而 ``total=0 / bars_written=0 / elapsed_ms=0``
—— **一只票都没同步，却报成功**。

根因是 ``BarsSyncer.sync_stock_list`` 只认「全市场股票列表」
（``hub.get_stock_list``），而**券商侧没有该接口**（``_BoundBrokerSource.get_stock_list``
恒返回 None）。于是纯券商环境下「定时更新日线」每天静默空转：数据一天没更新，
界面还显示「已完成」。

同一份能力在别处是可用的：券商能提供板块成分代码（实测「沪深A股」5224 只），
``app/routes/market.py`` 的 K 线同步就走这条路。本测试锁住两条修复：

1. 列表为空时**退化到券商板块成分**，让同步真能跑；
2. 真的一只都没有时，任务必须**失败**而不是 done（静默空转比直接失败危险得多）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class _StubHub:
    """hub 桩：全市场列表为空，券商板块成分可用。"""

    def __init__(self, sector_codes=None):
        self._sector = sector_codes
        self.sector_asked: list[str] = []

    async def get_stock_list(self, source: str = "auto"):
        return []            # 券商侧无全市场列表接口

    async def get_sector_stocks(self, sector: str = "沪深A股", conn_id=None):
        self.sector_asked.append(sector)
        if self._sector is None:
            return None, None
        return list(self._sector), "broker"


class _StubStore:
    def __init__(self):
        self.upserted = None

    def upsert_stock_list(self, items):
        self.upserted = items


def _patch_hub(monkeypatch, hub):
    import datasource.registry as reg
    monkeypatch.setattr(reg, "get_hub", lambda: hub)


def test_empty_list_falls_back_to_broker_sector():
    """全市场列表为空时，必须用券商板块成分顶上（否则同步恒空转）。"""
    from app.sync.bars import BarsSyncer

    hub = _StubHub(["600000.SH", "600519.SH"])
    store = _StubStore()
    syncer = BarsSyncer(store=store)
    seen: dict = {}

    async def _fake_sync_many(codes, progress_cb=None):
        seen["codes"] = list(codes)
        from app.sync.bars import SyncSummary
        return SyncSummary(started="", finished="", total=len(codes),
                           failed=0, ok=len(codes), errors=[], elapsed_ms=1)

    syncer.sync_many = _fake_sync_many  # type: ignore[assignment]

    import datasource.registry as reg
    orig = reg.get_hub
    reg.get_hub = lambda: hub
    try:
        summary = asyncio.run(syncer.sync_stock_list())
    finally:
        reg.get_hub = orig

    assert summary.total == 2, f"应同步 2 只，实际 {summary.total}"
    assert seen["codes"] == ["600000.SH", "600519.SH"]
    assert hub.sector_asked == ["沪深A股"]
    # 兜底路径只有代码没有名称，写进股票列表会把名称覆盖成空 —— 必须跳过
    assert store.upserted is None, "兜底数据不得回写股票列表（会把名称抹成空）"


def test_no_codes_at_all_returns_empty_summary():
    """券商也没给成分 ⇒ 老老实实返回空汇总（由调用方判失败）。"""
    from app.sync.bars import BarsSyncer

    hub = _StubHub(None)
    syncer = BarsSyncer(store=_StubStore())
    called = {"v": False}

    async def _fake_sync_many(codes, progress_cb=None):
        called["v"] = True
        raise AssertionError("没有代码就不该进同步流程")

    syncer.sync_many = _fake_sync_many  # type: ignore[assignment]

    import datasource.registry as reg
    orig = reg.get_hub
    reg.get_hub = lambda: hub
    try:
        summary = asyncio.run(syncer.sync_stock_list())
    finally:
        reg.get_hub = orig

    assert summary.total == 0
    assert called["v"] is False


def test_sync_runner_raises_on_empty_instead_of_fake_success():
    """★ total=0 时必须抛错（任务失败），绝不能 done + progress=100%。"""
    from app.runtime.jobs import sync_runner

    class _EmptySummary:
        failed = 0
        bars_written = 0
        finished = "x"

        def to_dict(self):
            return {"total": 0, "ok": 0, "failed": 0, "bars_written": 0}

    class _Syncer:
        @staticmethod
        async def sync_stock_list(limit=None, progress_cb=None):
            return _EmptySummary()

    import app.sync.bars as bars_mod
    orig = bars_mod.BarsSyncer
    bars_mod.BarsSyncer = lambda *a, **k: _Syncer()
    try:
        runner = sync_runner({})
        job: dict = {"report": lambda p, m: None}
        with pytest.raises(RuntimeError, match="股票池为空"):
            asyncio.run(runner(job))
    finally:
        bars_mod.BarsSyncer = orig
