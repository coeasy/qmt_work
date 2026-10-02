# -*- coding: utf-8 -*-
"""策略端 agent 的**自动验证器**：只读证据文件，输出可执行判定。

设计原则（与项目「零猜测」纪律一致）
------------------------------------
* 判定证据有**四份**，都不依赖人的口述：
    - **bundle 源文件本身**   AST 语法 + 污染签名（P0 护栏；先于运行时数据）
    - ``probe_result.json``  启动自检（注入函数 / ContextInfo 方法面 / 写权限）
    - ``agent_status.json``  心跳（uptime / trading_enabled / 主循环最近异常）
    - 客户端日志的注册树片段  策略**是否已登记**（这是「模型交易里看不到」的根因面）
* 心跳**新鲜度**才是「策略在跑」的判据 —— 「文件存在」不代表进程活着
  （QMT 关闭后文件会留着，这正是最容易自欺的一条）。
* 每一项失败都给出**下一步动作**，而不是只报错。

**Bundle 完整性**（R17 引入 · 2026-10-02）
----------------------------------------
真实事故：`QMT_WORK_AGENT.py` 头部被拼了 18 行另一支 CCI 策略（`#encoding:gbk`
+ `import pandas/numpy/talib` + `init(ContextInfo)` stub），docstring 边界处
Python 直接 `IndentationError`。所以除了运行时证据，还要在**运行前**做两件事：

1. `check_bundle_syntax` — AST 解析能否通过；不通过就直接定位到行号
2. `check_bundle_pollution` — 前 N 行是否出现 pandas/numpy/talib/sklearn 等
   非 qmt_api 白名单 import（合法 agent 只应 import 标准库）

注册状态为什么必须单独验
------------------------
QMT 的策略列表是**客户端持久化注册树**，不是策略目录扫描。实测判据：
目录里两个 **md5 完全相同**的文件，重启后一个在列表、一个不在。所以
「文件放到 python/ 了」与「模型交易里能选到它」是两件事，缺了注册这一步，
心跳永远不会出现——必须先分清是哪一步断的。

用法::

    python scripts/qmt_agent_verify.py [--bridge-dir DIR] [--json] [--wait 60]
"""
from __future__ import print_function

import argparse
import glob
import io
import json
import os
import re
import sys
import time

DEFAULT_BRIDGE = r"C:\Users\Administrator\qmt_work\bigqmt_bridge"

#: 心跳超过这个秒数视为「策略没在跑」（写心跳间隔 10s，留 3 倍余量）。
STALE_S = 45

#: 判定「交易能力」必需捕获的函数。
NEED_FOR_TRADE = ("passorder", "cancel")
NEED_FOR_QUERY = ("get_trade_detail_data",)


def _load(path):
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Bundle 完整性护栏（P0 · R17 引入）
# ---------------------------------------------------------------------------

#: QMT agent bundle **只应** import 标准库 + `__future__`。下列第三方/科学计算库
#: 出现在 bundle 前部就是**污染**（另一支策略被前缀拼进来）：
#: 2026-10-02 真实事故 `QMT_WORK_AGENT.py` 被 18 行 CCI 策略头污染，前缀含
#: `import pandas as pd` + `import numpy as np` + `import talib`。
POLLUTION_IMPORTS = (
    "pandas", "numpy", "talib", "sklearn", "scipy",
    "matplotlib", "seaborn", "plotly", "torch", "tensorflow",
)

#: 合法 QMT agent 顶部只应有标准库 import（白名单）。
_AGENT_STDLIB_IMPORTS = frozenset({
    "json", "os", "sys", "time", "traceback", "ast", "io", "re", "glob",
    "argparse", "threading", "signal", "shutil", "tempfile", "logging",
    "collections", "collections.abc", "functools", "itertools",
    "datetime", "math", "random", "string", "textwrap", "base64",
    "hashlib", "struct", "enum", "contextlib", "types", "operator",
    "copy", "pprint", "socket", "http", "http.client", "urllib",
    "thread", "select", "select.select", "subprocess", "unicodedata",
    "__future__",
})


