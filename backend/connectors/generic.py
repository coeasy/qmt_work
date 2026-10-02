"""通用连接器：由 Dialect × Transport 组合而成（V4 §3）。

这是 Phase 1 的核心产物 —— **客户端连接器不再手写一个类**，而是：

    GenericConnector(dialect=XtQuantV1(), transport=InProcessTransport(adapter))
    GenericConnector(dialect=BigQmtV1(),   transport=FileSignalTransport(dir))

新增客户端 = 新增 1 个 dialect；新增部署形态 = 新增 1 个 transport。
组合表里的每个格子都不需要专属类。
"""
from __future__ import annotations

import logging
from typing import Any

from core.clock import now_iso  # 时间戳唯一实现（V11 R8 护栏：禁止内联生产者）

from .canonicalize import account_snapshot  # noqa: F401 (re-export for callers)
from .dialects import Ops, UnsupportedOp, is_write_op
from .ports import (
    AccountSnapshot,
    CanonicalEvent,
    Capability,
    CapabilityLevel,
    CapabilitySet,
    ConnectorDescriptor,
    ConnectorError,
    EventSemantics,
    EventSemanticsSpec,
    InstrumentId,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
)
from .transport import MAX_TIMEOUT, TransportError, WireRequest

log = logging.getLogger("qmt_work.connectors")

#: 注入函数名 → 其所能支撑的 canonical 能力（用于运行时真探，UNKNOWN 优先）。
#: 仅当对端**实际捕获到**该函数时才算 SUPPORTED，否则诚实 UNKNOWN ——
#: 这正是大 QMT 桥接的教训（"未捕获函数"被误报成"终端没有该接口"会掩盖真 bug）。
_FUNC_TO_CAPS: dict[str, tuple[str, ...]] = {
    "passorder": ("trade", "trade_submit"),
    "cancel": ("trade_cancel",),
    # ★ ``positions``（复数）——必须与能力词表一致。
    #   能力名只在两处定义：``registry._DEFAULT_CAPS`` 与 ``qmt.QmtConnector.descriptor``,
    #   两处都用复数 ``positions``。这里曾写单数 ``position`` ⇒ probe 会**同时**输出
    #   ``position``(SUPPORTED) 与 ``positions``(UNKNOWN) 两条互相矛盾的能力，
    #   前端按声明名查 ``positions`` 会看到「不支持」，而持仓明明是通的。
    "get_trade_detail_data": ("account", "positions", "order", "deal"),
    "get_full_tick": ("quote", "realtime"),
    "get_market_data": ("kline",),
    "get_instrument_detail": ("instrument",),
    # ★ 日历要**两个注入名都列**（少一个就是反向假阴性）：
    #   - ``get_trading_calendar``：miniQMT / xtdata 侧的命名；
    #   - ``get_trading_dates``  ：大 QMT agent（ContextInfo）实际捕获的命名
    #     （见 ``agent_bigqmt/qmt_api.py`` 的 ``_pick("get_trading_dates")``）。
    #   只写前者时，大 QMT 明明捕获到了日历函数，probe 仍报 UNKNOWN
    #   —— 正是本模块注释警告的「未捕获函数被误报成终端没有接口」。
    "get_trading_calendar": ("calendar",),
    "get_trading_dates": ("calendar",),
}


