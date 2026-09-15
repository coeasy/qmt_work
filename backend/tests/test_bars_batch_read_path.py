"""R4 吞吐优化 3：批量取数的**连接与消费方式**护栏（2026-09-15）。

背景：``LocalStore.get_bars_batch`` 原先用 ``DB.execute()`` 取数 —— 那是
「**主连接** + **写锁**」，而且 ``fetchall()`` 还在锁外跑在主连接上
（``core/db.py`` 自己写明「主连接上的读必须与写互斥」）。实测代价：

  - 空载单次批量读 201ms → 并发写负载下 **3925ms（19.5×）**，20s 只完成 3 轮；
  - 反向把写者单次写入的 p99 从 0.15ms 推到 **73.6ms**。

现改为 ``DB.readonly_conn()`` 的**独占只读连接** + 游标**流式**消费。

本文件钉住四件事（全部为结构性断言，不依赖耗时，不会 flaky）：
1. 批量读**不再走** ``DB.execute``（即不再占写锁 / 不再用主连接）；
2. 消费行的整个过程中**不持有任何 DB 锁**（``_rw`` 读者与写者计数均为 0）；
3. **不物化**整批行（流式游标上不得出现 ``fetchall``）；
4. 连接在分块循环**外**只开一次（多块共用一条连接）。
"""
import contextlib
import sqlite3
import threading
import time

import pytest

import datasource.local_store as local_store
from core.db import DB, _rw
from datasource.local_store import LocalStore
from datasource.models import Bar


@pytest.fixture()
def store(tmp_path):
    db = DB(tmp_path / "test_read_path.db")
    yield LocalStore(db)
    db._conn.close()


def _bars(n=5, start=20260101):
    return [Bar(time=str(start + i), open=10 + i, high=11 + i, low=9 + i,
                close=10.5 + i, volume=1000 * (i + 1), amount=10500 * (i + 1))
            for i in range(n)]


def _seed(store, codes, n=5):
    for c in codes:
        store.upsert_bars(c, _bars(n), adjust="qfq")


# ---------------------------------------------------------------------------
# 探针：包装 readonly_conn，记录连接次数 / SQL / 是否 fetchall
# ---------------------------------------------------------------------------
class _CursorProxy:
    def __init__(self, cur, rec):
        self._cur = cur
        self._rec = rec

    def __iter__(self):
        return iter(self._cur)

    def fetchall(self):
        self._rec["fetchall"] += 1
        return self._cur.fetchall()


class _ConnProxy:
    def __init__(self, conn, rec):
        self._conn = conn
        self._rec = rec

    def execute(self, sql, params=()):
        self._rec["sql"].append((sql, tuple(params)))
        return _CursorProxy(self._conn.execute(sql, params), self._rec)


def _rec() -> dict:
    return {"conns": 0, "sql": [], "fetchall": 0}


def _patch_readonly(monkeypatch, store, rec):
    orig = store._db.readonly_conn

    @contextlib.contextmanager
    def patched():
        rec["conns"] += 1
        with orig() as conn:
            yield _ConnProxy(conn, rec)

    monkeypatch.setattr(store._db, "readonly_conn", patched)


# ---------------------------------------------------------------------------
# 1. 不再走 DB.execute（写锁 + 主连接）
# ---------------------------------------------------------------------------
def test_batch_read_does_not_go_through_db_execute(store, monkeypatch):
    """批量读不得再调用 ``DB.execute`` —— 那是写锁 + 主连接，正是本次要消掉的路径。"""
    codes = [f"60000{i}.SH" for i in range(20)]
    _seed(store, codes, n=4)

    calls = {"n": 0}
    orig = store._db.execute

    def spy(sql, params=()):
        calls["n"] += 1
        return orig(sql, params)

    monkeypatch.setattr(store._db, "execute", spy)
    out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)

    assert calls["n"] == 0, (
        f"批量读仍走了 DB.execute {calls['n']} 次 —— 会占写锁并与写者争抢主连接")
    assert len(out) == 20


