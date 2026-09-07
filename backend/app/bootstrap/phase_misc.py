"""阶段 6：杂项 - 行情缓存定时 + 资金流采集 + 数据源预热。

- 行情缓存定时维护（runtime_config 热控）
- 资金流自动采集（交易时段每 5 分钟）
- 多源行情补充源预热（best-effort 后台）
"""
from __future__ import annotations

import logging

from app.state import state
from fastapi import FastAPI

log = logging.getLogger("qmt_work.bootstrap.misc")


async def setup(app: FastAPI) -> dict:
    from gateway.market_sync import MarketSync
    state.market_sync = MarketSync(state, state.runtime_config)
    await state.market_sync.start()
    log.info("market sync ready: enabled=%s sync_time=%s",
             state.market_sync.enabled, state.market_sync.sync_time)

    try:
        from app.datasource.registry import get_hub
        import asyncio
        asyncio.create_task(get_hub().warmup_all())
        log.info("行情数据源预热任务已提交（后台）")
    except Exception as exc:  # noqa: BLE001
        log.warning("行情数据源预热任务提交失败：%s", exc)

    try:
        from app.routes.market import start_moneyflow_collector
        start_moneyflow_collector()
    except Exception as exc:  # noqa: BLE001
        log.warning("资金流自动采集启动失败：%s", exc)

    log.info("qmt_work started (real broker mode)")
    return {}


__all__ = ["setup"]
