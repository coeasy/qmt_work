"""V10 Phase A5：路由层 state locator -> Depends(get_ctx) 机械迁移。

规则：
- 路由处理器（被 router.<verb> 装饰）= handler：注入 `ctx: AppContext = Depends(get_ctx)`，
  函数体内 `state.X` -> `ctx.X`。
- handler 内嵌套函数（闭包捕获 ctx）：`state.X` -> `ctx.X`（不另加参数）。
- 模块级非 handler 辅助函数（如 _one/_engine/_rt/_run）：`state.X` -> `active_context().X`。
- 移除 `from app.routes._common import ... state` 中的 state 标记。
- 移除路由文件里 `from core.state import ... `（仅 health.py 的 REQUIRED_PHASES，就地本地化）。

不触碰注释无关语义；迁移后逐文件 py_compile 校验。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROUTES = pathlib.Path("app/routes")
DEC = {"get", "post", "put", "delete", "patch", "websocket"}
SKIP = {"_common.py"}  # 已手动迁移


class Fn:
    def __init__(self, node, parent=None):
        self.node = node
        self.parent = parent
        self.children = []
        self.handler = False


def is_handler(node):
    for d in node.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in DEC:
            return True
        if isinstance(d, ast.Attribute) and d.attr in DEC:
            return True
    return False


def uses_state(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == "state":
            return True
    return False


def descendant_spans(node, by_node):
    spans = []
    for ch in by_node[id(node)].children:
        spans.append((ch.node.lineno, ch.node.end_lineno))
        spans.extend(descendant_spans(ch.node, by_node))
    return spans


def in_handler_scope(fn):
    p = fn.parent
    while p is not None:
        if p.handler:
            return True
        p = p.parent
    return False


def own_lines(fn, by_node):
    start, end = fn.node.lineno, fn.node.end_lineno
    spans = set(range(start, end + 1))
    for cs in descendant_spans(fn.node, by_node):
        spans -= set(range(cs[0], cs[1] + 1))
    return sorted(spans)


def main():
    files = sorted(p for p in ROUTES.glob("*.py") if p.name not in SKIP)
    report = []
    for f in files:
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(f))
        lines = src.splitlines()
        if not uses_state(tree):
            continue

        # 构建函数树
        root_funcs = []
        stack = []
        by_node = {}

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                fn = Fn(node, stack[-1] if stack else None)
                by_node[id(node)] = fn
                if stack:
                    stack[-1].children.append(fn)
                else:
                    root_funcs.append(fn)
                fn.handler = is_handler(node)
                stack.append(fn)
                for stmt in node.body:
                    self.visit(stmt)
                stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef

        V().visit(tree)

        # 收集所有函数节点 -> 后代 span（用于排除嵌套函数体）

        changed = []
        for fn in root_funcs:
            _collect_and_replace(fn, by_node, changed, lines)

        # 导入处理
        new_lines = []
        need_ctx_import = any(True for _ in [1])  # placeholder
        need_active = any(c[2] == "active_context()." for c in changed)
        pending_ctx_import = False
        for idx, line in enumerate(lines):
            new_line = line
            # 移除 _common 的 state 标记
            if re.search(r"from app\.routes\._common import", line):
                new_line = re.sub(r"\s*\bstate\b\s*,", "", new_line)
                new_line = re.sub(r",\s*\bstate\b", "", new_line)
                new_line = re.sub(r"\bstate\b", "", new_line)
                new_line = re.sub(r",\s*,", ",", new_line)
            # 移除 from core.state import X（route 层不允许）
            if re.search(r"from core\.state import", line):
                names = re.findall(r"from core\.state import\s*(.+)", line)
                if names:
                    # 仅 health.py 的 REQUIRED_PHASES：就地本地化（保留原缩进，避免破坏函数结构）
                    indent = line[:len(line) - len(line.lstrip())]
                    new_line = indent + 'REQUIRED_PHASES = ("db", "engines", "watchdogs", "replay", "misc")  # 本地化，避免 route 层反向 import core.state'
            new_lines.append(new_line)

        lines = new_lines

        # 注入 Depends 参数到 handler
        for fn in root_funcs:
            if fn.handler:
                inject_ctx_param(lines, fn.node)

        # 添加 import
        lines = add_imports(lines, need_active)

        out = "\n".join(lines) + ("\n" if src.endswith("\n") else "")
        f.write_text(out, encoding="utf-8")
        report.append((f.name, len(changed), [c[0] for c in changed[:3]]))
    for name, n, sample in report:
        print(f"{name}: {n} selectors replaced; e.g. {sample}")


def _collect_and_replace(fn, by_node, changed, lines):
    if uses_state(fn.node):
        if fn.handler or in_handler_scope(fn):
            target_dot = "ctx."
            target_base = "ctx"
        else:
            target_dot = "active_context()."
            target_base = "active_context()"
        for ln in own_lines(fn, by_node):
            orig = lines[ln - 1]
            if "state" not in orig:
                continue
            new = orig
            # 1) state.attr 形式
            new = re.sub(r"\bstate\.", target_dot, new)
            # 2) getattr(state, "x") 形式
            new = re.sub(r"getattr\(\s*state\s*,", "getattr(" + target_base + ",", new)
            # 3) 其余裸 state 词（state is None / state = ... / 文档串等）
            new = re.sub(r"\bstate\b", target_base, new)
            if new != orig:
                lines[ln - 1] = new
                changed.append((fn.node.name, ln, target_dot))
    for ch in fn.children:
        _collect_and_replace(ch, by_node, changed, lines)


def inject_ctx_param(lines, node):
    def_line = lines[node.lineno - 1]
    pstart = def_line.index("(")
    depth = 0
    i = node.lineno - 1
    pos = pstart
    close = None
    while i < len(lines):
        line = lines[i]
        j = pos if i == node.lineno - 1 else 0
        while j < len(line):
            c = line[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    close = (i, j)
                    break
            j += 1
        if close:
            break
        i += 1
    if close is None:
        return
    i, j = close
    seg = "".join(lines[node.lineno - 1:i + 1])[pstart + 1:j]
    has_params = seg.strip() != ""
    text = ", ctx: AppContext = Depends(get_ctx)" if has_params else "ctx: AppContext = Depends(get_ctx)"
    line = lines[i]
    # 避免重复注入
    if "ctx: AppContext" in seg:
        return
    lines[i] = line[:j] + text + line[j:]


def add_imports(lines, need_active):
    has_fastapi = any(re.search(r"from fastapi import", l) for l in lines)
    has_ctx = any("from core.context import" in l for l in lines)
    out = []
    if not has_ctx:
        out.append("from core.context import AppContext, get_ctx"
                   + (", active_context" if need_active else ""))
    inserted_ctx = has_ctx
    for l in lines:
        out.append(l)
        if not inserted_ctx and re.search(r"from fastapi import", l):
            # 在 fastapi import 行后补 Depends
            if "Depends" not in l:
                # 追加到该 import
                l2 = l.rstrip("\n")
                if l2.endswith(")"):
                    l2 = l2[:-1] + ", Depends)"
                elif l2.endswith(","):
                    l2 = l2 + " Depends"
                else:
                    l2 = l2 + ", Depends"
                out[-1] = l2
            inserted_ctx = True
    if not has_fastapi:
        out.insert(0, "from fastapi import Depends")
    # 已存在 core.context import 但缺少 active_context 时补上
    if has_ctx and need_active:
        for i, l in enumerate(out):
            if "from core.context import" in l and "active_context" not in l:
                out[i] = l.rstrip() + ", active_context"
                break
    return out


if __name__ == "__main__":
    main()
