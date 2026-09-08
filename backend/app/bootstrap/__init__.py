"""应用启动阶段编排（R1 重构）。

将 ``app/main.py`` 的 lifespan 拆分为按阶段组织的初始化函数，
主进程只负责 ``FastAPI()`` 构建 + include_router + 挂中间件。

历史背景：原 main.py 655 行单文件承担全部 lifespan 初始化（约 20+ 引擎/服务按序手动挂接），
维护困难。R1 重构把 lifespan 拆为 6 个阶段模块 + 优雅停机，按域分组、单一职责。

阶段顺序（启动）：
  phase_db        : DB 初始化、密钥、通知、告警、运行时配置、缓存、WAL
  phase_broker    : 券商连接、引导连接、行情管道注册
  phase_engines   : 风控、同步引擎、WS 管理、回测队列、模拟盘、策略运行时
  phase_watchdogs : 健康监控、泵守护、涨停、算法、条件单、订单超时
  phase_replay    : WAL 重放、委托对账
  phase_misc      : 行情缓存定时、elgt资金流采集、数据源预热

阶段顺序（停机）：shutdown.shutdown() 逆序关闭。

零功能回退：所有外部行为完全等价，仅内部代码组织。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from fastapi import FastAPI


log = logging.getLogger("qmt_work.bootstrap")

# 阶段函数签名：async def fn(app) -> dict  返回 dict 暴露给后续阶段
PhaseFn = Callable[[FastAPI], Awaitable[dict]]


def _ordered(phases: list[tuple[str, PhaseFn]]) -> list[tuple[str, PhaseFn]]:
    """返回阶段列表（顺序由调用方决定）。"""
    return phases


async def run_all(app: FastAPI, phases: list[tuple[str, PhaseFn]]) -> dict:
    """按顺序执行所有阶段，记录耗时和错误。"""
    results: dict = {}
    for name, fn in phases:
        t0 = asyncio.get_event_loop().time()
        try:
            results[name] = await fn(app)
            dt = (asyncio.get_event_loop().time() - t0) * 1000
            log.info("bootstrap phase %s done in %.0fms", name, dt)
        except Exception as exc:  # noqa: BLE001
            dt = (asyncio.get_event_loop().time() - t0) * 1000
            log.exception("bootstrap phase %s failed in %.0fms: %s", name, dt, exc)
            results[name] = {"error": str(exc)}
    return results


__all__ = ["PhaseFn", "run_all", "_ordered"]
