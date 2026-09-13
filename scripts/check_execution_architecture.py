#!/usr/bin/env python3
"""Static gate: real trading entry points must not bypass ExecutionService.

Phase 0 ④（P0-10 重写）：
- AST 全量扫描 ``app / gateway / tools / engines / mcp_server / connectors``；
- **白名单语义**：任何 ``*.place_order(...)`` 直调均视为绕过执行链，除非 receiver
  本身就是执行服务（receiver 文本含 "execution"，如 ``ExecutionService(...)`` /
  ``self._execution`` / ``get_execution_service()``）；
- 旧启发式（receiver ∈ {gateway, adapter} 或以 .gateway 结尾）会漏掉
  ``self._adapter.place_order`` 等形态，已废弃。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SCAN_ROOTS = tuple(BACKEND / d for d in
                   ("app", "gateway", "tools", "engines", "mcp_server", "connectors"))
SERVICE = BACKEND / "gateway" / "execution.py"


def _receiver_allowed(receiver: str) -> bool:
    """仅执行服务自身（含 execution 命名）允许调用 place_order。"""
    low = receiver.lower()
    return "execution" in low


def main() -> int:
    if "class ExecutionService" not in SERVICE.read_text(encoding="utf-8"):
        print("ExecutionService is missing", file=sys.stderr)
        return 1
    violations: list[str] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path == SERVICE:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                print(f"cannot parse {path}: {exc}", file=sys.stderr)
                return 1
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "place_order":
                    continue
                receiver = ast.unparse(node.func.value)
                if not _receiver_allowed(receiver):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: {receiver}.place_order")
    if violations:
        print("direct real order entry detected; route through ExecutionService:",
              file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("execution architecture gate: OK (AST full scan, whitelist semantics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
