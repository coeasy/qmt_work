"""阶段 5：WAL 重放 + 委托对账。

- WAL 算法单启动重放（按 algo_id 聚合终态，仅重放 pending/running；按已发切片量扣减）
- 启动后立即对账一次 + 定时巡检
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI

from core.state import state

log = logging.getLogger("qmt_work.bootstrap.replay")


def _sum_sent_volume(db, algo_id: str) -> int:
    """聚合某算法单已发切片量。"""
    if db is None:
        return 0
    try:
        rows = db.query(
            "SELECT params_json FROM audit_log WHERE action='algo.slice' "
            "AND target LIKE ?", (f"{algo_id}:%",))
    except Exception:  # noqa: BLE001
        return 0
    total = 0
    for r in rows or []:
        try:
            total += int((json.loads(r.get("params_json") or "{}") or {})
                         .get("volume") or 0)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return total


def _replay_algos() -> None:
    """WAL 算法单启动重放。"""
    if not state.wal:
        return
    try:
        records = state.wal.all_records()
    except Exception as exc:  # noqa: BLE001
        log.warning("wal replay read failed: %s", exc)
        return
    pending_jobs: dict[str, dict] = {}
    for rec in records:
        if rec.get("entity") != "algo":
            continue
        aid = str(rec.get("entity_id") or "")
        op = rec.get("op", "")
        if not aid or op in ("pause", "resume"):
            continue
        if op == "create":
            pending_jobs[aid] = rec.get("payload", {})
        elif op in ("final", "cancel"):
            pending_jobs.pop(aid, None)
    replayed = 0
    for aid, payload in pending_jobs.items():
        if payload.get("status", "") not in ("pending", "running"):
            continue
        already_sent = _sum_sent_volume(state.db, aid)
        try:
            asyncio.create_task(state.algo_engine.submit(
                payload.get("code", ""), payload.get("direction", "buy"),
                int(payload.get("volume", 0)), payload.get("algo", "twap"),
                int(payload.get("duration", 300)), int(payload.get("slices", 5)),
                payload.get("price_type", "market"),
                float(payload.get("limit_price", 0) or 0), payload.get("remark", ""),
                already_sent=already_sent))
            replayed += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("wal replay algo failed: %s", exc)
    log.info("wal algo replay: %d pending job(s) restarted", replayed)


async def setup(app: FastAPI) -> dict:
    _replay_algos()

    from core.config import settings
    from gateway.reconcile import OrderReconciler
    state.reconciler = OrderReconciler(
        state.broker_manager, wal=state.wal, db=state.db,
        on_event=state.ws_manager.broadcast, notifier=state.notifier)
    try:
        res = await state.reconciler.reconcile()
        log.info("startup reconcile: %s",
                 {k: v for k, v in res.items() if k != "details"})
    except Exception as exc:  # noqa: BLE001
        log.warning("startup reconcile failed: %s", exc)
    await state.reconciler.start(interval=settings.reconcile_interval)
    return {}


__all__ = ["setup"]
