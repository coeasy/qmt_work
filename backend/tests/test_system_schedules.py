"""默认定时调度（收盘后更新日线 / 经典策略选股）的播种与幂等。

为什么单开一个文件
------------------
``system_jobs.register_all`` 只注册 runner 工厂，**不建任何调度** —— 用户想要
「每天自动更新日线 / 自动选股」得自己填 cron，门槛很高（Sequoia-X 本身就是靠
crontab 在收盘后跑，这里把它内置成开箱即用）。

播种的**危险点**是幂等：若每次启动都无脑重建，用户改过的 cron / 关掉过的开关
会被悄悄覆盖回去 —— 这比「没有默认调度」更难发现（用户以为自己关了任务，其实
第二天又跑了）。所以这里钉死三条：
  1. 首次播种建出全部默认调度；
  2. 二次播种**一条都不新建**（按固定 id 判存在）；
  3. 用户改过的 cron / enabled **永不**被覆盖。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.db  # noqa: E402
from _phase7_support import tmp_db  # noqa: F401,E402
from app.runtime import system_jobs  # noqa: E402
from app.runtime.schedules import ScheduleStore  # noqa: E402
from app.runtime.cron import CronExpr, validate as validate_cron  # noqa: E402


def _bind(monkeypatch, db):
    """把 system_jobs 的 `core.db.get_db` 绑到临时库（它在函数内部延迟导入）。"""
    monkeypatch.setattr(core.db, "get_db", lambda: db)


def test_default_schedules_are_seeded_once(monkeypatch, tmp_db):
    _bind(monkeypatch, tmp_db)
    first = system_jobs.ensure_default_schedules()
    assert len(first) == len(system_jobs.DEFAULT_SCHEDULES)
    ids = {r.get("id") for r in first}
    assert ids == {s["id"] for s in system_jobs.DEFAULT_SCHEDULES}

    # 二次播种必须零新建 —— 否则每次启动都会重复建任务
    again = system_jobs.ensure_default_schedules()
    assert again == []


def test_user_modified_schedule_is_not_overwritten(monkeypatch, tmp_db):
    _bind(monkeypatch, tmp_db)
    system_jobs.ensure_default_schedules()
    store = ScheduleStore(tmp_db)

    sid = system_jobs.DEFAULT_SCHEDULES[0]["id"]
    store.update(sid, cron="45 20 * * 1-5", enabled=False)

    system_jobs.ensure_default_schedules()      # 重启「模拟」
    row = store.get(sid)
    assert row["cron"] == "45 20 * * 1-5"       # 用户改的 cron 原样保留
    assert row["enabled"] in (0, False)         # 用户关掉的开关不被重新打开


def test_deleted_default_is_reseeded(monkeypatch, tmp_db):
    """用户删掉默认调度后再启动 → 重建（与「改过的不覆盖」是互补语义）。

    删掉 == 明确不要，重建听起来矛盾；但这里重建的是**条目本身**（默认 cron），
    因为删除是不可见的（用户删完就忘了），而重建能救回「误删后完全不知道
    为什么不再自动更新」的困境。真不想跑，关掉 enabled 即可 —— 上一条钉住它不被覆盖。
    """
    _bind(monkeypatch, tmp_db)
    system_jobs.ensure_default_schedules()
    store = ScheduleStore(tmp_db)
    sid = system_jobs.DEFAULT_SCHEDULES[0]["id"]
    store.delete(sid)
    assert store.get(sid) is None

    created = system_jobs.ensure_default_schedules()
    assert [r.get("id") for r in created] == [sid]


def test_default_crons_are_parseable():
    """cron 写错的话播种只会在运行时静默不触发 —— 这条把错误前移到这里。"""
    from datetime import datetime

    for spec in system_jobs.DEFAULT_SCHEDULES:
        validate_cron(spec["cron"])                       # 写错 → 这里就 ValueError
        expr = CronExpr.parse(spec["cron"])
        nxt = expr.next_after(datetime(2026, 9, 18, 9, 0))   # 2026-09-18 是周五
        assert nxt is not None and nxt > datetime(2026, 9, 18, 9, 0)
        assert spec["kind"] in system_jobs.SYSTEM_JOB_KINDS


def test_classic_screen_job_is_registered():
    """`system.classic_screen` 必须既有 kind 也有 runner 工厂，否则调度到点空转。"""
    assert "system.classic_screen" in system_jobs.SYSTEM_JOB_KINDS
    factory = system_jobs.runner_for("system.classic_screen")
    assert callable(factory)
    assert callable(factory({"strategies": ["turtle_trade"]}))


def test_ensure_default_schedules_never_raises(monkeypatch, tmp_db):
    """播种失败绝不能阻断启动（DB 未就绪是常见的启动早期状态）。"""
    monkeypatch.setattr(core.db, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert system_jobs.ensure_default_schedules() == []


# ---------------------------------------------------------------------------
# 顺序即依赖：同步必须**早于**选股
# ---------------------------------------------------------------------------
def test_default_schedules_run_sync_before_screen():
    """日线同步必须早于经典选股，且**不同分钟**。

    「错开」不是美观问题，是正确性问题：选股跑在日线更新之前，就是拿昨天的
    K 线在选。二者现已**同时**归入 ``local_bars`` 互斥组（app/runtime/jobs.py
    的 ``RESOURCE_GROUP``），所以时间错开只是第二道保险 —— 用户把 cron 改成
    同一时刻也不会并发读到半更新的日线。
    """
    from datetime import datetime

    specs = {s["kind"]: s for s in system_jobs.DEFAULT_SCHEDULES}
    sync_spec = specs["system.sync_bars"]
    screen_spec = specs["system.classic_screen"]

    # 2026-09-18 是周五，两者都应在当天触发
    day = datetime(2026, 9, 18, 0, 0)
    sync_at = CronExpr.parse(sync_spec["cron"]).next_after(day)
    screen_at = CronExpr.parse(screen_spec["cron"]).next_after(day)

    assert sync_at < screen_at, (sync_spec["cron"], screen_spec["cron"])
    assert (sync_at.hour, sync_at.minute) != (screen_at.hour, screen_at.minute)


def test_default_sync_bars_is_incremental():
    """默认调度必须是增量 —— 全量回补是几小时的作业，绝不能做成默认。"""
    spec = next(s for s in system_jobs.DEFAULT_SCHEDULES
                if s["kind"] == "system.sync_bars")
    assert spec["params"].get("mode") == "incremental"


# ---------------------------------------------------------------------------
# v27 迁移：把**旧种子值**上的时间改掉，但绝不碰用户改过的配置
# ---------------------------------------------------------------------------
def _apply_v27(db):
    from core.db import _split_statements
    from core.db_migrations import MIGRATIONS

    sql = dict(MIGRATIONS).get(27)
    assert sql, "缺少迁移 v27"
    for stmt in _split_statements(sql):
        db.execute(stmt)


def _insert_schedule(db, sid, cron):
    db.execute(
        "INSERT OR REPLACE INTO schedules (id, name, kind, cron, enabled, "
        "params_json) VALUES (?,?,?,?,1,'{}')",
        (sid, "测试", "system.sync_bars", cron))


def test_migration_v27_rewrites_legacy_seed_crons(tmp_db):
    """存量库停在旧种子时间（15:30 / 16:00）⇒ 迁移到 16:00 / 16:15。"""
    _insert_schedule(tmp_db, "sch-default-sync-bars", "30 15 * * 1-5")
    _insert_schedule(tmp_db, "sch-default-classic-screen", "0 16 * * 1-5")

    _apply_v27(tmp_db)

    rows = {r["id"]: r["cron"] for r in tmp_db.query(
        "SELECT id, cron FROM schedules")}
    assert rows["sch-default-sync-bars"] == "0 16 * * 1-5"
    assert rows["sch-default-classic-screen"] == "15 16 * * 1-5"


def test_migration_v27_keeps_user_modified_crons(tmp_db):
    """用户自己改过的 cron 不是旧种子值 ⇒ 迁移**必须原样保留**。

    这是迁移最容易犯的错：一条无条件的 ``UPDATE ... SET cron=新值`` 会把所有
    用户配置抹平，而且用户完全无从察觉（改完第二天又变回去了）。
    """
    _insert_schedule(tmp_db, "sch-default-sync-bars", "45 20 * * 1-5")
    _insert_schedule(tmp_db, "sch-default-classic-screen", "5 9 * * 1-5")

    _apply_v27(tmp_db)

    rows = {r["id"]: r["cron"] for r in tmp_db.query(
        "SELECT id, cron FROM schedules")}
    assert rows["sch-default-sync-bars"] == "45 20 * * 1-5"
    assert rows["sch-default-classic-screen"] == "5 9 * * 1-5"


def test_migration_v27_is_idempotent(tmp_db):
    """重复应用不产生变化（迁移可能因回滚/重放被再执行一次）。"""
    _insert_schedule(tmp_db, "sch-default-sync-bars", "30 15 * * 1-5")

    _apply_v27(tmp_db)
    first = {r["id"]: r["cron"] for r in tmp_db.query(
        "SELECT id, cron FROM schedules")}
    _apply_v27(tmp_db)
    second = {r["id"]: r["cron"] for r in tmp_db.query(
        "SELECT id, cron FROM schedules")}

    assert first == second
    assert first["sch-default-sync-bars"] == "0 16 * * 1-5"


# ---------------------------------------------------------------------------
# 相位自愈（normalize_phases）
# ---------------------------------------------------------------------------

def _set_phase(db, sid, phase):
    db.execute("UPDATE schedules SET next_run_at=? WHERE id=?", (phase, sid))


def test_normalize_phases_fixes_phase_that_lost_sync_with_cron(tmp_db):
    """★ 相位与 cron 对不上时必须清空，交给调度器按当前 cron 重新对表。

    实测（2026-09-20 真实库）：迁移 v27 把默认同步从 ``30 15`` 改到 ``0 16``、
    选股从 ``0 16`` 改到 ``15 16``，但 ``UPDATE schedules SET cron=...`` **没有**
    重算 ``next_run_at`` ⇒ 同步相位仍停在 15:30、选股相位停在 16:00。
    而 ``ScheduleRunner._fire`` 是**按相位**触发的，于是它真的会在 15:30 跑同步、
    16:00 跑选股 —— 把「先同步、再选股」**倒着**执行一次，选股读到半更新的日线，
    正是 v27 想避免的事。配置看起来却完全正常。
    """
    _insert_schedule(tmp_db, "s1", "0 16 * * 1-5")           # 16:00
    _set_phase(tmp_db, "s1", "2026-09-21T15:30:00")          # 旧相位（15:30）
    _insert_schedule(tmp_db, "s2", "15 16 * * 1-5")          # 16:15
    _set_phase(tmp_db, "s2", "2026-09-21T16:00:00")          # 旧相位（16:00）

    fixed = ScheduleStore(tmp_db).normalize_phases()

    assert sorted(fixed) == ["s1", "s2"]
    assert {r["id"]: r["next_run_at"] for r in tmp_db.query(
        "SELECT id, next_run_at FROM schedules")} == {"s1": "", "s2": ""}


def test_normalize_phases_keeps_consistent_phase(tmp_db):
    """相位本来就是 cron 的合法触发点 ⇒ **不许动它**。

    否则「已到期该补跑的调度」会被清空相位而丢掉补跑机会
    （``_fire`` 对空相位的行为是「重算下一相位、不立即触发」）。
    """
    _insert_schedule(tmp_db, "s1", "0 16 * * 1-5")
    _set_phase(tmp_db, "s1", "2026-09-21T16:00:00")           # 正是 16:00
    assert ScheduleStore(tmp_db).normalize_phases() == []
    assert tmp_db.query("SELECT next_run_at FROM schedules WHERE id='s1'")[0][
        "next_run_at"] == "2026-09-21T16:00:00"


def test_normalize_phases_keeps_past_due_phase(tmp_db):
    """**已过期**的相位同样是合法触发点 ⇒ 保留，让它照常补跑。

    「过期」不等于「不一致」：这是 catch_up / coalesce 语义的基础。
    """
    _insert_schedule(tmp_db, "s1", "0 16 * * 1-5")
    _set_phase(tmp_db, "s1", "2020-01-06T16:00:00")           # 很久以前，但合法
    assert ScheduleStore(tmp_db).normalize_phases() == []


def test_normalize_phases_skips_unparseable_cron(tmp_db):
    """cron 本身非法时不碰它（不是这里该修的问题，乱清相位只会更难查）。"""
    _insert_schedule(tmp_db, "s1", "这不是 cron")
    _set_phase(tmp_db, "s1", "2026-09-21T15:30:00")
    assert ScheduleStore(tmp_db).normalize_phases() == []


def test_normalize_phases_ignores_empty_phase(tmp_db):
    """空相位本来就是「待对表」状态，不算不一致。"""
    _insert_schedule(tmp_db, "s1", "0 16 * * 1-5")
    _set_phase(tmp_db, "s1", "")
    assert ScheduleStore(tmp_db).normalize_phases() == []


# ---------------------------------------------------------------------------
# 时区（schedules.timezone 此前是**装饰性字段**：写了但全代码从不读取）
# ---------------------------------------------------------------------------

def test_next_after_tz_returns_naive_local():
    """★ 存进库的必须是 naive 本地时间 —— 全项目时间戳都是这个口径。

    存一个带偏移的字符串进去，其余比较点（parse_iso vs local_now）会按本地读，
    偏移被丢弃 ⇒ 触发时间整整差一小时，且只在跨时区机器上出现。
    """
    from datetime import datetime

    from app.runtime.schedules import next_after_tz

    nxt = next_after_tz(CronExpr.parse("0 16 * * 1-5"),
                        datetime(2026, 9, 19, 10, 0, 0), "Asia/Shanghai")
    assert nxt.tzinfo is None, f"返回了带时区的时间：{nxt!r}"


def test_next_after_tz_shifts_by_offset():
    """同一 cron 在上海 vs 东京触发：东京 16:00 = 上海 15:00 ⇒ 早一小时。"""
    from datetime import datetime

    from app.runtime.schedules import next_after_tz

    expr = CronExpr.parse("0 16 * * *")
    now = datetime(2026, 9, 19, 10, 0, 0)
    sh = next_after_tz(expr, now, "Asia/Shanghai")
    tk = next_after_tz(expr, now, "Asia/Tokyo")
    delta = (tk - sh).total_seconds() / 3600.0
    assert abs(delta - (-1.0)) < 1e-6, f"上海/东京应相差 1 小时，实际 {delta} 小时"


def test_next_after_tz_unknown_falls_back_to_local():
    """未知时区**降级**而不是抛异常：一个坏字段不该让整台调度停摆。"""
    from datetime import datetime

    from app.runtime.schedules import next_after_tz

    expr = CronExpr.parse("0 16 * * *")
    now = datetime(2026, 9, 19, 10, 0, 0)
    assert next_after_tz(expr, now, "Mars/Olympus") == expr.next_after(now)
    assert next_after_tz(expr, now, "") == expr.next_after(now)


def test_update_cron_recomputes_phase(tmp_db):
    """★ 改 cron 必须**立刻**重算 next_run_at。

    保留旧相位会让新时间在旧相位到达前一次都不触发 —— 界面上就是
    「改了触发时间没反应」，而调度看起来完全正常。
    """
    from app.runtime.schedules import ScheduleStore

    _insert_schedule(tmp_db, "sch-tz-1", "0 16 * * 1-5")
    store = ScheduleStore(tmp_db)
    before = store.get("sch-tz-1")["next_run_at"]
    store.update("sch-tz-1", cron="0 9 * * 1-5")
    after = store.get("sch-tz-1")["next_run_at"]
    assert after and after != before, "改了 cron 却沿用旧相位（新时间不会触发）"
    # 9 点触发 → 下一次的小时必须是 9
    assert after[11:13] == "09", after


def test_update_rejects_unknown_timezone(tmp_db):
    """timezone 现在**真的会生效**，因此非法值必须当场拒绝，不能静默收下。"""
    import pytest

    from app.runtime.schedules import ScheduleStore

    _insert_schedule(tmp_db, "sch-tz-2", "0 16 * * 1-5")
    store = ScheduleStore(tmp_db)
    with pytest.raises(ValueError):
        store.update("sch-tz-2", timezone="Mars/Olympus")
