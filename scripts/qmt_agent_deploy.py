# -*- coding: utf-8 -*-
"""大 QMT agent 一键部署/巡检工具（把「人工搬文件」这件事自动化掉）。

它做五件事，每一步都可单跑：
  1. ``deploy``   —— 生成单文件 bundle（默认 GBK，QMT 官方口径）放进策略目录顶层；
  2. ``config``   —— 写入/刷新 agent_config.json（bridge_dir / token / trading_enabled）；
  3. ``register`` —— 打印**唯一可行**的注册路径（含自动验证），并说清为什么拷贝无效；
  4. ``inspect``  —— QMT 关闭时导出 configFormula / UiSettingConfig（注册树），
                     用于分析「策略列表 + 自动运行」的持久化格式；
  5. ``check``    —— 体检：QMT 是否在跑、bundle 是否最新、是否**已注册**、配置是否齐。

为什么需要它（2026-10-01 真机实测根因）
--------------------------------------
* **策略列表 = 客户端持久化的注册树，不是策略目录扫描。**
  决定性实验：目录里两个 **md5 完全相同**（`3b69572...`、3562 B）的文件，
  重启客户端后一个在列表、一个不在 ⇒ 与内容/名字/位置全都无关。
* 注册树 = ``config/user/root/{configFormula,lua,UiSettingConfig}``（`XTF1` 容器），
  运行期被**整文件字节区间锁**（读会拿到 Win32 ERROR_LOCK_VIOLATION=33）。
* ⇒ 「把 .py 拷进 python/ 就会出现在模型交易里」是错的；只有「新建策略 /
  导入策略」这类会写注册树的 UI 动作才行。子目录仍不扫、`_` 前缀仍被排除。

用法::

    python scripts/qmt_agent_deploy.py check
    python scripts/qmt_agent_deploy.py deploy
    python scripts/qmt_agent_deploy.py register
    python scripts/qmt_agent_deploy.py config --bridge-dir D:/qmt_bridge --token XXX --trading
    python scripts/qmt_agent_deploy.py inspect
"""
from __future__ import print_function

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import gen_qmt_agent_bundle as bundle  # noqa: E402

#: QMT 常见安装盘位（本机实测命中 P:\stock\gd_qmt）
_CANDIDATE_ROOTS = ("P:/stock/gd_qmt", "C:/光大证券金阳光远航版", "D:/", "E:/")

#: 安装目录识别标记（存在其一即认为是 QMT 安装根）
_MARKERS = ("bin.x64", "userdata/log", "config/user/root")

#: QMT 进程名（用于判断「现在改配置会不会被覆盖」）
_QMT_PROCS = ("XtItClient.exe", "XtMiniQmt.exe", "XtItClient", "xtquoter")


# ---------------------------------------------------------------------------
def find_qmt_dir(explicit=None):
    if explicit:
        p = os.path.abspath(explicit)
        return p if _is_qmt_dir(p) else None
    for root in _CANDIDATE_ROOTS:
        if _is_qmt_dir(root):
            return os.path.abspath(root)
        # 二级目录里找（P:/stock/gd_qmt 这种）
        try:
            for name in os.listdir(root):
                cand = os.path.join(root, name)
                if os.path.isdir(cand) and _is_qmt_dir(cand):
                    return os.path.abspath(cand)
        except Exception:
            continue
    return None


def _is_qmt_dir(p):
    if not os.path.isdir(p):
        return False
    hits = sum(1 for m in _MARKERS
               if os.path.exists(os.path.join(p, m.replace("/", os.sep))))
    return hits >= 2


def qmt_running():
    """返回正在运行的 QMT 相关进程名列表（GBK 解码，tasklist 在本机可用）。"""
    try:
        out = subprocess.check_output(["tasklist"], stderr=subprocess.STDOUT)
    except Exception:
        return []
    text = out.decode("gbk", "replace")
    low = text.lower()
    return [n for n in _QMT_PROCS if n.lower().split(".")[0] in low]


def strategy_dir(qmt_dir):
    return os.path.join(qmt_dir, "python")


