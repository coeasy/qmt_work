"""R28 数据集 SSOT 与分钟线仓的契约测试。

锁的是**声明与承载的一致性**——数据集加一行就能多一种可下载数据，代价是
「声明」与「实现/存储」可能漂移。这里把漂移点逐个钉死：

1. 每个数据集声明的 ``capability`` 必须在能力链里（否则同步必然空转）；
2. 分钟线必须落独立仓且**必须**有保留窗口（否则磁盘与主库 WAL 一起撑爆）；
3. 默认启用的数据集 cron 必须能被调度器解析（否则播种静默失败）；
4. 时间列拆分必须覆盖各源的真实形状（否则整批分钟线被静默丢弃）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from datasource import datasets as DS
from datasource.intraday_store import IntradayStore, split_dt_tm
from datasource.providers import DEFAULT_CAPABILITY_CHAINS


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
def test_ids_unique_and_nonempty():
    assert len(DS.DATASETS) >= 18
    assert all(s.id and s.id.strip() == s.id for s in DS.DATASETS.values())
    assert len(set(DS.DATASETS)) == len(DS.DATASETS)


def test_capability_exists_in_chain():
    """声明的能力必须能在 DEFAULT_CAPABILITY_CHAINS 找到。

    找不到 ⇒ ``resolve_chain`` 返回空链 ⇒ 同步永远空转却报「已完成」。
    """
    for s in DS.DATASETS.values():
        assert s.capability in DEFAULT_CAPABILITY_CHAINS, (
            f"{s.id} 声明的 capability={s.capability} 不在能力链里")


def test_kline_datasets_have_period_and_known_category():
    for s in DS.DATASETS.values():
        if s.category == DS.CAT_BARS:
            assert s.period, f"{s.id} 是 K 线数据集却没有 period"
            assert s.cursor == DS.CUR_BAR_DATE
        if s.cursor == DS.CUR_BAR_DATE:
            assert s.category == DS.CAT_BARS


def test_intraday_datasets_must_have_retention():
    """分钟线必须有保留窗口。

    全市场 1 分钟线约 3 亿行/年；retention_days=0 意味着永不清理，
    这是把「数据量常识」写进断言，防止后来者加数据集时漏配。
    """
    for s in DS.DATASETS.values():
        if s.store == DS.STORE_INTRADAY:
            assert s.retention_days > 0, f"{s.id} 落独立仓却没有保留窗口"


def test_event_datasets_have_retention():
    for s in DS.DATASETS.values():
        if s.cursor == DS.CUR_EVENT:
            assert s.retention_days > 0, f"{s.id} 是按日切片数据却没有保留窗口"


def test_snapshot_datasets_have_no_period():
    """整批刷新的数据集不该有 period —— 有就是概念错配（无时间序列却按周期存）。"""
    for s in DS.DATASETS.values():
        if s.cursor == DS.CUR_SNAPSHOT:
            assert s.period == "", f"{s.id} 是快照类却声明了 period={s.period}"


def test_default_enabled_cron_parseable():
    """默认播种的调度 cron 必须能解析，否则 ScheduleStore.create 会静默失败。"""
    # ``validate()`` 是「通过返回 None、失败抛异常」的风格，不适合当断言值用；
    # 这里直接用 ``parse`` —— 非法表达式会抛，测试失败信息自带数据集 id。
    from app.runtime.cron import CronExpr

    for s in DS.enabled_by_default():
        try:
            CronExpr.parse(s.cron)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{s.id} 的 cron 非法（{s.cron}）：{exc}")


def test_enabled_by_default_excludes_heavy():
    """重量级数据集默认关闭。

    分钟线/逐笔/板块成分是全市场量级，默认开启会让首次启动就去拉几亿行。
    """
    heavy = {s.id for s in DS.DATASETS.values() if "heavy" in s.tags}
    enabled = {s.id for s in DS.enabled_by_default()}
    assert not (heavy & enabled), f"重量级数据集默认开启了：{heavy & enabled}"


def test_bars_1d_prefers_broker_then_third_party():
    """「QMT 优先、否则第三方」必须在声明链里体现（链首 = broker）。"""
    spec = DS.require("bars_1d")
    assert spec.chain[0] == "broker"
    # 第三方兜底至少要有 tdx
    assert "tdx" in spec.chain


def test_ticks_chain_excludes_broker():
    """逐笔刻意不挂 broker：它的存在意义就是无券商也能拿到真实成交流。"""
    assert "broker" not in DS.require("ticks").chain


def test_require_unknown_raises():
    with pytest.raises(KeyError):
        DS.require("not-a-dataset")
    assert DS.get("not-a-dataset") is None


def test_as_dict_has_frontend_fields():
    d = DS.require("bars_5m").as_dict()
    for k in ("id", "label", "category", "category_label", "capability",
              "chain", "store", "cursor", "period", "retention_days", "cron"):
        assert k in d, f"前端需要的字段缺失：{k}"


# ---------------------------------------------------------------------------
# 时间列拆分
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,dt,tm", [
    ("20261008093000", "20261008", "0930"),   # 紧凑 14 位（分钟线源最常见）
    ("202610080930", "20261008", "0930"),     # 紧凑 12 位
    ("20261008", "20261008", ""),             # 纯日期（日线）
    ("2026-10-08 09:30", "20261008", "0930"),  # ISO + 时间
    ("2026/10/08 09:30", "20261008", "0930"),  # 斜杠 + 时间
    ("2026-10-08", "20261008", ""),
    (20261008093000, "20261008", "0930"),     # 数值形态
])
def test_split_dt_tm_formats(raw, dt, tm):
    assert split_dt_tm(raw) == (dt, tm)


@pytest.mark.parametrize("raw", ["", None, "bad", "2026-13-45", "xyz"])
def test_split_dt_tm_invalid(raw):
    """非法值返回空而不是猜一个日期——猜错会让坏数据进主键。"""
    assert split_dt_tm(raw) == ("", "")


def test_split_never_fakes_midnight():
    """无分钟精度时返回 ``""`` 而不是 ``"0000"``。

    ``"0000"`` 会伪装成「当天 0 点的一根真实分钟线」，把日线污染进分钟仓。
    """
    assert split_dt_tm("20261008")[1] == ""


# ---------------------------------------------------------------------------
# 分钟线仓
# ---------------------------------------------------------------------------
def _store(tmp_path: Path) -> IntradayStore:
    return IntradayStore(tmp_path / "bars_intraday.db")


def test_intraday_upsert_and_read(tmp_path):
    st = _store(tmp_path)
    bars = [{"time": "20261008" + f"{h:02d}{m:02d}00", "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "volume": 100, "amount": 150.0}
            for h in (9, 10) for m in (30, 45)]
    assert st.upsert_bars("600000.SH", bars, period="5m", provider_id="tdx") == 4
    got = st.get_bars("600000.SH", "5m")
    assert len(got) == 4
    # 按 dt/tm 升序
    assert [g["tm"] for g in got] == ["0930", "0945", "1030", "1045"]
    assert got[0]["provider_id"] == "tdx"
    st.close()


def test_intraday_idempotent_overwrite(tmp_path):
    """重跑同一批 = 覆盖而非翻倍（主键幂等）。"""
    st = _store(tmp_path)
    b = [{"time": "20261008093000", "close": 1.0, "volume": 10}]
    assert st.upsert_bars("600000.SH", b, period="1m") == 1
    assert st.upsert_bars("600000.SH", b, period="1m") == 1
    assert st.count() == 1
    st.close()


def test_intraday_skips_unparseable_rows(tmp_path):
    st = _store(tmp_path)
    assert st.upsert_bars("600000.SH", [{"time": "bad", "close": 1}],
                          period="1m") == 0
    assert st.count() == 0
    st.close()


def test_intraday_cursors(tmp_path):
    st = _store(tmp_path)
    for day in ("20261006", "20261007", "20261008"):
        st.upsert_bars("600000.SH", [{"time": day + "093000", "close": 1.0}],
                       period="1m")
    assert st.earliest_dt("600000.SH", "1m") == "20261006"
    assert st.latest_dt("600000.SH", "1m") == "20261008"
    assert st.earliest_dt("000001.SZ", "1m") == ""
    st.close()


def test_intraday_batch_cursor_maps(tmp_path):
    """批量游标（BarsSyncer 断点续传依赖它）签名需与 LocalStore 对齐。"""
    st = _store(tmp_path)
    st.upsert_bars("A", [{"time": "20261008093000", "close": 1}], period="1m")
    st.upsert_bars("B", [{"time": "20261007093000", "close": 1}], period="1m")
    m = st.latest_dt_map(["A", "B", "ZZZ"], "1m")
    assert m["A"] == "20261008" and m["B"] == "20261007"
    assert "ZZZ" not in m
    e = st.earliest_dt_map(["A", "B"], "1m")
    assert e["A"] == "20261008" and e["B"] == "20261007"
    st.close()


def test_intraday_prune(tmp_path):
    st = _store(tmp_path)
    for day in ("20261006", "20261008"):
        st.upsert_bars("600000.SH", [{"time": day + "093000", "close": 1.0}],
                       period="1m")
    assert st.count() == 2
    assert st.prune_before("20261008") == 1   # 只删 1006
    assert st.count() == 1
    st.close()


def test_intraday_prune_rejects_bad_cutoff(tmp_path):
    """非法截止日必须拒绝 —— ``WHERE dt < ''`` 会命中所有行，等于误删全表。"""
    st = _store(tmp_path)
    st.upsert_bars("600000.SH", [{"time": "20261008093000", "close": 1.0}],
                   period="1m")
    assert st.prune_before("") == 0
    assert st.prune_before("not-a-date") == 0
    assert st.count() == 1
    st.close()


def test_intraday_stats(tmp_path):
    st = _store(tmp_path)
    st.upsert_bars("600000.SH", [{"time": "20261008093000", "close": 1.0}],
                   period="1m")
    st.upsert_bars("600000.SH", [{"time": "20261008093000", "close": 1.0}],
                   period="5m")
    s = st.stats()
    assert s["total_rows"] == 2
    assert set(s["periods"]) == {"1m", "5m"}
    assert s["periods"]["1m"]["codes"] == 1
    st.close()


def test_get_intraday_store_explicit_path(tmp_path):
    from datasource.intraday_store import get_intraday_store
    a = get_intraday_store(tmp_path / "x.db")
    b = get_intraday_store(tmp_path / "x.db")
    # 显式路径每次新建（测试隔离）；不复用进程单例
    assert isinstance(a, IntradayStore) and isinstance(b, IntradayStore)
    a.close()
    b.close()
