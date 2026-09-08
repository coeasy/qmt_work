"""阶段 3：核心引擎（风控 / 同步 / WS / 回测 / 模拟盘 / 策略）。

- 风险配置加载
- 同步引擎（SyncEngine）初始化 + 行情管道注册
- WS 管理器
- 回测任务队列
- 模拟盘引擎
- 策略运行时
"""
from __future__ import annotations

import json
import logging

from core.state import state
from fastapi import FastAPI
from sync import SyncEngine, WSManager

log = logging.getLogger("qmt_work.bootstrap.engines")


async def setup(app: FastAPI) -> dict:
    # 风险配置：把持久化参数载入模块级 risk 单例
    if state.db is not None:
        try:
            row = state.db.query_one(
                "SELECT params_json FROM risk_config WHERE scope='global'")
            if row and row.get("params_json"):
                state.risk.update_from(json.loads(row["params_json"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("load risk config failed, use defaults: %s", exc)

    # 行情共享总线（默认内存；配置 redis 时跨进程共享）
    from core.config import settings
    from gateway.quote_bus import create_quote_bus
    state.quote_bus = create_quote_bus(
        redis_url=settings.quote_bus_redis_url,
        enabled=(settings.quote_bus_backend == "redis"))
    log.info("quote bus: %s", state.quote_bus.stats().get("mode"))

    # 出站 webhook
    from gateway.webhook_out import WebhookOut
    state.webhook_out = WebhookOut(
        state.db, base_delay=settings.webhook_out_retry_backoff)
    log.info("webhook out ready: %d subs", len(state.webhook_out._configs()))

    # 同步引擎
    state.sync_engine = SyncEngine(state.broker_manager, state.db,
                                   quote_bus=state.quote_bus,
                                   risk=state.risk, notifier=state.notifier,
                                   webhook_out=state.webhook_out,
                                   runtime_config=state.runtime_config)
    state.ws_manager = WSManager(state.sync_engine)
    state.sync_engine.on_notify(state.ws_manager.broadcast)

    # 行情管道统一注册（C3 修复：SyncEngine 构造之后）
    _register_quote_handlers = state.sync_engine.on_event
    for conn in state.broker_manager.all_connections():
        if conn.bridge is not None:
            conn.bridge.on("quote", _register_quote_handlers)
            state.sync_engine.register_realtime_trade_handlers(conn)

    state.sync_engine.start_batch()
    await state.sync_engine.start_account_snapshots(interval=5.0)

    # 回测任务队列
    from backtest import BacktestQueue
    state.backtest_queue = BacktestQueue(max_workers=2)
    state.backtest_queue.on_event(state.ws_manager.broadcast)

    # 模拟盘引擎
    from engines.paper_engine import PaperEngine
    state.paper_engine = PaperEngine().init(state.db)

    def _paper_ref_close(code: str):
        se = state.sync_engine
        if se is None:
            return None
        q = (getattr(se, "latest_quotes", None) or {}).get(code.upper())
        if not isinstance(q, dict):
            return None
        for k in ("preClose", "lastClose", "prevClose"):
            v = q.get(k)
            if v:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None
    state.paper_engine.set_ref_close_provider(_paper_ref_close)
    log.info("paper trading engine ready")

    # 策略运行时
    from engines.strategy_runtime import StrategyRuntime
    state.strategy_runtime = StrategyRuntime(state)
    restored = state.strategy_runtime.restore()
    log.info("strategy runtime ready: restored %d running instance(s)", restored)

    return {"strategy_runs_restored": restored}


__all__ = ["setup"]
