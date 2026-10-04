#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态门禁：**无出口循环**与**无超时等待**（"不存在死循环"的可证伪判据）。

为什么需要它
------------
「死循环」在本项目不是假想风险，而是已入库的缺陷家族：

* ``stop_moneyflow_collector`` 曾因停止顺序写错，出现**两条 async 循环同时写
  ``moneyflow_cache``** 的窗口期竞态（R20 修）；
* ``_CONNECT_RETRY_BUDGET`` / ``_READY_TIMEOUT`` / 前端 ``DEFAULT_TIMEOUT`` 三层
  预算链曾互相掩盖，导致「15 秒超时把 100 秒的根因藏掉」（见 docs/TECH_DEBT.md）；
* 备份路径 ``SQLITE_BUSY`` **无限重试且不回调 progress** ⇒ 进程退不出（TD-25）。

这些的共同形态是「**循环/等待没有可终止的出口**」。运行期极难复现（要恰好
撞上那条路径），但**静态可见** —— 所以放在门禁里，而不是留给运气。

判据（AST，全量扫描 ``backend`` 产品代码）
----------------------------------------
**Gate A —— 无出口 ``while`` 循环。**
``while True:`` / ``while 1:`` / ``while <常量真值>`` 的循环体内，若**既没有**
``break``/``return``/``raise``，**也没有** ``await``，则它**不可中断**：
既无法自行退出，也没有协作式取消点（asyncio 只能在 ``await`` 处抛
``CancelledError``），只能杀进程。这是一条铁证式的无限循环。

★ 反过来，「无出口但有 ``await``」**不报** —— 那是本仓大量存在的**常驻服务循环**
（WS 路由、看门狗、限流桶、桥接 poll），它们的设计终止方式就是外部
``task.cancel()``。把它们报成死循环，等于让门禁第一天就被关掉 ——
**误报比漏报更快杀死一条门禁**。这类循环单独计数输出，供人工核对。

★ 同样不报「**进程生命周期守护线程**」里的循环：以
``Thread(target=<fn>, daemon=True)`` 启动的线程，其终止方式就是**随进程消亡**
（daemon 线程不阻塞解释器退出）。本仓 4 处即此类：apikey 用量刷盘、日志告警
消费者、桥接子进程的父进程看护与连接态泵。

  这条判据刻意**认语义（``daemon=True``）而不是认行号**：行号豁免会随编辑漂移，
  而 ``daemon=True`` 本身就是「我不需要独立停止机制」的显式声明。
  反过来，**非** daemon 线程里出现无出口循环仍会被拦 —— 那才会真的卡住进程退出。

★ 三种出口出现在循环体**任意深度**（含 ``if`` / ``try``）都算数；但**不跨函数边界**
（``def _inner(): ... return`` 里的 ``return`` 属于另一个函数，是「假出口」）。

★ 刻意**不**报 ``while <变量>:`` —— 「变量是否可能一直为真」需要数据流分析，
超出静态判据能诚实回答的范围。宁可只抓铁证，不猜。

**Gate B —— 无超时的阻塞等待。**
``threading.Event().wait()`` / ``Thread.join()`` 不传超时：一旦目标卡住，
调用方**永远**回不来（TD-25 正是这么冻住进程退出的）。

★ 超时可以是**位置参数**：``threading.Event.wait()`` 的签名是
``wait(timeout=None)``，实测代码里写的就是 ``ev.wait(timeout)``（位置传参）。
只查关键字参数会把这种**真·有超时**的调用误报成缺陷（首版即踩到）。

豁免
----
逐条列在 ``EXEMPT``，必须写明理由；``--list-exempt`` 可打印。空豁免也合法
（当前 0 条），但**每加一条都要在评审里过一遍**——这个清单是门禁唯一的软化点。

用法：
    cd backend && python ../scripts/check_unbounded_waits.py
    cd backend && python ../scripts/check_unbounded_waits.py --list-exempt
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SCAN_DIRS = tuple(BACKEND / d for d in ("app", "core", "gateway", "datasource",
                                        "engines", "tools", "mcp_server",
                                        "connectors", "xtquant_client",
                                        "agent_bigqmt"))
SKIP_PARTS = frozenset({"runtimes", "dist", "build", "__pycache__", "node_modules",
                        ".venv", "output", "tests", "probes"})

#: (相对仓库根的路径, 行号) → 理由。软化的唯一入口，逐条必须能说服人。
EXEMPT_LOOPS: dict[tuple[str, int], str] = {}
EXEMPT_JOINS: dict[tuple[str, int], str] = {}

#: Gate A 认定的「出口」语句。
_EXITS = (ast.Break, ast.Return, ast.Raise)


def _is_true_const(node: ast.AST) -> bool:
    """``while True`` / ``while 1`` / ``while "x"`` —— 字面量真值。"""
    if isinstance(node, ast.Constant):
        return bool(node.value)
    # while 1 == 1 这类；只认一元/二元常量折叠的一层，不猜变量
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return isinstance(node.operand, ast.Constant) and not node.operand.value
    if isinstance(node, ast.Compare) and all(
            isinstance(c, ast.Constant) for c in [node.left, *node.comparators]):
        try:
            return bool(eval(compile(ast.Expression(node), "<x>", "eval")))  # noqa: S307
        except Exception:  # noqa: BLE001
            return False
    return False


def _has_exit(body: list[ast.stmt]) -> bool:
    """循环体内（**不下钻嵌套函数/类**）是否存在 break/return/raise。

    不下钻是刻意的：``def _inner(): while True: ... return`` 里的 ``return``
    属于另一个函数，不能当作外层循环的出口 —— 那正是「假出口」。
    """
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, _EXITS):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda)):
            continue                      # 不跨函数边界
        stack.extend(ast.iter_child_nodes(node))
    return False


