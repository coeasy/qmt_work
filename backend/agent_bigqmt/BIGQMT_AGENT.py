# -*- coding: utf-8 -*-
"""qmt_work 大 QMT 桥接 —— 策略端入口。

=========================== 部署到讯投/国投 QMT 的步骤 ===========================
1. 把整个 agent_bigqmt/ 目录拷进 QMT 的 Python 策略目录
   （常见：<QMT安装目录>/python/ ，具体以券商的 QMT「策略→Python策略」为准）；
2. 同目录下放 agent_config.json（内容见 agent_config.example.json）：

       {
         "bridge_dir": "C:/Users/<你>/qmt_work/bigqmt_bridge",
         "transport": "file",
         "auth_token": "<与 qmt_work 端一致>",
         "trading_enabled": false,
         "poll_interval_ms": 500
       }

   ★ bridge_dir 必须与 qmt_work 连接配置里的 bridge_dir **完全一致**（这是日常
     排障第一名：两边路径不一致时表现为「一直超时」，而不是「路径错误」）。
3. 在 QMT 里新建 Python 策略，选中本文件，运行。
4. trading_enabled 默认 false —— 确认连通后再置 true 才允许下单。
===============================================================================

★ py3.6 兼容铁律（本机是大 QMT 内置的 Python 3.6.x）：
  无 dataclasses、无 walrus(:=)、无 asyncio/shared_memory、只用标准库。
  CI 有 AST 白名单闸，误用 3.7+ 语法会被拦下。

★ 唯一来源捕获（最重要的一条）：
  QMT 只往**被挂载的这个入口文件**的命名空间注入全局函数。
  必须通过 ``capture_qmt_injected_funcs(globals())`` 从唯一来源取，**严禁手抄
  函数名单** —— 手抄表漏一个函数，就会把「桥的 bug」误报成「终端没有该接口」。

两种运行形态（都是实测，别想当然）
----------------------------------
1. **公式模式**：终端把源码 ``exec(compile(src, '<string>', 'exec'), ns)`` 进它自己的
   命名空间，注入函数在下单/查询前就位。此时**没有 __file__**
   （本文件已改成逐级退让解析，见 ``_resolve_self_dir``）。
2. **独立进程模式**：QMT「模型交易 → 运行」是
   ``pythonw.exe -u <策略.py> <userdata> <时间戳>``（XtClient 日志的
   ``execude cmd`` + ``return code`` 可自证），解释器是内置 Python 3.6.8。
   该模式**没有任何注入函数**，但 ``xtquant.xtdata`` 可导入 —— 本文件的
   ``__main__`` 自举（``run_standalone``）在此模式下常驻主循环并如实转发行情接口。

★ 源码编码铁律（血的教训，2026-10-08）：
  必须用 **UTF-8** 部署。QMT 内置 Python 3.6.8 的 tokenizer 在处理
  ``#coding:gbk`` / ``#coding:gb18030`` 的源文件时会在特定内容下报
  ``SyntaxError: encoding problem: <enc>`` / ``invalid token``，
  导致策略进程 ``return code:1``、模型交易里只看到「启动即停止」。
  同一个文件内容改成 UTF-8（有无 cookie 均可）在 67KB 中文下稳定跑通。
"""
from __future__ import print_function

import json
import os
import sys
import time
import traceback

def _resolve_self_dir():
    """本文件所在目录。

    ★ 为什么不能直接用 ``os.path.dirname(__file__)``（P0，2026-10-08 实测）：
      QMT 的公式引擎是把源码 ``exec(compile(src, '<string>', 'exec'), ns)`` 进
      终端自己的命名空间（日志里 ``File "<string>", line N, in <module>`` 可自证），
      那个命名空间**没有 __file__** ⇒ 模块级引用 ``__file__`` 直接抛 NameError，
      策略连 init 都进不去，且**一条自检都不会落盘**（表现为「点了运行没反应」）。
      这里按 ① ``__file__`` ② ``argv[0]``（独立进程模式下就是本 .py 的路径，
      QMT 的 ``pythonw.exe -u <策略.py> <userdata> <ts>`` 即此形态）
      ③ ``sys.path[0]`` ④ ``cwd`` 逐级退让，绝不因为拿不到而崩。
    """
    try:
        path = __file__
    except NameError:
        path = ""
    if path:
        try:
            return os.path.dirname(os.path.abspath(path))
        except Exception:
            pass
    argv0 = ""
    try:
        if getattr(sys, "argv", None):
            argv0 = sys.argv[0] or ""
    except Exception:
        argv0 = ""
    if argv0 and os.path.splitext(argv0)[1].lower() == ".py":
        try:
            return os.path.dirname(os.path.abspath(argv0))
        except Exception:
            pass
    try:
        if getattr(sys, "path", None) and sys.path[0]:
            return os.path.abspath(sys.path[0])
    except Exception:
        pass
    try:
        return os.getcwd()
    except Exception:
        return "."


