# qmt_work 通用证券客户端统一适配平台 —— 最终规划方案

> **版本**：V4.0（终稿，2026-10-01）
> **谱系**：V3 愿景稿（内容已并入本文、实体已删除：`docs/QMT_UNIVERSAL_BROKER_PLATFORM_ARCHITECTURE_V3.md`）
> 　　　　→ 并入 `docs/UNIFIED_TRADING_ABSTRACTION.md`（选型论证，结论：方案 C）
> 　　　　→ 并入 `docs/BIG_QMT_COMPAT_PLAN.md`（首个落地案例：大 QMT）
> **本文定位**：**唯一执行口径**。前序文档分别退居「论证」「案例」角色（愿景稿已并入本文），凡与本文冲突，以本文为准。
> **已开工标记**：第 10 节记录 P0-a 已落地的代码与门禁同步（非纸面计划）。

---

## 0. 结论先行

### 0.1 一句话

qmt_work 升级为 **Universal Securities Broker Runtime Platform**：**在不破坏现有不变量的前提下**，把「客户端差异」压缩为 **Dialect（说什么）× Transport（怎么送）** 两个正交维度，用 **11 个 canonical Port + 运行时能力协商** 承接，使 MiniQMT / BigQMT / PTrade / 同花顺 / 掘金等客户端以**线性成本**接入。

### 0.2 三条不可妥协的原则（V3 没有写，这是本文最重要的增补）

| # | 原则 | 违反后果 |
|---|---|---|
| **INV-1 唯一交易入口** | 任何真实下单必须经 `SignalRouter.submit`（`backend/gateway/signal_router.py`）。Port / Connector / Dialect 层**不得**成为新的下单入口 | 风控 Mandatory、幂等、TOTP、WAL intent→result、审计、通知被绕过 —— 这不是「少一层封装」，是**合规底线失守** |
| **INV-2 零 mock** | 券商不可达 → `200 + code=503 + 引导`；柜台拒单 → `400 + 真因`；**失败绝不包成 `code=0`**；拿不到柜台委托号就**不许**声称「已受理」 | 假绿灯。项目已两次栽在「绿灯是另一个 bug 遮出来的」（TD-23/24/25） |
| **INV-3 不新建第四套东西** | 状态／撮合词表复用 `xtquant_client/order_status.py` SSOT；生命周期复用 `ConnectionSupervisor`；传输复用 `XTQuantBridge` 语义 | 历史上的根因从来不是「缺抽象」，而是「同一个概念有三处各自的实现」 |

### 0.3 三份前序文档的关系

```
V3 愿景（我要成为什么）  ──►  本文 §2 目标架构
        │                            ▲
        │                            │ 修订 12 项（§1）
UNIFIED_TRADING_ABSTRACTION（为什么是这个内核）
        │   论证：A/B/C/D/E/F 六方案加权，C 胜出（4.62）
        │   ──► 本文 §3 Dialect×Transport、§4 canonical 模型、§6 Capability
BIG_QMT_COMPAT_PLAN（第一个客户）
        │   ──► 本文 §9 首个落地（路径 A 直连 / 路径 B 内置策略桥）
        └────► 同时反向修正 V3：状态不再新建映射表
```

### 0.4 V3 → V4 改进摘要（详见附录 A 逐条对照）

| # | V3 的做法 | V4 的修订 | 类别 |
|---|---|---|---|
| 1 | 核心接口 9 个动词（connect/account/positions/…） | 保留动词语义，但**下沉为 11 个 Port**，并明确动词之上必须先过编排层 | 结构性 |
| 2 | 未区分「编排」与「适配」 | 显式加 **Orchestration 层**，并配套 AST 护栏使 INV-1 机器可校验 | 不变量 |
| 3 | 领域模型 5~6 字段裸 dict | canonical frozen dataclass + `.validate()` + status SSOT + `raw` 留证 | 契约 |
| 4 | `broker/adapters/{qmt_mini,qmt_big,ptrade,ths}` 平铺 | **Dialect × Transport 正交组合**（3 方言 × 4 传输 → 6 个组合由配置合成，非 6 个类） | 结构性 |
| 5 | Runtime Manager 只有目录名 | 落到 **detector/loader/manager/health 四件事**，且**复用**既有 `ConnectionSupervisor`，严禁第二套生命周期 | 防重复 |
| 6 | Capability = `{"margin": false}` 静态 bool | 四级 `SUPPORTED/DEGRADED/UNSUPPORTED/UNKNOWN` + reason + probed_at，**UNKNOWN 是一等公民** | 正确性 |
| 7 | 无事件层 | 新增 **EventPort** + `EventSemantics`（PUSH / POLL_DIFF / PUSH_WITH_GAP / NONE）+ **延迟上界** | 正确性 |
| 8 | 多账户路由只有一张图 | 补 AccountRoute 解析、失败隔离、`QMT_ONLY` 钉死策略不静默降级 | 完整性 |
| 9 | EasyTrader 兼容层悬空 | 定性为 **Ports 之下的可选 façade**，且禁止提供能绕过 INV-1 的逃逸口；确需原生能力走**受控逃生舱**（过 WAL + 审计） | 不变量 |
| 10 | 未提失败语义 | 503/400/429/500 语义表贯通到 Port 层 | 零 mock |
| 11 | Phase 1~5 无验收判据 | 每阶段 DoD + 回滚方案 + 「假绿灯」防护（护栏+日志为准） | 可执行 |
| 12 | 未提既有分期-26 环境护栏 | 全文贴合项目既有硬约束（契约计数、AST 护栏、目录/防护 TD-23~26） | 可执行 |

---

## 1. 为什么要修订 V3（V3 的五个真实风险）

V3 是一份**正确的愿景**，但直接照它落地会在第 3 个客户端接入时出问题。评估如下：

| V3 条目 | 具体问题 | 为什么必须现在改 |
|---|---|---|
| §3 Broker Core 抽象（9 动词） | 动词级抽象**天然允许绕过**：任何人可以拿到 adapter 对象直接 `place_order`。easytrader 的 `user.trader` 逃逸口就是这个结构的必然产物 | qmt_work 的差异化资产恰恰是「不能绕过」。动词层不是「兜不住」，是「设计上允许绕过」 |
| §4 统一领域模型 | 字段过少（OrderRequest 无 `client_order_id`/策略名；Account 无 frozen/raw），且**没有规定状态必须归一** | 放任就会重演「`reconcile._norm_status` / `order_watchdog._ACTIVE` / `sync._order_fp` 三处各维护一套状态映射」的历史根因 |
| §7 Adapter 插件体系 | 目录按**客户端**平铺。一旦同一客户端有 N 种接法（直连/文件桥/redis 桥），类数量 = N×M | 大 QMT 已经至少 4 种接法（direct / bridge.file / bridge.redis / bridge.zmq）。平铺会导致同一份 wire 协议被抄 3 遍 |
| §6 Runtime Manager | 只给了四个文件名，没说与现有 `BrokerManager` / `ConnectionSupervisor` 的关系 | 直接实现 = 项目里出现**两套**生命周期管理（连接去重、退避、健康），是 TD 级的重复债 |
| §8 Capability 模型 | `features: {bool}` 静态声明 | 大 QMT 的真实能力随**券商授权串、客户端版本、注入函数、传输可达性**漂移。静态 bool 只能表达「编写时的猜测」，而 guessing 正是「禁止据单点探测断言终端能力」纪律要防的 |
| §10 EasyTrader 兼容层 | 「兼容层」画在架构图里但没有边界 | 若兼容层能直接下单，等于给 INV-1 开了一个官方后门 |

