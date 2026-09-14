"""Phase B：选股 SQL pivot 批量取数验证（P3）。

验收点（对应 V10 方案 Phase B DoD）：
1. 逐只循环（N 次 SQL）被单条（分块）SQL 批量取数取代 —— monkeypatch ``db.execute``
   断言小批（< 900 只）仅触发 1 次 SQL 调用。
2. 批量结果与逐只 ``get_bars`` 完全一致（canonical 选主逻辑一致）。
3. 全市场规模（5000 只 × 5 根）本地向量化扫描性能达标（≤10s，沙箱 IO 宽松阈值；
   本地 SSD 实测通常 <3s）。
4. latest-N 语义与 ``get_bars(limit=N)`` 一致（每标的最近 N 根、时间升序返回）。
"""
import time

import pytest

from datasource.local_store import LocalStore
from datasource.models import Bar


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    db = DB(tmp_path / "test_pivot.db")
    yield LocalStore(db)
    db._conn.close()


def _bars(n=3, start=20260826):
    out = []
    for i in range(n):
        d = str(start + i)
        out.append(Bar(time=d, open=10 + i, high=11 + i, low=9 + i,
                       close=10.5 + i, volume=1000 * (i + 1), amount=10500 * (i + 1)))
    return out


def _seed(store, codes, n=5):
    for c in codes:
        store.upsert_bars(c, _bars(n), adjust="qfq")


def _seed_bulk(store, codes, n=5, start=20260826):
    """批量直插（仅性能测试用），避免 5000 次 upsert 的写入开销。"""
    rows = []
    for c in codes:
        for i in range(n):
            d = str(start + i)
            rows.append((c, "1d", "qfq", d, 10 + i, 11 + i, 9 + i, 10.5 + i,
                         1000 * (i + 1), 10500 * (i + 1),
                         "", "", "", "", "1", "unknown"))
    store._db.executemany(
        "INSERT OR REPLACE INTO local_bars "
        "(code,period,adjust,dt,open,high,low,close,volume,amount,fetched_at,"
        "provider_id,batch_id,checksum,schema_version,quality_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def test_get_bars_batch_single_query_count(store, monkeypatch):
    """小批（100 只 < 900 分块阈值）应仅触发 1 次 db.execute。"""
    codes = [f"{i:06d}.SH" for i in range(100)]
    _seed(store, codes, n=3)
    calls = {"n": 0}
    orig = store._db.execute

    def spy(sql, params=()):
        calls["n"] += 1
        return orig(sql, params)

    monkeypatch.setattr(store._db, "execute", spy)
    out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)
    assert calls["n"] == 1, f"期望 1 次 SQL，实际 {calls['n']} 次"
    assert len(out) == 100
    for c in codes:
        assert len(out[c]) == 3, c


def test_get_bars_batch_matches_per_code(store):
    """批量结果与逐只 get_bars 完全一致（canonical 选主一致）。"""
    codes = [f"{i:06d}.SZ" for i in range(50)]
    _seed(store, codes, n=4)
    batch = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)
    for c in codes:
        per = store.get_bars(c, period="1d", adjust="qfq", limit=250)
        assert [b.time for b in batch[c]] == [b.time for b in per], c
        assert [b.close for b in batch[c]] == [b.close for b in per], c


def test_get_bars_batch_latest_n(store):
    """latest-N 语义与 get_bars(limit=N) 一致。"""
    codes = [f"{i:06d}.SH" for i in range(20)]
    _seed(store, codes, n=10)
    batch = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=3)
    per = {c: store.get_bars(c, period="1d", adjust="qfq", limit=3) for c in codes}
    for c in codes:
        assert [b.time for b in batch[c]] == [b.time for b in per[c]], c
        assert len(batch[c]) == 3


def test_get_bars_batch_full_market_perf(store):
    """全市场规模（5000 只 × 5 根）本地批量取数性能达标。"""
    N = 5000
    codes = [f"{i:06d}.SH" for i in range(N)]
    _seed_bulk(store, codes, n=5)
    t1 = time.time()
    out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)
    elapsed = time.time() - t1
    assert len(out) == N
    assert all(len(out[c]) == 5 for c in codes[:100])
    # 本地向量化阈值：≤10s（本地 SSD 通常 <3s）；仅计时取数，不含种子写入
    assert elapsed <= 10.0, f"全市场批量取数 {elapsed:.2f}s 超阈值"


# ---------------------------------------------------------------------------
# 2026-09-14：批量取数改为「索引序取回 + Python 侧选主」后的两条回归护栏
# ---------------------------------------------------------------------------

