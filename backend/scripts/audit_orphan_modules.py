# -*- coding: utf-8 -*-
"""孤儿模块扫描：找出**没有任何地方 import** 的产品代码模块。

为什么需要它（"不存在孤儿逻辑"）：重构/迁移之后常有"模块还在、引用早没了"
的残留 —— 它不会被任何测试覆盖，也不会报错，只会持续腐化（有人照着它改，
改完发现根本没生效）。本仓已有「孤儿字段/孤儿参数/孤儿 prop」的教训，
这一条把同样的判据用在**模块**粒度上。

判据（保守，宁少报不误报）：
- 只看 ``backend/`` 下的产品代码目录（测试、脚本、迁移、工具产物排除）；
- 一个模块算"被引用"，只要全仓任意 ``.py`` 里出现
  ``import <模块名>`` / ``from <模块名> import`` / ``from ... import <模块名>``
  或是包内相对 import（``from .x import`` 归到同包前缀）；
- **命令行/文本引用也算**：源码注释、CI yml、``.bat``/``.sh``、文档里出现
  ``<产品目录>/<模块>.py`` 路径即视为引用（见 ``_cmdline_refs``）；
- 入口/被动态加载的模块需登记在 ALLOW（如 ``__main__``、被字符串加载的探针）。

★ 为什么必须加"命令行引用"这一条（本脚本曾**假告状**，TD-33 同族）：
  ``tools/fetch_runtimes.py`` 是**构建关键路径**——被 ``release.yml`` /
  ``build-client.yml`` 以 ``python tools/fetch_runtimes.py --with-deps --strict``
  调用，缺失即打包出"没有桥接运行时"的包；``tools/diag_qmt.py`` 则是人工
  命令行诊断入口，并被 ``xtquant_client/xtp/{env,adapter}.py`` 的活代码 docstring
  点名。二者都**永远不会被 import**，于是只认 ``import`` 的旧判据把它们长期
  报成孤儿 —— 一份"永远有 2 条假红"的报告，等于告诉使用者"忽略这个脚本"。
  修法是把判据补全（承认非 import 的引用方式），而**不是**把它们塞进 ALLOW：
  白名单会让真死掉的那天也没人发现。

用法：cd backend && python scripts/audit_orphan_modules.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

#: 参与扫描的产品代码目录（相对 backend/）。
#: ⚠️ 刻意**不含** ``runtimes``：那里装的是捆绑的 CPython 运行时（cp311 及其
#: site-packages），是**依赖产物**不是本仓代码 —— 扫它只会得到几千条第三方
#: 模块噪声（实测把 4 条真信号淹成 3000+ 行）。
SCAN_DIRS = ("app", "engines", "core", "connectors", "datasource", "gateway",
             "sync", "tools", "xtquant_client", "plugins",
             "mcp_server", "search")

#: 显式豁免：入口点、被字符串/配置动态加载、或有非 import 的引用方式。
#: 每一条都必须写明**为什么**它没有静态 import 也是活的。
#: ⚠️ 不要把"其实是被命令行调用"的模块塞进来 —— 那属于判据缺口，见文件头。
ALLOW = {
    "__main__",                      # 入口
    "__init__",                      # 包标记
    "conftest",                      # pytest 注入
    "setup",                         # 打包脚本
}

_RE_IMPORT = re.compile(
    r"^\s*(?:from\s+([\w\.]+)\s+import\s+([^\n#]+)|import\s+([\w\.]+))", re.M)

#: 命令行引用：``tools/fetch_runtimes.py`` / ``backend/app/routes/market.py``
#: 这类写法出现在注释、CI、bat/sh、文档里。前导负向断言只为避免在
#: ``fooapp/bar.py`` 这种更长标识符中间误切。
_RE_INVOKE = re.compile(
    r"(?<![A-Za-z0-9_])((?:%s)/[\w/]+)\.py\b" % "|".join(SCAN_DIRS))

#: 命令行引用要扫的文件类型（比 import 扫描宽：含 CI / 批处理 / 文档）。
TEXT_SUFFIXES = {".py", ".bat", ".sh", ".cmd", ".ps1", ".yml", ".yaml", ".md"}
#: 这些目录属产物/依赖，扫它们只会制造噪声。
SKIP_PARTS = {"node_modules", "__pycache__", ".venv", "dist", "build",
              "runtimes", "release", "logs", "htmlcov", "coverage"}


def _referenced_names(text: str) -> set[str]:
    """从源码里抽出「被 import 到的模块/名字」集合。

    ★ 必须解析 ``from X import a, b`` 的**名字列表**：只记 ``X`` 会把
      ``from app.services import account_store`` 判成「只引用了 app.services」，
      于是 ``app.services.account_store`` 被误报成孤儿（实测 13 条里 10 条是这类误报）。
    """
    refs: set[str] = set()
    for m in _RE_IMPORT.finditer(text):
        if m.group(3):                      # import a.b.c
            _add_dotted(refs, m.group(3).strip())
            continue
        base = (m.group(1) or "").strip()
        _add_dotted(refs, base)
        names = (m.group(2) or "").replace("(", " ")
        for raw in names.split(","):
            nm = raw.strip().split(" as ")[0].strip()
            if not nm or nm == "*":
                continue
            refs.add(nm)
            if base:
                refs.add(f"{base}.{nm}")
    return refs


def _add_dotted(refs: set[str], spec: str) -> None:
    if not spec:
        return
    parts = [p for p in spec.split(".") if p]
    if not parts:
        return
    for i in range(len(parts), 0, -1):
        refs.add(".".join(parts[:i]))
    # ★ 末段也要记：相对 import（``from .ptrade_v1 import X``）拿不到包前缀，
    #   只有末段 ``ptrade_v1`` 能跟「包内模块」对上；漏了它会把整个 dialects
    #   目录误报成孤儿。
    refs.add(parts[-1])


def _module_names() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for d in SCAN_DIRS:
        root = BACKEND / d
        if not root.is_dir():
            continue
        for p in root.rglob("*.py"):
            if any(part.startswith(".") or part in ("dist", "build", "__pycache__")
                   for part in p.parts):
                continue
            rel = p.relative_to(BACKEND)
            mod = ".".join(rel.with_suffix("").parts)
            out[mod] = p
    return out


def _all_sources() -> list[Path]:
    files = []
    for p in BACKEND.rglob("*.py"):
        if any(part in ("__pycache__", "build") or part.startswith(".")
               for part in p.parts):
            continue
        if "dist" in p.parts and "_internal" in p.parts:
            continue          # PyInstaller 产物
        if ".venv" in p.parts:
            continue
        files.append(p)
    return files


def _text_files() -> list[Path]:
    """repo 内参与「命令行引用」扫描的文本文件。

    ★ 用 ``os.walk`` 原地剪枝而不是 ``rglob`` 后再过滤：``node_modules`` 下
      十万级文件会让 rglob 仍然逐个 stat 一遍（实测把本脚本从 ~2s 拖到 26s）。
    """
    out: list[Path] = []
    for dirpath, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in SKIP_PARTS and (not d.startswith(".") or d == ".github")]
        for name in files:
            if Path(name).suffix.lower() in TEXT_SUFFIXES:
                out.append(Path(dirpath) / name)
    return out


def _invoke_names(text: str) -> set[str]:
    """从一段文本里抽出「被按路径点名」的模块名（点号全名 + 末段名）。"""
    refs: set[str] = set()
    for m in _RE_INVOKE.finditer(text):
        dotted = m.group(1).replace("/", ".")
        refs.add(dotted)
        refs.add(dotted.rsplit(".", 1)[-1])
    return refs


def _cmdline_refs() -> set[str]:
    """从「按路径点名调用」的角度收集被引用模块（非 import 那一路）。

    覆盖：源码注释/docstring、CI workflow、``.bat``/``.sh``、文档。返回值与
    ``_referenced_names`` 同构（同时给**点号全名**与**末段名**），便于直接并集。
    """
    refs: set[str] = set()
    for p in _text_files():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs |= _invoke_names(text)
    return refs


def main() -> int:
    mods = _module_names()
    sources = _all_sources()
    print(f"扫描 {len(mods)} 个模块 / {len(sources)} 个源文件")

    blob = []
    for p in sources:
        try:
            blob.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    text = "\n".join(blob)

    referenced = _referenced_names(text)
    invoked = _cmdline_refs()
    print(f"命令行/文本引用（非 import 那一路）：{len(invoked)} 个名字")
    referenced |= invoked

    orphans = []
    for mod, path in sorted(mods.items()):
        leaf = mod.rsplit(".", 1)[-1]
        if leaf in ALLOW:
            continue
        if mod in referenced or leaf in referenced:
            continue
        orphans.append(mod)

    print(f"\n=== 零引用模块：{len(orphans)} ===")
    for m in orphans:
        print(f"  {m}")
    if not orphans:
        print("  （无 — 无孤儿模块）")
        return 0
    # ★ 从「只打印」升级为「会红」：孤儿模块不会自己报错，只会持续腐化，
    #   没人看的报告等于没有报告。真发现时给可操作的两条出路。
    print("\n[FAIL] 存在零引用模块。请二选一：\n"
          "  1) 真死掉了 → 删除文件；\n"
          "  2) 是入口/被动态加载 → 若属「按路径被调用」，检查 _RE_INVOKE 是否漏了\n"
          "     该调用方式；确属无静态线索的，登记进 ALLOW **并写明原因**。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
