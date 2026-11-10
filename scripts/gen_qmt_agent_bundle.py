# -*- coding: utf-8 -*-
"""把 agent_bigqmt 的「入口 + 执行器」两个源文件打成**单文件策略**。

为什么必须存在这个生成器
------------------------
1. **QMT 只把下单/查询等函数注入「被挂载的那一个入口文件」的命名空间。**
   如果入口只做 `from BIGQMT_AGENT import *`，`globals()` 指向被导入模块而
   不是入口 → 唯一来源捕获拿到一堆普通函数、一个注入函数都拿不到，能力
   探测全灭。所以入口必须**自己就是实现**（单文件内联），不能是薄壳转发。

2. **单文件便于「导入注册」这一步。** QMT 的策略列表是客户端**持久化注册树**
   （`config/user/root/{configFormula,lua,UiSettingConfig}`，运行期被整文件
   字节锁），**不是策略目录扫描**。实测判据：目录里放两个 **md5 完全相同**
   的文件，重启后一个在列表、一个不在 —— 所以「把 .py 拷进 `python/` 就会
   出现在模型交易里」是错的，注册必须走会写注册树的 UI 动作（新建/导入）。
   单文件 = 导入时只需选一个文件，不涉及相对导入。

3. 单一真源留在 `backend/agent_bigqmt/`（便于维护/测试），发布产物由本脚本
   生成，杜绝「源码改了、QMT 里跑的还是旧文件」这类静默漂移。

编码
----
QMT 内置的解释器是 ``bin.x64/pythonw.exe`` = **Python 3.6.8**。2026-10-15 在本机
把它当真实解释器逐形态跑过一遍（结论修正了 2026-10-08 的「GBK 不能跑」）：

===================  ==========================================
utf-8（LF / CRLF）    通过
utf-8 + BOM          通过
**gbk + CRLF**        **通过**（连跑 3 次 3/3）
gbk + LF             **必然 SyntaxError**
gb18030 + cookie     失败（`encoding problem: gb18030`）
===================  ==========================================

★ 所以 ``--encoding gbk`` 现在是**受支持的真跑口径**，但有两条硬前提，本生成器
  都自动处理：① 行尾必须 CRLF（见 ``recode`` 的长注释）；② 内容必须 GBK 可表示
  （``gbk_safe`` 做白名单替换，替换不掉的直接报错，不静默写坏）。
  ``gb18030`` 仍是实测失败项，只作兼容保留。

无论哪种编码，落盘后都请用 ``qmt_agent_deploy.py deploy`` 的前置门禁
（真解释器跑一遍）来判定 —— 「开发机能编译」从来都不等于「QMT 能跑」。

用法::

    python scripts/gen_qmt_agent_bundle.py --out dist/qmt_work_agent.py
    python scripts/gen_qmt_agent_bundle.py --out X.py --stamp "..."
    python scripts/gen_qmt_agent_bundle.py --check          # 只做自检
"""
from __future__ import print_function

import argparse
import ast
import io
import json
import os
import pprint
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _resolve_agent_src():
    """解析 agent 分片真源目录。

    支持三种布局：
      1. ``<repo>/backend/agent_bigqmt``  —— 源码开发态
      2. ``<internal>/agent_bigqmt``      —— PyInstaller 打包态
        （``--add-data=<backend>/agent_bigqmt;agent_bigqmt``）
      3. ``<internal>/backend/agent_bigqmt`` —— 兼容旧打包清单（保留原路径）

    找不到任何候选时回退到路径 1（保持向后兼容，报错信息与旧版一致）。
    背景（2026-10-08 R27）：客户端安装包内没有 backend/ 目录，
    而 `_SRC` 曾硬编码 ``os.path.join(_ROOT, "backend", "agent_bigqmt")``
    ⇒ 打包态下 `gen_qmt_agent_bundle.build()` 一调就 FileNotFoundError，
    `/api/v1/qmt-agent/bundle` 直接 500，前端「一键部署」点了没反应。
    """
    for cand in (
        os.path.join(_ROOT, "backend", "agent_bigqmt"),  # 源码开发态
        os.path.join(_ROOT, "agent_bigqmt"),              # PyInstaller 打包态
    ):
        if os.path.isdir(cand):
            return cand
    return os.path.join(_ROOT, "backend", "agent_bigqmt")


