"""阶段 5 Agent REST 端点：会话持久化 + 对话。

设计原则（与全局一致）：
- LLM Provider 缺配（agent_enabled=False 或空 key）即 503 降级，绝不返回假回复；
- 对话走真实 Provider（OpenAI 兼容 / Anthropic）+ 真实工具（行情/账户/券商状态）；
- 会话落 sessions/messages 孤儿表，前端可加载历史会话。
"""
from __future__ import annotations

from typing import Any, Dict

from app.config import settings
from app.routes._common import err, ok
from app.state import state
from agent.core import AgentCore
from agent.default_tools import build_default_registry
from agent.errors import AgentNotConfigured
from agent.providers import build_provider
from fastapi import APIRouter

router = APIRouter()


def _build_core() -> AgentCore:
    """按配置构建 AgentCore；未启用 / 缺 key 即抛 AgentNotConfigured（端点转 503）。"""
    if not settings.agent_enabled or not settings.agent_api_key:
        raise AgentNotConfigured(
            "Agent 未配置：请在「设置」开启 agent_enabled 并配置 LLM API Key。")
    provider = build_provider(
        settings.agent_provider, settings.agent_api_key,
        settings.agent_model, settings.agent_base_url)
    return AgentCore(state.db, provider, build_default_registry())


def _try_core():
    """返回 (core, err_resp)；未配置时 err_resp 为 503 响应。"""
    try:
        return _build_core(), None
    except AgentNotConfigured as exc:
        return None, err(503, str(exc))


@router.get("/agent/status")
async def agent_status():
    """Agent 是否可用：未启用 / 缺 key 即明确标记未配置（前端据此显示引导）。"""
    configured = bool(settings.agent_enabled and settings.agent_api_key)
    if not configured:
        return ok({
            "enabled": bool(settings.agent_enabled),
            "configured": False,
            "detail": "Agent 未配置：请在「设置」配置 agent_enabled 与 LLM API Key。",
        })
    return ok({
        "enabled": True, "configured": True,
        "provider": settings.agent_provider,
        "model": settings.agent_model or "(provider 默认)",
    })


@router.get("/agent/sessions")
async def list_sessions():
    core, e = _try_core()
    if e:
        return e
    return ok({"sessions": core.list_sessions()})


@router.post("/agent/sessions")
async def create_session(body: Dict[str, Any]):
    core, e = _try_core()
    if e:
        return e
    title = (body or {}).get("title", "") or ""
    sid = core.create_session(title=title)
    return ok({"session_id": sid})


@router.get("/agent/sessions/{sid}")
async def get_session(sid: int):
    core, e = _try_core()
    if e:
        return e
    s = core.get_session(sid)
    if s is None:
        return err(404, "会话不存在")
    return ok(s)


@router.delete("/agent/sessions/{sid}")
async def delete_session(sid: int):
    core, e = _try_core()
    if e:
        return e
    core.delete_session(sid)
    return ok({"deleted": sid})


@router.post("/agent/chat")
async def agent_chat(body: Dict[str, Any]):
    """对话：system+历史+user → Provider → 顺序执行工具 → 再循环（至多 max_iterations）。"""
    message = (body or {}).get("message", "")
    if not message:
        return err(400, "message 必填")
    session_id = (body or {}).get("session_id")
    conn_id = (body or {}).get("conn_id") or None
    core, e = _try_core()
    if e:
        return e
    try:
        result = await core.chat(message, session_id=session_id, conn_id=conn_id)
    except AgentNotConfigured as exc:
        return err(503, str(exc))
    return ok(result)
