"""测试隔离护栏（V11 R8）。

## 为什么需要

``tests/test_webhook_out.py`` 曾用

    monkeypatch.setattr("gateway.webhook_out.asyncio.sleep", fake_sleep)

来跳过重试退避。问题：``gateway.webhook_out.asyncio`` **就是全局 asyncio 模块**，
所以这是**进程级**打桩。而 ``app_client``（``scope="session"``）用
``TestClient(app).__enter__()`` 启动 lifespan —— 它跑在 **anyio 后台 portal 线程**的
事件循环里，且**全程存活**；lifespan 启动的后台任务会周期性 ``await asyncio.sleep(x)``：

| 后台任务 | 位置 | 周期 |
|----------|------|------|
| 行情微批 flush | ``sync/__init__.py:436`` ``_batch_loop`` | **0.1s** |
| 调度循环 | ``app/runtime/schedules.py:231`` | 30s |
| 作业回收 | ``app/runtime/jobs.py:317`` | ≥5s |
| 对账 / 看门狗 / 资金流采集 | ``gateway/reconcile.py`` / ``order_watchdog.py`` / ``kline_io.py`` | 30~600s |

打桩期间 ``fake_sleep`` 内部 ``await real_sleep(0)`` **不再真正等待** → ``_batch_loop``
**空转**，把几百个无关的 ``0.1`` 记进被测的 ``delays`` →

    assert [0.1, 0.1, 0....0.1, 0.1, ...] == [0.5, 1.0]
    E  Left contains 276 more items, first extra item: 0.1

**同一命令、同一代码，时好时坏**（实测 5 次里 2 次红）。

## 规矩

**不要打桩进程级全局**（``asyncio.sleep`` / ``time.sleep`` / ``time.time`` 等）。
改为在被测模块里留一个**模块级间接层**，测试只打桩那一层：

```python
# 生产代码（见 gateway/webhook_out.py、app/runtime/schedules.py 的既有约定）
async def _sleep(seconds: float) -> None:
    await _asyncio.sleep(seconds)

# 测试
monkeypatch.setattr("gateway.webhook_out._sleep", fake_sleep)
```

本护栏扫描 ``tests/`` 下所有 ``setattr`` 目标，禁止末段为 ``asyncio.sleep`` /
``time.sleep`` / ``time.time`` / ``time.monotonic`` 的写法。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: 禁止被「进程级打桩」的全局符号（末段匹配）
FORBIDDEN_TARGETS = ("asyncio.sleep", "time.sleep", "time.time",
                     "time.monotonic", "time.perf_counter")

#: monkeypatch.setattr("a.b.c", ...) / monkeypatch.setattr("a.b.c", ..., raising=...)
_SETATTR_RX = re.compile(r"""setattr\s*\(\s*["']([^"']+)["']""")


def _iter_test_files():
    for p in sorted(TESTS_DIR.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def _code_lines(p: Path):
    """产出 (行号, 行内容)，**跳过注释行与 docstring 行**。

    必须只看代码：本模块 docstring 里就写着反例写法，若把 docstring 算进去会自我误报
    （R7 在 ``_body_source`` 上、R8 在 ``test_clock_unity`` 上都踩过同一个坑）。
    """
    src = p.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc = body[0]
                for ln in range(doc.lineno, (doc.end_lineno or doc.lineno) + 1):
                    skip.add(ln)
    for i, line in enumerate(src.splitlines(), 1):
        if i in skip:
            continue
        if line.strip().startswith("#"):
            continue
        yield i, line


def test_no_process_global_monkeypatch_of_stdlib_clocks():
    """禁止对 ``asyncio.sleep`` / ``time.sleep`` 等做**进程级** monkeypatch。

    反例（会被本用例判红）：把 ``asyncio.sleep`` 作为 setattr 目标
    —— 该写法等价于改全局 ``asyncio.sleep``，会截走 app_client lifespan 留在 portal
    线程上的后台循环（尤其 ``sync`` 的 100ms 微批），造成间歇性假失败。
    """
    offenders: list[str] = []
    for p in _iter_test_files():
        for i, line in _code_lines(p):
            for m in _SETATTR_RX.finditer(line):
                target = m.group(1)
                if target in FORBIDDEN_TARGETS or \
                        any(target.endswith("." + t) for t in FORBIDDEN_TARGETS):
                    offenders.append(f"{p.relative_to(TESTS_DIR.parent).as_posix()}:{i}: {target}")
    assert not offenders, (
        "检测到对进程级全局时钟的 monkeypatch（请改为打桩被测模块的模块级间接层，"
        "例如 gateway.webhook_out._sleep）：\n  " + "\n  ".join(offenders)
    )