def _read_bundle_source(path, encoding_candidates=("utf-8", "gb18030", "gbk")):
    """按多个编码候选读 bundle 源文件。返回 (text, encoding) 或 (None, reason)。"""
    if not path or not os.path.isfile(path):
        return None, "file_not_found"
    for enc in encoding_candidates:
        try:
            with io.open(path, "r", encoding=enc) as fh:
                return fh.read(), enc
        except (UnicodeDecodeError, IOError):
            continue
    return None, "encoding_failed"


def check_bundle_syntax(path):
    """AST 解析校验。返回 dict：
    {'ok': bool, 'error': str|None, 'encoding': str|None,
     'line_count': int, 'size_bytes': int}
    """
    out = {"ok": False, "error": None, "encoding": None,
           "line_count": 0, "size_bytes": 0}
    try:
        out["size_bytes"] = os.path.getsize(path)
    except OSError:
        return out
    text, enc = _read_bundle_source(path)
    if text is None:
        out["error"] = "cannot_read (%s)" % enc
        return out
    out["encoding"] = enc
    out["line_count"] = text.count("\n") + 1
    try:
        import ast  # 延迟导入，避免在没用的分支上浪费
        ast.parse(text)
        out["ok"] = True
    except SyntaxError as e:
        out["error"] = "SyntaxError at line %s: %s" % (e.lineno, e.msg)
    except Exception as e:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def check_bundle_pollution(path, top_n=20):
    """检测 bundle 顶部 N 行是否出现「非法第三方 import」。
    返回 dict：{'ok': bool, 'imports_found': list, 'first_import_line': int}
    """
    out = {"ok": True, "imports_found": [], "first_import_line": -1}
    text, _ = _read_bundle_source(path)
    if text is None:
        # 读不出来的情况由 check_bundle_syntax 负责报错
        return out
    lines = text.splitlines()
    import_re = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)")
    for i, line in enumerate(lines[:top_n]):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = import_re.match(stripped)
        if not m:
            continue
        mod = m.group(1)
        root = mod.split(".")[0]
        if root in POLLUTION_IMPORTS:
            out["ok"] = False
            out["imports_found"].append("%s@L%d" % (root, i + 1))
            if out["first_import_line"] < 0:
                out["first_import_line"] = i + 1
    return out


def bundle_health(path):
    """一次跑完语法 + 污染两项检查，返回给 diag_report / 前端聚合用。

    ``checked=True``：调用方已给出确定的 bundle 路径，结论具判定力。
    """
    syn = check_bundle_syntax(path)
    pol = check_bundle_pollution(path)
    return {
        "checked": True,
        "path": path,
        "size_bytes": syn.get("size_bytes", 0),
        "line_count": syn.get("line_count", 0),
        "encoding": syn.get("encoding"),
        "syntax_ok": syn.get("ok", False),
        "syntax_error": syn.get("error"),
        "pollution_ok": pol.get("ok", True),
        "pollution_hits": pol.get("imports_found", []),
        "pollution_first_line": pol.get("first_import_line", -1),
        "ok": bool(syn.get("ok") and pol.get("ok")),
    }


#: 只有**主日志**里才有注册树行。副日志（`XtClient_datasource_*.log` 等）
#: 没有；按 mtime 取「最新」会取到副日志 → 注册树解析成空集 → 把「已注册」
#: 全部误报成「未注册」。静默假阴性比报错危险，必须按名字精确匹配。
_MAIN_LOG_RE = re.compile(r"^(?:XtClient|XtMiniQmt)_\d{8}\.log$", re.I)


def _newest_log(qmt_dir):
    cands = []
    for d in (os.path.join(qmt_dir, "userdata", "log"),
              os.path.join(qmt_dir, "userdata_mini", "log")):
        for p in glob.glob(os.path.join(d, "*.log")):
            if _MAIN_LOG_RE.match(os.path.basename(p)):
                cands.append(p)
    return max(cands, key=os.path.getmtime) if cands else None


