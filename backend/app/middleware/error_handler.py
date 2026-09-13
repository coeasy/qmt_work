"""全局异常处理器（V9 Phase 5 / P1-27）。

统一信封覆盖框架级错误，消除「绕过信封契约」的三类漏网响应：
- 404（未匹配路由，Starlette 默认 ``{"detail": "Not Found"}``）
- 422（请求校验失败，FastAPI 默认 ``{"detail": [...]}``）
- 500 / 未捕获异常（默认裸 500 文本）

约定：``/api/v1/*`` 路径一律返回 ``{code, message, data}`` 信封；
业务码与 HTTP 状态同值（404/422/500），与「业务异常 200+code!=0」约定并存。
其余路径（/mcp、静态、文档）保持框架默认行为，不劫持。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("qmt_work.error_handler")

_API_PREFIX = "/api/v1/"


def _envelope(code: int, message: str, data=None) -> dict:
    return {"code": code, "message": message, "data": data}


def register_error_handlers(app: FastAPI) -> None:
    """在 create_app 中注册三个全局 handler。"""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        if not request.url.path.startswith(_API_PREFIX):
            # 非 API 路径（/mcp、静态、文档）：保持框架默认语义
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        code = exc.status_code if exc.status_code != 401 else 401
        return JSONResponse(
            _envelope(code, str(exc.detail)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith(_API_PREFIX):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        # errors() 可能含非序列化对象（bytes/Exception），做一次安全化
        try:
            import json
            json.dumps(exc.errors(), default=str)
            errs = exc.errors()
        except (TypeError, ValueError):
            errs = [{"msg": str(e)} for e in exc.errors()]
        return JSONResponse(
            _envelope(422, "请求参数校验失败", {"errors": errs}),
            status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled_exc(request: Request, exc: Exception):
        log.exception("unhandled exception on %s %s: %s",
                      request.method, request.url.path, exc)
        if not request.url.path.startswith(_API_PREFIX):
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        return JSONResponse(_envelope(500, "服务器内部错误"), status_code=500)
