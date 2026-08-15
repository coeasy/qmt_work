from app.routes._common import ok, err, state

from fastapi import APIRouter
# --- stdlib imports injected by fix_route_imports ---
import asyncio
import base64
import datetime
import hashlib
import io
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional



router = APIRouter()

@router.post("/backtest/jobs")
async def create_backtest_job(body: dict):
    kind = body.get("kind", "backtest")
    if kind not in ("backtest", "compare", "sensitivity", "sweep"):
        return err(400, f"unknown kind: {kind}")
    job = await state.backtest_queue.submit(kind, body.get("params", {}))
    return ok(job)

@router.get("/backtest/jobs")
async def list_backtest_jobs():
    jobs = state.db.query("SELECT * FROM backtest_jobs ORDER BY created_at DESC LIMIT 50")
    return ok(jobs)

@router.get("/backtest/jobs/{job_id}")
async def get_backtest_job(job_id: str):
    job = state.backtest_queue.get(job_id)
    if not job:
        row = state.db.query_one("SELECT * FROM backtest_jobs WHERE id=?", (job_id,))
        return ok(row) if row else err(404, "job not found")
    return ok({k: v for k, v in job.items()})

@router.delete("/backtest/jobs/{job_id}")
async def cancel_backtest_job(job_id: str):
    ok_flag = state.backtest_queue.cancel(job_id)
    return ok({"cancelled": ok_flag})


@router.post("/backtest/sweep")
async def create_sweep_job(body: dict):
    """参数网格扫描（P1 向量化）：穷举 param_grid 组合，按夏普排序选优。

    body: {symbol, strategy, param_grid:{fast:[...],slow:[...]}, initial_capital?, count?, broker_id?}
    """
    if not body.get("param_grid"):
        return err(400, "param_grid 不能为空")
    job = await state.backtest_queue.submit("sweep", body)
    return ok(job)


# ---------------- 历史 K 线（缓存优先） ----------------

