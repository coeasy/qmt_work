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

@router.post("/target-portfolio/sync")
async def target_portfolio_sync(body: dict):
    from tools.target_portfolio import TargetPortfolioEngine
    engine = TargetPortfolioEngine(state.broker_manager, state.signal_router, state.db)
    res = await engine.sync(
        body.get("targets", {}),
        float(body.get("total_capital", 0) or 0),
        body.get("mode", "volume"),
        body.get("broker_id", ""),
        bool(body.get("dry_run", False)))
    if isinstance(res, dict) and res.get("ok"):
        return ok(res)
    return err(503, res.get("reason", "同步失败") if isinstance(res, dict) else "同步失败")

@router.get("/target-portfolio/plans")
async def target_portfolio_list():
    from tools.target_portfolio import TargetPortfolioEngine
    return ok(TargetPortfolioEngine(state.broker_manager, state.signal_router, state.db).list_plans())

@router.post("/target-portfolio/plans")
async def target_portfolio_save(body: dict):
    from tools.target_portfolio import TargetPortfolioEngine
    nid = TargetPortfolioEngine(state.broker_manager, state.signal_router, state.db).save_plan(
        body.get("name", ""), body.get("weights", {}))
    return ok({"id": nid})

@router.delete("/target-portfolio/plans/{pid}")
async def target_portfolio_delete(pid: int):
    from tools.target_portfolio import TargetPortfolioEngine
    TargetPortfolioEngine(state.broker_manager, state.signal_router, state.db).delete_plan(pid)
    return ok({"deleted": True})


# ---------------- Agent 对话（SSE 流式） ----------------