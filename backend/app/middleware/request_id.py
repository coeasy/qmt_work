"""X-Request-ID 请求链路追踪：中间件 + 日志上下文。

- 每个 HTTP 请求分配一个唯一 id（客户端可经 `X-Request-ID` 头透传，便于跨服务串联）。
- id 通过 contextvars 注入，所有该请求生命周期内的日志自动带上 `[<id>]`，
  便于在并发日志中按请求聚合排障。
- RequestIDFilter 把 id 填进 logging 记录的 `request_id` 字段。
"""
import logging
import uuid
from contextvars import ContextVar

from starlette.requests import Request

_request_id_ctx: ContextVar[str | None] = ContextVar("qmt_work_request_id", default=None)


def get_request_id() -> str | None:
    """获取当前请求 id（可在任意日志/业务代码处调用）。"""
    return _request_id_ctx.get()


async def request_id_middleware(request: Request, call_next):
    """FastAPI/Starlette http 中间件：注入并回传 X-Request-ID。"""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = _request_id_ctx.set(rid)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        _request_id_ctx.reset(token)


class RequestIDFilter(logging.Filter):
    """日志过滤器：为每条记录附加 request_id（无则 `-`）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_ctx.get() or "-"
        return True
