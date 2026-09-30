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

import ast
import asyncio
import gc
import warnings
from pathlib import Path

from core.emit import emit_event

BACKEND = Path(__file__).resolve().parents[1]

# ★ 扫描范围：全后端**源码**包，不是只扫 gateway。
#
# 2026-09-28 审计发现的历史漏洞：原实现只扫 `(BACKEND / "gateway")`，而
# `engines/algo.py` / `engines/limitup.py` / `engines/condition_order.py` 三处
# 同样是 `self._on_event(event)` 同步直调 —— 它们接的也是 async 的
# `ws_manager.broadcast`，导致 10 类事件静默丢失，护栏却常年全绿。
# 「有门禁但扫描根写窄」等于没有门禁，故改为穷举所有一级源码包。
SCAN_ROOTS = ("gateway", "engines", "sync", "app", "tools", "core",
              "mcp_server", "datasource", "connectors", "xtquant_client")
# 这些子树是构建产物 / 第三方随包代码，不属本项目源码
SKIP_PARTS = {"dist", "build", "__pycache__", "runtimes", "_internal"}


def _source_files():
    for name in SCAN_ROOTS:
        root = BACKEND / name
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.py")):
            if SKIP_PARTS & set(p.parts):
                continue
            yield p


def _direct_call_sites() -> list[str]:
    """AST 扫描「同步直调被注入的回调槽」。

    为什么不用逐行正则：本文件与 `core/emit.py` 的文档字符串里都要引用
    `self._on_event(event)` 这个**反例**（否则没人看得懂在防什么），正则会把
    解释性文字判成违规；而去掉这些说明又会失去文档价值。AST 天然只看真实代码。

    为什么要排除「自己定义了 _on_event 方法」的类：`xtquant_client/bridge_client.py`
    的 `_on_event` 是**成员方法**（处理子进程事件的处理器），那里的
    `self._on_event(event, msg)` 是调自己的方法，语义完全不同、**不是**被注入的
    回调槽 —— 这类调用合法，不得误报。
    """
    offenders: list[str] = []
    for p in _source_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError:  # 语法破损文件交给 py_compile 类门禁管，这里跳过
            continue
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            has_own = any(
                isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))
                and c.name == "_on_event" for c in cls.body)
            if has_own:
                continue
            for call in (n for n in ast.walk(cls) if isinstance(n, ast.Call)):
                f = call.func
                if (isinstance(f, ast.Attribute) and f.attr == "_on_event"
                        and isinstance(f.value, ast.Name) and f.value.id == "self"):
                    offenders.append(f"{p.relative_to(BACKEND)}:{call.lineno}")
    return offenders


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
    # 真实违规 = AST 命中的「同步直调注入回调槽」
    offenders = _direct_call_sites()
    assert offenders == [], "必须改用 core.emit.emit_event：" + ", ".join(offenders)
