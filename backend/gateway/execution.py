"""统一真实交易执行核心（Phase 3）。

所有 REST/MCP/批量/策略入口都应先把委托意图交给这里，再由这里完成真实行情
估价、风控、加锁调用与审计。该模块不生成价格，也不提供模拟交易旁路；paper
模式由上层 SignalRouter 明确选择，不得伪装成 live 成交。
"""
from __future__ import annotations

from typing import Any, Optional

from core.quote_fields import pick_order_ref_price


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
            # 下单参考价唯一入口（V11 R14）：最新价优先，退到盘口可成交价。
            # 此前这里是 `last or ask or bid`，与 signal_router 里的复制体
            # 各自维护，且与行情/风控链路的键序不同。
            latest = pick_order_ref_price(quote)
            if latest is not None:
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
        risk_checked: bool = False,
    ) -> dict:
        """执行一笔真实委托；市价/无价委托必须使用真实行情完成风控估价。

        risk_checked=True 表示**调用方已完成风控**（当前仅 SignalRouter 的下单路径，
        它在 route() 里先跑风控再调本方法）。此时本方法跳过二次校验，只做价格估价——
        P0-2：此前两处都校验，同一笔委托消耗两倍频率窗口与日额度，正常单被提前误拦。

        默认 False：MCP 工具 / 批量等直连路径仍强制风控，Mandatory Risk 不因此被削弱。
        """
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
        # 估价无论是否 risk_checked 都要做：市价单需把真实最新价作为保护价送柜台
        # （P0-11），价格传 0 会被判废单。
        risk_price, price_error = await self._risk_price(bridge, code, price)
        if risk_price is None:
            self._audit("order.rejected", code, params, price_error)
            return {"ok": False, "reason": price_error}
        if not risk_checked:
            allowed, reason = checker.check_order(
                code, risk_price, volume, direction, price_type,
                # P0-5：直连路径（MCP 工具/批量）都是真实下单 → 要求账户快照就绪，
                # 避免用演示级总资产放行真单。
                require_account=True)
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
        if isinstance(result, dict):
            # 关键正确性修复（P0-12）：以网关返回的 ok 为准。旧实现读取不存在的
            # `code` 键（cancel_order 根本不返回 code），导致撤单永远被判成功。
            # 仅在网关未显式给出 ok 时，才回退到 code==0 的兼容判定。
            if "ok" not in result:
                result["ok"] = result.get("code", 0) == 0
        # ★ 审计必须写在**判定之后**（R25）：此前先 `_audit(action, ..., "ok")` 再判结果，
        #   撤单失败时 audit_log 里仍留一条「成功」记录 —— 合规/追溯直接失真。
        if isinstance(result, dict) and result.get("ok"):
            self._audit(action, order_id, {}, "ok")
        else:
            self._audit(f"{action}.failed", order_id, {}, str(result)[:200])
        return result

    async def cancel_order_price(self, bridge, order_id: str, deviation: float = 0.01,
                                 *, action: str = "order.cancel_price") -> dict:
        # ★ 能力探测（R25）：`XTQuantGateway` 与 `BridgeAdapter` 都**没有**实现
        #   `cancel_order_price`（只有 `xtquant_client/base.py` 抽象层声明过）。
        #   直接调用会 `AttributeError` ⇒ 全局兜底 500「服务器内部错误」，
        #   真相却是「当前适配器不支持超价撤单」。这里给出明确出路。
        gw = getattr(bridge, "gateway", None)
        if gw is None or not hasattr(gw, "cancel_order_price"):
            return {"ok": False,
                    "reason": "当前券商适配器不支持超价撤单（cancel_order_price 未实现）"}
        result = await bridge.call_locked(gw.cancel_order_price, order_id, deviation)
        if isinstance(result, dict):
            # 与 cancel_order 同一套判定（R25 修复）：优先用网关显式给出的 `ok`。
            # 旧实现写 `result.get("code", 0) == 0`，而撤单类网关**不返回 code**
            # ⇒ 恒为 True ⇒ 撤单失败也被覆写成成功。
            if "ok" not in result:
                result["ok"] = result.get("code", 0) == 0
        if isinstance(result, dict) and result.get("ok"):
            self._audit(action, order_id, {"deviation": deviation}, "ok")
        else:
            self._audit(f"{action}.failed", order_id, {"deviation": deviation},
                        str(result)[:200])
        return result


def get_execution_service() -> ExecutionService:
    from core.state import state
    return ExecutionService(risk=state.risk, db=state.db)


__all__ = ["ExecutionService", "get_execution_service"]
