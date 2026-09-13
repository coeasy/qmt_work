#!/usr/bin/env python
"""Tier-0 契约基线生成器（Phase 1 · 先锁后改）。

从真实代码内省契约面，落盘到 ``backend/tests/contracts/*.json``：
    rest_endpoints.json / mcp_tools.json / qmt_contracts.json /
    screen_contract.json / ws_events.json

用法：
    python scripts/gen_contracts.py           # 生成/刷新全部快照
    python scripts/gen_contracts.py --diff    # 仅打印与现网的差异，不写盘

语义（与 test_contracts_baseline.py 配套）：
- 基线 ⊆ 现网：删除/改名任何已锁定契约 → CI 红灯；
- 新增契约不阻塞，但应运行本脚本刷新基线后一并提交（变更可见、可审）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))

from contracts.introspect import collect_all  # noqa: E402

_CONTRACT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "tests", "contracts")
_FILES = ("rest_endpoints", "mcp_tools", "qmt_contracts", "screen_contract", "ws_events")


def main() -> int:
    os.makedirs(_CONTRACT_DIR, exist_ok=True)
    data = collect_all()
    changed = 0
    for name in _FILES:
        path = os.path.join(_CONTRACT_DIR, f"{name}.json")
        new = data[name]
        old = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        if old == new:
            print(f"[contracts] {name}: unchanged ({_size(new)})")
            continue
        changed += 1
        if "--diff" in sys.argv:
            _print_diff(name, old, new)
            continue
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(new, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"[contracts] {name}: WRITTEN ({_size(new)})")
    if "--diff" not in sys.argv:
        with open(os.path.join(_CONTRACT_DIR, "_meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"),
                       "policy_version": "chain.v1"}, fh, ensure_ascii=False, indent=2)
    print(f"[contracts] done: {changed} file(s) "
          f"{'inspected (diff mode)' if '--diff' in sys.argv else 'written/updated'}")
    return 0


def _size(v) -> str:
    if isinstance(v, list):
        return f"{len(v)} items"
    if isinstance(v, dict):
        return f"{len(v)} keys"
    return type(v).__name__


def _print_diff(name: str, old, new) -> None:
    def keys(v):
        return set(v) if isinstance(v, dict) else set(v or [])

    if old is None:
        print(f"[contracts] {name}: NEW ({_size(new)})")
        return
    removed = sorted(keys(old) - keys(new))
    added = sorted(keys(new) - keys(old))
    print(f"[contracts] {name}: removed={removed or '[]'} added={added or '[]'}")


if __name__ == "__main__":
    sys.exit(main())
