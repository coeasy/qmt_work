#!/usr/bin/env python3
"""CI 数字核对：后端测试收集数 / 前端组件数 / 注册页数量 / API 契约必须自洽。

四项契约（不是通过率旁路）：
  1. EXPECTED_TESTS      —— pytest 逐文件 --collect-only 收集到的用例总数。
  2. EXPECTED_COMPONENTS —— frontend-next/src/{components,shell,charts,domains} 下全部 .tsx。
  3. EXPECTED_PAGES      —— app/routes.tsx 的 PAGES 键数量（= 前端「页」的真源）。
  4. API 契约            —— 前端 services/api 声明的端点必须都存在于后端注册表（P1-5）。

  注：旧 frontend/ 已退役；前端契约全部改指 frontend-next/（见 §12.4 / 退役改造）。

★ 第 4 项为什么放在这里而不是另起一个 hook：它和前三项是**同一类**问题 ——
  「两处对同一事实的声明已经不一致，而没有任何编译期错误会提醒你」。三项核的是
  文档里的数字，第四项核的是前端代码里的路径。分开放只会让「跑门禁」变成
  「记得跑两个门禁」，而漏跑的那个永远不会有人发现。

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
FRONTEND = os.path.join(ROOT, "frontend-next")
SRC = os.path.join(FRONTEND, "src")
PAGES_REGISTRY = os.path.join(SRC, "app", "routes.tsx")
README = os.path.join(ROOT, "README.md")
GITATTRIBUTES = os.path.join(ROOT, ".gitattributes")

# ── 计数契约（与 README「核心能力」「项目结构」章节同步）─────────────────────
EXPECTED_TESTS = 1696          # 后端用例收集数
EXPECTED_COMPONENTS = 72     # 前端 .tsx 组件数（components + shell + charts + domains）
EXPECTED_PAGES = 43          # 注册页数量（routes.tsx PAGES 键；含占位）

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
    """src/{components,shell,charts,domains} 下全部 .tsx（递归，排除 tests）。

    frontend-next 无 features/ 目录：可复用 UI 在 components/shell/charts，
    页面级（域）组件在 domains/。glob 天然排除 *.tsx.dead 等已移出构建路径的文件。
    注意：Python 的 glob 不支持 bash 式的 {a,b} 花括号展开，须逐目录列。
    """
    n = 0
    for d in ("components", "shell", "charts", "domains"):
        n += len(glob.glob(os.path.join(FRONTEND, "src", d, "**", "*.tsx"),
                           recursive=True))
    return n


def count_pages() -> int:
    """pagesRegistry.jsx 的 PAGES 键数量 —— 前端「页」的唯一真源。"""
    if not os.path.isfile(PAGES_REGISTRY):
        raise RuntimeError(f"缺少页面注册表：{PAGES_REGISTRY}")
    src = open(PAGES_REGISTRY, encoding="utf-8").read()
    # 兼容两种声明：旧 `PAGES = {` 与 frontend-next 的 `PAGES: Record<...> = {`
    m = re.search(r"PAGES\b[^\{]*=\s*\{", src)
    if not m:
        raise RuntimeError(f"{os.path.basename(PAGES_REGISTRY)} 中未找到 PAGES = {{")
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


def check_readme_numbers(tests: int, pages: int) -> list[str]:
    """README 里的**计数声明**必须与实测一致 —— README 漂移门禁。

    ★ 为什么必须自动核：README 曾长期声称「37 个页面入口（36 已实现，1 占位：分仓再平衡）」，
    而实际早已是 38 个且全部 ``status: "done"``；测试文件数与用例数同样停在很早以前。
    根因是门禁只核 ``EXPECTED_*`` 常量，**没人核 README 本身** —— 于是文档可以无限期地
    说错话。文档说谎比没有文档更糟：读者会照着它做判断。

    只在 README 里**确实写了**该数字时才比对（缺了不算错，避免门禁变成格式检查）。
    """
    if not os.path.isfile(README):
        return [f"缺少 README：{README}"]
    text = open(README, encoding="utf-8").read()
    problems: list[str] = []

    m = re.search(r"(\d+)\s*个页面", text)
    if m and int(m.group(1)) != pages:
        problems.append(f"README 称「{m.group(1)} 个页面」，实测 {pages} 个")

    m = re.search(r"(\d+)\s*个\s*`test_\*\.py`\s*[，,]\s*(\d+)\s*个用例", text)
    if m:
        n_files = len(glob.glob(os.path.join(BACKEND, "tests", "test_*.py")))
        if int(m.group(1)) != n_files:
            problems.append(f"README 称「{m.group(1)} 个 test_*.py」，实测 {n_files} 个")
        # pytest 不可用时 tests 为 0，跳过（否则会误报）
        if tests and int(m.group(2)) != tests:
            problems.append(f"README 称「{m.group(2)} 个用例」，实测收集 {tests} 个")

    # 路由模块数：README 长期写「36 个路由模块」而实测 34 —— 与页面数同源的漂移。
    # 判据用「文件里真有 @router. 装饰器」，_common.py 之类辅助模块不该被算进去。
    m = re.search(r"(\d+)\s*个\s*(?:REST\s*)?路由模块", text)
    if m:
        route_dir = os.path.join(BACKEND, "app", "routes")
        n_mod = len([f for f in glob.glob(os.path.join(route_dir, "*.py"))
                     if not f.endswith("__init__.py")
                     and "@router." in open(f, encoding="utf-8", errors="ignore").read()])
        if int(m.group(1)) != n_mod:
            problems.append(f"README 称「{m.group(1)} 个路由模块」，实测 {n_mod} 个")
    return problems


def check_api_contract_drift() -> list[str]:
    """第 4 项契约（P1-5）：前端调用的每个端点都必须存在于后端注册表。

    判据逻辑**不在这里复制**，而是复用 ``scripts/check_api_contract_drift.py`` ——
    复制一份就会出现「两份判据各自漂移」，那正是本门禁要防的病。这里只负责
    把它的结果翻译成 ci_reconcile 的问题清单。

    解析不到任何东西时（正则与代码形态脱节 / 契约文件缺失）返回一条失败，
    而不是静默通过 —— 一个「永远绿」的检查比没有检查更糟。
    """
    script = os.path.join(ROOT, "scripts", "check_api_contract_drift.py")
    if not os.path.isfile(script):
        return [f"缺少契约漂移检查脚本：{script}"]
    # 同目录模块：用 spec 加载，避免依赖运行时的 sys.path[0] 恰好是 scripts/
    import importlib.util
    spec = importlib.util.spec_from_file_location("_api_contract_drift", script)
    if spec is None or spec.loader is None:
        return [f"无法加载契约漂移检查脚本：{script}"]
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not os.path.isfile(mod.CONTRACT):
        return [f"缺少后端契约文件：{mod.CONTRACT}（先跑 tests/contracts/introspect.py）"]
    if not os.path.isdir(mod.API_DIR):
        return [f"缺少前端 API 目录：{mod.API_DIR}"]

    backend = mod.load_backend_endpoints()
    backend_norm = mod.contract_normalized(backend)
    frontend, _unresolved = mod.scan_frontend()

    if len(backend) < 50:
        return [f"后端契约只解析到 {len(backend)} 个端点 —— 检查失效（疑契约文件被清空）"]
    if len(frontend) < 20:
        return [f"前端只解析到 {len(frontend)} 个端点 —— 正则已与代码形态脱节"]

    missing = sorted(k for k in frontend if k not in backend_norm)
    return [f"前端调用了后端不存在的端点：{k}（{', '.join(frontend[k])}）"
            for k in missing]


def check_line_endings() -> list[str]:
    """`.gitattributes` 声明的行尾 ↔ 工作区实际字节 —— 混排即失败。

    ★ 为什么必须自动核（2026-09-26 R30 实测）：`build_all.bat` 是 `.bat`，cmd.exe
      **逐字节**读它。只要文件里出现**混排行尾**（绝大多数行 CRLF、刚被某个编辑器
      保存过的那几行是裸 LF），括号块（`if` / `for`）就会在 LF 处被撕裂。症状极其
      隐蔽：**脚本照跑、产物照出、退出码仍是 0**，只在 stderr 多几行
      ``'xxx' is not recognized as an internal or external command``
      —— 那正是被截断那几行的后半段，肉眼与 `git diff` 都看不出来。

      而「工具只把**改动行**写成 LF」是默认行为（本项目实测踩到）。`.gitattributes`
      早已声明谁必须 CRLF、谁必须 LF，但**从没有人核过工作区是否遵约** ⇒
      同一个坑踩了三次（TD-16 入库 LF / TD-17 检出未生效 / TD-21 编辑引入混排）。
      这里把它变成门禁：声明即契约，违约就报红。
    """
    if not os.path.isfile(GITATTRIBUTES):
        return [f"缺少行尾契约文件：{GITATTRIBUTES}"]
    rules = []
    for line in open(GITATTRIBUTES, encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\S+)\s+.*?\beol=(crlf|lf)\b", line)
        if m:
            rules.append((m.group(1), m.group(2)))
    if not rules:
        return ["`.gitattributes` 里没有任何 `eol=crlf` / `eol=lf` 声明 —— 门禁已失效"]

    problems: list[str] = []
    for pattern, want in rules:
        # 本仓库的规则都是字面路径（无通配）；仍用 glob 兼容将来加通配
        matches = glob.glob(os.path.join(ROOT, pattern), recursive=True)
        if not matches:
            continue  # 该文件在本工作区不存在（如 .sh 在别处生成）时不算错
        for path in matches:
            if os.path.isdir(path):
                continue
            raw = open(path, "rb").read()
            bad: list[int] = []
            line_no = 1
            for i, byte in enumerate(raw):
                if byte != 0x0A:
                    continue
                crlf = i > 0 and raw[i - 1] == 0x0D
                if (want == "crlf" and not crlf) or (want == "lf" and crlf):
                    bad.append(line_no)
                line_no += 1
            if bad:
                rel = os.path.relpath(path, ROOT).replace("\\", "/")
                shown = ", ".join(str(n) for n in bad[:20])
                more = f" …共 {len(bad)} 行" if len(bad) > 20 else ""
                problems.append(
                    f"{rel} 要求 eol={want}，但第 {shown} 行{more} 不符"
                    f" → 用编辑器「行尾=CRLF/LF」另存，或对该文件做一次整文件行尾归一化")
    return problems


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

    # README 里写死的计数同样要核（否则文档可以无限期地说错话）
    readme_problems = check_readme_numbers(tests, pages)
    if readme_problems:
        ok = False
        for p in readme_problems:
            print(f"[✗] README: {p}")
        print("     → 请更新 README 对应数字（或删掉该处数字声明）")
    else:
        print("[✓] README 计数声明与实测一致")

    # P1-5：前端 API 层 ↔ 后端 REST 契约（前端调了后端没有的端点 ⇒ 运行时 404）
    drift = check_api_contract_drift()
    if drift:
        ok = False
        for p in drift:
            print(f"[✗] API 契约: {p}")
        print("     → 修前端路径或补后端端点；详见 "
              "`python scripts/check_api_contract_drift.py -v`")
    else:
        print("[✓] 前端 API 调用全部命中后端契约")

    # 行尾契约（.gitattributes ↔ 工作区字节）：混排会让 .bat 的括号块被撕裂
    eol = check_line_endings()
    if eol:
        ok = False
        for p in eol:
            print(f"[✗] 行尾契约: {p}")
    else:
        print("[✓] .gitattributes 声明的行尾与工作区一致")

    if not ok:
        print("RECONCILE FAILED")
        return 1
    print("RECONCILE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
