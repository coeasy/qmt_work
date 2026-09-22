"""选股结果落库（``screen_picks`` / ``app/screener/picks.py``）契约测试。

为什么需要它
------------
选股结果此前**只存在于作业返回的 JSON 里**：手动选股关掉页面就没了；定时选股跑完
用户只能去「任务运行时」翻一个巨大的 JSON 字段。于是「每天收盘后自动选股」这条
链路事实上是**跑给日志看的** —— 没有任何界面能稳定看到昨天的选股结果。

锁定五条不变量（错了会出真事）：

1. **跑过就必须留下痕迹**：命中 0 只也要落一条运行记录，否则「跑过但没选出票」与
   「从来没跑过」在界面上完全一样；
2. **``scanned`` / ``bar_date`` 必须落库**：``scanned == 0`` 意味着「根本没拿到数据」
   （同步没跑成），与「行情不好所以没命中」是两件事，不记录就永远分不清；
3. **``reason`` 不能丢**：命中理由是「为什么选中它」的唯一解释，也是原先被前端
   ``detailText`` 跳过的字段；
4. **``source`` 要能区分手动 / 定时**：用户必须知道这批票是我自己跑的还是一早自动跑的；
5. **未初始化 DB 时落库失败不得影响选股结果返回**（降级而非崩）。
"""
from __future__ import annotations

import pytest

from core import db as db_mod
from app.screener import picks as picks_mod


@pytest.fixture
def db(tmp_path):
    """独立临时库（用完还原全局 DB，避免污染其它用例）。"""
    prev = db_mod._db
    db_mod.init_db(tmp_path / "picks.db")
    try:
        yield db_mod.get_db()
    finally:
        db_mod._db = prev


def _row(code="600519.SH", close=1500.0, chg=3.2, reason="创60日新高+成交额达标+阳线", **over):
    r = {"code": code, "close": close, "change_pct": chg, "reason": reason,
         "ma20": 1450.5, "vol_ratio": 1.9, "is_new_high": True}
    r.update(over)
    return r


def test_save_and_read_roundtrip(db):
    saved = picks_mod.save_run(
        results={"turtle_trade": [_row(), _row("000001.SZ", 11.2, -1.1, "未同时满足（新高/成交额/阳线）")]},
        scanned=5200, source="manual", bar_date="20260918",
    )
    assert saved["saved"] == 2 and saved["total_hits"] == 2 and saved["error"] == ""

    out = picks_mod.picks_of_run()
    assert out["run"] is not None
    run = out["run"]
    assert run["run_id"] == saved["run_id"]
    assert run["source"] == "manual"
    assert run["hits"] == 2
    assert run["scanned"] == 5200
    assert run["bar_date"] == "20260918"
    assert run["strategies"] == ["turtle_trade"]
    assert len(out["picks"]) == 2


def test_reason_is_preserved(db):
    """★ 命中理由必须落库并原样读出（它是「为什么选中它」的唯一解释）。"""
    picks_mod.save_run(results={"turtle_trade": [_row(reason="涨停后回踩不破位且再度转强")]},
                       scanned=10, source="manual")
    out = picks_mod.picks_of_run()
    assert out["picks"][0]["reason"] == "涨停后回踩不破位且再度转强"


def test_detail_json_carries_other_fields_but_not_columns(db):
    """其余明细进 detail；已单独成列的字段不重复塞进去。"""
    picks_mod.save_run(results={"turtle_trade": [_row()]}, scanned=10, source="manual")
    d = picks_mod.picks_of_run()["picks"][0]["detail"]
    assert d["ma20"] == 1450.5 and d["vol_ratio"] == 1.9 and d["is_new_high"] is True
    for k in ("code", "close", "change_pct", "reason"):
        assert k not in d


def test_zero_hits_still_records_a_run(db):
    """★ 命中 0 只也要留下运行记录 —— 否则「跑过没选中」与「从没跑过」界面一样。"""
    saved = picks_mod.save_run(results={"turtle_trade": []}, scanned=5200,
                               source="schedule", job_id="job-1", bar_date="20260918")
    assert saved["total_hits"] == 0
    run = picks_mod.latest_run()
    assert run is not None
    assert run["hits"] == 0
    assert run["scanned"] == 5200          # ← 关键：能看出「扫了 5200 只但没命中」
    assert run["source"] == "schedule"
    assert run["job_id"] == "job-1"


def test_scanned_zero_is_visible(db):
    """★ ``scanned == 0`` 表示「根本没拿到数据」，必须能读出来。"""
    picks_mod.save_run(results={}, scanned=0, source="schedule", bar_date="")
    run = picks_mod.latest_run()
    assert run is not None and run["scanned"] == 0 and run["bar_date"] == ""


def test_source_filter_separates_manual_and_schedule(db):
    picks_mod.save_run(results={"turtle_trade": [_row()]}, scanned=1, source="manual")
    picks_mod.save_run(results={"turtle_trade": [_row()]}, scanned=2, source="schedule")
    assert picks_mod.latest_run(source="manual")["scanned"] == 1
    assert picks_mod.latest_run(source="schedule")["scanned"] == 2


