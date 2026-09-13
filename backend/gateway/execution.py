"""统一真实交易执行核心（Phase 3）。

所有 REST/MCP/批量/策略入口都应先把委托意图交给这里，再由这里完成真实行情
估价、风控、加锁调用与审计。该模块不生成价格，也不提供模拟交易旁路；paper
模式由上层 SignalRouter 明确选择，不得伪装成 live 成交。
"""
from __future__ import annotations

from typing import Any, Optional


class ExecutionService:
    """单券商连接上的统一下单/撤单服务。"""

    def __init__(self, risk=None, db=None):
        self._risk = risk
        self._db = db

    def _audit(self, action: str, target: str, params: dict, result: str) -> None:
        if self._db is None:
            return
        try:
            self._db.audit("trading", action, target, params, result)
        except Exception:  # noqa: BLE001
            # 审计故障必须记录日志，但不能把已提交的柜台结果改写成失败。
            import logging
            logging.getLogger("qmt_work.gateway.execution").exception(
                "audit failed after execution: %s %s", action, target)

    async def _risk_price(self, bridge, code: str, price: float) -> tuple[Optional[float], str]:
        if price and price > 0:
            return float(price), ""
        try:
            quote = await bridge.call(bridge.gateway.get_quote, code)
        except Exception as exc:  # noqa: BLE001
            return None, f"无法取得真实最新价，拒绝市价单风控：{exc}"
        if isinstance(quote, dict):
            latest = quote.get("last") or quote.get("ask") or quote.get("bid")
            if latest is not None and float(latest) > 0:
                return float(latest), ""
        return None, "无法取得真实最新价，拒绝市价单风控"

    async def place_order(
        self,
        bridge,
        code: str,
        direction: str,
        volume: int,
        price: float = 0.0,
        price_type: str = "limit",
        strategy_name: str = "",
        remark: str = "",
        *,
        risk=None,
        audit_action: str = "order.submitted",
    ) -> dict:
        """执行一笔真实委托；市价/无价委托必须使用真实行情完成风控估价。"""
        checker = risk or self._risk
        params = {
            "code": code, "direction": direction, "volume": volume,
            "price": price, "price_type": price_type,
            "strategy": strategy_name, "remark": remark,
        }
        # V9 §7 强制规则：Mandatory Risk 不可绕过。风控未初始化时必须拒绝，
        # 绝不允许「checker is None 即放行」的无风控裸单。
        if checker is None:
            reason = "风控未初始化，拒绝下单（Mandatory Risk）"
            self._audit("order.rejected", code, params, reason)
            return {"ok": False, "reason": reason}
        risk_price, price_error = await self._risk_price(bridge, code, price)
        if risk_price is None:
            self._audit("order.rejected", code, params, price_error)
            return {"ok": False, "reason": price_error}
        allowed, reason = checker.check_order(
            code, risk_price, volume, direction, price_type)
        if not allowed:
            self._audit("order.rejected", code, params, reason)
            return {"ok": False, "reason": reason}
        # 关键正确性修复（P0-11）：市价单/无价单必须把「真实估价后的价格」
        # 作为保护价传给柜台，绝不能传原始 price（市价单 price=0 会被柜台判为
        # 废单）。risk_price 已通过真实行情校验：限价单时等于用户原始价，市价单时
        # 为最新价（见 _risk_price）。未知价场景在上方已被拒绝，不会走到这里。
        result: Any = await bridge.call_locked(
            bridge.gateway.place_order, code, direction, price_type, risk_price,
            volume, strategy_name, remark)
        if isinstance(result, dict) and result.get("code", 0) != 0:
            result["ok"] = False
            self._audit("order.failed", code, params, str(result.get("message") or result))
            return result
        if not isinstance(result, dict):
            result = {"result": result}
        result["ok"] = True
        self._audit(audit_action, code, params, f"order_id={result.get('order_id')}")
        return result

    async def cancel_order(self, bridge, order_id: str, *, action: str = "order.cancel") -> dict:
        result = await bridge.call_locked(bridge.gateway.cancel_order, order_id)
        self._audit(action, order_id, {}, "ok")
        if isinstance(result, dict):
            # 关键正确性修复（P0-12）：以网关返回的 ok 为准。旧实现读取不存在的
            # `code` 键（cancel_order 根本不返回 code），导致撤单永远被判成功。
            # 仅在网关未显式给出 ok 时，才回退到 code==0 的兼容判定。
            if "ok" not in result:
                result["ok"] = result.get("code", 0) == 0
        return result

    async def cancel_order_price(self, bridge, order_id: str, deviation: float = 0.01,
                                 *, action: str = "order.cancel_price") -> dict:
        result = await bridge.call_locked(
            bridge.gateway.cancel_order_price, order_id, deviation)
        self._audit(action, order_id, {"deviation": deviation}, "ok")
        if isinstance(result, dict):
            result["ok"] = result.get("code", 0) == 0
        return result


def get_execution_service() -> ExecutionService:
    from core.state import state
    return ExecutionService(risk=state.risk, db=state.db)


__all__ = ["ExecutionService", "get_execution_service"]
