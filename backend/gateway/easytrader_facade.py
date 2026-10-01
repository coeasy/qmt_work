"""EasyTrader 风格兼容 façade（V4 §8）。

为什么放在 ``gateway/`` 而不是 ``connectors/``
---------------------------------------------
它是**编排层之上**的薄包装：下单必须经 ``SignalRouter.submit``（INV-1），
而 SignalRouter 就在 ``gateway/``。放进 ``connectors/`` 会造成 connectors → gateway
的反向依赖，把「方言边界」撕开一个口子。

它的边界（与 easytrader 的关键差异）
----------------------------------
1. **没有 ``user.trader`` 式的无痕逃逸口**。``.raw`` 属性返回的是**只读**的面。
   确需方言原生能力请走显式、可审计的路径（V4 §8 的受控逃生舱），这里是刻意的缺失：
   一个不能用来越过风控的兼容层，才是能放进平台的兼容层。
2. **查询必须显式给 connector**。没有 connector 就报错，绝不返回空列表 ——
   空列表会被上层当成「你没有持仓」，而真相是「还没接好」。
3. **返回值可以是空的，但空的原因必须说得出来**（``broker_unavailable`` / 真失败）。

迁移价值：``user.buy(code, price, amount)`` 这类旧脚本几乎可以无痛迁过来，
同时自动获得风控、幂等、TOTP、WAL 与审计。
"""
from __future__ import annotations

from typing import Any

from core.context import active_context_or_none


class FacadeError(RuntimeError):
    """façade 层拒绝执行（区别于 BrokerError：这是使用方式/配置问题）。"""