def registration(qmt_dir, name):
    """从最新客户端日志还原「注册树」状态（不依赖任何文件能否被读）。"""
    out = {"log": None, "registered": None, "autorun": None, "file_exists": None}
    if not qmt_dir or not os.path.isdir(qmt_dir):
        return out
    sfile = os.path.join(qmt_dir, "python", name + ".py")
    out["file_exists"] = os.path.exists(sfile)
    log = _newest_log(qmt_dir)
    if not log:
        return out
    out["log"] = log
    with open(log, "rb") as fh:
        raw = fh.read().decode("utf-8", "replace")
    names = set(m.group(2).strip() for m in
                re.finditer(r"from configFormula, index:(\d+), utfName:([^,]+),", raw))
    if not names:
        # 主日志里一条注册树记录都没有 ⇒ 无法判定，**不能**报「未注册」
        out["registered"] = None
        return out
    out["registered"] = name in names
    # ★ 字段名是客户端自己的拼写 `FomrulaName`；写错会 0 命中并伪装成
    #   「没配自动运行」。两种拼写都收。
    pat = (r"\[CStrategyLoadSetting\]Account:\S+ , Fom(?:rula|rla)Name: %s, "
           r"startupAutorun: (\w+), ID:" % re.escape(name))
    m = re.search(pat, raw)
    if not m:
        m = re.search(r"CStrategyLoadSetting\].*?Name: %s, startupAutorun: (\w+), ID:"
                      % re.escape(name), raw)
    out["autorun"] = (m.group(1) == "true") if m else False
    return out


def collect(bridge_dir, qmt_dir=None, name="qmt_work_agent"):
    # Bundle 完整性（P0 护栏）：从 <qmt_dir>/python/<name>.py 读文件；
    # 大小写不敏感匹配（Windows 上 QMT_WORK_AGENT.py 与 qmt_work_agent.py 是同一文件）。
    #
    # ★ checked 语义（R19 第 1 轮）：只有**知道 QMT 目录**时才谈得上校验 bundle。
    #   未传 --qmt-dir 时置 ``checked=False``（状态未知），既不能断言"文件在"，
    #   也不能断言"文件不在" —— 后者会把「只验桥目录」的调用方误判成失败，
    #   前者则会让 R17 的污染事故再次以假绿灯形式溜过去。
    #   `diag_qmt_report` 路径总会先解析出真实 qmt_dir（解析不到直接 exit 1），
    #   所以「部署后校验」这一真实场景始终是 checked=True。
    bundle_path = None
    if qmt_dir:
        for cand in (os.path.join(qmt_dir, "python", name + ".py"),
                     os.path.join(qmt_dir, "python", name.upper() + ".py")):
            if os.path.isfile(cand):
                bundle_path = cand
                break
    if not qmt_dir:
        bundle = {
            "checked": False, "ok": None, "path": None, "syntax_ok": None,
            "syntax_error": None, "pollution_ok": None, "pollution_hits": [],
            "size_bytes": 0, "line_count": 0, "encoding": None,
        }
    elif bundle_path:
        bundle = bundle_health(bundle_path)
        bundle["checked"] = True
    else:
        bundle = {
            "checked": True, "ok": False, "path": None, "syntax_ok": False,
            "syntax_error": "bundle_not_found",
            "pollution_ok": True, "pollution_hits": [],
            "size_bytes": 0, "line_count": 0, "encoding": None,
        }
    return {
        "bridge_dir": bridge_dir,
        "bundle": bundle,
        "probe": _load(os.path.join(bridge_dir, "probe_result.json")),
        "status": _load(os.path.join(bridge_dir, "agent_status.json")),
        "req_dir": os.path.isdir(os.path.join(bridge_dir, "req")),
        "resp_dir": os.path.isdir(os.path.join(bridge_dir, "resp")),
        "reg": registration(qmt_dir, name),
        "strategy": name,
    }


