"""鉴权分层中间件（工业级多密钥）：

- 本机 loopback 免鉴权（开发/桌面壳同源访问）
- 远程必须携带 Key：
  - settings.api_key（主密钥）：拥有全部权限（向后兼容）
  - api_keys 表中的密钥：按 scope + rate_limit 校验
- scope 分级：market/trade/account/backtest/admin/*（通配）
- 每密钥独立限流（rate_limit>0 时覆盖全局配额）
"""
from fastapi import Request
from fastapi.responses import JSONResponse

import hmac

from .apikey import scope_for_path, scope_match, _is_public_path

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def is_loopback(request) -> bool:
    """loopback 免鉴权判定。

    阶段 0-E（F10）：原实现仅看 request.client.host——一旦启用 --proxy-headers 或
    同机反代，伪造 `X-Forwarded-For: 127.0.0.1` 即可绕过全部鉴权。现要求：
    只要请求携带任何转发头（x-forwarded-for / forwarded），一律不再信任
    client.host，必须走完整鉴权。
    """
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        return False
    host = (request.client.host if request.client else "") or ""
    return host in _LOOPBACK


def _extract_token(request: Request) -> str:
    token = request.headers.get("x-api-key") or ""
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        token = request.query_params.get("token", "")
    return token


def make_auth_middleware(master_key: str, store_getter, limiter_getter):
    """store_getter/limiter_getter 为延迟解析函数（运行时从 state 取，避免初始化顺序问题）。"""

    async def auth_middleware(request: Request, call_next):
        from .metrics import get_metrics
        m = get_metrics()
        host = (request.client.host if request.client else "") or ""
        if is_loopback(request):
            resp = await call_next(request)
            m.record_request("public", resp.status_code, "loopback")
            return resp
        path = request.url.path
        # 公共只读路径（健康检查/API 文档/前端静态页）免鉴权
        if _is_public_path(path):
            resp = await call_next(request)
            m.record_request("public", resp.status_code, "public")
            return resp
        token = _extract_token(request)
        required_scope = scope_for_path(path)
        # 1) 主密钥（向后兼容，全权限）；阶段 0-E：常量时间比较防时序侧信道
        if token and master_key and hmac.compare_digest(token, master_key):
            resp = await call_next(request)
            m.record_request("admin", resp.status_code, "master")
            return resp
        # 2) 多密钥（校验 scope + 过期 + IP 白名单）
        store = store_getter()
        if store is not None:
            row = store.verify(token, host)
            if row:
                if not scope_match(required_scope, row.get("scopes", "")):
                    m.record_request(required_scope or "public", 403, str(row["id"]))
                    return JSONResponse(status_code=403, content={
                        "code": 403, "message": f"scope 不足：需要 {required_scope}", "data": None})
                limiter = limiter_getter()
                if limiter is not None and not limiter.allow_key(
                        str(row["id"]), int(row.get("rate_limit") or 0)):
                    m.record_request(required_scope or "public", 429, str(row["id"]))
                    return JSONResponse(status_code=429, content={
                        "code": 429, "message": "rate limit exceeded (api key)", "data": None})
                request.state.api_key_id = row["id"]
                request.state.api_key_name = row.get("name", "")
                resp = await call_next(request)
                m.record_request(required_scope or "public", resp.status_code, str(row["id"]))
                return resp
        resp = JSONResponse(status_code=401, content={
            "code": 401, "message": "invalid or missing api key", "data": None})
        m.record_request(required_scope or "public", 401, "")
        return resp

    return auth_middleware
