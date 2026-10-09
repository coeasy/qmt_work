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
★ **部署必须用 UTF-8**（2026-10-08 真机实测，血的教训）：

QMT 内置的解释器是 ``bin.x64/pythonw.exe`` = **Python 3.6.8**，它的 tokenizer
在处理带 ``#coding:gbk`` / ``#coding:gb18030`` cookie 的源文件时，会在特定内容下
报 ``SyntaxError: encoding problem: <enc>`` 或 ``SyntaxError: invalid token``
（实测：706 字节的 gb18030 片段通过、707 字节就失败；52 KB 的 gb18030 全量稳定失败），
进程随即 ``return code:1`` —— 在「模型交易」里表现为**点了运行立刻停止**，
而且**一条自检都不会落盘**（最难查的一类故障）。

同一份内容改成 UTF-8（带或不带 cookie）在 67 KB 全量中文下**稳定跑通**。
所以本生成器默认 ``--encoding utf-8``，并且 ``gbk`` / ``gb18030`` 仅作为
「本机不跑、只给 QMT 编辑器看」的兼容选项保留（会打警告）。

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
)

#: `_OWN_NAMES` 在源码里的占位（生成时按 bundle 的 AST 替换成真值）。
_OWN_ANCHOR = "_OWN_NAMES = frozenset()"

_HEADER = '''# -*- coding: utf-8 -*-
"""qmt_work 大 QMT 桥接 agent —— 单文件策略（自动生成，请勿手改）。

来源   : backend/agent_bigqmt/qmt_api.py + backend/agent_bigqmt/BIGQMT_AGENT.py
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
# BEGIN BIGQMT_AGENT.py（内联）
# ===========================================================================
'''

_TAIL = '''
# ===========================================================================
# END BIGQMT_AGENT.py
# ===========================================================================
'''


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
    """把 agent 里 `from qmt_api import Executor` 换成内联说明。

    内联后 Executor 已是本文件的顶层名字，导入行会变成循环/无效导入。
    """
    out = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("from qmt_api import"):
            indent = line[:len(line) - len(line.lstrip())]
            out.append(indent + "# bundle: Executor 已在本文件内联定义（无外部依赖）")
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


def build(embed=None, stamp=""):
    api = _strip_future(_read("qmt_api.py"))
    agent = _strip_agent_imports(_strip_future(_read("BIGQMT_AGENT.py")))
    if embed:
        agent = _embed(agent, embed)
    body = _HEADER.format(stamp=stamp) + api + _MID + agent + _TAIL
    if not body.endswith("\n"):
        body += "\n"
    # ★ _OWN_NAMES 必须在**整份 bundle 组装完之后**按 AST 注入 —— 它要覆盖
    #   qmt_api.py + BIGQMT_AGENT.py 两个来源的全部顶层名字。
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
    if "from qmt_api import" in text:
        problems.append("仍存在 `from qmt_api import`（内联不完整）")
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
    return problems


_CODING_RE = re.compile(r"^#.*?coding[:=]\s*[-\w.]+.*$")


def recode(text, enc):
    """把 bundle 转成指定源码编码。→ (text, 实际编码, 告警列表)。

    QMT 官方口径是 GBK。做法：去掉所有 PEP263 coding cookie（bundle 里内联了
    多个源文件的 cookie，全部清掉只留一条），改成 `#coding:<enc>`。

    ★ 若目标编码表示不了某个字符，**退回 utf-8 并把字符报出来** —— 绝不
    用 errors='replace' 静默写坏源码（那会变成最难查的一类故障）。
    """
    notes = []
    if enc.lower().replace("_", "-") in ("utf-8", "utf8"):
        return text, "utf-8", notes
    notes.append(
        "%s 编码**会在 QMT 内置 Python 3.6.8 上运行失败**（实测 2026-10-08："
        "gb18030 全量报 `SyntaxError: encoding problem: gb18030`，"
        "进程 return code:1，模型交易里只见「启动即停止」）。"
        "本选项仅用于「不在这台 QMT 上运行、只给编辑器阅读」的场景；"
        "要真跑请用 --encoding utf-8。" % enc)
    kept = [ln for ln in text.split("\n") if not _CODING_RE.match(ln)]
    cand = "#coding:%s\n" % enc + "\n".join(kept)
    try:
        cand.encode(enc)
        return cand, enc, notes
    except UnicodeEncodeError as exc:
        bad = cand[exc.start:exc.end]
        notes.append("%s 无法表示字符 %r（offset %d）—— 已退回 utf-8；"
                     "若要真 %s，请把该字符替换成 %s 内的等价写法"
                     % (enc, bad, exc.start, enc, enc))
        return text, "utf-8", notes


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 QMT 单文件 agent bundle")
    ap.add_argument("--out", help="输出文件路径")
    ap.add_argument("--embed-config", help="把该 agent_config.json 内嵌进 bundle")
    ap.add_argument("--check", action="store_true", help="只做自检（不写文件）")
    ap.add_argument("--stamp", default="", help="写进文件头的生成时间戳")
    ap.add_argument("--encoding", default="utf-8",
                    choices=["utf-8", "gbk", "gb18030"],
                    help="输出源码编码（QMT 官方口径为 gbk；不可表示时退回 utf-8）")
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

    text = build(embed=embed, stamp=args.stamp or "(未指定)")
    problems = check(text)
    if problems:
        for p in problems:
            print("[FAIL] " + p)
        return 1
    print("[OK] bundle 自检通过: %d 字符, %d 行"
          % (len(text), len(text.splitlines())))
    text, enc, notes = recode(text, args.encoding)
    for n in notes:
        print("[WARN] " + n)
    if args.check or not args.out:
        return 0

    out = os.path.abspath(args.out)
    d = os.path.dirname(out)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(out, "w", encoding=enc, newline="\n") as fh:
        fh.write(text)
    print("[OK] 已写出: %s (%d bytes, encoding=%s)"
          % (out, os.path.getsize(out), enc))
    if args.txt:
        # 路径 B 辅助：同内容 .txt 副本，记事本双击打开粘贴更顺手
        out_txt = os.path.splitext(out)[0] + ".txt"
        with io.open(out_txt, "w", encoding=enc, newline="\n") as fh:
            fh.write(text)
        print("[OK] 已写出 .txt 副本（供路径 B 粘贴用）: %s (%d bytes)"
              % (out_txt, os.path.getsize(out_txt)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
