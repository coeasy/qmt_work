"""WS 契约测试（Phase 1 ②：拆分自 smoke2.test_ws / test_ws_reconnect）。

进程内 app fixture，验证 /api/v1/ws：
- 首帧必须是 ``{"type":"snapshot", ...}``；
- ping → 后续帧含 pong（允许中间插播其它帧，兼容微批推送）；
- 断线重连后再次收到新快照。
"""
import json


def _ws_url() -> str:
    # TestClient host 非 loopback，WS 鉴权需要显式 token（loopback 免鉴权仅对真实本机请求生效）
    from core.config import settings
    return f"/api/v1/ws?token={settings.api_key}"


def _recv_until(ws, want: str, *, max_frames: int = 10, timeout: float = 5.0):
    """收帧直到某帧文本包含 want（跳过无关帧，如 quote 微批）。"""
    import time
    deadline = time.time() + timeout
    for _ in range(max_frames):
        remaining = max(0.1, deadline - time.time())
        frame = ws.receive_text() if hasattr(ws, "receive_text") else ws.recv(timeout=remaining)
        if want in frame:
            return frame
    raise AssertionError(f"{max_frames} 帧内未等到包含 {want!r} 的帧")


def test_ws_first_frame_is_snapshot(app_client):
    with app_client.websocket_connect(_ws_url()) as ws:
        frame = ws.receive_text()
        obj = json.loads(frame)
        assert obj.get("type") == "snapshot", str(obj)[:120]


def test_ws_ping_pong(app_client):
    with app_client.websocket_connect(_ws_url()) as ws:
        first = json.loads(ws.receive_text())
        assert first.get("type") == "snapshot", str(first)[:120]
        ws.send_text(json.dumps({"action": "ping"}))
        frame = _recv_until(ws, "pong")
        assert "pong" in frame


def test_ws_reconnect_gets_fresh_snapshot(app_client):
    with app_client.websocket_connect(_ws_url()) as ws:
        obj1 = json.loads(ws.receive_text())
        assert obj1.get("type") == "snapshot", str(obj1)[:120]
    # 断线（with 退出）后重连 → 新快照
    with app_client.websocket_connect(_ws_url()) as ws2:
        obj2 = json.loads(ws2.receive_text())
        assert obj2.get("type") == "snapshot", str(obj2)[:120]
