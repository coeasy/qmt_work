# -*- coding: utf-8 -*-
"""大 QMT agent 一键部署/巡检工具（把「人工搬文件」这件事自动化掉）。

它做五件事，每一步都可单跑：
  1. ``deploy``   —— 生成单文件 bundle 放进策略目录顶层（**编码固定 utf-8**：
                     gbk/gb18030 会让 QMT 内置 Python 3.6 的 tokenizer 报
                     ``SyntaxError: encoding problem``，策略 ``return code:1``、
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
import re
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import gen_qmt_agent_bundle as bundle  # noqa: E402

#: 安装目录识别标记。
#: ★ 通用判据（不写死任何券商名 / 本机路径）：**必须有 ``python`` 策略目录**，
#: 且再命中其余标记里至少一个。这样国投 / 国泰君安 / 华泰 / 海通 / 招商 /
#: 光大 / 中信…… 任何券商版本的 QMT 都能识别，且不会把「随便一个叫 python 的
#: 文件夹」误判成 QMT。
_MARKER_STRATEGY_DIR = "python"
_MARKERS_SECONDARY = ("bin.x64", "userdata", "config/user/root", "user.dat",
                      "userdata_mini", "xtitdata")


def _candidate_roots():
    """通用扫描：枚举所有**固定盘**的 1~2 层子目录作为候选根。

    ★ 刻意**不写死**盘位或券商安装名（旧版硬编码 ``P:/stock/gd_qmt``、
    ``C:/光大证券金阳光远航版`` —— 换一台机器就废）。扫描是 O(目录数) 且只
    做 ``os.path.isdir``，实测全机扫一遍 <0.5s，比「猜路径」可靠得多。

    Windows 用盘符枚举；其他系统退化到常见安装根。
    """
    roots = []
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            base = "%s:\\" % letter
            if not os.path.isdir(base):
                continue
            roots.append(base)
            for name in _listdir(base):
                cand = os.path.join(base, name)
                if os.path.isdir(cand):
                    roots.append(cand)
                    # 二级目录：P:\stock\gd_qmt 这类「盘\分类\券商目录」
                    for sub in _listdir(cand):
                        subc = os.path.join(cand, sub)
                        if os.path.isdir(subc):
                            roots.append(subc)
    else:
        for base in ("/opt", "/usr/local", "/home", "/root",
                     os.path.expanduser("~")):
            if os.path.isdir(base):
                roots.append(base)
    return roots


def _listdir(path):
    """listdir 的容错封装：网络盘 / 权限不足 / 目录被占用都不该让探测中断。"""
    try:
        return sorted(os.listdir(path))
    except Exception:
        return []


def find_qmt_dir(explicit=None):
    if explicit:
        p = os.path.abspath(explicit)
        return p if is_qmt_dir(p) else None
    # ★ 优先读环境变量：批处理 / CI / 用户显式声明时不扫描全盘
    env_dir = os.environ.get("QMT_DIR", "").strip()
    if env_dir:
        p = os.path.abspath(env_dir)
        if is_qmt_dir(p):
            return p
    for root in _candidate_roots():
        if is_qmt_dir(root):
            return os.path.abspath(root)
    return None


def is_qmt_dir(p):
    """通用 QMT 安装根判定：``python`` 策略目录 + 至少一个次级标记。"""
    if not os.path.isdir(p):
        return False
    if not os.path.isdir(os.path.join(p, _MARKER_STRATEGY_DIR)):
        return False
    return any(os.path.exists(os.path.join(p, m.replace("/", os.sep)))
               for m in _MARKERS_SECONDARY)

#: QMT 进程名（用于判断「现在改配置会不会被覆盖」）
_QMT_PROCS = ("XtItClient.exe", "XtMiniQmt.exe", "XtItClient", "xtquoter")


def qmt_running():
    """返回正在运行的 QMT 相关进程名列表（GBK 解码，tasklist 在本机可用）。"""
    try:
        out = subprocess.check_output(["tasklist"], stderr=subprocess.STDOUT,
                                      timeout=10)
    except Exception:
        return []
    text = out.decode("gbk", "replace")
    low = text.lower()
    return [n for n in _QMT_PROCS if n.lower().split(".")[0] in low]


def strategy_dir(qmt_dir):
    return os.path.join(qmt_dir, "python")


#: 外部进程启动行：``[TC::CTradeStrategyData::doRun] execude cmd:  -u "<file>" "<userdata>" <ts>``
_RE_EXTERNAL_RUN = re.compile(r"execude cmd:\s+-u\s+\"([^\"]+)\"")
#: 公式（内置 Python）启动行：``doRun: m_type:0, ... m_luaFile:<file>, ... m_scriptType:1 ...``
_RE_INPROC_RUN = re.compile(r"m_luaFile:([^,]+),\s*m_startTime")


def _repair_mojibake(s):
    """把 QMT 日志里「utf-8 字节被按 cp1252/latin-1 解码」的中文文件名还原。

    QMT 主日志同时写 utf-8 与 GBK 两份文件名，其中一份是 mojibake
    （实测：``P:\\...\\å›½å€ºé€†å›žè´­.py`` 即 ``国债逆回购.py``）。比文件名前先还原，
    否则中文策略名永远匹配不上，会得出「没找到启动记录」的假结论。

    ★ 必须优先试 **cp1252**：``国`` 的 utf-8 首字节 0x9B 在 cp1252 里是 ``›``
    （U+203A），**不在 latin-1 范围**；先按 latin-1 解会把该字节静默丢掉，
    还原出的名字是错的（实测踩过）。
    """
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = s.encode(codec, "strict").decode("utf-8", "strict")
            if fixed:
                return fixed
        except Exception:  # noqa: BLE001
            continue
    return s


def run_mode_of_strategy(qmt_dir, filename, max_logs=4):
    """判定某策略**实际以哪种形态被 QMT 运行**：内置Python（公式，可下单）还是独立进程（不可下单）。

    为什么必须判这个（2026-10-14 真机取证）
    -------------------------------------
    QMT 只把 ``passorder`` / ``get_trade_detail_data`` 等交易函数注入
    **在终端进程内被公式引擎挂载**的那个文件命名空间。若策略登记成
    「独立进程 / 原生 Python」，QMT 走的是
    ``pythonw.exe -u <file> <userdata> <ts>``（主日志出现 ``execude cmd: -u ...``），
    该进程**一个注入函数都拿不到** —— agent 心跳正常、PROBE 正常，但
    ``injected=[]``、``trade_surface.can_submit=false``，**永远下不了单**。

    本机实测（`P:\\stock\\gd_qmt`）：
      * ``QMT_WORK_AGENT`` → 全部日志里**唯一**出现 ``execude cmd -u`` 的策略
        （14 次），对应文件是**明文源码** ⇒ 独立进程 ⇒ 不能下单；
      * ``新股申购`` / ``国债逆回购`` / ``ZT996`` → 走
        ``doRun: ... m_scriptType:1 ... m_runMode:1``（进程内），对应文件是
        **单行不透明容器**（QMT 编译后的公式体）⇒ 有账户/下单能力。

    判据（两条独立证据，任一命中即可；冲突时以日志为准并如实标注）
    ------------------------------------------------------------
    1. **主日志**：最新若干份 ``XtClient_*.log`` 里，该文件名出现在
       ``execude cmd -u`` ⇒ ``independent``；出现在 ``m_luaFile:`` 且同行有
       ``m_scriptType:`` ⇒ ``formula``。
    2. **文件形态**：单行、前 200 字节无空格、非 ``#`` 开头 ⇒ 编译容器
       （``formula``）；以 ``#``/``#coding`` 开头的明文源码 ⇒ ``independent``。

    返回 ``{"mode": "formula"|"independent"|"unknown", "evidence": [...], ...}``。
    只读，不碰 QMT 进程、不写任何文件。
    """
    sdir = strategy_dir(qmt_dir)
    target = os.path.basename(filename or "")
    stem = os.path.splitext(target)[0]
    res = {"mode": "unknown", "certain": False, "evidence": [],
           "log": "", "file": os.path.join(sdir, target)}

    # ---- 证据 1：主日志里的启动形态 ----
    logdir = os.path.join(qmt_dir, "userdata", "log")
    logs = []
    try:
        for n in _listdir(logdir):
            # 主日志只有 XtClient_<YYYYMMDD>.log 这一种形态；
            # XtClient_datasource_ / _Message_ / _Formula_ / _FormulaOutput_ /
            # _PerformanceFile_ 都是旁路日志，不含 doRun 启动行。
            # ★ 别用「名字里有下划线」去排除 —— 主日志恰好没有下划线，会把它一起滤掉
            #   （2026-10-14 实测踩过：判据写反 ⇒ 永远取不到日志证据，只剩文件形态兜底）。
            if not (n.startswith("XtClient_") and n.endswith(".log")):
                continue
            tail = n[len("XtClient_"):-4]
            if len(tail) == 8 and tail.isdigit():
                logs.append(os.path.join(logdir, n))
    except Exception:  # noqa: BLE001
        logs = []
    logs.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    logs = logs[:max_logs]
    if logs:
        res["log"] = logs[0]

    ext_hits, inproc_hits = [], []
    for lp in logs:
        try:
            with open(lp, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "doRun" not in line:
                        continue
                    m = _RE_EXTERNAL_RUN.search(line)
                    if m and os.path.basename(m.group(1).replace("/", "\\")) == target:
                        ext_hits.append((os.path.basename(lp), m.group(1)))
                        continue
                    m2 = _RE_INPROC_RUN.search(line)
                    if m2:
                        got = os.path.basename(_repair_mojibake(m2.group(1))
                                               .replace("/", "\\").replace("\\", "/"))
                        if got == target:
                            inproc_hits.append((os.path.basename(lp), m2.group(1)))
        except Exception:  # noqa: BLE001
            continue

    if ext_hits:
        res["mode"] = "independent"
        res["certain"] = True
        res["evidence"].append(
            "主日志出现外部进程启动行（最新一次在 %s，最近 %d 份主日志共 %d 次）："
            "execude cmd: -u \"%s\""
            % (ext_hits[0][0], len(logs), len(ext_hits), ext_hits[0][1]))
    if inproc_hits:
        res["evidence"].append(
            "主日志出现进程内公式启动行（最新一次在 %s）：m_luaFile=%s"
            % (inproc_hits[0][0], inproc_hits[0][1]))
        if not ext_hits:
            res["mode"] = "formula"
            res["certain"] = True

    # ---- 证据 2：文件形态 ----
    fpath = res["file"]
    if os.path.exists(fpath):
        try:
            with open(fpath, "rb") as fh:
                head = fh.read(2048)
            first_line = head.split(b"\r\n")[0].split(b"\n")[0]
            is_container = (not head.lstrip().startswith(b"#")
                            and len(first_line) > 200 and b" " not in first_line[:200])
            res["container"] = is_container
            res["evidence"].append(
                "文件形态：%s" % ("单行不透明容器（QMT 编译后的公式体）"
                                  if is_container else "明文源码（未经公式编译）"))
            if res["mode"] == "unknown":
                # 没有启动记录时只能据文件形态推断 —— **如实标注是推断**，
                # 不冒充「已确认」（这正是本项目反复强调的「判断 ≠ 缺陷」）。
                res["mode"] = "formula" if is_container else "independent"
                res["certain"] = False
                res["evidence"].append(
                    "（无启动记录 ⇒ 运行形态是**据文件形态推断**，未由日志确认）")
        except Exception as exc:  # noqa: BLE001
            res["evidence"].append("文件形态：读取失败（%s）" % exc)
    else:
        res["evidence"].append("文件形态：**文件不存在**（%s）" % fpath)
    return res


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
def _backup_if_exists(path):
    """若 path 已存在则复制为 ``<stem>.bak.<epoch>.<ext>``，返回备份路径。

    与后端 ``app/routes/qmt_agent.py::_backup_if_exists`` 同一语义（CLI 独立于
    后端部署，故本地实现一份而不 import app 层）。
    """
    if not os.path.isfile(path):
        return None
    stem, ext = os.path.splitext(path)
    bak = "%s.bak.%d%s" % (stem, int(time.time()), ext)
    shutil.copy2(path, bak)
    return bak


def _atomic_write(path, content, encoding="utf-8"):
    """先写 ``<path>.tmp`` 再 os.replace，避免半写入留下截断文件。

    与后端 ``app/routes/qmt_agent.py::_atomic_write`` 同一语义。
    """
    d = os.path.dirname(path) or "."
    if not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding=encoding, newline="\n") as fh:
        fh.write(content)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # 某些网络盘不支持 fsync —— os.replace 的原子性已足够，不让部署失败。
            pass
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
def _preflight_run(out, qmt):
    """部署前置门禁：把刚写出的 bundle **用 QMT 内置解释器真跑一遍**。

    ★ 为什么必须放在「写文件之后、宣布成功之前」（2026-10-08 事故复盘）：
      编码 / 自举这类问题**只有真跑才暴露**。当时 deploy 一切"正常"，用户在
      「模型交易」里点运行得到的却是 `return code:1` + 立刻停止，来回排查一整天。
      现在把「能跑起来」变成部署的前置条件 —— 跑不起来就不许说部署成功。
    """
    try:
        import qmt_agent_local_run as localrun
    except Exception as exc:  # pragma: no cover - 只会出现在裁剪过的环境
        print("[WARN] 载入本地跑通验证器失败（跳过前置门禁）: %s" % exc)
        return True
    py = localrun.find_qmt_python(qmt)
    userdata = os.path.join(qmt, "userdata")
    if not os.path.isdir(userdata):
        userdata = ""
    workdir = tempfile.mkdtemp(prefix="qmt_deploy_check_")
    try:
        if py:
            res = localrun.run_process(out, workdir, py, userdata, max_seconds=20.0)
            print("[i] 前置门禁（独立进程模式 · %s）: %s"
                  % (os.path.basename(py), "通过" if res.get("ok") else "失败"))
        else:
            bridge = os.path.join(workdir, "bridge")
            os.makedirs(os.path.join(bridge, "req"))
            os.makedirs(os.path.join(bridge, "resp"))
            sandbox = os.path.join(workdir, "strategy")
            os.makedirs(sandbox)
            shutil.copy2(out, os.path.join(sandbox, os.path.basename(out)))
            with io.open(os.path.join(sandbox, "agent_config.json"), "w",
                         encoding="utf-8") as fh:
                fh.write(json.dumps({"bridge_dir": bridge.replace("\\", "/"),
                                     "auth_token": "deploy-check",
                                     "trading_enabled": False,
                                     "poll_interval_ms": 200}))
            res = localrun.run_framework(
                os.path.join(sandbox, os.path.basename(out)), bridge)
            print("[i] 前置门禁（公式模式模拟 · 未找到 QMT 内置解释器）: %s"
                  % ("通过" if res.get("ok") else "失败"))
        for s in res.get("steps") or []:
            print("      [%s] %s" % ("OK" if s["ok"] else "!!",
                                     s.get("detail") or s.get("name")))
        for p in res.get("problems") or []:
            print("      [FAIL] %s" % p)
        if not res.get("ok") and res.get("stderr"):
            print("      子进程 stderr（末尾）:\n%s" % res["stderr"][-900:])
        return bool(res.get("ok"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
    out = os.path.join(sdir, getattr(args, "filename", "") or "qmt_work_agent.py")
    # ★ 与后端 _deploy_bundle 保持同一写入语义：先备份 .bak.<epoch> 再原子替换。
    #   直接 io.open("w") 会在半写入时留下截断的 agent 文件，且无回滚路径——
    #   QMT 内置 py3.6 读到 SyntaxError 后策略 return code:1，用户还得手工还原。
    backup = _backup_if_exists(out)
    if backup:
        print("[i] 已备份旧文件 → %s" % backup)
    _atomic_write(out, text, enc)
    print("[OK] 已部署单文件 agent: %s (%d bytes, encoding=%s)"
          % (out, os.path.getsize(out), enc))
    print("     策略显示名/注册名 = 注册树里的条目名（本机实测为 %s）"
          % getattr(args, "strategy", "qmt_work_agent"))
    print("     ★ 落盘文件名必须与注册树条目指向的文件名一致，否则 QMT 找不到源码。")

    # 路径 B 辅助：同内容 .txt 副本，记事本双击打开粘贴更顺手（--txt 显式请求）
    if getattr(args, "txt", False):
        out_txt = os.path.splitext(out)[0] + ".txt"
        with io.open(out_txt, "w", encoding=enc, newline="\n") as fh:
            fh.write(text)
        print("[OK] 已额外写出 .txt 副本（供路径 B「新建策略→粘贴代码」用）: %s (%d bytes)"
              % (out_txt, os.path.getsize(out_txt)))

    running = qmt_running()
    if running:
        print("\n[!] 检测到 QMT 正在运行: %s" % ", ".join(running))

    # ★ 部署前置门禁：跑不起来的 bundle 不许说"部署成功"（2026-10-08 事故）。
    if not getattr(args, "no_run_check", False):
        if not _preflight_run(out, qmt):
            print("\n[FAIL] 前置门禁未通过 —— 这份 bundle 在 QMT 内置解释器上跑不起来。")
            print("       文件已写出（便于排查），但**先别去模型交易里点运行**。")
            print("       常见原因：")
            print("         ① 编码不是 utf-8（gbk/gb18030 会触发内置 py3.6 "
                  "tokenizer 报 encoding problem / invalid token）")
            print("         ② 缺 `if __name__ == \"__main__\":` 自举（进程启动即退出）")
            print("         ③ agent_config.json 的 bridge_dir 不可写或两端不一致")
            return 1

    print("\n[!!] 重要：拷贝文件**不会**让策略出现在「模型交易」里。")
    print("     策略列表来自客户端持久化注册树（实测：md5 完全相同的两个文件，")
    print("     一个在列表一个不在）。必须做一次注册动作 —— 见 `register` 子命令。")
    print("     ★ 注册时**必须选「内置Python」形态**：登记成「独立进程 / 原生Python」")
    print("       会得到 `pythonw.exe -u <file> <userdata>` 的外部进程，")
    print("       终端**不注入** passorder/get_trade_detail_data ⇒ 永远下不了单。")
    if getattr(args, "reveal", False):
        if reveal(out):
            print("\n[i] 已打开资源管理器并选中该文件（供「新建策略→粘贴」时对照路径）。")
    return 0


def cmd_register(args):
    """打印唯一可行的注册路径，并（可选）轮询验证是否注册成功。"""
    qmt = find_qmt_dir(args.qmt_dir)
    if not qmt:
        print("[FAIL] 未找到 QMT 安装目录")
        return 1
    sdir = args.strategy_dir or strategy_dir(qmt)
    target = args.strategy
    # ★ 不能硬编码文件名：本机实测注册树里的条目指向的是大写 QMT_WORK_AGENT.py，
    #   而早前这里写死 qmt_work_agent.py → `register --reveal` 会把用户引到一个
    #   根本不存在的文件上（假告状）。文件名统一由 --filename 决定。
    fname = getattr(args, "filename", "") or (target + ".py")
    sfile = os.path.join(sdir, fname)

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
    print("  ★ 只有「内置Python」形态才有下单能力 —— 见下面 [!!] 说明。")
    print()
    print("  路径 A（推荐：新建 + 粘贴，得到**内置Python**形态）")
    print("    1. 「我的」页 → 新建策略 → Python 策略")
    print("    2. 在编辑器的**类型/运行方式**里选「内置Python」")
    print("       （**不要**选「原生Python / 独立进程」—— 那种形态拿不到交易函数）")
    print("    3. 全选删除模板代码，粘贴 %s 的全部内容" % os.path.basename(sfile))
    print("    4. 点「编译」保存（编译/保存才会登记进注册树，并把源码编译成")
    print("       公式体写入该 .py；文件会由明文变成单行不透明容器）")
    print()
    print("  路径 B（导入本地策略 —— **仅行情/回测可用**）")
    print("    1. 左侧进「模型研究」（找不到就走「模型交易」）")
    print("    2. 在策略区（图标/列表）空白处 **右键**")
    print("    3. 选「导入本地策略」/「本地.rzrk导入」→ 文件类型选 *.py")
    print("    4. 选中：%s" % sfile)
    print()
    print("  [!!] 为什么路径 A 是推荐、路径 B 只够查行情：")
    print("       QMT 只把 passorder / get_trade_detail_data 注入**在终端进程内被公式")
    print("       引擎挂载**的那个文件命名空间。登记成独立进程时 QMT 走")
    print("       `pythonw.exe -u <file> <userdata>`，该进程**一个注入函数都拿不到** ——")
    print("       agent 心跳新鲜、PROBE 通过，但 injected=[]、can_submit=false，")
    print("       下单/查询一律失败。2026-10-14 本机实测：QMT_WORK_AGENT 是全部日志里")
    print("       **唯一**出现 `execude cmd: -u ...` 的策略，而 QMT 自带的可交易策略")
    print("       （新股申购 / 国债逆回购 / ZT996）都走 `doRun: ... m_scriptType:1`。")
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
            # ★ 登记 ≠ 能下单：还要看它被 QMT 以哪种形态运行。独立进程形态
            #   （明文源码 + `execude cmd -u`）永远拿不到交易函数注入。
            mode = run_mode_of_strategy(qmt, fname)
            if mode["mode"] == "independent":
                print("[FAIL] 但运行形态是**独立进程** ⇒ 仍不能下单。")
                for ev in mode["evidence"]:
                    print("       · %s" % ev)
                print("       请在编辑器里把类型改成「内置Python」并重新编译，然后重启策略。")
                return 2
            if mode["mode"] == "formula":
                print("[OK] 运行形态：内置Python（公式，进程内）—— 具备下单能力")
            else:
                print("[i] 运行形态尚未判定（策略还没被 QMT 启动过？）")
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
        # ★ 2026-10-09 修：QMT 运行期这两个文件被**整文件字节区间锁**，读取/复制会抛
        #   `PermissionError`。旧实现没有再包一层 try ⇒ `inspect --force` 直接以
        #   traceback 退出：另一个文件的信息也拿不到，用户更看不出「是锁、不是文件
        #   损坏」。而 `--force` 的语义**本就是**「只做只读尝试」——只读尝试失败必须
        #   如实降级，不能把整条命令打死。
        #   这里逐文件降级：锁住的照实说锁住（并**保留 `PermissionError` 关键字**，
        #   因为 qmt_diag_report.py 依赖它在输出里出现来汇总「注册树读不到」这条
        #   问题），没锁的照常输出。
        raw = None
        try:
            shutil.copy2(src, dst)
            raw = open(src, "rb").read()
        except PermissionError as exc:
            print("[!] %s 读取失败：PermissionError: %s" % (name, exc))
            print("     ↳ QMT 运行期该文件被整文件锁（策略注册树容器），属预期限制；"
                  "关闭 QMT 后即可完整读取。")
        except OSError as exc:
            print("[!] %s 读取失败：%s: %s" % (name, type(exc).__name__, exc))
        if raw is None:
            continue
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


def cmd_discover(args):
    """探测环境并打印机器可读结果。

    用途：批处理 / 其他语言写的引导脚本想知道「这台机器的 QMT 装在哪」，
    不必自己扫描全盘。输出形如::

        QMT_DIR=P:\\stock\\gd_qmt
        QMT_STRATEGY_DIR=P:\\stock\\gd_qmt\\python
        QMT_RUNNING=XtItClient.exe
        BUNDLE_FILE=<strategy_dir>\\qmt_work_agent.py

    未找到时输出 ``QMT_DIR=(none)``，退出码仍为 0 —— 探测是**只读**动作，
    不该用非零退出码表示「没装 QMT」，否则引导脚本会把正常状态当成错误。
    """
    qmt = find_qmt_dir(args.qmt_dir)
    if qmt:
        print("QMT_DIR=%s" % qmt.replace("\\", "/"))
        print("QMT_STRATEGY_DIR=%s" % strategy_dir(qmt).replace("\\", "/"))
        print("QMT_RUNNING=%s" % (",".join(qmt_running()) or "(none)"))
    else:
        print("QMT_DIR=(none)")
    return 0


def cmd_check(args):
    qmt = find_qmt_dir(args.qmt_dir)
    print("QMT 安装目录 : %s" % (qmt or "未找到"))
    if not qmt:
        return 1
    sdir = args.strategy_dir or strategy_dir(qmt)
    print("策略目录     : %s" % sdir)
    # ★ 同 register：文件名尊重 --filename（默认 qmt_work_agent.py，本机用
    #   --filename QMT_WORK_AGENT.py），否则体检会对着不存在的路径报「缺失」。
    fname = getattr(args, "filename", "") or (args.strategy + ".py")
    top = os.path.join(sdir, fname)
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

    # ---- 运行形态：这才是「连上了却永远下不了单」的直接判据 ----
    #
    # ★ 为什么必须查：QMT 只把 passorder / get_trade_detail_data 注入**在终端进程内
    #   被公式引擎挂载**的那个文件命名空间。登记成「独立进程 / 原生 Python」时，
    #   QMT 走 `pythonw.exe -u <file> <userdata>`，该进程拿不到任何注入函数
    #   （实测 injected=[]、trade_surface.can_submit=false）⇒ agent 心跳/ PROBE
    #   全绿，却**永远下不了单**。2026-10-14 本机取证见 run_mode_of_strategy。
    mode = run_mode_of_strategy(qmt, fname)
    label = {"formula": "内置Python（公式，进程内）—— 具备下单能力",
             "independent": "**独立进程（原生 Python）—— 永远下不了单**",
             "unknown": "无法判定"}[mode["mode"]]
    if not mode.get("certain") and mode["mode"] != "unknown":
        label += "（推断，未由日志确认）"
    print("\n运行形态     : %s" % label)
    for ev in mode["evidence"]:
        print("               · %s" % ev)
    if mode["mode"] == "independent":
        print("\n" + "!" * 70)
        print("[FAIL] 该策略被 QMT 以**独立进程**方式运行 ⇒ 终端不会注入任何交易函数。")
        print("       症状：agent 心跳新鲜、PROBE 通过，但 injected=[]、")
        print("             trade_surface.can_submit=false，下单/查询一律失败。")
        print("       修法：在 QMT 的策略编辑器里把它改成**内置Python**形态")
        print("             （编辑器里的类型/运行方式选择；不要选「原生Python/独立进程」），")
        print("             重新**编译/保存**后重启策略。")
        print("       自证：本机日志里该策略应只出现 `doRun: ... m_scriptType:1 ...`，")
        print("             且**不再**出现 `execude cmd: -u \"<本文件>\"`；")
        print("             且 bridge_dir/agent_status.json 的 runtime_mode 变成")
        print("             `qmt_formula`、injected 含 passorder。")
        print("!" * 70)
        # ★ 退出码必须如实（2026-10-14）：体检**打出 [FAIL] 却 return 0** 属于本项目
        #   最忌讳的「假绿灯」—— `if qmt_agent_deploy.py check; then echo ok; fi`
        #   会得到「一切正常」，而这台机器**一单都下不出去**。
        #   2 = 「已部署但需要人工动作才能下单」，与 `register --wait` 超时的退出码
        #   以及 `qmt_agent_verify.py` 的 0/2 语义保持一致；1 仍留给「环境缺失」
        #   （QMT 未找到）。本机实测：改前此形态恒 0，改后返回 2。
        return 2
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="QMT agent 一键部署/巡检")
    ap.add_argument("cmd", choices=("deploy", "config", "inspect", "check", "register", "discover"))
    ap.add_argument("--qmt-dir", help="QMT 安装目录（默认自动探测）")
    ap.add_argument("--strategy-dir", help="策略目录（默认 <qmt>/python）")
    ap.add_argument("--filename", default="qmt_work_agent.py",
                    help="bundle 落盘文件名。必须与**注册树里的条目指向的文件名**一致"
                         "（本机实测注册名是大写 QMT_WORK_AGENT → 用 "
                         "--filename QMT_WORK_AGENT.py）")
    ap.add_argument("--strategy", default="qmt_work_agent", help="策略名（注册态核对用）")
    ap.add_argument("--bridge-dir", help="桥目录")
    ap.add_argument("--token", help="auth token")
    ap.add_argument("--trading", dest="trading", action="store_true", default=None,
                    help="打开下单开关")
    ap.add_argument("--no-trading", dest="trading", action="store_false",
                    help="关闭下单开关")
    ap.add_argument("--encoding", default="utf-8",
                    choices=["utf-8", "gbk", "gb18030"],
                    help="bundle 源码编码。**默认 utf-8（必须）** —— 2026-10-08 实测："
                         "QMT 内置 bin.x64/pythonw.exe（Python 3.6.8）的 tokenizer 读 "
                         "#coding:gbk / gb18030 的源文件会在特定内容下报 "
                         "`SyntaxError: encoding problem` / `invalid token`，"
                         "策略进程 return code:1、模型交易里只见「启动即停止」。"
                         "utf-8（有无 cookie 均可）在 67KB 全量中文下稳定跑通。"
                         "gbk/gb18030 仅留给「不在本机跑、只给编辑器阅读」的场景。")
    ap.add_argument("--no-run-check", action="store_true",
                    help="deploy: 跳过「用 QMT 内置解释器把刚写出的 bundle 真跑一遍」"
                         "的前置门禁（默认开启，跑不起来就中止部署）")
    ap.add_argument("--wait", type=int, default=0,
                    help="register: 轮询等待注册生效的秒数（0=不等待）")
    ap.add_argument("--reveal", action="store_true",
                    help="deploy/register: 打开资源管理器并选中 bundle 文件"
                         "（把导入时的「找文件」一步降到一次点击；只开 explorer，"
                         "不触碰 QMT 进程）")
    ap.add_argument("--force", action="store_true", help="inspect: 忽略 QMT 运行状态")
    ap.add_argument("--txt", action="store_true",
                    help="deploy: 同目录额外产一份 .txt 副本（内容与 .py 相同，后缀改 .txt）。"
                         "用途：QMT「新建策略 → 粘贴代码」路径 —— 记事本打开 .txt 粘贴比 IDE 打开 .py 稳。"
                         "默认关闭（可用 --reveal 定位 .py 文件）。")
    args = ap.parse_args(argv)
    return {"deploy": cmd_deploy, "config": cmd_config, "inspect": cmd_inspect,
            "check": cmd_check, "register": cmd_register,
            "discover": cmd_discover}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
