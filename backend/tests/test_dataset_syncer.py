"""R28 通用数据集同步器测试。

注入的是**取数函数**（与 ``BarsSyncer.fetch_bars`` 同构），不是假数据：返回什么仍由
被测逻辑处理，落库 / 游标 / 溯源 / 汇总全走真实代码路径。

重点钉死三类「假成功」：
- 源返回空 ⇒ 必须 ``ok=False`` + 明确 reason（不能显示「已完成」）；
- 财务无券商 ⇒ 必须明确失败（不能返回空列表冒充成功）；
- 溯源拿不到 ⇒ 记 ``""`` 并置 ``source_unknown``（不能编造源名）。
"""
from __future__ import annotations

import pytest

from core.db import init_db
from datasource.local_store import get_store


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    """每个用例一个独立主库（迁移会自动跑到 v29）。

    ⚠️ 必须同时清掉 ``local_store._store``：它是**进程级单例且持有 DB 实例**，
    ``init_db`` 换了新库之后它仍指向上一个用例的 tmp 库 ⇒ 写进 A 库、在 B 库查，
    表现为「同步成功但查不到数据」——这是最容易误判成产品 bug 的测试污染。
    """
    from datasource import local_store

    init_db(tmp_path / "app.db")
    monkeypatch.setattr(local_store, "_store", None)
    # ★ 冻结「今天是交易日」：逐笔/分时/资金流数据集在非交易日会**整体跳过**，
    #   若不冻结，同一份测试周末跑就会变成「written=0」而被误读成产品 bug
    #   （与 niuniu 侧 `expected_bar_date` 那类随时间漂移的假失败同源）。
    from app.sync import calendar as cal
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    yield


# ---------------------------------------------------------------------------
# 快照类（证券列表）
# ---------------------------------------------------------------------------
def test_snapshot_sync_writes_rows():
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch():
        return ([{"code": "600000.SH", "name": "浦发银行"},
                 {"code": "000001.SZ", "name": "平安银行"}], "tdx")

    s = _run(DatasetSyncer("stock_list", codes=["600000.SH"],
                           fetcher=fake_fetch).sync())
    assert s.ok, s.problems
    assert s.written == 2
    assert s.sources_used == {"tdx": 2}
    rows = get_store().get_stock_list()
    assert {r["code"] for r in rows} == {"600000.SH", "000001.SZ"}


def test_snapshot_empty_source_is_failure_not_success():
    """源返回空 = 数据源不可用，必须失败并说明，绝不「完成但 0 行」。"""
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch():
        return []

    s = _run(DatasetSyncer("stock_list", fetcher=fake_fetch).sync())
    assert not s.ok
    assert s.written == 0
    assert any("空" in p for p in s.problems), s.problems


def test_snapshot_dry_run_does_not_write():
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch():
        return ([{"code": "600000.SH", "name": "浦发"}], "tdx")

    s = _run(DatasetSyncer("stock_list", dry_run=True,
                           fetcher=fake_fetch).sync())
    assert s.ok and s.written == 1
    assert s.dry_run is True
    assert get_store().get_stock_list() == []


def test_snapshot_exception_becomes_problem_not_raise():
    """同步器不抛异常 —— JobRuntime 需要区分「跑完但失败」与「跑挂了」。"""
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch():
        raise RuntimeError("源炸了")

    s = _run(DatasetSyncer("stock_list", fetcher=fake_fetch).sync())
    assert not s.ok
    assert any("源炸了" in p for p in s.problems)


def test_unknown_dataset_raises_keyerror():
    from app.sync.datasets import DatasetSyncer

    with pytest.raises(KeyError):
        DatasetSyncer("nope")


# ---------------------------------------------------------------------------
# 按日切片类（逐笔）
# ---------------------------------------------------------------------------
def test_event_sync_writes_ticks():
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch(code):
        return ([{"tm": "0930", "price": 10.0, "volume": 100},
                 {"tm": "0931", "price": 10.1, "volume": 200}], "tdx")

    s = _run(DatasetSyncer("ticks", codes=["600000.SH"],
                           fetcher=fake_fetch).sync())
    assert s.ok, s.problems
    assert s.written == 2
    assert s.requested == 1
    from core.db import get_db
    rows = get_db().query("SELECT code,tm,price FROM local_ticks WHERE code=?",
                          ("600000.SH",))
    assert len(rows) == 2


