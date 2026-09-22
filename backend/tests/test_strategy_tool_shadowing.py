"""MCP 策略工具「同名遮蔽」缺陷的回归测试（2026-09-22 修）。

为什么需要它
------------
`tools/strategy_gen.py` 的 `register_strategy_tools()` 里，两个**被 `@mcp.tool()`
装饰的嵌套函数**与它们各自要调用的**模块级函数同名**：

    def generate_strategy(...):        # 模块级实现（第 189 行）
    def save_qmt_strategy(...):        # 模块级实现（第 219 行）

    def register_strategy_tools(mcp):
        @mcp.tool()
        async def generate_strategy(...):        # ← 与模块级同名
            return generate_strategy(...)        # ← 调用的是自己（装饰后的 FunctionTool）

`@mcp.tool()` 会把被装饰的名字重绑为 `FunctionTool` 对象，于是函数体里的
`generate_strategy(...)` 解析到的是**本函数的闭包变量**（即装饰后的自己）⇒
运行期 `TypeError: 'FunctionTool' object is not callable`。

**为什么以前没被发现**：这两个工具**没有任何单测直接调用过**，而 `tools/list`
只证明「工具注册了」，不证明「调用得通」。实测（MCP `tools/call`，修复前）：

    Error calling tool 'save_qmt_strategy': 'FunctionTool' object is not callable

锁定四条不变量：

1. **对外工具名不变**：修法是「嵌套函数改名 + `@mcp.tool(name=...)`」，不是改工具名 ——
   工具名是 MCP 客户端的契约，改名等于打断调用方；
2. **两个工具真的能跑通**（不只是注册上）；
3. **`save_qmt_strategy` 真的落盘**到 `{client_path}/mpython/`；
4. **静态护栏**：`register_*_tools` 内的嵌套工具函数**不得**与模块级函数同名 ——
   这条是防复发的总闸，扫的是全部 `tools/` 与 `mcp_server/`，而不是只盯这一个文件。
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from fastmcp import FastMCP

from tools.strategy_gen import register_strategy_tools

BACKEND = Path(__file__).resolve().parent.parent


def _tools() -> dict:
    """用真实 FastMCP 注册（不用桩：桩不会把名字重绑成 FunctionTool，就复现不了本缺陷）。"""
    mcp = FastMCP("shadowing-test")
    register_strategy_tools(mcp)
    return asyncio.run(mcp.get_tools())


def test_tool_names_are_unchanged() -> None:
    """不变量 1：对外工具名必须仍是 `generate_strategy` / `save_qmt_strategy`。"""
    assert sorted(_tools()) == ["generate_strategy", "save_qmt_strategy"]


def test_generate_strategy_is_callable() -> None:
    """不变量 2：调用得通，且返回真实策略代码（不是 TypeError 文本）。"""
    tool = _tools()["generate_strategy"]
    res = asyncio.run(tool.run({"strategy_type": "ma_cross", "code": "600519.SH"}))
    data = res.structured_content
    assert data["strategy_type"] == "ma_cross"
    assert data["code"] == "600519.SH"
    body = data["content"]
    assert "on_bar" in body, "生成的策略代码里应有 QMT 的 on_bar 入口"
    assert "FunctionTool" not in str(data) and "TypeError" not in str(data)


def test_save_qmt_strategy_writes_file(tmp_path: Path) -> None:
    """不变量 3：真的写到 `{client_path}/mpython/`，不是只回一个 dict。"""
    tool = _tools()["save_qmt_strategy"]
    res = asyncio.run(tool.run({
        "filename": "shadow_probe", "content": "# probe\n",
        "client_path": str(tmp_path),
    }))
    data = res.structured_content
    assert data["filename"] == "shadow_probe.py", "缺 .py 后缀时应自动补齐"
    target = tmp_path / "mpython" / "shadow_probe.py"
    assert target.exists(), f"应落到 {target}"
    assert target.read_text(encoding="utf-8") == "# probe\n"
    assert data["size"] == len("# probe\n")


def test_no_nested_tool_shadows_module_function() -> None:
    """不变量 4：静态护栏 —— 全仓 `register_*_tools` 内的嵌套工具函数不得与模块级同名。

    这条断言的价值在于「**扫全仓**」而不是只测这一个文件：
    同类写法只要再写一次就会静默复发，而运行期报错要等到用户真的点那个工具。
    """
    offenders = []
    targets = sorted((BACKEND / "tools").glob("*.py")) + \
        sorted((BACKEND / "mcp_server").glob("*.py"))
    assert targets, "未找到任何待扫描模块（路径变了？）"
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_funcs = {n.name for n in tree.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("register"):
                continue
            for inner in ast.walk(node):
                if inner is node or not isinstance(
                        inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not inner.decorator_list or inner.name not in module_funcs:
                    continue
                # 只有「函数体内直接按名字调用」才必然崩；仅同名但未调用不算致命
                calls_self = any(
                    isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == inner.name
                    for c in ast.walk(inner)
                )
                if calls_self:
                    offenders.append(
                        f"{path.relative_to(BACKEND).as_posix()}:{inner.lineno} "
                        f"{node.name} → {inner.name}()")
    assert not offenders, "发现同名遮蔽（装饰后局部名被重绑为 FunctionTool）：\n  " + \
        "\n  ".join(offenders)