_SRC = _resolve_agent_src()

_PART_FUTURE = "from __future__ import print_function"

#: 生成物必须包含的顶层名字（少一个，QMT 侧就会静默不工作）。
_REQUIRED = (
    "init", "handle_init", "handlebar", "after_init",
    "order_callback", "deal_callback",
    "Executor", "ActionError", "capture_qmt_injected_funcs",
    "load_config", "self_probe", "write_status", "_STATE", "_AGENT_VERSION",
    # ★ 独立进程自举（2026-10-08 加）：QMT 的「运行」是
    #   `pythonw.exe -u <策略.py> <userdata> <ts>`，没有这几个名字就是
    #   「启动即退出」（日志里 return code 非 0 + try stop）。
    "run_standalone", "should_autorun", "_resolve_self_dir",
    # ★ 自有顶层名字台账：capture_qmt_injected_funcs 靠它把「我们自己的函数」
    #   从「终端注入的函数」里剔除，否则 probe 会把 30+ 个自有名字报成注入面。
    "_OWN_NAMES",
    # ★ 新规闭环（2026-10-31）：内联进来的 position_source / closed_loop 的名字。
    #   缺任何一个都意味着「内联没接上」，而症状是运行时 NameError —— 真机才炸。
    "PositionSource", "CounterUnavailable", "parse_plan", "PlanError",
    "Guard", "decide", "emit", "tick", "in_window", "hhmm_of",
    "EMBEDDED_PLAN", "EMBEDDED_BUNDLE_ID",
)

#: `_OWN_NAMES` 在源码里的占位（生成时按 bundle 的 AST 替换成真值）。
_OWN_ANCHOR = "_OWN_NAMES = frozenset()"

#: 计划 / bundle 标识在源码里的占位锚点（`--plan` / `--bundle-id` 时替换）。
_PLAN_ANCHOR = "EMBEDDED_PLAN = None"
_BUNDLE_ANCHOR = 'EMBEDDED_BUNDLE_ID = ""'

_HEADER = '''# -*- coding: utf-8 -*-
"""qmt_work 大 QMT 桥接 agent —— 单文件策略（自动生成，请勿手改）。

来源   : backend/agent_bigqmt/{{qmt_api.py, agent_runtime.py, position_source.py,
         closed_loop.py, BIGQMT_AGENT.py}}
生成器 : scripts/gen_qmt_agent_bundle.py
生成于 : {stamp}

部署（唯一正确姿势）
--------------------
1. 把本文件放到 QMT 的 Python 策略目录顶层（本机实测：`P:\\\\stock\\\\gd_qmt\\\\python\\\\`），
   即 `python/qmt_work_agent.py`。
2. ★ **拷贝文件不会让它出现在「模型交易」里** —— 列表来自客户端持久化的
   注册树，不是目录扫描（实测：两个 md5 完全相同、同目录同大小的 .py，
   重启后一个在列表、一个不在）。必须走会写注册树的 UI 动作二选一：
     A) 「模型研究」→ 策略区右键 → 导入本地策略 / 本地.rzrk导入 → 选中本文件；
     B) 「我的」→ 新建策略 → Python 策略 → 粘贴本文件内容 → 编译。
3. 注册后在「模型交易」里选中本策略运行；勾上「自动运行」，
   QMT 每次启动即自动拉起（`startupAutorun=true` 记在 UiSettingConfig）。

自查是否注册成功：`python scripts/qmt_strategy_list_probe.py --target qmt_work_agent`
（或 `scripts/qmt_agent_verify.py`，它连「在不在跑」一起判）。

配置文件来源（按序查找，找到即用；也可由生成器 --embed-config 内嵌）：
  1. <策略目录>/agent_config.json
  2. <策略目录>/agent_bigqmt/agent_config.json
  3. <策略目录>/../agent_config.json

运行时产物（全部写在 bridge_dir 下，外部端直接读）：
  - probe_result.json  启动自检证据（注入函数/ContextInfo 方法面/写权限）
  - agent_status.json  心跳（存活、uptime、trading_enabled、主循环最近异常）
  - events.ndjson      委托/成交/行情事件
  - req/ resp/         请求与响应

★ 新规闭环（`--plan` 烘焙了计划时）**以上产物一个都不会生成**：2026-10-23 起
  策略进程禁文件读写，`open()` 直接抛 `PermissionError: Forbidden FileIO`。
  闭环模式下事件改走 `print("@@QMTW|{{json}}")` 一行，由客户端日志带出，
  外部 `backend/connectors/bigqmt_log_tail.py` 跟日志读。回传记录种类：
  BOOT / STATUS / DAY / ORDER / REJECT / TRADE / LOST / HALT / READONLY /
  RECOVER / WARN / DROP / STOP。

py3.6 兼容（QMT 内置解释器）：仅标准库、无 dataclasses、无 walrus、无 f-string 之外的 3.7+ 语法。
"""

from __future__ import print_function

# ===========================================================================
# BEGIN qmt_api.py（内联）
# ===========================================================================
'''

