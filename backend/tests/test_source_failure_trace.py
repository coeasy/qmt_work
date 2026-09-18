"""数据源失败溯源（``last_failure_trace``）与「拿不到数据」错误文案的准确性。

锁定两条不变量（错了会把用户排查方向带偏）：

1. **「不支持」不得说成「网络坏了」** —— 实测（2026-09-19）用户连着券商、
   显式 ``source=broker`` 查板块榜，却拿到「TDX 行情源暂不可用，请检查网络或
   连接券商」。真实原因是 ``_sup_chain("broker")`` **刻意返回空链**（券商不提供
   sector 能力），与 TDX、与网络毫无关系。
2. **源链非空但全失败时，必须列出实际尝试过的源与各自原因** ——
   ``_first_supported`` 的返回值只有 ``(None, None)``，信息量不足以决定下一步。
"""
from __future__ import annotations

import asyncio

from datasource.registry import DataSourceManager


class _NoBoards:
    """注册了但不实现 get_boards 的源。"""

    name = "eltdx"

    def has(self) -> bool:
        return True


class _EmptyBoards:
    """实现了但返回空（模拟行情源不可用）。"""

    name = "eltdx"

    async def get_boards(self, *a, **k):
        return []


def _run(coro):
    return asyncio.run(coro)


def test_empty_chain_is_reported_as_unsupported_not_network():
    """``source=broker`` 查 sector ⇒ 空链，文案必须说「不提供该能力」。"""
    from app.services.market.aggregates import _unavailable

    mgr = DataSourceManager()
    mgr._plugins = {"eltdx": _NoBoards()}
    # broker 对 sector 能力刻意返回空链（见 _sup_chain 的注释）
    _run(mgr._first_supported("get_boards", "industry", "pct", 10,
                              source="broker", capability="sector"))
    assert mgr.last_failure_trace()["chain"] == []

    import app.services.market.aggregates as agg
    orig = agg.get_hub
    agg.get_hub = lambda: mgr
    try:
        msg = _unavailable("板块榜", "broker")
    finally:
        agg.get_hub = orig

    assert "不提供该能力" in msg, f"应说明「不支持」，实际：{msg}"
    assert "TDX" not in msg, f"不得再甩锅 TDX/网络：{msg}"
    assert "source=auto" in msg, f"应给出可执行的下一步：{msg}"


def test_auto_source_must_not_be_told_to_use_auto():
    """``source=auto`` 已经是 auto 了 —— 再提示「请改用 source=auto」是自相矛盾。

    这种提示比不提示更糟：用户会以为自己传错了参数，反复重试同一个请求。
    """
    from app.services.market.aggregates import _unavailable

    mgr = DataSourceManager()
    mgr._plugins = {}
    mgr._sup_chain = lambda *a, **k: []
    _run(mgr._first_supported("get_boards", "industry", "pct", 10, source="auto"))

    import app.services.market.aggregates as agg
    orig = agg.get_hub
    agg.get_hub = lambda: mgr
    try:
        msg = _unavailable("板块榜", "auto")
    finally:
        agg.get_hub = orig

    assert "无任何数据源声明该能力" in msg, f"auto 应说明「无源声明该能力」：{msg}"
    assert "请改用 source=auto" not in msg, f"不得让用户改用他已经在用的参数：{msg}"


def test_non_empty_chain_lists_tried_sources():
    """源链非空时，文案必须列出实际尝试过的源与原因。"""
    from app.services.market.aggregates import _unavailable

    mgr = DataSourceManager()
    mgr._plugins = {"eltdx": _EmptyBoards()}
    mgr._sup_chain = lambda *a, **k: ["eltdx"]
    _run(mgr._first_supported("get_boards", "industry", "pct", 10, source="auto"))

    trace = mgr.last_failure_trace()
    assert trace["chain"] == ["eltdx"]
    assert trace["tried"], f"应记录失败原因：{trace}"

    import app.services.market.aggregates as agg
    orig = agg.get_hub
    agg.get_hub = lambda: mgr
    try:
        msg = _unavailable("板块榜")
    finally:
        agg.get_hub = orig

    assert "eltdx" in msg, f"应说明试过哪个源：{msg}"


def test_trace_records_missing_method_and_breaker():
    """溯源要能区分「未注册 / 不支持该方法 / 熔断中 / 返回空」。"""
    mgr = DataSourceManager()
    mgr._plugins = {"eltdx": _NoBoards()}
    mgr._sup_chain = lambda *a, **k: ["eltdx", "ghost"]
    _run(mgr._first_supported("get_boards", "industry", "pct", 10, source="auto"))

    tried = mgr.last_failure_trace()["tried"]
    assert any("不支持 get_boards" in t for t in tried), tried
    assert any("未注册" in t for t in tried), tried


def test_trace_is_reset_between_calls():
    """每次调用都要重置溯源，不能拿上一次的残留去解释这一次。"""
    mgr = DataSourceManager()
    mgr._plugins = {"eltdx": _EmptyBoards()}
    mgr._sup_chain = lambda *a, **k: ["eltdx"]
    _run(mgr._first_supported("get_boards", "industry", "pct", 10, source="auto"))
    assert mgr.last_failure_trace()["tried"]

    mgr._sup_chain = lambda *a, **k: []  # 下一次：空链
    _run(mgr._first_supported("get_boards", "industry", "pct", 10, source="broker"))
    assert mgr.last_failure_trace()["tried"] == [], "溯源未重置，会拿上次原因解释这次"
