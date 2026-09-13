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