_MID = '''
# ===========================================================================
# END qmt_api.py
# BEGIN agent_runtime.py（内联）
# ===========================================================================
'''

_MID2 = '''
# ===========================================================================
# END agent_runtime.py
# BEGIN position_source.py（内联 · 新规闭环）
# ===========================================================================
'''

_MID3 = '''
# ===========================================================================
# END position_source.py
# BEGIN closed_loop.py（内联 · 新规闭环）
# ===========================================================================
'''

_MID4 = '''
# ===========================================================================
# END closed_loop.py
# BEGIN BIGQMT_AGENT.py（内联）
# ===========================================================================
'''

_TAIL = '''
# ===========================================================================
# END BIGQMT_AGENT.py
# ===========================================================================
'''

#: 会被内联的模块名（导入行必须全部剥掉，否则 bundle 里是无效/循环导入）。
_INLINED_MODULES = ("qmt_api", "agent_runtime", "position_source", "closed_loop")


def _read(name):
    with io.open(os.path.join(_SRC, name), "r", encoding="utf-8") as fh:
        return fh.read()


def _strip_future(src):
    """去掉 `from __future__` 行（bundle 顶部统一保留一条）。"""
    out = []
    for line in src.splitlines():
        if line.strip() == _PART_FUTURE:
            continue
        out.append(line)
    return "\n".join(out)


def _strip_agent_imports(src):
    """把 agent 里 `from <被内联模块> import ...` 换成内联说明。

    内联后这些名字都已是本文件的顶层名字，导入行会变成循环/无效导入。
    开发态需要这些导入来保证 IDE 补全与单独 import 可用；发布态必须剥掉。

    ★ 必须处理**多行括号导入**（`from x import (\n  a,\n  b,\n)`）：
      只剥首行会留下悬空的续行与未闭合括号，bundle 直接 SyntaxError。
    """
    out = []
    skipping = False
    depth = 0
    for line in src.splitlines():
        stripped = line.strip()
        if not skipping:
            note = None
            for module in _INLINED_MODULES:
                if stripped.startswith("from %s import" % module):
                    note = ("# bundle: %s 的名字已在本文件内联定义（无外部依赖）"
                            % module)
                    break
            if note is not None:
                indent = line[:len(line) - len(line.lstrip())]
                out.append(indent + note)
                depth = line.count("(") - line.count(")")
                skipping = depth > 0
                continue
        else:
            depth += line.count("(") - line.count(")")
            if depth <= 0:
                skipping = False
            continue
        out.append(line)
    return "\n".join(out)


