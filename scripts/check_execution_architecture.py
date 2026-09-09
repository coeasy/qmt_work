#!/usr/bin/env python3
"""Static gate: real trading entry points must not bypass ExecutionService."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (ROOT / "backend" / "app", ROOT / "backend" / "gateway",
              ROOT / "backend" / "tools", ROOT / "backend" / "mcp_server")


def main() -> int:
    service = ROOT / "backend" / "gateway" / "execution.py"
    if "class ExecutionService" not in service.read_text(encoding="utf-8"):
        print("ExecutionService is missing", file=sys.stderr)
        return 1
    violations: list[str] = []
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            if path == service:
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
                if receiver in {"gateway", "adapter"} or receiver.endswith(".gateway"):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {receiver}.place_order")
    if violations:
        print("direct real order entry detected; route through ExecutionService:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("execution architecture gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
