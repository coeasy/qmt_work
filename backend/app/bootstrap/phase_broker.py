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
import time

from fastapi import FastAPI

from core.config import settings
from core.state import state
from core.state import init_broker_manager
from xtquant_client.manager import ConnectionConfig

log = logging.getLogger("qmt_work.bootstrap.broker")


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

    started = 0
    failed = 0
    for conn in state.broker_manager.all_connections():
        if conn.cfg.active:
            try:
                await asyncio.wait_for(conn.bridge.start(), timeout=32.0)
                conn.connected = conn.adapter.is_connected()
                log.info("broker connection started: %s (%s) connected=%s",
                         conn.cfg.name, conn.cfg.conn_id, conn.connected)
                started += 1
            except asyncio.TimeoutError:
                log.warning("broker start timed out (32s) for %s: 券商客户端未就绪，已跳过自动连接",
                            conn.cfg.conn_id)
                conn.connected = False
                failed += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("broker start failed %s: %s", conn.cfg.conn_id, exc)
                failed += 1

    state.bridge = state.broker_manager.active_bridge()
    state.gateway = state.bridge.gateway if state.bridge else None

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
    has_intent = any(c.cfg.active for c in state.broker_manager.all_connections())
    if settings.broker_auto_connect and not has_intent:
        auto_conn_id = await _auto_connect_active()
        if auto_conn_id:
            state.bridge = state.broker_manager.active_bridge()
            state.gateway = state.bridge.gateway if state.bridge else None
            started += 1

    # 交易日历感知调度
    from gateway.trading_session import default_session
    try:
        if state.bridge is not None:
            cal = await state.bridge.call(state.bridge.gateway.get_trading_calendar)
            if not default_session.refresh_from_calendar(cal or []):
                default_session.use_fallback()
    except Exception as exc:  # noqa: BLE001
        default_session.use_fallback()
        log.warning("trading calendar unavailable, fallback weekday rule: %s", exc)
    log.info("trading session: %s", default_session.stats())

    state.started_at = time.time()
    return {"started": started, "failed": failed, "auto_connected": auto_conn_id}


__all__ = ["setup"]