def _embed(src, cfg):
    """把 EMBEDDED_CONFIG = None 替换成实际配置（一键部署，零外部文件）。

    ★ 必须用 **Python 字面量**渲染，不能用 json.dumps —— JSON 的
    true/false/null 在 Python 源码里是未定义名字，会把「部署」变成
    「策略一启动就 NameError」。这条由 tests/test_bigqmt_agent_bundle.py 锁住。
    """
    payload = pprint.pformat(dict(cfg), width=96, sort_dicts=False)
    needle = "EMBEDDED_CONFIG = None"
    if needle not in src:
        raise SystemExit("生成失败：agent 源码里找不到 `%s` 锚点" % needle)
    lines = []
    for line in src.splitlines():
        if line.strip() == needle:
            lines.append("# 由生成器 --embed-config 注入（后端一键部署用）")
            lines.append("EMBEDDED_CONFIG = " + payload)
            continue
        lines.append(line)
    return "\n".join(lines)


def _load_plan_parser():
    """拿到策略端**同一份** ``parse_plan``（单一真源，不在这里重写校验规则）。

    ★ 为什么必须复用而不是复制：生成器与策略端各写一套校验，就会出现
      「生成时通过、真机只读」—— 而真机只读的表现是策略安静地一单不发，
      排查成本极高。这里 import 的是 ``agent_bigqmt/closed_loop.py`` 本身。
    """
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)
    from closed_loop import PlanError, parse_plan  # noqa: F401
    return PlanError, parse_plan


def _validate_plan(plan):
    """校验计划；不合法抛 ``PlanError``（由调用方转成 [FAIL]）。"""
    _err, parse_plan = _load_plan_parser()
    return parse_plan(plan)


def _extract_embedded_plan(text):
    """从**已组装**的 bundle 文本里取出烘焙进去的计划（没有则 None）。

    ★ 用 AST 而不是文本匹配：注释里也写着 ``EMBEDDED_PLAN``，
      文本匹配会把「解释这个变量」的说明行当成真计划。
    """
    try:
        tree = ast.parse(text, filename="<bundle>", feature_version=(3, 6))
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id == "EMBEDDED_PLAN"):
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
            return value if isinstance(value, dict) and value else None
    return None


def _replace_anchor(src, needle, replacement_lines):
    """把**独占一行**的 `needle` 换成若干行（找不到就 SystemExit）。"""
    if needle not in src:
        raise SystemExit("生成失败：agent 源码里找不到 `%s` 锚点" % needle)
    lines = []
    for line in src.splitlines():
        if line.strip() == needle:
            lines.extend(replacement_lines)
            continue
        lines.append(line)
    return "\n".join(lines)


def _embed_plan(src, plan, bundle_id=""):
    """把盘前计划烘焙进 ``EMBEDDED_PLAN``（新规闭环的唯一入站通道）。

    ★ 为什么是「烘进源码」而不是「放进配置文件」：2026-10-23 起策略进程禁文件
      读写，``open()`` 直接抛 ``PermissionError: Forbidden FileIO``。计划必须在
      **策略被编译之前**就固化在源码里 —— 这是新规下唯一还开着的入站通道
      （改源码 = 一次人工动作，天然满足「不被外部信号驱动」）。
    ★ 必须用 Python 字面量渲染（``pprint``），不能 json.dumps：JSON 的
      ``true/false/null`` 在 Python 源码里是未定义名字 ⇒ 策略一启动就 NameError。
    """
    payload = pprint.pformat(dict(plan), width=96, sort_dicts=False)
    src = _replace_anchor(src, _PLAN_ANCHOR, [
        "# 由生成器 --plan 注入：盘前计划（新规闭环的唯一入站通道）",
        "EMBEDDED_PLAN = " + payload,
    ])
    src = _replace_anchor(src, _BUNDLE_ANCHOR, [
        "# 由生成器 --bundle-id 注入：回传记录里带上它，对账时区分部署产物",
        "EMBEDDED_BUNDLE_ID = %r" % (bundle_id or ""),
    ])
    return src


