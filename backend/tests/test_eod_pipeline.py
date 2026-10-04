"""V9 Phase 7 DoD：EOD 管线端到端（EOD 跑完 → 选股数据到位语义）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _phase7_support import eod_db, tmp_db  # noqa: F401,E402
import app.runtime.eod as eod_mod  # noqa: E402
import app.runtime.system_jobs as sj  # noqa: E402


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _stub_bars_hub(monkeypatch):
    """把日线同步的数据源 hub 换成桩 —— EOD 测试锁的是**管线编排**，不是数据源。

    ★ 为什么必须有（2026-10-04 全量回归实测挂起 30+ 分钟）：``BarsSyncer.
    sync_stock_list`` 会经全局 hub 拉全市场股票列表再逐只同步 K 线。本机装了
    eltdx 且 TDX 服务器可达时，这是**真实的全市场网络同步**（5224 只 × 并发 8），
    单跑时无券商快速失败看不出来，全量回归直接挂死。
    """
    class _StubHub:
        async def get_stock_list(self, source="auto"):
            return []

        async def get_sector_stocks(self, *_a, **_k):
            raise RuntimeError("stub：EOD 测试禁止触达真实数据源")

    monkeypatch.setattr("app.sync.bars.get_hub", lambda: _StubHub())


def test_eod_pipeline_runs_and_reports(eod_db, monkeypatch):
    """在临时 DB 上跑完整 EOD：无券商 → calendar 降级但管线不中断。"""
    monkeypatch.setattr("core.db.get_db", lambda: eod_db)

    class _Store:
        def get_stock_list(self):
            return [{"code": "600000.SH", "name": "浦发", "category": "沪深A股"}]
    import datasource.local_store as ls
    monkeypatch.setattr(ls, "get_store", lambda: _Store())
    # reconcile/coverage 直接传 db 参数，无需 patch；bars_sync 无券商 → degraded
    result = _run(eod_mod.run_eod_pipeline({"lookback": 5, "limit": 3}))
    assert result["steps"]["universe"]["codes"] == 1
    assert "degraded" in result and result["duration_ms"] >= 0
    # 无券商连接：bars_sync 记录错误但管线继续（跨步降级语义）
    assert any("calendar" in d for d in result["degraded"])


def test_default_eod_schedule_18_30_enabled(eod_db):
    """F9 修复：默认 EOD 调度存在、18:30 工作日、enabled=True。"""
    sid = eod_mod.ensure_default_schedule(eod_db)
    assert sid
    from app.runtime.schedules import ScheduleStore
    row = ScheduleStore(eod_db).get(sid)
    assert row["cron"] == "30 18 * * 1-5"
    assert row["enabled"] is True
    # 幂等：重复调用不新建
    assert eod_mod.ensure_default_schedule(eod_db) == sid
    rows = eod_db.query("SELECT COUNT(*) AS n FROM schedules")
    assert rows[0]["n"] == 1


def test_eod_jobkind_submittable(eod_db, monkeypatch):
    """system.eod 可作为 JobKind 提交执行（走真实管线）。"""
    monkeypatch.setattr("core.db.get_db", lambda: eod_db)
    _stub_bars_hub(monkeypatch)

    class _Store:
        def get_stock_list(self):
            return []
    import datasource.local_store as ls
    monkeypatch.setattr(ls, "get_store", lambda: _Store())

    runner = sj._eod_runner({"lookback": 5})
    result = _run(runner({"report": lambda p, m: None}))
    assert "steps" in result
