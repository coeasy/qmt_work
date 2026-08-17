"""单飞（single-flight）幂等（阶段 0-B / F1 修复）。

旧实现是「get 检查 → await 下单 → set 写入」的竞态窗口：两个同 ``client_order_id``
的并发调用（LLM 超时重试 / 前端双击）会双双通过检查、各下一单 → 真实重复成交。

新实现「先占位后执行」语义：
- 窗口内已完成的同 key 请求直接返回缓存结果（标记 duplicated）；
- 并发的同 key 请求复用**同一个进行中的执行**（future 共享），绝不二次下单。
"""
from __future__ import annotations

import asyncio
import time

_WINDOW = 30.0
_lock = asyncio.Lock()
_cache: dict[str, tuple[float, object]] = {}
_inflight: dict[str, "asyncio.Future"] = {}


def _mark_dup(result):
    if isinstance(result, dict):
        return {**result, "duplicated": True}
    return result


async def single_flight(key: str, coro_factory, window: float = _WINDOW):
    """同一 key 的并发调用只执行一次真实逻辑，其余复用其结果。

    coro_factory: 无参协程工厂（每次执行时调用，返回真实结果）。
    """
    now = time.time()
    async with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] <= window:
            # 阶段 3：幂等命中可观测——重复请求被窗口缓存拦截，不再二次下单
            try:
                from gateway.metrics import get_metrics
                get_metrics().record_idempotency_hit()
            except Exception:  # noqa: BLE001
                pass
            return _mark_dup(hit[1])
        fut = _inflight.get(key)
        if fut is not None:
            # 并发同 key：复用进行中的执行，不重复下发，同样标记 duplicated
            # （与窗口缓存命中语义一致——凡未执行真实逻辑的调用都标记重复）
            result = await fut
            return _mark_dup(result)
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        _inflight[key] = fut
    try:
        result = await coro_factory()
    except Exception as exc:  # noqa: BLE001
        fut.set_exception(exc)
        raise
    else:
        fut.set_result(result)
        _cache[key] = (time.time(), result)
        return result
    finally:
        _inflight.pop(key, None)
