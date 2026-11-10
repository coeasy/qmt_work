"""响应信封兜底中间件。

所有 ``/api/v1/*`` 端点应返回 ``{code: 0, data: ...}``（成功）或 ``{code: 非0, message: ...}``（失败）。
此中间件为安全网：若响应是 JSON 列表或 dict 但缺少 ``code`` 字段，自动包成 ``{code: 0, data}``。

历史背景：
- 2026-09-06 R3 重构：发现多个端点（positions/orders/deals/calendar/sectors/l2）直接返回
  ``_call`` 的原始值（list 或 dict），导致前端 ``_req`` 判 ``j.code !== 0`` 抛错 → 列表空白。
- 修复路径分两层：
  1. 路由层：手工 ``envelope_ok()`` 包裹（已用于 trade/reference/market L2）。
  2. 中间件层：兜底（任何新端点若再犯同错，自动包信封，不影响前端）。

注意：
- 静态资源（``/static/*``, ``/``, ``/docs*``）和 WebSocket 路径不处理。
- 仅处理 ``Content-Type: application/json``。
- 若响应已是信封（dict 含 int ``code``），原样放行**body**，但会把
  ``code=503`` 提升为真实 HTTP 503（见 ``_promote_status``）。
- 仅兜底业务 503 字典（如 ``err(503, msg)``），该响应也含 ``code`` 字段，原样放行。

### 为什么只提升 503（2026-11-07，D-12）

历史实现里 ``err(400/404/503, ...)`` 一律返回 **HTTP 200 + body code**，理由是
「前端统一取 ``message``」。这在前端是能用的（``http.ts`` 两个分支都读
``env.message``），但代价是**状态码彻底失真**：监控无法按 5xx 告警、反代无法按
5xx 重试、``fetch`` 的 ``res.ok`` 永远是 true。真出现「券商断开」时，看板上
HTTP 全是 200，只能靠翻 body 才发现。

修的口径是**按语义分层**，不是一刀切：

* ``503`` = **服务端依赖不可用**（券商未连 / 引擎未初始化 / 数据源未就绪）。
  这正是 HTTP 503 的定义，属传输层语义 ⇒ 提升为真实 503。
* ``400 / 404 / 409`` = **调用方语义**（参数不对、资源不存在、状态冲突）。
  重试无意义，且全仓大量代码与用例按 ``code`` 分支判断 ⇒ **保持** HTTP 200 + code。

一次只改一类：把 400 也改成真 400 会把「修一个缺陷」变成「迁移全仓契约」，
收益与风险不成比例。将来要改，按同一函数扩 ``_PROMOTE_CODES`` 即可。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("qmt_work.envelope")

# 跳过路径：静态资源、前端 SPA、健康检查
_SKIP_PREFIXES = (
    "/static/", "/assets/", "/_next/", "/favicon", "/api/v1/health",
    "/api/v1/system/", "/docs", "/openapi.json", "/redoc",
)
# 跳过 WebSocket（BaseHTTPMiddleware 不接管 WS，但稳妥起见显式排除）
_WS_PATH = "/ws"


def _is_envelope(obj: Any) -> bool:
    """判断 dict 是否已是 {code:int, ...} 信封。"""
    if not isinstance(obj, dict):
        return False
    code = obj.get("code")
    return isinstance(code, int)


def _envelope_data(data: Any) -> dict:
    """把任意 data 包成 {code: 0, data: ...}。"""
    return {"code": 0, "data": data}


# 哪些信封 code 要提升为真实 HTTP 状态码。
#
# 只有 503：表示**服务端依赖不可用**，是传输层语义（监控/反代/客户端都该按状态码
# 识别并告警或重试）。400/404/409 是调用方语义，保持 HTTP 200 + code。
_PROMOTE_CODES = frozenset({503})


def _promote_status(status_code: int, data: Any, path: str) -> int:
    """把信封里「传输层语义」的 code 提升为真实 HTTP 状态码。

    三重闸门，任一不满足就原样返回，**绝不猜测**：

    1. ``path`` 必须在 ``/api/v1/`` 下 —— 中间件本身没有按前缀过滤（``/mcp`` 的
       JSON-RPC 响应也会流经这里），而 JSON-RPC 也有 ``code`` 字段概念，跨协议
       套用 HTTP 语义是错的。
    2. ``status_code`` 必须仍是 ``200`` —— 路由若已显式给出状态码
       （``JSONResponse(status_code=...)`` / ``HTTPException``），那是更强的意图，
       中间件不许覆盖。
    3. ``code`` 必须是 ``_PROMOTE_CODES`` 里的值 —— 其余业务码保持 200。
    """
    if status_code != 200:
        return status_code
    if not path.startswith("/api/v1/"):
        return status_code
    if not _is_envelope(data):
        return status_code
    code = data.get("code")
    # _is_envelope 已保证 code 是 int（bool 是 int 子类，但 True/False 不在集合里）
    if code in _PROMOTE_CODES:
        return int(code)
    return status_code


class EnvelopeMiddleware(BaseHTTPMiddleware):
    """REST 响应信封兜底中间件。

    触发条件：
    - 路径以 ``/api/v1/`` 开头
    - 响应 Content-Type 是 ``application/json``
    - 响应体是 list/dict 但不是已包装的信封
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        # 跳过：WS / 静态 / 健康 / 文档
        if path == _WS_PATH or any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        response = await call_next(request)

        # 仅处理 application/json
        ctype = response.headers.get("content-type", "")
        if "application/json" not in ctype:
            return response

        # 读取并解析 body
        body_chunks = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, str):
                body_chunks.append(chunk.encode("utf-8"))
            else:
                body_chunks.append(chunk)
        body_bytes = b"".join(body_chunks)

        if not body_bytes:
            return response

        try:
            data = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError):
            # 非 JSON 响应（例如 {"detail": "Not Found"} 由 Starlette 抛）原样返回
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        # 已是信封（含 int code）原样放行 body；仅把 503 提升为真实状态码
        if _is_envelope(data):
            promoted = _promote_status(response.status_code, data, path)
            if promoted != response.status_code:
                log.info("envelope status promoted: %s %s -> HTTP %d",
                         path, response.status_code, promoted)
            return Response(
                content=body_bytes,
                status_code=promoted,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        # 兜底：list 或 dict 但无 code 字段 → 包信封
        if isinstance(data, (list, dict)):
            wrapped = _envelope_data(data)
            # 仅当路径是 /api/v1/ 时打 warning（避免误报其他 JSON）
            if path.startswith("/api/v1/"):
                log.warning(
                    "envelope fallback: %s 返回未包装数据 (%s)，已自动包裹为信封",
                    path, type(data).__name__)
            new_body = json.dumps(wrapped, ensure_ascii=False, default=str)
            new_headers = dict(response.headers)
            new_headers["content-length"] = str(len(new_body.encode("utf-8")))
            return Response(
                content=new_body.encode("utf-8"),
                status_code=response.status_code,
                headers=new_headers,
                media_type="application/json",
            )

        return Response(
            content=body_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