def evaluate(data):
    """→ (ok, lines, details)。lines 是给人看的判定；details 给 CI/自动化消费。"""
    lines = []
    details = {"alive": False, "trading_enabled": False, "problems": []}
    probe, status = data["probe"], data["status"]
    # ★ R19 第 1 轮修复：`name` 原先在第 1 段（注册树）才赋值，但第 0 段
    #   （bundle 完整性）已经要用它拼提示语 —— bundle 缺失时抛
    #   `UnboundLocalError`，把「策略没部署」误报成脚本崩溃。
    #   提到函数最前面，全段共用同一个真源。
    name = data.get("strategy", "qmt_work_agent")

    def bad(msg, action):
        details["problems"].append(msg)
        lines.append("  [x] %s" % msg)
        lines.append("      → %s" % action)

    def good(msg):
        lines.append("  [v] %s" % msg)

    lines.append("桥目录: %s" % data["bridge_dir"])

    # ---- 0. Bundle 完整性（P0 护栏 · R17 引入）----
    # ★ 这一段必须在其他运行时证据**之前**：文件被污染（头部被拼了另一支策略）
    #   时策略根本跑不起来，心跳/注入函数等后续证据全都拿不到。先拦下来才谈得上
    #   后续判断，否则会把「bundle 语法错」误诊成「策略未启动」。
    bundle = data.get("bundle") or {}
    bundle_checked = bool(bundle.get("checked"))
    details["bundle_checked"] = bundle_checked
    details["bundle_syntax_ok"] = bool(bundle.get("syntax_ok"))
    details["bundle_pollution_ok"] = bool(bundle.get("pollution_ok"))
    details["bundle_size_bytes"] = bundle.get("size_bytes", 0)
    if not bundle_checked:
        # 只验桥目录（未传 --qmt-dir）时 bundle 状态未知 —— **不**计入 problems，
        # 但也绝不写 [v]，避免读者误以为校验过。
        lines.append("  [-] bundle 未校验（未提供 --qmt-dir，仅能确认桥目录自身）")
    elif not bundle.get("path"):
        bad("策略文件不存在: <QMT>/python/%s.py" % name,
            "先跑 scripts/qmt_agent_deploy.py deploy 发布单文件 agent；"
            "或检查 --qmt-dir 是否传对了")
    elif not bundle.get("syntax_ok"):
        bad("bundle 语法校验失败: %s" % bundle.get("syntax_error"),
            "bundle 可能被手工改坏了（R17 真实事故：文件头被拼了另一支策略）。"
            "重跑 `python scripts/qmt_agent_deploy.py deploy --qmt-dir <QMT> --txt`"
            " 覆盖为干净版本，然后在 QMT 里重新「导入本地策略」")
    else:
        good("bundle 语法校验通过（%d 字节, %d 行, %s）"
             % (bundle.get("size_bytes", 0), bundle.get("line_count", 0),
                bundle.get("encoding") or "?"))
    if bundle_checked and bundle.get("path") and not bundle.get("pollution_ok"):
        bad("bundle 检测到污染签名: %s"
            % ", ".join(bundle.get("pollution_hits", [])),
            "顶部 N 行出现了 pandas/numpy/talib 等非标准库 import —— 说明文件被"
            " 前缀污染（另一支策略被拼进来）。重新部署覆盖。")

    # ---- 1. 策略有没有被客户端**登记**（这是「看不到」的根因面）----
    reg = data.get("reg") or {}
    # `name` 已在函数开头统一取值（bundle 段也要用），此处不再重复赋值。
    if reg.get("log"):
        details["registered"] = reg.get("registered")
        details["autorun"] = reg.get("autorun")
        if reg.get("registered") is False:
            bad("策略 %r 不在客户端注册树里 —— 「模型交易」里自然看不到它" % name,
                "注册只能由会写注册树的 UI 动作触发（拷贝文件无效，已实测："
                "目录里 md5 相同的两个文件一个在列表一个不在）。走 "
                "「模型研究 → 策略区右键 → 导入本地策略」，或"
                "「我的 → 新建策略 → Python 策略 → 粘贴 → 编译」；完成后重启客户端")
        elif reg.get("registered") is True:
            good("策略已登记进注册树")
            if reg.get("autorun"):
                good("已配置 startupAutorun=true（QMT 启动时自动拉起）")
            else:
                lines.append("  [!] 未配自动运行 —— 每次启动 QMT 需手动点运行；"
                             "在策略上右键勾选「自动运行」可免人工")
        else:
            lines.append("  [!] 注册状态无法判定（主日志 %s 里没有注册树记录）—— "
                         "不要据此认为未注册" % os.path.basename(reg.get("log") or ""))
    if reg.get("file_exists") is False:
        bad("策略文件不存在: <QMT>/python/%s.py" % name,
            "先跑 scripts/qmt_agent_deploy.py deploy 发布单文件 agent")
    elif reg.get("file_exists") is True:
        good("策略文件已就位")

    if not (data["req_dir"] and data["resp_dir"]):
        bad("req/ resp/ 子目录不存在",
            "agent 从未启动，或 bridge_dir 配错。检查 agent_config.json 的 bridge_dir "
            "与后端连接配置是否**逐字符一致**")

    # ---- 1. 策略是否在跑（以心跳新鲜度为准）----
    heartbeat_age = None   # 心跳年龄（秒）；None = 没有心跳数据（从未启动 / 文件不存在）
    if not status:
        bad("没有 agent_status.json —— 策略从未成功启动过",
            "先看上面的「是否已登记」：未登记就先做一次导入；已登记仍无心跳，"
            "说明策略没被运行起来（在「模型交易」里点运行，或勾上自动运行）")
    else:
        age = (time.time() * 1000 - float(status.get("ts", 0))) / 1000.0
        heartbeat_age = age
        details["alive"] = age <= STALE_S
        details["trading_enabled"] = bool(status.get("trading_enabled"))
        if age <= STALE_S:
            good("策略在运行（心跳 %.1fs 前，uptime=%ss，py=%s，agent_ver=%s）"
                 % (age, status.get("uptime_s"), status.get("py"),
                    status.get("agent_ver")))
        else:
            bad("心跳已过期 %.0fs（阈值 %ds）—— 策略当前**没有在运行**"
                % (age, STALE_S),
                "QMT 里确认策略状态为「运行中」；若 QMT 刚重启，勾选该策略的「自动运行」")
        if status.get("last_error"):
            bad("主循环最近异常: %s" % status["last_error"],
                "把 QMT 输出面板里 [qmt_work_bigqmt_agent] 的完整堆栈贴出来定位")

    # ---- 2. 环境自检 ----
    if not probe:
        bad("没有 probe_result.json —— 自检没跑完",
            "策略启动会在 init 阶段写该文件；缺失说明 init 就失败了（看 QMT 输出面板）")
    else:
        steps = dict((s["name"], s) for s in probe.get("steps", []))
        w = steps.get("bridge_dir_write")
        if w and w["ok"]:
            good("bridge_dir 可写（文件桥生死项通过）")
        else:
            bad("bridge_dir 不可写: %s" % (w["detail"] if w else "未测"),
                "QMT 进程对该目录无权限（常见于装在 Program Files）；把 bridge_dir "
                "换到用户目录，两侧同步改")
        # ★ 措辞纪律（与 agent 侧同源，见 qmt_api._need 注释）：
        #   probe_result.json 是**某一次运行**的快照。策略当前没在跑时它必然陈旧，
        #   此时只能说「陈旧快照里未见 X」——**绝不能**据此断言当前终端没有该能力
        #   （那正是 "未解析到 ≠ 终端没有" 这条纪律要防的事）。
        #   因此陈旧时的缺项降级为提示（`lines`），不计入致命 `problems`。
        probe_stale = (heartbeat_age is None) or (heartbeat_age > STALE_S)
        details["probe_stale"] = probe_stale
        if probe_stale:
            lines.append("  [!] probe_result.json 是**陈旧快照**（心跳距今 %s）—— "
                         "它只反映历史上那一次运行，不代表当前 bundle 的能力"
                         % ("未知" if heartbeat_age is None else "%.0fs" % heartbeat_age))
        details["injected"] = probe.get("injected", [])
        details["ctx_methods"] = probe.get("ctx_methods", [])
        good("注入函数 %d 个: %s%s" % (len(details["injected"]),
                                     ", ".join(sorted(details["injected"])[:12]) or "(空)",
                                     "（陈旧快照）" if probe_stale else ""))
        good("ContextInfo 方法面 %d 个: %s" % (
            len(details["ctx_methods"]), ", ".join(details["ctx_methods"]) or "(空)"))

        missing_t = [f for f in NEED_FOR_TRADE if f not in details["injected"]]
        missing_q = [f for f in NEED_FOR_QUERY if f not in details["injected"]]
        if missing_t:
            if probe_stale:
                lines.append("  [!] 陈旧快照里未见交易函数: %s —— **不能据此断言当前终端没有**；"
                             "注册并运行策略后重跑本工具才会得到真实数据" % ", ".join(missing_t))
            else:
                bad("未捕获交易函数: %s" % ", ".join(missing_t),
                    "① 确认运行的入口是 python/qmt_work_agent.py（单文件态）而不是子目录里的旧文件；"
                    "② 该券商版本可能不开交易函数注入")
        else:
            good("交易函数已捕获（passorder/cancel）")
        if missing_q:
            if probe_stale:
                lines.append("  [!] 陈旧快照里未见 get_trade_detail_data —— 同样不代表当前缺失，需运行后重测")
            else:
                bad("未捕获 get_trade_detail_data —— 资金/持仓/委托/成交查询全不可用",
                    "确认跑的是单文件 agent；若仍缺失，多半是该终端版本模型研究环境不下发交易函数")
        else:
            good("查询函数已捕获（get_trade_detail_data）")

    # ---- 3. 运行时能力 ----
    meta = (status or {}).get("meta") or (probe or {}).get("meta") or {}
    if meta:
        details["callback_bound"] = meta.get("callback_bound")
        good("actions=%s" % ",".join(meta.get("actions", [])[:6]))
        if meta.get("callback_bound"):
            good("终端的实时回调已被调用过（事件走推送而非轮询差分）")
        else:
            good("回调未绑定 → 事件按轮询差分合成（合法降级，不是错误）")
        if meta.get("direction_unknown"):
            lines.append("  [!] direction_unknown=%s —— 部分委托的买卖方向无法判定"
                         % meta["direction_unknown"])
    return (not details["problems"]), lines, details