def test_event_counts_failures_per_code():
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch(code):
        if code == "BAD":
            raise RuntimeError("这只挂了")
        return ([{"tm": "0930", "price": 1.0}], "tdx")

    s = _run(DatasetSyncer("ticks", codes=["OK1", "BAD", "OK2"],
                           fetcher=fake_fetch).sync())
    assert s.written == 2
    assert s.failed == 1


def test_event_all_failed_reports_problem():
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch(code):
        return None

    s = _run(DatasetSyncer("ticks", codes=["A", "B"],
                           fetcher=fake_fetch).sync())
    assert s.written == 0
    assert not s.ok
    assert s.problems


def test_event_records_progress():
    from app.sync.datasets import DatasetSyncer

    seen = []

    async def fake_fetch(code):
        return ([{"tm": "0930", "price": 1.0}], "tdx")

    s = _run(DatasetSyncer("ticks", codes=[f"C{i}" for i in range(25)],
                           fetcher=fake_fetch,
                           progress_cb=seen.append).sync())
    assert s.written == 25
    # 每 20 个报一次
    assert seen and seen[0]["total"] == 25


# ---------------------------------------------------------------------------
# 真实源形态拆包（★ 回归护栏：源给的是复合 dict，不是裸行列表）
# ---------------------------------------------------------------------------
# 这一组钉死 2026-10-09 修掉的断链：``get_ticks`` 返回 ``{code, items,
# trading_date, source}``、``get_minutes`` 返回 ``{code, trading_date, points}``。
# 早期实现把整个 dict 当「一行」落库 —— 同步报 ok=True，库里却是一行垃圾。
# 只注入 fetcher 的测试**永远测不到这里**（那正是它漏出去的原因），所以必须
# 用假 hub 走真正的 ``_fetch_ticks`` / ``_fetch_minutes``。
def test_ticks_fetcher_unpacks_items_and_trading_date(monkeypatch):
    from app.sync import datasets as dsync
    from app.sync.datasets import DatasetSyncer

    class _Hub:
        async def get_ticks(self, code, count=60, source="auto", conn_id=None):
            return {"code": code, "source": "eltdx", "trading_date": "2026-10-09",
                    "count": 2,
                    "items": [{"time": "09:30:00", "price": 10.0, "volume": 100,
                               "amount": 100000.0, "side": "buy"},
                              {"time": "09:31:00", "price": 10.1, "volume": 200,
                               "amount": 202000.0, "side": "sell"}]}

    monkeypatch.setattr(dsync, "get_hub", lambda: _Hub())
    s = _run(DatasetSyncer("ticks", codes=["600000.SH"]).sync())
    assert s.ok, s.problems
    assert s.written == 2, s.as_dict()
    from core.db import get_db
    rows = get_db().query(
        "SELECT dt,tm,price,volume,bs_flag FROM local_ticks "
        "WHERE code=? ORDER BY seq", ("600000.SH",))
    # 交易日取源给的 trading_date，不是「今天」——休市时源回的是上一交易日
    assert rows[0]["dt"] == "20261009"
    # 源键 time/side 映射到落库列 tm/bs_flag
    assert rows[0]["tm"] == "09:30:00"
    assert rows[0]["bs_flag"] == "buy"
    assert rows[1]["bs_flag"] == "sell"


def test_minutes_fetcher_unpacks_points(monkeypatch):
    from app.sync import datasets as dsync
    from app.sync.datasets import DatasetSyncer

    class _Hub:
        async def get_minutes(self, code, trading_date=None, source="auto"):
            return {"code": code, "trading_date": "2026-10-09", "pre_close": 9.9,
                    "points": [{"t": "09:30", "price": 10.0, "avg": 9.95,
                                "volume": 100},
                               {"t": "09:31", "price": 10.1, "avg": 9.98,
                                "volume": 200}]}

    monkeypatch.setattr(dsync, "get_hub", lambda: _Hub())
    s = _run(DatasetSyncer("minutes", codes=["600000.SH"]).sync())
    assert s.ok, s.problems
    assert s.written == 2, s.as_dict()
    from core.db import get_db
    rows = get_db().query(
        "SELECT dt,tm,price,avg_price,volume FROM local_minutes "
        "WHERE code=? ORDER BY tm", ("600000.SH",))
    assert rows[0]["dt"] == "20261009"
    # 源键 t/avg 映射到落库列 tm/avg_price
    assert rows[0]["tm"] == "09:30"
    assert rows[0]["avg_price"] == 9.95


