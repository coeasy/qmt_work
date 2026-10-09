# -*- coding: utf-8 -*-
"""把 QMT agent bundle **在本地真的跑起来**，并给出可判定的结论。

为什么要这个工具（而不是继续"看代码觉得没问题"）
------------------------------------------------
2026-10-08 的真机事故：部署到 `python/` 的 `QMT_WORK_AGENT.py` 是 **gb18030** 编码，
QMT 内置的 `bin.x64/pythonw.exe`（Python 3.6.8）tokenizer 直接报
``SyntaxError: encoding problem: gb18030``，进程 ``return code:1``，
在「模型交易」里表现为「点运行 → 立刻停止」，而且**一条自检都不落盘**。
从"源码看着对"到"真的能跑"之间隔着整整一层 —— 本工具就是补上这一层。

两种模式，对应 QMT 的两种真实挂载方式
------------------------------------
``framework``（公式模式）
    复刻终端的加载方式：把源码 ``exec(compile(src, '<string>', 'exec'), ns)`` 进一个
    **没有 __file__** 的命名空间，注入少量**测试替身**（显式声明、只验证协议与自举，
    不产出任何业务数据），然后调 ``init`` / ``handlebar``，再经文件桥发一条 PROBE
    并要求拿到响应。用来证明「入口契约 + 自举判别 + 桥协议」是对的。

``process``（独立进程模式，默认）
    完全按 QMT 的做法拉起：``<qmt>/bin.x64/pythonw.exe -u <bundle> <userdata> <ts>``，
    真实解释器、真实 xtquant、真实文件桥。断言进程**活着**、心跳**新鲜**、
    PROBE 有响应；可选再做一次真实行情探针（``--quote-probe``）。

判定三件套（缺一不可）
----------------------
  1. 进程/模块能**活过启动**（不是启动即退出）
  2. ``agent_status.json`` 心跳新鲜（"文件存在"不算数 —— QMT 退出后文件会留着）
  3. 文件桥能**来回**（req 进、resp 出，ok=true）

用法::

    python scripts/qmt_agent_local_run.py --bundle P:/stock/gd_qmt/python/QMT_WORK_AGENT.py
    python scripts/qmt_agent_local_run.py --mode framework            # 不依赖 QMT
    python scripts/qmt_agent_local_run.py --quote-probe 600036.SH --json
"""
from __future__ import print_function

import argparse
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

#: 心跳超过这个秒数视为「没在跑」（agent 写心跳间隔 10s，留 3 倍余量）。
STALE_S = 45

#: 默认的 QMT 安装目录候选（仅在用户没指定时用）。
_QMT_GUESS = (r"P:\stock\gd_qmt", r"D:\国金QMT交易端", r"D:\QMT", r"C:\QMT")


# ---------------------------------------------------------------------------
# 定位解释器 / userdata
# ---------------------------------------------------------------------------
def find_qmt_python(qmt_root):
    """返回 QMT 内置解释器路径（找不到返回 None）。

    ★ 用 ``pythonw.exe`` 而不是 ``bin.x64`` 下的其它 exe：这是 QMT
      ``CTradeStrategyData::doRun`` 建管道拉起策略时用的那一个
      （Formula.dll 里的字面量可自证），也就是**策略真正跑起来的那个解释器**。
    """
    if not qmt_root:
        return None
    for rel in (os.path.join("bin.x64", "pythonw.exe"),
                os.path.join("bin.x64", "python.exe")):
        p = os.path.join(qmt_root, rel)
        if os.path.isfile(p):
            return p
    return None


def guess_qmt_root(bundle_path=None, userdata=None):
    """猜 QMT 根目录：① 显式 userdata 的父目录 ② bundle 的 上上级
    ③ 常见安装位置。"""
    if userdata:
        cand = os.path.dirname(os.path.abspath(userdata))
        if find_qmt_python(cand):
            return cand
    if bundle_path:
        d = os.path.dirname(os.path.abspath(bundle_path))
        for _ in range(3):
            d = os.path.dirname(d)
            if d and find_qmt_python(d):
                return d
    for cand in _QMT_GUESS:
        if find_qmt_python(cand):
            return cand
    return None


# ---------------------------------------------------------------------------
# framework 模式：复刻终端的 exec 加载
# ---------------------------------------------------------------------------
#: 只用于「协议/自举」验证的测试替身名单。**显式声明、可数、不产出业务数据** ——
#: 与 backend/tests/fake_bigqmt_agent.py 同一性质：验证的是「协议有没有被正确处理」。
FAKE_INJECTED = ("passorder", "cancel", "get_trade_detail_data",
                 "get_stock_list_in_sector", "get_full_tick")


