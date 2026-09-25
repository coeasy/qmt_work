"""P1-4 守卫：``state.bridge`` / ``state.gateway`` 是**只写槽位**，业务代码不得读。

背景
----
这两个槽位只在装配期赋值（``app/bootstrap/phase_broker.py::_sync_active_bridge``
与 ``phase_watchdogs.py``）。业务侧读「当前活跃连接」的唯一入口是
``broker_manager.active_bridge()`` —— 它是**动态求值**，不存在「缓存过期」这个状态。

历史缺陷（本守卫锁死的那一类）
------------------------------
交易日历刷新曾读 ``state.bridge``。于是「先开软件、后开 QMT 客户端」这条**最常见**
路径上：启动预算内没连上 → 槽位为 ``None`` → 日历永久停在 fallback（工作日规则
把节假日当交易日）；等连接真的连上了，也没有任何机制去校正它。

为什么用 AST 而不是 grep
------------------------
``state.bridge = ...``（写）与 ``x = state.bridge``（读）只差一个赋值号，
grep 会把写点一起报出来，于是要么漏、要么逼人写例外清单。
AST 的 ``Load`` / ``Store`` 上下文是**精确**判据，不需要例外清单。

★ 可证伪性：本用例已被变异验证 —— 在任一业务文件里加一行 ``_x = state.bridge``
  即变红（见 docs/2026-09-25 第 26 轮记录）。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

#: 扫描范围：后端全部 .py，排除运行时 / 构建产物 / 打包输出 / 测试自身。
SKIP_PARTS = frozenset({"runtimes", "dist", "build", "__pycache__",
                        "tests", "node_modules", ".venv", "output"})

#: 只写槽位属性名
WRITE_ONLY_ATTRS = frozenset({"bridge", "gateway"})
#: 承载这两个槽位的单例名（``core.state.state``）
STATE_NAMES = frozenset({"state"})


def _load_reads(path: Path) -> list[tuple[int, str]]:
    """返回该文件里 ``state.bridge`` / ``state.gateway`` 的**读**（Load 上下文）位置。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.ctx, ast.Load):      # 写点（Store/Del）不算
            continue
        if node.attr not in WRITE_ONLY_ATTRS:
            continue
        base = node.value
        if isinstance(base, ast.Name) and base.id in STATE_NAMES:
            out.append((node.lineno, f"{base.id}.{node.attr}"))
    return out


def test_no_business_read_of_write_only_bridge_slots():
    """全后端 AST 扫描：只写槽位不得出现在任何读上下文里。"""
    scanned = 0
    offenders: list[str] = []
    for path in sorted(BACKEND.rglob("*.py")):
        if set(path.relative_to(BACKEND).parts) & SKIP_PARTS:
            continue
        scanned += 1
        for lineno, what in _load_reads(path):
            offenders.append(f"{path.relative_to(BACKEND)}:{lineno}: {what}")

    # 自检：扫不到文件说明路径/排除项写错 ⇒ 用例会「永远绿」。
    assert scanned > 50, f"只扫描到 {scanned} 个文件，守卫可能失效"
    assert not offenders, (
        "state.bridge / state.gateway 是**只写槽位**，业务不得读它；\n"
        "请改用 broker_manager.active_bridge()（动态求值，无缓存过期问题）：\n  "
        + "\n  ".join(offenders))


def test_calendar_refresh_uses_manager_not_cache():
    """日历刷新必须走 ``broker_manager.active_bridge()``（P1-4 的正面判据）。

    ★ 判据用 **AST** 而不是 ``ast.unparse`` 之后做子串匹配：
    本函数的 docstring 里就写着「不再读 state.bridge」，子串匹配会被**自己的注释**
    骗红 —— 注释不是可执行代码，源码扫描必须先剥掉它（第 26 轮踩过的坑）。
    """
    src = (BACKEND / "app" / "bootstrap" / "phase_broker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_refresh_trading_calendar"), None)
    assert fn is not None, "找不到 _refresh_trading_calendar"

    # 剥掉 docstring，只留可执行语句
    body = [s for s in fn.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                    and isinstance(s.value.value, str))]

    reads = [n for s in body for n in ast.walk(s)
             if isinstance(n, ast.Attribute) and n.attr in WRITE_ONLY_ATTRS
             and isinstance(n.ctx, ast.Load)
             and isinstance(n.value, ast.Name) and n.value.id in STATE_NAMES]
    assert not reads, "交易日历刷新不得读只写槽位 state.bridge / state.gateway"

    calls = [ast.unparse(n.func) for s in body for n in ast.walk(s)
             if isinstance(n, ast.Call)]
    assert any("active_bridge" in c for c in calls), \
        "交易日历刷新必须经 broker_manager.active_bridge() 取活跃连接"