def test_batch_read_holds_no_db_lock_while_consuming(store, monkeypatch):
    """消费行的过程中不得持有任何 DB 锁（读者与写者计数都为 0）。

    注意本条**不判别旧实现**（旧实现的 ``fetchall`` 也在锁外，消费阶段同样无锁），
    它防的是另一类回归：有人把取数包进 ``_rw.read()`` / ``_rw.write()``。因为
    ``write()`` 会等待 ``_readers == 0``，一旦读持有读锁，全市场取数的数秒内
    写者会被整段堵死。「旧路径（``DB.execute``）」由上面第一条用例判别。
    """
    codes = [f"60000{i}.SH" for i in range(20)]
    _seed(store, codes, n=4)

    observed: list[tuple[bool, int]] = []
    orig = local_store._row_to_bar

    def spy(row, lite=False):
        observed.append((_rw._writer, _rw._readers))
        return orig(row, lite)

    monkeypatch.setattr(local_store, "_row_to_bar", spy)
    store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)

    assert observed, "探针没被调用 —— 用例没覆盖到目标路径"
    bad = [s for s in observed if s != (False, 0)]
    assert not bad, (
        f"{len(bad)}/{len(observed)} 次消费持有了 DB 锁（写者/读者计数非零）：{bad[:3]}"
        " —— 读又回到与写互斥的老路了")


# ---------------------------------------------------------------------------
# 2. 流式消费：不物化整批行
# ---------------------------------------------------------------------------
def test_batch_read_does_not_materialize_rows(store, monkeypatch):
    """批量读必须**走流式只读连接**且**不物化**整批行。

    两个断言合起来才构成对旧实现的判别：只断言「没有 fetchall」会被旧路径蒙混
    过去（旧路径压根不调用 ``readonly_conn``，探针记录到 0 次 fetchall），故必须
    同时断言连接确实被使用了。
    """
    codes = [f"60000{i}.SH" for i in range(30)]
    _seed(store, codes, n=4)

    rec = _rec()
    _patch_readonly(monkeypatch, store, rec)
    out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)

    assert rec["conns"] == 1, (
        f"未走 readonly_conn（{rec['conns']} 次）—— 取数又回到了主连接路径")
    assert rec["fetchall"] == 0, f"批量读调用了 fetchall {rec['fetchall']} 次"
    assert len(out) == 30
    for c in codes:
        assert len(out[c]) == 4, c


def test_batch_read_opens_readonly_conn_once_for_multiple_chunks(store, monkeypatch):
    """超过 900 只会分块 → 应发多条 SQL，但连接**只开一次**（在循环外）。"""
    codes = [f"{i:06d}.SH" for i in range(1000)]   # > 900 → 2 块
    _seed(store, codes, n=2)

    rec = _rec()
    _patch_readonly(monkeypatch, store, rec)
    out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)

    assert len(rec["sql"]) == 2, f"期望 2 条分块 SQL，实际 {len(rec['sql'])}"
    assert rec["conns"] == 1, (
        f"开了 {rec['conns']} 条连接 —— 应在分块循环外只开一次，否则每块重连")
    assert len(out) == 1000


# ---------------------------------------------------------------------------
# 3. readonly_conn 的契约与降级
# ---------------------------------------------------------------------------
def test_readonly_conn_is_actually_readonly(tmp_path):
    """只读连接必须真的只读（mode=ro）—— 否则「读」会污染主库。"""
    db = DB(tmp_path / "ro.db")
    try:
        with db.readonly_conn() as conn:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO local_bars (code,period,adjust,dt,open,high,low,"
                    "close) VALUES ('x.SH','1d','','20260101',1,1,1,1)")
    finally:
        db._conn.close()