def _make_fake_injected(calls):
    def passorder(*a, **k):
        calls.append(("passorder", a))
        return 0

    def cancel(*a, **k):
        calls.append(("cancel", a))
        return 0

    def get_trade_detail_data(*a, **k):
        calls.append(("get_trade_detail_data", a))
        return []

    def get_stock_list_in_sector(*a, **k):
        calls.append(("get_stock_list_in_sector", a))
        return ["600036.SH"]

    def get_full_tick(codes=None, *a, **k):
        calls.append(("get_full_tick", codes))
        return {}

    return {"passorder": passorder, "cancel": cancel,
            "get_trade_detail_data": get_trade_detail_data,
            "get_stock_list_in_sector": get_stock_list_in_sector,
            "get_full_tick": get_full_tick}


class _FormulaContext(object):
    """公式模式下的 ContextInfo 替身（只提供方法面，不造行情数据）。"""

    def __init__(self):
        self.barpos = 0
        self.period = "1d"
        self.dividend_type = "none"

    def get_full_tick(self, codes=None):
        return {}

    def get_market_data(self, *a, **k):
        return {}


def _read_source(path):
    """按 PEP 263 cookie 读取源码（与解释器同口径），返回 (text, encoding)。"""
    raw = open(path, "rb").read()
    enc = "utf-8"
    head = raw.split(b"\n", 2)[:2]
    for line in head:
        if b"coding" in line:
            try:
                import re
                m = re.search(rb"coding[:=]\s*([-\w.]+)", line)
                if m:
                    enc = m.group(1).decode("ascii")
            except Exception:
                pass
            break
    return raw.decode(enc), enc


def run_framework(bundle_path, bridge_dir, timeout_s=10.0):
    """复刻终端的公式加载：exec 源码（**故意不给 __file__**）→ init → handlebar → PROBE。

    整个过程中 ``sys.argv``/``sys.path`` 都按终端公式引擎的形态摆好
    （argv[0] = 终端主程序、策略目录在 sys.path 上），否则 bundle 会把
    调用方（本脚本/测试进程）误当成"自己"，配置查找会整体落空。
    """
    saved_path = list(sys.path)
    saved_argv = list(sys.argv)
    sys.path.insert(0, os.path.dirname(os.path.abspath(bundle_path)))
    sys.argv = ["XtItClient.exe"]
    try:
        return _run_framework_inner(bundle_path, bridge_dir, timeout_s)
    finally:
        sys.path[:] = saved_path
        sys.argv[:] = saved_argv


