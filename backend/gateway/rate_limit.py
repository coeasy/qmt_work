"""滑动窗口限流（按 token/ip + 每密钥独立配额）。"""
import time
from collections import deque

from fastapi import Request
from fastapi.responses import JSONResponse


class RateLimiter:
    def __init__(self, window: int, max_requests: int):
        self.window = window
        self.max_requests = max_requests
        self._hits: dict[str, deque[float]] = {}

    def _key(self, request: Request) -> str:
        token = request.headers.get("x-api-key") or request.query_params.get("token", "")
        host = request.client.host if request.client else ""
        return token or host or "anon"

    def allow(self, request: Request) -> bool:
        return self.allow_key(self._key(request), 0)

    def allow_key(self, key: str, per_key_limit: int = 0) -> bool:
        """per_key_limit>0 时覆盖全局 max_requests（每密钥独立配额）。"""
        limit = per_key_limit if per_key_limit > 0 else self.max_requests
        now = time.monotonic()
        dq = self._hits.setdefault(key, deque())
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def make_rate_limit_middleware(limiter: RateLimiter):
    async def rate_limit_middleware(request: Request, call_next):
        # loopback 免限流（本机开发/桌面壳）
        host = (request.client.host if request.client else "") or ""
        if host in {"127.0.0.1", "::1", "localhost"}:
            return await call_next(request)
        if not limiter.allow(request):
            return JSONResponse(status_code=429, content={
                "code": 429, "message": "rate limit exceeded", "data": None})
        return await call_next(request)
    return rate_limit_middleware
