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


@contextlib.contextmanager
def no_qmt():
    """临时把进程级单例 ``core.state.state.broker_manager`` 置为 None。

    即「未连接任何券商」。``BarsProvider._qmt_connected()`` 读的正是这个**全局单例**，
    而 ``app_client`` 的 lifespan 会把它写成「真实 BrokerManager 且已连接」并**不还原**。
    凡断言「无 QMT 时如何降级」的用例，都必须**显式建立**该前提，不得依赖环境残留
    （否则同进程全量跑时顺序一变即假失败 —— 见 ``conftest._restore_process_state``）。
    """
    from core.state import state

    prev = getattr(state, "broker_manager", None)
    state.broker_manager = None
    try:
        yield
    finally:
        state.broker_manager = prev


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

    def get_bars_batch(self, codes, period="1d", adjust="", limit=250, lite=False):
        """Phase B 新增的批量取数接口（BarsProvider._local_batch 依赖）。

        ``lite`` 为 2026-09-15 新增（选股引擎传 True 走轻量视图）；本桩返回
        构造好的 Bar，属性同名同义，消费方按属性访问故无需分支。
        """
        return {c: list(self._bars.get(c, [])[-limit:]) for c in codes}

    def codes_with_bars(self, *, since="", period="1d", adjust=""):
        """**有日线**的标的代码（``universe`` 的最后一层兜底要用）。"""
        out = []
        for code, bars in self._bars.items():
            if not bars:
                continue
            if since and max((getattr(b, "time", "") for b in bars), default="") < since:
                continue
            out.append(code)
        return sorted(out)

    def get_boards(self, kind):
        return self._boards.get(kind, [])

    def latest_bar_dt(self):
        """全市场 K 线最近一根日期；未显式给 ``as_of`` 时从 ``bars`` 推导。"""
        if self._as_of:
            return self._as_of
        ds = [getattr(b, "time", "") for bars in self._bars.values() for b in bars]
        return max([d for d in ds if d], default="")

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
    """假补充源。

    ``capabilities`` 与真实 ``DataSource.capabilities`` **同名同义** —— 能力的唯一真源
    （V11 R6 起链路求值会拿它做能力校验）。

    ⚠️ 注意：**生产代码里目前没有任何源实现 ``get_fundamentals``**（逐源核对真实方法集
    后确认；baostock/akshare 的旧声明是「想当然」，已在 V11 R6 收敛）。因此
    ``DEFAULT_CAPABILITY_CHAINS["fundamental"]`` 只有 ``broker`` 占位，本桩**不在**链里。
    要验证 ``fetch_fundamentals`` 的字段级溯源逻辑，必须用 ``fundamental_chain(...)``
    临时把链指向本桩。
    """
    capabilities = frozenset({"fundamental"})

    def __init__(self, row):
        self._row = row

    async def get_fundamentals(self, codes):
        return {c: dict(self._row) for c in codes}


class FakeManager:
    """模拟 ``DataSourceManager``（含 ``_plugins`` / ``list_sources`` / ``_declared_map``）。

    ``_declared_map()`` 必须与真实实现**同名同义**（provider_id -> 声明能力集）：
    ``fundamentals._resolve_fundamental_chain`` 会调用它，而那里用 ``except Exception``
    兜底返回空链 —— 替身缺这个方法会被**静默吞掉**，表现为「源明明在却拿不到数据」的
    伪失败（2026-09-15 R6 真实踩到：`test_fundamental_factors.py` 2 条 KeyError）。
    """
    def __init__(self, registered, plugins=None):
        self._reg = set(registered)
        self._plugins = plugins or {}

    def list_sources(self):
        return list(self._reg)

    def _declared_map(self):
        out = {}
        for name, p in self._plugins.items():
            caps = getattr(type(p), "capabilities", None)
            if caps:
                out[name] = frozenset(caps)
        return out


@contextlib.contextmanager
def fundamental_chain(*names):
    """临时把 ``fundamental`` 契约链指向给定源（测试专用）。

    为何需要：``DEFAULT_CAPABILITY_CHAINS["fundamental"]`` 目前**只有 broker 占位**
    （见 ``FakePlugin`` 注释）。所以要验证 ``fetch_fundamentals`` 的字段级溯源逻辑本身，
    必须临时构造「有源可用」的场景；否则链为空，函数只会返回全 None。
    """
    from datasource.providers import provider_catalog
    provider_catalog.set_override("fundamental", list(names))
    try:
        yield
    finally:
        provider_catalog.clear_override()


def fake_reg_manager(registered, commercial_mode: bool = False, declared=None):
    """鸭子类型的 DataSourceManager 替身（只实现被调用到的方法）。

    ★ V11 R7：补 ``_declared_map()`` 与 ``commercial_mode`` 形参。

    背景（2026-09-15 实测）：``BarsProvider.get_bars_batch`` 原先把
    ``list_sources()`` 与 ``_declared_map()`` 放在**同一个 try** 里，
    替身缺 ``_declared_map`` 时 ``AttributeError`` 会把**已算好的 registered 一起丢掉**
    （静默降级为 ``None``）→ 注册态过滤整条失效 → 链里混进用例本已排除的源
    （如 ``test_env2`` 去掉 eltdx 却仍走 eltdx）。故替身必须与真实
    ``DataSourceManager`` 保持接口一致；生产侧亦已拆开 try（见 bars_provider）。

    ``declared`` 默认由 ``registered`` 推导：所有注册源都声明全部常用能力。
    """
    declared_map = declared if declared is not None else {
        pid: frozenset({"kline", "kline_qfq", "quote", "stock_list", "bars"})
        for pid in registered
    }

    class M:
        _commercial_mode = commercial_mode

        def list_sources(self_inner):
            return list(registered)

        def _declared_map(self_inner):
            return dict(declared_map)

    return M()


REG_ALL = {"broker", "eltdx", "baostock", "akshare"}
