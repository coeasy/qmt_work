"""QMT/XTQuant implementation of the canonical connector port."""
from __future__ import annotations

from typing import Any

from xtquant_client.base import BrokerAdapter, BrokerError
from xtquant_client.gateway import XTQuantBridge

from .canonicalize import (
    account_snapshot,
    order_snapshot,
    position_snapshot,
    trade_snapshot,
)
from .ports import (
    AccountSnapshot,
    ConnectorDescriptor,
    ConnectorError,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
)

#: client_order_id 透传标记（P0-e）。
#: 柜台 remark 字段是唯一能从平台侧带到券商侧的自由文本，用它承载幂等键，
#: 使对账与「重复提交」判定有第二锚点（第一道在 SignalRouter 的 single_flight）。
#: 格式刻意简短：部分券商对 remark 有长度限制。
_CID_PREFIX = "cid="


def _with_cid(remark: str, client_order_id: str) -> str:
    """把 client_order_id 追加进 remark；幂等键缺失时不污染原文。"""
    if not client_order_id:
        return remark or ""
    tag = f"{_CID_PREFIX}{client_order_id}"
    if not remark:
        return tag
    if tag in remark:  # 调用方已自行拼接，不重复
        return remark
    return f"{remark}|{tag}"


def _reraise_preserving_taxonomy(exc: BaseException, context: str = "") -> None:
    """把券商侧异常统一到连接器异常层级，**但不抹掉 503/400 的区分**。

    ★ 曾经的写法是 ``raise ConnectorError(str(exc)) from exc``，那是**有损**的：
      ``BrokerNotConnectedError`` / ``BrokerSDKError`` 被压成通用的
      ``ConnectorError`` 之后，``gateway.signal_router._live`` 的
      ``isinstance(exc, (BrokerNotConnectedError, BrokerSDKError))`` 判定
      就恒为 False ⇒ 「券商客户端没连上」被报成 **400「风控/执行拒绝」**，
      用户被引导去改下单参数，而正确动作是去连接/登录券商客户端。
      契约（README / trade.py）写得很清楚：**不可用 → 503；可用但被拒 → 400**。

    规则：已经是 ``BrokerError`` 的**原样重抛**（保留子类身份与全部诊断）；
      仅对非券商层的异常（``OSError`` 等）做一次包裹，``context`` 前缀不丢。
    """
    if isinstance(exc, BrokerError):
        raise exc
    raise ConnectorError(f"{context}{exc}" if context else str(exc)) from exc


