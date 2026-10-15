"""同步状态落库（``app/sync/state.py`` + 两条同步路径接入）契约测试。

为什么需要它
------------
「今天的数据到底同步了没有」是用户每天真正要问的问题，而在本次改动之前它
**答不上来**：

- 热窗口刷新的结果只写在进程内存（``AppContext._market_sync_last``）⇒ 一重启就归零；
- 全市场日线同步的结果只落在 ``runtime_jobs.result_json``，作业表会被裁剪；
- 而且**只有成功的运行才留痕** —— 「今天没跑」与「跑了但失败」在界面上长得一模一样。

锁定五条不变量（错了会出真事）：

1. **跑过就必须留下痕迹**，失败/部分成功/跳过同样要写（``status`` 三态 + ``skipped``）；
2. **从没跑过 ≠ 跑了但失败**：前者 ``last_run()`` 返回 ``None``，后者返回 ``status="error"``；
3. **每个流只保留最新一条**（``stream`` 唯一键，幂等覆盖），不无限增长；
4. **落库失败只降级不抛**：状态记录是观测增强项，不能把同步本身带崩；
5. **「ok 很高但数据全是陈的」不得记成 ok**（第三种假成功，``stale`` 必须进状态）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.sync import state as st_mod
from core import db as db_mod


@pytest.fixture
def db(tmp_path):
    """独立临时库（用完还原全局 DB，避免污染其它用例）。"""
    prev = db_mod._db
    db_mod.init_db(tmp_path / "sync_state.db")
    try:
        yield db_mod.get_db()
    finally:
        db_mod._db = prev


# ------------------------------------------------------------------ state 本体

def test_record_then_read_roundtrip(db):
    rec = st_mod.record_run(st_mod.STREAM_MARKET_SYNC, status="ok",
                            detail={"codes": 5, "ok": 5, "fail": 0})
    assert rec["persisted"] is True and rec["error"] == ""

    out = st_mod.last_run(st_mod.STREAM_MARKET_SYNC)
    assert out is not None
    assert out["status"] == "ok"
    assert out["detail"]["codes"] == 5
    assert out["detail"]["status"] == "ok"      # detail 里也冗余一份，便于单独读 detail
    assert out["last_ts"], "没有时间戳的记录无法回答「什么时候跑的」"


def test_never_ran_returns_none(db):
    """★ 「从没跑过」必须与「跑了但失败」可区分 —— 这是本次改动最核心的一条。"""
    assert st_mod.last_run(st_mod.STREAM_SYNC_BARS) is None


def test_record_run_is_idempotent_per_stream(db):
    st_mod.record_run(st_mod.STREAM_SYNC_BARS, status="error", detail={"error": "boom"})
    st_mod.record_run(st_mod.STREAM_SYNC_BARS, status="ok", detail={"ok": 1})
    rows = db.query("SELECT COUNT(*) AS n FROM sync_state WHERE stream=?",
                    (st_mod.STREAM_SYNC_BARS,))
    assert int(rows[0]["n"]) == 1, "同一 stream 只保留最新一条，否则表会无限增长"
    out = st_mod.last_run(st_mod.STREAM_SYNC_BARS)
    assert out["status"] == "ok" and out["detail"]["ok"] == 1


def test_failure_also_recorded(db):
    """★ 失败必须留下痕迹：否则「今天没跑」与「跑了但失败」在界面上一样。"""
    st_mod.record_run(st_mod.STREAM_SYNC_BARS, status="error",
                      detail={"error": "股票池为空"})
    out = st_mod.last_run(st_mod.STREAM_SYNC_BARS)
    assert out["status"] == "error"
    assert "股票池为空" in out["detail"]["error"]


def test_partial_and_skipped_are_distinct_states(db):
    """三态 + skipped 都必须能如实落库（partial 是最容易被压成 ok 的那一档）。"""
    for status in ("ok", "partial", "error", "skipped"):
        st_mod.record_run("s." + status, status=status, detail={"reason": status})
        assert st_mod.last_run("s." + status)["status"] == status


def test_all_runs_lists_every_stream(db):
    st_mod.record_run(st_mod.STREAM_MARKET_SYNC, status="ok")
    st_mod.record_run(st_mod.STREAM_SYNC_BARS, status="partial")
    runs = {r["stream"]: r["status"] for r in st_mod.all_runs()}
    assert runs == {st_mod.STREAM_MARKET_SYNC: "ok", st_mod.STREAM_SYNC_BARS: "partial"}


def test_last_ts_explicit_wins(db):
    st_mod.record_run(st_mod.STREAM_MARKET_SYNC, status="ok",
                      last_ts="2026-09-19T16:00:00")
    assert st_mod.last_run(st_mod.STREAM_MARKET_SYNC)["last_ts"] == "2026-09-19T16:00:00"


def test_record_degrades_when_db_unavailable(monkeypatch):
    """落库失败只降级不抛 —— 状态记录是观测增强项，不该把同步本身带崩。"""
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(db_mod, "get_db", lambda: _Boom())
    rec = st_mod.record_run(st_mod.STREAM_MARKET_SYNC, status="ok")
    assert rec["persisted"] is False
    assert "locked" in rec["error"]


def test_last_run_degrades_when_db_unavailable(monkeypatch):
    class _Boom:
        def query_one(self, *a, **k):
            raise RuntimeError("no such table: sync_state")

    monkeypatch.setattr(db_mod, "get_db", lambda: _Boom())
    assert st_mod.last_run(st_mod.STREAM_MARKET_SYNC) is None


def test_all_runs_degrades_when_db_unavailable(monkeypatch):
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("db gone")

    monkeypatch.setattr(db_mod, "get_db", lambda: _Boom())
    assert st_mod.all_runs() == []


def test_corrupt_detail_json_does_not_crash(db):
    """detail_json 坏掉（手工改库 / 旧版本残留）不得让端点 500。"""
    db.execute("INSERT INTO sync_state (stream,last_seq,last_ts,status,detail_json)"
               " VALUES (?,?,?,?,?)", ("x", 0, "2026-09-19T16:00:00", "ok", "{not json"))
    out = st_mod.last_run("x")
    assert out is not None and out["detail"] == {}


# ------------------------------------------------- system.sync_bars 路径接入

def _run_bars(params, summary, monkeypatch, raise_exc=None):
    """按真实调用形态跑一遍 ``system.sync_bars``（runner_for 返回的是**工厂**）。"""
    from app.runtime import jobs as jobs_mod
    from app.runtime import system_jobs

    async def _fake(job):
        if raise_exc is not None:
            raise raise_exc
        return summary

    monkeypatch.setattr(jobs_mod, "sync_runner", lambda p: _fake)
    factory = system_jobs.runner_for("system.sync_bars")
    assert factory is not None
    runner = factory(params)
    return asyncio.run(runner({"id": "job-1"}))


def _last_bars(zone: str = "hot"):
    """读日线同步状态 —— **必须按 zone 读分区流**，不能读遗留的 ``sync.bars``。

    ⚠️ 这是本文件曾经全红 5 条的根因（2026-10-14 查明）：v0.5.2 的冷热分层把
    写入从 ``sync.bars`` 拆成了 ``sync.bars.hot`` / ``sync.bars.cold``，但这 5 条
    由 ``_run_bars`` 驱动的用例仍读 ``STREAM_SYNC_BARS``（"sync.bars"）——
    于是 ``last_run`` 恒返回 ``None``，断言全部落空。
    ``STREAM_SYNC_BARS`` 现在**只**用于读取改造前的旧库记录（见 ``app/sync/state.py``）。
    """
    return st_mod.last_run(st_mod.sync_bars_stream(zone))


def test_sync_bars_records_ok(db, monkeypatch):
    out = _run_bars({}, {"total": 5224, "ok": 5224, "failed": 0, "stale": 0,
                         "bars_written": 100, "as_of_max": "20260918",
                         "errors": []}, monkeypatch)
    assert out["ok"] == 5224
    rec = _last_bars()
    assert rec is not None and rec["status"] == "ok"
    assert rec["detail"]["total"] == 5224
    assert rec["detail"]["as_of_max"] == "20260918"
    assert rec["detail"]["job_id"] == "job-1"


def test_sync_bars_stale_marks_partial(db, monkeypatch):
    """★ 「ok 很高但数据全是陈的」不得记成 ok —— 这是第三种假成功。

    实测（2026-09-18）：total=5224 / ok=5153 / stale=5093，界面显示「已完成」，
    但 5093 只的最后一根停在一年多前。状态必须把 stale 顶出来。
    """
    _run_bars({}, {"total": 5224, "ok": 5224, "failed": 0, "stale": 5093,
                   "bars_written": 1630066, "as_of_max": "20250418",
                   "errors": []}, monkeypatch)
    rec = _last_bars()
    assert rec["status"] == "partial"
    assert rec["detail"]["stale"] == 5093
    assert rec["detail"]["as_of_max"] == "20250418"


def test_sync_bars_failure_recorded_and_reraised(db, monkeypatch):
    """失败必须**既落库又抛出**：只落库会让作业显示成功，只抛出会让界面失忆。"""
    with pytest.raises(RuntimeError):
        _run_bars({}, {}, monkeypatch, raise_exc=RuntimeError("股票池为空"))
    rec = _last_bars()
    assert rec is not None and rec["status"] == "error"
    assert "股票池为空" in rec["detail"]["error"]


def test_sync_bars_errors_truncated(db, monkeypatch):
    """errors 在全市场量级可能上千条 —— 只留前 20 条并如实报告被截断多少。"""
    errs = [{"code": f"600{i:03d}", "error": "x"} for i in range(50)]
    _run_bars({}, {"total": 60, "ok": 10, "failed": 50, "stale": 0,
                   "bars_written": 10, "errors": errs}, monkeypatch)
    rec = _last_bars()
    assert len(rec["detail"]["errors"]) == 20
    assert rec["detail"]["errors_truncated"] == 30


# ---------------------------------------------------------------------------
# ⑥ 两条流不得互相冒充（V11 §5.3 P0-3 III）
# ---------------------------------------------------------------------------
def test_sync_bars_detail_exposes_full_backfill_fields(db, monkeypatch):
    """``sync.bars`` 的 detail 必须带 ``sync_mode`` / ``paged`` / ``as_of_min`` /
    ``skipped_complete`` —— 界面靠它们区分「全量真补齐」与「名义全量」。

    ⚠️ 同步模式落库键是 **``sync_mode``**，不是 ``mode``：``mode`` 早已是**流的
    判别标签**（``"sync_bars"`` / ``"market.sync"``）。两者混用会让界面把全量回补
    误判成普通增量同步。
    """
    _run_bars({}, {"total": 10, "ok": 10, "failed": 0, "stale": 0, "bars_written": 99,
                   "mode": "full", "paged": True, "as_of_min": "20140102",
                   "skipped_complete": 4100}, monkeypatch)
    d = _last_bars()["detail"]
    assert d["mode"] == "sync_bars"        # 流标签，保持不变
    assert d["sync_mode"] == "full"        # 同步模式
    assert d["paged"] is True
    assert d["as_of_min"] == "20140102"
    assert d["skipped_complete"] == 4100


def test_sync_status_exposes_both_streams(db, monkeypatch):
    """``GET /market/kline/sync-status`` 必须**同时**给出两条流的落库记录。

    ★ 为什么不能只给一条：``last_run_persisted`` 是**热窗口刷新**（``market.sync``），
    它的 detail 里**根本没有** ``stale`` / ``sync_mode`` / ``paged`` 这些键。
    界面若把「全量回补到底成没成」「数据够不够新」从它读，会**永远读到 undefined**，
    表现为「按钮点了没反应」—— 而单看端点返回 200 是发现不了的。
    """
    from app.routes.market import kline_sync_status

    st_mod.record_run(st_mod.STREAM_MARKET_SYNC, status="ok",
                      detail={"mode": "market.sync", "codes": 5200})
    st_mod.record_run(st_mod.STREAM_SYNC_BARS, status="ok",
                      detail={"mode": "sync_bars", "sync_mode": "full", "paged": False})

    class _Ms:
        enabled = True
        sync_time = "16:00"

    class _Ctx:
        market_sync = _Ms()
        runtime_config = None
        kline_cache = None

    body = asyncio.run(kline_sync_status(ctx=_Ctx()))
    payload = body.get("data", body)
    assert payload["last_run_persisted"]["detail"]["mode"] == "market.sync"
    assert payload["last_bars_run"]["detail"]["mode"] == "sync_bars"
    assert payload["last_bars_run"]["detail"]["paged"] is False


def test_sync_status_keeps_history_when_synchronizer_absent(db, monkeypatch):
    """同步器没装配时，落库历史**不能跟着一起消失**。

    ★ 「调度器没启动」与「从来没有同步过」是两件事。若在 not-initialized 分支里
    丢掉两条流的记录，用户在**出故障的那一刻**恰好失去唯一线索
    （上次跑到哪、成没成），而这正是他最需要它的时候。
    """
    from app.routes.market import kline_sync_status

    st_mod.record_run(st_mod.STREAM_SYNC_BARS, status="error",
                      detail={"mode": "sync_bars", "error": "股票池为空"})

    class _Ctx:
        market_sync = None
        runtime_config = None
        kline_cache = None

    payload = asyncio.run(kline_sync_status(ctx=_Ctx()))
    payload = payload.get("data", payload)
    assert payload["initialized"] is False
    assert payload["last_bars_run"]["status"] == "error"
    assert "股票池为空" in payload["last_bars_run"]["detail"]["error"]


# ---------------------------------------------------------------------------
# ⑦ 冷热分区流（v0.5.2 拆分）—— 此前**零覆盖**，正是上面 5 条静默变红的成因
# ---------------------------------------------------------------------------
def test_sync_bars_stream_is_partitioned_by_zone():
    """★ ``zone`` → 流名 的映射必须锁死。

    背景（2026-10-14 查明）：v0.5.2 把日线同步的**写入**从 ``sync.bars`` 拆成
    ``sync.bars.hot`` / ``sync.bars.cold``，但**没有任何用例断言这件事** ——
    于是拆分之后，本文件 5 条由 ``_run_bars`` 驱动的用例仍读遗留流名，
    ``last_run`` 恒返回 ``None``，套件一直红着却看不出坏在哪一层。
    这条锁映射本身，下面两条锁接线与缺省推断。
    """
    assert st_mod.sync_bars_stream("hot") == "sync.bars.hot"
    assert st_mod.sync_bars_stream("cold") == "sync.bars.cold"
    # ``both``（mode=full 的缺省推断）走热流：它是「热+冷都做」，主记录归热。
    assert st_mod.sync_bars_stream("both") == "sync.bars.hot"
    # 缺省 / 大小写 / 空白一律收敛到热流，**绝不返回空串** —— 空流名会让所有
    # 记录挤进同一条（等于把分区撤销了，而且界面上看不出来）。
    for z in ("", "  ", None, "HOT", " Hot ", "unknown"):
        assert st_mod.sync_bars_stream(z) == "sync.bars.hot", repr(z)
    assert st_mod.sync_bars_stream("COLD") == "sync.bars.cold"
    assert st_mod.sync_bars_stream(" cold ") == "sync.bars.cold"


def test_sync_bars_runner_writes_partitioned_stream_by_zone(db, monkeypatch):
    """★ 接线：``zone=cold`` 的调度必须写冷流，且**不碰**热流（反之亦然）。

    为什么必须锁：两条调度共用 ``kind == "system.sync_bars"``，而 ``record_run``
    以 stream 为唯一键。若二者写同一条流，后跑的覆盖先跑的 —— 冷补齐在无券商
    连接时落 ``status="error"``，界面读到的「上次日线同步」就成了冷补齐的失败，
    把正常的热同步显示成故障（假告警，最耗排查时间的一类）。
    """
    _run_bars({"zone": "cold"}, {"total": 1, "ok": 1, "failed": 0, "stale": 0,
                                 "bars_written": 1, "errors": []}, monkeypatch)
    assert st_mod.last_run("sync.bars.cold") is not None, "zone=cold 必须写冷流"
    assert st_mod.last_run("sync.bars.hot") is None, "zone=cold 不得污染热流"

    _run_bars({"zone": "hot"}, {"total": 2, "ok": 2, "failed": 0, "stale": 0,
                                "bars_written": 2, "errors": []}, monkeypatch)
    assert st_mod.last_run("sync.bars.hot") is not None, "zone=hot 必须写热流"
    assert st_mod.last_run("sync.bars.cold")["detail"]["total"] == 1, \
        "热流记录不得覆盖冷流记录"


def test_sync_bars_zone_defaults_to_hot_unless_mode_full(db, monkeypatch):
    """``zone`` 缺省推断与 ``jobs.sync_runner`` 一致：``mode=full`` ⇒ both，否则 hot。

    推断错了会让「全量回补」的记录落到热流上，把日常热同步的「上次同步」覆盖掉。
    """
    _run_bars({}, {"total": 1, "ok": 1, "failed": 0, "stale": 0, "errors": []},
              monkeypatch)
    assert st_mod.last_run("sync.bars.hot") is not None
    assert st_mod.last_run("sync.bars.cold") is None, "增量不该写冷流"

    _run_bars({"mode": "full"}, {"total": 3, "ok": 3, "failed": 0, "stale": 0,
                                 "errors": []}, monkeypatch)
    assert st_mod.last_run("sync.bars.hot")["detail"]["total"] == 3, \
        "mode=full ⇒ zone=both ⇒ 主记录仍归热流"


def test_last_hot_bars_run_falls_back_to_legacy_stream(db):
    """★ 旧库兼容：尚无 ``sync.bars.hot`` 时必须回退读遗留 ``sync.bars``。

    分区之前所有记录都在 ``sync.bars`` 上。升级后若只读新流名，老库用户看到的
    「上次同步」会瞬间变成「尚无记录」—— 恰好丢在最需要线索的时刻。
    """
    from app.routes.market import _last_hot_bars_run

    assert _last_hot_bars_run() is None, "两处都没有才叫「从没跑过」"

    st_mod.record_run(st_mod.STREAM_SYNC_BARS, status="ok",
                      detail={"mode": "sync_bars", "total": 7})
    legacy = _last_hot_bars_run()
    assert legacy is not None and legacy["detail"]["total"] == 7, "必须回退读遗留流"

    st_mod.record_run("sync.bars.hot", status="ok",
                      detail={"mode": "sync_bars", "total": 8})
    assert _last_hot_bars_run()["detail"]["total"] == 8, \
        "新流有记录时**优先**新流（回退只是兜底）"
