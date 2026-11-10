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
#: 1.3.0（2026-10-31）：新增**闭环模式**（`EMBEDDED_PLAN` 非空时走
#: `init`→柜台重建 / `handlebar`→`tick()` / 事件走日志行）。旧模式行为不变，
#: 但壳的语义变了 —— 外部端据版本号决定「要不要重部署」。
_AGENT_VERSION = "1.3.0"

#: 本文件自己定义的顶层名字（生成器按 bundle 的 AST **精确注入**；源码直部署时
#: 由 ``_is_self_defined`` 兜底）。用途见 ``capture_qmt_injected_funcs``。
_OWN_NAMES = frozenset()

#: 单文件 bundle 打包时由生成器注入（一键部署）；None = 只读本地配置文件。
#: 注入后即使 QMT 策略目录只放一个 .py 也能工作（零额外文件）。
EMBEDDED_CONFIG = None

#: 盘前烘焙进来的**条件计划**（新规闭环形态）。
#:
#: 非空 dict ⇒ 本策略走**闭环模式**：不读配置文件、不写心跳、不轮询 req/、
#: 不写 events.ndjson —— 因为 2026-10-23 起这些动作全部会抛
#: ``PermissionError: Forbidden FileIO``。它只做三件事：
#:   ① 启动时校验计划 + 从柜台重建持仓；
#:   ② 盘中在计划时间窗内自己算调仓意图并 ``passorder``；
#:   ③ 用 ``print("@@QMTW|…")`` 把事件回传给外部（客户端日志带出，外部 tail）。
#:
#: 由 ``scripts/gen_qmt_agent_bundle.py --plan`` 注入，人工覆盖一次即完成部署。
#: ★ 空 dict / None ⇒ 退回旧的「文件桥」模式（新规生效前仍可用）。
EMBEDDED_PLAN = None

#: bundle 标识（写进每条回传记录，对账时区分「哪一份部署产物」）。生成器注入，可空。
EMBEDDED_BUNDLE_ID = ""

#: 心跳文件写入间隔（秒）。外部端据此判断「策略是否真的在跑」，
#: 而不是靠「文件在不在」猜 —— 这正是减少人工核验的关键。
#: ★ 闭环模式下心跳改走日志行（``STATUS`` 记录），不落盘。
STATUS_INTERVAL_S = 10.0

#: 闭环模式的主循环节拍（秒）。窗口内每拍评估一次调仓意图。
#: ★ 不能用「每 tick 一次」：QMT 的 tick 频率是毫秒级，而 ``decide()`` 要查柜台
#:   与行情，拍太快会把柜台打爆；太慢则错过窗口尾部的成交机会。
TICK_INTERVAL_S = 3.0

#: 在途委托的**最短等待**：下单后这么久还没在柜台委托列表里看到，才允许重新决策。
#: ★ 这不是「超时重发」—— 重发前必须确认柜台**查不到**这单（见 §6.5）。
PENDING_GRACE_S = 30.0

#: 在途委托的**最长等待**：委托列表里看得到、但这么久还不成交/不回报 ⇒ 标 UNKNOWN
#: 并停止新开仓（不是撤单，也不是重发 —— 人去看一眼比程序猜一次便宜）。
PENDING_STALE_S = 300.0

#: 持仓**重新对齐**间隔（秒）。★ 这不是性能优化，是正确性要求：
#: ``deal_callback`` 在部分终端/版本上不触发（实测有只推委托不推成交的情形）。
#: 只靠回调更新持仓，策略会「下过一次单之后再也不动」，而且看起来一切正常。
#: 持仓是**真相源**，定期回柜台重读一次就能自愈。
POSITION_RESYNC_S = 60.0

#: 委托列表查询间隔（秒）。柜台查询有成本，3 秒一拍都查一次会把柜台打爆。
ORDER_QUERY_S = 15.0

#: 单笔最小股数（A 股 1 手）。用于「意图净额是否还值得发一单」的判断。
LOT_SIZE = 100

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
    # --- 闭环模式（EMBEDDED_PLAN 非空时才用到） ---------------------------
    "mode": "bridge",     # "bridge"（旧文件桥） / "closed"（新规闭环）
    "boot_id": "",        # 本次启动的唯一标识：解决「重启后 seq 归零」的去重歧义
    "boot_tag": "",       # boot_id 的短形式，拼进 cid（备注位有长度风险，越短越好）
    "plan": None,         # 校验归一后的计划
    "guard": None,        # 风控闸门
    "positions": None,    # PositionSource（柜台重建 + 成交增量）
    "nav": 0.0,           # 最近一次查到的总资产
    "last_nav_ts": 0.0,
    "last_tick": 0.0,
    "pending": {},        # 在途委托 {cid: {code, side, volume, ts, pos_before}}
    "order_seq": 0,       # 本日已发单序号（拼 cid 用）
    "last_resync": 0.0,   # 上次回柜台重读持仓的时刻
    "last_order_query": 0.0,  # 上次查委托列表的时刻
    "in_window": False,   # 上一拍是否在计划时间窗内（用于只报一次 WINDOW_CLOSED）
    "done_announced": False,  # 本日「已到位」是否已播报（只报一次，避免刷屏）
    "readonly_reason": "",  # 非空 ⇒ 只读；每个 tick 尝试自愈（柜台/行情恢复即解除）
    "fatal_reason": "",     # 非空 ⇒ 永久只读（计划/配置错，重试无意义）
    "once": {},           # 重复原因的节流表（避免每拍刷一条日志）
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


