"""阶段 2：券商连接 + 行情管道注册。

- 加载持久化的券商连接
- 引导连接（环境变量 QMT_ACCOUNT_ID/QMT_CLIENT_PATH）
- 启动各连接 bridge（带超时保护）
- 交易日历感知调度刷新
"""
from __future__ import annotations

import asyncio
import logging
import time

from core.config import settings
from core.state import state
from fastapi import FastAPI
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


async def setup(app: FastAPI) -> dict:
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
    return {"started": started, "failed": failed}


__all__ = ["setup"]
