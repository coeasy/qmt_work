"""``app/services/market/kline_io.py`` 回归测试（此前该模块 0 测试）。

重点：``crawl_market`` 的 market_cache 兜底分支必须把落库**移出事件循环**。
2026-09-14：``days×codes`` 条同步 sqlite 写若跑在事件循环里，会阻塞所有 HTTP 请求
（与 ``app/sync/bars.py`` 的 ``BarsSyncer.sync_one``、``app/screener/engine.py`` 的
``evaluate_scan`` 属同类缺陷）。

（项目约定：无 pytest-asyncio，顶层 asyncio.run 包裹。）
"""
import asyncio
import threading
from types import SimpleNamespace

from app.services.market import kline_io
from core.state import state


class _FakeGateway:
    """仅作为传给 ``b.call`` 的可调用对象；不应被直接调用。"""

    def get_kline(self, *args):
        raise AssertionError("crawl_market 不应绕过 bridge 直接调 gateway")


class _FakeBridge:
    """券商桥桩：``call`` 直接回放预置 K 线（返回 list 而非错误 dict）。"""

    def __init__(self, bars):
        self.gateway = _FakeGateway()
        self._bars = bars

    async def call(self, fn, *args):
        return self._bars


class _RecorderDB:
    """记录 upsert 的调用线程，用于验证「落库已离开事件循环」。"""

    def __init__(self):
        self.calls = []
        self.threads = set()

    def upsert(self, table, row):
        self.calls.append((table, row))
        self.threads.add(threading.current_thread())
        return 1


def test_crawl_market_cache_fallback_upserts_off_event_loop(monkeypatch):
    """无 kline_cache 时走 market_cache 兜底，逐条落库必须在线程里执行。"""
    bars = [{"time": "2026%03d" % i, "close": 10.0 + i} for i in range(5)]
    db = _RecorderDB()

    monkeypatch.setattr(
        state, "broker_manager",
        SimpleNamespace(bridge=lambda conn_id=None: _FakeBridge(bars)),
        raising=False)
    monkeypatch.setattr(state, "kline_cache", None, raising=False)
    monkeypatch.setattr(state, "db", db, raising=False)

    main_thread = threading.current_thread()
    out = asyncio.run(kline_io.crawl_market({"codes": ["600000.SH"], "days": 5}))

    assert out["bars_inserted"] == 5
    assert len(db.calls) == 5
    assert all(c[0] == "market_cache" for c in db.calls)
    assert db.threads, "没有任何 upsert 发生，用例没覆盖到兜底分支"
    assert main_thread not in db.threads, (
        "market_cache 落库仍在事件循环线程执行——会阻塞所有 HTTP 请求")
