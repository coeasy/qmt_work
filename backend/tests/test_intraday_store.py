"""分钟线独立仓（IntradayStore）契约测试。

这个仓是全市场分钟线的唯一落库地，也是 :mod:`app.sync.datasets` 把 K 线类
数据集路由到「独立文件」的落点。它必须与 :class:`LocalStore` 在游标语义上
完全同构（``earliest_dt_map`` / ``latest_dt_map`` / ``upsert_bars`` 签名），
否则 BarsSyncer 的断点续传会在两个仓之间漂移。

重点钉死三类历史事故：

1. **时间格式漂移** —— ``split_dt_tm`` 必须吞掉源端各种形状，尤其 14 位紧凑
   时间戳 ``"20261008093000"``（TDX / broker 最常见）。漏掉任何一种形状，
   结果都是「整批静默丢弃 + 报同步成功」。
2. **落库签名漂移** —— ``upsert_bars`` 必须接收 BarsSyncer 的完整关键字参数，
   少接一个就是所有分钟线数据集 100% 落库失败。
3. **保留清理误删** —— ``prune_before("")`` 必须返回 0，不能变成全表清空。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from datasource.intraday_store import IntradayStore, split_dt_tm


def _mk(path: Path) -> IntradayStore:
    return IntradayStore(path / "bars_intraday.db")


def _bar(code: str = "600000.SH", tm: str = "0930", close: float = 10.0) -> dict:
    return {"time": f"20261008{tm}", "open": close - 0.1, "high": close + 0.2,
            "low": close - 0.2, "close": close, "volume": 1000, "amount": 10000.0}


# ---------------------------------------------------------------------------
# split_dt_tm：时间形状是唯一入口，漏一种就丢一批
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,dt,tm", [
    ("20261008093000", "20261008", "0930"),   # 14 位紧凑（TDX 最常见）
    ("202610080930",   "20261008", "0930"),   # 12 位紧凑
    ("20261008",       "20261008", ""),       # 8 位纯日期
    ("2026-10-08 09:30", "20261008", "0930"), # ISO
    ("2026/10/08 09:30", "20261008", "0930"), # 斜杠
    (20261008093000,   "20261008", "0930"),   # 数值型（部分源返回 int）
    (20261008,         "20261008", ""),       # 数值型日期
])
def test_split_dt_tm_accepts_real_world_shapes(raw, dt, tm):
    got_dt, got_tm = split_dt_tm(raw)
    assert got_dt == dt, f"{raw!r} → dt"
    assert got_tm == tm, f"{raw!r} → tm"


@pytest.mark.parametrize("raw", ["", None, "not-a-date", "20269999999999", 0])
def test_split_dt_tm_rejects_garbage_without_inventing(raw):
    """非法输入必须返回空串，不能补零或猜日期——猜出来的日期是脏数据的源头。"""
    assert split_dt_tm(raw) == ("", "")


def test_split_dt_tm_drops_unparseable_rows(tmp_path):
    """落库时无法解析的时间行被丢弃，但**不会**连累同批合法行。"""
    st = _mk(tmp_path)
    n = st.upsert_bars("600000.SH", [_bar(tm="0930"), {"time": "bad"}, _bar(tm="0935")],
                       period="1m", provider_id="tdx")
    assert n == 2
    assert st.count("1m") == 2
    st.close()


# ---------------------------------------------------------------------------
# BarsSyncer 落库契约
# ---------------------------------------------------------------------------
def test_upsert_accepts_full_barssyncer_signature(tmp_path):
    """BarsSyncer 对主库与分钟仓用**同一个**调用；少接一个关键字就是全线失败。"""
    st = _mk(tmp_path)
    n = st.upsert_bars(
        "600000.SH", [_bar()], period="1m", adjust="qfq",
        provider_id="broker", batch_id="bars-test-1",
        schema_version="bars.v2", quality_state="raw")
    assert n == 1
    st.close()


def test_batch_and_provider_are_persisted(tmp_path):
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [_bar(tm="0930")], period="1m",
                   provider_id="broker", batch_id="b1")
    st.upsert_bars("600000.SH", [_bar(tm="0935")], period="1m",
                   provider_id="tdx", batch_id="b2")
    assert st.provider_counts(period="1m", batch_id="b1") == {"broker": 1}
    assert st.provider_counts(period="1m", batch_id="b2") == {"tdx": 1}
    # 不限批次 = 该周期全部历史（用于总览，不用于「本次同步」）
    assert st.provider_counts(period="1m") == {"broker": 1, "tdx": 1}
    st.close()


# ---------------------------------------------------------------------------
# 游标：增量跳过 / 全量回补
# ---------------------------------------------------------------------------
def test_dt_maps_are_the_resume_cursor(tmp_path):
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [_bar(tm="0930")], period="1m")
    st.upsert_bars("600000.SH", [_bar(tm="1500")], period="1m")
    st.upsert_bars("000001.SZ", [_bar(tm="1000")], period="5m")

    assert st.latest_dt("600000.SH", "1m") == "20261008"
    assert st.earliest_dt("600000.SH", "1m") == "20261008"
    assert st.latest_dt("600000.SH", "5m") == ""          # 周期隔离
    assert st.latest_dt("999999.SH", "1m") == ""           # 无数据 = 空串
    assert st.latest_dt_map(["600000.SH", "999999.SH"], "1m") == {"600000.SH": "20261008"}
    assert st.earliest_dt_map(["600000.SH"], "1m") == {"600000.SH": "20261008"}
    assert st.latest_dt_map([], "1m") == {}
    st.close()


def test_upsert_is_replace_not_duplicate(tmp_path):
    """同 (code,period,adjust,dt,tm) 重复写入 = 覆盖，不产生重复行。"""
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [_bar(close=10.0)], period="1m")
    st.upsert_bars("600000.SH", [_bar(close=11.0)], period="1m")
    assert st.count("1m") == 1
    assert st.get_bars("600000.SH", "1m")[0]["close"] == 11.0
    st.close()


# ---------------------------------------------------------------------------
# 读路径
# ---------------------------------------------------------------------------
def test_get_bars_orders_and_filters(tmp_path):
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [_bar(tm="1000"), _bar(tm="0930"), _bar(tm="1030")],
                   period="1m")
    rows = st.get_bars("600000.SH", "1m")
    assert [r["tm"] for r in rows] == ["0930", "1000", "1030"]
    assert st.get_bars("600000.SH", "1m", limit=2)[0]["tm"] == "0930"
    assert st.get_bars("600000.SH", "1m", end="20261007") == []
    st.close()


def test_get_bars_returns_provider_for_provenance(tmp_path):
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [_bar()], period="1m", provider_id="broker")
    row = st.get_bars("600000.SH", "1m")[0]
    assert row["provider_id"] == "broker"
    st.close()


# ---------------------------------------------------------------------------
# 保留清理：绝不能误删全表
# ---------------------------------------------------------------------------
def test_prune_removes_only_before_cutoff(tmp_path):
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [{"time": "20260901093000", "open": 1, "high": 1,
                                   "low": 1, "close": 1}], period="1m")
    st.upsert_bars("600000.SH", [_bar(tm="0930")], period="1m")   # 20261008
    assert st.prune_before("20261001") == 1
    assert st.count("1m") == 1
    st.close()


def test_prune_with_invalid_date_is_a_noop(tmp_path):
    """``WHERE dt < ''`` 会命中所有行；空串必须被显式拒绝。"""
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [_bar()], period="1m")
    assert st.prune_before("") == 0
    assert st.prune_before("garbage") == 0
    assert st.count("1m") == 1
    st.close()


def test_prune_period_scoped(tmp_path):
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [_bar()], period="1m")
    st.upsert_bars("600000.SH", [_bar()], period="5m")
    assert st.prune_before("20261009", period="1m") == 1
    assert st.count("1m") == 0
    assert st.count("5m") == 1
    st.close()


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def test_stats_reports_per_period_coverage(tmp_path):
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [_bar(tm="0930")], period="1m")
    st.upsert_bars("000001.SZ", [_bar(tm="0930")], period="5m")
    s = st.stats()
    assert s["total_rows"] == 2
    assert s["path"].endswith("bars_intraday.db")
    assert s["periods"]["1m"]["rows"] == 1
    assert s["periods"]["1m"]["codes"] == 1
    assert s["periods"]["5m"]["rows"] == 1
    st.close()


def test_stats_empty_store(tmp_path):
    st = _mk(tmp_path)
    s = st.stats()
    assert s["total_rows"] == 0
    assert s["periods"] == {}
    st.close()


def test_reopen_is_durable(tmp_path):
    """关掉再打开必须还能读到数据（独立文件的核心价值）。"""
    st = _mk(tmp_path)
    st.upsert_bars("600000.SH", [_bar()], period="1m", provider_id="tdx")
    st.close()
    st2 = _mk(tmp_path)
    assert st2.count("1m") == 1
    assert st2.latest_dt("600000.SH", "1m") == "20261008"
    assert st2.provider_counts(period="1m") == {"tdx": 1}
    st2.close()


def test_existing_file_missing_batch_id_column(tmp_path):
    """开发期已生成的旧分钟仓文件缺 ``batch_id`` 列，必须自动补齐而不报错。"""
    import sqlite3

    path = tmp_path / "bars_intraday.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE local_bars_intraday (
            code TEXT NOT NULL, period TEXT NOT NULL DEFAULT '1m',
            adjust TEXT NOT NULL DEFAULT '', dt TEXT NOT NULL,
            tm TEXT NOT NULL DEFAULT '', open REAL, high REAL, low REAL,
            close REAL, volume REAL, amount REAL,
            provider_id TEXT DEFAULT '', fetched_at TEXT DEFAULT '',
            PRIMARY KEY (code, period, adjust, dt, tm));
    """)
    conn.commit()
    conn.close()

    st = IntradayStore(path)
    st.upsert_bars("600000.SH", [_bar()], period="1m", provider_id="tdx",
                   batch_id="b1")
    assert st.provider_counts(period="1m", batch_id="b1") == {"tdx": 1}
    st.close()
