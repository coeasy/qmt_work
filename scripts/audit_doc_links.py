# -*- coding: utf-8 -*-
"""文档引用断链扫描：代码/文档里引用的仓库内文件路径是否真的存在。

为什么需要它（"不存在断链"）：删掉一份退役文档、改名、或把规划移进
``docs/archive/`` 之后，**引用它的注释不会报错**。于是读者按图索骥找不到文件，
只能猜"是不是我看漏了" —— 这比"没写引用"更糟：它消耗的是信任。

本仓已实测踩到：``plugins/__init__.py`` 的模块 docstring 引用了 3 份在
``488b2db``（"移除退役脚本与历史规划文档"）里被删除的 V9 审计文档，
一年多无人发现，因为**没有任何门禁看这一层**。

判据：只认 ``docs/...md`` / ``backend/...`` 这类**仓库内相对路径**（含裸文件名
`TECH_DEBT.md` 的宽松形态），排除 URL、排除已被 git 跟踪的"未来文件"。
逐文件解析，相对引用按其所在目录解析，其余按仓库根解析。

**扫描面（为什么不是全仓 rglob）**：只扫**产品面**——文档、后端源码、前端源码、
脚本、CI 配置。以下一律不扫，因为它们的语义不是"指向本仓文件"：
  * ``.workbuddy*/`` 历史工作日志 —— append-only 的历史记录，写的时候文件在，
    现在不在了属正常；它记的是"当时"不是"现在"，改它等于篡改历史。
  * ``dist`` / ``build`` / ``runtimes`` / ``node_modules`` —— 构建产物，其中
    大量路径来自第三方包清单，与仓库自身结构无关。
首版全仓 rglob 得到 1351 条"断链"，其中 1300+ 是上面两类噪声，淹没了真问题。

用法：python scripts/audit_doc_links.py
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN_SUFFIXES = (".py", ".md", ".ts", ".tsx", ".sh", ".bat", ".yml", ".yaml",
                 ".ps1", ".json")
SKIP_DIRS = {"node_modules", ".venv", "dist", "dist-electron", "build",
             "runtimes", "__pycache__", "logs", "site-packages", "release",
             ".tmp-test", "coverage", "htmlcov"}

#: 形如 ``docs/xxx.md`` / ``backend/a/b.py`` / 裸 ``TECH_DEBT.md``。
#: 末尾允许跟 ``::TD-16`` / `` §7.2`` / ``:79`` 这类定位后缀，一律剥掉。
#:
#: ★ 两条**必须**遵守的写法，否则本工具会自己造假断链（实测踩到）：
#:   1) ``tsx`` 必须排在 ``ts`` **前面**。正则选择分支是「先到先得」，
#:      写成 ``ts|tsx`` 时 ``routes.tsx`` 会被切成 ``routes.ts`` 并留下尾巴
#:      ``x`` —— 于是全仓 20 处 ``*.tsx`` 引用被集体误报成断链。
#:   2) 末尾必须有 ``(?![.\w])`` 边界，防止匹配到更长文件名/扩展名的前缀。
_RE_PATH = re.compile(
    r"(?<![\w/.-])((?:docs|backend|frontend-next|scripts|tests|\.github)/[\w./\-]+"
    r"\.(?:tsx|ts|md|py|sh|bat|ps1|yml|yaml|json))(?![.\w])"
    r"|(?<![\w/.-])([A-Z][A-Z0-9_\-]{3,}\.(?:tsx|ts|md|py))(?![.\w])")

#: 占位符/模板片段——**有意**不指向任何真实文件，不是缺陷。
_ALLOW_SUBSTR = ("xxx", "yyy", "zzz", "...", "…", "YYYY", "<", ">", "{}", "*")

#: 逐条给理由的已知有意非路径。
ALLOW = {
    "docs/…md",
    "backend/…",
    # 本脚本自身 docstring 里的**示例**（教学用假路径，不可能存在）
    "backend/a/b.py",
    "backend/tests/test_x.py",
    "tests/test_x.py",
}

#: 这些**源**里的引用天然无法解析，不应计入断链：
#:   * ``docs/release-notes/`` —— 发布记录是**冻结的历史事实**。「v0.4.1 当时的
#:     文档叫 X，后来被 Y 取代」正是发布记录的功能本身；去改它等于篡改历史。
#:     历史引用按当时布局解析，本就不该用今天的树去判真伪。
#:   * ``docs/archive/`` —— 同上，归档件是**归档前的快照**，内部互引用的是当时的
#:     路径（``docs/BIG_QMT_COMPAT_PLAN.md`` 而非 ``docs/archive/...``）。它们正是
#:     安装该目录移动动作的产物，回改等于伪造历史。见 docs/archive/README.md。
#:   * 本脚本自身 —— 它的 ``KNOWN_MISSING`` 表与 docstring 必须**写出**那些
#:     不存在的名字才能讨论它们，扫自己就会把自己的规则表当成断链（自指噪声）。
FROZEN_SOURCES = ("docs/release-notes/", "docs/archive/",
                  "scripts/audit_doc_links.py")

#: candidate → 理由。这些名字**确实**不在仓库里，但**引用是对的**：
#: 它们是运行期产物、生成物、仓外路径，或被引用处本身就在讲「它已退役」。
#: 把这类逐条列出来而不是放宽匹配规则，是为了不放过真正的断链。
KNOWN_MISSING: dict[str, str] = {
    "ENV_PROBE.py":
        "独立环境探针，其能力已并入 agent（BIGQMT_AGENT.py 自带 probe_result.json）；"
        "引用处正是在讲「为什么做进 agent 而不要求用户手动跑它」",
    "QMT_WORK_AGENT.py":
        "QMT 客户端侧**部署文件名**（生成物，落在客户端 python/ 目录，仓外）；"
        "与 qmt_work_agent.py 在 Windows 上同一文件，仅大小写不同",
    "backend/qmt_work_config.json":
        "**运行期自动生成**文件（首次启动写入 exe 同目录），不随仓库分发；"
        "引用处是测试在讲「别污染这个运行期文件」",
    "backend/scripts/check_frontend_classnames.py":
        "文档**明确标注**「已随旧前端退役而失效（现直接打印 [SKIP] 并 exit 0）」；"
        "引用即其主题，属讣告而非链接",
    "docs/QMT_UNIVERSAL_BROKER_PLATFORM_ARCHITECTURE_V3.md":
        "docs/README.md 的「已清理文档清单」条目（自述「已归档，执行口径改用 V4」）；"
        "引用即其主题",
    "QMT_UNIVERSAL_BROKER_PLATFORM_ARCHITECTURE_V3.md":
        "同上（同一文档的裸文件名形态，出现在归档计划的自述里）",
    "PROJECT_OPTIMIZATION_PLAN_2026-10-03.md":
        "本索引自身的**删除记录**：这份一次性计划已于 2026-10-05 删除，"
        "索引必须写出它的名字才能说明删了什么。属记录而非链接",
}

#: glob 形式的已知有意缺失（子串匹配），理由同上。
KNOWN_MISSING_GLOBS: dict[str, str] = {}


#: 仓库根下的**别名前缀**：文档/注释里对这些目录的简写，等价于真实目录。
#: 每一条都是本仓既有约定，不是为了让告警消失而加的：
#:   * ``backend/``      —— pytest rootdir（backend/pyproject.toml），故全文里
#:                          ``tests/test_x.py`` == ``backend/tests/test_x.py``。
#:   * ``frontend-next/``—— vitest rootdir，``tests/x.test.ts`` 同理。
#:   * ``backend/tests/probes/`` —— 探针脚本自带用法行写 ``tests/probe_x.py``。
RESOLVE_PREFIXES = ("backend", "frontend-next", "backend/tests/probes")


def _sources() -> list[Path]:
    """产品面待扫文件清单。

    ★ 用 ``os.walk`` + **就地剪枝**（``dirs[:] = ...``）而不是 ``Path.rglob``：
    rglob 先把整棵树走完，再在结果里过滤 —— 于是 ``node_modules``（~3 万文件）
    与 ``backend/dist``（PyInstaller 产物，4.6 GB）**照样被整个遍历了一遍**，
    实测 9 秒几乎全烧在这两者上。剪枝后 <1 秒。

    这不是微优化：跑 9 秒的门禁没人会跑，等于没有门禁。本脚本要靠它进 pytest。
    """
    out: list[Path] = []
    for dirpath, dirs, files in os.walk(ROOT):
        # 就地剪枝：从这里起不再下降（隐藏目录同理，`.github` 例外）
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS and (not d.startswith(".") or d == ".github")
        ]
        for name in files:
            if Path(name).suffix in SCAN_SUFFIXES:
                out.append(Path(dirpath) / name)
    return out


def _resolves(src: Path, cand: str, tracked: set[str]) -> bool:
    """candidate 是否真的指向一个存在的文件（含仓库既定别名）。"""
    if src.parent.joinpath(cand).exists() or ROOT.joinpath(cand).exists():
        return True
    for pre in RESOLVE_PREFIXES:
        if ROOT.joinpath(pre, cand).exists():
            return True
    # 裸文件名：仓库任意位置有同名文件即算存在
    if "/" not in cand:
        return any(t.endswith("/" + cand) or t == cand for t in tracked)
    return False


def main() -> int:
    tracked = set()
    res = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True)
    for line in res.stdout.decode("utf-8", "replace").splitlines():
        tracked.add(line.replace("\\", "/"))

    sources = _sources()
    broken: dict[str, list[str]] = {}
    known: dict[str, list[str]] = {}
    checked = 0
    for src in sources:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_src = src.relative_to(ROOT).as_posix()
        if rel_src.startswith(FROZEN_SOURCES):
            continue
        for m in _RE_PATH.finditer(text):
            cand = (m.group(1) or m.group(2) or "").rstrip(".,;:)")
            if not cand or cand in ALLOW:
                continue
            if any(tok in cand for tok in _ALLOW_SUBSTR):
                continue
            checked += 1
            if _resolves(src, cand, tracked):
                continue
            if cand in KNOWN_MISSING or any(
                    g in cand for g in KNOWN_MISSING_GLOBS):
                known.setdefault(cand, []).append(rel_src)
            else:
                broken.setdefault(cand, []).append(rel_src)

    print(f"扫描 {len(sources)} 个文件，检查 {checked} 处路径引用")
    print(f"（已跳过冻结历史源：{'、'.join(FROZEN_SOURCES)}）")

    print(f"\n=== 断链 {len(broken)} 处 ===")
    for cand, srcs in sorted(broken.items()):
        print(f"  {cand}")
        for s in sorted(set(srcs))[:6]:
            print(f"      ← {s}")
    if not broken:
        print("  （无 — 全部引用可解析）")

    print(f"\n=== 已知有意缺失 {len(known)} 项（非缺陷，逐条有理由）===")
    for cand, srcs in sorted(known.items()):
        reason = KNOWN_MISSING.get(cand) or next(
            (v for g, v in KNOWN_MISSING_GLOBS.items() if g in cand), "")
        print(f"  {cand}")
        print(f"      理由：{reason}")
        for s in sorted(set(srcs))[:3]:
            print(f"      ← {s}")

    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
