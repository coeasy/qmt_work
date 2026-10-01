"""远端 HTTP 交易网关传输（Ptrade / 自建网关类客户端的未来形态）。

这里只解决「怎么送」——把 wire 请求 POST 到远端网关。
具体 JSON 字段名由 Dialect 决定，本文件不做任何业务假设。

★ INV-8：每个请求带 ``timeout``（httpx 超时 = min(request.timeout, MAX_TIMEOUT)）。
★ 零 mock：HTTP 非 2xx / 连接失败 ⇒ TransportError（503 语义），
  网关回的业务失败 ⇒ ok=False + error_type（400 语义），两者**不得合并**。
"""
from __future__ import annotations

from typing import Any

from ..ports import EventSemanticsSpec
from ..transport import MAX_TIMEOUT, TransportError, WireRequest, WireResponse
from .base import BaseTransport
from .semantics import spec_for


class HttpGatewayTransport(BaseTransport):
    transport_id = "http"

    def __init__(self, base_url: str, api_key: str = "", *, timeout: float = MAX_TIMEOUT):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.timeout = min(float(timeout), MAX_TIMEOUT)
        self._client: Any = None

    # ------------------------------------------------------------------
    def _ensure(self):
        if self._client is not None:
            return self._client
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise TransportError(
                "HTTP 传输需要 httpx: pip install httpx") from exc
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout,
            headers={"Authorization": "Bearer %s" % self.api_key} if self.api_key else None,
        )
        return self._client

    async def _dispatch(self, request: WireRequest, started: float) -> WireResponse:
        client = self._ensure()
        body = {
            "v": 1, "signal_id": request.signal_id, "op": request.op,
            "params": dict(request.params or {}),
        }
        try:
            resp = await client.post("/invoke", json=body,
                                     timeout=min(request.timeout, self.timeout))
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"[http] {request.op} 网络失败: {exc}") from exc
        if resp.status_code >= 500:
            raise TransportError(
                f"[http] 网关不可用: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"[http] 响应不是合法 JSON: {exc}") from exc
        if resp.status_code >= 400:
            return self._fail(payload.get("error") or f"HTTP {resp.status_code}",
                              payload.get("error_type") or "BrokerError",
                              request, started)
        return self._ok(payload.get("result"), request, started)

    def is_alive(self) -> bool:
        """O(1) 探活：只检查 client 是否已建立，**不做网络 IO**（HealthPort 契约）。"""
        return self._client is not None

    def event_semantics(self) -> EventSemanticsSpec:
        return spec_for(self.transport_id)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 —— 关闭失败不得阻塞停机（TD-25）
                pass


__all__ = ["HttpGatewayTransport"]
