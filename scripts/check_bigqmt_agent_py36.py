#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""agent_bigqmt/ 的 py3.6 兼容 + 防手抄闸门。

为什么必须单独一个脚本（而不是在测试里 assert 一下）
--------------------------------------------------
这个包**不会**在项目自身的 Python（3.11+）里运行，它跑在大 QMT 内置的
Python 3.6.x 里。也就是说：任何在开发环境能跑通的代码都可能在这台机器上炸，
并且**炸的时候是客户的实盘**。CI 必须在合并前拦住。

三条闸门
--------
G1 **py3.6 语法**：``ast.parse(feature_version=(3, 6))``。
G2 **禁用特性/依赖**：walrus(``:=``)、``match``、``dataclasses``、
   ``multiprocessing.shared_memory``、``asyncio``、非白名单三方库。
G3 **入口文件防手抄**：``BIGQMT_AGENT.py`` 里 QMT 注入函数的**字符串字面量**
   不得超过 ``_MAX_NAMES`` 个 —— 注入函数只能从 ``globals()`` 唯一来源捕获，
   手抄名单曾把一个桥 bug 伪装成「终端没有两融接口」。
G4 **执行面越界**（2026-10-23 新规）：``os.system/popen/startfile/exec*/spawn*``、
``eval/exec/compile``、以及用**字面量**绕过的动态导入。
   ★ 为什么 G2 的白名单不够：``os`` 本身在白名单里，而它同时提供执行外部程序的
     入口 —— 白名单只管 ``import``，管不住「已白名单模块里的越界方法」。
   ★ 诚实边界：``getattr(os, "sys" + "tem")`` 这类**动态拼接**静态扫不出来。
     它由 ``tests/test_bigqmt_sandbox_compliance.py`` 在运行时兜住（沙箱替身里
     ``os.system`` 同样抛异常）。静态闸门负责「写得出来就拦」，运行期闸门负责
     「跑起来才发作的」—— 两者缺一都会留一个假绿灯。

★ **只扫代码，不扫字符串**（-= lessons learned）：本文件自身的注释/docstring 里
  就提到 walrus、dataclass 等词。若用 ``":=" in src`` 这类文本匹配，
  「写着禁止事项的说明文字」会被判违规 —— 必须走 AST 并从代码结构中判断。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "backend" / "agent_bigqmt"

#: 允许的第三方/本地模块（其余一律视为不可用于 QMT 内置环境）
_ALLOWED_MODULES = {
    "__future__", "json", "os", "sys", "time", "traceback",
    "threading", "math", "re", "ast", "io",
    "datetime", "collections", "itertools", "shelve", "csv", "shutil",
    "qmt_api",  # 同包的本地模块
    "agent_runtime",  # 同包的本地模块：独立进程模式运行环境层（2026-10-13 从
                      # BIGQMT_AGENT.py 按职责拆出，降 50KB 上帝文件）
    # ★ 新规闭环（2026-10-31）：两个同包本地模块，都会由 gen_qmt_agent_bundle
    #   内联进单文件产物，因此运行时**没有**额外文件依赖。
    "position_source",  # 柜台重建持仓（替代已失效的 position.json）
    "closed_loop",      # 计划解析 / 风控闸门 / 意图生成 / 日志回传行
    # ★ xtquant / xtdata 是 **QMT 自带**的包（bin.x64/Lib/site-packages/xtquant），
    #   不是「额外依赖」。2026-10-08 用内置 bin.x64/pythonw.exe (Python 3.6.8) 实测：
    #   `import xtquant` OK、`import xtquant.xttrader` OK、
    #   `from xtquant import xtdata` OK（注意 `import xtdata` 是失败的）。
    #   独立进程模式（QMT「模型交易 → 运行」= pythonw -u <策略.py>）没有终端注入的
    #   下单函数，行情只能走 xtdata —— 所以这两条 import 是必需的，且调用点都在
    #   try/except 里，缺失时如实降级、绝不伪造数据。
    "xtquant", "xtdata",
}

#: QMT 会注入到策略入口命名空间的代表性函数名（用于 G3 计数）
_INJECTABLE = (
    "passorder", "cancel", "get_trade_detail_data", "call_formula",
    "get_stock_list_in_sector", "get_instrument_detail",
    "download_history_data", "create_sector", "get_trading_dates",
)

_MAX_NAMES = 3

#: G4：``os`` 本身在白名单里，但它同时提供「执行外部程序」的入口。
#: 白名单只拦 ``import``，拦不住 ``os.system`` —— 这是白名单式闸门的**固有盲区**，
#: 必须单独补。新规（2026-10-23）把策略与外部的一切交互都关了，起一个子进程
#: 去读写文件/发网络包，等价于绕过全部三条限制。
_OS_EXEC = frozenset({
    "system", "popen", "startfile", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "posix_spawn", "posix_spawnp",
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
    "execlpe", "fork", "forkpty", "kill", "killpg",
})

