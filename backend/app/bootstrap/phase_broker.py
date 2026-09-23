"""阶段 2：券商连接 + 行情管道注册。

- 加载持久化的券商连接
- 引导连接（环境变量 QMT_ACCOUNT_ID/QMT_CLIENT_PATH）
- 启动各连接 bridge（带超时保护）
- **启动自动连接**（默认开）：无活跃连接时探测本机运行中的 QMT 客户端并接入
- 交易日历感知调度刷新
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import FastAPI

from core.config import settings
from core.errors import swallow
from core.state import state
from core.state import init_broker_manager
from xtquant_client.manager import ConnectionConfig

log = logging.getLogger("qmt_work.bootstrap.broker")


def path_usable(client_path: str) -> bool:
    """客户端路径是否**有可能**连上（唯一入口，界面与启动共用同一判据）。

    ★ 为什么需要它：指向不存在目录的历史/测试残留连接（如
    ``C:/no_such_qmt/userdata_mini``）永远连不上，但启动时仍会被逐个
    ``wait_for(bridge.start(), 32s)`` —— 本机 17 条这类残留就把启动拖到 ~90s，
    用户看到的是「软件半天打不开」，而不是「有 17 条无效连接」。

    路径为空时返回 True：空路径走「adapter 自行探测」，**不能**因此判定不可用
    （否则会把合法配置误杀）。

    注：与 ``manager.status()`` 的 ``path_exists`` 不是同一个问题 —— 那个是给界面
    显示的「路径是否存在」（空路径也算不存在），这里是「值不值得尝试连接」。
    """
    p = (client_path or "").strip()
    if not p:
        return True
    return os.path.isdir(p)


def _bootstrap_from_env() -> None:
    """环境变量引导连接。"""
    if settings.account_id or settings.client_path:
        cfg = ConnectionConfig(
            conn_id="bootstrap", name="引导连接",
            broker_id=settings.broker_id or "guojin",
            client_path=settings.client_path, account_id=settings.account_id,
            account_type=settings.account_type, session_id=settings.session_id,
            active=True)
        state.broker_manager.add_connection(cfg, autoconnect=False)


async def _auto_connect_active() -> str:
    """启动自动连接：探测本机**正在运行**的 QMT 客户端并接入。

    先按「券商 + 客户端路径 + 资金账号」找既有连接：**有就复用**（重新点亮持久意图
    并拉起），没有才新建 —— 否则「用户手动断开过 → 下次启动又新建一条」会让连接
    列表随启动次数膨胀（实测踩过：三次启动累积出 3 条重复的「迅投 XTQuant 22453951」）。

    全程**非阻断**：探测 / 建连的任何失败都只记日志，绝不抛出（启动流程优先）。
    返回接入的 conn_id；未接入返回 ""。
    """
    from xtquant_client.autoconnect import build_connection, detect_candidates, pick_active

    try:
        cands = await asyncio.wait_for(asyncio.to_thread(detect_candidates), timeout=60.0)
    except asyncio.TimeoutError:
        log.warning("自动探测本机 QMT 客户端超时（60s），跳过自动连接")
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("自动探测本机 QMT 客户端失败，跳过自动连接：%s", exc)
        return ""

    cand = pick_active(cands)
    if cand is None:
        log.info("未发现正在运行的 QMT 客户端（候选 %d 个），跳过自动连接", len(cands))
        return ""
    fields = build_connection(cand)
    if fields is None:
        log.warning("候选客户端缺少资金账号或客户端路径，跳过自动连接：%s",
                    cand.get("root") or cand.get("name"))
        return ""

    try:
        existing = state.broker_manager.find_by_identity(
            fields["broker_id"], fields["client_path"], fields["account_id"])
        if existing is not None:
            # 复用前先修路径：持久化写法（客户端根）与探测到的数据目录
            # （userdata_mini / userdata）常常不一致，不改会让这条连接一直连不上，
            # 又因为它「已存在」把自动连接挡在外面。只在未连上时修（能连上就别动）。
            if not existing.connected:
                await asyncio.to_thread(
                    state.broker_manager.repair_client_path,
                    existing.cfg.conn_id, fields["client_path"],
                    fields.get("client_mode") or "")
            conn = await asyncio.to_thread(
                state.broker_manager.activate, existing.cfg.conn_id)
            conn.connected = conn.adapter.is_connected()
            log.info("已复用既有连接接入本机 QMT 客户端：%s（账号 %s / %s）connected=%s",
                     conn.cfg.name, conn.cfg.account_id, conn.cfg.client_path, conn.connected)
            return conn.cfg.conn_id
        conn = await asyncio.to_thread(
            state.broker_manager.add_connection, ConnectionConfig(**fields), True)
    except Exception as exc:  # noqa: BLE001
        log.warning("自动连接失败（已跳过，不影响启动）：%s", exc)
        return ""

    conn.connected = conn.adapter.is_connected()
    log.info("已自动连接本机 QMT 客户端：%s（账号 %s / %s）connected=%s",
             fields["name"], fields["account_id"], fields["client_path"], conn.connected)
    return conn.cfg.conn_id


#: 启动期给「持久连接」的**总**等待预算（秒）。
#:
#: ★ 为什么必须有：``broker`` 相位在 ``lifecycle.PHASE_LEVELS`` 里是 **optional**，
#:   但「optional」只保证「**失败**不阻断 READY」—— ``run_phases`` 对每个相位是
#:   顺序 ``await`` 的，**「慢」照样阻断**。实测（2026-09-23 ``client.log``，本机
#:   「客户端已安装但未启动」这一最常见情形）：
#:
#:       ERROR  bridge 握手失败: _ping 调用超时 30.0s
#:       INFO   bootstrap phase broker (optional) done in 32011ms
#:       INFO   Application startup complete      <-- 这之后 uvicorn 才开始收请求
#:
#:   也就是说这 32s 里**整个 HTTP 服务一个字都不回**，用户看到的是「软件打不开」。
#:
#: 取值依据（来自 ``bridge_client`` 的设计注释）：客户端**已登录**时 ``_ping``
#: 5s 内返回；**未登录**时 SDK 要 10~30s 才阻塞/异常。8s 因此能干净切开
#: 「能连上」与「连不上」两类，既不误杀慢机器冷启动，也不再陪跑满 30s。
STARTUP_CONNECT_BUDGET = 8.0

#: 单条连接的**硬**上限（秒）。它不是启动预算：预算到点就把连接交还后台继续跑，
#: 这里只是兜底，防止某条连接的启动永久挂起。与 ``bridge_client._HANDSHAKE_TIMEOUT``
#: （30s）对齐并留 2s 余量，使「后台继续跑」的行为与改动前完全一致。
CONNECT_HARD_TIMEOUT = 32.0

#: 交易日历拉取的超时（秒）。券商 ``bridge.call`` 内部**没有** wait_for，
#: 接口卡住时这里会**无限等待**，把启动与「连接晚到后的重刷」一起堵死（R25 补齐）。
#: 超时即退回「工作日」规则 —— 日历只影响会话判定，不值得为它拖住任何东西。
CALENDAR_FETCH_TIMEOUT = 5.0

#: 启动预算内没跑完、仍在后台拉起的任务。**必须持强引用**：asyncio 只保留任务的
#: 弱引用，fire-and-forget 的任务可能在跑完之前就被 GC 掉（经典陷阱），
#: 表现为「连接永远停在半路」。
_bg_start_tasks: set[asyncio.Task] = set()


def _sync_active_bridge() -> None:
    """把进程级活跃指针同步为 manager 当前活跃连接（幂等，任何时刻可调用）。

    ★ 为什么不能只在 ``setup`` 末尾同步一次：预算内没连上的连接是在**之后**才连上的，
    而 ``state.bridge`` / ``state.gateway`` 只在装配期赋值 —— 不同步的话，
    「先开软件、后开 QMT 客户端」（最常见路径）会变成「连上了但活跃指针是空的」。

    注：业务侧取行情走 ``broker_manager.active_bridge()``（动态），不受影响；
    真正读 ``state.bridge`` 的是本模块的交易日历刷新。
    """
    mgr = state.broker_manager
    if mgr is None:
        return
    state.bridge = mgr.active_bridge()
    state.gateway = state.bridge.gateway if state.bridge else None


async def _refresh_trading_calendar() -> None:
    """用活跃券商拉交易日历刷新会话判定；不可用则退回「工作日」规则（永不抛）。

    抽成函数是为了能在**连接晚到**时再刷一次：启动预算内没连上时先按 fallback
    运行，等后台把连接拉起来后再校正 —— 否则整个进程生命周期都停在 fallback
    （工作日规则会把节假日当交易日）。
    """
    from gateway.trading_session import default_session
    try:
        if state.bridge is not None:
            # ★ 必须带超时（R25）：券商 `call` 内部没有 wait_for，接口卡住时
            #   这里会无限等待 —— 既堵启动，也堵「连接晚到后重刷日历」那条路。
            cal = await asyncio.wait_for(
                state.bridge.call(state.bridge.gateway.get_trading_calendar),
                timeout=CALENDAR_FETCH_TIMEOUT)
            if not default_session.refresh_from_calendar(cal or []):
                default_session.use_fallback()
    except asyncio.TimeoutError:
        default_session.use_fallback()
        log.warning("交易日历获取超时（%.0fs），改用工作日规则", CALENDAR_FETCH_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        default_session.use_fallback()
        log.warning("trading calendar unavailable, fallback weekday rule: %s", exc)
    log.info("trading session: %s", default_session.stats())


async def _start_one(conn) -> str:
    """拉起一条连接，返回 ``"started"`` / ``"failed"``（**绝不抛异常**）。"""
    try:
        await asyncio.wait_for(conn.bridge.start(), timeout=CONNECT_HARD_TIMEOUT)
    except asyncio.TimeoutError:
        conn.connected = False
        conn.last_error = f"连接超时（{CONNECT_HARD_TIMEOUT:.0f}s）：券商客户端未就绪"
        log.warning("broker start timed out (%.0fs) for %s: 券商客户端未就绪，已跳过自动连接",
                    CONNECT_HARD_TIMEOUT, conn.cfg.conn_id)
        return "failed"
    except Exception as exc:  # noqa: BLE001
        conn.connected = False
        conn.last_error = str(exc)[:500]
        log.warning("broker start failed %s: %s", conn.cfg.conn_id, exc)
        return "failed"
    conn.connected = conn.adapter.is_connected()
    log.info("broker connection started: %s (%s) connected=%s",
             conn.cfg.name, conn.cfg.conn_id, conn.connected)
    return "started"


def _hand_off_to_background(task: asyncio.Task, conn) -> None:
    """把「预算内没跑完」的连接交给后台：它结束时同步活跃指针 + 校正交易日历。

    交接而非取消：``bridge.start()`` 的真实工作在 ``run_in_executor`` 的线程里跑，
    取消协程**停不掉那个线程**，只会让状态停在半路；让它自然跑完（``_start_one``
    内部仍有 32s 硬上限）反而是确定的行为，与改动前一致。
    """
    _bg_start_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _bg_start_tasks.discard(t)
        try:
            outcome = t.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("后台拉起连接 %s 异常：%s", conn.cfg.conn_id, exc)
            return
        log.info("后台拉起连接 %s 结束：%s（connected=%s）",
                 conn.cfg.conn_id, outcome, conn.connected)
        if outcome != "started":
            return
        _sync_active_bridge()
        try:
            _cal = asyncio.get_running_loop().create_task(_refresh_trading_calendar())
            # 同样必须持**强引用**：这是 fire-and-forget，asyncio 只保留弱引用，
            # 任务可能在跑完前被 GC（R25 补齐 —— 与 _bg_start_tasks 的用法一致）。
            _bg_start_tasks.add(_cal)
            _cal.add_done_callback(_bg_start_tasks.discard)
        except RuntimeError as exc:
            # 停机中：事件循环已关闭，没有可调度的地方，也没有人再需要这份日历。
            # 用 swallow 而不是 `pass` —— 静默失败是本项目最难排查的一类问题。
            swallow(exc, why="停机中事件循环已关闭，交易日历无需再校正", logger=log)

    task.add_done_callback(_on_done)


async def start_persisted_connections(mgr, *,
                                      budget: float = STARTUP_CONNECT_BUDGET) -> dict:
    """拉起所有「应保持连接」的持久连接，返回
    ``{started, failed, skipped, deferred}``。

    ★ 单独抽成函数（而非内联在 ``setup`` 里）是为了**可测**：启动期逻辑最难验证，
    内联就只能用真库真适配器跑，而真适配器在 CI 里根本不存在。抽出来后可用
    桩 manager 断言「无效路径被跳过」「慢连接不占满启动预算」。

    计数语义（四类**互不混淆**，尤其别把「慢」算成「失败」）：

    - ``started``  : 预算内连上；
    - ``failed``   : 预算内**确定失败**（超时 / 异常），已写 ``last_error``；
    - ``skipped``  : 客户端路径不存在 ⇒ 直接跳过，不尝试连接（历史/测试残留）；
    - ``deferred`` : 预算内还没跑完 —— **不是失败**，已交给后台自愈
      （``gateway.health.BrokerHealthMonitor`` 的指数退避重连 +
      ``phase_watchdogs`` 的 30s 自动连接守护），启动流程不再等它。

    ``budget`` 是**总预算**（不是每条）：所有连接**并发**拉起、共享同一份预算，
    因此 N 条连接的启动耗时与 1 条相当。顺序 ``await`` 会让耗时随 N 线性增长
    —— 「十几条残留连接把启动拖到 ~90s」就是这么来的。
    """
    started = 0
    failed = 0
    skipped = 0
    deferred = 0
    eligible = []
    for conn in mgr.all_connections():
        if not conn.cfg.active:
            continue
        # 路径不存在 ⇒ 不可能连上，**立刻跳过**而不是烧掉超时预算。
        # 这类条目是历史/测试残留（界面已标「路径无效」），等待它们只会拖慢启动：
        # 每条 30s+，十几条就把「打开软件」变成一件要等几分钟的事。
        if not path_usable(conn.cfg.client_path):
            conn.connected = False
            conn.last_error = f"客户端路径不存在：{conn.cfg.client_path}"
            skipped += 1
            log.warning("跳过无效连接 %s（%s）：客户端路径不存在 %s —— 请在「连接管理」中修正或删除",
                        conn.cfg.name, conn.cfg.conn_id, conn.cfg.client_path)
            continue
        eligible.append(conn)

    if eligible:
        tasks = {asyncio.ensure_future(_start_one(c)): c for c in eligible}
        done, pending = await asyncio.wait(set(tasks), timeout=budget)
        for t in done:
            if t.result() == "started":
                started += 1
            else:
                failed += 1
        for t in pending:
            conn = tasks[t]
            conn.connected = False
            # 「慢」与「失败」必须分开归因：这里给的是**出路**（后台会继续连），
            # 不是判决。若写成「连接失败」，用户会跑去改配置 —— 而其实什么都不用做。
            conn.last_error = (f"启动期 {budget:.0f}s 内未连上（券商客户端可能未启动或未登录），"
                               f"已转入后台自动重连")
            log.warning("启动期 %s（%s）未在 %.0fs 内连上 —— 已转入后台自动重连，不阻断启动",
                        conn.cfg.conn_id, conn.cfg.name, budget)
            _hand_off_to_background(t, conn)
            deferred += 1

    return {"started": started, "failed": failed,
            "skipped": skipped, "deferred": deferred}


async def setup(app: FastAPI) -> dict:
    init_broker_manager()  # V10 A2：core 延迟绑定，避免在 core 顶层 import xtquant_client
    # V10 A4：注入连接事件指标回调（xtquant_client 不再反向依赖 gateway.metrics）
    try:
        from gateway.metrics import get_metrics
        state.broker_manager.metrics_fn = (
            lambda conn_id, ev: get_metrics().record_conn_event(conn_id, ev))
    except Exception:  # noqa: BLE001
        pass
    state.broker_manager.load_persisted()
    _bootstrap_from_env()

    stats = await start_persisted_connections(state.broker_manager)

    _sync_active_bridge()

    # 启动自动连接：**没有任何「应保持连接」的连接**时才探测并接入本机运行中的客户端。
    #
    # 判据是「有没有 cfg.active 的连接」而不是「连接列表是否为空」，也不是
    # 「有没有活跃 bridge」：
    # - 用「没有活跃 bridge」会在「用户配了连接但客户端当时没开」时每次启动都再塞
    #   一条新连接，列表随启动次数膨胀（实测三次启动累积出 3 条重复连接）；
    # - 用「列表是否为空」则「用户手动断开过」之后就再也不会自动接了 —— 而手动断开
    #   恰恰最常见的原因是「当时客户端没开」，下次启动正应该自动接上。
    # `cfg.active` 是**持久意图**（用户显式断开才熄灭），因此这个判据既幂等
    # （复用既有连接，见 `_auto_connect_active`）又能覆盖上述两种情形。
    auto_conn_id = ""
    # ★ 路径不存在的连接不算「意图」：它永远连不上，若把它当意图就会**永久挡住**
    # 自动连接 —— 用户明明开着客户端，软件却始终连不上，且界面只显示一堆无效条目。
    has_intent = any(
        c.cfg.active and path_usable(c.cfg.client_path)
        for c in state.broker_manager.all_connections()
    )
    if settings.broker_auto_connect and not has_intent:
        # ★ 自动连接同样受启动预算约束（R25 补齐）：`_auto_connect_active` 内部的
        #   客户端探测最长 60s，此前**在预算之外** await ⇒ optional 相位照样能堵住
        #   启动 60s（R24 只包住了 start_persisted_connections，这里是漏网的那条）。
        #   超时不影响最终结果：`_broker_auto_connect_guard` 每 30s 复探一次，
        #   接上后会重同步 state.bridge。
        try:
            auto_conn_id = await asyncio.wait_for(
                _auto_connect_active(), timeout=STARTUP_CONNECT_BUDGET)
        except asyncio.TimeoutError:
            log.warning("启动期 %.0fs 内未完成本机客户端探测 —— 已交给后台自动连接守卫，"
                        "不阻断启动", STARTUP_CONNECT_BUDGET)
            auto_conn_id = ""
        if auto_conn_id:
            _sync_active_bridge()
            stats["started"] += 1

    # 交易日历感知调度
    await _refresh_trading_calendar()

    state.started_at = time.time()
    return {**stats, "auto_connected": auto_conn_id}


__all__ = ["setup", "path_usable", "start_persisted_connections",
           "STARTUP_CONNECT_BUDGET"]

