"""V9 Phase 5：Lifecycle Required/Optional 显式分层（P1-22 / P1-17）。

启动阶段不再「失败一律吞掉继续跑」：
- **Required** 阶段失败 → lifespan 直接抛错（等效 sys.exit），进程不进入半残状态，
  外部探活 `/ready` 自然 503（进程未起）或编排器按启动失败处理；
- **Optional** 阶段失败 → 记录 error 状态并继续（Degraded），QMT/看门狗等失败
  绝不影响平台 READY（「QMT 失败不失去 READY」语义）。

Required 集合的唯一真源在 ``core/state.REQUIRED_PHASES``（core 不依赖 app，
state 的 mark_ready 需要同一份清单），本模块只做映射与执行。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from fastapi import FastAPI

from core.state import REQUIRED_PHASES

log = logging.getLogger("qmt_work.bootstrap.lifecycle")

PhaseFn = Callable[[FastAPI], Awaitable[dict]]

#: 阶段 -> 分层声明。未列出的阶段默认 Optional（显式声明表见 DoD）。
PHASE_LEVELS: dict[str, str] = {
    "db": "required",
    "broker": "optional",      # 券商连接失败 → Degraded，绝不阻断 READY
    "engines": "required",
    "watchdogs": "required",
    "replay": "required",
    "misc": "required",
}

assert set(REQUIRED_PHASES) == {k for k, v in PHASE_LEVELS.items() if v == "required"}, (
    "PHASE_LEVELS 与 state.REQUIRED_PHASES 不一致")


async def run_phases(app: FastAPI,
                     phases: list[tuple[str, PhaseFn]],
                     mark_phase: Callable[[str, str], None]) -> dict:
    """按序执行阶段；Required 失败抛 RuntimeError（阻断启动），Optional 失败降级。

    返回 {name: {"status": ..., "ms": ..., "error": ...}}（供 /health 展示）。
    """
    results: dict = {}
    loop = asyncio.get_running_loop()
    for name, fn in phases:
        level = PHASE_LEVELS.get(name, "optional")
        mark_phase(name, "starting")
        t0 = loop.time()
        try:
            payload = await fn(app)
            ms = (loop.time() - t0) * 1000
            mark_phase(name, "ready")
            log.info("bootstrap phase %s (%s) done in %.0fms", name, level, ms)
            # ★ 结构对齐本函数 docstring（R25）：此前直接存 fn 的返回值，于是
            #   `status` / `ms` / `error` 三个键**根本不存在**，谁按文档读谁拿到
            #   KeyError；相位的耗时与降级原因也因此无法上报到 /health。
            results[name] = {"status": "ready", "ms": round(ms, 1),
                             "level": level, "detail": payload}
        except Exception as exc:  # noqa: BLE001
            ms = (loop.time() - t0) * 1000
            mark_phase(name, "error")
            log.exception("bootstrap phase %s (%s) failed in %.0fms: %s",
                          name, level, ms, exc)
            if level == "required":
                # P1-22：Required 失败阻断启动 —— 让 lifespan 抛错，
                # uvicorn 退出（等效 sys.exit），绝不带病服务。
                raise RuntimeError(
                    f"required bootstrap phase '{name}' failed: {exc}") from exc
            results[name] = {"status": "error", "ms": round(ms, 1),
                             "level": level, "error": str(exc)}
    return results


__all__ = ["PHASE_LEVELS", "run_phases"]