def test_readonly_conn_memory_db_falls_back_to_main_conn():
    """内存库无法再开连接（新连接看到空库）→ 降级为主连接 + 写锁。"""
    db = DB(__import__("pathlib").Path(":memory:"))
    try:
        with db.readonly_conn() as conn:
            assert conn is db._conn, "内存库应降级为主连接"
            assert _rw._writer is True, "降级路径必须持写锁（与写互斥，语义安全）"
        assert _rw._writer is False, "退出 with 后写锁必须释放"
    finally:
        db._conn.close()


def test_readonly_conn_falls_back_when_ro_open_fails(tmp_path, monkeypatch):
    """只读连接开不出来（只读介质 / URI 不受支持等）→ 降级主连接 + 写锁，不抛错。"""
    import core.db as coredb

    db = DB(tmp_path / "fallback.db")
    real_connect = coredb.sqlite3.connect

    def boom(*a, **kw):
        if kw.get("uri") or (a and str(a[0]).startswith("file:")):
            raise sqlite3.OperationalError("simulated: readonly media")
        return real_connect(*a, **kw)

    try:
        monkeypatch.setattr(coredb.sqlite3, "connect", boom)
        with db.readonly_conn() as conn:
            assert conn is db._conn, "开只读连接失败时必须回退主连接"
            assert _rw._writer is True, "回退路径必须持写锁"
        assert _rw._writer is False
    finally:
        db._conn.close()


# ---------------------------------------------------------------------------
# 4. 流式读进行中提交写：不得报错、不得残缺
# ---------------------------------------------------------------------------
def test_committed_write_during_stream_does_not_break_read(store, monkeypatch):
    """流式读进行中在**另一条连接**上提交一次写，读不得报错、已读行不得残缺。

    这正是旧实现（主连接 + 锁外 fetchall）会踩的场景；新实现读走独立只读连接，
    在 WAL 下与写天然并发。
    """
    codes = [f"60000{i}.SH" for i in range(10)]
    _seed(store, codes, n=5)

    fired = {"n": 0}
    orig = local_store._row_to_bar

    def spy(row, lite=False):
        if fired["n"] == 0:
            fired["n"] = 1
            store.upsert_bars(
                codes[0],
                [Bar(time="20261231", open=1.0, high=2.0, low=0.5, close=1.5,
                     volume=1.0, amount=1.5)],
                adjust="qfq", provider_id="mid-stream")
        return orig(row, lite)

    monkeypatch.setattr(local_store, "_row_to_bar", spy)
    out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)

    assert fired["n"] == 1, "探针没被调用 —— 用例没覆盖到目标路径"
    assert len(out) == 10
    seeded = [str(20260101 + k) for k in range(5)]
    for c in codes:
        assert len(out[c]) >= 5, c
        # 已落库的 5 根必须完整且顺序正确（不因并发写而残缺/错序）
        assert [b.time for b in out[c]][:5] == seeded, c


def test_batch_read_under_concurrent_writer_raises_nothing(store):
    """并发写者持续落库时，批量读必须稳定完成（回归旧实现的连接争抢）。

    只断言「无异常 + 结果形状正确」，不断言耗时（避免 flaky）。
    """
    codes = [f"60000{i}.SH" for i in range(200)]
    _seed(store, codes, n=6)

    stop = threading.Event()
    errors: list[str] = []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                store.upsert_bars(
                    codes[i % len(codes)],
                    [Bar(time="20260701", open=1.0, high=2.0, low=0.5, close=1.5,
                         volume=1.0, amount=1.5)],
                    adjust="qfq", provider_id="racer")
                i += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"write: {type(exc).__name__}: {exc}")
                return

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(5):
            out = store.get_bars_batch(codes, period="1d", adjust="qfq", limit=250)
            assert len(out) == 200
            assert all(len(out[c]) >= 6 for c in codes)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"并发写负载下批量读抛异常：{type(exc).__name__}: {exc}")
    finally:
        stop.set()
        t.join(timeout=10)

    assert not errors, f"并发写者出错：{errors[:3]}"