_HERE = _resolve_self_dir()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_HERE_MARKER = "qmt_work_bigqmt_agent"

#: agent 自身版本（与 qmt_api.VERSION 独立演进：这是"桥壳"的版本号）。
#: 打在 probe/status 里，外部端能一眼区分「旧 agent 跑在新后端上」。
_AGENT_VERSION = "1.2.0"

#: 本文件自己定义的顶层名字（生成器按 bundle 的 AST **精确注入**；源码直部署时
#: 由 ``_is_self_defined`` 兜底）。用途见 ``capture_qmt_injected_funcs``。
_OWN_NAMES = frozenset()

#: 单文件 bundle 打包时由生成器注入（一键部署）；None = 只读本地配置文件。
#: 注入后即使 QMT 策略目录只放一个 .py 也能工作（零额外文件）。
EMBEDDED_CONFIG = None

#: 心跳文件写入间隔（秒）。外部端据此判断「策略是否真的在跑」，
#: 而不是靠「文件在不在」猜 —— 这正是减少人工核验的关键。
STATUS_INTERVAL_S = 10.0

_STATE = {
    "injected": {},       # 捕获到的注入函数
    "ctx": None,          # ContextInfo
    "cfg": {},
    "executor": None,
    "seq": 0,             # 事件序号（合成流用它排序/去重）
    "order_seen": {},     # 委托差分基线
    "trade_seen": {},     # 成交差分基线
    "last_poll": 0.0,
    "primed": False,
    "started_at": 0.0,
    "last_status": 0.0,   # 心跳节流
    "last_error": "",     # 主循环最近一次异常（写进心跳，外部端可见）
    "standalone": False,  # 是否由本文件的 __main__ 自举（独立进程模式）
    "qmt_root": "",       # 独立进程模式下从 argv[1](userdata) 反推出的 QMT 根目录
    "forwarded": [],      # 独立进程模式下从 xtdata 转发进来的真实接口名
}