def reveal(path):
    """在资源管理器里定位文件（把「导入本地策略」的选文件步骤降到一次点击）。

    ★ 只调 explorer、**不碰 QMT 进程**：早前用 Win32 窗口 API 操作 QMT 主窗口
    造成了黑屏（无 GPU 会话），已放弃一切 GUI 自动化；打开一个只读的资源管理器
    窗口是安全的，且仅在显式 `--reveal` 时执行。
    """
    if os.name != "nt":
        return False
    try:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            d = os.path.dirname(path) or "."
            if os.path.isdir(d):
                subprocess.Popen(["explorer", os.path.normpath(d)])
            else:
                return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
def cmd_deploy(args):
    qmt = find_qmt_dir(args.qmt_dir)
    if not qmt:
        print("[FAIL] 未找到 QMT 安装目录，请用 --qmt-dir 指定")
        return 1
    sdir = args.strategy_dir or strategy_dir(qmt)
    if not os.path.isdir(sdir):
        print("[FAIL] 策略目录不存在: %s" % sdir)
        return 1
    text = bundle.build(stamp=time.strftime("%Y-%m-%d %H:%M"))
    problems = bundle.check(text)
    if problems:
        for p in problems:
            print("[FAIL] bundle 自检: %s" % p)
        return 1
    text, enc, notes = bundle.recode(text, args.encoding)
    for n in notes:
        print("[WARN] %s" % n)
    out = os.path.join(sdir, "qmt_work_agent.py")
    with io.open(out, "w", encoding=enc, newline="\n") as fh:
        fh.write(text)
    print("[OK] 已部署单文件 agent: %s (%d bytes, encoding=%s)"
          % (out, os.path.getsize(out), enc))
    print("     策略显示名 = 注册树里的条目名（建议就叫 qmt_work_agent）")

    running = qmt_running()
    if running:
        print("\n[!] 检测到 QMT 正在运行: %s" % ", ".join(running))
    print("\n[!!] 重要：拷贝文件**不会**让策略出现在「模型交易」里。")
    print("     策略列表来自客户端持久化注册树（实测：md5 完全相同的两个文件，")
    print("     一个在列表一个不在）。必须做一次注册动作 —— 见 `register` 子命令。")
    if getattr(args, "reveal", False):
        if reveal(out):
            print("\n[i] 已打开资源管理器并选中该文件 —— 直接拖/选进 QMT 导入框。")
    return 0


def cmd_register(args):
    """打印唯一可行的注册路径，并（可选）轮询验证是否注册成功。"""
    qmt = find_qmt_dir(args.qmt_dir)
    if not qmt:
        print("[FAIL] 未找到 QMT 安装目录")
        return 1
    sdir = args.strategy_dir or strategy_dir(qmt)
    target = "qmt_work_agent"
    sfile = os.path.join(sdir, target + ".py")

    print("=" * 70)
    print("把策略登记进 QMT 注册树（只能由客户端 UI 动作完成）")
    print("=" * 70)
    print("策略文件: %s  %s" % (sfile,
                               "已就位" if os.path.exists(sfile) else "缺失 → 先跑 deploy"))
    print()
    print("为什么不能靠拷贝文件：策略列表 = 客户端持久化的注册树")
    print("  (config/user/root/configFormula)，不是 python/ 目录扫描。")
    print("  实测判据：目录里两个 md5 完全相同的 .py，重启后一个在列表一个不在。")
    print()
    print("二选一，在 QMT 客户端里操作（约 30 秒）：")
    print()
    print("  路径 A（导入本地策略）")
    print("    1. 左侧进「模型研究」（找不到就走「模型交易」）")
    print("    2. 在策略区（图标/列表）空白处 **右键**")
    print("    3. 选「导入本地策略」/「本地.rzrk导入」→ 文件类型选 *.py")
    print("    4. 选中：%s" % sfile)
    print()
    print("  路径 B（新建 + 粘贴）")
    print("    1. 「我的」页 → 新建策略 → Python 策略")
    print("    2. 全选删除模板代码，粘贴 %s 的全部内容" % os.path.basename(sfile))
    print("    3. 点「编译」保存（编译/保存才会登记进注册树）")
    print()
    print("  注册后：在「模型交易」里找到它 → 右键勾选「自动运行」")
    print("          （startupAutorun=true 记进 UiSettingConfig，之后 QMT 每次")
    print("            启动都会自动拉起，零人工）")
    print()

    if getattr(args, "reveal", False) and reveal(sfile):
        print("[i] 已打开资源管理器并选中 %s —— 导入框里直接选它即可。"
              % os.path.basename(sfile))
        print()

    if not args.wait:
        print("复制完成后重启客户端，再跑：")
        print("  python scripts/qmt_agent_deploy.py check")
        print("验证是否已登记。")
        return 0

    print("等待注册生效（轮询客户端日志，最多 %ds）..." % args.wait)
    deadline = time.time() + args.wait
    sys.path.insert(0, _HERE)
    import qmt_agent_verify as ver  # noqa: E402
    while time.time() < deadline:
        reg = ver.registration(qmt, target)
        if reg.get("registered"):
            print("[OK] 已登记进注册树（autorun=%s）" % reg.get("autorun"))
            return 0
        time.sleep(5)
    print("[FAIL] 超时仍未在注册树里看到 %r —— 导入动作没生效（或客户端未重启）" % target)
    return 2


