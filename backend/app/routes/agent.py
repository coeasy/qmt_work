from app.routes._common import ok, err, state, crypto, json, StreamingResponse, LLMConfig, build_provider, AgentCore, _build_agent

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

@router.post("/agent/chat")
async def agent_chat(body: dict):
    message = (body.get("message") or "").strip()
    if not message:
        return err(400, "message 不能为空")
    provider, agent = await _build_agent()

    async def event_stream():
        if agent is None:
            yield "event: text\ndata: " + json.dumps(
                {"delta": "尚未配置 LLM Provider：请到「设置 → 模型供应商」填写 Base URL / API Key / 模型后重试。"
                          "（不配置 LLM 不影响回测/下单/分析页面与 MCP/REST 接口）"}, ensure_ascii=False) + "\n\n"
            yield "event: done\ndata: " + json.dumps(
                {"message": "LLM 未配置"}, ensure_ascii=False) + "\n\n"
            return
        try:
            async for evt in agent.run(message):
                yield f"event: {evt['type']}\ndata: " + json.dumps(
                    evt, ensure_ascii=False, default=str) + "\n\n"
        except Exception as exc:
            yield "event: error\ndata: " + json.dumps({"message": str(exc)}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- 涨停监控 / 打板助手 ----------------