def test_ticks_source_unavailable_is_failure_not_skip(monkeypatch):
    """源返回 None（所有候选源不可用）必须计 failed，不是「跳过」也不是成功。"""
    from app.sync import datasets as dsync
    from app.sync.datasets import DatasetSyncer

    class _Hub:
        async def get_ticks(self, code, count=60, source="auto", conn_id=None):
            return None

    monkeypatch.setattr(dsync, "get_hub", lambda: _Hub())
    s = _run(DatasetSyncer("ticks", codes=["600000.SH"]).sync())
    assert not s.ok
    assert s.failed == 1 and s.written == 0 and s.skipped == 0


def test_event_skips_entire_batch_on_non_trading_day(monkeypatch):
    """非交易日整体跳过：不能跑完全场再把上个交易日的数据当成今天。"""
    from app.sync import calendar as cal
    from app.sync.datasets import DatasetSyncer

    monkeypatch.setattr(cal, "is_trading_day", lambda d: False)
    called = []

    async def fake_fetch(code):
        called.append(code)
        return ([{"tm": "0930", "price": 1.0}], "tdx")

    s = _run(DatasetSyncer("ticks", codes=["A", "B"], fetcher=fake_fetch).sync())
    assert called == [], "非交易日不应发起任何取数"
    assert s.skipped == 2 and s.written == 0
    assert "非交易日" in s.detail.get("note", "")


# ---------------------------------------------------------------------------
# 财务（仅券商）
# ---------------------------------------------------------------------------
def test_financial_without_broker_fails_loudly(monkeypatch):
    """无券商 ⇒ 明确失败并说明原因，绝不返回空列表冒充成功。"""
    from app.sync import datasets as dsync
    from app.sync.datasets import DatasetSyncer

    class _NoBrokerHub:
        def active_bridge(self, *a, **k):
            return None

    monkeypatch.setattr(dsync, "get_hub", lambda: _NoBrokerHub())
    s = _run(DatasetSyncer("financial", codes=["600000.SH"]).sync())
    assert not s.ok
    assert any("券商" in p for p in s.problems), s.problems


def test_financial_with_broker_writes(monkeypatch):
    from app.sync import datasets as dsync
    from app.sync.datasets import DatasetSyncer

    class _B:
        async def get_financial(self, code):
            return {"period": "2025-12-31", "EPS": 1.23}

    class _Hub:
        def active_bridge(self, *a, **k):
            return _B()

    monkeypatch.setattr(dsync, "get_hub", lambda: _Hub())
    s = _run(DatasetSyncer("financial", codes=["600000.SH"]).sync())
    assert s.ok, s.problems
    assert s.written == 1
    assert s.sources_used == {"broker": 1}
    from core.db import get_db
    rows = get_db().query("SELECT code,period,payload_json FROM local_fundamentals")
    assert len(rows) == 1 and "EPS" in rows[0]["payload_json"]


# ---------------------------------------------------------------------------
# 溯源 / 汇总契约
# ---------------------------------------------------------------------------
def test_missing_source_recorded_as_unknown():
    """源不返回来源名时记 ``""`` 并置 source_unknown，绝不编造源名。"""
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch():
        return [{"code": "600000.SH", "name": "浦发"}]   # 裸 list，无来源

    s = _run(DatasetSyncer("stock_list", fetcher=fake_fetch).sync())
    assert s.ok
    assert s.sources_used == {"": 1}
    d = s.as_dict()
    # 空来源在汇总里体现为「来源未知」，而不是伪装成某个具体源
    assert "" in d["sources_used"]


def test_summary_has_frontend_fields():
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch():
        return ([{"code": "600000.SH"}], "tdx")

    s = _run(DatasetSyncer("stock_list", fetcher=fake_fetch).sync())
    d = s.as_dict()
    for k in ("dataset", "label", "ok", "mode", "requested", "written",
              "skipped", "failed", "source", "sources_used", "problems",
              "dry_run", "started_at", "finished_at", "duration_s"):
        assert k in d, f"前端需要的字段缺失：{k}"


def test_retention_prune_runs_for_event_dataset():
    """按日切片数据集同步后必须执行保留清理（否则磁盘只增不减）。"""
    from app.sync.datasets import DatasetSyncer

    async def fake_fetch(code):
        return ([{"tm": "0930", "price": 1.0}], "tdx")

    s = _run(DatasetSyncer("ticks", codes=["600000.SH"],
                           fetcher=fake_fetch).sync())
    assert "retention_cutoff" in s.detail
    assert s.detail["retention_cutoff"]


