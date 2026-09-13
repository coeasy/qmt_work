"""MCP Streamable HTTP 契约冒烟（Phase 1 ②：拆分自 smoke2.test_mcp_handshake）。

进程内 app fixture（conftest.app_client），CI 无需存活后端。
契约：POST /mcp/ initialize 握手返回 200 且含 protocolVersion/capabilities；
GET 无 SSE Accept 时按协议返回 4xx（而非 500）。
"""
MCP_URL = "/mcp/"
_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "pytest", "version": "1.0"}},
}
_HEADERS = {"Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"}


def test_mcp_initialize_handshake(app_client):
    r = app_client.post(MCP_URL, json=_INIT, headers=_HEADERS)
    assert r.status_code == 200, f"status={r.status_code} body={r.text[:200]}"
    assert "protocolVersion" in r.text, r.text[:200]
    assert "capabilities" in r.text, r.text[:200]


def test_mcp_get_without_sse_accept_is_4xx(app_client):
    r = app_client.get(MCP_URL, headers={"Accept": "application/json"})
    assert 400 <= r.status_code < 500, f"status={r.status_code}"


def test_mcp_initialize_bad_payload_is_4xx(app_client):
    """协议层健壮性：非法 JSON-RPC 载荷不得 500。"""
    r = app_client.post(MCP_URL, json={"jsonrpc": "2.0", "id": 2, "method": "__no_such__"},
                        headers=_HEADERS)
    assert r.status_code < 500, f"status={r.status_code} body={r.text[:200]}"
