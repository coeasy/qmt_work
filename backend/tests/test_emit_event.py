"""事件派发唯一实现 `core/emit.py::emit_event` 的契约测试。

★ 两条不变量：

1. **异步回调不得被同步调用后丢弃** —— 引擎 `on_event` 接线是 `ws_manager.broadcast`
   （`async def`），同步调用只会创建协程、永不 await ⇒ 下单/成交/风控/对账事件
   **静默丢失**（实测：`POST /trade/order` 后端日志出现
   `RuntimeWarning: coroutine 'WSManager.broadcast' was never awaited`，
   而 `qmt_work.signal` 的事件从未到达 WS 客户端）。
2. **无事件循环时必须显式 close 协程** —— 否则又是一条 never-awaited 警告
   （同步单测 / 脚本直调场景）。
"""
from __future__ import annotations

import asyncio
import gc
import re
import warnings
from pathlib import Path

from core.emit import emit_event

BACKEND = Path(__file__).resolve().parents[1]


def test_none_callback_is_noop():
    assert emit_event(None) is None


def test_sync_callback_runs_and_value_returned():
    seen: list = []
    assert emit_event(lambda a, b=2: seen.append((a, b)) or "ok", 1, b=3) == "ok"
    assert seen == [(1, 3)]


def test_async_callback_is_scheduled_on_running_loop():
    """★不变量 1：有事件循环时，async 回调必须真的被执行（不是只创建协程）。"""
    got: list = []

    async def cb(ev):
        got.append(ev)

    async def scenario():
        task = emit_event(cb, {"type": "order"})
        assert task is not None
        await asyncio.sleep(0)          # 让 create_task 排上
        await task

    asyncio.run(scenario())
    assert got == [{"type": "order"}], "async 事件回调被丢弃 → 前端永远收不到事件"


def test_async_callback_without_loop_is_closed_without_warning():
    """★不变量 2：无事件循环时关闭协程，不得冒 never-awaited 警告。"""
    ran: list = []

    async def cb():
        ran.append(1)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert emit_event(cb) is None
        gc.collect()
    assert ran == [], "无事件循环时不该有协程被执行"
    noisy = [w for w in caught if "never awaited" in str(w.message)]
    assert noisy == [], "协程未关闭 → 每次 GC 都冒 RuntimeWarning：%s" % noisy


def test_callback_exception_is_swallowed():
    """事件推送失败不得影响业务主流程。"""
    def boom():
        raise RuntimeError("ws closed")

    assert emit_event(boom) is None


def test_emit_does_not_swallow_callback_return_of_falsy_value():
    """返回 0/False/"" 也是「已同步完成」，不得被当成失败。"""
    assert emit_event(lambda: 0) == 0
    assert emit_event(lambda: False) is False


# ---------------- 引擎接线验收 ----------------

def test_signal_router_emit_reaches_async_callback():
    """接线验收：SignalRouter 的事件必须真的走到 async 回调。"""
    from gateway.signal_router import SignalRouter

    sr = SignalRouter.__new__(SignalRouter)
    got: list = []

    async def cb(ev):
        got.append(ev)

    sr._on_event = cb

    async def scenario():
        sr._emit({"type": "order", "data": {"code": "600519.SH"}})
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert got and got[0]["type"] == "order"


def test_reconciler_emit_reaches_async_callback():
    from gateway.reconcile import OrderReconciler

    rc = OrderReconciler.__new__(OrderReconciler)
    got: list = []

    async def cb(ev):
        got.append(ev)

    rc._on_event = cb

    async def scenario():
        rc._emit({"type": "reconcile", "data": {}})
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert got and got[0]["type"] == "reconcile"


def test_no_direct_on_event_call_sites():
    """★静态护栏：不得直接同步调用 `self._on_event(...)`。

    这是本缺陷的**唯一形态**（散落多份手写补丁）—— 接线时 `on_event` 通常是
    `ws_manager.broadcast`（async），同步调用只会创建协程、永不 await。
    必须统一走 `core/emit.py::emit_event`。新增引擎照抄旧写法会被本护栏拦下。
    """
    offenders: list[str] = []
    for p in sorted((BACKEND / "gateway").rglob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"self\._on_event\s*\(", line):
                offenders.append(f"{p.relative_to(BACKEND)}:{i}")
    assert offenders == [], "必须改用 core.emit.emit_event：" + ", ".join(offenders)
