"""MCP 接入端点回归：`/mcp` 与 `/mcp/` 都必须能用。

★ 锁定的真实缺陷（2026-09-18 打包态实测发现）：
    ``POST /mcp``（无尾斜杠）返回 **405**，只有 ``POST /mcp/`` 才 200。
    而 README、``GET /capabilities/mcp`` 的 ``endpoint``、根端点说明**全都写成
    ``/mcp``** ⇒ **照文档配置的 MCP 客户端一律握手失败**，且 405 的报错
    完全看不出「加个斜杠就好」。

修复方式是在 ASGI 层重写 path（不是 307 重定向）—— 部分 MCP 客户端不跟随重定向，
或对 3xx 直接判定接入失败。

★ 两个路径都要测：只测 ``/mcp/`` 的话，别名一旦回退测试照样全绿，
而用户按文档配的恰恰是**不带斜杠**的那条路。

★ 必须复用 conftest 的 ``app_client``（session 作用域、已跑 lifespan）：
MCP 的 ``StreamableHTTPSessionManager.run()`` 每个实例只能跑一次，自己再建一个
app 会直接 RuntimeError —— 那与被测行为无关，纯属测试基建冲突。
"""
from __future__ import annotations

import json


def _init_body() -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "regression", "version": "1.0"},
        },
    }).encode("utf-8")


def _mcp_headers() -> dict:
    return {
        "Content-Type": "application/json",
        # streamable-http：JSON 与 SSE 两种响应都要能接
        "Accept": "application/json, text/event-stream",
    }


def _parse(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(text)


def test_mcp_without_trailing_slash_is_not_405(app_client):
    """`/mcp`（无斜杠）必须能握手 —— 文档里写的就是这个路径。"""
    r = app_client.post("/mcp", content=_init_body(), headers=_mcp_headers())
    assert r.status_code != 405, (
        "POST /mcp 返回 405：照文档配置的 MCP 客户端会全部握手失败"
    )
    assert r.status_code == 200, f"status={r.status_code} body={r.content[:200]!r}"
    assert "result" in _parse(r.content)


def test_mcp_with_trailing_slash_still_works(app_client):
    """`/mcp/` 必须照旧可用（不能为了修别名把原路径弄坏）。"""
    r = app_client.post("/mcp/", content=_init_body(), headers=_mcp_headers())
    assert r.status_code == 200, f"status={r.status_code} body={r.content[:200]!r}"
    assert "result" in _parse(r.content)


def test_capabilities_mcp_endpoint_matches_served_path(app_client):
    """自省接口告诉用户的 endpoint 必须真的可用（否则等于给了个错地址）。"""
    import asyncio

    from app.routes.capabilities import capabilities_mcp

    ep = asyncio.run(capabilities_mcp())["data"]["endpoint"]
    assert ep in ("/mcp", "/mcp/"), ep
    r = app_client.post(ep, content=_init_body(), headers=_mcp_headers())
    assert r.status_code == 200, f"自省返回的 endpoint {ep} 实际不可用：{r.status_code}"
