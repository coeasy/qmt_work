# -*- coding: utf-8 -*-
"""探明「QMT 策略列表」到底由什么决定 —— 只读证据工具。

真机实测结论（2026-10-01，金阳光 QMT 2.1.29.0 @ P:\\stock\\gd_qmt）
----------------------------------------------------------------
**列表 = 客户端持久化的「公式注册树」，与策略目录无关。**

判定这条的决定性实验（可复算）：目录里放两个 **md5 完全相同**的文件
（`尾盘闲置资金自动逆回购.py` 与 `尾盘闲置资金自动通用回购逆回购.py`，
同为 `3b69572732f16d4a0ef96033b93f9601`、同为 3562 B），重启客户端后
**前者在列表里、后者不在**。⇒ 与文件内容、文件名、目录位置统统无关，
只与「客户端有没有把这条登记进注册树」有关。

注册树落在（运行期被 **整文件字节区间锁**，普通读取报
`PermissionError` / Win32 `ERROR_LOCK_VIOLATION(33)`）：

    <QMT>/config/user/root/configFormula    公式树（含非 python 公式）
    <QMT>/config/user/root/lua              公式体
    <QMT>/config/user/root/UiSettingConfig  每个策略的 startupAutorun

三者都是 `XTF1` 容器（表头 + 块表 + 压缩数据体）。要离线读，需
**先退出客户端**（或走卷影副本，见 `--dump-registry`）。

⇒ 因此「把 .py 拷进 python/ 就会出现在模型交易里」是**错的**；正确通道只有
「新建策略」或「导入策略」这类**会写注册树的 UI 动作**。

本工具把上述结论变成可复算证据：从客户端日志还原注册表条目、与策略目录
实际文件做差集，并对指定策略给出「文件是否就位 / 是否已注册 / 是否自动运行」
三态判定。

用法::

    python scripts/qmt_strategy_list_probe.py                 # 自动找 QMT 与最新日志
    python scripts/qmt_strategy_list_probe.py --target qmt_work_agent
    python scripts/qmt_strategy_list_probe.py --dump-registry  # 需先退出客户端
"""
from __future__ import print_function

import argparse
import glob
import os
import re
import sys
import time

_TARGET = "qmt_work_agent"
_ROOT = ("config", "user", "root")
_REG_FILES = ("configFormula", "UiSettingConfig", "lua")

#: 只有**主日志**里才有注册树行。副日志（`XtClient_datasource_*.log`、
#: `XtClient_Message_*.log` …）没有，按 mtime 取「最新」会取到副日志，
#: 于是注册树解析出空集、把「已注册」全部误报成「未注册」——
#: 这类**静默假阴性**比报错危险得多，必须按名字精确匹配。
_MAIN_LOG_RE = re.compile(r"^(?:XtClient|XtMiniQmt)_\d{8}\.log$", re.I)


def _newest_log(qmt_dir):
    cands = []
    for d in (os.path.join(qmt_dir, "userdata", "log"),
              os.path.join(qmt_dir, "userdata_mini", "log")):
        for p in glob.glob(os.path.join(d, "*.log")):
            if _MAIN_LOG_RE.match(os.path.basename(p)):
                cands.append(p)
    return max(cands, key=os.path.getmtime) if cands else None


def _read_text(path):
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def _listed(log_path):
    """从日志的 `[python formula] from configFormula` 行还原注册树条目。"""
    seen = {}
    for m in re.finditer(r"from configFormula, index:(\d+), utfName:([^,]+),",
                         _read_text(log_path)):
        seen.setdefault(int(m.group(1)), m.group(2).strip())
    return seen


def _autorun(log_path):
    """`CStrategyLoadSetting` 行 → {策略名: (startupAutorun, ID)}。

    ★ 字段名在客户端日志里是 **`FomrulaName`**（客户端自身的拼写），按
    「FormulaName / FomrlaName」去找会 0 命中 —— 而 0 命中会伪装成
    「这台机器没配自动运行」，属于最危险的一类假阴性。两种拼写都收。
    """
    raw = _read_text(log_path)
    out = {}
    pat = (r"\[CStrategyLoadSetting\]Account:\S+ , Fom(?:rula|rla)Name: (.+?), "
           r"startupAutorun: (\w+), ID:(\d+)")
    for m in re.finditer(pat, raw):
        out[m.group(1).strip()] = (m.group(2) == "true", int(m.group(3)))
    if not out:  # 兜底：字段名将来再变也不能静默变空集
        pat = r"CStrategyLoadSetting\].*?Name: (.+?), startupAutorun: (\w+), ID:(\d+)"
        for m in re.finditer(pat, raw):
            out[m.group(1).strip()] = (m.group(2) == "true", int(m.group(3)))
    return out


def _file_state(path):
    raw = open(path, "rb").read()
    text = raw.decode("utf-8", "replace")
    first = (text.splitlines() or [""])[0]
    return {
        "size": len(raw),
        "encrypted": len(first) > 200 and " " not in first[:200],
        "has_init": bool(re.search(r"^def init\b", text, re.M)),
        "has_handlebar": bool(re.search(r"^def handlebar\b", text, re.M)),
    }


