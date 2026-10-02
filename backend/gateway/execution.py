"""统一真实交易执行核心（Phase 3）。

所有 REST/MCP/批量/策略入口都应先把委托意图交给这里，再由这里完成真实行情
估价、风控、加锁调用与审计。该模块不生成价格，也不提供模拟交易旁路；paper
模式由上层 SignalRouter 明确选择，不得伪装成 live 成交。
"""
from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Optional

from core.quote_fields import pick_order_ref_price

log = logging.getLogger("qmt_work.gateway.execution")

#: V4 Phase 0-c **灰度接线开关**：0=走既有 bridge 路径（默认，行为完全不变）；
#: 1=走 canonical 端口路径（SignalRouter → ExecutionPort → Dialect×Transport）。
#: 只接受这两个值：任何拼写错误都会被当作「未启用」而不是悄悄换一条路径。
_QMT_USE_PORTS = os.environ.get("QMT_USE_PORTS", "0").strip() in ("1", "true", "True")


def _split_instrument(code: str):
    """``600036.SH`` → InstrumentId(code=600036, exchange=SH)。"""
    from connectors.ports import InstrumentId

    base, _, ex = (code or "").partition(".")
    return InstrumentId(code=base, exchange=ex.upper())


def _account_type_arg(gateway, account_type: str) -> tuple:
    """``account_type`` → 追加位置实参元组（网关不支持时返回空元组）。

    ★ 为什么必须探测而不是无条件传（R19 第 1 轮）：
      走**旧直连路径**（``QMT_USE_PORTS=0``，默认值）时
      ``bridge.gateway.place_order`` 可能是 **mini 适配器**
      （``xtquant_client/xtp/trading.py``），它底层调 ``trader.order_stock``，
      **没有 ``opAccountType`` 形参**，信用/融资语义由连接级 ``account_type``
      派生。无条件追加第 8 个位置实参 → mini 连接**下单必 TypeError**。

      mini 侧「收下却不用」正是本项目明确反对的形态（见
      ``tests/test_bigqmt_bridge_face.py::_ACCEPTED_MINI_WIDENINGS`` 的注释），
      所以这里选择「按签名投递」：只有真正声明该形参的网关（大 QMT 网关）才收到。

      探测结果按函数对象缓存，避免每单重复 ``inspect.signature``。
    """
    if not account_type:
        return ()
    fn = getattr(gateway, "place_order", None)
    if fn is None:
        return ()
    cached = _ACCOUNT_TYPE_SUPPORT.get(fn)
    if cached is None:
        try:
            cached = "account_type" in inspect.signature(fn).parameters
        except (TypeError, ValueError):  # 内建/装饰器不可内省 → 保守不传
            cached = False
        _ACCOUNT_TYPE_SUPPORT[fn] = cached
    return (account_type,) if cached else ()


#: 缓存：网关 place_order 是否声明 ``account_type`` 形参（见 _account_type_arg）。
_ACCOUNT_TYPE_SUPPORT: dict = {}


def _port_for_bridge(bridge):
    """灰度期内把 bridge 提升为 canonical ExecutionPort；开关关闭时返回 None。

    提升是**纯适配**：转发给同一个 adapter 的同一个方法，不新增任何 SDK 调用，
    也不改变调用顺序。

    ★ 开关为 1 时**禁止静默回退**：gateway 缺失就抛错。
    「管理员以为在跑端口层、实际跑的是旧路径」是一种特别坏的假绿灯 ——
    它会让接线验证的结论失真（该项目已两次栽在绿灯被另一个 bug 遮出来）。
    要恢复旧路径，只有显式把 ``QMT_USE_PORTS`` 置回 0 这一条路。
    """
    if not _QMT_USE_PORTS:
        return None
    gateway = getattr(bridge, "gateway", None)
    if gateway is None:
        from connectors.ports import ConnectorError

        raise ConnectorError(
            "QMT_USE_PORTS=1 但该 bridge 未提供 gateway —— 端口层无法装配。"
            "若仍需旧路径，请把环境变量 QMT_USE_PORTS 置为 0")
    try:
        from connectors.dialects import get_dialect
        from connectors.generic import GenericConnector
        from connectors.transports import InProcessTransport

        dialect = get_dialect("xtquant.v1")
        write_ops = frozenset(getattr(dialect, "write_ops", ()) or ())
        transport = InProcessTransport(gateway, bridge=bridge,
                                       write_ops=write_ops or None)
        return GenericConnector(dialect=dialect, transport=transport,
                                connector_id="qmt.mini")
    except Exception:  # noqa: BLE001
        # 装配失败必须显式失败（不能静默降级 —— 那正是假绿灯的来源）。
        log.exception("[ports] 端口装配失败")
        raise