def _top_level_names(text):
    """bundle 自己定义的顶层名字（函数/类/赋值/导入的绑定名）。

    这是 ``_OWN_NAMES`` 的真源：运行时 ``capture_qmt_injected_funcs`` 靠它把
    「我们自己的函数」从「终端注入的函数」里剔除。**由 AST 算出来，不手抄** ——
    手抄一份名字表就等着它随重构腐烂，然后把桥的 bug 伪装成终端缺能力。
    """
    tree = ast.parse(text, filename="<bundle>", feature_version=(3, 6))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
    return names


def _inject_own_names(text):
    """把 ``_OWN_NAMES = frozenset()`` 替换成 bundle 实际顶层名字的 frozenset。"""
    if _OWN_ANCHOR not in text:
        raise SystemExit("生成失败：bundle 里找不到 `%s` 锚点" % _OWN_ANCHOR)
    names = sorted(_top_level_names(text))
    lines = ["_OWN_NAMES = frozenset(["]
    for i in range(0, len(names), 4):
        lines.append("    " + " ".join('"%s",' % n for n in names[i:i + 4]))
    lines.append("])")
    literal = "\n".join(lines)
    out = []
    for line in text.splitlines():
        if line.strip() == _OWN_ANCHOR:
            out.append("# 由生成器按 bundle 的 AST 注入（运行时用于剔除自有名字）")
            out.append(literal)
            continue
        out.append(line)
    return "\n".join(out)


def _has_main_guard(tree):
    """bundle 是否自带 ``if __name__ == "__main__"`` 自举入口。"""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        candidates = []
        if isinstance(test, ast.Compare):
            candidates.append(test.left)
            candidates.extend(test.comparators)
        elif isinstance(test, ast.BoolOp):
            for v in test.values:
                if isinstance(v, ast.Compare):
                    candidates.append(v.left)
                    candidates.extend(v.comparators)
        has_name = any(isinstance(n, ast.Name) and n.id == "__name__"
                       for n in candidates)
        has_main = any(isinstance(n, ast.Constant) and n.value == "__main__"
                       for n in candidates)
        if has_name and has_main:
            return True
    return False


def build(embed=None, stamp="", plan=None, bundle_id=""):
    api = _strip_future(_read("qmt_api.py"))
    runtime = _strip_future(_read("agent_runtime.py"))
    positions = _strip_future(_read("position_source.py"))
    loop = _strip_future(_read("closed_loop.py"))
    agent = _strip_agent_imports(_strip_future(_read("BIGQMT_AGENT.py")))
    if embed:
        agent = _embed(agent, embed)
    if plan:
        agent = _embed_plan(agent, plan, bundle_id)
    body = (_HEADER.format(stamp=stamp) + api + _MID + runtime + _MID2
            + positions + _MID3 + loop + _MID4 + agent + _TAIL)
    if not body.endswith("\n"):
        body += "\n"
    # ★ _OWN_NAMES 必须在**整份 bundle 组装完之后**按 AST 注入 —— 它要覆盖
    #   qmt_api.py + agent_runtime.py + position_source.py + closed_loop.py
    #   + BIGQMT_AGENT.py 五个来源的全部顶层名字。
    return _inject_own_names(body)