def test_dataset_schedules_generated_from_specs():
    """调度由 SSOT 动态生成：新增数据集自动获得调度，不需要另抄一份字面量。"""
    from app.runtime.system_jobs import dataset_default_schedules

    specs = dataset_default_schedules()
    ids = {s["id"] for s in specs}
    assert "sch-default-ds-bars_1w" in ids
    assert "sch-default-ds-stock_list" in ids
    # bars_1d 走专用链路，不重复播种
    assert "sch-default-ds-bars_1d" not in ids
    for s in specs:
        assert s["kind"] == "system.sync_dataset"
        assert s["params"]["dataset"]


def test_sync_dataset_kind_registered():
    from app.runtime.system_jobs import SYSTEM_JOB_KINDS, runner_for

    assert "system.sync_dataset" in SYSTEM_JOB_KINDS
    assert runner_for("system.sync_dataset") is not None


# ---------------------------------------------------------------------------
# K 线类（委托 BarsSyncer）
# ---------------------------------------------------------------------------
async def _future_bars(self, code, period, adjust, count):
    """BarsSyncer 抓取器替身：真实形状的一根 K 线 + 真实来源名。"""
    return ([{"time": "20261008", "open": 10.0, "high": 10.5, "low": 9.9,
              "close": 10.2, "volume": 1000, "amount": 10000.0}], "tdx")


def test_bars_dataset_delegates_to_barssyncer_with_progress(monkeypatch, tmp_path):
    """K 线数据集必须真实委托 BarsSyncer，且进度回调按三参契约调用。

    ★ 钉死一处断链：BarsSyncer 的契约是 ``cb(done, total, code)``；
    DatasetSyncer 曾只声明两参 → 就地 TypeError。而 BarsSyncer 在
    :meth:`sync_one` **落库成功之后**才回调，异常被 ``return_exceptions``
    吃掉并按「任务级失败」计入 ⇒ 表现为「数据写进去了，汇总却说全部失败」
    （假失败，与假绿灯同族：界面显示失败、数据其实是对的，用户会反复重跑）。
    """
    from app.sync.bars import BarsSyncer
    from app.sync.datasets import DatasetSyncer
    from datasource.intraday_store import IntradayStore

    monkeypatch.setattr(BarsSyncer, "_default_fetch", _future_bars)
    seen: list[dict] = []

    st = IntradayStore(tmp_path / "bars_intraday.db")
    s = _run(DatasetSyncer(
        "bars_5m", codes=["600000.SH"], mode="incremental",
        intraday_store=st, progress_cb=seen.append).sync())

    assert s.ok, s.problems
    assert s.written == 1, (s.failed, s.problems, s.detail)
    assert seen, "进度回调从未触发"
    assert seen[0]["total"] == 1
    assert seen[0]["code"] == "600000.SH"
    assert st.latest_dt("600000.SH", "5m") == "20261008"
    assert st.count("5m") == 1
    # 溯源必须来自库里真实落库的 provider_id，不能用请求时的 "auto" 冒充
    assert s.sources_used == {"tdx": 1}, s.sources_used
    assert s.detail["as_of_max"] == "20261008"
    assert s.detail["batch_id"]
    st.close()


def test_last_sync_at_recorded_for_bars_dataset(monkeypatch, tmp_path):
    """K 线/逐笔/财务数据集同步成功后必须写入「上次同步时间」。

    ★ 钉死一处状态断链：早期只有 snapshot 分支写 ``dataset.<id>.last_sync_at``，
    导致 14 个数据集的「上次同步」永远为空，前端一直显示「从未同步」——
    而数据其实已经下载好了。现在统一在 :meth:`sync` 记录（唯一入口）。
    """
    from app.sync.bars import BarsSyncer
    from app.sync.datasets import DatasetSyncer
    from datasource.intraday_store import IntradayStore

    monkeypatch.setattr(BarsSyncer, "_default_fetch", _future_bars)
    st = IntradayStore(tmp_path / "bars_intraday.db")
    s = _run(DatasetSyncer("bars_5m", codes=["600000.SH"], mode="incremental",
                           intraday_store=st).sync())
    assert s.ok, s.problems
    meta = get_store().get_meta("dataset.bars_5m.last_sync_at", "")
    assert meta, "同步成功后必须记录 last_sync_at"
    assert meta == s.finished_at
    st.close()