> 结论不是推翻 V3，而是**给它补上「不可绕过」的骨架**和「组合而非继承」的装配方式。

---

## 2. 目标架构（V4）

### 2.1 六层

```
┌──────────────────────────────────────────────────────────────────────────┐
│ L6  接入面        REST / WebSocket / MCP / Python SDK                      │
│                  frontend-next  ·  mcp_server  ·  app/routes               │
├──────────────────────────────────────────────────────────────────────────┤
│ L5  编排层 Orchestration      ★ 唯一交易入口，客户端无关，不可绕过 ★        │
│     SignalRouter.submit → 幂等(single_flight) / 风控(Mandatory) /          │
│     TOTP 大额二次确认 / WAL(intent→result) / 审计 / 通知 / core.emit        │
│     ── AST 护栏：engines/ app/ gateway/ 禁止直连 adapter/bridge ──         │
├──────────────────────────────────────────────────────────────────────────┤
│ L4  契约层 Canonical Ports（connectors/ports.py —— 唯一方言边界）          │
│     既有 9 口：Connector Execution Account Positions MarketData            │
│               QuoteFeed Instrument Calendar Health Capability              │
│     P0 新增 2 口：EventPort（第10） · CapabilityProbePort（第11）           │
│     模型：OrderRequest OrderSnapshot PositionSnapshot AccountSnapshot      │
│           CanonicalEvent InstrumentId CapabilitySet EventSemanticsSpec      │
├──────────────────────────────────────────────────────────────────────────┤
│ L3  连接器层 Connector = Dialect（说什么）× Transport（怎么送）            │
│     Dialect   : XtQuantV1 │ BigQmtV1 │ (PtradeV1 / ThsUiV1 …)             │
│     Transport : InProcess │ SubprocessBridge │ FileSignal │ Redis │ Zmq    │
│     由 ConnectorRegistry 按 `<broker>.<mode>[.<transport>]` 组合装配        │
├──────────────────────────────────────────────────────────────────────────┤
│ L2  生命周期 ConnectionSupervisor（唯一）                                   │
│     六态 FSM + 健康探测 + 指数退避 + 单连接故障隔离 + 尊重人工停机           │
├──────────────────────────────────────────────────────────────────────────┤
│ L1  客户端进程  MiniQMT(XtMiniQmt.exe) │ 大QMT(内置 py3.6) │ PTrade │ …    │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.2 三条边界纪律

| 边界 | 规则 | 机器校验 |
|---|---|---|
| **L5 / L4** | 只有 `signal_router.py` 与 `execution.py` 可以持有 Port 句柄；其余模块下单必须经 `SignalRouter.submit` | `scripts/check_execution_architecture.py` 扩展 Gate（AST 扫描 `adapter.place_order` / `bridge.gateway.*` / `manager.bridge(...).call(`） |
| **L4 / L3** | Port 层**禁止**出现任何 SDK 类型／字段名；方言字段提取只准写在 `connectors/canonicalize.py` 或 dialect 包内 | 契约层 `import` 白名单（ports.py 只允许 stdlib + `xtquant_client.order_status`） |
| **L3 / L1** | dialect 不得感知自己在哪个进程；transport 决定进程边界 | Port 层无 IPC 概念；每个 transport 自带 `transport_id` 供诊断 |

### 2.3 不变量清单（进 CI）

| ID | 不变量 | 判据 |
|---|---|---|
| INV-1 | `SignalRouter.submit` 唯一交易入口 | `check_execution_architecture` Gate 无违规；新增 Agent Role Review 检查项 |
| INV-2 | 零 mock / 失败不包 `code=0` | 契约测试套件（既有 `test_execution_semantics` 类）+ 新增 canonicalize 不粉饰用例 |
| INV-3 | 状态唯一 SSOT | `grep` 门禁：禁止在 `order_status.py` 之外定义第二张「原始状态→标准状态」映射表 |
| INV-4 | 生命周期唯一实现 | 门禁：禁止新建 `*manager*loop` / `*reconnect*` 独立 asyncio task（除 supervisor） |
| INV-5 | 事件出口唯一 `core.emit.emit_event`（三参标准形态） | `tests/test_emit_event.py` AST 全量扫描（含 `engines/`）已覆盖 |
| INV-6 | 增删改字段名必须两侧同步 | `scripts/check_api_contract_drift.py`（路径+参数级）+ `check_capability_coverage.py` |
| INV-7 | 测试计数不漂移 | `scripts/ci_reconcile.py::EXPECTED_TESTS`（**1716**，155 文件）+ README 两处计数 |
| INV-8 | 停机路径禁止无界等待（TD-25） | 所有 transport IO 必须有 `timeout=`；新增 transport 的 review 必查项 |

---

## 3. 核心改造：Dialect × Transport 正交

### 3.1 为什么必须拆

V3 的 `adapters/{qmt_mini, qmt_big, ptrade, ths}` 把两件正交的事耦在一个类里：

- **Dialect（说什么）**：调用哪些动词、参数如何排列、返回值形状；
- **Transport（怎么送）**：同进程 SDK 调用？子进程 JSON 桥？文件轮询？Redis？ZMQ？

不拆 ⇒ 每新增一种接法都要**重写一遍「动词+提取+错误映射」**。拆开后：

| Connector 实例 | Dialect | Transport | 备注 |
|---|---|---|---|
| `qmt.mini` | XtQuantV1 | InProcess（ABI 不匹配时自动降级 SubprocessBridge） | **现状已支持**（`XTPQuantAdapter` / `BridgeAdapter`） |
| `qmt.big.direct` | XtQuantV1 | InProcess | 路径 A，`client_mode=full`，交易目录 `userdata` |
| `qmt.big.bridge.file` | BigQmtV1 | FileSignal | 路径 B 默认，零部署，秒级延迟 |
| `qmt.big.bridge.redis` | BigQmtV1 | RedisRPC | Phase 3，<50ms |
| `qmt.big.bridge.zmq` | BigQmtV1 | ZmqRPC | Phase 3，同机极致延迟 |
| `ptrade.http` | PtradeV1 | HttpSidecar | 未来：`HttpTradingConnector`（`connectors/http.py`）已是样板 |

**3 方言 × 4 传输 ⇒ 实际 6 个组合，全部由配置组合产生，没有一个是手写类。**
新增一个客户端 = 新增 1 个 dialect；新增一种部署形态 = 新增 1 个 transport。

### 3.2 Dialect / Transport 的最小接口面

```python
# connectors/transport.py（新增，示意）
class Transport(Protocol):
    transport_id: str                                   # "inprocess" | "subprocess" | "file" | "redis" | "zmq"
    def invoke(self, wire: dict, *, timeout: float) -> dict: ...
    # wire = dialect 产出的方言无关信封；transport 只负责「送到 + 取回 + 超时」
```

```python
# connectors/dialects/xtquant_v1.py（新增，从 XTPQuantAdapter 剥离）
class XtQuantV1(Dialect):
    """只知道 xtquant 动词与返回形状，不知道自己跑在哪个进程。"""
    def build_wire(self, op: str, args: dict) -> dict: ...
    def parse(self, op: str, envelope: dict) -> dict: ...   # 语法归一（非语义）
```

> `Transport.invoke` 的 `timeout` **必填**：这是 INV-8（TD-25）的可执行形式。文件轮询单次上限建议 5s，Redis/ZMQ 2s。

### 3.3 最终目录规划

```
backend/connectors/
├── ports.py                 # [已改] 11 口 Protocol + canonical 模型 + CANONICAL_PORTS/EXTENDED_PORTS
├── canonicalize.py          # [已新增] 方言 raw dict → canonical 快照（唯一转换层）
├── transport.py             # [待] Transport 抽象 + invoke(timeout=)
├── registry.py              # [待] "<broker>.<mode>[.<transport>]" → Connector 装配
├── supervisor.py            # [不改] 生命周期唯一实现
├── qmt.py                   # [改] QmtConnector 满足 ExecutionPort/AccountPort/… 并返回 canonical 快照
├── http.py                  # [不改] HttpTradingConnector 样板
├── dialects/
│   ├── xtquant_v1.py        # mini 与 big.direct 共用
│   ├── bigqmt_v1.py         # passorder / cancel / get_trade_detail_data / ContextInfo
│   └── ptrade_v1.py         # 未来
└── transports/
    ├── inprocess.py         # 包装现有 XTQuantBridge.call / call_locked
    ├── subprocess_bridge.py # 复用 bridge_client / bridge_server（行分隔 JSON）
    ├── file_signal.py       # bridge_dir/req|resp/<signal_id>.json + events.ndjson
    ├── redis_rpc.py         # rpush/blpop + pub/sub
    └── zmq_rpc.py           # ROUTER/DEALER

backend/agent_bigqmt/        # [待] 拷给用户放进大 QMT 的 py3.6 策略端（不进 PyInstaller 打包体）
frontend-next/src/           # [待] 连接向导：客户端选择 + 传输配置 + capability 驱动的动态表单
```

> **注意**：不新建顶层 `broker/` 包。项目既有分层是 `core/ → engines/ → datasource/ → app/`，客户端适配的既有位置是 `xtquant_client/` 与 `connectors/`；再开一个 `broker/` 会制造第四个「客户端抽象」包（违反 INV-3）。

### 3.4 Port 全表（11 口）

| # | Port | 关键方法 | 谁实现 | 状态 |
|---|---|---|---|---|
| 1 | `ConnectorPort` | `start / close / is_connected / place_order / cancel_order` | `QmtConnector` | ✅ 已定义 |
| 2 | `ExecutionPort` | `place_order / cancel_order / get_orders / get_deals` | `QmtConnector` | ⚠️ 见 §11 P0-b（异步口径） |
| 3 | `AccountPort` | `get_account / get_cash` | `QmtConnector` | ⚠️ 返回 raw dict（D1） |
| 4 | `PositionsPort` | `get_positions(symbol=None)` | `QmtConnector` | ⚠️ 同上 |
| 5 | `MarketDataPort` | `get_quote / get_kline / get_full_tick` | `QmtConnector` | ⚠️ 签名缺 `start/end`（D4） |
| 6 | `QuoteFeedPort` | `subscribe_quote(codes, on_tick)` | `QmtConnector` | ✅ |
| 7 | `InstrumentPort` | `get_instrument_detail / get_stock_list / get_sector_list` | `QmtConnector` | ✅ |
| 8 | `CalendarPort` | `get_trading_calendar(start, end)` | `QmtConnector` | ✅ |
| 9 | `HealthPort` | `is_connected / test_connection` | `QmtConnector` | ✅ |
| 10 | `CapabilityPort` | `capabilities()` 静态声明 | 全部 | ✅ 保留（与 #11 并存） |
| **11** | **`CapabilityProbePort`** | **`probe_capabilities() -> CapabilitySet`** | 新增实现 | 🆕 **P0-a 已定义** |
| **12** | **`EventPort`** | **`event_semantics() / stream_events()`** | 新增实现 | 🆕 **P0-a 已定义** |

> 计数口径说明：历史上称「9 端口清单」（`CANONICAL_PORTS` 元组含 `CapabilityPort` 实为 10 名）。V4 起显式区分 **`CANONICAL_PORTS`（既有 10 名，不得改动——`tests/test_connector_ports.py` 有结构化断言）** 与 **`EXTENDED_PORTS`（新 2 口）**，合计 12 个 Port 类型。

---

## 4. canonical 数据模型

### 4.1 模型清单

| 模型 | 用途 | 关键字段 |
|---|---|---|
| `InstrumentId` | 合约标识 | `code` / `exchange` / `.canonical`（`600036` + `SH` → `600036.SH`） |
| `OrderRequest` | 下单入参（frozen + `.validate()`） | instrument / side / order_type / price / quantity / account_id / **client_order_id** / strategy_name / remark |
| `OrderSnapshot` | 委托快照 | **status（SSOT）** / broker_order_id / client_order_id / **accepted** / instrument / side / requested_quantity / filled_quantity / avg_price / **raw_status** / raw |
| `PositionSnapshot` | 持仓 | instrument / quantity / available_quantity / average_cost / market_value |
| `AccountSnapshot` | 资金 | account_id / cash / frozen / assets / **raw** |
| `CanonicalEvent` | 统一事件 | kind / occurred_at / data / seq / **synthetic** |
| `CapabilitySet` | 能力快照 | capabilities(Capability×N) / probed_at |

### 4.2 状态纪律（INV-3 的落点）

```
大 QMT m_nOrderStatus ─┐
xtquant OrderStatus  ─┼─► connectors/canonicalize.py ─► xtquant_client.order_status.normalize_order_status
easytrader 中英文串 ─┘                                    └─► pending / partial / filled / cancelled / rejected / unknown
```

**这是三方互证的同一整数族群**（50 已报 / 56 已成 / 57 废单）。因此：

- ❌ **不为大 QMT 新建 `status_map.py`**（这是 V3/初版大 QMT 方案的过度设计，已修正）；
- ✅ 方言原始值只落到 `raw_status`，**禁止参与任何判定**；
- ✅ 拿不到 ⇒ `unknown`，**绝不静默映射成 `pending`**（`_ACTIVE` 超时守护會据此误撤单）。

### 4.3 `accepted` 判据（零 mock 的可执行形式）

```python
def _accepted(status: str, broker_order_id: str) -> bool:
    if not broker_order_id:          # 没有柜台委托号 ⇒ 谈不上受理
        return False
    if status in (UNKNOWN, REJECTED):  # 废单即使有号也不算受理
        return False
    return True
```

`accepted=False` 时上层必须**自行决定重查还是报错**，不得当成成功。

### 4.4 EventPort 与延迟上界

| 语义 | 适用 | 上层必须知道什么 |
|---|---|---|
| `PUSH` | miniQMT 原生回调 | 可当实时 |
| `PUSH_WITH_GAP` | 断线补推/回放 | 可能丢帧 ⇒ 需 seq 去重 |
| `POLL_DIFF` | 大 QMT 桥（file/redis/zmq） | **有延迟上界**（file ≤1s / redis ≤50ms）⇒ 超时守护必须留出上界，否则**误撤未回报的单** |
| `NONE` | 无事件客户端 | 上层自行轮询 |

`EventSemanticsSpec.max_latency_ms` 是**必填契约**：上层用它推导 `order_watchdog` 的守护阈值。

---

## 5. Runtime Manager：落地到已有设施（防止第二套生命周期）

V3 给了 `runtime/{detector,loader,manager,health}.py`。**V4 决定不新建包**，而是把四件事落到既有位置，理由是 INV-4：

| V3 组件 | V4 落点 | 说明 |
|---|---|---|
| `detector.py` | `xtquant_client/xtp/env.py::probe_environment` + 新增 `connectors/detectors.py`（薄封装） | 探测客户端安装路径 / `userdata` vs `userdata_mini` / 端口 58600·58610 / 授权串字段（须读文件**头部**，勿只读 tail） / Python ABI |
| `loader.py` | `xtquant_client/registry.py::create_adapter` → 扩为 `ConnectorRegistry.resolve(key)` | 按 `<broker>.<mode>[.<transport>]` 解析 dialect + transport 并装配 |
| `manager.py` | **`connectors/supervisor.py::ConnectionSupervisor`（已存在，不改）** | 启停、退避、隔离、尊重人工停机。**严禁**新建第二个 asyncio 探测循环 |
| `health.py` | `gateway/health.py` + `HealthPort.test_connection` + `CapabilityProbePort` | `/api/v1/live`、`/api/v1/ready`、`/api/v1/broker/health` 三个既有端点，不新增 |

### 5.1 六态 FSM

```
DISCONNECTED ──start──► STARTING ──ok──► CONNECTED
                          │                │
                      失败/部分能力     健康探测失败
                          ▼                ▼
                       FAILED ◄──────── DEGRADED ──退避重试──► CONNECTED
                          ▲                                    │
                          └────────── STOPPING ◄──── close ────┘
```

`DEGRADED` 的判定由 **`CapabilitySet`** 给出：核心能力（`TRADE_SUBMIT` / `ACCOUNT`）缺失 ⇒ FAILED；非核心（`CREDIT_ACCOUNT` / `L2`）缺失 ⇒ DEGRADED + 前端置灰。

### 5.2 失败语义表（Port 层统一）

| 情形 | 异常 | HTTP | 文案来源 |
|---|---|---|---|
| 客户端未启动 / agent 不可达 / PROBE 失败 | `BrokerNotConnectedError` | `200 + code=503` | 「未连接」+ 三类排查项（客户端未运行 / 路径不一致 / token 不匹配） |
| 柜台拒单（`order_stock` 返 -1、`passorder` 失败、57 废单） | `BrokerError` | `400` | 券商真因原文透传 |
| 注入函数缺失 / SDK 调用异常 | `BrokerSDKError` | `400` | 跨进程文案重建（既有 `from_message()`） |
| PID 白名单拒绝（`pid X not allowed`） | `BrokerError` + `needs_action` | `400` | 明确指向路径 B（桥接）引导，不再泛化为「找券商」 |
| 能力 UNSUPPORTED | `ConnectorError(unsupported)` | `400` + capability name | 前端据此置灰而非报错 |

---

## 6. Capability 模型

### 6.1 四级模型（替代 V3 的 bool）

```python
class CapabilityLevel(str, Enum):
    SUPPORTED = "supported"       # 实调验证通过
    DEGRADED  = "degraded"        # 可用但有降级（如事件走轮询而非推送）
    UNSUPPORTED = "unsupported"   # 实调验证明确不支持（可用于「别再重试」）
    UNKNOWN   = "unknown"         # ★ 一等公民：单点探测未捕获 ≠ 终端没有
```

**为什么 `UNKNOWN` 必须存在**：大 QMT 桥接的血泪教训（参考文献 `xtquant_big_convert`）——「未在该解析路径捕获到注入函数」被误报成「终端没有两融接口」，导致真正的桥 bug 被掩盖数周。
给一个**错误的确定性结论比返回空更糟**：它会让人不再去真正该查的地方查。

配套两个查询方法，语义刻意分开：

- `is_supported(name)` —— 只有显式 `SUPPORTED` 为真；用于「能不能走这条路」；
- `is_definitely_unsupported(name)` —— 只有**实调验证过**的 `UNSUPPORTED` 为真；用于「别再重试」。

### 6.2 probe 纪律

1. **禁止据单点探测断言终端能力**。捕获不到函数 ⇒ `UNKNOWN`，诊断文案写「该解析路径未捕获」，**禁写**「终端没有 XX 接口」；
2. probe 结果必须带 `probed_at`，前端展示「探测于 HH:MM:SS」，避免把陈旧快照当现状；
3. probe **必须有界**（每子项 ≤2s，总预算 ≤10s），且**不得阻塞 `/ready`** —— 超时的子项落 `UNKNOWN` 后台重试（TD-25 纪律）。

### 6.3 Capability 词表（初版）

| Capability | 含义 | 典型缺失后果 |
|---|---|---|
| `TRADE_SUBMIT` / `TRADE_CANCEL` | 下单 / 撤单 | 缺 ⇒ FAILED（不能交易） |
| `ACCOUNT` / `POSITION` / `ORDER` / `DEAL` | 四类查询 | `ACCOUNT` 缺 ⇒ FAILED |
| `QUOTE` / `KLINE` / `SUBSCRIBE` | 行情 | 缺 ⇒ 走 eltdx/公共源降级链 |
| `EVENT_PUSH` | 原生回调 | 缺 ⇒ `EventSemantics=POLL_DIFF/NONE` |
| `CREDIT_ACCOUNT` | 两融 | 缺 ⇒ DEGRADED + 前端置灰（首版不实现） |
| `L2` / `FINANCIAL` | 高阶数据 | 缺 ⇒ DEGRADED |

---

## 7. 多账户路由

V3 只有一张图。V4 补三条硬规则：

1. **路由键 = `account_id`**：`AccountRouter` 由 `(account_id)` → `connector_key`；同一 `broker` 下多账户共享进程 handle 但独立 supervisor 条目；
2. **故障隔离**：单连接健康失败只把**该连接**置 DEGRADED/FAILED，**不得**影响同进程其它 connector（这正是 `ConnectionSupervisor` 已有的隔离能力，不要绕过它）；
3. **不得静默降级**：`source_policy` 的 `qmt_only` 钉死策略在该策略下**必须**返回 503 + 引导，不得偷偷换成公用源。

```
账户A(8888) ─► qmt.mini ─────────┐
账户B(6666) ─► qmt.big.bridge.file ─► AccountRouter ─► ConnectionSupervisor（单连接隔离/退避）
账户C(9999) ─► ptrade.http ──────┘
```

---

## 8. EasyTrader 兼容层：给位置，也给边界

### 8.1 V4 定性

**EasyTrader 兼容层是 Ports 之下的可选 façade**，不是新的中间路线：

```
旧策略脚本 ──► easytrader 兼容 façade ──► SignalRouter.submit ──► Port ──► Connector
                     （薄 wrapper，无独立行为）
```

- ✅ 它**提供** `buy/sell/cancel_entrust/position/balance` 这套社区心智；
- ❌ 它**不得**提供 `user.trader` 式的无痕原生对象逃逸口 —— 那等于给 INV-1 开官方后门；
- ✅ 确需方言原生能力时，走**受控逃生舱** `CallNativePort.native_call(op, params)`：**必须过 WAL + 审计**（每条留痕），区别于 easytrader 的静默逃逸。

### 8.2 从 easytrader 学到的两件事（其余摒弃）

| 学 | 摒弃 |
|---|---|
| `use()` 工厂形态（对应我们的 `ConnectorRegistry`，已有） | dict 契约（不可机器校验，违反 W4） |
| 诚实承认抽象必然泄漏 → 进化为**受控+可审计**的逃生舱 | property 式最小公分母（`user.position` 隐含「所有客户端都能给持仓」） |
| 严格参数检查的教训（#520：follow 链路上多余参数不能崩在签名检查） | `ttype='对手方最优价格委托'` 式中文本土字符串枚举（沪/深可选值还不一致）→ 改为带 market 约束的枚举 + capability 声明 |

---

## 9. 首个落地：大 QMT（完整版）

保留双通道决策，全部细节见 `docs/BIG_QMT_COMPAT_PLAN.md`（v1.1）。本文只锁定它对 V4 的**贡献面**：

| 路径 | V4 中的组装形态 | 何时用 |
|---|---|---|
| **A：xtquant 直连** | Dialect=`XtQuantV1` + Transport=`InProcess`，`client_mode=full`，交易目录 `userdata` | 券商未启用 PID 白名单时**优先**（延迟最低，零部署） |
| **B：内置策略桥** | Dialect=`BigQmtV1` + Transport=`FileSignal`/`RedisRPC`/`ZmqRPC` | PID 白名单封死外部直连时的**兜底**（未来主流形态） |

从 V3 继承但被 V4 修正的三点：

1. **不新建 `bigqmt/status_map.py`** —— 复用 `order_status.py` SSOT（§4.2）；
2. **不做 `BigQmtBridgeAdapter(BrokerAdapter)` 平行类** —— 改为 dialect + transport 组合（§3.1），避免第 3 套抽象；
3. **回调 diff 合成不写在 adapter 内** —— 归入 `EventPort` 实现并声明 `POLL_DIFF` + 延迟上界（§4.4）。

---

## 10. 落地进度（P0 全部 + Phase 1/2/4 已实现，非纸面计划）

### 10.1 交付清单

| # | 交付 | 位置 | 状态 |
|---|---|---|---|
| **P0-a** | `OrderSnapshot` / `CanonicalEvent` / `EventSemantics(Spec)` / `Capability(Set)` / `CapabilityLevel` + **`EventPort`**、**`CapabilityProbePort`**；`CANONICAL_PORTS` 不动、新增 `EXTENDED_PORTS` | `connectors/ports.py` | ✅ |
| **P0-a** | 方言 → canonical 转换层（状态过 SSOT、`accepted` 双判据、畸形输入诚实返回） | `connectors/canonicalize.py`（新） | ✅ |
| **P0-b** | 端口异步口径统一（查询类全部 `async`；`is_connected` 刻意保持同步并钉死「O(1) 无 IO」） | `connectors/ports.py` + `connectors/qmt.py` | ✅ |
| **P0-d** | canonical 返回值 + `get_kline` 补 `start/end/adjust` | `connectors/qmt.py`、`generic.py` | ✅ |
| **P0-e** | `client_order_id` 透传（写进 remark，回读侧可还原） | `connectors/qmt.py::_with_cid`、`dialects/xtquant_v1.py::_extract_cid` | ✅ |
| **P0-c** | 灰度接线：`QMT_USE_PORTS=1` 走端口层，**默认 0 = 行为与接线前一致**；装配失败**显式报错不静默回退** | `gateway/execution.py::_port_for_bridge` | ✅ |
| **Phase 1** | `Transport` 抽象 + `Dialect` 抽象 + `GenericConnector` + `ConnectorRegistry` | `connectors/transport.py`、`dialects/`、`transports/`、`generic.py`、`registry.py` | ✅ |
| **Phase 2** | 大 QMT 方言 + 文件桥 transport + 差分事件合成 + 延迟上界契约 + agent 包 | `dialects/bigqmt_v1.py`、`transports/file_signal.py`、`events.py`、`transports/semantics.py`、`agent_bigqmt/` | ✅ |
| **Phase 3** | redis / zmq transport（延迟导入第三方库，缺失时报明确指引） | `transports/lowlatency.py` | ✅ |
| **Phase 4** | PtradeV1 接入模板 + HTTP 网关 transport + EasyTrader façade（无逃逸口） | `dialects/ptrade_v1.py`、`transports/http_gateway.py`、`gateway/easytrader_facade.py` | ✅ |
| **门禁** | `agent_bigqmt` py3.6 闸门（AST：语法 + 禁用特性 + 入口防手抄） | `scripts/check_bigqmt_agent_py36.py` | ✅ |

### 10.2 组合表已可实例化（全部由配置产生，无一个手写类）

| key | Dialect | Transport | 状态 |
|---|---|---|---|
| `qmt.mini` | xtquant.v1 | inprocess | ✅ 可用（生产主通道） |
| `qmt.big.direct` | xtquant.v1 | inprocess | ✅ 可用（路径 A） |
| `qmt.big.bridge.file` | bigqmt.v1 | file | ✅ 可用（路径 B 默认，端到端已测） |
| `qmt.big.bridge.redis` | bigqmt.v1 | redis | ✅ 可用（需 `pip install redis`） |
| `qmt.big.bridge.zmq` | bigqmt.v1 | zmq | ✅ 可用（需 `pip install pyzmq`） |
| `ptrade.http` | ptrade.v1 | http | ✅ 骨架（字段口径待实机核对） |

### 10.3 落地过程中捕获并修掉的真实缺陷

| # | 问题 | 处置 |
|---|---|---|
| **B1** | **56 的语义冲突**：xtquant 里 56 = 全部成交，大 QMT 文档里 56 被写成「已拒」 | 事件合成**禁止硬编码整数**，一律交 `order_status.normalize_order_status` 裁决；agent 侧只如实上报原始状态，判定权归 SSOT（否则会把真实成交判成拒单） |
| **B2** | 新增端口若并入既有 Port 会破坏 `test_connector_ports.py` 的 `runtime_checkable` 断言 | 新端口一律独立 Protocol + 单列 `EXTENDED_PORTS` |
| **B3** | 灰度期「端口装配失败 → 静默走旧路径」会造出假绿灯 | 开关为 1 时装配失败直接抛错，只有显式置 0 才回旧路径 |
| **B4** | `asyncio.to_thread` 的线程非 daemon，可能让进程退不出去（TD-25） | InProcess transport 复用既有 `XTQuantBridge` 线程池，**不引入新线程语义** |
| **B5** | 静态护栏用文本匹配会误伤「写着禁止事项的 docstring」 | 闸门脚本走 AST，剥掉字符串后判定（与 TD-25 教训一致） |

> **为什么按 P0-a → P0-b → P0-c 分步**：`ExecutionPort.get_orders/get_deals` 原本是同步方法直调同步 SDK，直接接线会把同步调用跑进事件循环。先把「返回值形状 + 状态归一 + 异步口径」钉死，接线时只替换调用目标，风险面最小。

### 10.4 运维启用指引

**（1）灰度接线（默认关闭，行为与接线前完全一致）**

```bash
# 后端环境变量；置 1 后下单/撤单走 ports → Dialect×Transport
QMT_USE_PORTS=1
```

验证：日志出现 `[ports] order_id=...` 即走了端口路径；置 1 后若装配失败会**直接报错**
（不会悄悄回退）。回退方式只有一种：置回 `0`。

**（2）大 QMT 文件桥（`qmt.big.bridge.file`）**

```python
from connectors.registry import resolve

conn = resolve("qmt.big.bridge.file", bridge_dir=r"C:/Users/me/qmt_work/bigqmt_bridge",
               auth_token="<同 agent 侧>")
await conn.test_connection()        # PROBE：拿 agent 元数据与捕获到的函数清单
caps = await conn.probe_capabilities()
spec = conn.event_semantics()       # POLL_DIFF + max_latency_ms=1500
```

agent 侧部署见 `backend/agent_bigqmt/DEPLOY.md`。

**（3）切换到低延迟通道**（协议不变，只换 transport）

```bash
pip install redis      # 或 pyzmq
```

然后用 `qmt.big.bridge.redis` / `qmt.big.bridge.zmq`，方言代码零改动。

**（4）旧脚本迁移**（easytrader 心智，但过平台的风控/幂等/TOTP/WAL/审计）

```python
from gateway.easytrader_facade import use

user = use("qmt.mini", adapter=my_adapter)
await user.buy("600036.SH", price=35.5, amount=100)   # → SignalRouter.submit
await user.position()                                  # → canonical 快照
```

⚠️ `user.raw` 只提供**只读**句柄；它没有下单出口 —— 这是刻意的，绕不开就是绕不开。

### 10.5 验证结果

| 项 | 结果 |
|---|---|
| 新增/改动用例 | 4 个新测试文件共 **49** 用例全绿（canonicalize 12 / 文件桥 16 / 灰度双跑 6 / 组合装配 15） |
| 既有连接器测试 | `test_connector_ports`、`test_connectors_phase8`、`test_supervisor_reconnect` 无回归 |
| 契约核对 | `scripts/ci_reconcile.py`：`EXPECTED_TESTS=1891`（166 个 `test_*.py`）/ 组件 72 / 页 43 / README 计数 / 行尾 —— **RECONCILE OK** |
| 静态检查 | `ruff --select=F,E9` 全仓 **All checks passed** |
| 架构护栏 | `check_execution_architecture` OK；`check_api_contract_drift` 参数级通过；`check_capability_coverage` 无新增未覆盖 |
| agent 闸门 | `scripts/check_bigqmt_agent_py36.py` **OK** |

### 10.6 大小 QMT 接口对齐升级小结（本轮）

> 触发：用户指出「大小 QMT 存在接口差异，需要对齐完善，一起完善改进，全面升级」。
> 以下 6 项均为**真实未对齐点**（非纸面补全），每项都配套了测试与零 mock 纪律校验。

| # | 未对齐点 | 根因 | 处置 | 验证 |
|---|---|---|---|---|
| **A1** | `EventPort` 对文件桥 / 进程内传输均返回空，事件订阅形同虚设 | `stream_events()` 只在 inprocess 实现，其余传输缺实现 | ① inprocess 绑定 adapter 回调 + `PushEventSource` 推送；② file 桥 `_drain_events`（**字节偏移游标**，2026-10-01 由 `_read_events_since` 的 seq 水位改来 —— 见 A13）增量 → `canonical_from_agent_event` 翻译为 `CanonicalEvent`（只翻译不二次 diff）；③ redis/zmq `stream_events()` 诚实返回 `[]`（pub/sub 未实现，不伪造） | `test_inprocess_stream_events_returns_pushed`、`test_file_bridge_stream_events_emits_canonical` |
| **A2** | `subscribe_quote` 跨线把 Python `callable on_tick` 丢进 `WireRequest` → JSON 序列化崩溃 | **回调**无法越过 JSON 线（进程内对象）。跨线传输只剩**意图式订阅**一条路：`want_quotes()` 记意图 → `SUB_QUOTE` 下发 → agent 侧 `get_full_tick` 轮询差分 → 写成 `quote` 事件进 `events.ndjson` → 外部端翻译 | transport 新增 `supports_callback` 标志（inprocess=True，其余 False）；`GenericConnector.subscribe_quote` 跨线传输抛 `UnsupportedOp`（并引导改用意图式订阅） | `test_subscribe_quote_rejected_on_wire_transport`（两处） |
| **A3** | `probe_capabilities` 仅回显静态声明，能力状态失真 | 能力应是**运行时真探**结果，不是写死的常量 | 重写：调 `test_connection()` 拿 agent 上报 `captured` 函数清单 → 经 `_FUNC_TO_CAPS` 映射为 SUPPORTED；实时能力据 `event_semantics()` 推导（PUSH→SUPPORTED，POLL_DIFF→DEGRADED）；未捕获项诚实 **UNKNOWN**，禁止据单点探测断言终端能力 | `test_capability_probe_marks_unverified_as_unknown`（realtime 现标 DEGRADED） |
| **A4** | 大 QMT 资产映射 `m_dBalance` 冲突：在 QMT 是总资产，却被当可用资金 → 风控误放行真单 | `cash` 候选集误含 `m_dBalance` | `_ASSET_KEYS["cash"]` 移除 `m_dBalance`（保留 `cash/m_dAvailable/available`）；总资产仅走 `assets` 候选集 | 既有资产测试无回归 |
| **A5** | 写操作串行化靠 adapter 方法名白名单匹配，脆弱易漏 | 串行化逻辑与传输层耦合，新增写 op 须改两处 | `WireRequest` 增 `write: bool`；`dialects/base.py` 新增唯一判定 `is_write_op(op)`（`PLACE_ORDER/CANCEL_ORDER/SUBSCRIBE_QUOTE`）；`_call` 自动置 `write`，Transport 据此选串行/并发 | 组合装配测试无回归 |
| **A6** | 能力声明 `realtime` 误标 SUPPORTED（文件桥实际是 POLL_DIFF，有轮询延迟） | 静态声明未结合传输语义 | 实时能力一律由 `event_semantics()` 推导，文件桥 → DEGRADED（`max_latency_ms=1500`） | 探针测试断言 realtime=DEGRADED |

**不变量守护**：A1~A6 全程未触碰 INV-1/2/3；A3 的 UNKNOWN 一等公民与零 mock 纪律（业务失败→400 / 传输故障→503）保持一致；A4 的修正直接堵住「假绿灯放行真单」的合规风险。

**测试增量**：自 §10.5 基线以来（R40 两轮 + R41 复核）使全仓 `EXPECTED_TESTS` 由 1757 增至 **1922**、`test_*.py` 由 158 增至 **167**（已在 `scripts/ci_reconcile.py` 与 `README.md` 两处同步）。新增/强化集中在 5 个文件：

| 文件 | 用例数 | 本轮新增覆盖 |
|---|---|---|
| `test_bigqmt_file_bridge.py` | **24**（原 19） | agent 重启 seq 回卷 / 轮转到**更大**文件 / 事件文件删除后重建 / 半行不吃且只投一次 / 两次 drain 间 700 条突发零丢失 |
| `test_bigqmt_relay_redis.py` | **7**（原 5） | redis 侧会话回卷检测；拐点重放（不回退到 0，避免旧会话高 seq 记录倒灌） |
| `test_bigqmt_bridge_face.py` | **33**（原 20） | ① 网关相对 mini 契约的**唯一加宽**（`subscribe_quote(period)`）声明化对账 + AST 反向断言（调用点不得把 `period` 漏给网关）；② §6 **wire action 词表五方 AST 对账**（dialect `_OP_TO_ACTION` / `Ops` / agent `_ACTIONS` / agent `execute()` 派发分支 / fake agent，双向集合差必须为空）；③ §7 **存活判据** 7 例（90s 空闲才探、5s 短超时、30s 退避、**连续 2 次**失败才判死、一次成功往返即清旗、`is_connected()` 随之转 False、以及**事件泵确实调用探活**的接线断言） |
| `test_metrics_wiring.py` | **8**（新增） | 指标「生产者缺失」护栏（TD-27）：从 `Metrics` 反射出全部 `record_*`，在**排除 `tests/`** 的产线代码里逐个核对调用点；反向用 `render()` 的**真实输出**核对每个被渲染的 `qmt_*` 都有生产者；另有自证用例防扫描器静默失效 |
| `test_db_backup_policy.py` | **53**（原 46） | WAL 指纹形态可比性 7 条：降级↔归一互认、`_shape()` 排除非形态键（`norm`/`pre_wal`）、真写入仍备份、纯 checkpoint 不破 skip 链 |

> **A14 顺带修掉的放大器**：该文件原有的 `_live_db_backup` fixture 直指 1.4 GB 生产库且 `keep=10`，
> 单文件一轮就要写出约 **5.6 GB** 备份（正是「磁盘满 → checkpoint 降级 → 偶发为红」的元凶）。
> 已改为 tmp 路径的真 `DB` 实例（仍保持「同一个库」的语义），单次备份体积 1.4 GB → **420 KB**。

---

## 11. 实施路线图

> **状态（2026-10-01）**：Phase 0 全部、Phase 1、Phase 2（含 agent + 闸门）、
> Phase 3 传输实现、Phase 4 模板与 façade **均已落地**（详见 §10）。
> 下表保留为**验收口径与剩余工作清单**；带 ✅ 的是已通过验收的项。

### Phase 0 —— 接线既有 Port（让已死资产活起来）

| # | 任务 | DoD | 回滚 |
|---|---|---|---|
| 0-a | canonical 模型 + 转换层 + 用例 | **已完成**（§10） | 纯增量，删文件即可回退 |
| 0-b | **异步口径统一**：`ExecutionPort/AccountPort/PositionsPort/MarketDataPort` 的同步方法改 `async`，内部 offload 到线程池；或在 port docstring 钉死「实现方必须自行 offload」（推荐前者） | 无同步 SDK 调用出现在 `async def` 路径上；`QMT_BRIDGE_CALL_TIMEOUT` 语义不变 ✅ | 仅端口层，adapter 签名不动 |
| 0-c | **灰度接线**：`ExecutionService` 改为持有 `ExecutionPort`，功能开关 `QMT_USE_PORTS=0/1` 双跑对比 | 双跑结果一致（含错误语义）；全量 1753 用例不回归；`check_execution_architecture` 无违规 ✅ | 开关置 0 即回旧路径 |
| 0-d | `MarketDataPort.get_kline` 补 `start/end`；`QmtConnector` 返回 canonical 快照（关闭缺口 D1/D4） | `_BoundBrokerSource` 日期区间取数能力经端口可达 ✅ | 同上 |
| 0-e | `client_order_id` 透传（关闭缺口 D3）：映射到 `remark` / 外部映射表 | 同一 `client_order_id` 二次提交被引擎幂等拦下（第二道防线成立） ✅ | 同上 |

### Phase 1 —— 正交拆分（Transport × Dialect）

| # | 任务 | DoD |
|---|---|---|
| 1-a | 抽 `Transport` 接口 + `InProcess`（包装 `XTQuantBridge.call/call_locked`）+ `SubprocessBridge`（复用 `bridge_client/bridge_server`） ✅ | `qmt.mini` 经组合方式与旧行为一致（含超时/并发锁定） |
| 1-b | `XtQuantV1` dialect 从 `XTPQuantAdapter` 剥离 ✅ | `qmt.mini` 与 `qmt.big.direct` 仅差 transport/数据目录，**无重复动词代码** |
| 1-c | `ConnectorRegistry.resolve("qmt.big.direct")` ✅ | Phase 0 的路径 A 可经 registry 实例化 |

### Phase 2 —— 大 QMT 桥接 MVP（EventPort + 能力协商）

| # | 任务 | DoD |
|---|---|---|
| 2-a | `BigQmtV1` dialect + `FileSignal` transport + `agent_bigqmt/`（**py3.6 兼容**，标准库 only） ✅ | `fake_bigqmt_agent.py` 全链路：正常 / 拒单(-1→400) / agent 不可达(503) / 废单 57→order_error / signal_id 幂等 / token 校验失败 |
| 2-b | `EventPort` 实现：轮询 diff 合成，`POLL_DIFF` + `max_latency_ms=1000` ✅ | 合成事件与推送**同形**；`order_watchdog` 按上界放宽容忍，无误撤 |
| 2-c | `CapabilityProbePort` 实现 + probe 结果进 `/api/v1/broker/health` + 前端诊断面板 ⚠️ 部分（**探针真探已落地**，面板待做） | 运行时真探：据 agent 上报注入函数清单 + 事件语义推导 realtime，未验证项诚实 UNKNOWN（无误导性断言）；前端诊断面板待做 |
| 2-d | AST 防手抄闸（入口文件函数名字面量 >5 即红） ✅ | `agent_bigqmt/BIGQMT_AGENT.py` 通过（继 `xtquant_big_convert` 教训） |

### Phase 3 —— 低延迟传输 + 部署体验

| # | 任务 | DoD |
|---|---|---|
| 3-a | `RedisRPC` / `ZmqRPC` transport ✅ | 与 file 走**同一套** dialect 测试（仅换 transport 参数） |
| 3-b | 一键部署：agent 包生成 + 路径两端一致性校验（`PROBE` 内做） ⚠️ 部分（DEPLOY.md 已给路径一致性排障表，向导 UI 待做） | 部署向导端到端走通；`trading_enabled` 默认关闭 |

### Phase 4 —— 未来客户端

| # | 任务 | DoD |
|---|---|---|
| 4-a | `PtradeV1` dialect（复用 `HttpTradingConnector` 形态） ✅ | 新增客户端**改动集中在一个 dialect 文件**，编排层零改动 |
| 4-b | EasyTrader 兼容 façade（§8，受控 + 可审计） ✅ | 旧脚本可迁移，且**无**绕过 SignalRouter 的路径 |
| 4-c | 第三方 Broker Plugin 生态（描述符 + 打包约定） ⬜ 未开始 | `docs/BROKER_ONBOARDING.md` 更新为 V4 口径 |

### 跨阶段门禁（每阶段结束必做）

1. `pytest` **逐文件跑** + `CODEBUDDY_SAFE_DELETE_ENABLED=0` + `--basetemp` 放 OS 临时目录；`QMT_DB_BACKUP_ENABLED=0`；汇总用 JUnit XML（**不信 shell 重定向**）；
2. `scripts/ci_reconcile.py` **必须后台跑**（前台必被环境 SIGTERM）；重算 `EXPECTED_TESTS` + README 两处计数；
3. `check_execution_architecture.py` / `check_api_contract_drift.py` / `check_capability_coverage.py` / `test_emit_event.py` 全绿；
4. 前端 `npm run test:serial`（并行 worker 会**静默丢文件**而汇总仍全绿）；
5. 新文件进 `.gitattributes` 行尾门禁；`agent_bigqmt/` 单独 py36 lint：
   ```bash
   python scripts/check_bigqmt_agent_py36.py   # py3.6 语法 + 禁用特性 + 入口防手抄
   ```
   ★ 该闸门走 **AST** 判定：文本匹配（`":=" in src`）会误伤「docstring 里写着禁止 walrus」
   的说明文字本身 —— 这正是 TD-25 记下的静态护栏教训。

---

## 12. 风险清单

| # | 风险 | 等级 | 对策 |
|---|---|---|---|
| R1 | 券商继续收紧（大 QMT 内置策略也受限） | 高 | 多通道共存（`qmt.mini`/`qmt.big.direct`/桥接）+ 未来 dialect 占位；**不要**把任一通道做成唯一路径 |
| R2 | 「接线」期间踩到已闭环的坑（连接去重、session 预算链 45s < ready 100s < 前端 120s） | 高 | 预算链**一律不动**；supervisor 退避上限与 `BrokerError.needs_action` 联动不在本轮调整；`QMT_USE_PORTS` 灰度开关随时回退 |
| R3 | **假绿灯**（被护栏 bug 或另一个 bug 互相遮盖，TD-23/24 教训） | 高 | 验收以**契约门禁 + 后台日志**为准，不只看 pytest 汇总；每阶段末尾做一次「故意注入故障」的反向验证 |
| R4 | 同步 SDK 调用进事件循环 | 高 | P0-b 必须先于 P0-c 完成；端口层 docstring 钉死 offload 要求 |
| R5 | 新增第 N 套平行实现（违反 INV-3） | 中 | AST/import 门禁：同一概念只允许一处实现；每 phase review 检查是否新建 manager/mapping |
| R6 | 大 QMT 内置 py3.6 版本碎片 | 中 | agent 标准库 only；PROBE 上报版本；独立 AST 兼容闸 |
| R7 | 注入函数跨客户端版本漂移 | 中 | 唯一来源捕获 `capture_qmt_injected_funcs(globals())` + 实调 probe + `UNKNOWN` 兜底；诊断禁绝对断言 |
| R8 | 桥目录被第三方进程写入 | 中 | token 校验 + 目录 ACL + `trading_enabled` 默认关闭 |
| R9 | POLL_DIFF 延迟导致误撤单 | 中 | `max_latency_ms` 进 `order_watchdog` 阈值推导；file 传输明确标注不适合打板/条件单 |
| R10 | 打包态写运行期文件（TD-26） | 中 | 运行期文件一律跟随 `settings.db_path.parent`；`exe_dir()` 只用于只读资源；`purge_dist_runtime_state` 已内置于构建脚本 |
| R11 | 测试计数漂移导致 CI 红 | 低 | 每阶段后台跑 `ci_reconcile` 并重算；README 两处计数同步 |

---

## 13. 决策记录（ADR 摘要）

| ADR | 决策 | 被否决的选项 | 理由 |
|---|---|---|---|
| ADR-1 | 采用方案 C（Ports 接线 + 正交拆分 + EventPort + 运行时协商） | A 扁平 API(2.14) / B 只接线(3.30) / D 全 Sidecar(3.24) / E CCXT 式(3.38) / F 表驱动 DSL(2.62) | 加权 4.62 唯一在「不变量强制」与「新客户端线性成本」双满；详见 `UNIFIED_TRADING_ABSTRACTION.md` §4 |
| ADR-2 | 状态不新建 SSOT，复用 `order_status.py` | 新建 `bigqmt/status_map.py` | 同一整数族群（三方互证）；新建会重演三处映射的历史根因 |
| ADR-3 | 不新建顶层 `broker/` 包 | V3 的 `broker/adapters/` | 既有 `connectors/` 就是该位置；新包 = 第四个客户端抽象（INV-3） |
| ADR-4 | Runtime Manager 落到既有 `ConnectionSupervisor` + `probe_environment` | 新建 `runtime/` 包 | 防止第二套生命周期（INV-4） |
| ADR-5 | 先 canonical 模型、再异步口径、最后灰度接线 | 直接接线（V3 Phase 1→2） | 同步方法接线即事故；分步可灰度可回滚 |
| ADR-6 | EasyTrader 兼容层定在 Ports 之下，禁止无痕逃逸口 | easytrader 式 `user.trader` | INV-1；诚实但必须可审计（`CallNativePort` 过 WAL） |
| ADR-7 | 明确排除 UI 自动化客户端路线 | easytrader 同花顺 pywinauto 路线 | 脆弱、不可观测，过不了零 mock 验收 |

---

## 附录 A：V3 → V4 逐条对照

| V3 章节 | V3 内容 | V4 处置 |
|---|---|---|
| §1 项目定位 | Universal Securities Broker Runtime Platform | ✅ 继承，作为目标定位 |
| §2 总体架构 | 五层图 | ✅ 升级为**六层**（加入 Orchestration 层，明确不可绕过） |
| §3 Broker Core 抽象 | 9 动词 | ♻️ 下沉为 **12 个 Port**，动词保留在上层 façade |
| §4 统一领域模型 | 裸字段列举 | ♻️ canonical frozen dataclass + `.validate()` + status SSOT + `raw` |
| §5 Mini/BigQMT | BigQMT 用 Agent 隔离 | ✅ 保留优势（Python/DLL/多版本/崩溃隔离）；♻️ 明确「异步 + Transport 可插拔 + 进程内直连不被强制淘汰」（全 Sidecar 方案的 W8 教训） |
| §6 Runtime Manager | 四个文件名 | ♻️ 落到既有 `ConnectionSupervisor` / `probe_environment` / `gateway/health.py`，**不新建包** |
| §7 Adapter 插件体系 | 按客户端平铺目录 | ❌ 替换为 **Dialect × Transport 正交组合** |
| §8 Capability | `features: {bool}` | ❌ 替换为**四级运行时协商** + reason + probed_at |
| §9 多账户路由 | 一张图 | ➕ 补 AccountRoute 解析 / 单连接隔离 / `qmt_only` 不静默降级 |
| §10 EasyTrader 兼容 | 悬空一层 | ➕ 定性为 Ports 之下 façade + 受控逃生舱（WAL+审计） |
| §11 扩展方向 | 列举 | ✅ 继承，纳入 Phase 4 |
| §12 实施路线 | Phase 1-5 无判据 | ♻️ 重排 Phase 0-4，每步带 DoD + 回滚 + 门禁 |
| §13 最终目标 | CCXT 思想 + easytrader 兼容 + 现代插件架构 | ✅ 继承；➕ 追加第八条原则：**先有不可绕过的不变量，才有可插拔的插件** |

## 附录 B：文档索引

| 文档 | 角色 | 状态 |
|---|---|---|
| `docs/QMT_UNIVERSAL_BROKER_PLATFORM_ARCHITECTURE_V3.md` | 愿景稿 | 已删除（内容并入本文） |
| `docs/QMT_大小版本使用说明.md` | 大小 QMT 差异与逐步操作说明 | 有效（本文架构的用户侧展开） |
| `docs/UNIFIED_TRADING_ABSTRACTION.md` | 选型论证（六方案加权） | 有效（论证部分） |
| `docs/BIG_QMT_COMPAT_PLAN.md` | 首个落地案例（大 QMT） | 有效（实施细则），其 4 处设计已被修正 §9 |
| **本文** | **执行口径** | **V4.0 终稿** |
