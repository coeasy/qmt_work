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


async def _broker_auto_connect_guard() -> None:
    """启动后自动连接守护。

    解决的问题：``phase_broker.setup`` 的启动自动连接只在进程启动时探测一次。
    若那时本机券商客户端还没启动 / 没登录，``_auto_connect_active`` 直接跳过，
    此后**没有任何机制重试** —— 用户「先开软件、后开券商客户端」就会永远连不上。

    本守护每 30s 复探一次，在**没有任何连接真正连上**时自动接入本机运行中的客户端：

    - 已有连接处于 connected → 不动；
    - 列表为空（全新安装 / 从未连过）→ 探测到运行中的客户端即接入；
    - **有持久意图（cfg.active）但没连上 → 自愈**（复用 + 必要时修正客户端路径后接入）；
      这是「先开软件、后开 QMT 客户端」能自动连上的关键；
    - 列表非空且全部 inactive → 用户手动断开过，尊重其意图，不介入；
    - ``settings.broker_auto_connect`` 为 False → 完全不介入。

    复用 ``xtquant_client.autoconnect`` 的 ``detect_candidates`` / ``pick_active``，
    与「启动自动连接」「GET /brokers/auto-detect」同源，避免界面推荐 A、后台连 B。
    ``_auto_connect_active`` 内部按「券商+路径+账号」复用既有连接，不会重复建连。
    """
    from app.bootstrap.phase_broker import _auto_connect_active
    from xtquant_client.autoconnect import detect_candidates, pick_active

    while True:
        await asyncio.sleep(30.0)
        try:
            if not settings.broker_auto_connect:
                continue
            mgr = state.broker_manager
            if mgr is None:
                continue
            conns = mgr.all_connections()
            if not _should_auto_connect(conns):
                continue
            try:
                cands = await asyncio.wait_for(
                    asyncio.to_thread(detect_candidates), timeout=60.0)
            except Exception as exc:  # noqa: BLE001 — 探测失败静默跳过，下轮再试
                log.debug("auto-connect guard detect failed: %s", exc)
                continue
            if pick_active(cands) is None:
                continue
            cid = await _auto_connect_active()
            if cid:
                # 同步进程级活跃指针，供 routes/tools/sync 取行情
                state.bridge = mgr.active_bridge()
                state.gateway = state.bridge.gateway if state.bridge else None
                log.info("broker auto-connect guard connected: %s", cid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 单轮异常不终止守护
            log.warning("broker auto-connect guard error: %s", exc)


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
    # boot 的 phase_broker 只在启动时探测一次；若当时客户端未运行，之后永不重试。
    # 这里每 30s 复探：仅当「完全没有连接」时自动接入本机运行中的客户端，
    # 尊重「用户手动断开」(连接列表非空但均 inactive) —— 此时不自动连，交给用户操作。
    _acg = asyncio.create_task(_broker_auto_connect_guard())
    app.state._broker_acg = _acg

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
    state.signal_router = SignalRouter(
        state.broker_manager, state.risk, state.db, state.wal,
        state.notifier, state.ws_manager.broadcast, runtime_config=state.runtime_config,
        # P0-6：paper 模式统一走 PaperEngine（与策略运行共用同一模拟盘账户）
        paper_engine=state.paper_engine)

    return {}


__all__ = ["setup"]