def _has_await(body: list[ast.stmt]) -> bool:
    """循环体内（不下钻嵌套函数）是否存在 ``await`` —— 即协作式取消点。"""
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Await):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


def _loop_line(node: ast.AST) -> int:
    return getattr(node, "lineno", 0)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """子节点 → 父节点（``ast.walk`` 不给父指针，找外层函数需要它）。"""
    out: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _daemon_thread_targets(tree: ast.AST) -> set[str]:
    """本文件里以 ``Thread(target=<name>, daemon=True)`` 启动的函数名。

    这些函数的循环**不需要**独立停止机制：daemon 线程随进程消亡。
    见模块 docstring「进程生命周期守护线程」一节。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_thread = ((isinstance(func, ast.Attribute) and func.attr == "Thread")
                     or (isinstance(func, ast.Name) and func.id == "Thread"))
        if not is_thread:
            continue
        daemon = False
        target = None
        for kw in node.keywords:
            if kw.arg == "daemon":
                daemon = (isinstance(kw.value, ast.Constant)
                          and kw.value.value is True)
            elif kw.arg == "target":
                if isinstance(kw.value, ast.Name):
                    target = kw.value.id
                elif isinstance(kw.value, ast.Attribute):
                    target = kw.value.attr
        if daemon and target:
            names.add(target)
    return names


def _enclosing_func_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(cur)
    return None


def scan_file(path: Path) -> tuple[list[str], list[str], list[str]]:
    loops: list[str] = []
    resident: list[str] = []
    joins: list[str] = []
    rel = path.relative_to(ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
    except SyntaxError as exc:
        return [f"{rel}: 无法解析：{exc}"], [], []

    parents = _parents(tree)
    daemon_targets = _daemon_thread_targets(tree)

    for node in ast.walk(tree):
        # ---- Gate A: while 恒真、无出口、无 await ⇒ 不可中断 ----
        if isinstance(node, ast.While) and _is_true_const(node.test):
            if not _has_exit(node.body):
                ln = _loop_line(node)
                owner = _enclosing_func_name(node, parents)
                if (rel, ln) in EXEMPT_LOOPS:
                    pass
                elif owner is not None and owner in daemon_targets:
                    resident.append(f"{rel}:{ln}(daemon 线程 {owner})")
                elif _has_await(node.body):
                    resident.append(f"{rel}:{ln}")      # 常驻可取消循环，仅计数
                else:
                    loops.append(
                        f"{rel}:{ln}: while <恒真> 既无 break/return/raise，"
                        f"也无 await —— 不可中断（无法自行退出，也没有取消点）")
        # ---- Gate B: join/wait 无超时（关键字或位置参数都算）----
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in ("join", "wait"):
                recv = ast.unparse(node.func.value).lower()
                looks_blocking = ("thread" in recv or "thr" in recv
                                  or isinstance(node.func.value, ast.Name)
                                  and node.func.value.id.lower() in
                                  ("t", "th", "thread", "worker", "ev", "event"))
                if not looks_blocking:
                    continue
                has_timeout = (any(k.arg == "timeout" for k in node.keywords)
                               or bool(node.args))          # 位置参数同样有效
                if not has_timeout and (rel, _loop_line(node)) not in EXEMPT_JOINS:
                    joins.append(
                        f"{rel}:{_loop_line(node)}: {recv}.{attr}(...) 未传 timeout "
                        f"（位置或关键字）—— 目标卡住则调用方永不返回")
    return loops, resident, joins


def _iter_sources():
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if set(p.relative_to(BACKEND).parts) & SKIP_PARTS:
                continue
            yield p


def main() -> int:
    ap = argparse.ArgumentParser(description="无出口循环 / 无超时等待静态门禁")
    ap.add_argument("--list-exempt", action="store_true", help="打印豁免清单并退出")
    args = ap.parse_args()
    if args.list_exempt:
        print("EXEMPT_LOOPS:")
        for (rel, ln), why in sorted(EXEMPT_LOOPS.items()):
            print(f"  {rel}:{ln} —— {why}")
        print("EXEMPT_JOINS:")
        for (rel, ln), why in sorted(EXEMPT_JOINS.items()):
            print(f"  {rel}:{ln} —— {why}")
        return 0

    files = list(_iter_sources())
    # ★ 自检：扫不到文件说明目录写错 ⇒ 门禁会「永远绿」。宁可报错也不静默放行。
    if len(files) < 50:
        print(f"只扫描到 {len(files)} 个 .py —— 门禁失效（检查 SCAN_DIRS/SKIP_PARTS）",
              file=sys.stderr)
        return 1

    all_loops: list[str] = []
    all_joins: list[str] = []
    resident_n = 0
    for f in files:
        a, r, b = scan_file(f)
        all_loops += a
        resident_n += len(r)
        all_joins += b

    if all_loops:
        print("uninterruptible while loop(s) detected:", file=sys.stderr)
        print("\n".join(all_loops), file=sys.stderr)
    if all_joins:
        print("unbounded blocking wait(s) detected:", file=sys.stderr)
        print("\n".join(all_joins), file=sys.stderr)
    if all_loops or all_joins:
        return 1
    print(f"unbounded-wait gate: OK ({len(files)} files, "
          f"uninterruptible while = 0, unbounded join/wait = 0, "
          f"resident cancellable loops = {resident_n} 已确认可取消)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