class GenericConnector:
    """Dialect（说什么）× Transport（怎么送）的装配结果。

    实现 12 个 canonical port 中的 11 个查/写面；第 12 个（``EventPort``）由
    具体连接的 ``events`` 属性提供（不同传输的事件来源语义不同）。
    """

    def __init__(self, dialect: Any, transport: Any, *, connector_id: str = "",
                 name: str = "", capabilities: tuple[str, ...] = ()):
        self.dialect = dialect
        self.transport = transport
        self.descriptor = ConnectorDescriptor(
            id=connector_id or f"{dialect.dialect_id}@{transport.transport_id}",
            name=name or f"{dialect.dialect_id} via {transport.transport_id}",
            version="1.0.0",
            capabilities=tuple(capabilities),
            metadata={
                "dialect": dialect.dialect_id,
                "transport": transport.transport_id,
            },
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        starter = getattr(self.transport, "start", None)
        if starter is not None:
            await starter()

    async def close(self) -> None:
        await self.transport.close()

    def is_connected(self) -> bool:
        """O(1) 探活：只做本地状态/轻探测，禁止 IO（HealthPort 契约）。"""
        probe = getattr(self.transport, "is_alive", None)
        if probe is None:
            return True  # 无状态传输（如文件桥）由 events/health 面负责真实判活
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # 内部：一次 canonical 调用
    # ------------------------------------------------------------------
    async def _call(self, op: str, payload: dict[str, Any] | None = None,
                    timeout: float = MAX_TIMEOUT) -> Any:
        dialect_op = self.dialect.op_of(op)
        params = self.dialect.prepare(op, payload or {})
        request = WireRequest(op=dialect_op, params=params, timeout=timeout,
                              write=is_write_op(op))
        response = await self.transport.invoke(request)
        return _unwrap(response, self.dialect, op)

    # ------------------------------------------------------------------
    # ExecutionPort
    # ------------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> OrderSnapshot:
        request.validate()
        payload = {
            "code": request.instrument.canonical,
            "side": request.side,
            "order_type": request.order_type,
            "price": request.price,
            "quantity": request.quantity,
            "account_id": request.account_id,
            "client_order_id": request.client_order_id,
            "strategy_name": request.strategy_name,
            "remark": request.remark,
            # 下单级账户/标的类型（stock/etf/future/option/credit）。方言负责把它
            # 翻成自己 wire 上的键名（大 QMT = account_type）；不支持的方言忽略即可。
            "account_type": request.account_type,
        }
        snap = await self._call(Ops.PLACE_ORDER, payload)
        if snap is None or not snap.broker_order_id:
            # 零 mock：拿不到柜台委托号 ⇒ 不能声称受理。
            raise ConnectorError(f"下单未取得柜台委托号（op={Ops.PLACE_ORDER}）")
        return snap

    async def cancel_order(self, order_id: str) -> OrderSnapshot:
        if not order_id:
            raise ValueError("order_id is required")
        return await self._call(Ops.CANCEL_ORDER, {"order_id": order_id})

    async def get_orders(self) -> list[OrderSnapshot]:
        return list(await self._call(Ops.GET_ORDERS) or [])

    async def get_deals(self) -> list[OrderSnapshot]:
        return list(await self._call(Ops.GET_DEALS) or [])

    # ------------------------------------------------------------------
    # AccountPort / PositionsPort
    # ------------------------------------------------------------------
    async def get_account(self) -> AccountSnapshot:
        snap = await self._call(Ops.GET_ACCOUNT)
        return snap if isinstance(snap, AccountSnapshot) else account_snapshot(snap or {})

    async def get_cash(self) -> AccountSnapshot:
        snap = await self._call(Ops.GET_CASH)
        return snap if isinstance(snap, AccountSnapshot) else account_snapshot(snap or {})

    async def get_positions(self, symbol: str | None = None) -> list[PositionSnapshot]:
        return list(await self._call(Ops.GET_POSITIONS, {"symbol": symbol}) or [])

    # ------------------------------------------------------------------
    # MarketDataPort / QuoteFeedPort
    # ------------------------------------------------------------------
    async def get_quote(self, code: str) -> dict[str, Any]:
        return dict(await self._call(Ops.GET_QUOTE, {"code": code}) or {})

    async def get_kline(self, code: str, period: str, count: int,
                        adjust: str = "", start: str = "",
                        end: str = "") -> list[dict[str, Any]]:
        return list(await self._call(Ops.GET_KLINE, {
            "code": code, "period": period, "count": count,
            "adjust": adjust, "start": start, "end": end,
        }) or [])

    async def get_full_tick(self, codes: list[str]) -> dict[str, Any]:
        return dict(await self._call(Ops.GET_FULL_TICK, {"codes": list(codes)}) or {})

    async def subscribe_quote(self, codes: list[str], on_tick) -> None:
        """订阅实时行情。

        ★ 跨进程传输（文件/Redis/ZMQ）**不支持回调式订阅**：``on_tick`` 是进程内的
        Python 可调用对象，无法序列化过线。这类传输的「实时」走 **SUB_QUOTE → agent
        轮询转发**（tick 变化写 events.ndjson，type=quote，经 EventPort 消费），
        或直接用 EventPort/轮询查询 —— 此处对回调形态给出清晰的不支持原因，
        而不是让 JSON 序列化崩溃或返回 400。
        进程内直连（miniQMT）才把回调真正传下去。
        """
        transport = self.transport
        if not getattr(transport, "supports_callback", False):
            raise UnsupportedOp(
                "本传输（%s）不支持回调式实时订阅：on_tick 无法跨进程传递。"
                "请改用 EventPort（event_semantics()/stream_events()）或轮询查询；"
                "miniQMT 进程内直连支持回调订阅。" % transport.transport_id)
        await self._call(Ops.SUBSCRIBE_QUOTE,
                         {"codes": list(codes), "on_tick": on_tick})

    async def subscribe_quote_polling(self, codes: list[str]) -> Any:
        """订阅行情（**轮询转发形态**：只把 codes 告诉对端，不传回调）。

        与 ``subscribe_quote`` 的关系与分工
        -----------------------------------
        ``subscribe_quote`` 实现的是 ``QuotePort`` 的**回调契约**；跨进程传输
        （文件/Redis/ZMQ）满足不了它（回调对象不可序列化）⇒ 如实抛
        ``UnsupportedOp``（判据是 ``transport.supports_callback``）。

        但「把 codes 交给 agent、由它轮询转发 tick 变化」这件事**只有跨进程传输
        才需要**，且它是大 QMT 行情链的**唯一**入口：agent 侧 ``do_subscribe``
        把这些 code 记进状态，``quote_events``（由终端 handlebar 驱动）才发现变化、
        写进 ``events.ndjson``。少了这一步，agent 永远不知道自己该转发什么，
        事件泵每秒空转一次，界面上的价格就**永远停在订阅那一刻的种子值**
        —— 而连接状态、健康检查、`subscribe_quote` 调用日志全都是绿的。

        因此本方法与回调契约**并存**而非替代：异步、可 await、失败会抛
        （调用方据此重试），由桥在自己的泵循环里对账下发（见
        ``BigQmtBridge._reconcile_quote_subscriptions``）。

        幂等：整集下发，agent 侧是集合 update，重复调用无副作用。
        """
        return await self._call(
            Ops.SUBSCRIBE_QUOTE, {"codes": [c for c in (codes or []) if c]})

    # ------------------------------------------------------------------
    # InstrumentPort / CalendarPort / HealthPort
    # ------------------------------------------------------------------
    async def get_instrument_detail(self, code: str) -> dict[str, Any]:
        return dict(await self._call(Ops.GET_INSTRUMENT_DETAIL, {"code": code}) or {})

    async def get_stock_list(self, sector: str = "沪深A股") -> list[dict[str, Any]]:
        return list(await self._call(Ops.GET_STOCK_LIST, {"sector": sector}) or [])

    async def get_sector_list(self) -> list[str]:
        return list(await self._call(Ops.GET_SECTOR_LIST) or [])

    async def get_trading_calendar(self, start: str = "", end: str = "") -> list[str]:
        return list(await self._call(
            Ops.GET_TRADING_CALENDAR, {"start": start, "end": end}) or [])

    async def test_connection(self, timeout: float = MAX_TIMEOUT) -> dict[str, Any]:
        """握手 / 能力探测。

        ``timeout`` 是给**探活**用的短预算：大 QMT 桥的空闲存活探测必须能在
        几秒内得出结论（否则每次探活都会把事件泵拖住），而它不能靠改全局
        ``MAX_TIMEOUT`` 实现 —— 业务调用的 30s 上界是另一条独立的纪律。
        """
        return dict(await self._call(Ops.TEST_CONNECTION, None, timeout=timeout) or {})

    def capabilities(self) -> tuple[str, ...]:
        return tuple(self.descriptor.capabilities)

    def supported_ops(self) -> list[str]:
        """本方言能翻译的 canonical op 清单（**诊断面用**，不参与业务判定）。

        由 ``dialect.supported_ops()`` 派生（各方言从自己的 op 映射表产出，
        绝不手抄）—— 排障时把它和 agent 上报的 ``actions`` 并排看：
        数量对不上就是「外部端与 agent 的契约漂移」，而不是「终端没这个能力」。
        """
        ops = getattr(self.dialect, "supported_ops", None)
        return list(ops() if callable(ops) else ())

    # ------------------------------------------------------------------
    # CapabilityProbePort / EventPort
    # ------------------------------------------------------------------
    async def probe_capabilities(self) -> CapabilitySet:
        """运行时能力协商（V4 §6）—— **真正探测**，不是回显静态声明。

        流程：
        1. 调 ``test_connection()`` 拿对端上报的注入函数清单（文件/Redis/ZMQ 走
           PROBE；进程内走 ``adapter.test_connection``）；
        2. 按 ``_FUNC_TO_CAPS`` 把**实际捕获到的函数**映射到能力 → SUPPORTED；
        3. 实时能力由事件语义推导（PUSH→SUPPORTED，POLL_DIFF/PUSH_WITH_GAP→DEGRADED）；
        4. 真探已发生但某能力无对应函数支撑 ⇒ 诚实 UNKNOWN，**绝不臆造 SUPPORTED**。

        ★ 这正是大 QMT 桥接的教训（"未捕获函数"被误报成"终端没有接口"会掩盖真 bug）。
        """
        captured: set[str] = set()
        try:
            reply = await self.test_connection() or {}
            funcs = reply.get("captured") or (
                (reply.get("agent") or {}).get("funcs"))
            if funcs:
                captured = set(funcs)
        except Exception:  # noqa: BLE001  探测失败不应让 probe 整体崩
            log.warning("[connectors] probe 探测失败，退化为静态声明")

        # 事实验证：被捕获函数支撑的能力 → SUPPORTED。
        backed: dict[str, str] = {}
        for fn, caps in _FUNC_TO_CAPS.items():
            if fn in captured:
                for c in caps:
                    backed[c] = fn

        # 实时能力由事件语义推导。
        sem = self.event_semantics()
        realtime_level = {
            EventSemantics.PUSH: CapabilityLevel.SUPPORTED,
            EventSemantics.POLL_DIFF: CapabilityLevel.DEGRADED,
            EventSemantics.PUSH_WITH_GAP: CapabilityLevel.DEGRADED,
            EventSemantics.NONE: CapabilityLevel.UNKNOWN,
        }.get(sem.semantics, CapabilityLevel.UNKNOWN)

        declared = set(self.capabilities())
        names = set(declared) | set(backed) | {"realtime"}
        out: list[Capability] = []
        for name in sorted(names):
            if name == "realtime":
                out.append(Capability(
                    name="realtime", level=realtime_level,
                    reason=f"事件语义={sem.semantics.value}",
                    probed_at=now_iso()))
                continue
            if name in backed:
                out.append(Capability(
                    name=name, level=CapabilityLevel.SUPPORTED,
                    reason=f"实调: 捕获到 {backed[name]}", probed_at=now_iso()))
                continue
            if captured:
                # 真探已发生但该函数未捕获 ⇒ 诚实 UNKNOWN（不臆造 SUPPORTED）。
                out.append(Capability(
                    name=name, level=CapabilityLevel.UNKNOWN,
                    reason=f"未捕获到支撑函数（声明={'是' if name in declared else '否'}）",
                    probed_at=now_iso()))
                continue
            # 无 captured 清单（进程内 mini）：退化为静态声明。
            out.append(Capability(
                name=name,
                level=CapabilityLevel.SUPPORTED if name in declared
                else CapabilityLevel.UNKNOWN,
                reason="静态声明" if name in declared else "未经实调验证",
                probed_at=now_iso()))
        return CapabilitySet(capabilities=tuple(out), probed_at=now_iso())


    def event_semantics(self) -> EventSemanticsSpec:
        """传输层若提供事件流语义则用它，否则声明 NONE（让上层自行轮询）。

        ``NONE`` 是**诚实的答案**：假装没有能力会让上层把「暂时没事件」
        误判成「成功也没有事件」，从而放过真实的成交回报。
        """
        spec = getattr(self.transport, "event_semantics", None)
        if callable(spec):
            return spec()
        return EventSemanticsSpec(semantics=EventSemantics.NONE)

    def stream_events(self) -> list[CanonicalEvent]:
        tail = getattr(self.transport, "stream_events", None)
        if tail is None:
            return []
        return list(tail() or [])


def _unwrap(response, dialect, op: str) -> Any:
    """wire 响应 → canonical 模型；失败按 error_type 重建异常。"""
    if not response.ok:
        raise _rebuild(response.error_type, response.error)
    try:
        return dialect.parse(op, response.result)
    except Exception as exc:  # noqa: BLE001  parse 失败＝方言 drift，必须显式炸出
        raise ConnectorError(f"{op} 方言解析失败: {exc}") from exc


def _rebuild(error_type: str, message: str) -> Exception:
    """按 error_type 重建跨进程丢失的异常类型。

    刻意「查无此类型即降级为 ConnectorError」而不是抛 KeyError：
    新增异常类型时旧版对端仍能工作（向后兼容），但消息完整保留（真因不丢）。

    ★ 这张表是**两端 error_type 词表的唯一对账点**（勿只改一侧）：
      * 生产端：``XtQuantV1.classify_error`` / ``BigQmtV1.classify_error``
        （进程内异常 → wire error_type）；
      * agent 端：``agent_bigqmt/qmt_api.py`` 的 ``ActionError``（直接抛字面量）。

      映射目标是**HTTP 归因**，不是类型好看：
        - 券商可用但拒绝（拒单 / TTL 过期）→ ``BrokerError`` → 400 + 真因；
        - 连不上（未连接 / SDK 缺失 / 超时未应答）→ 相应的 ``...NotConnected``
          / ``TransportError`` → 503 + 「去连接券商」引导。
      映射错了会把「拒单」显示成「券商不可用」（用户去重连，白折腾），
      或把「agent 没运行」显示成「请求非法」（用户去改参数，同样白折腾）。
    """
    try:
        from xtquant_client.base import (
            BrokerError,
            BrokerNotConnectedError,
            BrokerSDKError,
        )
    except Exception:  # noqa: BLE001
        return ConnectorError(message or error_type)
    table = {
        # —— 券商可用但拒绝：400 + 真因 ——
        "BrokerError": BrokerError,
        # 大 QMT agent 的拒单字面量（trading_enabled=false / 参数非法 / 方向缺失）。
        "Rejected": BrokerError,
        # 本地 TTL 拦截幽灵单（agent 与 transport 两侧都会产出）。
        "Expired": BrokerError,
        # —— 连不上 / 能力缺失：503 + 引导 ——
        "BrokerNotConnected": BrokerNotConnectedError,
        "BrokerSDKError": BrokerSDKError,
        # 对端收到请求但未在预算内应答 ⇒ 与「不可达」同归因，绝不降级成 400。
        "Timeout": TransportError,
        "Unsupported": UnsupportedOp,
        # 兜底：仍属券商层（ConnectorError ⊂ BrokerError），不会是 500。
        "ConnectorError": ConnectorError,
    }
    cls = table.get(error_type or "", ConnectorError)
    # ★ ``BrokerSDKError`` 的签名是 ``(sdk, extra="")``，**不是** ``(message)``。
    #   直接 ``BrokerSDKError(msg)`` 会把真因塞进 sdk 位，产出
    #   「缺少券商 SDK：<真因>。请在…安装（参见券商文档）…」——真因被包进一个
    #   语义完全错误的模板里（上面那个 except 兜不住，因为单参能构造成功）。
    #   这里显式走命名参数：文案里必须完整保留对端上报的原始原因。
    if cls is BrokerSDKError:
        return BrokerSDKError("券商侧所需函数/能力", message or error_type)
    try:
        return cls(message or error_type)
    except Exception:  # noqa: BLE001  构造签名差异时退回通用异常，消息不丢
        return ConnectorError(f"{error_type}: {message}")


__all__ = ["GenericConnector", "InstrumentId", "TransportError"]