def check(text):
    """静态自检：语法 + py3.6 特性 + 必需入口齐备 + 独立进程自举入口。"""
    problems = []
    try:
        tree = ast.parse(text, filename="<bundle>", feature_version=(3, 6))
    except SyntaxError as exc:
        return ["语法/版本不兼容: %s" % exc]
    top = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            top.add(node.name)
        elif isinstance(node, ast.ClassDef):
            top.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    top.add(t.id)
    for name in _REQUIRED:
        if name not in top:
            problems.append("缺少顶层名字: %s" % name)
    # ★ 内联完整性用 **AST** 判，不用文本匹配（2026-10-09 修）：
    #   文本匹配 `"from qmt_api import" in text` 会把**注释里提到该写法**的说明文字
    #   判成违规 —— 实测就是这么误伤的（注释解释「必须用无别名形式导入」直接触发
    #   `bundle 生成有问题`，而真实导入早已被正确剥掉）。
    #   本仓早已写过这条教训（见 `check_bigqmt_agent_py36.py` 的「只扫代码，不扫字符串」）。
    #   内联后 qmt_api 的名字都在本文件顶层 ⇒ 任何仍存在的 qmt_api 导入都是错的。
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _INLINED_MODULES:
            problems.append(
                "仍存在 %s 导入语句（第 %d 行，内联不完整）"
                % (node.module, node.lineno))
    if text.count("from __future__ import print_function") != 1:
        problems.append("`from __future__` 行数不为 1")
    if "\t" in text:
        problems.append("存在 Tab 缩进（QMT 内置解释器对混排敏感）")
    if _OWN_ANCHOR in text:
        problems.append("`_OWN_NAMES` 仍是占位 frozenset()（生成器注入没生效）——"
                        "会导致 probe 把自有函数报成终端注入函数")
    if not _has_main_guard(tree):
        problems.append(
            "缺少 `if __name__ == \"__main__\":` 自举入口 —— QMT 的「运行」是把本文件"
            "当独立进程拉起的（pythonw.exe -u <策略.py> <userdata> <ts>），"
            "没有自举 = 进程瞬间退出 = 模型交易里看到「启动即停止」")
    # ★ 烘焙进 bundle 的计划必须在这里就校验一次。
    #   否则「生成成功、部署成功、真机只读」—— 而只读的表现是策略安静地
    #   一单不发，排查成本极高（要看日志里那条 READONLY 才知道）。
    plan = _extract_embedded_plan(text)
    if plan:
        try:
            _validate_plan(plan)
        except Exception as exc:                      # noqa: BLE001
            problems.append(
                "EMBEDDED_PLAN 不合法（策略启动会进只读、一单不发）: %s" % exc)
    return problems


_CODING_RE = re.compile(r"^#.*?coding[:=]\s*[-\w.]+.*$")

#: GBK 表示不了的字符 → GBK 内的等价写法。
#:
#: ★ 2026-10-15 实测：整份 bundle 里**只有这三个** GBK 编不出来。
#:   `★`(A1EE) / `→`(A1FA) / `—`(A1AA) 都在 GBK 码表内 —— **不要**顺手替换，
#:   那只会凭空制造一份大 diff，还会把「哪些字符真的有问题」这条信息埋掉。
_GBK_FALLBACK = {
    u"\u21d2": u"=>",   # ⇒  重言式箭头，只出现在注释/文案里
    u"\u26a0": u"!",    # ⚠
    u"\ufe0f": u"",     # U+FE0F 变体选择符（只跟在 ⚠ 后面，直接去掉）
}


def gbk_safe(text):
    """把 GBK 表示不了的字符换成等价写法。→ (新文本, 替换明细 {字符: 次数})。

    ★ 绝不用 ``errors='replace'`` 静默写坏源码（那是本项目最忌讳的一类故障）。
      本函数只做**显式白名单**替换；遇到白名单外的字符直接抛错 ——
      宁可生成失败，也不要产出一份在 QMT 里跑不起来的策略。
    """
    repl = {}
    out = []
    for ch in text:
        if ch in _GBK_FALLBACK:
            repl[ch] = repl.get(ch, 0) + 1
            out.append(_GBK_FALLBACK[ch])
            continue
        try:
            ch.encode("gbk")
        except UnicodeEncodeError:
            raise SystemExit(
                "生成失败：字符 %r (U+%04X) 无法用 GBK 表示，且不在 _GBK_FALLBACK "
                "白名单里。请先在 gen_qmt_agent_bundle._GBK_FALLBACK 登记它的等价写法。"
                % (ch, ord(ch)))
        out.append(ch)
    return u"".join(out), repl


