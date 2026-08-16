"""滑动窗口限流（按 token/ip + 每密钥独立配额）。

阶段 2.3 内存治理：用 OrderedDict 实现 LRU 上限（max_keys），并在窗口过期后
清理空桶，避免大量不同 key 导致内存无限增长。
"""
import time
from collections import OrderedDict, deque

from fastapi import Request
from fastapi.responses import JSONResponse


class RateLimiter:
    def __init__(self, window: int, max_requests: int, max_keys: int = 10000):
        self.window = window
        self.max_requests = max_requests
        self.max_keys = max_keys
        # OrderedDict：访问/写入移到末尾，超出容量弹开头（LRU 淘汰）
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

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
        dq = self._hits.get(key)
        if dq is None:
            dq = deque()
            self._hits[key] = dq
            # 超出容量：淘汰最久未触达的键，防止内存无限增长
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(key)
        # 时间窗 pruning
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        # 命中成功 → 触达末尾（LRU 保鲜）
        self._hits.move_to_end(key)
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