def _run_framework_inner(bundle_path, bridge_dir, timeout_s):
    res = {"mode": "framework", "ok": False, "steps": [], "problems": []}
    text, enc = _read_source(bundle_path)
    res["encoding"] = enc

    calls = []
    ns = {"__name__": "qmt_formula_sim"}      # 故意不放 __file__
    ns.update(_make_fake_injected(calls))
    code = compile(text, "<string>", "exec")
    exec(code, ns)
    res["steps"].append({"name": "exec_without___file__", "ok": True,
                         "detail": "无 __file__ 仍能加载（QMT 公式模式的实际形态）"})

    for name in ("init", "handlebar", "Executor", "_OWN_NAMES", "should_autorun"):
        if name not in ns:
            res["problems"].append("顶层缺少 %s" % name)
    if res["problems"]:
        return res

    ctx = _FormulaContext()
    ns["init"](ctx)
    res["steps"].append({"name": "init", "ok": True, "detail": "init(ContextInfo) 返回"})

    status_path = os.path.join(bridge_dir, "agent_status.json")
    probe_path = os.path.join(bridge_dir, "probe_result.json")
    for _ in range(20):
        ns["handlebar"](ctx)
        if os.path.exists(status_path) and os.path.exists(probe_path):
            break
        time.sleep(0.05)
    if not os.path.exists(status_path):
        res["problems"].append("handlebar 20 次后仍没有 agent_status.json")
        return res
    res["steps"].append({"name": "heartbeat", "ok": True, "detail": status_path})

    probe = json.loads(io.open(probe_path, encoding="utf-8").read())
    injected = set(probe.get("injected") or [])
    fake = set(FAKE_INJECTED)
    extra = sorted(injected - fake)
    if extra:
        res["problems"].append(
            "probe 把非注入名字报成注入面（假绿灯）：%s" % extra[:8])
    missing = sorted(fake - injected)
    if missing:
        res["problems"].append("probe 漏报真实注入函数：%s" % missing)
    res["steps"].append({"name": "injected_face", "ok": not extra and not missing,
                         "detail": "injected=%s" % sorted(injected)})

    # 文件桥往返
    token = ""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(bundle_path)),
                            "agent_config.json")
    if os.path.exists(cfg_path):
        try:
            token = json.loads(io.open(cfg_path, encoding="utf-8").read()).get(
                "auth_token", "")
        except Exception:
            token = ""
    signal_id = "localtest%06d" % int(time.time() * 1000 % 1000000)
    envelope = {"v": 1, "signal_id": signal_id, "op": "PROBE", "auth": token,
                "params": {}, "ts": int(time.time() * 1000)}
    req_path = os.path.join(bridge_dir, "req", signal_id + ".json")
    with io.open(req_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(envelope, ensure_ascii=False))
    resp = None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ns["handlebar"](ctx)
        rp = os.path.join(bridge_dir, "resp", signal_id + ".json")
        if os.path.exists(rp):
            resp = json.loads(io.open(rp, encoding="utf-8").read())
            break
        time.sleep(0.05)
    if resp is None:
        res["problems"].append("文件桥往返超时：req 发出后没有 resp")
        return res
    if not resp.get("ok"):
        res["problems"].append("PROBE 返回 ok=false: %s" % resp.get("error"))
        return res
    actions = (resp.get("result") or {}).get("agent", {}).get("actions") or []
    res["steps"].append({"name": "bridge_roundtrip", "ok": True,
                         "detail": "PROBE ok=true，actions=%d 个" % len(actions)})
    res["actions"] = len(actions)
    res["ok"] = not res["problems"]
    return res


# ---------------------------------------------------------------------------
# process 模式：按 QMT 的做法真拉起进程
# ---------------------------------------------------------------------------
def _read_bridge_dir_from_config(bundle_dir):
    cfg_path = os.path.join(bundle_dir, "agent_config.json")
    if not os.path.exists(cfg_path):
        return None, ""
    try:
        cfg = json.loads(io.open(cfg_path, encoding="utf-8").read())
    except Exception:
        return None, ""
    return cfg.get("bridge_dir"), cfg.get("auth_token", "")


def _bridge_op(bridge_dir, token, op, params=None, timeout_s=12.0):
    """经文件桥发一条请求并等响应。返回 (resp_dict_or_None, note)。"""
    req_dir = os.path.join(bridge_dir, "req")
    resp_dir = os.path.join(bridge_dir, "resp")
    for d in (req_dir, resp_dir):
        if not os.path.isdir(d):
            return None, "桥目录缺少 %s" % d
    signal_id = "localrun%06d" % int(time.time() * 1000 % 1000000)
    envelope = {"v": 1, "signal_id": signal_id, "op": op, "auth": token,
                "params": params or {}, "ts": int(time.time() * 1000)}
    with io.open(os.path.join(req_dir, signal_id + ".json"), "w",
                 encoding="utf-8") as fh:
        fh.write(json.dumps(envelope, ensure_ascii=False))
    target = os.path.join(resp_dir, signal_id + ".json")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(target):
            try:
                return json.loads(io.open(target, encoding="utf-8").read()), ""
            except Exception as exc:
                return None, "响应解析失败: %s" % exc
        time.sleep(0.1)
    return None, "等响应超时（%ss）" % int(timeout_s)


