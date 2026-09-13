"""批量执行服务（V9 §6.2 Execution Unification）。

V9 要求批量交易不再形成独立风控/执行旁路：

    BatchOrderCommand → BatchExecutionService → N × OrderIntent
                      → SignalRouter(统一链路) → ExecutionService → ExecutionPort

每个批量子单都是一个完整意图：经过 ExecutionMode(live/paper/dry_run)、
Mandatory Risk、幂等（batch_id + 序号 + 内容哈希）、WAL、审计。
单子单幂等键带 batch_id，崩溃/重试后重新提交同一批次不会重复下单。
"""
from __future__ import annotations

import asyncio

BATCH_CONCURRENCY = 4  # 与批量重连一致的最大并发，防止打爆柜台


class BatchExecutionService:
    """跨账户批量下单：N × OrderIntent 全部经 SignalRouter 统一链路执行。"""

    def __init__(self, router, concurrency: int = BATCH_CONCURRENCY):
        self._router = router
        self._sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def submit_batch(self, orders: list[dict], batch_id: str = "") -> dict:
        """orders: [{conn_id, code, direction, volume, price, price_type}]。

        返回 {"total", "ok", "results":[{conn_id,code,direction,volume,status,order_id?,detail}]}，
        形状与旧批量接口一致；新增 mode 字段标明每单的执行模式（live/paper/dry_run）。
        """
        results: list[dict] = [None] * len(orders)  # type: ignore[list-item]

        async def _one(idx: int, o: dict) -> None:
            rec = {"conn_id": o.get("conn_id", ""), "code": o.get("code", ""),
                   "direction": o.get("direction", ""), "volume": o.get("volume", 0),
                   "status": "rejected", "detail": "", "mode": ""}
            code = str(o.get("code", "")).strip().upper()
            direction = (o.get("direction") or "").lower()
            volume = int(o.get("volume", 0) or 0)
            if not code or direction not in ("buy", "sell") or volume <= 0:
                rec["detail"] = "参数非法（code/direction/volume）"
                results[idx] = rec
                return
            key_src = (f"batch:{batch_id}:{idx}:{o.get('conn_id', '')}:{code}:"
                       f"{direction}:{volume}:{o.get('price', 0)}:{o.get('price_type', 'limit')}")
            try:
                async with self._sem:
                    res = await self._router.submit(
                        code, direction, volume, float(o.get("price", 0) or 0),
                        o.get("price_type", "limit"), source="batch",
                        broker_id=str(o.get("conn_id", "") or ""),
                        remark=str(o.get("remark", "") or f"batch-{batch_id}-{idx}"),
                        idempotency_key=key_src, auto_confirm=True)
            except Exception as exc:  # noqa: BLE001
                rec["detail"] = f"执行异常：{exc}"
                results[idx] = rec
                return
            if isinstance(res, dict) and res.get("ok", False):
                rec["status"] = "submitted"
                rec["mode"] = res.get("mode", "")
                if res.get("order_id"):
                    rec["order_id"] = res.get("order_id")
                rec["detail"] = "ok" if res.get("mode", "live") == "live" else str(res.get("mode"))
            else:
                rec["mode"] = (res.get("mode", "") if isinstance(res, dict) else "")
                rec["detail"] = (res.get("reason") or res.get("message")
                                 if isinstance(res, dict) else str(res))
            results[idx] = rec

        await asyncio.gather(*(_one(i, o) for i, o in enumerate(orders)))
        ok_count = sum(1 for r in results if r and r.get("status") == "submitted")
        return {"total": len(orders), "ok": ok_count, "results": results}


def get_batch_execution_service() -> BatchExecutionService:
    """从全局状态取统一信号路由构造批量服务（每次调用轻量，无连接态）。"""
    from core.state import state
    if state.signal_router is None:
        raise RuntimeError("SignalRouter 未初始化，批量执行不可用")
    return BatchExecutionService(state.signal_router)


__all__ = ["BatchExecutionService", "get_batch_execution_service"]