def recode(text, enc):
    """把 bundle 转成指定源码编码。→ (text, 实际编码, 告警列表, 行尾)。

    QMT 官方口径是 GBK。做法：去掉所有 PEP263 coding cookie（bundle 里内联了
    多个源文件的 cookie，全部清掉只留一条），改成 `# -*- coding: <enc> -*-`。

    ★★ 2026-10-15 真机实测（本机 ``bin.x64/pythonw.exe`` = Python 3.6.8），
       修正了 2026-10-08 「GBK 不能跑」的结论 —— **GBK 能跑，但必须 CRLF**：

       ===========================  ==================================
       utf-8（LF 或 CRLF）          通过
       utf-8 + BOM                  通过
       gbk   + LF                   **必然 SyntaxError**
       gbk   + CRLF                 通过（连跑 3 次 3/3）
       gb18030 + cookie             失败
       ===========================  ==================================

       GBK+LF 的报错极具误导性：`invalid character in identifier`，指向**模块
       docstring 正中间**的一行中文（本机实测第 7 行 `部署（唯一正确姿势）`）——
       看着像「某个中文字写错了」，其实是内置 py3.6 的 tokenizer 在 LF 行尾下读
       「带编码 cookie 的文件」时丢掉了开头两行，docstring 的三引号因此没生效，
       中文行被当成代码解析。同一份字节换成 CRLF 立刻通过。
       ⇒ **行尾是这条链路上唯一不明显的开关，必须由调用方写死。**

    返回第 4 项 ``newline`` 是硬要求：调用方**必须**用它落盘，
    否则又会退回到「GBK+LF 必然失败」。
    """
    notes = []
    key = enc.lower().replace("_", "-")
    if key in ("utf-8", "utf8"):
        return text, "utf-8", notes, "\n"

    if key not in ("gbk", "gb18030"):
        raise SystemExit("不支持的编码: %s" % enc)

    if key == "gb18030":
        notes.append(
            "gb18030 在 QMT 内置 Python 3.6.8 上实测**失败**"
            "（`SyntaxError: encoding problem: gb18030`，进程 return code:1）。"
            "要真跑请用 --encoding gbk（本机 3/3 通过）。")

    text, repl = gbk_safe(text)
    if repl:
        notes.append(
            "为兼容 GBK 替换了 %d 个字符：%s（仅出现在注释/文案，不影响行为）"
            % (sum(repl.values()),
               "、".join("%s→%s×%d" % (c, _GBK_FALLBACK[c], n)
                         for c, n in sorted(repl.items()))))
    notes.append(
        "行尾已强制 CRLF —— QMT 内置 py3.6 的 tokenizer 读 **LF 行尾** 的 GBK 源码时"
        "会丢掉开头两行，docstring 失效 ⇒ `SyntaxError: invalid character in "
        "identifier`（本机连跑 3 次 3/3 复现；同内容 CRLF 3/3 通过）。"
        "**不要**把本文件再存成 LF。")

    kept = [ln for ln in text.split("\n") if not _CODING_RE.match(ln)]
    return u"# -*- coding: %s -*-\n" % key + u"\n".join(kept), key, notes, "\r\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 QMT 单文件 agent bundle")
    ap.add_argument("--out", help="输出文件路径")
    ap.add_argument("--embed-config", help="把该 agent_config.json 内嵌进 bundle")
    ap.add_argument("--plan", help="把该 plan.json 烘焙进 EMBEDDED_PLAN（新规闭环）。"
                                  "给了它就是闭环模式：不读配置文件、不写心跳、"
                                  "不轮询 req/，事件走日志行回传。")
    ap.add_argument("--bundle-id", default="",
                    help="bundle 标识（写进每条回传记录，对账时区分部署产物）。"
                         "缺省由 plan id 派生。")
    ap.add_argument("--check", action="store_true", help="只做自检（不写文件）")
    ap.add_argument("--stamp", default="", help="写进文件头的生成时间戳")
    ap.add_argument("--encoding", default="utf-8",
                    choices=["utf-8", "gbk", "gb18030"],
                    help="输出源码编码。utf-8（默认）与 gbk 都在本机 QMT 内置 "
                         "Python 3.6.8 上实测跑通；**gbk 会自动改用 CRLF 行尾**"
                         "（LF 行尾的 GBK 源码必然 SyntaxError，详见 recode 文档）。"
                         "gb18030 实测失败，仅作兼容保留。")
    ap.add_argument("--txt", action="store_true",
                    help="同目录额外产一份 .txt 副本（内容与 .py 完全相同，后缀改为 .txt）。"
                         "用途：QMT「新建策略 → 粘贴代码」路径 —— 记事本双击打开 .txt 比 IDE 打开 .py "
                         "快得多，剪贴板粘贴 52 KB 稳得多。默认关闭（主路径 A「导入本地策略」用 .py 即可）。")
    args = ap.parse_args(argv)

    embed = None
    if args.embed_config:
        with io.open(args.embed_config, "r", encoding="utf-8") as fh:
            embed = json.load(fh)
        embed.pop("_config_path", None)

    plan = None
    bundle_id = args.bundle_id
    if args.plan:
        with io.open(args.plan, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
        # ★ 生成时就校验：不合法直接失败，不要产出一份「真机只读」的产物。
        try:
            _validate_plan(plan)
        except Exception as exc:                      # noqa: BLE001
            print("[FAIL] --plan 校验失败: %s" % exc)
            return 1
        if not bundle_id:
            bundle_id = "b-%s" % str(plan.get("id") or "plan")
        if embed is None:
            embed = {}
        # ★ trading_enabled 由**生成器**显式写 true：烘焙了计划就代表操作者的意图，
        #   但这条必须是一个可审计的动作（写进产物），而不是 agent 里的默认值推断。
        embed.setdefault("trading_enabled", True)

    text = build(embed=embed, stamp=args.stamp or "(未指定)",
                 plan=plan, bundle_id=bundle_id)
    problems = check(text)
    if problems:
        for p in problems:
            print("[FAIL] " + p)
        return 1
    if plan is not None:
        # 闭环的一条「必炸」前提，在生成时就拦下 —— 别等真机才发现策略只读。
        if not (embed or {}).get("account_id"):
            print("[FAIL] 闭环 bundle 的配置里没有 account_id —— 柜台查询没有默认账户，"
                  "agent 不会猜（猜一个等于拿别人的持仓去调仓），策略会以只读启动。"
                  "请用 --embed-config 提供，或在配置里加 account_id。")
            return 1
        if not (embed or {}).get("trading_enabled"):
            print("[WARN] 闭环 bundle 的 trading_enabled=false —— 计划会加载但**不会下单**"
                  "（agent 不会替你把它改成 true）。")
        # 建仓型计划的换手上限必须容得下目标权重合计，否则每腿都被闸门裁掉。
        total = sum(float(t.get("w") or 0.0)
                    for t in (plan.get("rule") or {}).get("target") or [])
        cap = float((plan.get("guard") or {}).get("max_turnover") or 0.5)
        if cap + 1e-9 < total:
            print("[WARN] guard.max_turnover=%.2f 小于目标权重合计 %.2f —— "
                  "从空仓建满仓本身就是 %.0f%% 换手，超出的腿会被 DROP。"
                  "已有仓位做小幅再平衡时才该配小值。"
                  % (cap, total, total * 100))
    print("[OK] bundle 自检通过: %d 字符, %d 行%s"
          % (len(text), len(text.splitlines()),
             "（闭环模式: plan=%s bundle=%s）" % (plan.get("id"), bundle_id)
             if plan is not None else ""))
    text, enc, notes, newline = recode(text, args.encoding)
    for n in notes:
        print("[WARN] " + n)
    if args.check or not args.out:
        return 0

    out = os.path.abspath(args.out)
    d = os.path.dirname(out)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(out, "w", encoding=enc, newline=newline) as fh:
        fh.write(text)
    print("[OK] 已写出: %s (%d bytes, encoding=%s, newline=%s)"
          % (out, os.path.getsize(out), enc,
             "CRLF" if newline == "\r\n" else "LF"))
    if args.txt:
        # 路径 B 辅助：同内容 .txt 副本，记事本双击打开粘贴更顺手
        out_txt = os.path.splitext(out)[0] + ".txt"
        with io.open(out_txt, "w", encoding=enc, newline=newline) as fh:
            fh.write(text)
        print("[OK] 已写出 .txt 副本（供路径 B 粘贴用）: %s (%d bytes)"
              % (out_txt, os.path.getsize(out_txt)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