def run_process(bundle_path, workdir, python_exe, userdata, max_seconds=25.0,
                quote_probe=None, log=print):
    """用真实解释器把 agent 当独立进程拉起，跑完三件套判定。"""
    res = {"mode": "process", "ok": False, "steps": [], "problems": [],
           "python": python_exe}
    sandbox = os.path.join(workdir, "strategy")
    bridge = os.path.join(workdir, "bridge")
    os.makedirs(sandbox)
    os.makedirs(bridge)
    shutil.copy2(bundle_path, os.path.join(sandbox, os.path.basename(bundle_path)))
    sandbox_bundle = os.path.join(sandbox, os.path.basename(bundle_path))
    # ★ 沙箱配置必须**带 auth_token**：不带 token 时 agent 会「放行但告警」，
    #   那样这条本地验证就测不到 token 校验这一段。
    with io.open(os.path.join(sandbox, "agent_config.json"), "w",
                 encoding="utf-8") as fh:
        fh.write(json.dumps({"bridge_dir": bridge.replace("\\", "/"),
                             "transport": "file", "auth_token": "local-run-token",
                             "trading_enabled": False, "poll_interval_ms": 200},
                            ensure_ascii=False, indent=2))
    env = dict(os.environ)
    env["QMT_WORK_AGENT_MAX_SECONDS"] = str(max_seconds)
    env.pop("QMT_WORK_AGENT_NO_AUTORUN", None)
    argv = [python_exe, "-u", sandbox_bundle, userdata or workdir, str(int(time.time() * 1000))]
    res["argv"] = argv
    proc = subprocess.Popen(argv, cwd=sandbox, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # 1) 活过启动 + 心跳新鲜
        status_path = os.path.join(bridge, "agent_status.json")
        deadline = time.time() + 15.0
        hb = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if os.path.exists(status_path):
                try:
                    hb = json.loads(io.open(status_path, encoding="utf-8").read())
                    if int(time.time() * 1000) - int(hb.get("ts", 0)) < STALE_S * 1000:
                        break
                except Exception:
                    hb = None
            time.sleep(0.2)
        if proc.poll() is not None and hb is None:
            out, err = proc.communicate()
            res["problems"].append(
                "进程启动即退出（return code=%s）—— 这就是 QMT 里"
                "「点运行马上停止」的形态" % proc.returncode)
            res["exit_code"] = proc.returncode
            res["stderr"] = (err or b"").decode("utf-8", "replace")[-1500:]
            res["stdout"] = (out or b"").decode("utf-8", "replace")[-800:]
            return res
        if hb is None:
            res["problems"].append("等不到新鲜心跳（agent_status.json）")
            return res
        res["steps"].append({"name": "alive_with_fresh_heartbeat", "ok": True,
                             "detail": "pid=%s py=%s agent_ver=%s runtime_mode=%s"
                                       % (proc.pid, hb.get("py"),
                                          hb.get("agent_ver"),
                                          hb.get("runtime_mode"))})
        res["agent"] = {"py": hb.get("py"), "agent_ver": hb.get("agent_ver"),
                        "runtime_mode": hb.get("runtime_mode"),
                        "injected": hb.get("injected")}
        token = "local-run-token"

        # 2) 文件桥往返（PROBE）
        resp, note = _bridge_op(bridge, token, "PROBE")
        if resp is None:
            res["problems"].append("PROBE 无响应: %s" % note)
            return res
        if not resp.get("ok"):
            res["problems"].append("PROBE 返回 ok=false: %s" % resp.get("error"))
            return res
        agent_meta = (resp.get("result") or {}).get("agent") or {}
        res["steps"].append({"name": "bridge_roundtrip", "ok": True,
                             "detail": "PROBE ok=true，py=%s，actions=%d"
                                       % (agent_meta.get("py"),
                                          len(agent_meta.get("actions") or []))})
        res["actions"] = len(agent_meta.get("actions") or [])

        # 3) 可选：真实行情探针（走 xtdata，不伪造）
        if quote_probe:
            q, qnote = _bridge_op(bridge, token, "QUERY_QUOTE",
                                  {"codes": [quote_probe]}, timeout_s=20.0)
            if q is None:
                res["quote"] = {"ok": False, "detail": "无响应: %s" % qnote}
            else:
                res["quote"] = {"ok": bool(q.get("ok")) and q.get("result") is not None,
                                "detail": (q.get("error") or "")[:200] or "已回包",
                                "result": q.get("result")}
        res["ok"] = not res["problems"]
        return res
    finally:
        # 优雅退出：置 STOP 文件（agent 的主循环会看到），超时再 kill
        try:
            io.open(os.path.join(bridge, "STOP"), "w").write("stop")
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            out, err = proc.communicate(timeout=5)
            res.setdefault("stdout", (out or b"").decode("utf-8", "replace")[-1500:])
            res.setdefault("stderr", (err or b"").decode("utf-8", "replace")[-1500:])
            res.setdefault("exit_code", proc.returncode)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _default_bundle():
    for pat in (os.path.join(_ROOT, "dist", "qmt_work_agent.py"),
                os.path.join(_ROOT, "dist", "QMT_WORK_AGENT.py"),
                r"P:\stock\gd_qmt\python\QMT_WORK_AGENT.py"):
        for p in glob.glob(pat):
            if os.path.exists(p):
                return p
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="本地真跑 QMT agent bundle")
    ap.add_argument("--bundle", help="bundle 路径（默认 dist/ 或已部署的那份）")
    ap.add_argument("--mode", choices=("auto", "process", "framework"), default="auto",
                    help="auto: 找得到 QMT 内置解释器就跑 process，否则 framework")
    ap.add_argument("--python", help="解释器（默认 QMT 内置 pythonw.exe）")
    ap.add_argument("--qmt-dir", help="QMT 安装目录（用于定位内置解释器）")
    ap.add_argument("--userdata", help="传给策略的 userdata 目录（默认 <qmt>/userdata）")
    ap.add_argument("--keep", action="store_true", help="保留沙箱目录（排查用）")
    ap.add_argument("--quote-probe", help="额外做一次真实行情探针，如 600036.SH")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 结论")
    args = ap.parse_args(argv)

    bundle = args.bundle or _default_bundle()
    if not bundle or not os.path.exists(bundle):
        print("[FAIL] 找不到 bundle，请用 --bundle 指定")
        return 2
    bundle = os.path.abspath(bundle)

    root = args.qmt_dir or guess_qmt_root(bundle, args.userdata)
    py = args.python or find_qmt_python(root)
    mode = args.mode
    if mode == "auto":
        mode = "process" if py else "framework"

    workdir = tempfile.mkdtemp(prefix="qmt_agent_run_")
    try:
        if mode == "framework":
            bridge = os.path.join(workdir, "bridge")
            os.makedirs(os.path.join(bridge, "req"))
            os.makedirs(os.path.join(bridge, "resp"))
            # framework 模式用临时配置（bundle 自己找 <策略目录>/agent_config.json）
            sandbox = os.path.join(workdir, "strategy")
            os.makedirs(sandbox)
            shutil.copy2(bundle, os.path.join(sandbox, os.path.basename(bundle)))
            with io.open(os.path.join(sandbox, "agent_config.json"), "w",
                         encoding="utf-8") as fh:
                fh.write(json.dumps({"bridge_dir": bridge.replace("\\", "/"),
                                     "auth_token": "local-run-token",
                                     "trading_enabled": False,
                                     "poll_interval_ms": 200}, ensure_ascii=False))
            res = run_framework(os.path.join(sandbox, os.path.basename(bundle)), bridge)
        else:
            userdata = args.userdata
            if not userdata:
                userdata = os.path.join(root, "userdata") if root else ""
                if userdata and not os.path.isdir(userdata):
                    userdata = ""
            res = run_process(bundle, workdir, py, userdata,
                              quote_probe=args.quote_probe)

        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            _report(bundle, mode, py, res)
        return 0 if res.get("ok") else 1
    finally:
        if args.keep:
            print("[i] 沙箱保留在: %s" % workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def _report(bundle, mode, py, res):
    print("=" * 72)
    print("QMT agent 本地真跑验证")
    print("=" * 72)
    print("bundle : %s" % bundle)
    print("mode   : %s" % mode)
    if py and mode == "process":
        print("python : %s" % py)
    if res.get("encoding"):
        print("编码   : %s" % res["encoding"])
    print("-" * 72)
    for s in res.get("steps") or []:
        print("  [%s] %-30s %s" % ("OK" if s["ok"] else "!!", s["name"],
                                   s.get("detail", "")))
    if res.get("quote") is not None:
        q = res["quote"]
        print("  [%s] %-30s %s" % ("OK" if q.get("ok") else "--", "quote_probe",
                                   q.get("detail", "")))
        if q.get("result"):
            print("        结果: %s" % json.dumps(q["result"], ensure_ascii=False)[:400])
    for p in res.get("problems") or []:
        print("  [FAIL] %s" % p)
    if res.get("stderr"):
        print("-" * 72)
        print("子进程 stderr（末尾）:\n%s" % res["stderr"])
    print("-" * 72)
    print("结论: %s" % ("可以真的运行 ✔" if res.get("ok") else "跑不起来 ✘"))
    if not res.get("ok"):
        print("下一步: ① 确认 bundle 用 UTF-8 编码部署（gbk/gb18030 会让 QMT 内置 "
              "py3.6 报 encoding problem）")
        print("        ② 确认带 __main__ 自举（否则进程启动即退出）")
        print("        ③ 确认 agent_config.json 的 bridge_dir 两端一致")


if __name__ == "__main__":
    sys.exit(main())
