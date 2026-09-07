"""routes 子模块共享层：响应辅助、券商调用依赖、文件级共享导入。

拆分 routes.py（原 1400+ 行单体）后，各业务域子模块（broker/account/trade/...）
统一从此处导入 ok/err/_need/_call 与共享符号，避免循环依赖、保持单一真相来源。
"""
import asyncio
import logging

from fastapi import Request, WebSocket, WebSocketDisconnect

from app import crypto  # noqa: F401  (re-export for routes: config.py/signal.py)
from app.config import settings
from app.state import state
from xtquant_client.base import BrokerError
from xtquant_client.manager import ConnectionConfig  # noqa: F401  (re-export for routes: broker.py)
from xtquant_client.registry import get_profile, list_profiles  # noqa: F401  (re-export for routes: broker.py)

log = logging.getLogger("qmt_work.routes.common")


def ok(data) -> dict:
    return {"code": 0, "message": "ok", "data": data}


def err(code: int, message: str, extra=None) -> dict:
    """统一错误响应。extra 携带附加数据（兼容旧代码 err(code, msg, obj) 三参调用）。"""
    return {"code": code, "message": message, "data": extra if extra is not None else None}


# 无券商连接的统一 503 文案（单一真相源在 app/state.py；零 mock 降级口径见 README）。
from app.state import MSG_NO_BROKER  # noqa: E402  (re-export 供各路由域使用)


def no_broker() -> dict:
    """无可用券商连接时的统一 503 信封。"""
    return err(503, MSG_NO_BROKER)


def audit_log(actor: str, action: str, target: str, params: dict | None = None,
              result: str = "ok", ip: str = "") -> None:
    """统一写审计（T10）：D4 hash 链 + E4 脱敏，DB 未就绪时静默跳过（绝不阻断业务）。

    供所有写域端点调用：``audit_log("api", "screen.save_board", body.get("name", ""), body)``。
    """
    try:
        state.db.audit(actor, action, target, params or {}, result, ip)
    except (AttributeError, OSError, RuntimeError) as exc:
        # 审计失败不阻断业务路径（仅丢一条审计记录，可观测性降级）
        log.debug("审计写入失败（已忽略）：%s.%s %s: %s", actor, action, target, exc)


def _need(conn_id: str | None = None):
    """取指定/活跃 bridge；无连接返回 None（调用方返回 503）。"""
    return state.broker_manager.bridge(conn_id)


def envelope_ok(res):
    """把券商网关原始返回值（list / dict）统一包成 {code:0, data} 业务信封。

    前端 ``api`` 客户端强制要求 ``{code:0, data}`` 形态，否则 ``j.code !== 0``
    抛错导致列表界面空白。``_call`` 在超时 / BrokerError 时已返回 err 信封
    （int 型 code!=0），此处识别并原样放行，避免双层信封或损坏 503 文案。
    """
    if isinstance(res, dict) and isinstance(res.get("code"), int) and res.get("code") != 0:
        return res
    return ok(res)


async def _call(b, fn, *args, timeout: float = 12.0):
    """统一包装券商调用，BrokerError -> 错误响应字典；超时保护防止桥接子进程僵死时请求被永久挂起（否则 curl 5s 断开 → http=000）。"""
    try:
        return await asyncio.wait_for(b.call(fn, *args), timeout=timeout)
    except asyncio.TimeoutError:
        return err(503, f"券商响应超时（>{int(timeout)}s），请检查券商客户端是否已连接并登录")
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
