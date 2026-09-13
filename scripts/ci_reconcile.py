#!/usr/bin/env python3
"""CI 数字核对：后端测试收集数 / 前端组件数 / 注册页数量必须与 README 声明一致。

三项计数契约（不是通过率旁路）：
  1. EXPECTED_TESTS      —— pytest 逐文件 --collect-only 收集到的用例总数。
  2. EXPECTED_COMPONENTS —— frontend/src/{components,features} 下全部 .jsx。
  3. EXPECTED_PAGES      —— pagesRegistry.jsx 的 PAGES 键数量（= 前端「页」的真源）。

任一项不符即退出非 0，提示是「改了代码忘了更文档」还是「文档数字过时」。
用法：
    python scripts/ci_reconcile.py            # 校验，不符即失败
    python scripts/ci_reconcile.py --update   # 用实测值回写下方常量

为什么逐文件收集：pytest 在同进程内一次性收集全部 86 个测试文件会在 Windows
上偶发崩溃/导入污染（项目已知问题，见 .workbuddy/memory/MEMORY.md §5）。
因此每文件起独立子进程收集，再汇总。
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")
SRC = os.path.join(FRONTEND, "src")
PAGES_REGISTRY = os.path.join(SRC, "pagesRegistry.jsx")

# ── 计数契约（与 README「核心能力」「项目结构」章节同步）─────────────────────
EXPECTED_TESTS = 778          # 后端用例收集数
EXPECTED_COMPONENTS = 70     # 前端 .jsx 组件数（components + features）
EXPECTED_PAGES = 17          # 注册页数量

# ── 收集失败时允许跳过的辅助模块（非测试）────────────────────────────────────
_SKIP_FILES = {"smoke2.py", "fake_bridge_server.py", "_phase4_support.py",
               "_phase7_support.py", "_verify_phase34.py", "_verify_phase4.py"}


def _pytest_available() -> bool:
    probe = subprocess.run(
        [sys.executable, "-c", "import pytest, sys; print(pytest.__version__)"],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        sys.stderr.write(
            "✗ 当前解释器无 pytest，无法核对测试收集数。\n"
            f"  解释器: {sys.executable}\n"
            "  请使用项目托管 venv 运行本脚本，例如：\n"
            "    python backend/pyproject.toml 约束 Python >=3.11,<3.12\n"
            "    pip install -r backend/requirements.txt && pip install pytest\n"
        )
        return False
    return True


def collect_backend_tests() -> int:
    """逐文件 collect-only 汇总。每个文件独立子进程，规避同进程收集崩溃。"""
    files = sorted(
        f for f in glob.glob(os.path.join(BACKEND, "tests", "test_*.py"))
        if os.path.basename(f) not in _SKIP_FILES
    )
    if not files:
        raise RuntimeError(f"未找到任何测试文件：{BACKEND}/tests/test_*.py")

    total = 0
    failed = []
    for f in files:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", f, "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=BACKEND, capture_output=True, text=True, check=False,
        )
        combined = (out.stdout or "") + "\n" + (out.stderr or "")
        # 优先解析汇总行「N tests? collected」（含参数化展开的真实计数），
        # 回退到逐行 '::' 计数。
        m = re.search(r"(\d+) tests? collected", combined)
        if m:
            total += int(m.group(1))
            continue
        if out.returncode == 0:
            total += sum(1 for line in out.stdout.splitlines() if "::" in line)
            continue
        failed.append(os.path.basename(f))
        tail = "\n".join(combined.strip().splitlines()[-6:])
        sys.stderr.write(f"  ✗ collect 失败 {os.path.basename(f)}:\n{tail}\n")
        if any(k in combined for k in ("ModuleNotFoundError", "ImportError")):
            sys.stderr.write(
                "    → 依赖未装：请使用后端托管 venv 运行，先 "
                "`pip install -r backend/requirements.txt`。\n"
            )

    if failed:
        raise RuntimeError(f"{len(failed)}/{len(files)} 个测试文件 collect 失败：{failed}")
    return total


def count_frontend_components() -> int:
    """src/components 与 src/features 下全部 .jsx（递归）。

    V10 重构后页面从 components/ 迁到 features/{market,research,trading,accounts,system}，
    只看 components/ 会漏计（曾因此出现 EXPECTED=48 而实际只有 36 的假漂移）。
    glob 天然排除 *.jsx.dead 等已移出构建路径的文件。
    注意：Python 的 glob 不支持 bash 式的 {a,b} 花括号展开，须逐目录列。
    """
    n = 0
    for d in ("components", "features"):
        n += len(glob.glob(os.path.join(FRONTEND, "src", d, "**", "*.jsx"),
                           recursive=True))
    return n


def count_pages() -> int:
    """pagesRegistry.jsx 的 PAGES 键数量 —— 前端「页」的唯一真源。"""
    if not os.path.isfile(PAGES_REGISTRY):
        raise RuntimeError(f"缺少页面注册表：{PAGES_REGISTRY}")
    src = open(PAGES_REGISTRY, encoding="utf-8").read()
    m = re.search(r"\bPAGES\s*=\s*\{", src)
    if not m:
        raise RuntimeError("pagesRegistry.jsx 中未找到 PAGES = {")
    # 从 PAGES = { 起做括号配对，取到对象体
    depth = 0
    start = src.index("{", m.end() - 1)
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                body = src[start + 1:i]
                break
    else:
        raise RuntimeError("PAGES 对象括号不配对")
    # 只统计顶层「key: {" 形式的条目：缩进为对象首层
    return len(re.findall(r"(?m)^\s{2,6}([a-z][a-z0-9_]*)\s*:\s*\{", body))


def _self_update(tests: int, components: int, pages: int) -> None:
    """把实测值回写本文件的期望常量。"""
    path = os.path.abspath(__file__)
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"EXPECTED_TESTS\s*=\s*\d+", f"EXPECTED_TESTS = {tests}", text)
    text = re.sub(r"EXPECTED_COMPONENTS\s*=\s*\d+",
                  f"EXPECTED_COMPONENTS = {components}", text)
    text = re.sub(r"EXPECTED_PAGES\s*=\s*\d+", f"EXPECTED_PAGES = {pages}", text)
    open(path, "w", encoding="utf-8", newline="\n").write(text)
    print(f"已回写 {path}")
    print(f"  EXPECTED_TESTS      = {tests}")
    print(f"  EXPECTED_COMPONENTS = {components}")
    print(f"  EXPECTED_PAGES      = {pages}")


def main() -> int:
    parser = argparse.ArgumentParser(description="qmt_work 文档/代码计数核对")
    parser.add_argument("--update", action="store_true",
                        help="用实测值回写本文件期望常量后退出")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    tests = components = pages = 0
    if _pytest_available():
        tests = collect_backend_tests()
    components = count_frontend_components()
    pages = count_pages()

    if args.update:
        _self_update(tests, components, pages)
        return 0

    checks = [
        ("backend tests", tests, EXPECTED_TESTS, "改动了测试文件或用例，请同步 README 与 EXPECTED_TESTS"),
        ("frontend components", components, EXPECTED_COMPONENTS,
         "增删了 .jsx 组件，请同步 README 与 EXPECTED_COMPONENTS"),
        ("registered pages", pages, EXPECTED_PAGES,
         "页面注册表有增删，请同步 README 与 EXPECTED_PAGES"),
    ]
    ok = True
    for name, actual, expected, hint in checks:
        flag = "✓" if actual == expected else "✗"
        print(f"[{flag}] {name} = {actual} (expected {expected})")
        if actual != expected:
            ok = False
            print(f"     → {hint}；执行 `python scripts/ci_reconcile.py --update` 可回写期望值")

    if not ok:
        print("RECONCILE FAILED")
        return 1
    print("RECONCILE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
