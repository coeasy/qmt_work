"""P0-1 / P0-2 门禁可证伪性验证。

对每类违规：注入 → 断言门禁变红 → **立即字节级还原** → 全量复核 sha256。
★ 变异锚点落在**可执行代码**上（模块末尾追加语句），不是注释。
★ 影子模块用例会临时创建 ``backend/app/<内核名>.py``，用 try/finally 保证删除。

★★ Gate 1（``place_order`` 白名单）**原先没有任何可证伪用例**（2026-10-01 补）——
   后果实测到了：大 QMT 桥落地后它的允许集不足 ⇒ 门禁**恒红**，而它接在
   ``ci.yml`` 上却没人再去看，等于把这条护栏**关掉了**。所以这里有 G1-A…G1-D
   四例，逐条证伪「目录 / 方法名 / 类名」三个放行条件**缺一即拦**。

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

# Gate 1（下单入口不得绕过 ExecutionService）：注入形态刻意**只差一个条件**，
# 从而逐条证伪白名单的精确性。
GATE1_ROUTE_FILE = BACKEND / "app" / "routes" / "alerts.py"
GATE1_PORT_FILE = BACKEND / "connectors" / "_gate1_probe.py"
GATE1_ROUTE_CASES = [
    ("G1-A 目录条件：app/ 里即使类名/方法名都对也必须拦",
     "class _FakeAdapter:\n    def place_order(self):\n"
     "        self._c.place_order(1)\n", "self._c.place_order"),
    ("G1-A2 route 里普通形态同样拦",
     "class _Buyer:\n    def buy(self):\n"
     '        self.gateway.place_order("x", "buy", "limit", 1.0, 100)\n',
     "self.gateway.place_order"),
]
GATE1_PORT_CASES = [
    ("G1-B 对照组：端口实现层的纯转发必须放行（否则门禁恒红）",
     "class _ProbeAdapter:\n    def place_order(self, code):\n"
     "        return self._c.place_order(code)\n", None),
    ("G1-C 方法名条件：改名绕开「纯转发」即拦",
     "class _ProbeAdapter:\n    def sneaky_buy(self, code):\n"
     "        return self._c.place_order(code)\n", "self._c.place_order"),
    ("G1-D 类名条件：非端口类里的 place_order 即拦",
     "class _PlainHelper:\n    def place_order(self, code):\n"
     "        return self._c.place_order(code)\n", "self._c.place_order"),
]

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

    # ---- Gate 1：下单入口（route 侧注入）----
    rbase = GATE1_ROUTE_FILE.read_bytes()
    try:
        for label, injected, expect in GATE1_ROUTE_CASES:
            GATE1_ROUTE_FILE.write_bytes(rbase + injected.encode("utf-8"))
            rc, out = _run_gate()
            GATE1_ROUTE_FILE.write_bytes(rbase)
            _check(label, rc, out, expect, failures)
    finally:
        GATE1_ROUTE_FILE.write_bytes(rbase)

    # ---- Gate 1：下单入口（端口实现层注入）----
    try:
        for label, injected, expect in GATE1_PORT_CASES:
            GATE1_PORT_FILE.write_text(injected, encoding="utf-8")
            rc, out = _run_gate()
            GATE1_PORT_FILE.unlink()
            _check(label, rc, out, expect, failures)
    finally:
        if GATE1_PORT_FILE.exists():
            GATE1_PORT_FILE.unlink()

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
    print(f"Gate1 探针已清理: {'✅' if not GATE1_PORT_FILE.exists() else '❌ 仍存在'}",
          flush=True)
    if GATE1_PORT_FILE.exists():
        failures.append("Gate1 探针未清理")

    if failures:
        print(f"\n结论: ❌ 失败项 {failures}", flush=True)
        return 1
    print("\n结论: ✅ 门禁均可证伪，且源码/现场字节级还原", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
