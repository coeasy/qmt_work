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
"""
from __future__ import print_function

import json
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_HERE_MARKER = "qmt_work_bigqmt_agent"

#: agent 自身版本（与 qmt_api.VERSION 独立演进：这是"桥壳"的版本号）。
#: 打在 probe/status 里，外部端能一眼区分「旧 agent 跑在新后端上」。
_AGENT_VERSION = "1.1.0"

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


def capture_qmt_injected_funcs(ns):
    """从当前命名空间捕获 QMT 注入的全局函数（唯一来源）。

    不做任何「应该有哪些函数」的假设：这里有什么就捕获什么，
    能力的判定交给 probe（实际调用一次才知道）。
    """
    injected = {}
    for name, value in list(ns.items()):
        if name.startswith("__"):
            continue
        if callable(value):
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
    ]


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
    for mod in ("xtquant", "xtdata", "xtquant.xttrader"):
        try:
            __import__(mod)
            step("import:" + mod, True, "导入成功")
        except Exception as exc:
            step("import:" + mod, False, "%s: %s" % (type(exc).__name__, exc))

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
