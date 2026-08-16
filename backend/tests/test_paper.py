"""模拟盘引擎与路由单元测试（临时 SQLite，不依赖券商 / 不启动整个 app）。

运行：cd backend && python -m pytest tests/test_paper.py -q
"""
import os
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB  # noqa: E402
from paper.paper_engine import PaperEngine  # noqa: E402


@pytest.fixture()
def engine():
    tmp = tempfile.mkdtemp(prefix="qmt_paper_")
    db = DB(Path(tmp) / "paper.db")
    e = PaperEngine().init(db)
    e.reset(1_000_000.0)
    yield e
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 引擎 ----------------
def test_reset_initial_state(engine):
    acc = engine.reset(500_000.0)
    assert acc["cash"] == 500_000.0
    assert acc["market_value"] == 0.0
    assert acc["total_assets"] == 500_000.0
    assert acc["positions"] == []
    assert engine.get_trades() == []


def test_buy_deducts_cash_and_creates_position(engine):
    o = engine.submit_order("600519.SH", "buy", 100.0, 1000)
    comm = max(100.0 * 1000 * 0.0003, 5.0)      # 30.0
    assert o["status"] == "filled" and o["order_id"].startswith("PAPER-")
    assert o["cash_after"] == pytest.approx(1_000_000.0 - 100_000.0 - comm, abs=0.01)
    pos = engine.get_positions()
    assert len(pos) == 1
    assert pos[0]["code"] == "600519.SH"
    assert pos[0]["volume"] == 1000
    assert pos[0]["avg_cost"] == pytest.approx(100.0)


def test_buy_twice_average_cost(engine):
    engine.submit_order("000001.SZ", "buy", 10.0, 1000)
    engine.submit_order("000001.SZ", "buy", 12.0, 1000)
    pos = engine.get_positions()[0]
    assert pos["volume"] == 2000
    assert pos["avg_cost"] == pytest.approx(11.0)


def test_sell_realizes_pnl_and_clears_position(engine):
    import paper.paper_engine as pe
    orig = pe._today_date
    pe._today_date = lambda: "2024-01-01"          # 买入日 D1
    engine.submit_order("600519.SH", "buy", 100.0, 1000)
    cash_after_buy = engine.cash
    pe._today_date = lambda: "2024-01-02"          # T+1 次日方可卖
    o = engine.submit_order("600519.SH", "sell", 110.0, 1000)
    pe._today_date = orig
    amount = 110.0 * 1000
    comm = max(amount * 0.0003, 5.0)            # 33.0
    stamp = amount * 0.001                      # 110.0
    assert o["cash_after"] == pytest.approx(cash_after_buy + amount - comm - stamp, abs=0.01)
    assert o["pnl"] == pytest.approx(10_000.0 - comm - stamp, abs=0.01)
    assert engine.get_positions() == []
    acc = engine.get_account()
    assert acc["realized_pnl"] == pytest.approx(o["pnl"], abs=0.01)
    assert acc["market_value"] == 0.0
    trades = engine.get_trades()
    assert len(trades) == 2 and trades[0]["side"] == "sell"


def test_partial_sell_keeps_position(engine):
    import paper.paper_engine as pe
    orig = pe._today_date
    pe._today_date = lambda: "2024-01-01"
    engine.submit_order("600519.SH", "buy", 100.0, 2000)
    pe._today_date = lambda: "2024-01-02"
    engine.submit_order("600519.SH", "sell", 105.0, 500)
    pe._today_date = orig
    pos = engine.get_positions()[0]
    assert pos["volume"] == 1500
    assert pos["avg_cost"] == pytest.approx(100.0)


def test_market_value_uses_real_quote(engine):
    engine.submit_order("600519.SH", "buy", 100.0, 1000)
    res = engine.process_quote("600519.SH", 120.0)
    assert res["unrealized_pnl"] == pytest.approx(20_000.0)
    acc = engine.get_account()
    assert acc["market_value"] == pytest.approx(120_000.0)
    assert acc["unrealized_pnl"] == pytest.approx(20_000.0)
    assert acc["total_assets"] == pytest.approx(acc["cash"] + 120_000.0, abs=0.01)
    # 无持仓标的的行情不产生盈亏
    assert engine.process_quote("000002.SZ", 9.9)["unrealized_pnl"] == 0.0


def test_invalid_orders_raise(engine):
    with pytest.raises(ValueError):
        engine.submit_order("600519.SH", "short", 10.0, 100)
    with pytest.raises(ValueError):
        engine.submit_order("600519.SH", "buy", 10.0, 0)
    with pytest.raises(ValueError):
        engine.submit_order("600519.SH", "buy", 0, 100)
    with pytest.raises(ValueError):
        engine.submit_order("600519.SH", "sell", 10.0, 100)      # 无持仓
    with pytest.raises(ValueError):
        engine.submit_order("600519.SH", "buy", 10_000.0, 10_000)  # 现金不足


