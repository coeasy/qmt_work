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


def test_eod_fallback_keeps_primary_summary(eod_db, monkeypatch):
    """备用源续跑**不得**把已成功的主源汇总覆盖成 error（2026-10-10 审计）。

    ★ 钉死的真 bug：``f"主源失败 {len(summary.failed)} 只"`` 里
    ``SyncSummary.failed`` 是 **int**（不是失败列表），``len(int)`` 就地
    ``TypeError`` ⇒ 被外层 ``except`` 捕获 ⇒ ``steps["bars_sync"]`` 被写成
    ``{"error": ...}``，于是「同步明明写进去了」却连快照 quality 一起被判
    ``invalid`` —— 假失败，且根因深埋在 degraded 里。
    """
    monkeypatch.setattr("core.db.get_db", lambda: eod_db)
    _stub_bars_hub(monkeypatch)

    from app.sync.bars import SyncSummary

    calls: list[str] = []

    async def _fake_sync_stock_list(self, limit=None, progress_cb=None):
        calls.append(str(self._provider_id))
        return SyncSummary(started="", finished="", total=2, ok=1, failed=1,
                           bars_written=2, batch_id="bars-test",
                           errors=[{"code": "600000.SH", "error": "源无数据"}])

    import app.sync.bars as bars_mod
    monkeypatch.setattr(bars_mod.BarsSyncer, "sync_stock_list",
                        _fake_sync_stock_list)

    result = _run(eod_mod.run_eod_pipeline(
        {"lookback": 5, "primary_provider": "eltdx",
         "fallback_provider": "tencent"}))

    step = result["steps"]["bars_sync"]
    assert "error" not in step, step          # ★ 成功结果没被覆盖
    assert step["bars_written"] == 2
    assert "fallback" in step                 # 备用源确实续跑了
    assert step["fallback"]["retried_codes"] == 1
    assert calls == ["eltdx", "tencent"]
    # 续跑本身只是降级，不该把整条管线判成失败
    assert any("备用源续跑" in d for d in result["degraded"])


def test_eod_quality_not_invalid_when_all_unchanged(eod_db, monkeypatch):
    """写入裁剪后 ``written=0`` 但 ``bars_unchanged>0`` ＝ 数据已最新，不是 invalid。"""
    monkeypatch.setattr("core.db.get_db", lambda: eod_db)
    _stub_bars_hub(monkeypatch)

    from app.sync.bars import SyncSummary

    async def _fake_sync_stock_list(self, limit=None, progress_cb=None):
        return SyncSummary(started="", finished="", total=2, ok=2, failed=0,
                           bars_written=0, bars_unchanged=640,
                           batch_id="bars-test2")

    import app.sync.bars as bars_mod
    monkeypatch.setattr(bars_mod.BarsSyncer, "sync_stock_list",
                        _fake_sync_stock_list)

    result = _run(eod_mod.run_eod_pipeline({"lookback": 5}))
    assert result["steps"]["snapshot"]["quality_state"] == "provisional"