def main(argv=None):
    ap = argparse.ArgumentParser(description="验证 QMT agent 是否真的在跑")
    ap.add_argument("--bridge-dir", default=os.environ.get("QMT_BRIDGE_DIR",
                                                           DEFAULT_BRIDGE))
    ap.add_argument("--qmt-dir", default=os.environ.get("QMT_DIR"),
                    help="QMT 安装目录（用于核对策略是否已登记进注册树）")
    ap.add_argument("--strategy", default="qmt_work_agent", help="策略名")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--wait", type=int, default=0,
                    help="最多等待 N 秒直到心跳新鲜（用于刚启动策略后）")
    args = ap.parse_args(argv)

    qmt = args.qmt_dir
    if not qmt:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import qmt_agent_deploy as dep
            qmt = dep.find_qmt_dir(None)
        except Exception:
            qmt = None

    deadline = time.time() + max(0, args.wait)
    while True:
        data = collect(args.bridge_dir, qmt, args.strategy)
        ok, lines, details = evaluate(data)
        if ok or time.time() >= deadline:
            break
        time.sleep(3)

    if args.json:
        print(json.dumps({"ok": ok, "details": details}, ensure_ascii=False))
    else:
        print("=" * 66)
        print("QMT agent 验证: %s" % ("通过" if ok else "未通过"))
        print("=" * 66)
        for ln in lines:
            print(ln)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