class EasyTraderFacade:
    """社区心智 + 平台不变量的折中面。"""

    def __init__(self, router=None, connector=None, broker_id: str = ""):
        self._router = router
        self._connector = connector
        self.broker_id = broker_id or ""

    # ------------------------------------------------------------------
    @property
    def router(self):
        if self._router is not None:
            return self._router
        # ★ 走 ``core.context`` 的规约访问器，**不**直接 import ``core.state.state``：
        #   单例是「进程里恰好有一个」的装配产物，而 ctx 是**显式**的当前上下文；
        #   路由层一律经 ``Depends(get_ctx)`` 取同一个 ctx，这里与它们同源。
        #   直接抓单例会被 ``scripts/check_appcontext.py`` 记成新增直接引用
        #   （V9 Phase 5 的收敛门禁），且让「装配完成」这件事变成隐式假设。
        ctx = active_context_or_none()
        router = getattr(ctx, "signal_router", None) if ctx is not None else None
        if router is None:
            raise FacadeError(
                "SignalRouter 未就绪（应用启动未完成）—— 下单必须经 SignalRouter "
                "（INV-1：唯一交易入口）。请显式传入 router 或稍后重试。")
        return router

    def connect(self, connector=None, *, broker_id: str = "") -> "EasyTraderFacade":
        """easytrader 的 ``connect()`` 同源VEL[: 这里是「接哪条连接器」。"""
        if connector is not None:
            self._connector = connector
        if broker_id:
            self.broker_id = broker_id
        return self

    def use(self, key: str, **options) -> "EasyTraderFacade":
        """easytrader 的 ``use('miniqmt')`` 同形态：按 connector key 装配。

        :param key: 如 ``qmt.mini`` / ``qmt.big.bridge.file``（见 registry.available_keys）
        """
        from connectors.registry import resolve

        self._connector = resolve(key, **options)
        return self

    @property
    def raw(self) -> Any:
        """**只读** adapter 句柄。提供它是为了排障，不是为了绕过风控。

        ★ 任何真实下单都必须回到 ``submit/buy/sell`` —— 直接调用 adapter 的下单
        方法会绕过 Mandatory Risk、幂等与审计，不在平台的支持范围内。
        """
        if self._connector is None:
            raise FacadeError("未连接任何连接器")
        return getattr(getattr(self._connector, "transport", None), "adapter", None)

    # ------------------------------------------------------------------
    # 交易（唯一入口）
    # ------------------------------------------------------------------
    async def _submit(self, code: str, side: str, amount: int, price: float = 0.0,
                      price_type: str = "limit", remark: str = "",
                      idempotency_key: str = "") -> dict:
        return await self.router.submit(
            code=code, side=side, volume=int(amount), price=float(price or 0),
            price_type=price_type, source="easytrader_facade",
            broker_id=self.broker_id, remark=remark,
            idempotency_key=idempotency_key)

    async def buy(self, code: str, price: float = 0.0, amount: int = 0,
                  **kwargs) -> dict:
        """买入。价 0 且未在 kwargs 指定市价 ⇒ 视为限价 0（会被风控单价校验拦下）。"""
        ptype = "market" if float(price or 0) <= 0 else "limit"
        return await self._submit(code, "buy", int(amount), float(price or 0), ptype,
                                  remark=kwargs.get("remark", ""),
                                  idempotency_key=kwargs.get("idempotency_key", ""))

    async def sell(self, code: str, price: float = 0.0, amount: int = 0,
                   **kwargs) -> dict:
        ptype = "market" if float(price or 0) <= 0 else "limit"
        return await self._submit(code, "sell", int(amount), float(price or 0), ptype,
                                  remark=kwargs.get("remark", ""),
                                  idempotency_key=kwargs.get("idempotency_key", ""))

    async def cancel_entrust(self, order_id: str) -> dict:
        """撤单。**必须**经 SignalRouter —— 撤单同样要过审计（R25 教训）。"""
        if not order_id:
            raise FacadeError("cancel_entrust 缺少 order_id")
        return await self.router.cancel(order_id)

    # ------------------------------------------------------------------
    # 查询（需 connector）
    # ------------------------------------------------------------------
    def _need_connector(self):
        if self._connector is None:
            raise FacadeError(
                "查询需要 connector：请用 facade.use('qmt.mini', ...) 或直接 "
                "connect(connector=...) 指定。零 mock：不会返回空列表冒充「无持仓」。")
        return self._connector

    async def position(self) -> list[dict]:
        rows = await self._need_connector().get_positions()
        return [{"security": p.instrument.canonical if p.instrument else "",
                 "stock_code": p.instrument.code if p.instrument else "",
                 "volume": p.quantity, "can_use_volume": p.available_quantity,
                 "open_price": p.average_cost, "market_value": p.market_value}
                for p in rows]

    async def balance(self) -> list[dict]:
        acc = await self._need_connector().get_account()
        return [{"account_id": acc.account_id, "cash": acc.cash,
                 "frozen_cash": acc.frozen, "total_asset": acc.assets}]

    async def today_entrusts(self) -> list[dict]:
        rows = await self._need_connector().get_orders()
        return [{"order_id": o.broker_order_id,
                 "order_sysid": o.broker_order_id,
                 "stock_code": o.instrument.code if o.instrument else "",
                 "order_volume": o.requested_quantity,
                 "traded_volume": o.filled_quantity,
                 "order_status": o.status,          # ★ SSOT 标准词表，不是方言整数
                 "raw_status": o.raw_status,
                 "client_order_id": o.client_order_id}
                for o in rows]

    async def today_trades(self) -> list[dict]:
        rows = await self._need_connector().get_deals()
        return [{"traded_id": t.broker_order_id,
                 "stock_code": t.instrument.code if t.instrument else "",
                 "traded_price": t.avg_price, "traded_volume": t.filled_quantity}
                for t in rows]


def use(key: str, **options) -> EasyTraderFacade:
    """easytrader 的 ``use('miniqmt')`` 同形态工厂。"""
    facade = EasyTraderFacade()
    return facade.use(key, **options)


__all__ = ["EasyTraderFacade", "FacadeError", "use"]
