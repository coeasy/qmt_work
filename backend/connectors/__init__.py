"""Platform-neutral connector ports and lifecycle supervision (V9 Phase 8).

**接线状态（2026-10-01 大小 QMT 接口对齐升级后）**：本包已**部分接入产线**，不再
是「零产线引用」：

* 灰度开关 ``QMT_USE_PORTS=1`` 时，`gateway/execution.py` 的
  ``ExecutionService.place_order/cancel_order`` 经 `GenericConnector`
  （`Dialect×Transport`）下单/撤单；**默认 0 = 行为与接线前完全一致**；
* `gateway/easytrader_facade.py` 的 ``use(key)`` 经 `connectors.registry.resolve`
  装配连接器（如 `qmt.mini` / `qmt.big.bridge.file`）；
* **EventPort 已真正接通**：进程内直连（miniQMT）走 `PUSH`（adapter 回调 →
  `PushEventSource`），文件桥（大 QMT）走 `POLL_DIFF`（读 `events.ndjson` →
  `canonical_from_agent_event`）；两者对上层**同形**；
* **大小 QMT 接口已对齐**：回调式订阅在跨进程传输下显式 `UnsupportedOp`（不再
  JSON 序列化崩溃）；`probe_capabilities` 改为**运行时真探**（据 agent 上报的
  注入函数清单 + 事件语义推导 realtime，未验证项诚实 `UNKNOWN`）；写操作串行化
  改由规范化 op 的 `WireRequest.write` 判定，解除对 adapter 方法名的依赖；
* **异常层级已接缝（2026-10-01）**：`ConnectorError` ⊂ `BrokerError`，
  `TransportError` 同时 ⊂ `BrokerNotConnectedError`。此前两套层级之间**没有接缝**
  ⇒ 桥的任何传输故障（agent 未运行 / bridge_dir 两端不一致 / 超时）都会穿透到
  FastAPI 兜底，变成 **HTTP 500「服务器内部错误」**，而 `transport.py` 的注释
  早已写明「上层映射为 `BrokerNotConnectedError` → 503」却从未实现。现归因正确：
  `_call` → 503 + 真因，`signal_router._live` → 503（连不上）/ 400（可用但被拒）。
  回归判据 `tests/test_connector_error_taxonomy.py`；
* **前端诊断面已接通**：连接管理页的「连接诊断」面板渲染大 QMT 探针
  （`BigQmtProbeBlock`，判定逻辑在 `domains/system/bigqmtProbe.ts`），
  六种「连上了但没数据」的故障在界面上互相可区分。

实际在用的券商客户端仍是 ``xtquant_client/``；本包的 ``qmt.py`` 只是包在它之上的
适配层（``from xtquant_client.base import BrokerAdapter``），**不构成重复实现**。

**仍 NOT WIRED 的部分**：
* 默认路径（`QMT_USE_PORTS=0`）仍走旧 bridge，`connectors` 仅在开关开启时介入；
* 部署向导 UI（V4 §11 的 2-c / 3-b）尚未做；
* Phase 3 完整摘开关（移除旧路径）是后续动作，届时本段说明须同步更新。
"""
from .canonicalize import (
    account_snapshot,
    order_snapshot,
    position_snapshot,
    trade_snapshot,
)
from .dialects import get_dialect
from .events import PollDiffEventSource, PushEventSource
from .generic import GenericConnector
from .http import HttpTradingConnector
from .ports import (
    CANONICAL_PORTS,
    EXTENDED_PORTS,
    AccountSnapshot,
    CanonicalEvent,
    Capability,
    CapabilityLevel,
    CapabilityProbePort,
    CapabilitySet,
    ConnectorDescriptor,
    ConnectorError,
    ConnectorPort,
    ConnectorState,
    EventPort,
    EventSemantics,
    EventSemanticsSpec,
    InstrumentId,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
)
from .qmt import QmtConnector
from .registry import available_keys, describe, resolve
from .supervisor import ConnectionSupervisor
from .transport import TransportError, WireRequest, WireResponse
from .transports import get_transport
from .transports.semantics import max_latency_ms, spec_for

__all__ = [
    "AccountSnapshot", "ConnectorDescriptor", "ConnectorError", "ConnectorPort",
    "ConnectorState", "ConnectionSupervisor", "InstrumentId", "OrderRequest",
    "PositionSnapshot", "QmtConnector", "HttpTradingConnector",
    # P0-a 新增
    "OrderSnapshot", "CanonicalEvent", "Capability", "CapabilityLevel",
    "CapabilitySet", "CapabilityProbePort", "EventPort", "EventSemantics",
    "EventSemanticsSpec", "CANONICAL_PORTS", "EXTENDED_PORTS",
    "order_snapshot", "trade_snapshot", "position_snapshot", "account_snapshot",
    # Phase 1/2 新增
    "resolve", "describe", "available_keys", "get_dialect", "get_transport",
    "GenericConnector", "TransportError", "WireRequest", "WireResponse",
    "PollDiffEventSource", "PushEventSource", "spec_for", "max_latency_ms",
]
