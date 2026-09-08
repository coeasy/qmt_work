"""G7 条件选股测试（条件求值 + 本地仓扫描 + 动态板块）。

密闭：种子 LocalStore（内存临时库）+ 确定性 K 线模式（UP 涨/DOWN 跌/FLAT 平），
零网络。注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import pytest

from app.screener.conditions import evaluate
from app.screener.engine import list_saved_boards, save_as_board, scan


@pytest.fixture()
def store(tmp_path):
    from app.datasource.local_store import LocalStore
    from core.db import DB
    db = DB(tmp_path / "test_screen.db")
    st = LocalStore(db)
    # 三只确定性标的：UP 单边上涨 / DOWN 单边下跌 / FLAT 横盘（time 递增唯一，
    # 否则本地仓主键 (code,period,adjust,dt) 会互相覆盖只剩 1 根）
    def _rising(n=30, start=100.0, step=1.7):
        return [{"time": f"2026{i:03d}", "open": start + i * step,
                 "high": start + i * step + 1, "low": start + i * step - 1,
                 "close": start + i * step, "volume": 1_000_000 + i * 1000}
                for i in range(n)]

    def _falling(n=30, start=150.0, step=1.7):
        bars = []
        for i in range(n):
            c = start - i * step
            bars.append({"time": f"2026{i:03d}", "open": c, "high": c + 1,
                         "low": c - 1, "close": c, "volume": 900_000 - i * 500})
        return bars

    def _flat(n=30, level=100.0):
        return [{"time": f"2026{i:03d}", "open": level, "high": level + 0.5,
                 "low": level - 0.5, "close": level, "volume": 500_000}
                for i in range(n)]

    st.upsert_stock_list([
        {"code": "UP.T", "name": "上涨股", "category": "主板"},
        {"code": "DOWN.T", "name": "下跌股", "category": "主板"},
        {"code": "FLAT.T", "name": "横盘股", "category": "主板"},
    ])
    st.upsert_bars("UP.T", _rising(), adjust="qfq")
    st.upsert_bars("DOWN.T", _falling(), adjust="qfq")
    st.upsert_bars("FLAT.T", _flat(), adjust="qfq")
    yield st
    db._conn.close()


def _bars(store, code):
    return store.get_bars(code, adjust="qfq")


# ============================ 条件求值 ======================================
def test_indicator_roc_gt(store):
    cond = {"indicator": {"name": "roc", "params": {"win": 12},
                          "output": "roc", "op": "gt", "value": 0}}
    assert evaluate(cond, _bars(store, "UP.T"))[0] is True
    assert evaluate(cond, _bars(store, "DOWN.T"))[0] is False
    assert evaluate(cond, _bars(store, "FLAT.T"))[0] is False   # roc≈0 非 gt


def test_field_close_gt(store):
    cond = {"field": {"name": "close", "op": "gt", "value": 120}}
    assert evaluate(cond, _bars(store, "UP.T"))[0] is True      # 最新 148.7
    assert evaluate(cond, _bars(store, "DOWN.T"))[0] is False   # 最新 100.7


def test_nested_and_or(store):
    cond = {"and": [
        {"indicator": {"name": "roc", "params": {"win": 12}, "output": "roc",
                       "op": "gt", "value": 0}},
        {"or": [
            {"field": {"name": "close", "op": "gt", "value": 120}},
            {"field": {"name": "close", "op": "lt", "value": 90}},
        ]},
    ]}
    assert evaluate(cond, _bars(store, "UP.T")) == (True, 2, 3)
    assert evaluate(cond, _bars(store, "DOWN.T"))[0] is False
    assert evaluate(cond, _bars(store, "FLAT.T"))[0] is False


def test_window_null_no_match(store):
    """窗口处为 null（数据不足）→ 不命中，绝不估算。"""
    # window=0 → roc[0] 为 null → 不命中
    cond0 = {"indicator": {"name": "roc", "params": {"win": 12}, "output": "roc",
                           "op": "gt", "value": -99999, "window": 0}}
    assert evaluate(cond0, _bars(store, "UP.T"))[0] is False


def test_bad_conditions_raise():
    with pytest.raises(ValueError):
        evaluate({}, [])
    with pytest.raises(ValueError):
        evaluate({"and": [{"indicator": {"name": "roc", "op": "xx", "value": 0}}]}, [])
    with pytest.raises(KeyError):
        evaluate({"indicator": {"name": "no_such", "op": "gt", "value": 0}}, [])


# ============================ 全市场扫描 ====================================
def test_scan_filters_and_sorts(store):
    cond = {"indicator": {"name": "roc", "params": {"win": 12}, "output": "roc",
                          "op": "gt", "value": 0}}
    out = scan(store, cond, sort_by="change_pct", sort_desc=True)
    assert out["count"] == 1
    assert out["results"][0]["code"] == "UP.T"
    assert out["results"][0]["score"] == 1
    assert out["total_scanned"] == 3
    assert out["elapsed_ms"] >= 0
    assert out["conditions"] == cond


def test_scan_price_filter(store):
    cond = {"field": {"name": "close", "op": "gt", "value": 100}}
    out = scan(store, cond, min_price=140)
    assert out["count"] == 1 and out["results"][0]["code"] == "UP.T"


def test_scan_limit(store):
    cond = {"field": {"name": "close", "op": "gt", "value": 90}}
    out = scan(store, cond, limit=1)
    assert out["count"] == 1


def test_scan_empty_warehouse(tmp_path):
    from app.datasource.local_store import LocalStore
    from core.db import DB
    db = DB(tmp_path / "empty.db")
    st = LocalStore(db)
    with pytest.raises(RuntimeError):
        scan(st, {"field": {"name": "close", "op": "gt", "value": 0}})
    db._conn.close()


def test_scan_bad_sort_key(store):
    with pytest.raises(ValueError):
        scan(store, {"field": {"name": "close", "op": "gt", "value": 0}},
             sort_by="nope")


# ============================ 动态板块 ======================================
def test_save_and_list_board(store):
    out = scan(store, {"field": {"name": "close", "op": "gt", "value": 100}})
    saved = save_as_board(store, "强势股", out["conditions"], out["results"])
    assert saved["kind"] == "screen:强势股"
    assert saved["count"] == 2
    boards = list_saved_boards(store)
    assert len(boards) == 1
    assert boards[0]["name"] == "强势股"
    assert boards[0]["count"] == 2


def test_save_board_empty_name(store):
    with pytest.raises(ValueError):
        save_as_board(store, "  ", {}, [])
