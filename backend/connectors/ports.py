"""Canonical connector contracts.

The existing ``xtquant_client`` package remains the compatibility implementation.
These contracts are deliberately small: application services depend on canonical
commands and snapshots, while a connector owns SDK-specific mapping and lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

# ★ 状态词汇表刻意**复用**既有 SSOT，而不是在这里重新定义一组常量。
#   重复定义会重演「三处各自维护一套状态映射」的历史根因（见 order_status.py 头部）。
#   ``xtquant_client.order_status`` 零第三方依赖（只有 stdlib），导入成本可忽略。
from xtquant_client.base import BrokerError
from xtquant_client.order_status import UNKNOWN  # noqa: E402  (见上方说明)
from xtquant_client.order_status import is_terminal  # noqa: E402


class ConnectorError(BrokerError):
    """连接器无法完成一次真实操作。

    ★ 为什么继承 ``BrokerError`` 而不是 ``RuntimeError``（唯一接缝，勿改回）
      HTTP 层的归因规则**只认这一个根**：

      * ``app/routes/_common._call`` —— 只 ``except BrokerError`` → 503 信封；
      * ``gateway/signal_router._live`` —— 只按
        ``isinstance(exc, (BrokerNotConnectedError, BrokerSDKError))`` 判 503/400；
      * ``app/routes/account.py`` / ``trade.py`` —— 多处 ``except BrokerError``。

      本类若独立于该层级，任何从 bridge 冒出的连接器异常都会穿透到 FastAPI 兜底
      → **HTTP 500「服务器内部错误」**，真实原因（bridge_dir 两端不一致 / agent 未
      运行 / 方言不支持该 op）被完全吞掉，前端只能看到无信息量的 500。
      继承后归因立即正确：``_call`` → 503 + 真因；``_live`` → 400 + 真因。
    """


class ConnectorState(str, Enum):
    DISCONNECTED = "disconnected"
    STARTING = "starting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class ConnectorDescriptor:
    id: str
    name: str
    version: str
    capabilities: tuple[str, ...] = ()
    optional_sdk: str = ""
    account_types: tuple[str, ...] = ("STOCK",)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InstrumentId:
    code: str
    exchange: str = ""

    @property
    def canonical(self) -> str:
        return self.code if not self.exchange else f"{self.code}.{self.exchange}"


@dataclass(frozen=True)
class OrderRequest:
    instrument: InstrumentId
    side: str
    order_type: str = "limit"
    price: float = 0.0
    quantity: int = 0
    account_id: str = ""
    client_order_id: str = ""
    strategy_name: str = ""
    remark: str = ""

    def validate(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError(f"unsupported order side: {self.side}")
        if self.order_type not in {"limit", "market"}:
            raise ValueError(f"unsupported order type: {self.order_type}")
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.order_type == "limit" and self.price <= 0:
            raise ValueError("limit order price must be positive")


@dataclass(frozen=True)
class PositionSnapshot:
    """持仓快照。

    ★ ``name`` / ``raw`` 不是可选的装饰，是**契约的一部分**：
      * ``raw`` 保留方言原始行。大 QMT 桥的 ``_raw_of()`` 直接把 ``snap.raw``
        还给旧适配器面（``conn.adapter.get_positions`` → 账户看板 / 同步引擎 /
        ``enrich_positions``），下游按 dict 键取值（``p["code"]`` / ``p["avail"]``）。
        本字段缺失时 ``_raw_of`` 恒返回 ``{}``，持仓会**静默变成一串空对象**
        （不报错、不抛异常，只是数据没了 —— 典型的「假成功」）。
      * ``name`` 是持仓行展示所需的标的中文名，与 ``AccountSnapshot.account_id``
        同属「标准字段之外的必需展示字段」。
    """

    instrument: InstrumentId
    quantity: int
    available_quantity: int = 0
    average_cost: float = 0.0
    market_value: float = 0.0
    name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    cash: float
    frozen: float = 0.0
    assets: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ConnectorPort(Protocol):
    """Async boundary used by lifecycle and execution orchestration."""

    descriptor: ConnectorDescriptor

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def is_connected(self) -> bool: ...

    async def place_order(self, request: OrderRequest) -> OrderSnapshot: ...

    async def cancel_order(self, order_id: str) -> OrderSnapshot: ...


# ---------------------------------------------------------------------------
# V9 Phase 6（D-A）：9 端口拆分 —— 应用服务依赖窄接口，连接器负责 SDK 映射。
# 全部 runtime_checkable，供 test_connector_ports.py 做结构化契约测试。
# ---------------------------------------------------------------------------

@runtime_checkable
class ExecutionPort(Protocol):
    """交易执行：下单 / 撤单 / 委托与成交查询。

    ★ **P0-b 异步口径（接线前的硬前置）**：全部方法必须是 ``async``。
      QMT / 大 QMT 的 SDK 调用都是**同步阻塞**的（``get_stock_list`` 全市场遍历、
      ``get_trading_calendar`` 可取数秒），若端口是同步方法，接线后这些调用会直接
      跑在 asyncio 事件循环线程上 → 全站卡顿。实现方必须在内部把调用 offload 到
      线程池（``XTQuantBridge.call`` / ``call_locked`` 已提供该语义 + 30s 硬超时）。
    ★ 返回值必须是 canonical 快照，禁止透传方言 raw dict（杜绝字段漂移进上层）。
    """

    async def place_order(self, request: OrderRequest) -> OrderSnapshot: ...

    async def cancel_order(self, order_id: str) -> OrderSnapshot: ...

    async def get_orders(self) -> list[OrderSnapshot]: ...

    async def get_deals(self) -> list[OrderSnapshot]: ...


@runtime_checkable
class AccountPort(Protocol):
    """账户资产：净值摘要与资金（async，理由同 ExecutionPort）。"""

    async def get_account(self) -> AccountSnapshot: ...

    async def get_cash(self) -> AccountSnapshot: ...


@runtime_checkable
class PositionsPort(Protocol):
    """持仓快照查询（async）。"""

    async def get_positions(self, symbol: str | None = None) -> list[PositionSnapshot]: ...


@runtime_checkable
class MarketDataPort(Protocol):
    """历史/快照行情：K 线、报价、全 tick（async）。

    返回值保留 ``dict`` / ``list[dict]``：K 线与盘口是**高频大数据**，逐条构造成
    dataclass 的收益低于其成本，且其字段契约由数据面（``datasource``）而非适配层
    负责。这是唯一有意例外的端口。
    """

    async def get_quote(self, code: str) -> dict[str, Any]: ...

    async def get_kline(self, code: str, period: str, count: int,
                        adjust: str = "", start: str = "",
                        end: str = "") -> list[dict[str, Any]]: ...

    async def get_full_tick(self, codes: list[str]) -> dict[str, Any]: ...


@runtime_checkable
class QuoteFeedPort(Protocol):
    """实时行情订阅（回调式）。注册动作本身是 SDK 调用，故为 async。"""

    async def subscribe_quote(self, codes: list[str],
                              on_tick: Callable[[dict], None]) -> None: ...


@runtime_checkable
class InstrumentPort(Protocol):
    """合约与板块基础数据（async；``get_stock_list`` 可能很慢）。"""

    async def get_instrument_detail(self, code: str) -> dict[str, Any]: ...

    async def get_stock_list(self, sector: str = "沪深A股") -> list[dict[str, Any]]: ...

    async def get_sector_list(self) -> list[str]: ...


@runtime_checkable
class CalendarPort(Protocol):
    """交易日历（async）。"""

    async def get_trading_calendar(self, start: str = "",
                                   end: str = "") -> list[str]: ...


@runtime_checkable
class HealthPort(Protocol):
    """连接健康：探活 + 结构化诊断。

    ★ ``is_connected`` **刻意保持同步**：它被 ``ConnectionSupervisor._health_check``
      周期性调用，且必须在事件循环线程上瞬时返回。
      契约要求：**只能做 O(1) 状态读取，禁止任何 IO**（实现方需要 IO 请自行缓存
      最近一次探测结果）。真正的 SDK 探活走 ``async test_connection``。
    """

    def is_connected(self) -> bool: ...

    async def test_connection(self) -> dict[str, Any]: ...


@runtime_checkable
class CapabilityPort(Protocol):
    """能力协商：连接器声明自己支持什么（quote/kline/trade/...）。"""

    descriptor: ConnectorDescriptor

    def capabilities(self) -> tuple[str, ...]: ...


# ---------------------------------------------------------------------------
# P0 阶段新增（方案 C：``docs/UNIFIED_TRADING_ABSTRACTION.md`` §5.2）
#
# 三条设计纪律，改动前先读：
#  1) **不新建状态 SSOT**。``OrderSnapshot.status`` 必须是
#     ``xtquant_client.order_status`` 的标准词表之一；大 QMT 的 ``m_nOrderStatus``
#     与 xtquant 的 ``OrderStatus`` 是同一整数族群（50 已报 / 56 已成 / 57 废单），
#     重复建映射表会重演「三处各自维护一套状态映射」的历史根因。
#  2) **不得据单点探测断言终端能力**。``UNKNOWN`` 是合法答案——见
#     ``CapabilityLevel`` 注释。
#  3) 新增端口一律作为**独立 Protocol**，不往既有端口里加方法：
#     ``runtime_checkable`` 的 isinstance 校验要求成员齐全，改既有可能口会让
#     ``tests/test_connector_ports.py`` 的结构化断言意外失败。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderSnapshot:
    """委托快照（canonical 返回值，替代各处自造的 raw dict）。

    ★ ``status`` 只允许取 ``order_status`` SSOT 的六个标准值；方言原始值一律
    落到 ``raw_status``（仅供诊断，**禁止参与任何判定**）。
    · ``accepted`` 是否已被柜台受理：``False`` 表示「未确认/被拒/未知」。
      零 mock 契约：拿不到柜台委托号就绝不置 True，由上层决定重查还是报错。
    """

    status: str = UNKNOWN
    broker_order_id: str = ""
    client_order_id: str = ""
    accepted: bool = False
    instrument: InstrumentId | None = None
    side: str = ""
    requested_quantity: int = 0
    filled_quantity: int = 0
    avg_price: float = 0.0
    raw_status: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)


class EventSemantics(str, Enum):
    """事件来源语义：上层必须据此判断「能不能依赖事件做实时决策」。

    - ``PUSH``            柜台原生推送（xtquant/CTP 类回调），可当作实时；
    - ``POLL_DIFF``       由查询差分合成（大 QMT 桥接常态），**有延迟上界**，
                          超时守护类逻辑必须留出这个上界，否则会误撤未回报的单；
    - ``PUSH_WITH_GAP``   推送但可能丢帧（回放/断线补推），需 acknowledgment 去重；
    - ``NONE``            不提供事件；上层必须自行轮询撒面。
    """

    PUSH = "push"
    PUSH_WITH_GAP = "push_with_gap"
    POLL_DIFF = "poll_diff"
    NONE = "none"


@dataclass(frozen=True)
class EventSemanticsSpec:
    semantics: EventSemantics
    max_latency_ms: int = 0     # POLL_DIFF / PUSH_WITH_GAP 必须 >0；PUSH 填 0
    poll_interval_ms: int = 0


@dataclass(frozen=True)
class CanonicalEvent:
    """统一事件形态：不论底层是推送还是轮询合成，对上层同形。"""

    kind: str                   # order / trade / order_error / cancel_error
    occurred_at: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0                # 合成源自增序号，用于兜住差分流的乱序/重放
    synthetic: bool = False     # True = 非柜台原生，由差分/轮询合成


@runtime_checkable
class EventPort(Protocol):
    """委托/成交事件流端口（新增第 10 口）。

    为什么不直接用 ``BrokerAdapter.on_order/on_trade``：那两个钩子是「有回调
    才注册」的 no-op 语义，上层无法区分「这个客户端没有回调」和「暂时没事件」。
    本端口要求实现方显式声明 ``event_semantics()`` ——大 QMT 桥接答 POLL_DIFF +
    延迟上界，miniQMT 答 PUSH，上层据此决定超时守护阈值。
    """

    def event_semantics(self) -> EventSemanticsSpec: ...

    def stream_events(self) -> list[CanonicalEvent]: ...


class CapabilityLevel(str, Enum):
    """能力等级。

    ★ ``UNKNOWN`` 是**一等公民**：单次探测（如「未在该解析路径捕获到注入函数」）
    只能得出 UNKNOWN，**绝不能据以断言终端没有该能力**——给错误的确定性结论
    比返回空更糟（会让人不再去真正的地方查）。这是大 QMT 桥的血泪教训。
    """

    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Capability:
    name: str
    level: CapabilityLevel = CapabilityLevel.UNKNOWN
    reason: str = ""
    probed_at: str = ""


@dataclass(frozen=True)
class CapabilitySet:
    """运行时协商结果（快照）。静态 bool 标记不够用：大 QMT 的实际能力随
    券商授权、客户端版本、注入函数、传输可达性漂移，必须每次连接后重探。"""

    capabilities: tuple[Capability, ...] = ()
    probed_at: str = ""

    def level_of(self, name: str) -> CapabilityLevel:
        for c in self.capabilities:
            if c.name == name:
                return c.level
        return CapabilityLevel.UNKNOWN

    def is_supported(self, name: str) -> bool:
        """只有显式 SUPPORTED 才算可用；DEGRADED/UNKNOWN 都走降级路径。"""
        return self.level_of(name) is CapabilityLevel.SUPPORTED

    def is_definitely_unsupported(self, name: str) -> bool:
        """仅在**实调验证过**的 UNSUPPORTED 下为 True（可用于「别再重试」决策）。"""
        return self.level_of(name) is CapabilityLevel.UNSUPPORTED


@runtime_checkable
class CapabilityProbePort(Protocol):
    """运行时能力协商（新增第 11 口）。

    与既有 ``CapabilityPort.capabilities()``（静态声明）并存而非替换，
    逐步迁移：新连接器优先实现本端口。
    """

    async def probe_capabilities(self) -> CapabilitySet: ...


#: 端口清单（DoD 测试与文档引用的唯一真源）。
#: 口径：「**9 个窄端口**」（Execution / Account / Positions / MarketData / QuoteFeed /
#: Instrument / Calendar / Health / Capability）+「1 个旧聚合口」``ConnectorPort``
#: （lifecycle + place/cancel 的最小面，保留给尚未迁移的调用方）= 共 10 个条目。
#: 与 ``EXTENDED_PORTS``（P0 新增）无关。
CANONICAL_PORTS: tuple[str, ...] = (
    "ConnectorPort", "ExecutionPort", "AccountPort", "PositionsPort",
    "MarketDataPort", "QuoteFeedPort", "InstrumentPort", "CalendarPort",
    "HealthPort", "CapabilityPort",
)

#: P0 新增端口（尚未全部实现：``QmtConnector`` 将在接线阶段陆续满足）
EXTENDED_PORTS: tuple[str, ...] = ("EventPort", "CapabilityProbePort")