class QmtConnector:
    """Adapter around the real XTQuant adapter and its thread-safe bridge.

    No SDK calls are made until ``start``.  This keeps QMT optional for research,
    backtest and paper workflows while preserving the existing bridge semantics.

    ★ 线程模型（P0-b）：所有 SDK 调用都经 ``XTQuantBridge`` offload 到线程池，
      端口层**没有任何同步 SDK 调用**：

      * 读操作（查询类）走 ``bridge.call``      —— 并发池，互不阻塞；
      * 写操作（下单/撤单/订阅）走 ``call_locked`` —— 串行化 + 30s 硬超时。

      这样接进 asyncio 之后不会出现「同步 SDK 调用占用事件循环」的全站卡顿。
    """

    def __init__(self, adapter: BrokerAdapter):
        self.adapter = adapter
        self.bridge = XTQuantBridge(adapter)
        self.descriptor = ConnectorDescriptor(
            id="qmt",
            name="QMT / XTQuant",
            version=getattr(adapter, "client_version", "") or "unknown",
            capabilities=("quote", "kline", "trade", "account", "positions", "realtime"),
            optional_sdk=getattr(adapter, "sdk_required", "xtquant") or "xtquant",
            account_types=tuple(getattr(adapter, "supported_account_types", ["STOCK"])),
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        try:
            await self.bridge.start()
        except Exception as exc:  # noqa: BLE001
            # 同样保留原有层级：启动失败最常见的原因就是「客户端未登录」
            # （BrokerNotConnectedError）或「SDK 未安装」（BrokerSDKError），
            # 健康监控按这两类决定退避与提示，压成通用 ConnectorError 会丢语义。
            _reraise_preserving_taxonomy(exc, "QMT connector start failed: ")

    async def close(self) -> None:
        await self.bridge.stop()

    def is_connected(self) -> bool:
        """O(1) 探活：不得做任何 IO（HealthPort 契约，supervisor 周期调用）。"""
        try:
            return bool(self.adapter.is_connected())
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> OrderSnapshot:
        request.validate()
        remark = _with_cid(request.remark, request.client_order_id)
        try:
            raw = await self.bridge.call_locked(
                self.adapter.place_order,
                request.instrument.canonical,
                request.side,
                request.order_type,
                request.price,
                request.quantity,
                request.strategy_name,
                remark,
            )
        except (BrokerError, OSError) as exc:
            # 零 mock：柜台拒单必须抛出，绝不返回一个「看起来像成功」的快照。
            _reraise_preserving_taxonomy(exc)
            raise AssertionError("unreachable")  # pragma: no cover
        snap = order_snapshot(raw, client_order_id=request.client_order_id)
        if snap.status == "unknown" and not snap.broker_order_id:
            # 下单回执里既无委托号也无状态 ⇒ 无法确认受理，如实告知上层去查单。
            raise ConnectorError(
                f"下单回执无法确认受理（无柜台委托号）: {raw!r}")
        return snap

    async def cancel_order(self, order_id: str) -> OrderSnapshot:
        if not order_id:
            raise ValueError("order_id is required")
        try:
            raw = await self.bridge.call_locked(self.adapter.cancel_order, order_id)
        except (BrokerError, OSError) as exc:
            _reraise_preserving_taxonomy(exc)
            raise AssertionError("unreachable")  # pragma: no cover
        return order_snapshot(raw)

    # ------------------------------------------------------------------
    # 查询（全部 async，均 offload 到 bridge.call 并发池）
    # ------------------------------------------------------------------
    async def get_orders(self) -> list[OrderSnapshot]:
        raws = await self.bridge.call(self.adapter.get_orders) or []
        return [order_snapshot(r) for r in raws]

    async def get_deals(self) -> list[OrderSnapshot]:
        raws = await self.bridge.call(self.adapter.get_deals) or []
        return [trade_snapshot(r) for r in raws]

    async def get_account(self) -> AccountSnapshot:
        raw = await self.bridge.call(self.adapter.get_account)
        return account_snapshot(raw or {})

    async def get_cash(self) -> AccountSnapshot:
        raw = await self.bridge.call(self.adapter.get_cash)
        return account_snapshot(raw or {})

    async def get_positions(self, symbol: str | None = None) -> list[PositionSnapshot]:
        raws = await self.bridge.call(self.adapter.get_positions, symbol) or []
        return [position_snapshot(r) for r in raws]

    async def get_quote(self, code: str) -> dict[str, Any]:
        return dict(await self.bridge.call(self.adapter.get_quote, code) or {})

    async def get_kline(self, code: str, period: str, count: int,
                        adjust: str = "", start: str = "",
                        end: str = "") -> list[dict[str, Any]]:
        """K 线。端口恢复了对日期区间的表达力（缺口 D4）。

        ★ **必须按关键字传参**，不能按位置。
          两个签名同名但**顺序不同**：
            端口   ``(code, period, count, adjust, start, end)``
            adapter ``(code, period, count, start, end, adjust)``
          按位置传（旧写法）时第 4 个实参在端口是 ``adjust``、在 adapter 是
          ``start`` —— 一旦任一签名调整顺序，``adjust="1d"`` 会被静默当成起始
          日期送进取数接口，返回一批错口径/错区间的 K 线且**不报任何错**。
          关键字传参把这个映射钉死。
        """
        return list(await self.bridge.call(
            self.adapter.get_kline, code, period, count,
            start=start, end=end, adjust=adjust) or [])

    async def get_full_tick(self, codes: list[str]) -> dict[str, Any]:
        return dict(await self.bridge.call(self.adapter.get_full_tick, codes) or {})

    async def subscribe_quote(self, codes: list[str], on_tick) -> None:
        await self.bridge.call_locked(self.adapter.subscribe_quote, codes, on_tick)

    async def get_instrument_detail(self, code: str) -> dict[str, Any]:
        return dict(await self.bridge.call(self.adapter.get_instrument_detail, code) or {})

    async def get_stock_list(self, sector: str = "沪深A股") -> list[dict[str, Any]]:
        return list(await self.bridge.call(self.adapter.get_stock_list, sector) or [])

    async def get_sector_list(self) -> list[str]:
        return list(await self.bridge.call(self.adapter.get_sector_list) or [])

    async def get_trading_calendar(self, start: str = "", end: str = "") -> list[str]:
        return list(await self.bridge.call(
            self.adapter.get_trading_calendar, start, end) or [])

    async def test_connection(self) -> dict[str, Any]:
        return dict(await self.bridge.call(self.adapter.test_connection) or {})

    def capabilities(self) -> tuple[str, ...]:
        return tuple(self.descriptor.capabilities)


__all__ = ["QmtConnector", "_CID_PREFIX", "_with_cid"]
