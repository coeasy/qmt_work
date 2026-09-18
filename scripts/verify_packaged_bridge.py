"""打包态「桥接子进程闭包 + 静态资源完整性」核验 —— 每次出包后必跑。

## 为什么需要它

打包态后端**不是在 EXE 内**跑券商 SDK，而是以
`_internal/runtimes/cp311/python.exe -m xtquant_client.bridge_server` 起一个
**普通 Python 进程**。普通进程读不到 EXE 内部的 PYZ ⇒ `xtquant_client` 及其
**首方依赖闭包**必须是 `_internal/` 下的**真实 `.py`**。

R11 真凶：`xtquant_client/xtp/quotes.py:5` 有 `from core.clock import local_now, now_iso`
（R8 时间戳统一引入的跨包依赖），而 `build_exe.py` 当时只收集了 `xtquant_client` ⇒
打包态一点「连接」就报「桥接子进程已退出」，且报错被 `_stderr_buf` **三重截断**
（每行后 200 字符 + 只留最后 8 行 + 再截 500 字符），真 `ImportError` 行被切掉。

## 判据（不依赖 QMT 是否安装）

- **静态判据（权威）**：`_internal/core/` 与 `_internal/xtquant_client/` 下必须是
  **真实 `.py`**，且 `core/clock.py` 在位。缺一个就必然握手失败。
- **动态判据（加分）**：用包内解释器按真实方式（`cwd=_internal` + `PYTHONPATH=_internal`）
  真跑一次 import 闭包。若失败但 stderr 里**没有** `No module named 'core'` /
  `'xtquant_client'`，说明是别的原因（如未装 QMT），降级为 WARN 而不是 FAIL ——
  这条脚本要能区分「打包漏收」与「机器没装券商客户端」。

用法：
    backend/runtimes/cp311/python.exe scripts/verify_packaged_bridge.py
    backend/runtimes/cp311/python.exe scripts/verify_packaged_bridge.py --pkg <包内 resources/backend/qmt_work>
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PKG = (ROOT / "frontend-next" / "dist-electron" / "win-unpacked"
               / "resources" / "backend" / "qmt_work")
SRC_STATIC = ROOT / "backend" / "static"

# 桥接子进程会 import 的首方包 —— 与 backend/build_exe.py 的收集清单必须一致
CLOSURE_PKGS = ("core", "xtquant_client")
# 闭包里「最靠前、最先炸」的那个模块：xtp → adapter → quotes → core.clock
SMOKE_IMPORTS = "import xtquant_client.xtp, xtquant_client.bridge_server, core.clock"
MISSING_MOD_RE = re.compile(r"No module named '([^']+)'")


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, name: str, detail: str) -> None:
        self.rows.append((level, name, detail))

    def ok(self, name: str, detail: str = "") -> None:
        self.add("PASS", name, detail)

    def warn(self, name: str, detail: str = "") -> None:
        self.add("WARN", name, detail)

    def fail(self, name: str, detail: str = "") -> None:
        self.add("FAIL", name, detail)

    def render(self) -> int:
        width = max((len(n) for _, n, _ in self.rows), default=10)
        for level, name, detail in self.rows:
            print(f"  [{level}] {name.ljust(width)}  {detail}")
        failed = [r for r in self.rows if r[0] == "FAIL"]
        warned = [r for r in self.rows if r[0] == "WARN"]
        print()
        print(f"  合计 {len(self.rows)} 项："
              f"{len(self.rows) - len(failed) - len(warned)} PASS / "
              f"{len(warned)} WARN / {len(failed)} FAIL")
        return 1 if failed else 0


def real_py_files(d: Path) -> int:
    """只数真实 .py —— `.pyc` 不算：桥接子进程是普通 Python 进程，读不到 PYZ，
    但 .pyc 是可以被 import 的……前提是它有对应的源码目录结构与有效 magic。
    实测打包态若只留 .pyc 会因 `__pycache__` 布局不匹配而失败，故只认 .py。"""
    if not d.is_dir():
        return 0
    return sum(1 for p in d.rglob("*.py") if p.is_file())


def check_static(rep: Report, pkg: Path) -> None:
    pkg_static = pkg / "_internal" / "static"
    if not pkg_static.is_dir():
        rep.fail("包内 static 目录", f"不存在：{pkg_static}")
        return
    n_pkg = sum(1 for p in pkg_static.rglob("*") if p.is_file())
    n_src = sum(1 for p in SRC_STATIC.rglob("*") if p.is_file()) if SRC_STATIC.is_dir() else 0
    if n_src == 0:
        rep.warn("包内 static 文件数", f"{n_pkg}（源目录为空，无法比对）")
    elif n_pkg == n_src:
        rep.ok("包内 static 文件数", f"{n_pkg} / 源 {n_src} 一致")
    else:
        rep.fail("包内 static 文件数", f"{n_pkg} != 源 {n_src}（白屏的直接原因）")

    idx = pkg_static / "index.html"
    if not idx.is_file():
        rep.fail("包内 index.html", "缺失")
    else:
        m = re.findall(r"/assets/([A-Za-z0-9_.-]+\.js)", idx.read_text(encoding="utf-8", errors="replace"))
        entry = [x for x in m if x.startswith("index-")]
        if not entry:
            rep.warn("入口分片 hash", f"未在 index.html 里找到 index-*.js（找到 {len(m)} 个引用）")
        else:
            hit = pkg_static / "assets" / entry[0]
            (rep.ok if hit.is_file() else rep.fail)(
                "入口分片在包内", entry[0] if hit.is_file() else f"{entry[0]} 不在 assets/ 下")
    (rep.ok if (pkg_static / "theme-boot.js").is_file() else rep.fail)(
        "theme-boot.js 在包内", "CSP 主题预热文件，缺了会白屏")


def check_closure(rep: Report, pkg: Path) -> None:
    internal = pkg / "_internal"
    for name in CLOSURE_PKGS:
        n = real_py_files(internal / name)
        if n == 0:
            rep.fail(f"闭包 {name}/ 真实 .py", f"0 个（{internal / name}）—— 桥接子进程必然握手失败")
        else:
            rep.ok(f"闭包 {name}/ 真实 .py", f"{n} 个")
    clock = internal / "core" / "clock.py"
    (rep.ok if clock.is_file() else rep.fail)(
        "core/clock.py 在位",
        "R11 真凶：xtp/quotes.py 依赖它" if clock.is_file() else "缺失 ⇒ ImportError ⇒ 桥接子进程退出")


def check_runtime(rep: Report, pkg: Path) -> Path | None:
    py = pkg / "_internal" / "runtimes" / "cp311" / "python.exe"
    if not py.is_file():
        rep.fail("随包 cp311 解释器", f"缺失：{py} ⇒ 桥接子进程将回退系统解释器（ABI 可能不匹配）")
        return None
    rep.ok("随包 cp311 解释器", str(py.relative_to(pkg)))
    return py


def check_import(rep: Report, pkg: Path, py: Path) -> None:
    internal = pkg / "_internal"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = str(internal)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([str(py), "-c", SMOKE_IMPORTS], cwd=str(internal), env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        rep.warn("桥接 import 冒烟", "120s 超时（可能是 SDK 在等 QMT 登录，非闭包问题）")
        return
    if proc.returncode == 0:
        rep.ok("桥接 import 冒烟", "xtquant_client.xtp + bridge_server + core.clock 全部可导入")
        return
    missing = set(MISSING_MOD_RE.findall(proc.stderr or ""))
    if missing & set(CLOSURE_PKGS):
        rep.fail("桥接 import 冒烟", f"缺首方模块 {sorted(missing & set(CLOSURE_PKGS))} ⇒ 打包漏收")
    elif missing:
        rep.warn("桥接 import 冒烟", f"缺第三方模块 {sorted(missing)}（多为未装 QMT，非打包问题）")
    else:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        rep.warn("桥接 import 冒烟", f"rc={proc.returncode}，非缺模块错误：{' | '.join(tail)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="打包态桥接闭包与静态资源核验")
    ap.add_argument("--pkg", default=str(DEFAULT_PKG),
                    help="包内 resources/backend/qmt_work 目录")
    args = ap.parse_args()
    pkg = Path(args.pkg).resolve()

    print(f"=== 打包态核验 (pkg={pkg}) ===")
    if not pkg.is_dir():
        print(f"  [FAIL] 包目录不存在：{pkg}\n  先跑 bash build_all.sh --nsis")
        return 1
    exe = pkg / "qmt_work.exe"
    print(f"  后端 EXE: {'OK ' if exe.is_file() else 'MISS'} "
          f"{exe.stat().st_size if exe.is_file() else 0} B")
    print()

    rep = Report()
    check_static(rep, pkg)
    check_closure(rep, pkg)
    py = check_runtime(rep, pkg)
    if py is not None:
        check_import(rep, pkg, py)
    return rep.render()


if __name__ == "__main__":
    sys.exit(main())