def _make_order_request(code: str, direction: str, price_type: str, price: float,
                        volume: int, strategy_name: str, remark: str,
                        account_type: str = ""):
    """构造 canonical 下单请求（price_type/direction 已在更上游校验）。"""
    from connectors.ports import OrderRequest

    return OrderRequest(
        instrument=_split_instrument(code),
        side=str(direction or "").lower(),
        order_type=str(price_type or "limit").lower(),
        price=float(price or 0.0),
        quantity=int(volume or 0),
        strategy_name=strategy_name or "",
        remark=remark or "",
        # 幂等锚点：上层若已生成，应在此透传；留空则由 SingleFlight 的第一道防线兜住。
        client_order_id="",
        # 下单级账户/标的类型（stock/etf/future/option/credit）；空串 = 不覆盖。
        account_type=str(account_type or ""),
    )


def _snapshot_to_legacy(snap) -> dict:
    """OrderSnapshot → 既有下游期望的下单回执 dict。

    刻意保留 ``code: 0`` 兼容位：下游 WAL/审计/emit 都以此为成功判据，
    灰度期**不改变任何下游契约**，只是把来源换成 canonical 快照。
    """
    data = dict(snap.raw or {})
    data.setdefault("order_id", snap.broker_order_id)
    data["ok"] = bool(snap.accepted)
    data["status"] = snap.status
    data["code"] = 0
    if snap.client_order_id:
        data["client_order_id"] = snap.client_order_id
    return data


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
        account_type: str = "",
        *,
        risk=None,
        audit_action: str = "order.submitted",
        risk_checked: bool = False,
    ) -> dict:
        """执行一笔真实委托；市价/无价委托必须使用真实行情完成风控估价。

        account_type：下单级账户/标的类型（stock/etf/future/option/credit）。
        透传到 connector → 方言 → agent 的 passorder ``opAccountType``；空串表示
        不覆盖（agent 侧 ``default_account_type`` / stock 兜底）。**不是**连接级
        ``ConnectionConfig.account_type``（那是账户归类，用于连接发现与展示）。

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
            "account_type": account_type,
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
        port = _port_for_bridge(bridge)
        if port is not None:
            # V4 Phase 0-c：经 canonical ExecutionPort 下单（灰度）。
            # 估价/风控已在上方完成，此处只是把「送达」交给端口层。
            request = _make_order_request(code, direction, price_type, risk_price,
                                          volume, strategy_name, remark,
                                          account_type)
            try:
                snap = await port.place_order(request)
            except Exception as exc:  # noqa: BLE001
                log.error("[ports] 下单失败（order=%s）: %s", code, exc)
                self._audit("order.failed", code, params, f"[ports] {exc}")
                raise
            result = _snapshot_to_legacy(snap)
            result.setdefault("order_id", snap.broker_order_id)
            self._audit(audit_action, code, params,
                        f"[ports] order_id={snap.broker_order_id}")
            return result

        result: Any = await bridge.call_locked(
            bridge.gateway.place_order, code, direction, price_type, risk_price,
            volume, strategy_name, remark, *_account_type_arg(bridge.gateway, account_type))
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
        port = _port_for_bridge(bridge)
        if port is not None:
            snap = await port.cancel_order(order_id)
            result = {"ok": bool(snap.accepted), "order_id": snap.broker_order_id,
                      "status": snap.status, "code": 0}
            self._audit(action if result["ok"] else f"{action}.failed",
                        order_id, {}, f"[ports] status={snap.status}")
            return result
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
