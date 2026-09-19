"""定时经典选股 runner（``system.classic_screen``）的「假成功」护栏。

为什么需要它
------------
``_classic_screen_runner`` 此前在 ``bars_map`` 为空时会一路跑到 ``run_classic``，
返回 ``total_hits=0`` 并**报成功**。用户看到的是「今天行情不好，没选出票」，而真相
是**日线同步没跑成**、根本一只都没扫到 —— 本项目「假成功」家族的标准形态：
流程跑完了，数据没动。

锁定三条：

1. **``scanned == 0`` 必须让作业失败并留下可操作原因**，不能返回 0 命中；
2. **真的扫了但没命中**时作业仍成功，但结论要写明「无命中（行情形态不满足）」，
   与「没数据」区分开；
3. **每次运行都要落库**（含 0 命中），否则「跑过没选中」与「从没跑过」界面一样。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.db  # noqa: E402
from _phase7_support import tmp_db  # noqa: F401,E402
from app.runtime import system_jobs  # noqa: E402
from app.screener import picks as picks_mod  # noqa: E402


class _Report:
    def __init__(self, degraded=False, reason=""):
        self.provider_used = "local"
        self.degraded = degraded
        self.degraded_reason = reason

    def to_provenance(self):
        return {"provider": self.provider_used}


def _patch(monkeypatch, *, bars_map, report=None):
    """把股票池与批量取数替换成桩（不碰真实数据源）。"""
    report = report or _Report()

    async def _resolve_universe(*_a, **_kw):
        return {"codes": ["600519.SH", "000001.SZ"]}

    class _BP:
        async def get_bars_batch(self, codes, **_kw):
            return bars_map, report

    monkeypatch.setattr("app.data.bars_provider.BarsProvider", _BP)
    monkeypatch.setattr("app.screener.universe.resolve_universe", _resolve_universe)


def _run(monkeypatch, db, **params):
    """按真实调用形态跑一次：``工厂(params) → Runner(job)``。

    ★ ``runner_for`` 返回的是**工厂**（params → Runner 协程函数），不是 Runner 本身。
    """
    monkeypatch.setattr(core.db, "get_db", lambda: db)
    factory = system_jobs.runner_for("system.classic_screen")
    assert factory is not None
    runner = factory(dict(params))
    return asyncio.run(runner({"id": "job-x"}))


_BARS = [{"time": "2026-09-18", "open": 10.0, "high": 11.0, "low": 9.8,
          "close": 10.9, "volume": 1000, "amount": 1.09e7}]


def test_empty_bars_fails_instead_of_reporting_zero_hits(monkeypatch, tmp_db):
    """★ 核心护栏：一只都没扫到 ⇒ 失败 + 可操作原因，绝不返回「0 命中」。"""
    _patch(monkeypatch, bars_map={})
    with pytest.raises(RuntimeError) as ei:
        _run(monkeypatch, tmp_db, strategies=["turtle_trade"])
    msg = str(ei.value)
    assert "未取得任何 K 线" in msg
    assert "日线" in msg          # 告诉用户该去跑哪个任务


def test_empty_bars_does_not_write_a_success_run(monkeypatch, tmp_db):
    """失败时不该留下「成功跑过」的运行记录（否则界面会显示一次 0 命中的正常运行）。"""
    _patch(monkeypatch, bars_map={})
    with pytest.raises(RuntimeError):
        _run(monkeypatch, tmp_db, strategies=["turtle_trade"])
    assert picks_mod.list_runs() == []


def test_scanned_but_no_hits_still_succeeds_and_records(monkeypatch, tmp_db):
    """真的扫了（2 只）但没命中：作业成功，且运行记录里能看出 scanned=2。"""
    _patch(monkeypatch, bars_map={"600519.SH": _BARS, "000001.SZ": _BARS})
    out = _run(monkeypatch, tmp_db, strategies=["turtle_trade"], limit=5)
    assert out["scanned"] == 2
    assert out["total_hits"] == 0
    # ★ 结论文案必须区分「没数据」与「没命中」
    assert "无命中" in out["conclusion"]
    run = picks_mod.latest_run()
    assert run is not None and run["scanned"] == 2 and run["hits"] == 0
    assert run["source"] == "schedule"
    assert run["bar_date"] == "20260918"


def test_run_records_bar_date_for_freshness(monkeypatch, tmp_db):
    """★ 落库要带上「数据截至日」——「非空 ≠ 够新」。"""
    _patch(monkeypatch, bars_map={"600519.SH": _BARS})
    out = _run(monkeypatch, tmp_db, strategies=["turtle_trade"])
    assert out["bar_date"] == "20260918"
    assert picks_mod.latest_run()["bar_date"] == "20260918"


def test_unknown_strategy_fails_with_reason(monkeypatch, tmp_db):
    """写错的策略名必须失败（否则每天跑出 0 命中，用户以为行情不好）。"""
    _patch(monkeypatch, bars_map={"600519.SH": _BARS})
    with pytest.raises(RuntimeError) as ei:
        _run(monkeypatch, tmp_db, strategies=["not_a_strategy"])
    assert "未知经典策略" in str(ei.value)


def test_job_id_is_recorded(monkeypatch, tmp_db):
    _patch(monkeypatch, bars_map={"600519.SH": _BARS})
    _run(monkeypatch, tmp_db, strategies=["turtle_trade"])
    assert picks_mod.latest_run()["job_id"] == "job-x"
