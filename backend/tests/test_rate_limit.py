"""限流测试：窗口配额 + 每密钥配额 + LRU 上限 + 窗口过期。"""
import time

from gateway.rate_limit import RateLimiter


def test_allows_within_limit_then_blocks():
    rl = RateLimiter(window=60, max_requests=3)
    assert rl.allow_key("a") is True
    assert rl.allow_key("a") is True
    assert rl.allow_key("a") is True
    assert rl.allow_key("a") is False  # 第 4 次超限


def test_per_key_limit_overrides_global():
    rl = RateLimiter(window=60, max_requests=1, max_keys=10)
    for _ in range(5):
        assert rl.allow_key("b", per_key_limit=5) is True
    assert rl.allow_key("b", per_key_limit=5) is False


def test_lru_cap_bounded_memory():
    rl = RateLimiter(window=60, max_requests=1000, max_keys=5)
    for i in range(100):
        rl.allow_key(f"key-{i}")
    # 远超容量，但内存中键数被 LRU 上限钳制
    assert len(rl._hits) <= 5


def test_window_expiry_allows_again():
    rl = RateLimiter(window=1, max_requests=1, max_keys=10)
    assert rl.allow_key("c") is True
    assert rl.allow_key("c") is False
    time.sleep(1.1)
    assert rl.allow_key("c") is True  # 窗口过期后放行


# ---- 中间件级 x-forwarded-for 防绕过测试 ----
# 场景：同机反代（如 nginx 127.0.0.1:8080 → qmt_work 127.0.0.1:21118），
# request.client.host = 127.0.0.1。若中间件仅看 client.host，所有经代理转发的
# 外部请求都会被判为 loopback 而豁免限流（实测踩到）。
# 修复：中间件改用 gateway.auth.is_loopback，含 x-forwarded-for 检查。

from unittest.mock import MagicMock
from starlette.datastructures import Address
from gateway.rate_limit import make_rate_limit_middleware


def _mock_request(host: str, forwarded: str = "", forwarded_h: str = ""):
    """构造最小 Request mock：client.host + x-forwarded-for / forwarded 头。"""
    req = MagicMock()
    req.client = Address(host=host, port=80)
    # 用 dict-like 的 Mock 直接模拟 headers.get()，避免 Headers(raw=...) 的 bytes 转换问题
    hdr_dict = {}
    if forwarded:
        hdr_dict["x-forwarded-for"] = forwarded
    if forwarded_h:
        hdr_dict["forwarded"] = forwarded_h
    req.headers = MagicMock()
    req.headers.get = MagicMock(side_effect=lambda key, default=None: hdr_dict.get(key, default))
    return req


def test_middleware_exempts_loopback_no_forward():
    """无转发头的 127.0.0.1 请求应豁免限流（直接 pass-through）。"""
    rl = RateLimiter(window=60, max_requests=1)
    mw = make_rate_limit_middleware(rl)
    req = _mock_request("127.0.0.1")

    async def fake_call_next(request):
        return {"status": 200}

    import asyncio
    resp = asyncio.run(mw(req, fake_call_next))
    assert resp == {"status": 200}
    # 豁免后不应消耗限流配额
    assert len(rl._hits) == 0


def test_middleware_blocks_with_forwarded_for():
    """携带 x-forwarded-for 时不再豁免限流（走正常配额）。"""
    rl = RateLimiter(window=60, max_requests=1)
    mw = make_rate_limit_middleware(rl)
    req = _mock_request("127.0.0.1", forwarded="203.0.113.50")

    async def fake_call_next(request):
        return {"status": 200}

    import asyncio
    # 第 1 次：通过限流（配额=1），返回 call_next 的原始结果
    resp = asyncio.run(mw(req, fake_call_next))
    assert resp == {"status": 200}
    # 第 2 次：超限，返回 JSONResponse(429)
    from fastapi.responses import JSONResponse
    resp = asyncio.run(mw(req, fake_call_next))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 429


def test_middleware_blocks_with_forwarded_header():
    """Forwarded 头（HTTP/2 风格）同样触发非豁免路径。"""
    rl = RateLimiter(window=60, max_requests=1)
    mw = make_rate_limit_middleware(rl)
    req = _mock_request("127.0.0.1", forwarded_h="for=192.168.1.100")

    async def fake_call_next(request):
        return {"status": 200}

    import asyncio
    resp = asyncio.run(mw(req, fake_call_next))
    assert resp == {"status": 200}
    # 超限
    from fastapi.responses import JSONResponse
    resp = asyncio.run(mw(req, fake_call_next))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 429