def cmd_config(args):
    qmt = find_qmt_dir(args.qmt_dir)
    if not qmt:
        print("[FAIL] 未找到 QMT 安装目录")
        return 1
    sdir = args.strategy_dir or strategy_dir(qmt)
    path = os.path.join(sdir, "agent_config.json")
    cfg = {}
    if os.path.exists(path):
        with io.open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    legacy = os.path.join(sdir, "agent_bigqmt", "agent_config.json")
    if not cfg and os.path.exists(legacy):
        with io.open(legacy, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        print("[i] 已从旧位置继承配置: %s" % legacy)
    cfg.setdefault("bridge_dir", args.bridge_dir or r"C:\Users\Administrator\qmt_work\bigqmt_bridge")
    cfg.setdefault("transport", "file")
    cfg.setdefault("poll_interval_ms", 500)
    if args.bridge_dir:
        cfg["bridge_dir"] = args.bridge_dir
    if args.token:
        cfg["auth_token"] = args.token
    if args.trading is not None:
        cfg["trading_enabled"] = bool(args.trading)
    cfg.setdefault("trading_enabled", False)
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    print("[OK] 写入 %s" % path)
    print("     bridge_dir=%s trading_enabled=%s token=%s"
          % (cfg["bridge_dir"], cfg["trading_enabled"],
             "已设置" if cfg.get("auth_token") else "空(不安全)"))
    print("[!] 修改配置后需**重启策略**（QMT 里停止再运行 qmt_work_agent）才生效。")
    return 0


def cmd_inspect(args):
    qmt = find_qmt_dir(args.qmt_dir)
    if not qmt:
        print("[FAIL] 未找到 QMT 安装目录")
        return 1
    running = qmt_running()
    if running and not args.force:
        print("[FAIL] QMT 正在运行(%s) —— 这两个配置文件被独占锁定，读不到内容。"
              % ", ".join(running))
        print("       请先关闭 QMT 再执行 inspect（或加 --force 只做只读尝试）。")
        return 1
    root = os.path.join(qmt, "config", "user", "root")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(_ROOT, "logs", "qmt_config_bak", stamp)
    if not os.path.isdir(bak):
        os.makedirs(bak)
    for name in ("configFormula", "UiSettingConfig"):
        src = os.path.join(root, name)
        if not os.path.exists(src):
            print("[!] 不存在: %s" % src)
            continue
        dst = os.path.join(bak, name)
        shutil.copy2(src, dst)
        raw = open(src, "rb").read()
        print("[OK] %s → %s (%d bytes)" % (name, dst, len(raw)))
        for enc in ("utf-8", "gbk", "utf-16"):
            try:
                head = raw[:600].decode(enc)
                print("     编码探测=%s 头部: %s" % (enc, head[:200].replace("\n", "\\n")))
                break
            except Exception:
                continue
        else:
            print("     头部(hex): %s" % raw[:80].hex())
    print("\n[i] 备份目录: %s —— 用它分析「策略注册 + 自动运行」的持久化格式" % bak)
    return 0


def cmd_check(args):
    qmt = find_qmt_dir(args.qmt_dir)
    print("QMT 安装目录 : %s" % (qmt or "未找到"))
    if not qmt:
        return 1
    sdir = strategy_dir(qmt)
    print("策略目录     : %s" % sdir)
    top = os.path.join(sdir, "qmt_work_agent.py")
    legacy = os.path.join(sdir, "agent_bigqmt", "BIGQMT_AGENT.py")
    print("单文件 agent : %s" % ("存在 (%d bytes, %s)"
                                % (os.path.getsize(top),
                                   time.strftime("%Y-%m-%d %H:%M",
                                                 time.localtime(os.path.getmtime(top))))
                                if os.path.exists(top) else "缺失 → 跑 deploy"))
    print("旧子目录包   : %s" % ("存在（列表不可见，仅作配置兜底）" if os.path.exists(legacy) else "无"))
    for cand in (os.path.join(sdir, "agent_config.json"),
                 os.path.join(sdir, "agent_bigqmt", "agent_config.json")):
        print("配置         : %s %s" % (cand, "存在" if os.path.exists(cand) else "缺失"))
    running = qmt_running()
    print("QMT 进程     : %s" % (", ".join(running) if running else "未运行"))
    root = os.path.join(qmt, "config", "user", "root")
    for name in ("configFormula", "UiSettingConfig"):
        p = os.path.join(root, name)
        # ★ 必须真开一次：os.stat 永远成功，拿它判「可读」是典型的假绿。
        try:
            with open(p, "rb") as fh:
                opened = len(fh.read(1)) == 1
            rw = "可读" if opened else "读不到内容"
        except Exception as exc:
            rw = "被 QMT 独占锁定(%s)" % type(exc).__name__
        try:
            st = os.stat(p)
        except Exception:
            st = None
        print("注册表 %-14s: %s %s" % (name,
                                      ("%d bytes, %s" % (st.st_size,
                                                         time.strftime("%Y-%m-%d %H:%M",
                                                                       time.localtime(st.st_mtime)))
                                       if st else "-"), rw))

    # ---- 注册状态：这才是「模型交易里看不到」的直接判据 ----
    target = args.strategy
    try:
        import qmt_agent_verify as ver
        reg = ver.registration(qmt, target)
    except Exception as exc:  # pragma: no cover - 诊断工具，失败不掩盖主结论
        reg = {}
        print("注册状态     : 无法判定 (%s)" % exc)
    if reg.get("log"):
        print("注册状态     : %s（策略 %r）"
              % ("已登记" if reg.get("registered") else "**未登记** ← 列表里当然看不到", target))
        if reg.get("registered"):
            print("自动运行     : %s" % ("是（启动即拉起）" if reg.get("autorun") else "否"))

    if reg.get("registered") is False:
        print("\n下一步: 跑 `python scripts/qmt_agent_deploy.py register` —— "
              "拷贝文件不会注册，必须做一次「导入/新建策略」。")
    elif running:
        print("\n下一步: 到「模型交易」找到 %s → 右键勾选「自动运行」，之后零人工。" % target)
    else:
        print("\n下一步: 启动 QMT（已登记的策略会出现在列表里）。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="QMT agent 一键部署/巡检")
    ap.add_argument("cmd", choices=("deploy", "config", "inspect", "check", "register"))
    ap.add_argument("--qmt-dir", help="QMT 安装目录（默认自动探测）")
    ap.add_argument("--strategy-dir", help="策略目录（默认 <qmt>/python）")
    ap.add_argument("--strategy", default="qmt_work_agent", help="策略名（注册态核对用）")
    ap.add_argument("--bridge-dir", help="桥目录")
    ap.add_argument("--token", help="auth token")
    ap.add_argument("--trading", dest="trading", action="store_true", default=None,
                    help="打开下单开关")
    ap.add_argument("--no-trading", dest="trading", action="store_false",
                    help="关闭下单开关")
    ap.add_argument("--encoding", default="gb18030",
                    choices=["utf-8", "gbk", "gb18030"],
                    help="bundle 源码编码。默认 gb18030 —— 它是 GBK 的超集，"
                         "对 GBK 可表示的中文**字节完全一致**，又能表示 ⇒/★ 这类"
                         "GBK 之外的字符；纯 gbk 遇到这类字符会退回 utf-8 并告警")
    ap.add_argument("--wait", type=int, default=0,
                    help="register: 轮询等待注册生效的秒数（0=不等待）")
    ap.add_argument("--reveal", action="store_true",
                    help="deploy/register: 打开资源管理器并选中 bundle 文件"
                         "（把导入时的「找文件」一步降到一次点击；只开 explorer，"
                         "不触碰 QMT 进程）")
    ap.add_argument("--force", action="store_true", help="inspect: 忽略 QMT 运行状态")
    args = ap.parse_args(argv)
    return {"deploy": cmd_deploy, "config": cmd_config, "inspect": cmd_inspect,
            "check": cmd_check, "register": cmd_register}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