def test_metrics(engine):
    import paper.paper_engine as pe
    orig = pe._today_date
    pe._today_date = lambda: "2024-01-01"          # 买入日 D1
    engine.submit_order("600519.SH", "buy", 100.0, 1000)
    engine.submit_order("000001.SZ", "buy", 20.0, 1000)
    engine.submit_order("600036.SH", "buy", 30.0, 1000)
    pe._today_date = lambda: "2024-01-02"          # T+1 次日卖
    engine.submit_order("600519.SH", "sell", 110.0, 1000)   # 盈利
    engine.submit_order("000001.SZ", "sell", 18.0, 1000)    # 亏损
    pe._today_date = orig
    engine.process_quote("600036.SH", 33.0)
    m = engine.metrics()
    assert m["trade_count"] == 5
    assert m["close_count"] == 2
    assert m["win_count"] == 1
    assert m["win_rate"] == 0.5
    assert m["unrealized_pnl"] == pytest.approx(3_000.0)
    assert m["realized_pnl"] < 10_000.0 and m["realized_pnl"] > 0
    assert m["total_pnl"] == pytest.approx(m["realized_pnl"] + m["unrealized_pnl"], abs=0.01)


def test_persist_across_restart(engine):
    engine.submit_order("600519.SH", "buy", 100.0, 1000)
    cash = engine.cash
    e2 = PaperEngine().init(engine.db)          # 模拟重启后重新加载
    assert e2.cash == pytest.approx(cash, abs=0.01)
    pos = e2.get_positions()
    assert len(pos) == 1 and pos[0]["volume"] == 1000
    assert pos[0]["last_price"] == pytest.approx(100.0)


def test_to_from_dict(engine):
    engine.submit_order("600519.SH", "buy", 100.0, 1000)
    snap = engine.to_dict()
    e2 = PaperEngine()
    e2.from_dict(snap)
    assert e2.cash == pytest.approx(engine.cash, abs=0.01)
    assert e2.positions["600519.SH"]["volume"] == 1000


# ---------------- 路由（最小 app，仅挂载 paper 路由） ----------------
@pytest.fixture()
def client(engine):
    import app.routes.paper as paper_routes
    from app.state import state
    state.paper_engine = engine
    app = FastAPI()
    app.include_router(paper_routes.router, prefix="/api/v1")
    yield TestClient(app)
    if hasattr(state, "paper_engine"):
        try:
            del state.paper_engine
        except AttributeError:
            state.paper_engine = None


def test_api_order_and_account(client):
    r = client.post("/api/v1/paper/order", json={"code": "600519.SH", "side": "buy",
                                                 "price": 100.0, "volume": 1000})
    body = r.json()
    assert r.status_code == 200 and body["code"] == 0
    assert body["data"]["status"] == "filled"

    r = client.get("/api/v1/paper/account")
    acc = r.json()["data"]
    assert acc["position_count"] == 1
    assert acc["cash"] < 1_000_000.0
    assert acc["total_assets"] == pytest.approx(acc["cash"] + acc["market_value"], abs=0.01)

    assert len(client.get("/api/v1/paper/positions").json()["data"]) == 1
    assert len(client.get("/api/v1/paper/trades").json()["data"]) == 1
    assert client.get("/api/v1/paper/metrics").json()["data"]["trade_count"] == 1


def test_api_order_invalid_returns_400_code(client):
    r = client.post("/api/v1/paper/order", json={"code": "600519.SH", "side": "hold",
                                                 "price": 10.0, "volume": 100})
    assert r.json()["code"] == 400


def test_api_reset(client):
    client.post("/api/v1/paper/order", json={"code": "600519.SH", "side": "buy",
                                             "price": 100.0, "volume": 1000})
    acc = client.post("/api/v1/paper/reset", json={"initial_capital": 200_000.0}).json()["data"]
    assert acc["cash"] == 200_000.0 and acc["positions"] == []


def test_api_503_when_engine_missing():
    import app.routes.paper as paper_routes
    from app.state import state
    old = getattr(state, "paper_engine", None)
    state.paper_engine = None
    app = FastAPI()
    app.include_router(paper_routes.router, prefix="/api/v1")
    c = TestClient(app)
    assert c.get("/api/v1/paper/account").json()["code"] == 503
    assert c.post("/api/v1/paper/order", json={}).json()["code"] == 503
    state.paper_engine = old