def capture_qmt_injected_funcs(ns, exclude=None):
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

    ★ **2026-10-09 修（同一家族的第二次，真机实测）**：还必须排除
      ``_forward_xtdata_funcs()`` **我们自己转发进来的 xtdata 接口**。
      独立进程模式下那 76 个接口被塞进 ``globals()``，本函数把它们**当成了终端注入**
      —— 实测 ``injected`` 与 ``forwarded`` **完全相同（76/76）**。两个后果：
        ① ``run_standalone()`` 里 ``if not _STATE["injected"]`` 那条关键警告
           （"本模式没有任何终端注入的函数 ⇒ 下单/查询能力不可用"）被**静默吞掉**：
           用户只看到一句行情报错，完全不知道下单根本不可能（本次用户的困惑正源于此）；
        ② ``Executor.meta()["funcs"]`` / probe 的 ``injected`` 报出 76 项，外部端
           （``connectors/generic.probe_capabilities``）据此把 quote / realtime /
           kline / instrument / calendar 判成 **SUPPORTED** —— 而实调是抛错的。
      排除后 ``injected`` 只含**终端注入**，字段名与语义一致。转发的 xtdata 接口
      仍经 ``Executor(extra=...)`` 的独立查找面使用，**能力不受影响**（``_pick`` 与
      ``_download_once`` 本就带 ``ctx`` 兜底）。
    """
    skip = set(exclude or ())
    injected = {}
    for name, value in list(ns.items()):
        if name.startswith("__"):
            continue
        if name in _OWN_NAMES:
            continue
        if name in skip:
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


def _closed_loop():
    """是否走闭环模式（新规形态）。

    判据只有一条：生成器有没有把计划烘焙进 ``EMBEDDED_PLAN``。
    **绝不用「文件读不到」当判据** —— 那样在旧环境（文件能读）与新环境
    （文件被禁）下会走进两条不同的代码路径，而两条都没被完整测过。
    """
    return isinstance(EMBEDDED_PLAN, dict) and bool(EMBEDDED_PLAN)


def _load_embedded_config():
    """闭环模式的配置：**只认** ``EMBEDDED_CONFIG``，一次磁盘访问都不做。

    ★ 为什么不能沿用 ``load_config()`` 的候选路径查找：那套逻辑要
      ``os.path.exists`` + ``open``，在新规下第一句就抛
      ``PermissionError: Forbidden FileIO``，而它发生在 ``init()`` 里
      ⇒ 策略「启动即停止」，用户看到的只是模型交易里闪一下。
    """
    cfg = dict(EMBEDDED_CONFIG) if isinstance(EMBEDDED_CONFIG, dict) else {}
    cfg["_config_path"] = "<embedded:closed-loop>"
    # ★ 计划里更自然的写法是 ``account_type``，而 agent 里既有的键是
    #   ``default_account_type``（do_place 读它）。这里做一次**单向别名**，
    #   而不是让两个名字同时生效 —— 两个都生效时，谁优先会变成排障黑洞。
    if cfg.get("account_type") and not cfg.get("default_account_type"):
        cfg["default_account_type"] = cfg.pop("account_type")
    if not cfg.get("account_id"):
        raise RuntimeError(
            "闭环模式的 EMBEDDED_CONFIG 缺少 account_id —— "
            "柜台查询 get_trade_detail_data(account, type, 'POSITION') 没有默认账户，"
            "猜一个等于拿别人的持仓去调仓")
    # ★ trading_enabled 的默认值**保持 False**（与旧模式一致）：不在 agent 里替用户
    #   做「这份计划等于授权」的推断。要真下单，由生成器显式写入 true ——
    #   那是一个可审计的动作，而 agent 的默认值不是一个可审计的动作。
    return cfg


def load_config():
    if _closed_loop():
        return _load_embedded_config()
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


def _diagnose_quote_service(xtdata=None):
    """行情实调失败时的**根因 + 出路**（只用 stdlib，py3.6 可用）。

    ★ 机制取自 xtquant 源码（``xtdata.py::load_global_config``），非猜测：xtdata 先
       逐个试连 ``%USERPROFILE%\\.xtquant\\<guid>\\xtdata.cfg`` 登记的端口，全失败才
       回落同目录 ``xtdata.ini`` 的 ``address``（出厂 ``127.0.0.1:58610``），仍连不上
       即抛 ``Exception: 无法连接行情服务！``。xtdata.cfg 由**客户端**在对外提供行情
       服务时写下；完整客户端（``XtItClient.exe``）只监听自身 IPC（实测 58600），
       **不**提供 58610。故该错几乎总是：① ``~/.xtquant`` 缺失/无 xtdata.cfg ⇒
       客户端未开「极简模式」（大小合一安装里极简模式由 ``bin.x64/XtMiniQmt.exe`` +
       ``miniquote.exe`` 提供并拉起 58610）；② 或回落地址确实无人监听。
    ★ 只回一句原文会把用户困在「界面正常/行情空白」之间，写清机制与两条出路才有用。
    """
    facts = []
    try:
        base = os.path.join(os.environ.get("USERPROFILE", ""), ".xtquant")
        if not os.path.isdir(base):
            facts.append("~/.xtquant 不存在 ⇒ xtdata 的自动发现必然落空（会回落 xtdata.ini）")
        else:
            cfgs = []
            try:
                entries = sorted(os.listdir(base))
            except Exception:
                entries = []
            for name in entries:
                cfg_path = os.path.join(base, name, "xtdata.cfg")
                if not os.path.isfile(cfg_path):
                    continue
                port = None
                try:
                    with open(cfg_path, "r") as fh:
                        port = (json.load(fh) or {}).get("port")
                except Exception:
                    port = None
                cfgs.append("%s(port=%s)" % (name[:8], port))
            facts.append("~/.xtquant 下的 xtdata.cfg: %s" % (", ".join(cfgs) if cfgs else "无"))
    except Exception as exc:
        facts.append("读 ~/.xtquant 失败: %s" % exc)

    ini_addr = "?"
    try:
        mod = xtdata if xtdata is not None else _import_xtdata()
        mod_file = getattr(mod, "__file__", "") or ""
        ini_path = os.path.join(os.path.dirname(mod_file), "xtdata.ini")
        if os.path.isfile(ini_path):
            with open(ini_path, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if line.lower().startswith("address="):
                        ini_addr = line.split("=", 1)[1]
                        break
    except Exception:
        pass
    facts.append("xtdata.ini 回落地址=%s" % ini_addr)
    return ("；".join(facts)
            + "。出路：① 客户端开「极简模式」提供本地行情服务后重启本策略；"
              "② 或让本策略以**公式模式**被 QMT 挂载（用 ContextInfo 的行情方法，"
              "不经 xtdata 的本地服务）。")


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
    # ★ 导入面须与**真正取用方式**一致（2026-10-08 实测）：py3.6 里 `import xtdata`
    #   会失败，而 `from xtquant import xtdata` 成功 —— 拿前者当判据会得假结论。
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
    # ★ 同步给 Executor ⇒ 随 PROBE 应答回给外部端（能力协商必须看到实调结论，
    #   否则「接口在但实调失败」会被判成 SUPPORTED —— 本仓最忌的假绿灯）。
    _exec = _STATE.get("executor")
    if _exec is not None:
        try:
            _exec.quote_call = quote_probe
        except Exception:
            pass
    if quote_probe is not None:
        detail = quote_probe[1]
        if not quote_probe[0]:
            # ★ 把「一句异常」升级成「根因 + 出路」，否则用户卡在
            #   「界面说正常 / 行情永远空白」之间（2026-10-09 真机踩到）。
            detail = "%s。根因/出路: %s" % (detail, _diagnose_quote_service())
        step("quote_call", quote_probe[0], detail)

    # ★ 下单能力**单独成项**（2026-10-09），答案须在此而非藏在行情报错旁；分类逻辑
    #   下沉到 qmt_api.trade_surface（入口文件受 G3 限制，保持零注入函数名字面量）。
    #   ★ 必须**无别名**导入：qmt_api 已内联进同一文件，`as 别名` 会在 bundle 留未绑定
    #     名字 → NameError（与 init() 里导入 Executor 同一约定）。
    trade = None
    try:
        from qmt_api import trade_surface
        trade = trade_surface(injected)
    except Exception as exc:
        step("order_funcs", False,
             "无法判定下单能力（qmt_api.trade_surface 不可用: %s）" % exc)
    if trade is not None:
        if trade.get("can_submit"):
            step("order_funcs", True,
                 "已捕获终端注入的交易函数: %s ⇒ 本进程可下单" % trade.get("present"))
        else:
            step("order_funcs", False,
                 "未捕获任何终端注入的交易函数（present=%s, missing=%s）⇒ 本进程"
                 "**不能下单**。独立进程模式下这是常态（终端只把下单函数注入"
                 "**被挂载的那一个文件**的命名空间）；要有下单能力，需让本策略以"
                 "**公式模式**被终端 in-process 挂载。"
                 % (trade.get("present"), trade.get("missing")))

    captured = sorted(injected.keys())
    step("injected_funcs", bool(captured),
         json.dumps(captured, ensure_ascii=False) if captured
         else "无终端注入函数（独立进程模式的常态；注意：xtdata 转发面不算注入，"
              "它另列在 forwarded_from_xtdata）")
    # ★ 唯一来源纪律（G3）：只如实列出**捕获到什么**，绝不手抄「终端应该有 X」的
    #   名单 —— 手抄会把桥的 bug 伪装成「终端缺能力」。期望集由外部端持有。
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
    # ★ 能力结论（外部端与界面**直接消费**，不必从 steps 里猜）：trading=能否下单
    #   （唯一判据 = 是否捕获到终端注入的 passorder）；quote=能否取行情（唯一判据 =
    #   **实调一次**的结果）。``expected_in_mode`` 标注该结论是否由**运行模式**决定：
    #   独立进程模式下二者不可用是设计使然，不该当成环境故障（否则出反向「假告状」）。
    standalone = bool(_STATE.get("standalone"))
    result["capability"] = {
        "trading": {
            "available": bool(trade.get("can_submit")) if trade else False,
            "reason": ("已捕获 passorder" if (trade or {}).get("can_submit")
                       else "未捕获终端注入的 passorder；"
                            + ("独立进程模式拿不到（需公式模式）" if standalone
                               else "终端未注入（请确认本文件是 QMT 挂载的入口）")),
            # ★ 下单不可用在独立进程模式下是**模式定义**使然（终端只注入被挂载的那
            #   一个文件），非环境故障 ⇒ 不当故障告警。
            "expected_in_mode": standalone,
        },
        "quote": {
            "available": bool(quote_probe[0]) if quote_probe else False,
            "reason": (quote_probe[1] if quote_probe
                       else "未发现行情 getter（既无注入也无 ContextInfo 方法）"),
            # ★ 行情**本该**在两种模式下都可用（独立进程模式的用途正是转发真实行情）
            #   ⇒ 取不到是客户端/环境问题，必须当故障告警，不能被「模式」二字豁免。
            "expected_in_mode": False,
        },
    }
    if trade is not None:
        result["trade_surface"] = trade
    # ★ 自检总判定：**严格**口径 —— 任一 step 不 ok 即 False（mode-expected 的失败也计入，
    #   因为「能力当前不可用」是事实；是否属模式使然由 expected_in_mode 单独标注）。
    #   历史 bug：本字段**根本不存在**，后端 qmt_agent.py 却读 probe.get("ok") ⇒
    #   probe_ok 恒 False，界面永远显示自检未通过。
    bad_steps = [s["name"] for s in result["steps"] if not s.get("ok")]
    result["ok"] = not bad_steps
    result["bad_steps"] = bad_steps
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


def write_status(cfg, injected, ctx, executor, started_at, last_error="", alive=True):
    """心跳：外部端据此知道「策略真的在跑」，而不是靠超时反推。

    文件极小、写入频率有界（STATUS_INTERVAL_S），不会成为磁盘负担。

    ★ ``alive`` 必须由调用方显式给（2026-10-12 修）：退出时写的那一份心跳过去
      恒为 ``alive: true`` —— 一个**已经死掉**的 agent 的最后遗言却自称活着，
      属假绿灯家族（外部端若直接读该字段、不看心跳年龄，就会被这句遗言骗过）。
    """
    payload = {
        "ts": int(time.time() * 1000),
        "alive": bool(alive),
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


# ---------------------------------------------------------------------------
# 闭环模式（新规形态）
#
# 为什么需要一整套新控制流，而不是「把文件桥的路径改一下」
# --------------------------------------------------------
# 2026-10-23 起，策略沙箱内 ``open()`` 直接抛 ``PermissionError: Forbidden
# FileIO``；连 ``127.0.0.1`` 的回环网络也算外联，处置是**停模 + 防外挂弹窗**。
# 所以旧控制流的三个支点同时失效：
#   · ``load_config()`` 读 agent_config.json      → 改读 EMBEDDED_CONFIG
#   · ``poll_once()`` 扫 bridge_dir/req/          → 删除（外部再也递不进指令）
#   · ``_emit_event()`` 追加 events.ndjson        → 改 print 结构化日志行
#
# 新控制流只有一条主线：
#   init: 校验计划 → 柜台重建持仓 → 建风控闸门 → （失败则只读，不崩）
#   tick: 窗口判定 → 查净值 → 算意图 → 闸门裁剪 → passorder → 记在途
#   emit: 每个动作一行 ``@@QMTW|{json}``，外部 LogTail 跟日志读
#
# ★ 三条不可让步的纪律（改这里之前先读）：
#   1. **不猜**：净值/行情/柜台任一项拿不到 ⇒ 本拍不动，且把原因说清楚；
#      绝不拿 0 当「空仓」、拿 0 价当「免费」。
#   2. **不重发**：在途委托超时只标 UNKNOWN 并停机，绝不自动重发 PLACE
#      （重发的代价是重复建仓，比少做一次贵得多）。
#   3. **不静默**：只读、拒单、丢单、闸门裁剪，每一条都有一条回传记录。
# ---------------------------------------------------------------------------

#: 柜台查询接口名。**只作为查找键**，不是「终端应该注入哪些函数」的名单
#: （G3 闸门：入口文件里的注入函数名字面量 ≤3 个）。
_QUERY_FN_NAME = "get_trade_detail_data"


def _new_boot_id():
    """本次启动的唯一标识。

    ★ 存在的唯一理由：``seq`` 是进程内计数器，重启即归零。外部按 ``seq``
      去重会把「重启后的第 1 条」当成「上次的第 1 条」丢掉。带上 boot_id 后
      去重键变成 ``(boot_id, seq)``，重启前后互不干扰。
    """
    try:
        pid = os.getpid()
    except Exception:
        pid = 0
    return "%013x-%x" % (int(time.time() * 1000), pid)


def _print_line(line):
    """把一行结构化回传写到 stdout —— 这是新规下**唯一**允许的回传通道。

    客户端会把策略 stdout 收进 ``userdata/log/XtClient_<date>.log``，外部
    ``LogTail`` 跟日志文件读，全程不碰策略进程、不违反任何一条限制。

    ★ 必须 flush：QMT 用 ``pythonw.exe -u`` 拉起策略，stdout 是管道；
      不 flush 时进程被停掉，缓冲区里最后几条（往往正是出错前那几条）会丢。
    """
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return True
    except Exception:
        try:
            print(line)
            return True
        except Exception:
            return False


def _row_field(obj, name):
    """柜台行字段读取（兼容对象属性 / 字典键 / 去 m_ 前缀三种形态）。"""
    if isinstance(obj, dict):
        value = obj.get(name)
        if value is None and name.startswith("m_"):
            value = obj.get(name[2:])
        return value
    value = getattr(obj, name, None)
    if value is None and name.startswith("m_"):
        value = getattr(obj, name[2:], None)
    return value


def _num_of(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _query_account_type():
    """柜台查询用的账户类型字符串（``STOCK`` / ``CREDIT`` / ``FUTURE`` / ``OPTION``）。

    ★ 与 ``account_type``（passorder 的标的类型）是**两个不同的东西**，不能混用：
      · ``account_type='etf'`` 决定 passorder 的 opAccountType 与代码格式校验；
      · 柜台查询接口只认 ``STOCK/CREDIT/FUTURE/OPTION``，ETF 走的仍是股票账户。
      混用的后果是 ``get_trade_detail_data(acct, 'ETF', 'POSITION')`` 返回空
      ⇒ 被当成空仓 ⇒ 重复建仓。
    """
    cfg = _STATE.get("cfg") or {}
    return str(cfg.get("query_account_type") or "STOCK").strip().upper() or "STOCK"


def _pick_func(name):
    """按「注入 → 转发 → executor.extra → ContextInfo」顺序找一个可调用对象。

    ★ 顺序有语义：终端注入的才算**能力**，转发的 xtdata 只是**查找面**。
      这里只是「拿函数来用」，不是能力上报，所以可以退让；
      能力上报一律走 ``_STATE["injected"]``（见 capture_qmt_injected_funcs）。
    """
    st = _STATE
    for holder in (st.get("injected"), st.get("forwarded_funcs")):
        if isinstance(holder, dict):
            fn = holder.get(name)
            if callable(fn):
                return fn
    executor = st.get("executor")
    extra = getattr(executor, "extra", None)
    if isinstance(extra, dict):
        fn = extra.get(name)
        if callable(fn):
            return fn
    ctx = st.get("ctx")
    if ctx is not None:
        fn = getattr(ctx, name, None)
        if callable(fn):
            return fn
    return None


def _plan_expired(raw_plan, now):
    """计划日期 ≠ 今日 ⇒ 返回原因（空串表示没写日期或日期匹配）。

    ★ 为什么必须校验：盘前烘焙的计划一旦忘记覆盖，隔天启动就会**照着昨天的
      目标权重去调仓** —— 那是纯亏损，而且看不出来（策略一切正常）。
      「不写日期」不做时效判断：不替用户编造一条他没同意的规则。
    """
    date = str((raw_plan or {}).get("date") or "").strip()
    if not date:
        return ""
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if date != today:
        return "计划日期 %s ≠ 今日 %s（盘前未覆盖计划？）" % (date, today)
    return ""


def _enter_readonly(reason, fatal=False):
    """进入只读：停止下单，把原因回传出去。

    :param fatal: True = 重试无意义（计划/配置错）；False = 每个 tick 尝试自愈
                  （柜台/行情暂时不可用）。
    """
    st = _STATE
    text = str(reason)
    if fatal:
        st["fatal_reason"] = text
    if st.get("readonly_reason") == text:
        return
    st["readonly_reason"] = text
    _emit_event("READONLY", {"reason": text, "fatal": bool(fatal)})
    log("闭环只读: %s" % text)


def _emit_once(key, kind, data, ttl=300.0):
    """同一个原因在 ``ttl`` 内只回传一次。

    ★ 为什么必须节流：闭环是 3 秒一拍、一天 5 小时 ⇒ 单条「行情缺失」能刷
      6000 行，把客户端日志轮转掉，真正要紧的那几条（REJECT/HALT）被挤没。
    """
    st = _STATE
    table = st.get("once")
    if not isinstance(table, dict):
        table = {}
        st["once"] = table
    now = time.time()
    last = table.get(key)
    if last is not None and now - last < ttl:
        return False
    table[key] = now
    if len(table) > 256:
        for old in list(table.keys())[:128]:
            table.pop(old, None)
    _emit_event(kind, data)
    return True


def _try_recover(now):
    """只读态自愈：重试柜台重建。→ True 表示可以继续交易。"""
    st = _STATE
    if st.get("fatal_reason"):
        return False
    if not st.get("readonly_reason"):
        return True
    source = st.get("positions")
    if source is None:
        return False
    try:
        source.rebuild()
    except CounterUnavailable:
        return False
    st["readonly_reason"] = ""
    _emit_event("RECOVER", {"reason": "柜台持仓查询已恢复，解除只读"})
    log("闭环只读解除：柜台查询已恢复")
    return True


def _refresh_nav(now, announce=True):
    """查总资产。→ 正数或 None（None = 查不到，调用方必须跳过本拍）。"""
    st = _STATE
    cached = float(st.get("nav") or 0.0)
    # 节流：总资产是慢变量，不必每拍都打柜台（3s 一拍 × 每拍一次查询 = 打爆）。
    if cached > 0 and now - float(st.get("last_nav_ts") or 0.0) < 5.0:
        return cached
    st["last_nav_ts"] = now
    query = st.get("query")
    if not callable(query):
        return None
    cfg = st.get("cfg") or {}
    try:
        rows = query(cfg.get("account_id") or "",
                     _query_account_type(), "ACCOUNT")
    except Exception as exc:
        if announce:
            _emit_once("nav_err", "WARN",
                       {"reason": "总资产查询抛错: %s: %s" % (type(exc).__name__, exc)})
        return None
    if rows is None:
        if announce:
            _emit_once("nav_err", "WARN",
                       {"reason": "总资产查询返回 None（查询失败，不是零资产）"})
        return None
    for row in rows:
        for field in ("m_dBalance", "m_dAssureAsset", "balance", "total_asset"):
            value = _num_of(_row_field(row, field))
            if value > 0:
                st["nav"] = value
                return value
    if announce:
        _emit_once("nav_err", "WARN",
                   {"reason": "账户行里取不到总资产（m_dBalance/m_dAssureAsset 均为 0）"})
    return None


def _tick_price(tick):
    """一条 tick → 最新价。取不到返回 0（调用方据此跳过该标的）。"""
    for field in ("lastPrice", "last_price", "close", "lastClose"):
        value = _num_of(_row_field(tick, field))
        if value > 0:
            return value
    return 0.0


def _last_prices(codes):
    """批量取最新价。→ {code: price}；取不到的标的不出现在结果里。"""
    if not codes:
        return {}
    getter = _pick_func("get_full_tick")
    if getter is None:
        return {}
    try:
        ticks = getter(list(codes))
    except Exception as exc:
        _emit_once("quote_err", "WARN",
                   {"reason": "get_full_tick 抛错: %s: %s" % (type(exc).__name__, exc)})
        return {}
    if not isinstance(ticks, dict):
        return {}
    prices = {}
    for code, tick in ticks.items():
        price = _tick_price(tick)
        if price > 0:
            prices[str(code).upper()] = price
    return prices


def _query_orders():
    """查柜台委托列表。→ 行列表 或 None（None = 查不到，**不能**据此下结论）。"""
    query = _STATE.get("query")
    if not callable(query):
        return None
    cfg = _STATE.get("cfg") or {}
    try:
        return query(cfg.get("account_id") or "",
                     _query_account_type(), "ORDER")
    except Exception:
        return None


def _remark_of(row):
    for field in ("m_strRemark", "remark", "order_remark", "m_strOrderRemark"):
        value = _row_field(row, field)
        if value:
            return str(value)
    return ""


def _settle_pending_by_position():
    """按**持仓变化**核销在途委托。

    ★ 为什么必须有这一条（本轮最重要的修复）：
      ``deal_callback`` 在部分终端/版本上不触发（实测有「只推委托、不推成交」
      的情形）。只靠回调核销，在途表会一直挂着 → 300 秒后被误判成「久不成交」
      → 停机。而事实是**早就成交了**。症状极具欺骗性：策略跑了一整天，
      只下了第一轮单，之后再也不动，日志里只有一条 HALT。
      持仓是**真相源**：它朝委托方向动了，就说明这一腿已经落地，在途可以销账。
    """
    st = _STATE
    pending = st.get("pending")
    source = st.get("positions")
    if not isinstance(pending, dict) or not pending or source is None:
        return
    for cid in list(pending.keys()):
        item = pending.get(cid) or {}
        code = item.get("code", "")
        before = int(item.get("pos_before") or 0)
        now_volume = int(source.volume_of(code))
        moved = now_volume - before
        is_buy = str(item.get("side", "")).upper() == "BUY"
        if (is_buy and moved > 0) or ((not is_buy) and moved < 0):
            pending.pop(cid, None)
            _emit_event("SETTLED", {
                "cid": cid, "code": code, "by": "position",
                "pos_before": before, "pos_now": now_volume,
                "reason": "持仓已朝委托方向变化 ⇒ 这一腿落地，在途销账"
                          "（deal_callback 可能没触发，不能等它）"})


def _resync_positions(now):
    """按 ``POSITION_RESYNC_S`` 回柜台重读持仓。→ False 表示本拍不能交易。

    ★ 与 ``_try_recover`` 的分工：那个处理「启动就失败、整体只读」；这个处理
      「运行中回调丢了、内存持仓过期」。两者都会导致**按错误的仓位调仓**，
      但成因不同，日志里必须能分开看。
    """
    st = _STATE
    source = st.get("positions")
    if source is None:
        return False
    interval = float((st.get("cfg") or {}).get("position_resync_s") or POSITION_RESYNC_S)
    if now - float(st.get("last_resync") or 0.0) < max(5.0, interval):
        return True
    st["last_resync"] = now
    try:
        source.rebuild()
    except CounterUnavailable as exc:
        # 不整体转只读（那会每拍刷 READONLY/RECOVER 两条），只跳过本拍并播报一次。
        _emit_once("resync_err", "WARN",
                   {"reason": "持仓重新对齐失败，本拍不交易: %s" % exc})
        return False
    _settle_pending_by_position()
    return True


def _reconcile_pending(now):
    """在途委托的三态判定：查不到 ⇒ 允许重发；查得到但久不成交 ⇒ 停机；否则等。

    ★ 「查不到」与「查询失败」是两件事：``_query_orders()`` 返回 None 时
      我们**什么结论都不下**（既不放行重发，也不停机）—— 猜错任一方向都贵。
    ★ 查询本身有成本，按 ``ORDER_QUERY_S`` 节流；节流窗口内维持上一次结论
      （即：什么都不做），这比每拍都打一次柜台安全。
    """
    st = _STATE
    pending = st.get("pending")
    if not isinstance(pending, dict) or not pending:
        return
    interval = float((st.get("cfg") or {}).get("order_query_s") or ORDER_QUERY_S)
    if now - float(st.get("last_order_query") or 0.0) < max(1.0, interval):
        return
    st["last_order_query"] = now
    rows = _query_orders()
    remarks = None
    if rows is not None:
        remarks = []
        for row in rows:
            text = _remark_of(row)
            if text:
                remarks.append(text)
    for cid in list(pending.keys()):
        item = pending.get(cid) or {}
        age = now - float(item.get("ts") or now)
        seen = None
        if remarks is not None:
            seen = any(cid in text for text in remarks)
        if seen is False and age >= PENDING_GRACE_S:
            pending.pop(cid, None)
            _emit_event("LOST", {
                "cid": cid, "code": item.get("code", ""), "age_s": int(age),
                "reason": "柜台委托列表里查不到该 cid ⇒ 这单没有送达，"
                          "允许重新决策（唯一允许重发的情形）"})
        elif seen and age >= PENDING_STALE_S:
            guard = st.get("guard")
            if guard is not None and not guard.halted:
                guard.halt("委托 %s 已 %d 秒既未成交也未回报（先查后动，不自动重发）"
                           % (cid, int(age)))
                _emit_event("HALT", {"reason": guard.halt_reason, "cid": cid,
                                     "code": item.get("code", "")})


def _net_out_pending(intents):
    """扣掉在途委托已覆盖的股数，避免「同一目标连下两单」。

    ★ 为什么必须做：``decide()`` 只看柜台持仓，而下单到成交之间有一个窗口
      （限价单可能挂着不成交）。不扣在途，每一拍都会算出同样的差额并再发一单
      —— 3 秒一拍、窗口 5 小时 = 几千张重复委托。
    """
    st = _STATE
    pending = st.get("pending")
    if not isinstance(pending, dict) or not pending:
        return intents
    open_shares = {}
    for item in pending.values():
        code = item.get("code", "")
        sign = 1 if str(item.get("side", "")).upper() == "BUY" else -1
        open_shares[code] = open_shares.get(code, 0) + sign * int(item.get("volume") or 0)
    out = []
    for intent in intents:
        want = int(intent.get("volume") or 0)
        if str(intent.get("side", "")).upper() == "SELL":
            want = -want
        remaining = want - open_shares.get(intent.get("code", ""), 0)
        if abs(remaining) < LOT_SIZE:
            continue
        if (remaining > 0) != (want > 0):
            # 方向已被在途委托吃掉 ⇒ 不再反向加单（那会把调仓变成来回打）
            continue
        fresh = dict(intent)
        fresh["side"] = "BUY" if remaining > 0 else "SELL"
        fresh["volume"] = abs(remaining)
        fresh["value"] = abs(remaining) * float(intent.get("ref_price") or 0.0)
        out.append(fresh)
    return out


def _place(intent, now):
    """下一单。返回 cid（成功）或 None（失败，原因已回传）。"""
    st = _STATE
    executor = st.get("executor")
    if executor is None:
        return None
    cfg = st.get("cfg") or {}
    st["order_seq"] = int(st.get("order_seq") or 0) + 1
    # cid 必须**短**（passorder 备注位有长度风险）且**唯一**（重启后 boot_tag 变）。
    cid = "cw%s%d" % (st.get("boot_tag") or "000000", st["order_seq"])
    params = {
        "stock_code": intent.get("code", ""),
        "side": str(intent.get("side", "")).lower(),
        "volume": int(intent.get("volume") or 0),
        "price": float(intent.get("ref_price") or 0.0),
        "price_type": "limit",
        "account_id": cfg.get("account_id") or "",
        # ★ 这个值会落进 passorder 的第 9 个位置参数（备注位），回报里以
        #   m_strRemark 回显 —— 它是**唯一**能把「意图」和「柜台委托」串起来的
        #   跨进程锚点（见 docs 方案 §6.5）。绝不改成随机数或时间戳。
        "client_order_id": cid,
    }
    base = {"cid": cid, "code": intent.get("code", ""),
            "side": intent.get("side", ""), "volume": int(intent.get("volume") or 0),
            "price": float(intent.get("ref_price") or 0.0),
            "value": float(intent.get("value") or 0.0)}
    try:
        result = executor.do_place(params)
    except Exception as exc:
        payload = dict(base)
        payload["reason"] = "%s: %s" % (type(exc).__name__, exc)
        _emit_event("REJECT", payload)
        # ★ 归还换手额度：这一单没送到市场，不该占当天的预算。
        #   不退的后果：一个配置错（如账户类型不匹配）就把额度烧光，
        #   改好之后当天也做不了任何调仓。
        guard = st.get("guard")
        if guard is not None:
            guard.refund(intent.get("value") or 0.0)
        return None
    payload = dict(base)
    if isinstance(result, dict):
        payload["order_id"] = result.get("order_id") or ""
        payload["generated"] = bool(result.get("generated_client_order_id"))
    source = st.get("positions")
    pos_before = int(source.volume_of(intent.get("code", ""))) if source is not None else 0
    pending = st.get("pending")
    if not isinstance(pending, dict):
        pending = {}
        st["pending"] = pending
    pending[cid] = {"cid": cid, "code": intent.get("code", ""),
                    "side": intent.get("side", ""),
                    "volume": int(intent.get("volume") or 0), "ts": now,
                    # ★ 记下单前的持仓：这是「回调没触发时靠持仓核销」的锚点。
                    "pos_before": pos_before}
    _emit_event("ORDER", payload)
    return cid


def _maybe_reset_day(now):
    """跨日复位：闸门计数、在途表、发单序号。"""
    st = _STATE
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if st.get("trade_date") == today:
        return
    st["trade_date"] = today
    guard = st.get("guard")
    if guard is not None:
        guard.reset_day(st.get("nav") or 0.0)
    st["pending"] = {}
    st["order_seq"] = 0
    st["in_window"] = False
    st["done_announced"] = False
    st["last_resync"] = 0.0
    st["last_order_query"] = 0.0
    _emit_event("DAY", {"date": today})


def _maybe_status(now):
    """周期心跳（走日志行，不落盘）—— 替代旧模式的 agent_status.json。"""
    st = _STATE
    if now - float(st.get("last_status") or 0.0) < STATUS_INTERVAL_S:
        return
    st["last_status"] = now
    source = st.get("positions")
    guard = st.get("guard")
    plan = st.get("plan") or {}
    pending = st.get("pending") or {}
    _emit_event("STATUS", {
        "mode": "closed",
        "bundle": EMBEDDED_BUNDLE_ID or "",
        "agent_ver": _AGENT_VERSION,
        "py": sys.version.split()[0],
        "uptime_s": int(now - float(st.get("started_at") or now)),
        "nav": float(st.get("nav") or 0.0),
        "positions": len(source.snapshot()) if source is not None else -1,
        "pending": len(pending),
        # ★ 带出在途 cid：外部据此把「已发单」和「柜台委托」对上，
        #   不用等盘后对账。上限 8 条，避免把一条心跳撑成几 KB。
        "pending_cids": sorted(pending.keys())[:8],
        "orders_sent": int(getattr(guard, "orders_sent", 0) or 0),
        "turnover": round(float(getattr(guard, "turnover", 0.0) or 0.0), 2),
        "halted": bool(getattr(guard, "halted", False)),
        "in_window": bool(st.get("in_window")),
        "readonly": st.get("readonly_reason") or "",
        "plan_id": plan.get("id", ""),
        "window": plan.get("window", []),
        "last_error": st.get("last_error") or "",
    })


def _boot_payload(ctx):
    st = _STATE
    plan = st.get("plan") or {}
    source = st.get("positions")
    cfg = st.get("cfg") or {}
    return {
        "mode": "closed",
        "bundle": EMBEDDED_BUNDLE_ID or "",
        "agent_ver": _AGENT_VERSION,
        "py": sys.version.split()[0],
        "boot_id": st.get("boot_id") or "",
        "standalone": bool(st.get("standalone")),
        "plan_id": plan.get("id", ""),
        "strat": plan.get("strat", ""),
        "window": plan.get("window", []),
        "targets": len((plan.get("rule") or {}).get("target") or []),
        "guard": plan.get("guard", {}),
        "account": cfg.get("account_id", ""),
        # ★ 这两个是**两个不同的轴**，混用会「静默空仓」或「全部拒单」：
        #   default_account_type → passorder 的标的类型与代码格式校验；
        #   query_account_type   → get_trade_detail_data 的账户类型（只认 STOCK/CREDIT/…）。
        "default_account_type": cfg.get("default_account_type") or "stock",
        "query_account_type": _query_account_type(),
        "trading_enabled": bool(cfg.get("trading_enabled")),
        "nav": float(st.get("nav") or 0.0),
        "positions": len(source.snapshot()) if source is not None else -1,
        "injected": sorted((st.get("injected") or {}).keys()),
        "readonly": st.get("readonly_reason") or "",
        "fatal": st.get("fatal_reason") or "",
        "ctx_methods": _ctx_methods(ctx),
    }


def _init_closed_loop(ctx):
    """闭环模式初始化：校验计划 → 柜台重建 → 建闸门 → 决定可交易/只读。

    ★ 任何一步失败都**不抛异常**：抛出去 = 策略「启动即停止」，用户只能看到
      模型交易里闪一下，日志里连原因都没有。这里一律降级为只读 + 一条
      ``READONLY`` 回传记录 —— 人能从日志里读到为什么。
    """
    st = _STATE
    st["mode"] = "closed"
    st["boot_id"] = _new_boot_id()
    st["boot_tag"] = st["boot_id"].split("-")[0][-6:]
    st["started_at"] = time.time()
    st["injected"] = capture_qmt_injected_funcs(globals(), exclude=st.get("forwarded"))
    st["ctx"] = ctx

    try:
        cfg = load_config()
    except Exception as exc:
        _enter_readonly("配置不可用: %s" % exc, fatal=True)
        _emit_event("BOOT", _boot_payload(ctx))
        return
    st["cfg"] = cfg
    cfg["_started_at"] = st["started_at"]
    cfg["_agent_version"] = _AGENT_VERSION

    raw_plan = EMBEDDED_PLAN if isinstance(EMBEDDED_PLAN, dict) else {}
    try:
        plan = parse_plan(raw_plan)
    except Exception as exc:
        _enter_readonly("计划校验失败: %s" % exc, fatal=True)
        _emit_event("BOOT", _boot_payload(ctx))
        return
    st["plan"] = plan
    expired = _plan_expired(raw_plan, st["started_at"])
    if expired:
        _enter_readonly(expired, fatal=True)
        _emit_event("BOOT", _boot_payload(ctx))
        return

    # Executor 只用来借 do_place（它有 passorder 的三级签名降级 + trading 闸）。
    # ★ 闭环模式下不调用它的 poll_once / _write_response —— 那两条要写文件。
    from qmt_api import Executor
    lookup = dict(st.get("forwarded_funcs") or {})
    lookup.update(st["injected"])
    st["executor"] = Executor(cfg, st["injected"], ctx, extra=lookup)

    query = _pick_func(_QUERY_FN_NAME)
    if not callable(query):
        _enter_readonly(
            "未捕获柜台查询接口 —— 闭环需要**公式模式**挂载（终端只把下单/查询函数"
            "注入被挂载的那个文件）；独立进程模式拿不到，此时无法重建持仓，"
            "按纪律不交易", fatal=True)
        _emit_event("BOOT", _boot_payload(ctx))
        return
    st["query"] = query
    source = PositionSource(query, cfg.get("account_id"),
                            _query_account_type())
    st["positions"] = source
    st["guard"] = Guard(plan["guard"], 0.0)
    try:
        source.rebuild()
    except CounterUnavailable as exc:
        # ★ 不是空仓！空仓是合法的、可以照常调仓；查不到是**不可用**。
        _enter_readonly("柜台持仓查询失败（不是空仓）: %s" % exc)
    nav = _refresh_nav(st["started_at"], announce=False)
    _emit_event("BOOT", _boot_payload(ctx))
    _maybe_reset_day(st["started_at"])
    if nav is None:
        # 不是致命错：每个 tick 会再试。但必须在 BOOT 之后说清「净值未知」，
        # 否则外部看到 nav=0 会以为账户是空的（「查不到 ≠ 零」）。
        _emit_once("nav_err", "WARN",
                   {"reason": "启动时查不到总资产，净值未知（不是零资产）；"
                              "拿不到净值就不会算调仓"})
    log("闭环就绪: plan=%s 窗口=%s 目标=%d 只读=%s"
        % (plan["id"], "-".join(plan["window"]),
           len(plan["rule"]["target"]), st["readonly_reason"] or "否"))


def tick(now=None):
    """闭环主循环的一拍（由 ``handlebar`` 驱动，也允许被测试直接调用）。

    顺序有意为之，改之前先看每段的理由：

    1. **节流 + 心跳**：心跳必须在所有早退之前 —— 否则「只读/停机」的策略在外部
       看来就是「死了」，运维会去查错方向（重启、换机器），而真正的原因
       （计划过期、账户类型错）一直躺在 READONLY 记录里没人看。
    2. **持仓重新对齐**：回调可能不触发，内存持仓会过期；过期持仓算出来的
       调仓意图是错的，且看不出来。
    3. **窗口 → 净值 → 在途 → 意图 → 闸门 → 下单**。
    """
    st = _STATE
    if st.get("mode") != "closed":
        return
    now = time.time() if now is None else float(now)
    interval = float((st.get("cfg") or {}).get("tick_interval_s") or TICK_INTERVAL_S)
    if now - float(st.get("last_tick") or 0.0) < max(0.5, interval):
        return
    st["last_tick"] = now
    _maybe_reset_day(now)
    _maybe_status(now)
    if st.get("fatal_reason"):
        return
    if not _try_recover(now):
        return
    plan = st.get("plan")
    guard = st.get("guard")
    source = st.get("positions")
    if plan is None or guard is None or source is None:
        return
    if not _resync_positions(now):
        return
    if guard.halted:
        return

    hhmm = hhmm_of(now)
    inside = in_window(plan, hhmm)
    if st.get("in_window") and not inside:
        # 窗口收尾必须留一条：否则「今天到底跑没跑完」只能靠翻全天日志推断。
        _emit_event("WINDOW_CLOSED", {
            "hhmm": hhmm, "window": plan["window"],
            "orders_sent": int(getattr(guard, "orders_sent", 0) or 0),
            "pending": len(st.get("pending") or {}),
            "turnover": round(float(getattr(guard, "turnover", 0.0) or 0.0), 2),
            "halted": bool(getattr(guard, "halted", False)),
        })
    st["in_window"] = inside
    if not inside:
        return

    nav = _refresh_nav(now)
    if nav is None or nav <= 0:
        return
    if guard.mark_nav(nav):
        _emit_event("HALT", {"reason": guard.halt_reason, "nav": nav})
        return
    _reconcile_pending(now)
    if guard.halted:
        return
    codes = [t["code"] for t in plan["rule"]["target"]]
    prices = _last_prices(codes)
    missing = sorted(c for c in codes if c not in prices)
    if missing:
        # ★ 缺价不是「跳过就好」：用 0 价算出来的股数是无穷大。
        _emit_once("noprice:" + ",".join(missing), "WARN",
                   {"reason": "行情缺失，这些标的本拍不参与决策", "codes": missing})
    if not prices:
        return
    try:
        intents = decide(plan, source.snapshot(), nav, prices, LOT_SIZE)
    except PlanError as exc:
        _enter_readonly("调仓计算失败: %s" % exc, fatal=True)
        return
    if not intents:
        # 到位了。**只播报一次** —— 3 秒一拍刷 5 小时 DONE 会把日志轮转掉。
        if not st.get("done_announced") and int(st.get("order_seq") or 0) > 0 \
                and not st.get("pending"):
            st["done_announced"] = True
            _emit_event("DONE", {
                "nav": nav, "hhmm": hhmm,
                "positions": len(source.snapshot()),
                "orders_sent": int(getattr(guard, "orders_sent", 0) or 0),
                "turnover": round(float(getattr(guard, "turnover", 0.0) or 0.0), 2),
            })
        return
    st["done_announced"] = False      # 又出现偏离 ⇒ 之前那个 DONE 不再成立
    intents = _net_out_pending(intents)
    if not intents:
        return
    allowed, dropped = guard.filter(intents)
    if dropped:
        _emit_once("drop:" + ",".join(sorted(d["code"] + ":" + d["reason"] for d in dropped)),
                   "DROP", {"dropped": dropped})
    for intent in allowed:
        _place(intent, now)


def init(ContextInfo):
    """QMT 策略入口（老版本 QMT 用 init，新版亦有 handle_init 别名）。"""
    # ★ 先发布状态再做任何 IO
    log("agent 初始化开始")
    if _closed_loop():
        # ★ 这里**绝不能**让异常冒出去：冒出去 = 策略「启动即停止」，
        #   模型交易里只闪一下，日志里连原因都没有（2026-10-08 踩过）。
        #   闭环模式下「失败」的唯一合法表现是**只读 + 一条可读的回传记录**。
        try:
            _init_closed_loop(ContextInfo)
        except Exception:
            tb = traceback.format_exc()
            lines = [ln for ln in tb.strip().splitlines() if ln.strip()]
            try:
                _enter_readonly("闭环初始化异常: %s" % (lines[-1] if lines else "unknown"),
                                fatal=True)
            except Exception:
                pass
            log("闭环初始化异常:\n" + tb)
        return
    # ★ exclude=转发名 ⇒ injected **只含终端注入**（独立进程模式下为空）。
    #   不排除就会把「下单不可用」这条警告吞掉，并让外部端看到 76 个"注入函数"
    #   —— 见 capture_qmt_injected_funcs 的 2026-10-09 注释。
    _STATE["injected"] = capture_qmt_injected_funcs(
        globals(), exclude=_STATE.get("forwarded"))
    _STATE["ctx"] = ContextInfo
    try:
        cfg = load_config()
        _STATE["cfg"] = cfg
        from qmt_api import Executor  # py3.6 相对 import 在 QMT 环境下不可靠，用同级绝对导入

        # ★ 职责分离：Executor 需要的是「**能不能取到**函数」（查找面 = 终端注入 ∪
        #   我们自己转发的 xtdata 接口）；而「**能力面**」的上报由 _STATE["injected"]
        #   单独负责。把两者混成一个 dict 就等于让转发冒充终端注入。
        _lookup = dict(_STATE.get("forwarded_funcs") or {})
        _lookup.update(_STATE["injected"])
        _STATE["executor"] = Executor(cfg, _STATE["injected"], ContextInfo,
                                      extra=_lookup)
        _STATE["started_at"] = time.time()
        # ★ 把启动时刻写进 cfg，使 Executor.meta() 的 uptime_s 诚实
        #   （否则 capacity 面会显示 uptime=0，违背「不伪造」纪律）。
        cfg["_started_at"] = _STATE["started_at"]
        # ★ 桥壳版本注入 cfg：协议层信封（qmt_api.meta）据此如实上报
        #   「跑的是哪个 agent」。否则外部端只能看到协议层 VERSION，
        #   两个常量独立演进必然漂移（曾实测健康端点显示 1.0.0 而实跑 1.2.0）。
        cfg["_agent_version"] = _AGENT_VERSION
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
    if st.get("mode") == "closed":
        # 闭环：不轮询 req/、不写心跳文件 —— 两条都是文件 IO，新规下直接抛
        # `PermissionError: Forbidden FileIO`。整条主循环只剩 tick()。
        try:
            tick()
            st["last_error"] = ""
        except Exception:
            tb = traceback.format_exc()
            lines = [ln for ln in tb.strip().splitlines() if ln.strip()]
            st["last_error"] = lines[-1] if lines else "unknown"
            log("闭环主循环异常:\n" + tb)
        return
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
    if st.get("mode") == "closed":
        _on_closed_callback(kind, info)
        return
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


def _on_closed_callback(kind, info):
    """闭环模式下的回报处理：持仓增量 + 在途核销 + 回传。

    ★ 这里**不落盘**：新规下文件写会抛错。持仓真相源是柜台，成交回报只是
      在两次柜台查询之间把内存状态拉近一点 —— 即使全丢，下一拍查柜台也能对齐。
    """
    st = _STATE
    try:
        if kind == "trade":
            source = st.get("positions")
            if source is not None:
                source.apply_deal(info)
            _settle_pending(info)
        _emit_event("TRADE" if kind == "trade" else "ORDER",
                    {"event": "callback", "kind": kind,
                     "code": str(_row_field(info, "m_strInstrumentID")
                                 or _row_field(info, "stock_code") or ""),
                     "volume": int(_num_of(_row_field(info, "m_nVolume")
                                           or _row_field(info, "volume"))),
                     "remark": _remark_of(info),
                     "raw": str(info)[:200]})
    except Exception:
        log("闭环回报处理异常(%s):\n%s" % (kind, traceback.format_exc()))


def _settle_pending(info):
    """成交回报 → 核销对应在途委托（按备注位里的 cid 认领）。"""
    st = _STATE
    pending = st.get("pending")
    if not isinstance(pending, dict) or not pending:
        return
    remark = _remark_of(info)
    if not remark:
        return
    volume = int(_num_of(_row_field(info, "m_nVolume") or _row_field(info, "volume")))
    for cid in list(pending.keys()):
        if cid not in remark:
            continue
        item = pending.get(cid) or {}
        remain = int(item.get("volume") or 0) - volume
        if remain > 0:
            item["volume"] = remain
            pending[cid] = item
        else:
            pending.pop(cid, None)
        return


def _on_request(envelope, executor):
    """处理一条请求：执行 → 原子写响应。返回 None（异常已在内部转成 ok=false）。

    ★ 闭环模式下这条路径**永不进入**（没有 req/ 目录可扫）。保留它是为了让
      ``Executor.poll_once`` 的签名与旧模式保持一致，避免一次性大改。
    """
    return executor.execute(envelope)


def _emit_event(event_type, data):
    """回传一条事件。

    **两条通道，由运行模式决定，绝不混用**：

    * 闭环模式（``EMBEDDED_PLAN`` 非空）：``print("@@QMTW|{json}")`` 一行。
      客户端把 stdout 收进 ``userdata/log/XtClient_<date>.log``，外部
      ``LogTail`` 跟日志读 —— 全程不写文件、不联网，是**唯一**合规通道。
    * 旧文件桥模式：追加 ``events.ndjson``（新规生效前仍可用）。

    ★ 闭环模式下**绝不能**退化成写文件：那会在每一条事件上抛
      ``PermissionError: Forbidden FileIO``，把主循环打成「每拍异常」，
      而外部端只会看到「策略在跑但没有任何事件」—— 一个典型的假绿灯。
    """
    st = _STATE
    st["seq"] += 1
    if st.get("mode") == "closed":
        try:
            _print_line(emit(event_type, data, st["seq"], st.get("boot_id") or ""))
        except Exception:
            log("回传失败(%s):\n%s" % (event_type, traceback.format_exc()))
        return
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

from agent_runtime import (  # bundle: 已内联，生成器会剔除本行
    _StandaloneContext, _augment_sys_path, _consume_stop_file,
    _env_float, _forward_xtdata_funcs, _import_xtdata,
    _qmt_root_from_userdata, should_autorun,
)
from position_source import (  # bundle: 已内联，生成器会剔除本行
    CounterUnavailable, PositionSource,
)
from closed_loop import (  # bundle: 已内联，生成器会剔除本行
    Guard, PlanError, decide, emit, hhmm_of, in_window, parse_plan,
)

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

    # ★ 闭环模式下**绝不导入 xtdata**（新规第二条 · 实测踩点）。
    #   xtdata 取行情要连 ``127.0.0.1:58610`` 的本地行情服务 —— 新规原文把
    #   「策略发起的 HTTP、TCP、UDP 等网络连接」一并关闭，回环并不豁免；处置是
    #   **停模 + 防外挂弹窗**。而闭环本来就要求公式模式挂载（终端只把 passorder /
    #   查询函数注入**被挂载的那一个文件**），独立进程下永远拿不到查询接口 ⇒
    #   无论如何都是只读：导入 xtdata 换不来任何能力，只换来一次违规。
    #   宁可如实报「行情不可用」，也不去碰那条端口。
    closed_boot = _closed_loop()
    xtdata = None if closed_boot else _import_xtdata()
    ctx = _StandaloneContext(xtdata, root)
    forwarded = {}
    if xtdata is not None:
        forwarded = _forward_xtdata_funcs(globals(), xtdata)
    # ★ 两者**必须分开记**：forwarded 是**我们自己**转发进来的 xtdata 接口，
    #   injected 是**终端注入**的（本模式下为空）。混记会让「下单不可用」的关键
    #   警告被吞、并让能力面出现假绿灯 —— 见 capture_qmt_injected_funcs 的注释。
    _STATE["forwarded"] = sorted(forwarded)
    _STATE["forwarded_funcs"] = forwarded
    log("独立进程模式: xtdata=%s 转发真实接口 %d 个"
        % ("跳过(闭环模式不连本地行情服务)" if closed_boot
           else ("可用" if xtdata is not None else "不可用(未导入)"),
           len(forwarded)))

    try:
        init(ctx)
    except Exception:
        log("init 失败，进程无法常驻：\n" + traceback.format_exc())
        return 2

    # ★ 把「本模式到底能用什么」主动说出来，别让用户对着永远空白的面板猜
    #   （本仓最常见的故障形态就是"绿灯是另一个 bug 遮出来的"）。
    #   ★ 2026-10-09 修正：独立进程模式下这里**终于会真的打印**了 —— 此前 76 个
    #     xtdata 转发被误捕获成「终端注入」，把这条警告静默吞掉，用户只看到一句
    #     行情报错，完全不知道下单根本不可能。
    if not _STATE["injected"]:
        log("注意: 本模式**没有任何终端注入的函数**（独立进程模式的常态）—— "
            "下单/查询能力不可用（这也是「能不能下单」的诚实答案）。要拿到注入能力，"
            "需让本策略以**公式策略**的形态被终端挂载（同系统自带策略那样走 "
            "in-process 公式引擎），而不是以外部 Python 脚本策略运行。")
    quote_result = _STATE.get("quote_call")
    if quote_result is not None and not quote_result[0]:
        log("注意: 行情接口存在但**实调失败**（%s）—— 外部端会把行情判为不可用，"
            "这是事实、不是伪造。%s" % (quote_result[1], _diagnose_quote_service()))
    cfg = _STATE["cfg"] or {}
    closed = _STATE.get("mode") == "closed"
    if closed:
        # ★ 闭环 + 独立进程 = 注定只读：终端只把 passorder/get_trade_detail_data
        #   注入**被挂载的那个文件**，独立进程拿不到 ⇒ 无法重建持仓 ⇒ 按纪律不交易。
        #   这里必须说出来，否则用户会以为「策略在跑就应该在下单」。
        log("注意: 闭环模式需要**公式模式**挂载。独立进程模式下拿不到柜台查询接口，"
            "策略会保持只读（原因已通过 READONLY 记录回传）。")
        interval = max(0.5, float(cfg.get("tick_interval_s") or TICK_INTERVAL_S))
        stop_file = ""
    else:
        interval = max(0.05, float(cfg.get("poll_interval_ms", 500)) / 1000.0)
        stop_file = os.path.join(cfg.get("bridge_dir", ""), "STOP")
    bridge_dir = cfg.get("bridge_dir", "")
    limit = _env_float("QMT_WORK_AGENT_MAX_SECONDS", 0.0)
    if closed:
        log("主循环开始(闭环): interval=%.3fs（置 QMT_WORK_AGENT_MAX_SECONDS 可正常退出；"
            "闭环下**不读 STOP 文件** —— 文件 IO 在新规里是被禁的）" % interval)
    else:
        log("主循环开始: interval=%.3fs bridge_dir=%s（置 QMT_WORK_AGENT_MAX_SECONDS "
            "或在本目录创建 STOP 文件可正常退出）" % (interval, bridge_dir))
    started = time.time()
    stop_reason = "exited"
    try:
        while True:
            handlebar(ctx)
            if limit > 0 and (time.time() - started) >= limit:
                log("达到 QMT_WORK_AGENT_MAX_SECONDS=%.1f，正常退出" % limit)
                stop_reason = "max_seconds"
                break
            if stop_file:
                try:
                    if os.path.exists(stop_file):
                        _consume_stop_file(stop_file)
                        log("检测到 STOP 文件，正常退出（已消费 STOP：改名 STOP.consumed.<ts> —— "
                            "避免下次启动被立即杀死，并留下「确被 STOP 停过」的物证）")
                        stop_reason = "stopped_by_stop_file"
                        break
                except Exception:
                    pass
            # 半周期休眠：handlebar 自带节流，调用频率高于 interval 才能稳定命中
            time.sleep(max(0.05, interval / 2.0))
    except KeyboardInterrupt:
        log("收到键盘中断，正常退出")
        stop_reason = "keyboard_interrupt"
    except Exception:
        log("主循环异常终止：\n" + traceback.format_exc())
        return 3

    if closed:
        # 闭环下退出只能靠回传行说明（写心跳文件会抛 Forbidden FileIO）。
        try:
            _emit_event("STOP", {"reason": stop_reason, "mode": "closed"})
        except Exception:
            pass
    else:
        try:
            # ★ 退出时 alive=False：已死的 agent 不得在最后一份心跳里自称活着
            write_status(cfg, _STATE["injected"], ctx, _STATE["executor"],
                         _STATE["started_at"], stop_reason, alive=False)
        except Exception:
            pass
    log("进程正常退出")
    return 0


if __name__ == "__main__" and should_autorun():
    # QMT「模型交易 → 运行」= pythonw.exe -u <本文件> <userdata> <ts> ⇒ 自举常驻。
    # 公式模式（终端把源码 exec 进自己的命名空间）不会被这条命中，
    # 此时只提供 init/handlebar 等入口，不动终端线程。
    sys.exit(run_standalone())
