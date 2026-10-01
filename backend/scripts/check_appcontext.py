"""V9 Phase 5 DoD：``import state`` 收敛门禁（冻结基线，防新增）。

目标态：全仓直接 ``from core.state import state`` 收敛到 bootstrap + context
两处。存量约 45 处（engines/tools/gateway/routes shim），一次性重构风险过大，
故本门禁采用**冻结基线**语义：
- 基线文件 ``tests/contracts/appcontext_baseline.json`` 记录当前允许直接引用
  state 的模块清单（生成：``python scripts/check_appcontext.py --update``）；
- 任何**新增**直接引用（基线外的文件）→ 门禁 FAIL；
- 存量文件减少 → 允许（说明在收敛），并用 ``--update`` 收紧基线。

shim（app/state.py、app/routes/_common.py）不算新增：它们是收敛的合法中转。

★ 匹配精度（2026-10-01 修）—— 为什么从正则改成 AST
  旧实现用 ``from core.state import .*`` 抓「直接引用 state」，把**任何**从
  ``core.state`` 取东西都算违规，于是 ``from core.state import AppState``（类型注解）
  和 ``from core.state import MSG_NO_BROKER``（消息常量）一起被点名 —— 实测
  ``tests/test_trade_chain_contract.py`` 只因取一个常量就被列进违规清单，
  ``tests/test_lifecycle*.py`` 两条基线条目也是这么来的。
  过宽匹配有两重害：①把无关文件逼进基线，基线里于是混着「其实没碰单例」的条目，
  谎话化；②真出现**单例**漂移时反而不显眼。故按本仓既有标准改为 **AST 判定**，
  只认「取到可变单例 ``state`` 这个名字」的三种形态（含 ``as`` 别名、含多名字列表）。

★ 扫描范围（含 ``tests/``，这是**刻意**的）
  目标态写的是「全仓收敛」，而测试确实需要拿到单例本身（``tests/conftest.py``
  要快照/还原它的全部槽位，否则用例间互相污染）。因此测试文件也在扫描范围内，
  新增一条就要显式 ``--update`` 接纳一次 —— 这正是「新增必须是有意识决定」的本意。
"""
from __future__ import annotations

import ast
import json
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

def _imports_state_singleton(py: Path) -> bool:
    """AST 判定：本文件是否**取用了可变单例 ``state``**。

    只认三种形态（见模块 docstring 的精度说明），其余（``AppState`` /
    ``MSG_NO_BROKER`` / 其它 ``core.state`` 成员）一律不算：

    - ``from core.state import state``（可带 ``as`` 别名、可夹在多名字列表里）；
    - ``from core import state``；
    - ``import state``。
    """
    try:
        tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in ("core.state", "core"):
                if any(a.name == "state" for a in node.names):
                    return True
        elif isinstance(node, ast.Import):
            if any(a.name == "state" for a in node.names):
                return True
    return False


def scan() -> list[str]:
    """返回当前直接引用 state 单例的模块相对路径（含 shim 判定前的原始集合）。"""
    offenders: list[str] = []
    for py in BACKEND.rglob("*.py"):
        rel = py.relative_to(BACKEND).as_posix()
        if any(rel.startswith(p) if p.endswith("/") else rel == p
               for p in ALWAYS_ALLOWED):
            continue
        if "/." in rel or "__pycache__" in rel:
            continue
        if _imports_state_singleton(py):
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