def test_degraded_metadata_is_persisted(db):
    picks_mod.save_run(results={"turtle_trade": [_row()]}, scanned=1, source="manual",
                       degraded=True, degraded_reason="已降级到本地仓")
    run = picks_mod.latest_run()
    assert run["degraded"] is True
    assert run["degraded_reason"] == "已降级到本地仓"


def test_empty_db_reports_no_run(db):
    """库里没有任何记录 ⇒ ``run`` 为 None（前端显示「尚未跑过」，不是空表格）。"""
    out = picks_mod.picks_of_run()
    assert out["run"] is None and out["picks"] == []


def test_picks_of_run_can_select_a_specific_run(db):
    a = picks_mod.save_run(results={"turtle_trade": [_row("600519.SH")]}, scanned=1, source="manual")
    picks_mod.save_run(results={"turtle_trade": [_row("000001.SZ")]}, scanned=1, source="manual")
    out = picks_mod.picks_of_run(a["run_id"])
    assert [p["code"] for p in out["picks"]] == ["600519.SH"]


def test_picks_can_filter_by_strategy(db):
    picks_mod.save_run(results={
        "turtle_trade": [_row("600519.SH")],
        "ma_volume": [_row("000001.SZ")],
    }, scanned=2, source="manual")
    out = picks_mod.picks_of_run(strategy="ma_volume")
    assert [p["code"] for p in out["picks"]] == ["000001.SZ"]


def test_available_runs_is_listed(db):
    for i in range(3):
        picks_mod.save_run(results={"turtle_trade": [_row()]}, scanned=i, source="manual")
    out = picks_mod.picks_of_run()
    assert len(out["available_runs"]) == 3


def test_write_failure_degrades_instead_of_raising(db, monkeypatch):
    """落库是「让结果看得见」的增强项：失败不得影响选股结果返回。"""
    class _Boom:
        def execute(self, *_a, **_kw):
            raise RuntimeError("db down")

        def executemany(self, *_a, **_kw):
            raise RuntimeError("db down")

        def query(self, *_a, **_kw):
            raise RuntimeError("db down")

    monkeypatch.setattr(db_mod, "get_db", lambda: _Boom())
    saved = picks_mod.save_run(results={"turtle_trade": [_row()]}, scanned=1, source="manual")
    assert saved["saved"] == 0
    assert saved["total_hits"] == 1          # 命中数仍如实给出
    assert "db down" in saved["error"]       # 原因不吞
    assert picks_mod.list_runs() == []       # 读失败降级为空列表，不抛


# ---------------- bars_last_date（数据截至日） ----------------


def test_bars_last_date_takes_max_not_first():
    """★ 取全池**最新**的日期，而不是第一个池子的。"""
    bars = {
        "600519.SH": [{"time": "20250418"}],
        "000001.SZ": [{"time": "20260918"}],
        "300750.SZ": [{"time": "2026-09-17"}],   # 带横线形状也必须能解析
    }
    assert picks_mod.bars_last_date(bars) == "20260918"


def test_bars_last_date_empty_or_broken_returns_blank():
    """解析不了返回 ``""`` —— 绝不猜成今天（否则陈旧数据会被当成最新的）。"""
    assert picks_mod.bars_last_date({}) == ""
    assert picks_mod.bars_last_date({"600519.SH": []}) == ""
    assert picks_mod.bars_last_date({"600519.SH": [{"time": ""}]}) == ""
    assert picks_mod.bars_last_date({"600519.SH": [{}]}) == ""
    assert picks_mod.bars_last_date({"600519.SH": [None]}) == ""


def test_bars_last_date_works_with_barlite_objects():
    """★ 批量路径给的是 ``BarLite`` 对象（只有属性、**没有 ``.get``**），也必须能解析。

    实测（2026-09-20 真实库）：``BarsProvider.get_bars_batch(lite=True)`` 返回
    ``BarLite``，而这里此前写的是 ``last.get("time") if hasattr(last, "get") else None``
    ⇒ 属性访问不到就传 ``None`` 给 ``bar_date()`` ⇒ **恒返回 ``""``**。
    后果：定时选股任务成功、命中 100 只、结果落库，但 ``bar_date=""`` ——
    界面上「这次选股基于哪一天的日线」永远是空的；更严重的是
    「日线落后于最近交易日就先自动补数」的前置体检
    （``last_date and last_date < expect_date``）在 ``last_date == ""`` 时整条为假
    ⇒ **数据陈旧永远触发不了自动补数**，正是这段代码当初要防的事。
    """
    from datasource.models import BarLite

    bars = {
        "600519.SH": [BarLite("20260917", 1.0, 2.0, 0.5, 1.5),
                      BarLite("20260918", 1.0, 2.0, 0.5, 1.8)],
        "000001.SZ": [BarLite("2026-09-16", 1.0, 2.0, 0.5, 1.9)],   # 带横线形状
    }
    assert picks_mod.bars_last_date(bars) == "20260918"
    assert picks_mod.bar_time(BarLite("20260918", 1.0, 2.0, 0.5, 1.8)) == "20260918"
    assert picks_mod.bar_time({"time": "20260918"}) == "20260918"
    assert picks_mod.bar_time(None) == ""
