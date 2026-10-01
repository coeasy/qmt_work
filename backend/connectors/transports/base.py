"""Transport 公共基类：统一错误翻译与超时纪律（INV-8）。"""
from __future__ import annotations

import time
from typing import Any

from ..transport import TransportError, WireRequest, WireResponse


class BaseTransport:
    """各 transport 的公共部分。

    子类只需实现 ``_dispatch``（真正的一次交互），其余（信封、计时、异常翻译）
    在这里统一，避免每个 transport 各写一套而出现语义漂移。
    """

    transport_id: str = "base"

    #: 是否支持**回调式**实时订阅（subscribe_quote 的 on_tick 能真正被调用）。
    #: 进程内直连（miniQMT）为 True；文件/Redis/ZMQ 等跨进程桥接为 False ——
    #: 回调对象无法序列化过线，这类传输的「实时」只能走 EventPort / 轮询查询。
    supports_callback: bool = False

    async def invoke(self, request: WireRequest) -> WireResponse:
        started = time.perf_counter()
        try:
            return await self._dispatch(request, started)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001  —— 传输层故障一律归 503 语义
            raise TransportError(
                f"[{self.transport_id}] {request.op} 传输失败: {exc}") from exc

    async def _dispatch(self, request: WireRequest, started: float) -> WireResponse:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _ok(result: Any, request: WireRequest, started: float, **meta: Any) -> WireResponse:
        return WireResponse.success(
            result, signal_id=request.signal_id, started=started, **meta)

    @staticmethod
    def _fail(error: str, error_type: str, request: WireRequest,
              started: float, **meta: Any) -> WireResponse:
        return WireResponse.failure(
            error, error_type, signal_id=request.signal_id, started=started, **meta)


__all__ = ["BaseTransport"]
