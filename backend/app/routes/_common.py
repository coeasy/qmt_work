"""routes 子模块共享层：响应辅助、券商调用依赖、文件级共享导入。

拆分 routes.py（原 1400+ 行单体）后，各业务域子模块（broker/account/trade/...）
统一从此处导入 ok/err/_need/_call 与共享符号，避免循环依赖、保持单一真相来源。
"""
from fastapi import Request, WebSocket, WebSocketDisconnect

from app.config import settings
from app.state import state
from app import crypto  # noqa: F401  (re-export for routes: config.py/signal.py)
from xtquant_client.base import BrokerError
from xtquant_client.manager import ConnectionConfig  # noqa: F401  (re-export for routes: broker.py)
from xtquant_client.registry import get_profile, list_profiles  # noqa: F401  (re-export for routes: broker.py)


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
