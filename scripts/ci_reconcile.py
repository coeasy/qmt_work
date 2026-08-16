#!/usr/bin/env python3
"""CI 数字核对：后端测试数 / 前端组件数 必须与方案文档（docs/功能梳理与优化改进方案（现状版）.md）一致。

- 后端：pytest 收集到的 `def test_` 数量（排除 smoke2 与 fake_bridge_server 辅助模块）。
- 前端：frontend/src/components/*.jsx 文件数量。

任一项不符即退出非 0，阻断 PR，并提示是“改了代码忘了更文档”还是“文档数字过时”。
修改测试/组件后，请同步更新本文件的期望值与文档。
"""
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")

EXPECTED_TESTS = 192
EXPECTED_COMPONENTS = 32


def collect_backend_tests() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=BACKEND, capture_output=True, text=True,
    )
    if out.returncode != 0:
        # 收集阶段本身出错（如导入失败），打印细节便于排查
        sys.stderr.write(out.stdout + out.stderr)
        raise RuntimeError("pytest collect-only failed")
    return sum(1 for line in out.stdout.splitlines() if "::" in line)


def count_frontend_components() -> int:
    files = glob.glob(os.path.join(FRONTEND, "src", "components", "*.jsx"))
    # 排除 Storybook / 非页面片段（如 *.stories.jsx 已不在此目录）
    return len(files)


def main() -> int:
    ok = True

    tests = collect_backend_tests()
    print(f"[backend] collected tests = {tests} (expected {EXPECTED_TESTS})")
    if tests != EXPECTED_TESTS:
        print("  ✗ 测试数与文档不一致：请同步 EXPECTED_TESTS 或补齐/删减测试")
        ok = False

    components = count_frontend_components()
    print(f"[frontend] components = {components} (expected {EXPECTED_COMPONENTS})")
    if components != EXPECTED_COMPONENTS:
        print("  ✗ 组件数与文档不一致：请同步 EXPECTED_COMPONENTS 或 components 目录")
        ok = False

    if not ok:
        print("RECONCILE FAILED")
        return 1
    print("RECONCILE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