#: G4：动态执行/动态导入 —— 用来把**字面量**绕过 import 白名单的两种写法。
_DYNAMIC_EXEC = frozenset({"eval", "exec", "compile"})


def _literal_import_name(node: "ast.Call") -> str | None:
    """``__import__("x")`` / ``importlib.import_module("x")`` 里的字面量模块名。"""
    if not node.args:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _is_dynamic_import(fn: "ast.expr") -> bool:
    if isinstance(fn, ast.Name) and fn.id == "__import__":
        return True
    return (isinstance(fn, ast.Attribute) and fn.attr == "import_module"
            and isinstance(fn.value, ast.Name) and fn.value.id == "importlib")


def check_dir(agent_dir=None) -> list[str]:
    """扫一个目录（缺省 = 真实的 ``backend/agent_bigqmt``）。

    ★ 目录参数存在的唯一理由是**让闸门本身可测**：一个「永远不会报错」的闸门
      等于没有闸门。测试用临时目录喂违规样本，证明它真的会红。
    """
    errors: list[str] = []
    target = Path(agent_dir) if agent_dir is not None else AGENT_DIR
    entry_file = target / "BIGQMT_AGENT.py"
    if not target.is_dir():
        return ["目录不存在: %s" % target]

    for path in sorted(target.glob("*.py")):
        src = path.read_text(encoding="utf-8")

        # ---- G1: py3.6 语法 ----
        try:
            tree = ast.parse(src, filename=str(path), feature_version=(3, 6))
        except SyntaxError as exc:
            errors.append("G1 py3.6 语法错误 %s: %s" % (path.name, exc))
            continue

        # ---- G2: 禁用特性 / 依赖（AST 判定，字符串里的同名词不算）----
        for node in ast.walk(tree):
            if isinstance(node, ast.NamedExpr):  # :=
                errors.append("G2 walrus 运算符是 py3.8+ (%s:%d)"
                              % (path.name, node.lineno))
            elif type(node).__name__ == "Match":
                errors.append("G2 match 语句是 py3.10+ (%s:%d)"
                              % (path.name, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in _ALLOWED_MODULES:
                        errors.append("G2 不允许的外部依赖: %s (%s:%d)"
                                      % (alias.name, path.name, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                base = (node.module or "").split(".")[0]
                if base not in _ALLOWED_MODULES:
                    errors.append("G2 不允许的外部依赖: %s (%s:%d)"
                                  % (node.module, path.name, node.lineno))

        # ---- G4: 绕过白名单的执行面（新规 2026-10-23 第二/三条）----
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                    and fn.value.id == "os" and fn.attr in _OS_EXEC):
                errors.append(
                    "G4 os.%s 可执行外部程序（%s:%d）—— 新规下策略不得与外部有任何"
                    "交互，起子进程等于绕过全部三条限制" % (fn.attr, path.name, node.lineno))
            elif isinstance(fn, ast.Name) and fn.id in _DYNAMIC_EXEC:
                errors.append("G4 %s() 动态执行（%s:%d）—— 禁止把代码当字符串执行"
                              % (fn.id, path.name, node.lineno))
            elif _is_dynamic_import(fn):
                name = _literal_import_name(node)
                if name is not None and name.split(".")[0] not in _ALLOWED_MODULES:
                    errors.append(
                        "G4 动态导入白名单外的模块 %r（%s:%d）—— 用字面量绕过了 G2"
                        % (name, path.name, node.lineno))

        # ---- G3: 入口防手抄（统计字符串字面量里出现的注入函数名）----
        if path == entry_file:
            hit = 0
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    low = node.value.strip()
                    if low in _INJECTABLE:
                        hit += 1
            if hit > _MAX_NAMES:
                errors.append(
                    "G3 入口文件出现 %d 个注入函数名字面量（上限 %d）—— "
                    "必须通过 capture_qmt_injected_funcs(globals()) 从唯一来源捕获，"
                    "严禁手抄名单（会把桥的 bug 伪装成终端能力缺失）"
                    % (hit, _MAX_NAMES))
    return errors


def main() -> int:
    errors = check_dir()
    if errors:
        sys.stderr.write("agent_bigqmt gate: FAILED\n")
        for line in errors:
            sys.stderr.write("  - %s\n" % line)
        return 1
    print("agent_bigqmt gate: OK (py3.6 syntax + no 3.7+ features + entry-file no "
          "hand-copied names + no exec/network escape)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
