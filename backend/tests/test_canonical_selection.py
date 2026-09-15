"""Canonical 选主规则的**单一性**护栏（2026-09-15）。

背景：仓库里曾有三份「同一 (code, dt) 多源行选哪一条」的实现，规则各不相同 ——

============  ==========================================================
位置           排序键
============  ==========================================================
``get_bars``  质量状态 → ``(provider_id='') DESC`` → ``provider_id``
``get_bars_batch``  同上（``_canonical_key`` 复刻 SQL）
``reconcile_bars``   ``PROVIDER_QUALITY_RANK``（空 provider = 999 最差）
============  ==========================================================

后果：一旦真有第二个数据源写入同一 (code, dt)，会出现
**「消费方读到的行」≠「对账侧标为 canonical 的行」**。
实测分歧：``akshare`` vs ``broker`` → 读侧选 ``akshare``、对账侧选 ``broker``；
``''`` vs ``broker`` → 读侧选 ``''``、对账侧选 ``broker``。

现统一为 :func:`datasource.quality.canonical_sort_key` 一份实现，读侧与对账侧共用。
本文件钉住：① 规则本身；② 两侧结果一致；③ SQL 的 ``CASE`` 由排名表程序化生成
（不会与 Python 侧漂移）。
"""
import pytest

from datasource.local_store import (
    _CANONICAL_ORDER_SQL,
    _PROVIDER_RANK_CASE_SQL,
    _QUALITY_STATE_CASE_SQL,
    LocalStore,
)
from datasource.models import Bar
from datasource.quality import (
    PROVIDER_QUALITY_RANK,
    QUALITY_STATE_RANK,
    canonical_sort_key,
    provider_rank,
    reconcile_bars,
)


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    db = DB(tmp_path / "test_canonical.db")
    yield LocalStore(db)
    db._conn.close()


def _bar(t, close):
    return Bar(time=t, open=close, high=close + 1, low=close - 1,
               close=close, volume=100, amount=close * 100)


# ---------------------------------------------------------------------------
# ① 规则本身
# ---------------------------------------------------------------------------

def test_provider_rank_empty_is_worst():
    """空 provider（未标注来源）必须是**最差**档，不得压过任何具名源。"""
    assert provider_rank("") == 999
    assert provider_rank(None) == 999
    assert provider_rank("未登记源") == 999
    # 具名源严格优于空
    for p in PROVIDER_QUALITY_RANK:
        assert provider_rank(p) < provider_rank(""), p


def test_provider_rank_matches_documented_chain():
    """质量序与模块文档写明的数据链 QMT→eltdx→baostock→akshare 一致。"""
    assert provider_rank("broker") < provider_rank("eltdx")
    assert provider_rank("eltdx") < provider_rank("baostock")
    assert provider_rank("baostock") < provider_rank("akshare")
    assert provider_rank("qmt") == provider_rank("broker")   # 同一档
    assert provider_rank("BROKER") == provider_rank("broker")  # 大小写不敏感


def test_canonical_sort_key_priority_order():
    """三级键：质量状态 > provider 质量序 > provider 名。"""
    # 质量状态优先（即使 provider 更差）
    assert canonical_sort_key("validated", "akshare") < canonical_sort_key("unknown", "broker")
    # 同质量 → provider 质量序（broker 胜 akshare，尽管 akshare 名字典序在前）
    assert canonical_sort_key("unknown", "broker") < canonical_sort_key("unknown", "akshare")
    # 同档位 → provider 名升序（确定性 tie-break）
    assert canonical_sort_key("unknown", "broker") < canonical_sort_key("unknown", "qmt")
    # 空 provider 最差
    assert canonical_sort_key("unknown", "broker") < canonical_sort_key("unknown", "")
    # NULL / 未知质量状态落最后一档
    assert canonical_sort_key(None, "broker") == canonical_sort_key("__x__", "broker")
    assert canonical_sort_key(None, "broker")[0] == 3


# ---------------------------------------------------------------------------
# ② 读侧与对账侧必须选出**同一行**
# ---------------------------------------------------------------------------