def test_last_sync_at_not_recorded_on_failure(monkeypatch, tmp_path):
    """失败**不能**写 last_sync_at —— 否则「上次同步」会暗示数据是新的。"""
    from app.sync.bars import BarsSyncer
    from app.sync.datasets import DatasetSyncer
    from datasource.intraday_store import IntradayStore

    async def _boom(self, code, period, adjust, count):
        return None

    monkeypatch.setattr(BarsSyncer, "_default_fetch", _boom)
    st = IntradayStore(tmp_path / "bars_intraday.db")
    s = _run(DatasetSyncer("bars_5m", codes=["600000.SH"], mode="incremental",
                           intraday_store=st).sync())
    assert not s.ok
    assert not get_store().get_meta("dataset.bars_5m.last_sync_at", "")
    st.close()


def test_bars_dry_run_is_readonly(monkeypatch, tmp_path):
    """K 线 dry_run 必须无副作用：不写库、也不执行保留清理。

    ★ 钉死一处断链：BarsSyncer 是「取数 → 立即落库」，没有 dry_run 开关。
    之前直接交真实 store，导致 ``dry_run=true`` 静默改库——而且
    ``retention_days`` 清理会**真的删掉**两周分钟线。预览不能变成删除。
    """
    from app.sync.bars import BarsSyncer
    from app.sync.datasets import DatasetSyncer
    from datasource.intraday_store import IntradayStore

    monkeypatch.setattr(BarsSyncer, "_default_fetch", _future_bars)
    st = IntradayStore(tmp_path / "bars_intraday.db")
    # 先写一行旧数据，验证 dry_run 不会把它清掉
    st.upsert_bars("600000.SH", [{"time": "20261008093000", "open": 1.0,
                                   "high": 1.0, "low": 1.0, "close": 1.0}],
                   period="5m", provider_id="tdx")

    s = _run(DatasetSyncer("bars_5m", codes=["600000.SH"], mode="incremental",
                           intraday_store=st, dry_run=True).sync())
    assert s.ok, s.problems
    assert s.written == 1, (s.problems, s.detail)
    assert s.dry_run is True
    assert st.count("5m") == 1, "dry_run 不应写入新数据"
    assert "dry_run" in s.detail.get("dry_run_note", "")
    assert "pruned" not in s.detail
    st.close()


def test_progress_callback_exception_keeps_written_data(monkeypatch, tmp_path):
    """进度回调抛异常时数据必须已经落库（上一用例的回归护栏）。"""
    from app.sync.bars import BarsSyncer
    from app.sync.datasets import DatasetSyncer
    from datasource.intraday_store import IntradayStore

    monkeypatch.setattr(BarsSyncer, "_default_fetch", _future_bars)

    def _boom(_d):
        raise RuntimeError("回调挂了")

    st = IntradayStore(tmp_path / "bars_intraday.db")
    s = _run(DatasetSyncer(
        "bars_5m", codes=["600000.SH"], mode="incremental",
        intraday_store=st, progress_cb=_boom).sync())
    assert st.latest_dt("600000.SH", "5m") == "20261008", \
        "进度回调异常不应导致数据丢失"
    assert s.failed == 1, "回调异常应如实计入失败"
    st.close()


def test_intraday_store_accepts_barssyncer_contract(monkeypatch, tmp_path):
    """分钟仓必须接收 BarsSyncer 的完整落库签名（含 batch_id 等审计参数）。

    签名漂移是最阴的断链：``unexpected keyword argument`` 只会以一句
    「落库失败」出现在汇总里，而**所有**分钟线数据集都会 100% 失败。
    同时验证 :meth:`BarsSyncer.sync_many` 结尾的 ``set_meta`` 对无 meta 表的
    分钟仓容错（否则数据写完之后抛 AttributeError，整批报成「同步异常」）。
    """
    from app.sync.bars import BarsSyncer
    from app.sync.datasets import DatasetSyncer
    from datasource.intraday_store import IntradayStore

    monkeypatch.setattr(BarsSyncer, "_default_fetch", _future_bars)
    st = IntradayStore(tmp_path / "bars_intraday.db")
    s = _run(DatasetSyncer("bars_15m", codes=["600000.SH"], mode="full",
                           intraday_store=st).sync())
    assert s.ok, (s.problems, s.detail)
    assert s.written == 1, s.detail
    assert st.count("15m") == 1
    st.close()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _run(coro):
    import asyncio
    return asyncio.run(coro)