def _seed_multi_source(store, code):
    """同一 (code, dt) 写多个 provider / 多档质量，覆盖选主三级排序键。"""
    def bar(t, close):
        return Bar(time=t, open=close, high=close + 1, low=close - 1,
                   close=close, volume=100, amount=close * 100)

    # dt1：质量优先 —— validated 的 src_z 应胜过 unknown 的 src_a
    store.upsert_bars(code, [bar("20260901", 12.0)], adjust="qfq",
                      provider_id="src_z", quality_state="validated")
    store.upsert_bars(code, [bar("20260901", 10.0)], adjust="qfq",
                      provider_id="src_a", quality_state="unknown")
    # dt2：同质量 → provider_id 升序 —— src_a 应胜出
    store.upsert_bars(code, [bar("20260902", 21.0)], adjust="qfq",
                      provider_id="src_b", quality_state="unknown")
    store.upsert_bars(code, [bar("20260902", 20.0)], adjust="qfq",
                      provider_id="src_a", quality_state="unknown")
    # dt3：空 provider 排前 —— 按**现状**钉住，见下方断言处的说明
    store.upsert_bars(code, [bar("20260903", 31.0)], adjust="qfq",
                      provider_id="src_a", quality_state="unknown")
    store.upsert_bars(code, [bar("20260903", 30.0)], adjust="qfq",
                      provider_id="", quality_state="unknown")


def test_get_bars_batch_multi_source_selection_matches_per_code(store):
    """多源同 dt：批量取数的 Python 侧选主必须与逐只 get_bars 的窗口选主逐行一致。

    这是「把选主从 SQL 窗口函数下沉到 Python」这一改动的**正确性**护栏：
    任一侧的排序键（质量 → 空 provider 优先 → provider 名）被改动而另一侧没跟上，
    这里就会红。
    """
    codes = [f"60000{i}.SH" for i in range(5)]
    for c in codes:
        _seed_multi_source(store, c)

    batch = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)
    for c in codes:
        per = store.get_bars(c, period="1d", adjust="qfq", limit=250)
        assert [b.time for b in batch[c]] == [b.time for b in per], c
        assert [b.close for b in batch[c]] == [b.close for b in per], c
        # 每 dt 只出一条（多源不膨胀）
        assert len(batch[c]) == 3, c

    # 三级排序键各自生效（数值即证据，避免只测「长度相等」）：
    #   dt1 12.0 = validated 胜 unknown；dt2 20.0 = 同质量下 provider 名升序 src_a 胜；
    #   dt3 30.0 = 空 provider 胜具名 —— ⚠️ 这是**已知的语义不一致**：
    #   SQL 的 `(provider_id = '') DESC` 让空 provider 排前，而两处 docstring 写的是
    #   「优先具名 provider」。当前库里 provider_id 从不为空（只有 auto/broker/tencent），
    #   故该 bug 一直是空操作。本用例按**现状**钉住，若将来决定修（改成具名优先），
    #   这里与 `local_store._canonical_key` 需同步改为 31.0。
    closes = [b.close for b in batch[codes[0]]]
    assert closes == [12.0, 20.0, 30.0], closes


def test_get_bars_batch_query_plan_has_no_temp_btree(store, monkeypatch):
    """批量取数的 SQL 必须是纯索引序取回 —— 执行计划里不得出现 TEMP B-TREE。

    2026-09-14 的吞吐优化就来自这里：原双层窗口函数在 117 万行真实库上
    触发 3 次 ``USE TEMP B-TREE FOR ORDER BY``（SQL 侧 ~9s）；改为
    ``ORDER BY code, dt`` 命中 ``idx_local_bars_lookup`` 后排序开销归零
    （全市场选股请求 API 实测中位 14.95s → 10.26s）。
    这条断言与耗时无关，不会因机器快慢而 flaky。
    """
    codes = [f"{i:06d}.SH" for i in range(50)]
    _seed(store, codes, n=3)

    captured = {}
    orig = store._db.execute

    def spy(sql, params=()):
        captured["sql"], captured["params"] = sql, params
        return orig(sql, params)

    monkeypatch.setattr(store._db, "execute", spy)
    store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)

    plan_rows = store._db.query("EXPLAIN QUERY PLAN " + captured["sql"],
                                tuple(captured["params"]))
    plan = " | ".join(r["detail"] for r in plan_rows)
    assert "TEMP B-TREE" not in plan.upper(), f"批量取数出现排序开销：{plan}"
    assert "idx_local_bars_lookup" in plan, f"未命中查询索引：{plan}"
