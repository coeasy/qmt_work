"""Phase 4 测试共享设施（离线 fake + 依赖门禁绕过）。

本模块让 Phase 4 多源选股逻辑可在**不安装 akshare/baostock/eltdx** 的离线环境下稳定
复现 D12 三环境矩阵：通过 monkeypatch ``importlib.util.find_spec`` 强制可选依赖「可用」，
使链路仅由注册态 / 许可证决定。
"""
from __future__ import annotations

import contextlib
import importlib.util
import types

from datasource.models import Bar


@contextlib.contextmanager
def force_deps():
    """强制所有可选依赖可用（仅测试用），还原后退出。"""
    orig = importlib.util.find_spec
    importlib.util.find_spec = lambda name: types.ModuleType(name)
    try:
        yield
    finally:
        importlib.util.find_spec = orig


def make_bar(close, open_=1, high=2, low=1, volume=100, time_="2024-01-02"):
    return Bar(time=time_, open=open_, high=high, low=low, close=close, volume=volume)


class FakeStore:
    def __init__(self, stock_list=None, bars=None, boards=None, as_of=None):
        self._stock = stock_list or []
        self._bars = bars or {}
        self._boards = boards or {}
        self._as_of = as_of

    def get_stock_list(self):
        return self._stock

    def get_bars(self, code, period="1d", adjust="", limit=250, start=None, end=None):
        return list(self._bars.get(code, [])[-limit:])

    def get_bars_batch(self, codes, period="1d", adjust="", limit=250):
        """Phase B 新增的批量取数接口（BarsProvider._local_batch 依赖）。"""
        return {c: list(self._bars.get(c, [])[-limit:]) for c in codes}

    def get_boards(self, kind):
        return self._boards.get(kind, [])

    def latest_bar_dt(self):
        return self._as_of

    def set_meta(self, k, v):
        pass

    def upsert_boards(self, kind, items):
        return len(items)


class FakeKlineHub:
    """source -> {code: [Bar,...]}。"""
    def __init__(self, data):
        self._data = data

    async def get_kline(self, code, period, count, *, source, adjust=None):
        return self._data.get(source, {}).get(code, None), source


class FakePlugin:
    def __init__(self, row):
        self._row = row

    async def get_fundamentals(self, codes):
        return {c: dict(self._row) for c in codes}


class FakeManager:
    """模拟 DataSourceManager（含 _plugins + list_sources）。"""
    def __init__(self, registered, plugins=None):
        self._reg = set(registered)
        self._plugins = plugins or {}

    def list_sources(self):
        return list(self._reg)


def fake_reg_manager(registered):
    class M:
        _commercial_mode = False
        def list_sources(self_inner):
            return list(registered)
    return M()


REG_ALL = {"broker", "eltdx", "baostock", "akshare"}
