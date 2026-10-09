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
ENTRY_FILE = AGENT_DIR / "BIGQMT_AGENT.py"

#: 允许的第三方/本地模块（其余一律视为不可用于 QMT 内置环境）
_ALLOWED_MODULES = {
    "__future__", "json", "os", "sys", "time", "traceback",
    "threading", "math", "re", "ast", "io",
    "datetime", "collections", "itertools", "shelve", "csv", "shutil",
    "qmt_api",  # 同包的本地模块
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


def check_dir() -> list[str]:
    errors: list[str] = []
    if not AGENT_DIR.is_dir():
        return ["目录不存在: %s" % AGENT_DIR]

    for path in sorted(AGENT_DIR.glob("*.py")):
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

        # ---- G3: 入口防手抄（统计字符串字面量里出现的注入函数名）----
        if path == ENTRY_FILE:
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
    print("agent_bigqmt gate: OK (py3.6 syntax + no 3.7+ features + entry-file no hand-copied names)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
