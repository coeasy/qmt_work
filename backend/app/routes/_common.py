"""routes 子模块共享层：响应辅助、券商调用依赖、文件级共享导入。

拆分 routes.py（原 1400+ 行单体）后，各业务域子模块（broker/account/trade/...）
统一从此处导入 ok/err/_need/_call 与共享符号，避免循环依赖、保持单一真相来源。
"""
import json
import time
import uuid
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from agent import AgentCore, LLMConfig, build_provider
from app import crypto
from app.config import settings
from app.state import state
from xtquant_client.base import BrokerError, BrokerNotConnectedError
from xtquant_client.manager import ConnectionConfig
from xtquant_client.registry import get_profile, list_profiles


def ok(data) -> dict:
    return {"code": 0, "message": "ok", "data": data}


def err(code: int, message: str, extra=None) -> dict:
    """统一错误响应。extra 携带附加数据（兼容旧代码 err(code, msg, obj) 三参调用）。"""
    return {"code": code, "message": message, "data": extra if extra is not None else None}


def _need(conn_id: str | None = None):
    """取指定/活跃 bridge；无连接返回 None（调用方返回 503）。"""
    return state.broker_manager.bridge(conn_id)


async def _call(b, fn, *args):
    """统一包装券商调用，BrokerError -> 错误响应字典。"""
    try:
        return await b.call(fn, *args)
    except BrokerError as exc:
        return err(503, str(exc))


def _ws_authorized(ws: WebSocket) -> bool:
    """WS 鉴权：loopback 免 Key；远程需 token（query 或 Sec-WebSocket-Protocol），校验主密钥或子密钥。"""
    from gateway.auth import is_loopback
    if is_loopback(ws):
        return True
    token = ws.query_params.get("token", "")
    if not token:
        proto = ws.headers.get("sec-websocket-protocol", "")
        token = proto.split(",")[0].strip() if proto else ""
    if not token:
        return False
    if token == settings.api_key:
        return True
    store = state.apikey_store
    if store is None:
        return False
    row = store.verify(token)
    if not row:
        return False
    from gateway.apikey import scope_match
    return scope_match("market", row.get("scopes", "")) or scope_match("trade", row.get("scopes", ""))


def _load_llm_config() -> LLMConfig:
    row = state.db.query_one("SELECT * FROM llm_config WHERE scope='global' ORDER BY id LIMIT 1")
    if not row:
        return LLMConfig()
    return LLMConfig(provider=row["provider"], base_url=row["base_url"],
                     api_key=crypto.decrypt_plain(row["api_key_enc"]) if row["api_key_enc"] else "",
                     model=row["model"], temperature=row["temperature"],
                     timeout=row["timeout_ms"] / 1000 if row["timeout_ms"] else 60)


async def _build_agent():
    cfg = _load_llm_config()
    provider = build_provider(cfg)
    tools = await state.mcp.get_tools()
    # FastMCP 2.x get_tools() 返回 dict {name: Tool}；统一转 list 供 AgentCore 使用
    tool_list = list(tools.values()) if isinstance(tools, dict) else list(tools)
    return provider, AgentCore(provider, tool_list) if provider else None