def log(msg):
    """统一输出前缀，便于在 QMT 日志里 grep。

    ★ 子系统的状态要在做**任何 IO 之前**先发布到 stderr（这是 qmt_work 的血的
    教训：QMT 侧的桥进程若在 stderr 就绪前就开始 IO，外部端会等到超时才知道
    它其实早就活着 —— 实测把启动耗时从 300s 降到 41.87s）。
    """
    try:
        print("[%s][%s] %s" % (_HERE_MARKER, time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _is_self_defined(value):
    """value 是否由本文件自己定义（QMT 注入的函数不满足）。

    在 QMT 公式模式下源码是被 exec 进终端自己的命名空间的，于是本文件定义的
    函数 ``__module__`` 就等于那个命名空间的 ``__name__``；终端注入的函数来自
    别处（``__module__`` 为 None 或其它模块名）。
    """
    try:
        return getattr(value, "__module__", None) == __name__
    except Exception:
        return False


def capture_qmt_injected_funcs(ns):
    """从当前命名空间捕获 QMT 注入的全局函数（唯一来源）。

    不做任何「应该有哪些函数」的假设：这里有什么就捕获什么，
    能力的判定交给 probe（实际调用一次才知道）。

    ★ 必须把**我们自己的**顶层名字排除掉（2026-10-08 修，假绿灯家族）：
      公式模式把源码 exec 进同一个命名空间后，``globals()`` 里同时住着终端注入的
      函数**和本文件自己定义的 30+ 个函数**。不排除就等于把 ``Executor`` /
      ``init`` / ``handlebar`` 全报成「终端提供的下单/查询接口」，外部端据此
      构建的能力面**整片是假的**（实测 bundle 会报出 37 个"注入函数"，
      其中真正的只有 2 个）。排除判据两条，任一命中即排除：
        ① 名字在 ``_OWN_NAMES``（生成器按 bundle 的 AST 精确注入，零手抄）；
        ② 值的 ``__module__`` 正是本模块（源码直部署路径的兜底）。
    """
    injected = {}
    for name, value in list(ns.items()):
        if name.startswith("__"):
            continue
        if name in _OWN_NAMES:
            continue
        if not callable(value):
            continue
        if _is_self_defined(value):
            continue
        injected[name] = value
    return injected


def _config_candidates():
    """配置查找顺序（兼容旧部署 + 单文件 bundle）。

    ★ 为什么不是单一路径：QMT 的策略列表来自客户端**持久化注册树**
    （``config/user/root/configFormula``，md5 相同的两个文件一个在列表一个
    不在即可自证），**不是** ``python/`` 目录扫描；注册条目记的是相对策略目录
    的路径，实践上入口仍是顶层单文件。把 agent 做成顶层单文件后，配置文件
    可能留在旧的 ``agent_bigqmt/`` 子目录里 —— 找不到就直接报错会逼用户搬
    配置，所以这里按候选顺序找，找到即用。
    """
    return [
        os.path.join(_HERE, "agent_config.json"),
        os.path.join(_HERE, "agent_bigqmt", "agent_config.json"),
        os.path.join(os.path.dirname(_HERE), "agent_config.json"),
    ] + _config_candidates_from_argv()


def _config_candidates_from_argv():
    """从 ``argv[1]`` 反推的配置候选（补 ``_HERE`` 失准时的最后一道保险）。

    QMT 的独立进程模式会把 ``<qmt>/userdata`` 作为 argv[1] 传进来，由此可以定位
    ``<qmt>/python/agent_config.json``。而公式模式下源码是被 exec 进终端命名空间的
    （**没有 __file__**），``_resolve_self_dir`` 只能靠 argv[0]/sys.path[0] 猜 ——
    猜错时若没有这条保险，策略会因为「找不到配置」直接起不来。
    """
    out = []
    try:
        argv = getattr(sys, "argv", None) or []
        userdata = argv[1] if len(argv) > 1 else ""
    except Exception:
        userdata = ""
    if userdata and os.path.isdir(userdata):
        root = os.path.dirname(os.path.abspath(userdata))
        out.append(os.path.join(root, "python", "agent_config.json"))
        out.append(os.path.join(os.path.abspath(userdata), "agent_config.json"))
    return out


def load_config():
    cfg = None
    tried = []
    for path in _config_candidates():
        tried.append(path)
        if os.path.exists(path):
            # 显式 utf-8：Windows 内置 py3.6 默认 GBK，中文 bridge_dir 用默认编码读
            # 会乱码成「路径不存在」——这正是排障第一名的一个变种。
            with open(path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            cfg["_config_path"] = path
            break
    if cfg is None and isinstance(EMBEDDED_CONFIG, dict) and EMBEDDED_CONFIG:
        # 单文件 bundle：配置已内嵌（后端一键部署时注入），无需外部文件。
        cfg = dict(EMBEDDED_CONFIG)
        cfg["_config_path"] = "<embedded>"
    if cfg is None:
        raise RuntimeError(
            "未找到 agent_config.json（已尝试: %s）—— 请把 agent_config.example.json "
            "复制为 agent_config.json 并填写 bridge_dir" % "; ".join(tried))
    if not cfg.get("bridge_dir"):
        raise RuntimeError("agent_config 缺少 bridge_dir")
    _ensure_dirs(cfg["bridge_dir"])
    if not cfg.get("auth_token"):
        log("警告: 未配置 auth_token —— 任意进程都能向 bridge_dir 写请求，请尽快配置")
    # ★ 传输现实（诚实声明，避免客户端误配）：本 stdlib agent 只能走文件桥。
    #   redis/zmq 是后端侧可选 transport，但客户端内置 Python 3.6 标准库无法
    #   引入 redis/pyzmq，必须改用独立 agent 变体 —— 这里只警告、不报错、按 file 跑。
    transport = str(cfg.get("transport", "file")).lower()
    if transport != "file":
        log("警告: transport=%s 不被本 stdlib agent 支持，仅 'file' 可用；"
            "已忽略该配置，按 file 运行（低延迟通道需独立 agent 变体）" % transport)
    return cfg


def _ensure_dirs(root):
    for sub in ("req", "resp"):
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            os.makedirs(d)


# ---------------------------------------------------------------------------
# 自动自检 / 心跳
#
# ★ 为什么要做进 agent 而不是让用户手动跑 ENV_PROBE.py：
#   真机联调的最大摩擦是「界面里点一次 → 人工回报 → 再分析」的往返。
#   策略一旦被 QMT 启动，这里立刻自己把环境证据落盘，外部端直接读，
#   人的角色从「执行者」降为「旁观者」——这是减少人工干预的核心一步。
# ---------------------------------------------------------------------------
def _ctx_methods(ctx):
    """列出 ContextInfo 上**真实存在**的公开方法。

    ★ 唯一来源纪律（G3 闸门）：绝不手抄「ContextInfo 应该有哪些方法」的名单，
    直接问对象本身（``dir(ctx)``）。手抄名单的代价是把「桥的 bug」伪装成
    「终端没有某接口」—— 详见 ``scripts/check_bigqmt_agent_py36.py`` 的 G3。
    返回去私有项后的排序列表，任何一个 QMT 版本都如实反映，不需维护。
    """
    if ctx is None:
        return []
    try:
        names = dir(ctx)
    except Exception:
        return []
    return sorted(n for n in names if not n.startswith("_"))


def _probe_quote_call(injected, ctx):
    """**真调一次**行情 getter —— 「带上了函数」不等于「能拿到行情」。

    ★ 为什么要真调（2026-10-08 实测教训）：独立进程模式下我们把 xtdata 的接口
      转发进了命名空间，于是 probe 的 ``injected`` 面里就出现了 ``get_full_tick``。
      可本机实测该调用直接抛 ``Exception: 无法连接行情服务！``（xtquant 的本地
      行情服务端口不监听）。只看"函数在不在"就会把**行情能力报成 SUPPORTED**，
      外部端据此渲染的行情面板会永远空白 —— 正是本仓反复出现的假绿灯家族。

    返回 ``(ok, detail)``；没有该接口时返回 ``None``（不产生任何结论，
    绝不因为"我没看到"就断言"终端没有"）。
    """
    getter = None
    candidates = []
    if isinstance(injected, dict):
        candidates.append(injected.get("get_full_tick"))
    if ctx is not None:
        try:
            candidates.append(getattr(ctx, "get_full_tick", None))
        except Exception:
            pass
    for fn in candidates:
        if callable(fn):
            getter = fn
            break
    if getter is None:
        return None
    try:
        data = getter(["000001.SZ"])
    except Exception as exc:
        return False, "调用 get_full_tick 抛错: %s: %s" % (type(exc).__name__, exc)
    try:
        n = len(data) if data else 0
    except Exception:
        n = -1
    if n > 0:
        return True, "实调成功：000001.SZ 返回 %d 条" % n
    return False, "调用未抛错但返回空（行情服务未就绪/未订阅）"


def self_probe(cfg, injected, ctx, executor):
    """一次性环境自检（跑在 QMT 进程内），结果写 bridge_dir/probe_result.json。

    与独立 ENV_PROBE.py 的区别：这里**带上了真实的 executor 画像**（注入函数
    捕获、ContextInfo 方法面、trading_enabled、配置来源），是真正可用于判定
    「桥能不能用」的证据，而不是「导入能不能成功」的弱证据。
    """
    result = {"ts": int(time.time() * 1000), "source": "agent_self_probe",
              "steps": []}

    def step(name, ok, detail):
        result["steps"].append({"name": name, "ok": bool(ok), "detail": detail})

    step("python_version", True, sys.version.replace("\n", " "))
    # ★ 导入面必须与**真正要用的取用方式**一致（2026-10-08 实测）：
    #   QMT 内置 Python 3.6 里 `import xtdata` 会失败，但
    #   `from xtquant import xtdata` 是成功的 —— 拿前者当判据会得出
    #   「行情不可用」的假结论。
    for probe_import in ("xtquant", "xtquant.xtdata", "xtquant.xttrader"):
        try:
            if probe_import == "xtquant.xtdata":
                from xtquant import xtdata  # noqa: F401
            else:
                __import__(probe_import)
            step("import:" + probe_import, True, "导入成功")
        except Exception as exc:
            step("import:" + probe_import, False, "%s: %s" % (type(exc).__name__, exc))

    mode = "standalone_process" if _STATE.get("standalone") else "qmt_formula"
    step("runtime_mode", True,
         "%s（%s）" % (mode, "本文件 __main__ 自举，终端未注入下单函数"
                       if _STATE.get("standalone")
                       else "由终端公式引擎挂载，注入函数来自 globals()"))
    if _STATE.get("forwarded"):
        step("forwarded_from_xtdata", True,
             "%d 个: %s" % (len(_STATE["forwarded"]),
                            json.dumps(sorted(_STATE["forwarded"]), ensure_ascii=False)))
    # ★ 「实际调用一次才知道」纪律：函数在不在 ≠ 能力可用（见 _probe_quote_call）。
    quote_probe = _probe_quote_call(injected, ctx)
    _STATE["quote_call"] = quote_probe
    if quote_probe is not None:
        step("quote_call", quote_probe[0], quote_probe[1])

    captured = sorted(injected.keys())
    step("injected_funcs", bool(captured), json.dumps(captured, ensure_ascii=False))
    # ★ 唯一来源纪律（G3 闸门）：只如实列出**捕获到什么**，绝不手抄
    #   「终端应该有 X」的名单 —— 手抄名单会把桥的 bug 伪装成「终端缺能力」。
    #   期望集由外部端持有（它读 result["injected"]），入口文件不带名单。
    for fn in captured:
        step("captured:" + fn, True, "在入口命名空间已捕获")
    step("contextinfo_methods", ctx is not None,
         json.dumps(_ctx_methods(ctx) if ctx is not None else [], ensure_ascii=False))

    bdir = cfg.get("bridge_dir", "")
    try:
        test_path = os.path.join(bdir, "probe_write_test.txt")
        with open(test_path, "w", encoding="utf-8") as fh:
            fh.write("ok %s" % time.time())
        with open(test_path, "r", encoding="utf-8") as fh:
            back = fh.read()
        os.remove(test_path)
        step("bridge_dir_write", back.startswith("ok"),
             "bridge_dir=%s 读写删除均成功" % bdir)
    except Exception as exc:
        step("bridge_dir_write", False, "写 bridge_dir 失败（文件桥不可用!）: %s" % exc)

    result["bridge_dir"] = bdir
    result["config_path"] = cfg.get("_config_path", "")
    result["auth_token_set"] = bool(cfg.get("auth_token"))
    result["trading_enabled"] = bool(cfg.get("trading_enabled"))
    result["agent_ver"] = _AGENT_VERSION
    result["runtime_mode"] = mode
    result["qmt_root"] = _STATE.get("qmt_root", "")
    result["forwarded"] = sorted(_STATE.get("forwarded", []))
    result["injected"] = captured
    result["ctx_methods"] = _ctx_methods(ctx)
    # ★ P1 多标的账户类型能力面（R18）：外部端据此枚举 agent 支持哪些标的
    #   类型（stock/etf/future/option/credit）。key=name, value=opAccountType
    #   数值（0=stock 走标准 11-arg 签名，非 0 走扩展 12-arg 签名）。
    result["account_types"] = getattr(executor, "_account_types", {}) or {}
    result["default_account_type"] = cfg.get("default_account_type", "stock")
    if executor is not None:
        try:
            result["meta"] = executor.meta()
        except Exception:
            pass
    _write_json(os.path.join(bdir, "probe_result.json"), result)
    return result


def write_status(cfg, injected, ctx, executor, started_at, last_error=""):
    """心跳：外部端据此知道「策略真的在跑」，而不是靠超时反推。

    文件极小、写入频率有界（STATUS_INTERVAL_S），不会成为磁盘负担。
    """
    payload = {
        "ts": int(time.time() * 1000),
        "alive": True,
        "agent_ver": _AGENT_VERSION,
        "py": sys.version.split()[0],
        "started_at": int(started_at or 0),
        "uptime_s": int(time.time() - float(started_at or time.time())),
        "bridge_dir": cfg.get("bridge_dir", ""),
        "config_path": cfg.get("_config_path", ""),
        "trading_enabled": bool(cfg.get("trading_enabled")),
        "injected": sorted(injected.keys()),
        "ctx_methods": _ctx_methods(ctx),
        "runtime_mode": "standalone_process" if _STATE.get("standalone") else "qmt_formula",
        "last_error": last_error,
    }
    if executor is not None:
        try:
            payload["meta"] = executor.meta()
        except Exception:
            pass
    _write_json(os.path.join(cfg.get("bridge_dir", ""), "agent_status.json"), payload)
    return payload


def _write_json(path, payload):
    """原子写 JSON（tmp + replace），外部端永远读不到半份文件。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except UnicodeEncodeError:
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=True)
            os.replace(tmp, path)
        except Exception:
            log("写 %s 失败:\n%s" % (path, traceback.format_exc()))
    except Exception:
        log("写 %s 失败:\n%s" % (path, traceback.format_exc()))


def init(ContextInfo):
    """QMT 策略入口（老版本 QMT 用 init，新版亦有 handle_init 别名）。"""
    # ★ 先发布状态再做任何 IO
    log("agent 初始化开始")
    _STATE["injected"] = capture_qmt_injected_funcs(globals())
    _STATE["ctx"] = ContextInfo
    try:
        cfg = load_config()
        _STATE["cfg"] = cfg
        from qmt_api import Executor  # py3.6 相对 import 在 QMT 环境下不可靠，用同级绝对导入

        _STATE["executor"] = Executor(cfg, _STATE["injected"], ContextInfo)
        _STATE["started_at"] = time.time()
        # ★ 把启动时刻写进 cfg，使 Executor.meta() 的 uptime_s 诚实
        #   （否则 capacity 面会显示 uptime=0，违背「不伪造」纪律）。
        cfg["_started_at"] = _STATE["started_at"]
        log("agent 就绪: bridge_dir=%s funcs=%d trading=%s"
            % (cfg.get("bridge_dir"), len(_STATE["injected"]),
               cfg.get("trading_enabled", False)))
        # ★ 自动自检 + 首帧心跳：策略一被启动，外部端就能读到环境证据，
        #   不必再让人工去界面里跑一次探测脚本。
        try:
            probe = self_probe(cfg, _STATE["injected"], ContextInfo,
                               _STATE["executor"])
            bad = [s["name"] for s in probe["steps"] if not s["ok"]]
            log("自检完成: %d 项，异常项=%s"
                % (len(probe["steps"]), bad if bad else "无"))
        except Exception:
            log("自检失败(不阻断桥):\n" + traceback.format_exc())
        try:
            write_status(cfg, _STATE["injected"], ContextInfo,
                         _STATE["executor"], _STATE["started_at"])
        except Exception:
            log("写心跳失败(不阻断桥):\n" + traceback.format_exc())
    except Exception:
        log("agent 初始化失败:\n" + traceback.format_exc())
        raise


# 兼容不同券商 QMT 的入口命名
handle_init = init


def handlebar(ContextInfo):
    """QMT 每根bar/tick 调用 —— 桥的主循环挂在这里（不用自建线程）。

    为什么不自建线程：QMT 内置 Python 对多线程的支持参差不齐（部分券商版本
    直接崩）。用框架回调驱动是最稳的形态。
    """
    st = _STATE
    if st["executor"] is None:
        return
    cfg = st["cfg"] or {}
    interval = max(0.05, float(cfg.get("poll_interval_ms", 500)) / 1000.0)
    now = time.time()
    if now - st["last_poll"] < interval:
        return
    st["last_poll"] = now
    try:
        st["executor"].poll_once(_on_request, _emit_event)
        st["executor"].diff_events(st, _emit_event)
        # SUB_QUOTE 的落地形态：轮询 get_full_tick，变化写 events.ndjson
        st["executor"].quote_events(st, _emit_event)
        st["last_error"] = ""
    except Exception:
        tb = traceback.format_exc()
        lines = [ln for ln in tb.strip().splitlines() if ln.strip()]
        st["last_error"] = lines[-1] if lines else "unknown"
        log("主循环异常:\n" + tb)
    # ★ 心跳：有界节流写 agent_status.json —— 外部端以此判定「策略在跑」。
    if now - st["last_status"] >= STATUS_INTERVAL_S:
        st["last_status"] = now
        try:
            write_status(cfg, st["injected"], st["ctx"], st["executor"],
                         st["started_at"], st["last_error"])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# D6 回调双轨：部分券商终端会在**主线程**回调这两个入口文件全局函数。
# 定义 ≠ 绑定成功 —— 只有真的被调用过（executor.note_callback 计数）才在
# PROBE 上报 callback_bound=true；在此之前事件上界仍按轮询 POLL_DIFF 计。
# ---------------------------------------------------------------------------
def order_callback(ContextInfo, order_info):
    _on_broker_callback("order", order_info)


def deal_callback(ContextInfo, deal_info):
    _on_broker_callback("trade", deal_info)


def _on_broker_callback(kind, info):
    """实时回报 → 事件 + 写入差分基线（防 diff_events 对同一变化二次发事件）。"""
    st = _STATE
    executor = st["executor"]
    if executor is None:
        return
    try:
        row, key, fp = executor.note_callback(kind, info)
        if key:
            baseline = st["order_seen"] if kind == "order" else st["trade_seen"]
            baseline[key] = fp
        _emit_event(kind, row)
    except Exception:
        log("回调处理异常(%s):\n%s" % (kind, traceback.format_exc()))


def _on_request(envelope, executor):
    """处理一条请求：执行 → 原子写响应。返回 None（异常已在内部转成 ok=false）。"""
    return executor.execute(envelope)


def _emit_event(event_type, data):
    """追加一条事件到 events.ndjson（带上递增序号，便于外部端排序/去重）。"""
    st = _STATE
    st["seq"] += 1
    cfg = st["cfg"] or {}
    path = os.path.join(cfg.get("bridge_dir", ""), "events.ndjson")
    record = {"seq": st["seq"], "type": event_type, "data": data,
              "ts": int(time.time() * 1000)}
    try:
        _rotate_if_huge(path)
        # ndjson 是**每行一条**的容器：单行 write 在常规文件系统上是原子的，
        # 外部端按行解析时看不到半行（半行会被 JSON 解析失败跳过）。
        # D5：默认 ensure_ascii=False 保中文原样；agent_config.json 置
        # "ascii_only": true 时全转义（某些券商终端有中文审计拦截前科）。
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=bool(cfg.get("ascii_only"))) + "\n")
    except UnicodeEncodeError:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=True) + "\n")
        except Exception:
            log("写事件失败:\n" + traceback.format_exc())
    except Exception:
        log("写事件失败:\n" + traceback.format_exc())


def _rotate_if_huge(path, max_bytes=10485760):
    """事件文件超过 10MB 就轮转一次，避免长期运行把磁盘写满。

    保留一份（.1），够回溯最近的问题；再多就需要外部归档了。
    """
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            backup = path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(path, backup)
    except Exception:
        pass


def after_init(ContextInfo):  # pragma: no cover - QMT 可选入口
    log("after_init: 注入函数 %d 个" % len(_STATE["injected"]))


# ===========================================================================
# 独立进程模式自举（QMT「模型交易 → 运行」的真实启动方式）
#
# 真机证据（XtClient 主日志原文，2026-10-08 20:04:32）：
#
#   [TC::CTradeStrategyData::doRun] execude cmd:  -u
#       "P:\stock\gd_qmt\python\QMT_WORK_AGENT.py"
#       "P:\stock\gd_qmt\userdata" 1791461072325
#
#   → 紧接着 `return code:1` + `try stop`（181 ms 后）
#
# 对应 Formula.dll 里 `CTradeStrategyData::doRun`（create pipe + `pythonw.exe -u`），
# 实测该进程的参数形状为 `argv = [<策略.py>, <userdata>, <时间戳>]`，
# 解释器是 QMT 自带的 `bin.x64/pythonw.exe` = Python 3.6.8。
#
# ★ 两条结论（都是实测，不是推测）：
#   1. QMT 期待这个文件**自己跑起来并常驻**。缺 `__main__` 自举时进程定义完
#      函数就退出 —— 即使编码修对了，QMT 侧看到的仍是「启动即结束」。
#   2. 这种「脚本策略」模式**没有终端注入的下单/查询函数**（用内置解释器实测
#      globals 为空），但 `xtquant.xtdata` / `xtquant.xttrader` **可导入** ——
#      所以本模式如实提供**行情**能力；下单能力取决于能否连上 xtquant 交易
#      服务，连不上就如实报错，**绝不伪造成功**（零 mock 契约）。
# ===========================================================================
def _qmt_root_from_userdata(userdata):
    """从 QMT 传进来的 userdata 目录反推 QMT 根目录（<root>/userdata）。"""
    if not userdata:
        return ""
    try:
        return os.path.dirname(os.path.abspath(userdata))
    except Exception:
        return ""


def _augment_sys_path(root):
    """把 QMT 自带解释器的库目录补进 sys.path（只追加，绝不抢占标准库位置）。

    返回实际补进去的目录列表（probe 里如实上报，不做「应该能找到」的假设）。
    """
    added = []
    if not root:
        return added
    candidates = [
        os.path.join(root, "bin.x64", "Lib", "site-packages"),
        os.path.join(root, "bin.x64", "Lib"),
        os.path.join(root, "python"),
    ]
    for d in candidates:
        try:
            if os.path.isdir(d) and d not in sys.path:
                sys.path.append(d)
                added.append(d)
        except Exception:
            pass
    return added


def _import_xtdata():
    """尽力导入 QMT 行情模块；失败返回 None（绝不伪造）。

    ★ `import xtdata` 在 QMT 内置解释器里是失败的（xtdata 是 xtquant 的子模块），
      必须用 `from xtquant import xtdata` —— 拿前者当判据会误判「行情不可用」。
    """
    try:
        from xtquant import xtdata as _xtd
        return _xtd
    except Exception:
        pass
    try:
        import xtdata as _xtd2  # 某些发行版把 xtdata 直接暴露在顶层
        return _xtd2
    except Exception:
        return None


def _forward_xtdata_funcs(ns, xtdata):
    """把 xtdata 的**全部**公开可调用接口转发进入口命名空间。

    ★ 为什么是「全部」而不是挑几个：本仓铁律是**禁止手抄名单**（CI 的 G3 闸门
      就是拦这个）。手抄一份「行情函数应该有哪些」的清单必然随版本漂移，漏掉的
      那个会被误报成「终端没有该接口」。这里直接问模块本身（``dir(xtdata)``）。

    返回转发的名字列表（排序），供 probe 如实上报。
    """
    forwarded = []
    if xtdata is None:
        return forwarded
    try:
        names = dir(xtdata)
    except Exception:
        return forwarded
    for name in names:
        if name.startswith("_"):
            continue
        if name in ns:
            continue
        try:
            fn = getattr(xtdata, name)
        except Exception:
            continue
        if callable(fn):
            ns[name] = fn
            forwarded.append(name)
    return sorted(forwarded)


class _StandaloneContext(object):
    """独立进程模式下的 ContextInfo 替身。

    ★ 诚实边界：所有属性**转发到真实 xtdata**；导入失败/没有该方法就不提供
      （``Executor`` 用 ``getattr(ctx, name, None)`` 取，拿不到会如实报「未捕获」）。
      绝不造一个返回编造行情的假 getter —— 那是零 mock 契约的红线。
    """

    def __init__(self, xtdata, root):
        self._xtdata = xtdata
        self._root = root
        self.barpos = 0
        self.period = "1d"
        self.dividend_type = "none"

    def __getattr__(self, name):
        xt = self.__dict__.get("_xtdata")
        if xt is not None:
            try:
                fn = getattr(xt, name)
            except Exception:
                fn = None
            if callable(fn):
                return fn
        raise AttributeError(name)

    def __dir__(self):
        """唯一来源纪律：方法面直接问对象，不手抄「ContextInfo 应该有哪些方法」。"""
        names = ["barpos", "period", "dividend_type"]
        xt = self.__dict__.get("_xtdata")
        if xt is not None:
            try:
                names += [n for n in dir(xt) if not n.startswith("_")]
            except Exception:
                pass
        return sorted(set(names))


def _is_process_main():
    """本文件是否**真的是**被当作主程序执行的（而不是被 exec 进别人的命名空间）。

    QMT 公式模式把源码 exec 进终端自己的命名空间，此时 ``sys.modules['__main__']``
    是终端主模块、其 ``__dict__`` 与本文件的 ``globals()`` **不是同一个对象**。
    —— 这条判别保证「独立进程自举」绝不会在公式模式下把终端线程卡死。
    """
    try:
        main_mod = sys.modules.get("__main__")
        return main_mod is not None and getattr(main_mod, "__dict__", None) is globals()
    except Exception:
        return False


def should_autorun(argv=None):
    """是否应进入独立进程自举。三条同时成立才启动主循环：

      ① ``__name__ == "__main__"``（由调用点保证，这里再核一遍）
      ② ``sys.modules['__main__'].__dict__ is globals()``（真的是主程序）
      ③ ``argv[0]`` 指向一个 .py 文件（QMT：``pythonw.exe -u <策略.py> ...``）

    这样：
      * 公式模式（终端 exec 源码）→ ② 为假 ⇒ 只定义入口函数，不动终端线程；
      * 独立进程模式 / 开发机 ``python QMT_WORK_AGENT.py`` → 三条全真 ⇒ 自举；
      * 被当模块 import（测试、IDE 补全）→ ① 为假 ⇒ 不自举。

    需要在不启动主循环的前提下执行本文件时，置 ``QMT_WORK_AGENT_NO_AUTORUN=1``。
    """
    if __name__ != "__main__":
        return False
    raw = os.environ.get("QMT_WORK_AGENT_NO_AUTORUN", "")
    if raw not in ("", "0", "false", "False", "no"):
        return False
    if not _is_process_main():
        return False
    argv = list(argv if argv is not None else (getattr(sys, "argv", None) or []))
    if not argv:
        return False
    try:
        return os.path.splitext(argv[0] or "")[1].lower() == ".py"
    except Exception:
        return False


def _env_float(name, default):
    try:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return default
        return float(raw)
    except Exception:
        return default


def run_standalone(argv=None):
    """把本文件当**独立进程**跑起来（QMT 的「运行」就是这样拉起策略的）。

    返回进程退出码：0 正常退出；2 初始化失败；3 主循环异常终止。
    """
    argv = list(argv if argv is not None else (getattr(sys, "argv", None) or []))
    userdata = argv[1] if len(argv) > 1 else ""
    root = _qmt_root_from_userdata(userdata) or os.path.dirname(_HERE)
    _STATE["standalone"] = True
    _STATE["qmt_root"] = root
    added = _augment_sys_path(root)
    log("独立进程模式启动: userdata=%s root=%s sys.path+=%s" % (userdata, root, added))

    xtdata = _import_xtdata()
    ctx = _StandaloneContext(xtdata, root)
    forwarded = []
    if xtdata is not None:
        forwarded = _forward_xtdata_funcs(globals(), xtdata)
    _STATE["forwarded"] = forwarded
    log("独立进程模式: xtdata=%s 转发真实接口 %d 个"
        % ("可用" if xtdata is not None else "不可用(未导入)", len(forwarded)))

    try:
        init(ctx)
    except Exception:
        log("init 失败，进程无法常驻：\n" + traceback.format_exc())
        return 2

    # ★ 把「本模式到底能用什么」主动说出来，别让用户对着永远空白的面板猜
    #   （本仓最常见的故障形态就是"绿灯是另一个 bug 遮出来的"）。
    if not _STATE["injected"]:
        log("注意: 本模式**没有任何终端注入的函数**（独立进程模式的常态）—— "
            "下单/查询能力不可用。要拿到注入能力，需让本策略以**公式策略**的形态"
            "被终端挂载（同系统自带策略那样走 in-process 公式引擎），"
            "而不是以外部 Python 脚本策略运行。")
    quote_result = _STATE.get("quote_call")
    if quote_result is not None and not quote_result[0]:
        log("注意: 行情接口存在但**实调失败**（%s）—— 外部端会把行情判为不可用，"
            "这是事实、不是伪造；请先把 QMT 本地行情服务打通再谈能力面。"
            % quote_result[1])
    cfg = _STATE["cfg"] or {}
    interval = max(0.05, float(cfg.get("poll_interval_ms", 500)) / 1000.0)
    bridge_dir = cfg.get("bridge_dir", "")
    stop_file = os.path.join(bridge_dir, "STOP")
    limit = _env_float("QMT_WORK_AGENT_MAX_SECONDS", 0.0)
    log("主循环开始: interval=%.3fs bridge_dir=%s（置 QMT_WORK_AGENT_MAX_SECONDS "
        "或在本目录创建 STOP 文件可正常退出）" % (interval, bridge_dir))
    started = time.time()
    try:
        while True:
            handlebar(ctx)
            if limit > 0 and (time.time() - started) >= limit:
                log("达到 QMT_WORK_AGENT_MAX_SECONDS=%.1f，正常退出" % limit)
                break
            try:
                if os.path.exists(stop_file):
                    log("检测到 STOP 文件，正常退出")
                    break
            except Exception:
                pass
            # 半周期休眠：handlebar 自带节流，调用频率高于 interval 才能稳定命中
            time.sleep(max(0.05, interval / 2.0))
    except KeyboardInterrupt:
        log("收到键盘中断，正常退出")
    except Exception:
        log("主循环异常终止：\n" + traceback.format_exc())
        return 3

    try:
        write_status(cfg, _STATE["injected"], ctx, _STATE["executor"],
                     _STATE["started_at"], "exited")
    except Exception:
        pass
    log("进程正常退出")
    return 0


if __name__ == "__main__" and should_autorun():
    # QMT「模型交易 → 运行」= pythonw.exe -u <本文件> <userdata> <ts> ⇒ 自举常驻。
    # 公式模式（终端把源码 exec 进自己的命名空间）不会被这条命中，
    # 此时只提供 init/handlebar 等入口，不动终端线程。
    sys.exit(run_standalone())
