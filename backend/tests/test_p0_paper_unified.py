"""P0-6 回归：paper 模式必须统一走 PaperEngine（单一模拟盘账户）。

## 缺陷回顾

`SignalRouter._paper()` 曾**直写 paper_orders 表**，既不影响 `/paper/account`
的持仓/现金，也不受 PaperEngine 的现金/T+1/涨跌停校验；而 `strategy_runtime`
走的是 `PaperEngine`。结果是同一个「模拟盘」存在**两套互不相通的账**：
外部信号下的模拟单在 /paper 页面上完全看不到，也不消耗现金。

## 本文件锁死什么

1. paper 信号成交后，**PaperEngine 的持仓与现金真实变化**（同一账户）；
2. PaperEngine 的业务校验（可卖数量/现金）对 paper 信号**同样生效**（不再无脑记账）；
3. 引擎未注入时**明确失败**，绝不静默假装成交（零 mock）。
"""
import asyncio

import pytest

from engines.paper_engine import PaperEngine
from gateway.signal_router import SignalRouter


class _NoBrokerManager:
    """无券商连接：paper 模式本就不需要券商。"""

    def bridge(self, conn_id=None):
        return None

    def all_connections(self):
        return []


@pytest.fixture()
def env(tmp_path):
    from core.db import DB
    db = DB(tmp_path / "paper_unified.db")
    pe = PaperEngine(initial_capital=1_000_000.0).init(db)
    yield pe, db
    db._conn.close()


def _router(pe, db, mode="paper"):
    r = SignalRouter(_NoBrokerManager(), risk=None, db=db, paper_engine=pe)
    r.mode = mode
    return r


def _paper_orders(db):
    return db.query("SELECT * FROM paper_orders")


def test_paper_signal_hits_paper_engine_account(env):
    """paper 信号成交后，PaperEngine 持仓/现金变化，且落一行信号受理日志。"""
    pe, db = env
    r = _router(pe, db)
    cash0 = pe.cash

    res = asyncio.run(r.submit(code="600519.SH", side="buy", volume=100, price=10.0,
                               source="manual"))
    assert res["ok"] is True and res["mode"] == "paper", res
    # 账户真源（PaperEngine）已变化 —— 这正是统一前缺失的
    pos = {p["code"]: p for p in pe.get_positions()}
    assert pos["600519.SH"]["volume"] == 100
    assert pe.cash < cash0
    # 信号受理日志恰好一行，且价格来自真实成交
    rows = _paper_orders(db)
    assert len(rows) == 1 and rows[0]["code"] == "600519.SH"
    assert rows[0]["price"] == 10.0


def test_paper_signal_respects_engine_rejections(env):
    """PaperEngine 的业务校验对 paper 信号生效：无可卖持仓 → 拒单且不落日志。"""
    pe, db = env
    r = _router(pe, db)
    res = asyncio.run(r.submit(code="600519.SH", side="sell", volume=100, price=10.0,
                               source="manual"))
    assert res["ok"] is False and "模拟盘" in res["reason"], res
    assert _paper_orders(db) == []          # 被拒单绝不记成已受理


def test_paper_signal_respects_insufficient_cash(tmp_path):
    """现金不足时 paper 信号被拒（统一前 _paper 无任何现金校验）。"""
    from core.db import DB
    db = DB(tmp_path / "paper_cash.db")
    try:
        pe = PaperEngine(initial_capital=1_000.0).init(db)
        r = _router(pe, db)
        res = asyncio.run(r.submit(code="600519.SH", side="buy", volume=100, price=100.0,
                                   source="manual"))   # 需 10000 > 现金 1000
        assert res["ok"] is False and "现金不足" in res["reason"], res
        assert pe.get_positions() == []
        assert _paper_orders(db) == []
    finally:
        db._conn.close()


def test_paper_signal_without_engine_fails_loudly(env):
    """引擎未注入 → 明确失败，绝不静默假装成交。"""
    _pe, db = env
    r = _router(None, db)
    res = asyncio.run(r.submit(code="600519.SH", side="buy", volume=100, price=10.0,
                               source="manual"))
    assert res["ok"] is False and "模拟盘引擎未初始化" in res["reason"], res
    assert _paper_orders(db) == []


def test_paper_market_signal_uses_estimated_price(env):
    """市价 paper 单用路由层估好的最新价成交（PaperEngine 拒绝 price<=0）。"""
    pe, db = env

    class _QuoteBridge:
        gateway = None

        def __init__(self):
            self.gateway = self

        def get_quote(self, code):
            return None

        async def call(self, fn, *a, **k):
            return {"last": 12.5}

    class _QuoteManager:
        def bridge(self, conn_id=None):
            return _QuoteBridge()

        def all_connections(self):
            return []

    r = SignalRouter(_QuoteManager(), risk=None, db=db, paper_engine=pe)
    r.mode = "paper"
    res = asyncio.run(r.submit(code="600519.SH", side="buy", volume=100, price=0.0,
                               price_type="market", source="manual"))
    assert res["ok"] is True, res
    assert res["price"] == 12.5                     # 以估价成交，而非 0
    pos = {p["code"]: p for p in pe.get_positions()}
    assert pos["600519.SH"]["volume"] == 100
