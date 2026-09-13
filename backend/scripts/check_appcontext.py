"""V9 Phase 5 DoD：``import state`` 收敛门禁（冻结基线，防新增）。

目标态：全仓直接 ``from core.state import state`` 收敛到 bootstrap + context
两处。存量约 45 处（engines/tools/gateway/routes shim），一次性重构风险过大，
故本门禁采用**冻结基线**语义：
- 基线文件 ``tests/contracts/appcontext_baseline.json`` 记录当前允许直接引用
  state 的模块清单（生成：``python scripts/check_appcontext.py --update``）；
- 任何**新增**直接引用（基线外的文件）→ 门禁 FAIL；
- 存量文件减少 → 允许（说明在收敛），并用 ``--update`` 收紧基线。

shim（app/state.py、app/routes/_common.py）不算新增：它们是收敛的合法中转。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
BASELINE = BACKEND / "tests" / "contracts" / "appcontext_baseline.json"

#: 收敛目标：这两处 + shim 永远合法
ALWAYS_ALLOWED = {
    "core/state.py",
    "app/bootstrap",      # 目录内全部合法（唯一装配出口）
    "core/context.py",
    "app/state.py",       # 兼容 shim
    "app/routes/_common.py",  # 路由层唯一取 state 的中转
}

_IMPORT_RE = re.compile(
    r"^\s*(?:from core\.state import .*|\bimport state\b|\bfrom core import state\b)",
    re.MULTILINE)


def scan() -> list[str]:
    """返回当前直接引用 state 的模块相对路径（含 shim 判定前的原始集合）。"""
    offenders: list[str] = []
    for py in BACKEND.rglob("*.py"):
        rel = py.relative_to(BACKEND).as_posix()
        if any(rel.startswith(p) if p.endswith("/") else rel == p
               for p in ALWAYS_ALLOWED):
            continue
        if "/." in rel or "__pycache__" in rel:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _IMPORT_RE.search(text):
            offenders.append(rel)
    return sorted(offenders)


def main() -> int:
    current = scan()
    if "--update" in sys.argv:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(current, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        print(f"[appcontext-gate] baseline updated: {len(current)} modules")
        return 0
    baseline = set()
    if BASELINE.exists():
        baseline = set(json.loads(BASELINE.read_text(encoding="utf-8")))
    new = [m for m in current if m not in baseline]
    removed = [m for m in sorted(baseline) if m not in set(current)]
    print(f"[appcontext-gate] baseline={len(baseline)} current={len(current)} "
          f"new={len(new)} converged={len(removed)}")
    for m in new:
        print(f"  NEW direct state import: {m}")
    for m in removed:
        print(f"  CONVERGED (可 --update 收紧基线): {m}")
    if new:
        print("[appcontext-gate] FAIL: 禁止新增直接 state 引用（请改用 Depends(get_ctx)）")
        return 1
    print("[appcontext-gate] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
