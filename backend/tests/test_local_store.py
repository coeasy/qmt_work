"""G1-4 本地数据仓测试（LocalStore + 迁移 v16）。

用临时 SQLite 文件实例化 DB（自动跑全部迁移），验证本地仓 CRUD / 幂等 /
复权维度隔离 / 同步游标。注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import pytest

from app.datasource.local_store import LocalStore
from app.datasource.models import Bar, BoardItem, StockInfo


@pytest.fixture()
def store(tmp_path):
    from app.db import DB
    db = DB(tmp_path / "test_local.db")
    yield LocalStore(db)
    db._conn.close()


def _bars(n=3, start="20260826"):
    out = []
    for i in range(n):
        d = str(int(start) + i)
        out.append(Bar(time=d, open=10 + i, high=11 + i, low=9 + i,
                       close=10.5 + i, volume=1000 * (i + 1), amount=10500 * (i + 1)))
    return out


# ---- K 线 ------------------------------------------------------------------
def test_upsert_get_bars(store):
    store.upsert_bars("600519.SH", _bars(3))
    got = store.get_bars("600519.SH")
    assert len(got) == 3
    assert [b.time for b in got] == ["20260826", "20260827", "20260828"]
    assert isinstance(got[0], Bar)
    assert got[0].close == 10.5


def test_upsert_bars_idempotent(store):
    store.upsert_bars("600519.SH", _bars(2))
    # 同 (code,period,adjust,dt) 再写一次 → REPLACE，不重复
    store.upsert_bars("600519.SH", _bars(2))
    assert store.count_bars("600519.SH") == 2


def test_bars_adjust_isolated(store):
    store.upsert_bars("600519.SH", _bars(1), adjust="qfq")
    store.upsert_bars("600519.SH", _bars(2), adjust="")
    assert store.count_bars("600519.SH", adjust="qfq") == 1
    assert store.count_bars("600519.SH", adjust="") == 2
    assert len(store.get_bars("600519.SH", adjust="qfq")) == 1


def test_get_bars_limit_and_range(store):
    store.upsert_bars("600519.SH", _bars(5))
    assert len(store.get_bars("600519.SH", limit=2)) == 2
    assert [b.time for b in store.get_bars("600519.SH", limit=2)] == ["20260826", "20260827"]
    got = store.get_bars("600519.SH", start="20260827", end="20260828")
    assert [b.time for b in got] == ["20260827", "20260828"]


def test_latest_dt(store):
    assert store.latest_dt("600519.SH") is None
    store.upsert_bars("600519.SH", _bars(3))
    assert store.latest_dt("600519.SH") == "20260828"


def test_upsert_bars_raw_dict(store):
    # 兼容原生 dict 输入（对齐 eltdx get_kline 形状）
    store.upsert_bars("000001.SZ", [{"time": "20260828", "open": 1, "high": 2,
                                     "low": 0.5, "close": 1.5, "volume": 100}])
    got = store.get_bars("000001.SZ")
    assert got[0].close == 1.5


# ---- 股票列表 / 板块榜 -------------------------------------------------------
def test_stock_list_roundtrip(store):
    items = [StockInfo(code="600519.SH", name="贵州茅台", category="主板"),
             {"code": "000001.SZ", "name": "平安银行", "category": "主板"}]
    assert store.upsert_stock_list(items) == 2
    lst = store.get_stock_list()
    assert len(lst) == 2
    assert lst[0]["code"] == "000001.SZ"   # ORDER BY code
    # 幂等：同 code 再写覆盖不重复
    store.upsert_stock_list([StockInfo(code="600519.SH", name="贵州茅台", category="主板")])
    assert len(store.get_stock_list()) == 2


def test_boards_roundtrip(store):
    items = [BoardItem(code="881001.TI", name="行业A", kind="industry",
                       last=100.0, change_pct=2.5, amount=1e9),
             BoardItem(code="880002.TI", name="概念B", kind="concept",
                       last=50.0, change_pct=-1.2, amount=5e8)]
    assert store.upsert_boards("industry", [items[0]]) == 1
    assert store.upsert_boards("concept", [items[1]]) == 1
    ind = store.get_boards("industry")
    assert len(ind) == 1 and ind[0]["code"] == "881001.TI"
    assert ind[0]["change_pct"] == 2.5


# ---- 同步元数据 / 运维 ------------------------------------------------------
def test_sync_meta_roundtrip(store):
    assert store.get_meta("last_sync_at") is None
    store.set_meta("last_sync_at", "2026-08-30T12:00:00")
    assert store.get_meta("last_sync_at") == "2026-08-30T12:00:00"


def test_stats(store):
    store.upsert_bars("600519.SH", _bars(3))
    store.upsert_stock_list([{"code": "000001.SZ", "name": "平安银行"}])
    s = store.stats()
    assert s["local_bars"] == 3
    assert s["local_stock_list"] == 1
    assert s["local_boards"] == 0
    assert s["last_sync"] is None


def test_clear(store):
    store.upsert_bars("600519.SH", _bars(3))
    store.clear()
    assert store.stats()["local_bars"] == 0


# ---- 迁移 v16 ---------------------------------------------------------------
def test_migration_v16_creates_tables(tmp_path):
    from app.db import DB
    db = DB(tmp_path / "mig.db")
    tables = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("local_bars", "local_stock_list", "local_boards", "local_sync_meta"):
        assert t in tables, f"迁移 v16 应创建 {t}"
    db._conn.close()