def _seed_multi_source(store, code):
    """三个 dt，每个都构造「读侧旧规则会选错」的多源数据。

    组内价差刻意 < 0.5%（``reconcile_bars`` 的 ``price_diff_pct`` 阈值）——
    否则整组被判 ``conflict``、不产出 canonical，就测不到「两侧选同一行」。
    """
    # dt1：空 provider vs 具名 → 具名 broker 必须胜（旧读侧选 ''）
    store.upsert_bars(code, [_bar("20260901", 10.00)], adjust="qfq",
                      provider_id="", quality_state="unknown")
    store.upsert_bars(code, [_bar("20260901", 10.02)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")
    # dt2：名字典序在前但质量档更差 → broker 必须胜（旧读侧选 akshare）
    store.upsert_bars(code, [_bar("20260902", 20.00)], adjust="qfq",
                      provider_id="akshare", quality_state="unknown")
    store.upsert_bars(code, [_bar("20260902", 20.02)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")
    # dt3：质量状态优先于 provider 质量序
    store.upsert_bars(code, [_bar("20260903", 30.00)], adjust="qfq",
                      provider_id="akshare", quality_state="validated")
    store.upsert_bars(code, [_bar("20260903", 30.02)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")


def _canonical_row(db, code, dt):
    r = db.query_one("SELECT provider_id, close FROM local_bars "
                     "WHERE code=? AND dt=? AND quality_state='final'", (code, dt))
    return (r["provider_id"], r["close"]) if r else None


def test_read_side_never_prefers_empty_provider(store):
    """回归：空 provider 不得胜具名源（旧规则 `(provider_id='') DESC` 会胜）。"""
    store.upsert_bars("600001.SH", [_bar("20260901", 10.0)], adjust="qfq",
                      provider_id="", quality_state="unknown")
    store.upsert_bars("600001.SH", [_bar("20260901", 11.0)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")
    per = store.get_bars("600001.SH", period="1d", adjust="qfq")
    batch = store.get_bars_batch(["600001.SH"], period="1d", adjust="qfq")
    assert [b.close for b in per] == [11.0]
    assert [b.close for b in batch["600001.SH"]] == [11.0]


def test_read_side_uses_provider_quality_order_not_name_order(store):
    """回归：同质量下按 provider **质量序**，而非名字典序。

    ``akshare`` 名字典序在 ``broker`` 之前，旧规则会选它；新规则必须选 ``broker``。
    """
    store.upsert_bars("600002.SH", [_bar("20260901", 20.0)], adjust="qfq",
                      provider_id="akshare", quality_state="unknown")
    store.upsert_bars("600002.SH", [_bar("20260901", 21.0)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")
    per = store.get_bars("600002.SH", period="1d", adjust="qfq")
    assert [b.close for b in per] == [21.0]


def test_read_side_matches_reconcile_on_multi_source(store):
    """**核心护栏**：``get_bars`` 读到的行 == ``reconcile_bars`` 标 canonical 的行。

    这条是本次统一的目的所在。回退任一侧的排序键（或让空 provider 优先）即红。
    """
    code = "600003.SH"
    _seed_multi_source(store, code)

    # 读侧（逐只 + 批量，两者必须一致）
    per = {b.time: b.close for b in store.get_bars(code, period="1d", adjust="qfq")}
    batch = {b.time: b.close
             for b in store.get_bars_batch([code], period="1d", adjust="qfq")[code]}
    assert per == batch, f"逐只与批量取数结果不一致：{per} vs {batch}"

    # 对账侧
    stats = reconcile_bars(store._db, period="1d", adjust="qfq", lookback_days=100000)
    assert stats["canonical_set"] == 3, stats

    for dt, close in per.items():
        canon = _canonical_row(store._db, code, dt)
        assert canon is not None, f"{dt} 没有 canonical 行"
        assert canon[1] == close, (
            f"{dt}：读侧读到 close={close}，对账侧 canonical close={canon[1]}"
            f"（provider={canon[0]}）—— 两侧选主规则不一致")


def test_batch_selection_equals_per_code_selection(store):
    """① get_bars 与 ② get_bars_batch 必须逐行一致（SQL 与 Python 两套实现等价）。"""
    code = "600004.SH"
    _seed_multi_source(store, code)
    per = [(b.time, b.close) for b in store.get_bars(code, period="1d", adjust="qfq")]
    bat = [(b.time, b.close)
           for b in store.get_bars_batch([code], period="1d", adjust="qfq")[code]]
    assert per == bat


# ---------------------------------------------------------------------------
# ③ SQL 侧不得与 Python 侧漂移
# ---------------------------------------------------------------------------

def test_sql_case_generated_from_rank_tables():
    """SQL 的 ``CASE`` 必须由排名表**程序化生成** —— 新增 provider 时不会漏改 SQL。

    若有人把 ``CASE`` 手写成字面量副本，这里会红。
    """
    for p, r in PROVIDER_QUALITY_RANK.items():
        assert f"WHEN '{p}' THEN {r}" in _PROVIDER_RANK_CASE_SQL, p
    for q, r in QUALITY_STATE_RANK.items():
        assert f"WHEN '{q}' THEN {r}" in _QUALITY_STATE_CASE_SQL, q
    assert "ELSE 999 END" in _PROVIDER_RANK_CASE_SQL
    assert "ELSE 3 END" in _QUALITY_STATE_CASE_SQL
    # 完整 ORDER BY 片段 = 质量状态 CASE + provider 质量序 CASE + provider 名
    assert _CANONICAL_ORDER_SQL == (
        f"{_QUALITY_STATE_CASE_SQL}, {_PROVIDER_RANK_CASE_SQL}, provider_id")


def test_sql_order_matches_python_key_on_multi_source(store):
    """SQL 的 ``ORDER BY`` 与 Python 的 ``canonical_sort_key`` 选出同一行。

    直接对同一批行跑两种排序，比对结果 —— 覆盖「新增一个 provider 但只改了一半」。
    """
    code = "600005.SH"
    for pid, close in (("", 10.0), ("akshare", 20.0), ("broker", 30.0),
                       ("eltdx", 40.0), ("baostock", 50.0)):
        store.upsert_bars(code, [_bar("20260901", close)], adjust="qfq",
                          provider_id=pid, quality_state="unknown")

    sql_pick = store._db.query_one(
        "SELECT provider_id, close FROM local_bars WHERE code=? AND period='1d' "
        f"AND adjust='qfq' ORDER BY {_CANONICAL_ORDER_SQL} LIMIT 1", (code,))
    rows = store._db.query(
        "SELECT provider_id, quality_state, close FROM local_bars WHERE code=? "
        "AND period='1d' AND adjust='qfq'", (code,))
    py_pick = min(rows, key=lambda r: canonical_sort_key(r["quality_state"],
                                                         r["provider_id"]))
    assert sql_pick["provider_id"] == py_pick["provider_id"], (
        f"SQL 选 {sql_pick['provider_id']}，Python 选 {py_pick['provider_id']}")
    assert sql_pick["close"] == 30.0  # broker 质量序最优