def _dump_registry(qmt):
    """尝试直读注册树。运行期会被字节锁挡住，退出客户端后即可读。"""
    print("\n注册树文件:")
    for name in _REG_FILES:
        p = os.path.join(qmt, *_ROOT, name)
        if not os.path.exists(p):
            print("  %-16s 不存在" % name)
            continue
        size = os.path.getsize(p)
        try:
            raw = open(p, "rb").read()
            magic = raw[:4]
            print("  %-16s %8d B  可读  magic=%r" % (name, size, magic))
        except Exception as exc:
            print("  %-16s %8d B  读不了: %s" % (name, size, type(exc).__name__))
            print("                   → 客户端在跑，整文件被字节区间锁；退出客户端后重跑即可")


def main(argv=None):
    ap = argparse.ArgumentParser(description="QMT 策略列表证据探针")
    ap.add_argument("--qmt-dir", default=None)
    ap.add_argument("--target", default=_TARGET,
                    help="要给出三态判定的策略名（默认 %s）" % _TARGET)
    ap.add_argument("--dump-registry", action="store_true",
                    help="顺带探测注册树文件可读性/格式")
    args = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import qmt_agent_deploy as dep

    qmt = dep.find_qmt_dir(args.qmt_dir)
    if not qmt:
        print("[FAIL] 未找到 QMT 安装目录（用 --qmt-dir 指定）")
        return 1
    sdir = os.path.join(qmt, "python")
    log = _newest_log(qmt)
    print("QMT 安装目录 : %s" % qmt)
    print("策略目录     : %s" % sdir)
    print("日志         : %s"
          % ("%s (%s)" % (log, time.strftime("%Y-%m-%d %H:%M",
                                            time.localtime(os.path.getmtime(log))))
             if log else "未找到"))
    if not log:
        return 1

    listed = _listed(log)
    autorun = _autorun(log)
    listed_names = set(listed.values())
    files = sorted(glob.glob(os.path.join(sdir, "*.py")))
    dir_names = [os.path.splitext(os.path.basename(p))[0] for p in files]

    print("\n注册树条目 %d 个；带 startupAutorun 记录 %d 个"
          % (len(listed), len(autorun)))
    for name, (flag, sid) in sorted(autorun.items(), key=lambda kv: kv[1][1]):
        print("   ID=%-3d %-28s startupAutorun=%s" % (sid, name, flag))

    print("\n%-34s %-6s %-8s %-6s %-9s %-8s"
          % ("策略文件", "在列表", "疑似加密", "init", "handlebar", "自动运行"))
    print("-" * 78)
    for p in files:
        name = os.path.splitext(os.path.basename(p))[0]
        st = _file_state(p)
        print("%-34s %-7s %-9s %-7s %-11s %-9s" % (
            name[:32], "Y" if name in listed_names else "N",
            "Y" if st["encrypted"] else "N",
            "Y" if st["has_init"] else "N",
            "Y" if st["has_handlebar"] else "N",
            "Y" if autorun.get(name, (False,))[0] else "-"))

    missing = [n for n in dir_names if n not in listed_names]
    extra = sorted(listed_names - set(dir_names))
    print("\n目录里有、列表里没有: %s" % (", ".join(missing) or "无"))
    print("列表里有、目录里没有: %s" % (", ".join(extra) or "无"))

    # ---- 目标策略三态判定（给自动化消费）----
    tgt = args.target
    # ★ target 既可能是「策略名」（qmt_work_agent），也可能是「文件名/路径」
    #   （python/qmt_work_agent.py 或绝对路径）。两种情况必须分别归一，否则：
    #   ① 无条件拼 ".py" → "xxx.py.py" ⇒ 「文件就位」误判为否（文件明明在）；
    #   ② 拿带后缀的 tgt 去注册列表查 ⇒ 「已注册」恒为否（列表里存的是无后缀策略名）。
    _base = tgt[:-3] if tgt.endswith(".py") else tgt
    if tgt.endswith(".py"):
        tgt_file = tgt if (os.path.isabs(tgt) or os.sep in tgt or "/" in tgt) else os.path.join(sdir, tgt)
    else:
        tgt_file = os.path.join(sdir, _base + ".py")
    print("\n目标策略 %r 判定:" % _base)
    print("  文件就位   : %s" % ("是 (%d B)" % os.path.getsize(tgt_file)
                                 if os.path.exists(tgt_file) else "否"))
    print("  已注册     : %s" % ("是" if _base in listed_names else
                                 "否 ← 这就是模型交易里看不到它的原因"))
    print("  自动运行   : %s" % ("是" if autorun.get(_base, (False,))[0] else "否"))
    if _base not in listed_names:
        print("\n  → 拷贝文件不会注册。必须走会写注册树的 UI 动作:")
        print("     A) 「模型研究」→ 策略区右键 → 导入本地策略/本地.rzrk导入，选 %s" % tgt_file)
        print("     B) 「我的」→ 新建策略 → Python 策略 → 粘贴代码 → 编译")
        print("     注册后重启客户端，本工具第二次运行应显示「已注册: 是」。")

    if args.dump_registry:
        _dump_registry(qmt)

    print("\n判读：列表来自客户端注册树（非目录扫描）—— md5 相同的两个文件"
          "一个在列表一个不在即可自证；`_` 前缀与子目录不进列表；"
          "startupAutorun=true 的策略由 QMT 启动时自动拉起。")
    return 0 if tgt in listed_names else 2


if __name__ == "__main__":
    sys.exit(main())
