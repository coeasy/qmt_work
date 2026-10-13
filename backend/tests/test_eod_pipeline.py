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


class _StubStore:
    """零标的的 LocalStore 替身（EOD 测试锁的是**管线编排**，不是股票列表）。"""

    def get_stock_list(self):
        return []


def _stub_store(monkeypatch):
    """把 ``BarsSyncer`` 构造时用的 LocalStore 换成桩。

    ★ 必须显式打桩，且**打的是 ``app.sync.bars.get_store``**（不是
    ``datasource.local_store.get_store``）：``app/sync/bars.py`` 在**模块级**
    ``from datasource.local_store import get_store`` 绑定了自己的引用，改
    local_store 那个名字不会影响到它。

    不打桩会怎样（2026-10-13 实测）：``BarsSyncer.__init__`` 在调用方没传
    ``store=`` 时落回 ``get_store()`` → ``LocalStore(get_db())``，而
    ``core.db.get_db`` 的打补丁同样够不着（local_store 也是模块级绑定）。
    本文件单独跑时，前面用例留下的**进程级 DB 残留**恰好让 ``core.db._db``
    非空而侥幸通过；一旦前面跑过别的文件（例如 ``test_sync_cold_hot.py``）
    重置了单例，这里就 ``RuntimeError: DB not initialized`` ⇒
    ``steps["bars_sync"]`` 被写成 error、快照步骤连带缺 key ⇒ **顺序相关的
    假失败**。测试前提必须自己搭，不能靠环境残留。
    """
    import app.sync.bars as bars_mod
    monkeypatch.setattr(bars_mod, "get_store", lambda: _StubStore())


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
    _stub_store(monkeypatch)   # ★ 见 _stub_store 说明：不打桩则依赖进程级 DB 残留

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
    _stub_store(monkeypatch)   # ★ 见 _stub_store 说明：不打桩则依赖进程级 DB 残留

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


def test_eod_snapshot_skipped_not_missing_when_sync_yields_no_batch(eod_db, monkeypatch):
    """同步整步失败（无批次）时快照必须**显式 skipped**，不能从结果里消失。

    ★ 钉死的真 bug：批次号取不到时 ``publish_local_bars`` 抛
    「必须提供 dataset_id/version/provider_id/batch_id」⇒ 被本步骤 ``except``
    吃掉 ⇒ ``steps["snapshot"]`` **整个 key 都不存在** ⇒ 下游
    ``result["steps"]["snapshot"]`` 直接 KeyError。消费方分不清「没跑快照」
    和「快照失败」，而真正原因（同步挂了）只埋在 degraded 列表里。
    """
    monkeypatch.setattr("core.db.get_db", lambda: eod_db)
    _stub_bars_hub(monkeypatch)
    _stub_store(monkeypatch)

    import app.sync.bars as bars_mod

    async def _boom(self, limit=None, progress_cb=None):
        raise RuntimeError("模拟同步整步失败")

    monkeypatch.setattr(bars_mod.BarsSyncer, "sync_stock_list", _boom)

    result = _run(eod_mod.run_eod_pipeline({"lookback": 5}))

    # 快照 key 必须在，且如实标注「跳过」而不是假装成功
    assert "snapshot" in result["steps"], result["steps"].keys()
    snap = result["steps"]["snapshot"]
    assert snap["skipped"] is True
    assert snap["quality_state"] == "invalid"
    # 原因可追溯：degraded 里有 snapshot 的跳过说明，也有同步本身的失败
    assert any("snapshot" in d for d in result["degraded"])
    assert any("bars_sync" in d for d in result["degraded"])
