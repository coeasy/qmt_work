"""阶段 4：监控/守护/算法引擎。

- DB 备份（启动时一次 + 后台周期）
- 系统状态广播
- 券商健康监控 + 自动重连
- 行情泵守护（2s 周期补注册）
- 涨停监控
- 算法单引擎
- 条件单引擎
- 订单超时守护
- 统一信号入口
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from core.config import settings
from core.state import state

log = logging.getLogger("qmt_work.bootstrap.watchdogs")


def _should_auto_connect(conns) -> bool:
    """守护是否应当尝试自动接入（纯函数，便于单测锁定判据）。

    判据是**「有没有真正连上的连接」**，而不是「连接列表是否为空」：

    - 已有 connected          → False（无事可做）；
    - 列表为空（全新安装）     → True；
    - 有持久意图但没连上       → True（自愈，覆盖「先开软件后开客户端」）；
    - 列表非空且全部 inactive  → False（用户手动断开过，尊重意图）。

    ★ 用「列表为空」当判据是错的：只要存在任意一条持久连接（哪怕它坏了），
    守护就永不介入 ⇒ 客户端明明开着却永远连不上。
    """
    if any(getattr(c, "connected", False) for c in conns):
        return False
    if conns and not any(getattr(getattr(c, "cfg", None), "active", False) for c in conns):
        return False
    return True


#: 守护两轮之间的**基础**间隔（秒）。
_GUARD_BASE_INTERVAL = 30.0

#: 连续「探了但没接上」时的退避上限（秒）。
#:
#: ★ 为什么必须有（2026-10-15 真机取证）：此前守护恒 30s 一轮、无退避。
#:   本机路径 A 因券商授权 ``mdl_auth_gt_ipc_pair=0`` 永远不可能成功
#:   （客户端日志 ``The XtQuantServer is not allowed to start.``），于是守护
#:   每 30s 就把**两个**候选目录 × 6 个 session 全试一遍 —— 每轮新建最多 12 个
#:   ``XtQuantTrader``，几十轮后 SDK 的 writer 池被撑爆，报
#:   ``WaitingFreeWriter instances exceed maximum limit``。这个自造的错误反过来
#:   盖住了真正的券商拒绝（rc=-1），且**越重试越不可能恢复**
#:   （实测 reconnect_attempts 累到 35/36 次、日志每 30s 刷一条假成功）。
#:   退避把「不可能成功」的通道从 30s 拉到 5min：既止血，也让真原因浮出水面。
_GUARD_MAX_BACKOFF = 300.0

#: 守护的连续失败计数（模块级，仅供本守护与被其驱动的用例读写）。
_guard_fail_streak = 0


def _reset_guard_backoff() -> None:
    """清零退避（供单测在用例之间隔离状态）。"""
    global _guard_fail_streak
    _guard_fail_streak = 0


def _guard_next_interval() -> float:
    """下一轮等待时长：基础 30s，连续未接入则指数退避到 300s 上限。

    ★ 语义注意（2026-10-15 循环改为「先跑再睡」后）：本函数返回的是**本轮结束
    之后、下一轮开始之前**要睡的时长。因此连续失败的节拍是 60/120/240/300s
    （第一次失败就翻倍）—— 比旧「先睡再跑」的 30/60/120 各晚一轮的时刻点，
    但每一轮都更早（首轮立即执行，见 ``_broker_auto_connect_guard``）。
    """
    global _guard_fail_streak
    return min(_GUARD_BASE_INTERVAL * (2 ** _guard_fail_streak), _GUARD_MAX_BACKOFF)


async def _broker_auto_connect_guard() -> None:
    """启动后自动连接守护。

    解决的问题：``phase_broker.setup`` 的启动自动连接只在进程启动时探测一次。
    若那时本机券商客户端还没启动 / 没登录，``_auto_connect_active`` 直接跳过，
    此后**没有任何机制重试** —— 用户「先开软件、后开券商客户端」就会永远连不上。

    本守护在**没有任何连接真正连上**时自动接入本机运行中的客户端：

    - 已有连接处于 connected → 不动；
    - 列表为空（全新安装 / 从未连过）→ 探测到运行中的客户端即接入；
    - **有持久意图（cfg.active）但没连上 → 自愈**（复用 + 必要时修正客户端路径后接入）；
      这是「先开软件、后开 QMT 客户端」能自动连上的关键；
    - 列表非空且全部 inactive → 用户手动断开过，尊重其意图，不介入；
    - ``settings.broker_auto_connect`` 为 False → 完全不介入。

    ★ 循环顺序是**先跑再睡**（2026-10-15 真机取证，此前是反的）：
    旧顺序 ``sleep(30)`` 在前 ⇒ 启动预算（8s）在冷缓存机器上都没探完时，守护
    首轮比启动还晚 30s；叠加路径 A 的慢失败无 bound（见
    ``phase_broker._bounded_connect_action``），本机实测桥要约 60~75s 才出现，
    用户看到的是「客户端起来了快一分钟还没自动连上」，而机制其实是通的。
    现在相位装配结束即跑第一轮，与 ``phase_broker`` 的启动自动连接无缝衔接
    （那次可能刚被 8s 预算掐断）；失败按 ``_guard_next_interval`` 退避
    （60→300s），连上或无事可做则回到 30s 基础节拍。

    ★ 「无事可做」不计失败：旧实现把 ``_auto_connect_once`` 返回空串一律当失败
    计入退避 ⇒ 连接稳定连上后每 30s 白涨一节拍，几分钟后退避到 300s 上限；
    真掉线时要等最多 5 分钟才复探。现在已连上 / 用户手动断开 / 开关关闭都会
    把退避清零（这些轮次根本没有探测动作，谈不上失败）。

    复用 ``xtquant_client.autoconnect``（``_auto_connect_active`` 内部先路径 A
    ``detect_candidates`` / ``pick_active``，再路径 B ``detect_bridge_agents``），
    与「启动自动连接」「GET /brokers/auto-detect」同源，避免界面推荐 A、后台连 B。
    ``_auto_connect_active`` 内部按「券商+路径+账号」复用既有连接，不会重复建连。

    ★ 2026-10-14：本循环**不再**自带「有没有运行中的 xtquant 客户端」前置闸门。
    旧写法先 ``detect_candidates`` + ``pick_active`` 判一次，拿不到就 ``continue`` ——
    于是**路径 B 永远走不到**：大 QMT 走文件桥时唯一可用的通道是 QMT 内置策略里的
    agent，而路径 A 因券商未授权（``mdl_auth_gt_ipc_pair=0``）永远连不上。
    这正是「QMT 开着、桥也部署好了，客户端却始终不自动接上」的第二条复现路径
    （第一条在 ``phase_broker.setup``，已修）。现在直接交给 ``_auto_connect_active``：
    它自身在「A 无候选且 B 无 agent」时是**纯 no-op**（不建任何连接），
    且带 60s / 30s 两级预算，失败静默跳过、下轮再试。

    ★ 扫描次数（2026-10-14 更正，此前这段注释的说法是错的）：去掉本循环的闸门
    **并没有**自动消掉重复的全盘扫描 —— 当时 ``_auto_connect_active`` 内部仍是
    「A 探一次 + B 内部再探一次」。现已改为**每轮只探一次**
    （``_detect_candidates_budgeted`` 的结果同时喂给 A / B）。本机实测单次
    ``detect_candidates`` 约 2.6~3.3s，守护每 30s 一轮 ⇒ 少一次就是每 30s 省 ~3s
    的常驻磁盘 IO。
    """
    global _guard_fail_streak
    while True:
        # 先跑再睡：见 docstring。首轮在相位装配结束后立即执行，不等 30s。
        try:
            cid = await _auto_connect_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 单轮异常不终止守护
            log.warning("broker auto-connect guard error: %s", exc)
            _guard_fail_streak += 1
            await asyncio.sleep(_guard_next_interval())
            continue
        # 分类本轮结局，决定退避计数：
        # - 接上了                    → 清零（无事可做，下轮 30s 后轻量复查）；
        # - 无事可做（已连上/手动断开/开关关闭）→ 清零。这些轮次没有任何探测
        #   动作，计入失败会让掉线后的复探被无谓地拖到 300s；
        # - 探了但没接上              → 计入连续失败，指数退避止血。
        mgr = state.broker_manager
        nothing_to_do = (
            not settings.broker_auto_connect
            or mgr is None
            or not _should_auto_connect(mgr.all_connections())
        )
        if cid:
            _guard_fail_streak = 0
        elif nothing_to_do:
            _guard_fail_streak = 0
        else:
            _guard_fail_streak += 1
        await asyncio.sleep(_guard_next_interval())


async def _auto_connect_once() -> str:
    """守护的**一轮**（抽出来是为了可被单测直接驱动，不必去推 30s 的死循环）。

    返回本轮接入的 conn_id（无接入 = ``""``）。判据与副作用与守护完全一致。
    """
    from app.bootstrap.phase_broker import _auto_connect_active

    if not settings.broker_auto_connect:
        return ""
    mgr = state.broker_manager
    if mgr is None:
        return ""
    if not _should_auto_connect(mgr.all_connections()):
        return ""
    # ★ 这里**只**调用 _auto_connect_active，绝不在前面再加「路径 A 有没有候选」的
    #   闸门 —— 那正是让路径 B 永远走不到的原因（详见 _broker_auto_connect_guard
    #   的 docstring）。闸门的正确形态是「_auto_connect_active 内部先 A 后 B」。
    cid = await _auto_connect_active()
    if not cid:
        return ""
    # ★ 假绿灯修复（2026-10-15 真机取证）：``_auto_connect_active`` 返回
    #   ``bid or cid`` —— 路径 A 只是**建出了连接对象**、并没连上时也会返回
    #   conn_id。旧实现拿到非空就打 ``broker auto-connect guard connected: xxx``
    #   并刷新活跃指针，于是日志每 30s 一条「已连接」，而 /brokers 里那条连接
    #   ``connected=false``、``last_error=交易连接异常：WaitingFreeWriter...``。
    #   这正是用户「客户端说连上了、却什么都做不了」的直接来源：
    #   **建连 ≠ 连通**。这里以连接对象的实际状态为准。
    conn = next((c for c in mgr.all_connections()
                 if getattr(getattr(c, "cfg", None), "conn_id", None) == cid), None)
    if conn is not None and not getattr(conn, "connected", False):
        log.info("broker auto-connect guard: %s 已建连但未连通（%s），下一轮再试",
                 cid, (conn.last_error or "无错误信息")[:160])
        return ""
    # 同步进程级活跃指针（只写槽位，唯一入口见 core.context.cache_active_bridge）
    state.cache_active_bridge()
    log.info("broker auto-connect guard connected: %s", cid)
    return cid


async def _install_auto_connect_guard(app) -> None:
    """装配自动连接守护任务；``broker_auto_connect=False`` 时**不装配**。

    ★ 为什么开关关闭也要拦住装配（2026-10-15）：该开关的语义是「完全不介入」，
    而旧实现照样 ``create_task`` ⇒ 一个每 30s 醒来只为发现「开关关着」再睡回去的
    空转任务。既与文档语义相反，又让「守护还活着」成为测试会话里与用例抢模块级
    全局的隐形方（本轮两处守护用例的全量 flaky 根因之一）。

    该开关只从配置文件读取、无运行时切换口（``phase_broker`` 同样只在启动读），
    故按启动时取值决定装配是完备的；用户改开关后重启即生效。

    抽成独立函数是为了能对「开 ⇒ 装配 / 关 ⇒ 不装配」两面都上回归锁 ——
    装配条件写反的后果是自动连接**整体停摆**（即用户报过的那个故障），必须可证伪。
    """
    if not settings.broker_auto_connect:
        return
    _acg = asyncio.create_task(_broker_auto_connect_guard())
    app.state._broker_acg = _acg


async def setup(app: FastAPI) -> dict:
    # ★ 主库备份**不在这里**装配（2026-09-20 迁走）。
    #   本阶段之后还有 replay / misc 两个阶段在写库，而启动备份要记录「源库指纹」
    #   用于「没变化就跳过」—— 指纹一记下来就已经过期，于是「客户端反复启停不重复
    #   整库复制」永远不成立（实测：每次启动都白复制一份 1GB+ 的主库）。
    #   备份装配已移到最后一个阶段 phase_misc（库静止之后指纹才准）。
    #   停机阶段与 /config/paths 读的都是 state.db_backup，位置不变。

    # 系统状态广播
    async def _system_broadcast_loop():
        import time

        from app.version import __version__
        from gateway.trading_session import default_session
        while True:
            await asyncio.sleep(5.0)
            if state.ws_manager is None:
                continue
            try:
                payload = {
                    "uptime_seconds": int(time.time() - state.started_at) if state.started_at else 0,
                    "version": __version__,
                    "brokers_total": len(state.broker_manager.all_connections()),
                    "brokers_connected": sum(1 for c in state.broker_manager.all_connections() if c.connected),
                    "clients": state.ws_manager.client_count(),
                    "trading_session": default_session.stats(),
                    "started_at": state.started_at,
                }
                await state.ws_manager.broadcast("system", {"event": "tick", **payload})
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("system broadcast failed: %s", exc)
    _system_task = asyncio.create_task(_system_broadcast_loop())
    app.state._system_task = _system_task

    # 健康监控
    from gateway.health import BrokerHealthMonitor
    state.health_monitor = BrokerHealthMonitor(
        state.broker_manager, state.ws_manager.broadcast, check_interval=5.0)
    await state.health_monitor.start()

    # 行情泵守护
    async def _pump_guard():
        while True:
            await asyncio.sleep(2.0)
            loop = asyncio.get_running_loop()
            for conn in state.broker_manager.all_connections():
                b = conn.bridge
                if b is None or not (conn.cfg.active or conn.connected):
                    continue
                if state.sync_engine is not None:
                    try:
                        b.ensure_handler("quote", state.sync_engine.on_event)
                        state.sync_engine.register_realtime_trade_handlers(conn)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("handler guard %s: %s", conn.cfg.conn_id, exc)
                if not b.pump_running():
                    try:
                        b.start_pump_on(loop)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("pump guard %s: %s", conn.cfg.conn_id, exc)
    _pump_task = asyncio.create_task(_pump_guard())
    app.state._pump_task = _pump_task

    # 启动自动连接守护：覆盖「启动后用户才启动券商客户端」的场景。
    # boot 的 phase_broker 只在启动时探测一次（8s 预算）；若当时客户端未运行，
    # 之后由本守护接力 —— 它**先跑再睡**（首轮在相位装配结束立即执行，不等 30s），
    # 仅当「没有连接真正连上」时自动接入本机运行中的客户端，尊重「用户手动断开」
    # (连接列表非空但均 inactive) —— 此时不自动连，交给用户操作。
    # 开关关闭时不装配（见 _install_auto_connect_guard）。
    _install_auto_connect_guard(app)

    # 涨停监控 + 算法单引擎 + 条件单引擎 + 订单守护
    from engines.algo import AlgoEngine
    from engines.condition_order import ConditionOrderEngine
    from engines.limitup import LimitUpMonitor
    state.limitup_monitor = LimitUpMonitor(state.broker_manager, state.risk,
                                           state.ws_manager.broadcast, wal=state.wal)
    state.algo_engine = AlgoEngine(state.broker_manager, state.risk,
                                   state.ws_manager.broadcast, wal=state.wal,
                                   notifier=state.notifier)
    state.condition_engine = ConditionOrderEngine(
        state.broker_manager, state.risk, state.db, state.ws_manager.broadcast,
        wal=state.wal, notifier=state.notifier)
    state.condition_engine.load_from_db()
    await state.condition_engine.start(interval=2.0)

    from gateway.order_watchdog import OrderWatchdog
    state.order_watchdog = OrderWatchdog(
        state.broker_manager, timeout=settings.order_watchdog_timeout,
        interval=settings.order_watchdog_interval,
        enabled=settings.order_watchdog_enabled,
        on_event=state.ws_manager.broadcast, notifier=state.notifier,
        # P0-8：撤单纳入统一链路（WAL + 审计），不再「只撤不留痕」。
        wal=state.wal, db=state.db)
    await state.order_watchdog.start()

    # 统一信号入口
    from gateway.signal_router import SignalRouter

    async def _paper_quote(code: str) -> float:
        """paper 模式无券商时的取价兜底：走 DataSourceManager（eltdx / 公开源）。

        ★ 为什么必须接（2026-10-14 本地下单流程实测）：前端 OrderForm 的「最新价」
          来自 ``POST /market/quotes``，该端点缓存未命中时走 **DataSourceManager**，
          **不需要券商**。而 SignalRouter._est_price_async 此前只认券商桥 ⇒ 同一标的
          「界面显示着真实行情价、后端却报取不到行情」，用户按界面价格下市价单
          换来一句「未连接券商」—— 前后端取价口径分叉造成的断链。
          这里注入的正是 /market/quotes 背后**同一个** hub，两边口径归一。

        只服务 paper：live 模式实盘必须用券商实时价，拿公开源滞后价下单比拒单更危险。
        取不到返回 0.0（上层据此挂起/拒绝，绝不臆造价格）。
        """
        try:
            from core.quote_fields import pick_order_ref_price
            from datasource.registry import get_hub
            q = await get_hub().get_quote(code, source="auto")
        except Exception as exc:  # noqa: BLE001
            log.debug("paper 取价兜底失败 %s: %s", code, exc)
            return 0.0
        if not isinstance(q, dict):
            return 0.0
        p = pick_order_ref_price(q)
        try:
            return float(p) if p else 0.0
        except (TypeError, ValueError):
            return 0.0

    state.signal_router = SignalRouter(
        state.broker_manager, state.risk, state.db, state.wal,
        state.notifier, state.ws_manager.broadcast, runtime_config=state.runtime_config,
        # P0-6：paper 模式统一走 PaperEngine（与策略运行共用同一模拟盘账户）
        paper_engine=state.paper_engine,
        # paper 模式取价兜底（无券商也能给市价单估到真实行情价）
        quote_fn=_paper_quote)

    return {}


__all__ = ["setup"]
