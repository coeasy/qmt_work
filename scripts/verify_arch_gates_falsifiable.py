"""P0-1 / P0-2 门禁可证伪性验证。

对每类违规：注入 → 断言门禁变红 → **立即字节级还原** → 全量复核 sha256。
★ 变异锚点落在**可执行代码**上（模块末尾追加语句），不是注释。
★ 影子模块用例会临时创建 ``backend/app/<内核名>.py``，用 try/finally 保证删除。

★ 常驻守卫（R26–R29 从 output/ 提升到 scripts/）：**改动门禁后必须重跑本脚本**。
  一个不会再被跑的「变异验证」只是一次性声明，不是护栏 —— 门禁失效时没人会知道。

用法：backend/runtimes/cp311/python.exe -u scripts/verify_arch_gates_falsifiable.py
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
GATE = ROOT / "scripts" / "check_execution_architecture.py"
PY = ROOT / "backend" / "runtimes" / "cp311" / "python.exe"

# Gate 2（routes 不得直连 DB）：往 route 文件末尾追加语句
ROUTE_FILE = BACKEND / "app" / "routes" / "alerts.py"
ROUTE_CASES = [
    ("G2-A ctx.db.query", '_mut_a = ctx.db.query("SELECT 1")\n', "ctx.db.query"),
    ("G2-B get_db() 访问器",
     "from core.db import get_db\n_mut_b = get_db().query(\"SELECT 1\")\n", "get_db().query"),
    ("G2-C sqlite3.connect",
     "import sqlite3\n_mut_c = sqlite3.connect(\"x.db\")\n", "sqlite3.connect"),
    ("G2-D 裸 SQL 字面量（receiver 非 db）",
     '_mut_d = _some_helper("SELECT id FROM alert_rules")\n', "裸 SQL 字面量"),
    ("G2-E 对照组：结构化 API 必须放行",
     '_mut_e = ctx.db.insert("alert_rules", {"name": "x"})\n'
     '_mut_f = ctx.db.audit("admin", "x", "", {}, "ok")\n', None),
]

# Gate 3（内核命名空间冻结）：往非 route 文件末尾追加 import
KERNEL_FILE = BACKEND / "app" / "services" / "audit_store.py"
KERNEL_CASES = [
    ("G3-F from app.db 引用", "from app.db import DB\n", "app.db"),
    ("G3-G import app.state 引用", "import app.state\n", "app.state"),
]

# Gate 3 影子模块用例：临时创建 backend/app/<name>.py
SHADOW_NAME = "quote_fields"
SHADOW_FILE = BACKEND / "app" / f"{SHADOW_NAME}.py"


def _run_gate() -> tuple[int, str]:
    p = subprocess.run([str(PY), str(GATE)], capture_output=True, text=True,
                       cwd=str(ROOT), timeout=180)
    return p.returncode, (p.stdout + p.stderr)


def _check(label: str, rc: int, out: str, expect: str | None,
           failures: list[str]) -> None:
    if expect is None:
        ok = (rc == 0)
        detail = f"rc={rc}（期望 0：结构化 API 必须放行）"
    else:
        hit = expect in out
        ok = (rc == 1 and hit)
        detail = f"rc={rc}（期望 1），命中「{expect}」={'是' if hit else '否'}"
    print(f"  {'✅' if ok else '❌'} {label}: {detail}", flush=True)
    if not ok:
        failures.append(label)
        for line in out.strip().splitlines()[:6]:
            print(f"     {line}", flush=True)


def main() -> int:
    failures: list[str] = []
    digests: dict[Path, str] = {}

    for f in (ROUTE_FILE, KERNEL_FILE):
        b = f.read_bytes()
        digests[f] = hashlib.sha256(b).hexdigest()
        print(f"锚点 {f.relative_to(ROOT)} sha256={digests[f][:16]}…", flush=True)

    # ---- Gate 2：追加到 route 文件 ----
    base = ROUTE_FILE.read_bytes()
    try:
        for label, injected, expect in ROUTE_CASES:
            ROUTE_FILE.write_bytes(base + injected.encode("utf-8"))
            rc, out = _run_gate()
            ROUTE_FILE.write_bytes(base)
            _check(label, rc, out, expect, failures)
    finally:
        ROUTE_FILE.write_bytes(base)

    # ---- Gate 3：引用 ----
    kbase = KERNEL_FILE.read_bytes()
    try:
        for label, injected, expect in KERNEL_CASES:
            KERNEL_FILE.write_bytes(kbase + injected.encode("utf-8"))
            rc, out = _run_gate()
            KERNEL_FILE.write_bytes(kbase)
            _check(label, rc, out, expect, failures)
    finally:
        KERNEL_FILE.write_bytes(kbase)

    # ---- Gate 3：影子模块 ----
    try:
        SHADOW_FILE.write_text("# 临时影子模块（可证伪性验证用）\n", encoding="utf-8")
        rc, out = _run_gate()
        _check(f"G3-H 影子模块 app/{SHADOW_NAME}.py", rc, out, "影子模块", failures)
    finally:
        if SHADOW_FILE.exists():
            SHADOW_FILE.unlink()

    # ---- 还原复核 ----
    print(flush=True)
    for f, want in digests.items():
        got = hashlib.sha256(f.read_bytes()).hexdigest()
        ok = (got == want)
        print(f"还原 {f.relative_to(ROOT)}: {'✅ 一致' if ok else '❌ 不一致！'}", flush=True)
        if not ok:
            failures.append(f"还原失败 {f.name}")
    print(f"影子模块已清理: {'✅' if not SHADOW_FILE.exists() else '❌ 仍存在'}", flush=True)
    if SHADOW_FILE.exists():
        failures.append("影子模块未清理")

    if failures:
        print(f"\n结论: ❌ 失败项 {failures}", flush=True)
        return 1
    print("\n结论: ✅ 三门禁均可证伪，且源码/现场字节级还原", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
