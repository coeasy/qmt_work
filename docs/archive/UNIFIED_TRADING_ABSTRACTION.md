# 统一交易接口抽象：可行性论证与方案横向对比

> 版本：v1.0（2026-10-01）
> **执行口径见 `docs/UNIVERSAL_BROKER_PLATFORM_FINAL_PLAN_V4.md`**（本文第 6 节的落地路径已被 V4 Phase 0-4 取代；本文的价值保留为「六方案加权对比」的选型论证）。
> 姊妹文档：`docs/BIG_QMT_COMPAT_PLAN.md`（大 QMT 兼容实施方案，本文第 7 节给出对它的 4 处修正）
> 问题：是否可以像 easytrader 一样抽象一套统一接口，让大小 QMT 及未来客户端共用同一套下单逻辑？是否存在更好的方案？

---

## 0. 结论先行

**能，而且不需要新建——qmt_work 已经有了两层统一抽象，其中一层还没接线。**

| 已有资产 | 位置 | 状态 |
|---|---|---|
| **统一「下单逻辑」层**（风控/幂等/TOTP/WAL/审计/通知） | `gateway/signal_router.py::SignalRouter.submit` | ✅ 已在产线，已是客户端无关的唯一入口 |
| **统一「客户端契约」层**（9 窄接口 + canonical dataclass + 状态机 + 能力协商 + supervisor） | `connectors/ports.py` / `supervisor.py` / `qmt.py` / `http.py` | ⚠️ **未接线**，自陈「端口形同虚设」 |

所以真正的命题不是「要不要引入 easytrader 式抽象」，而是三选一：

1. 引入第三套抽象（easytrader 风格扁平 API）→ **不建议**：制造第三层冗余；
2. 把已有的 `connectors` 端口层接线启用 → **必要，但不充分**；
3. **接线 + 把 Transport（传输）× Dialect（方言）正交拆开 + 补齐 EventPort/运行时能力协商/canonical 快照** → **推荐（方案 C）**。

一句话概括推荐差异：**easytrader 统一的是「动词」（buy/sell/position），qmt_work 需要统一的是「契约 + 生命周期 + 能力」——因为 qmt_work 有「SignalRouter 唯一入口」「零 mock」「失败绝不包 code=0」这类不可绕过的不变量，动词级抽象兜不住它们。**

---

## 1. 现状解剖：抽象已经有了，问题是它在空调房里

### 1.1 已在线的那层：SignalRouter 已经是「统一下单逻辑」

`backend/gateway/signal_router.py` 的 `_live()`（L450-523）已经做到了客户端无关：

```
SignalRouter.submit()          ← 唯一交易入口（所有引擎/手动/批量都经此）
  ├─ route(): 幂等(single_flight) + 市价估价 + 风控 + 大额二次确认(TOTP)
  ├─ _paper(): PaperEngine 旁路
  └─ _live(): WAL intent(下单前落盘) → ExecutionService → WAL result → 审计/通知/emit
```

这条提交路径上承载的东西（风控 Mandatory、幂等、TOTP、WAL 前写日志、审计、WS 事件、失败不粉饰）**全部与客户端无关**。
也就是说：**「类似下单逻辑」这件事已经被统一了**，miniQMT 与大 QMT 共用同一套逻辑。

### 1.2 没接线的那层：canonical ports（比 easytrader 强，但是死的）

`backend/connectors/ports.py` 已经定义了完整的面向接口编程体系：

| 资产 | 内容 | 评价 |
|---|---|---|
| `OrderRequest`（frozen dataclass，L48-68） | `InstrumentId` / side / order_type / price / quantity / client_order_id / **`.validate()`** | 强类型契约 + 入参自检 |
| `InstrumentId.canonical`（L44-45） | `600036` + `SH` → `600036.SH` | 消除大小写/后缀漂移 |
| 9 个窄接口（L112-194） | `ExecutionPort` / `AccountPort` / `PositionsPort` / `MarketDataPort` / `QuoteFeedPort` / `InstrumentPort` / `CalendarPort` / `HealthPort` / `CapabilityPort` | 严格 ISP，连接器可只实现部分 |
| `runtime_checkable` Protocol | 结构化契约测试（` isinstance` 直接校验，不需要 mock） | 与「零 mock」硬约束天然契合 |
| `ConnectorState`（L18-24） | DISCONNECTED/STARTING/CONNECTED/**DEGRADED**/STOPPING/FAILED | 六态 FSM，能表达「连上了但降级」 |
| `ConnectionSupervisor`（supervisor.py） | 注册/启停 + **健康探测循环 + 指数退避自动重连 + 尊重人工停机 + 单连接故障隔离** | easytrader 完全没有的一层 |
| `HttpTradingConnector`（http.py） | 第二个实现样板（远端 http 网关，健康检通过才收单） | 多客户端可扩展性的活样本 |

**但是** `backend/connectors/__init__.py` 的 docstring 自己写死了状态（原文摘录 L3-19）：

> ⚠️ **当前未接线（NOT WIRED）** …… 全仓**零产线引用**（`app/` `core/` `datasource/` `engines/` `gateway/` … 均不 import 本包）；
> …… V9 审计已记录 ``ConnectorPort.place_order`` 未被 ``ExecutionService`` 接线 ——「端口形同虚设」。

而真实产线链路（榆）是：

```
SignalRouter._live()
  → ExecutionService.place_order(bridge, ...)          # 直接吃 XTQuantBridge 对象
    → bridge.call_locked(bridge.gateway.place_order, ...)   # xtquant_client/gateway.py
      → BrokerAdapter 实现(XTPQuantAdapter | BridgeAdapter)
```

**接线前必须先修的四处缺口（否则接线即事故）：**

| # | 缺陷 | 证据 | 接线后的后果 |
|---|---|---|---|
| D1 | canonical 快照类型定义了却没人用 | `ports.py` 有 `AccountSnapshot`/`PositionSnapshot`；`qmt.py::get_account`(L80) 直接 `dict(adapter.get_account())` 返回 raw dict | 端口层沦为「透传装饰器」，状态/字段名方言泄漏到上层 |
| D2 | **同步阻塞方法会阻塞事件循环** | `ExecutionPort.get_orders/get_deals` 声明为**同步**方法，`QmtConnector.get_orders`(L74) 直调 `adapter.get_orders()` | 现在没人调用所以没炸；一旦接线，同步 SDK 调用跑在 event loop 线程上 → 全站卡顿（现有 `XTQuantBridge` 用 4 worker 线程池正是为此） |
| D3 | `client_order_id` 在映射时被丢弃 | `OrderRequest.client_order_id` 存在，但 `QmtConnector.place_order`(L46-58) 只传 strategy_name/remark | 幂等键到不了柜台，对账缺锚点（SignalRouter 已有 idempotency，此处是第二道防线丢失） |
| D4 | 端口签名比 adapter 窄 | `MarketDataPort.get_kline(code, period, count, adjust)` 丢了 adapter 的 `start/end`；缺 `search_stocks`/`on_order`/`on_trade` | `_BoundBrokerSource` 的「唯一支持日期区间取 K 线」能力无法经端口暴露 |

---

## 2. easytrader 模型解剖（作为对照组）

### 2.1 它的统一面

```python
user = easytrader.use('miniqmt')            # 工厂
user.connect(miniqmt_path=..., stock_account=..., trader_callback=None)
user.balance          # [{'total_asset','market_value','cash','frozen_cash','account_type','account_id'}]
user.position         # [{'security','stock_code','volume','can_use_volume','open_price','market_value',...}]
user.today_entrusts   # [{'order_id','order_sysid','order_status':56,'order_status_name':'已成',...}]
user.today_trades     # [{'traded_id','traded_price','traded_volume',...}]
user.buy('600036', price=35.5, amount=100)            # {'entrust_no': 123456}
user.sell('600036', price=36.0, amount=100)
user.market_buy('600036', amount=100, ttype='对手方最优价格委托')
user.cancel_entrust(123456)                            # {'success': True, 'message': 'success'}
user.trader / user.account                             # ← 原始对象逃逸口
```

结构分五层：**use/connect 工厂 → 统一动词面 → 归一化 dict → raw 逃逸口（`user.trader`）→ 回调（仅部分客户端有）**。

### 2.2 值得借鉴的三点（确实该学）

1. **极低的心智门槛**：`buy(code, price, amount)` 三参数就下单，社区心智成本低；
2. **`use()` 工厂 + 可插拔客户端**：`'miniqmt' / 'ths' / 'xq'` 一个字符串切实现，这是新客户端扩展的好形态；
3. **承认抽象必然泄漏**：主动提供 `user.trader` 逃逸口，而不是假装 API 能覆盖 100% → 对应 qmt_work 应有**受控的逃生舱**（但要记录、要审计、要过风控）。

### 2.3 六个对 qmt_work 而言致命的结构性缺陷

| # | 缺陷 | 说明 | qmt_work 为何不能接受 |
|---|---|---|---|
| **E1** | **不变量在抽象之外** | 任何人可 `user.trader.order_stock(...)` 绕过 `buy()` | qmt_work 的硬约束是「`SignalRouter.submit` 唯一交易入口」「Mandatory Risk 不可绕过」。**扁平动词 API 天然无法强制不变量**——它不是「兜不住」，而是「设计上就允许绕过」 |
| **E2** | **无能力协商，只有最小公分母** | `user.position` 是 property，隐含「所有客户端都能给持仓」。实际同花顺 UI 自动化版能力残缺 → 只能运行期崩或静默 | 大 QMT 桥接的实际能力随券商/客户端版本漂移（注入函数缺失、传输降级）；必须**运行时协商**而非编写期假设 |
| **E3** | **dict 契约不可机器校验** | 返回 dict，键漂移只能靠文档 | qmt_work 有 1704 用例 + 契约基线 + `check_api_contract_drift`（已升级到路径+参数级）的双层校验；dict 会直接掉出这套护栏 |
| **E4** | **本土语义用中文字符串表达** | `ttype='对手方最优价格委托'`，且**沪/深可选值不同**（文档中明确列出两套枚举） | 编译期不可捕获，两边名单不一致时静默错单。应建模为带 market 约束的枚举 + 能力声明 |
| **E5** | **无生命周期状态机/健康退避** | `auto` 刷新、`heartbeat` 依赖具体 client 实现 | qmt_work 已有 `ConnectionSupervisor`（退避 + 隔离 + 尊重人工停机）与 `BrokerError.needs_action`（非自愈失败人类可读性），不应退回去 |
| **E6** | **回调面不均匀** | miniqmt 有 `XtQuantTraderCallback`，UI 自动化版没有 | 这正是大 QMT 桥接的痛点：**有的客户端靠推送、有的必须轮询 diff 合成**。抽象必须统一事件形态，而不是让上层区分「这个客户端有没有回调」 |

> 补充证据（顺带验证了我们已有的 SSOT 是对的）：easytrader 直接把 `order_status: 56 / order_status_name: '已成'`、`'已报'(50)`、`'废单'(57)` 原样抛给用户。
> 而 qmt_work 的 `backend/xtquant_client/order_status.py` **早已把这些整数码收敛成平台标准状态 SSOT**（48-57、86、255 → pending/partial/filled/cancelled/rejected/unknown，含 `_XTP_INT_STATUS` 与中英文混排 `_RAW_TO_STD`）。
> **即：easytrader 把方言暴露给用户，qmt_work 已经在做的事是在方言之上建 SSOT。**

---

## 3. 候选方案清单（6 个）

| 代号 | 方案 | 一句话 |
|---|---|---|
| **A** | easytrader 风格扁平 API | 新建一层 `broker.buy/sell/cancel/position/balance`，dict 进出 |
| **B** | 现状 ports 原样接线 | 只做「把 `ExecutionService` 改成依赖 `ExecutionPort`」这一件事 |
| **C** | **Ports 接线 + Transport×Dialect 正交 + EventPort + 运行时能力协商 + canonical 快照** | 推荐 |
| **D** | 全 Sidecar/Agent 化 | 每个客户端一律做成独立进程/RPC agent（把大 QMT 桥方案推到极致且唯一化） |
| **E** | CCXT 式最小公分母 + params 逃逸 + capability flags | 统一 API + `extra_params` 兜差异 + 能力标记位 |
| **F** | 表驱动 DSL 方言映射 | 用 schema 表描述「参数位置/取值变换」，运行时解释执行 |

### 3.1 方案 C 的核心洞察：客户端差异其实是**两个正交维度**

现在 `XTPQuantAdapter` / `BridgeAdapter` 把两件事耦在一个类里。大 QMT 进来后组合会爆炸：

| 维度 | 取值 |
|---|---|
| **Dialect（方言）**：说什么 | `XtQuantV1`（xtquant SDK verbs）、`BigQmtV1`（passorder/get_trade_detail_data/ContextInfo）、未来 `PtradeV1` / `ThsUiV1` |
| **Transport（传输）**：怎么送到 | `InProcess`（进程内 SDK）、`SubprocessBridge`（已是现有 `bridge_server/client` 行 JSON）、`FileSignal`（signal/result）、`RedisRPC`、`ZmqRPC`、`WinAuto`（UI 自动化） |

不拆分 ⇒ N×M 个类。拆分后：

| 连接器 | Dialect | Transport | 备注 |
|---|---|---|---|
| `qmt.mini` | XtQuantV1 | InProcess（ABI 不匹配时自动切 SubprocessBridge） | 现状已支持 |
| `qmt.big.direct` | XtQuantV1 | InProcess | 路径 A（client_mode=full） |
| `qmt.big.bridge.file` | BigQmtV1 | FileSignal | 路径 B 默认 |
| `qmt.big.bridge.redis` | BigQmtV1 | RedisRPC | Phase 3 |
| `qmt.big.bridge.zmq` | BigQmtV1 | ZmqRPC | Phase 3 |
| `ptrade.*` | PtradeV1 | InProcess / HttpSidecar | 未来 |

**3 种方言 × 4 种传输 → 6 个实际组合，全部由组合而非继承产生。** 每个新客户端只需新增「1 个 dialect」或「1 个 transport」。

---

## 4. 加权对比矩阵

权重按 qmt_work 的实际约束排序（**不变量不可绕过 = 第一权重**，因为这是平台的零 mock/合规底线）。

| 维度（权重） | A 扁平API | B 只接线 | **C 推荐** | D 全Sidecar | E CCXT式 | F 表驱动DSL |
|---|---|---|---|---|---|---|
| **W1 不变量不可绕过** (0.20) | 1 | 4 | **5** | 4 | 3 | 3 |
| **W2 新客户端增量成本** (0.18) | 3 | 3 | **5** | 3 | 4 | 4 |
| **W3 能力/差异表达力** (0.12) | 2 | 3 | **5** | 3 | 4 | 2 |
| **W4 契约可机器校验** (0.12) | 1 | 4 | **5** | 4 | 3 | 2 |
| **W5 事件/回调一致性** (0.10) | 1 | 2 | **5** | 4 | 3 | 2 |
| **W6 降级与多通道共存** (0.10) | 2 | 3 | **5** | 4 | 3 | 2 |
| **W7 迁移成本（分越高越省）** (0.10) | 4 | 3 | **2** | 1 | 3 | 1 |
| **W8 延迟与可用性** (0.08) | 5 | 4 | **4** | 2 | 4 | 4 |
| **加权总分** | **2.14** | **3.30** | **4.62** | **3.24** | **3.38** | **2.62** |

评分依据（摘关键项）：

- **A（2.14）**：W1=1 —— `user.trader` 逃逸口的存在本身就证明动词层兜不住不变量；W3/W5=1~2 —— 无能力协商、回调面不均匀。**这是「好用的库」，不是「能承载不变量的平台架构」。**
- **B（3.30）**：成本最低的正确方向，但 W2=3 —— 大 QMT 要在 file/redis/zmq 三种传输下写三遍 connector（或在一个类里塞 if 分支）；W5=2 —— 缺 `EventPort`，大 QMT 的轮询 diff 合成无处安放。
- **C（4.62）**：唯一同时在「不变量强制」与「新增客户端线性成本」上拿满的方案；W7=2 是唯一代价。
- **D（3.24）**：把 mini 也强制过 IPC 反而丢掉本来最快的进程内直连（W8=2），且需全量重写（W7=1）。它适合「只有 sidecar 一种形态」的团队，不适合 qmt_work 这种**必须同时支持进程内直连和异进程桥接**的混合场景。
- **E（3.38）**：比 A 强很多（有 capability flags 与 escape params），本质是「C 的弱化版」——把能力做成了**静态 bool 标记**而非**运行时协商协议**，在处理「大 QMT 注入函数随客户端版本漂移」时不够。可作为 C 的过渡态。
- **F（2.62）**：表面优雅，实际会在「市价保护价」「异步 seq→order_id 回执」「回调合成」这类**非线性分支**上崩掉，最终要么退回手写 hook（等于 C），要么变成无人敢改的 DSL 泥潭。且与项目「>50KB 文件拦截」「静态护栏多靠 CI 脚本」的门禁文化冲突。

**结论：C。** 落地节奏建议：**B → C**（先接线拿到收益，再做正交拆分），具体见第 6 节。

---

## 5. 推荐方案 C 详细设计

### 5.1 分层图

```
┌────────────────────────────────────────────────────────────────────┐
│ 应用 / REST / MCP / WS   (app/routes, mcp_server)                    │
├────────────────────────────────────────────────────────────────────┤
│ 编排层 Orchestration（客户端无关，唯一入口，不可绕过）                 │
│   SignalRouter.submit → 幂等(single_flight) / 风控(Mandatory) /      │
│   TOTP / WAL(intent→result) / 审计 / 通知 / emit                     │
├────────────────────────────────────────────────────────────────────┤
│ ★ 契约层 Canonical Ports（connectors/ports.py，唯一方言边界）          │
│   ExecutionPort  AccountPort  PositionsPort  MarketDataPort          │
│   QuoteFeedPort  InstrumentPort  CalendarPort  HealthPort            │
│   CapabilityPort(运行时协商)   EventPort(★新增)                       │
│   数据模型: OrderRequest/InstrumentId/AccountSnapshot/PositionSnapshot│
│            OrderSnapshot(★新增) + order_status.py SSOT 归一化         │
├────────────────────────────────────────────────────────────────────┤
│ 连接器层 Connector = Dialect（说什么） × Transport（怎么送）           │
│   Dialect:   XtQuantV1  BigQmtV1  (PtradeV1/ThsUiV1 …)               │
│   Transport: InProcess  SubprocessBridge  FileSignal  Redis  Zmq     │
├────────────────────────────────────────────────────────────────────┤
│ 生命周期: ConnectionSupervisor（六态 FSM + 健康探测 + 指数退避 + 隔离） │
└────────────────────────────────────────────────────────────────────┘
```

### 5.2 端口补全清单（代码级）

**(1) `OrderSnapshot` / `OrderResult` —— canonical 返回值（对应缺口 D1）**

```python
@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str          # 幂等锚点（对应 D3，必须透传）
    broker_order_id: str = ""     # 柜台委托号；未确认时为 ""
    status: str = UNKNOWN         # ← 必须是 order_status.py 的 SSOT 词表，禁止原始串
    filled_quantity: int = 0
    avg_price: float = 0.0
    raw_status: str = ""          # 保留原始值仅用于诊断，不参与判定
```

**(2) `EventPort` —— 事件形态统一（对应 easytrader 缺陷 E6）**

```python
@runtime_checkable
class EventPort(Protocol):
    """委托/成交事件流。source 是 push 还是 poll-diff 合成，对上层完全同形。"""
    async def stream_events(self) -> AsyncIterator[CanonicalEvent]: ...
    def event_semantics(self) -> EventSemantics: ...   # PUSH | PUSH_WITH_GAP | POLL_DIFF
```

- `qmt.mini`：原生 `XtQuantTraderCallback` → `PUSH`；
- `qmt.big.bridge`：轮询 diff 合成 → `POLL_DIFF`，**且必须声明合成延迟上界**（file 传输 1s / redis 50ms），上层据此决定「是否可依赖事件做超时守护」；
- 废单（57）/拒单 → 一律合成为 `order_error` 事件，而非静默丢弃。

**(3) `CapabilityPort` 升级为运行时协商（对应缺口 D2/E2）**

```python
@runtime_checkable
class CapabilityPort(Protocol):
    async def probe_capabilities(self) -> CapabilitySet: ...
    # CapabilitySet: {capability: SUPPORTED|UNSUPPORTED|DEGRADED} + reason + probed_at
```

`qmt.big.bridge` 的 probe 必须包含：注入函数清单、传输可达性、transport 实际降级路径、大窗口/小窗口行情端口可用性。**严格遵守「不得据单点探测断言终端能力」的纪律**（见姊妹文档 §5.2.5）。

**(4) 异步口径统一（对应缺口 D2 的高危项）**

> ⚠️ `QmtConnector.get_orders/get_deals/get_account/...` 目前是**同步方法直调同步 SDK**。
> 现在因为没人调用所以安全；**一旦接线，这些同步 SDK 调用会跑在 asyncio 事件循环线程上**。
> 处置：端口层的同步方法一律改为 `async`（由 connector 内部把调用丢给已有线程池），或显式在端口 docstring 钉死「实现方必须自行 offload」——二者选一，**不能默认**（推荐前者，可用现有 `XTQuantBridge` 的 worker 池实现）。

**(5) `client_order_id` 透传（对应缺口 D3）**：`OrderRequest.client_order_id` → dialect 映射到各自客户端的 remark/order_remark 或外部映射表，保证对账与幂等二道防线成立。

**(6) `get_kline` 端口签名补 `start/end`**：恢复 `_BoundBrokerSource` 「唯一支持日期区间取 K 线」的能力暴露面。

### 5.3 方言回合表（新增客户端只需填表）

| canonical 概念 | XtQuantV1（mini / big direct） | BigQmtV1（桥接） |
|---|---|---|
| 下单 | `XtQuantTrader.order_stock(acc, code, op, vol, price_type, price, ...)` | `passorder(...)`（注入全局函数） |
| 异步回执 | `order_stock` 返回 seq → `on_order_stock_async_response(seq,result)` → `_wait_order_response` 换真实 id | `passorder` 同步返回；真实 id 靠 `get_trade_detail_data("ORDER")` diff 补齐 |
| 撤单 | `cancel_order_stock` 返回 0/-1（**必须捕获**） | `cancel(...)` 返回布尔；失败文案需归一为 `BrokerError` |
| 资金 | `query_stock_asset` | `get_trade_detail_data("ACCOUNT", ...)` |
| 持仓 / 委托 / 成交 | `query_stock_positions/orders/trades` | 同系列 `get_trade_detail_data("POSITION"/"ORDER"/"DEAL")` |
| 状态原始值 | `XtOrderResponse.OrderStatus` int（48-57/86/255） | `m_nOrderStatus` int（**同一族群**） |
| → 归一化 | `order_status.py::normalize_order_status` | **复用同一 SSOT**（无需新建映射表） |
| 实时行情 | `xtdata.get_full_tick`（需 miniquote 58610） | `ContextInfo.get_full_tick` |
| 事件来源 | 原生回调（PUSH） | 轮询 diff（POLL_DIFF）+ 传输延迟声明 |
| 传输 | InProcess / SubprocessBridge | FileSignal / Redis / Zmq |

> 关键收入：**大 QMT 的 `m_nOrderStatus` 与 xtquant 的 `OrderStatus` 是同一套整数族群**（50 已报 / 56 已成 / 57 废单，easytrader 文档与 qmt_work SSOT 三方互证）。
> 因此**不需要为桥接新建一份状态映射表**（这修正了姊妹文档 §5.2.4 的一处过度设计，详见第 7 节）。

### 5.4 不变量守卫（新增护栏）

- **AST 门禁**：禁止 `app/routes/`、`engines/`、`gateway/`（除 `signal_router.py`/`execution.py`）出现 `adapter.place_order` / `bridge.gateway.*` / `manager.bridge(...).call(` 直接调用 → 现有 `scripts/check_execution_architecture.py` 的 Gate 体系扩展一条即可。
- **逃生舱**（借鉴 easytrader 的诚实）：确需方言原生能力时，走显式 `CallNativePort.native_call(op, params)`，**必须经过 WAL + 审计**（区别于 easytrader 的 `user.trader` 无痕逃逸）。
- **契约回归**：端口面变更 ⇒ `tests/test_connector_ports.py` 结构化断言 + `gen_contracts.py` 基线重生成 + `scripts/ci_reconcile.py::EXPECTED_TESTS` 重算（`--update`），README 两处计数同步。

---

## 6. 落地路径

| 阶段 | 内容 | 产出 / 验收 |
|---|---|---|
| **P0 接线**（等价于方案 B） | ①`ExecutionService` 改为依赖 `ExecutionPort`；②补 `OrderSnapshot` + SSOT 归一化；③异步口径统一（线程池 offload）；④`client_order_id` 透传；⑤保留 `XTQuantBridge` 作为 InProcess transport 实现 | 产线路径改为 `SignalRouter → Port → Connector → Dialect×Transport`；1704 用例不回归；`check_execution_architecture` 无引用违规 |
| **P1 正交拆分** | 抽出 `Transport` 接口（复用现有 `XTQuantBridge.call/call_locked/QMT_BRIDGE_CALL_TIMEOUT=30s` 语义）；`XtQuantV1` dialect 从 `XTPQuantAdapter` 里剥离 | `qmt.mini` 与 `qmt.big.direct` 仅差 transport/数据目录 |
| **P2 EventPort + 能力协商** | 新增 `EventPort`、`probe_capabilities()`；`ConnectionSupervisor` 接入 capabilities 决定判定为 DEGRADED 还是 FAILED | `qmt.big.bridge.file` 上线；事件延迟(async)上界进健康面 |
| **P3 低延迟传输** | `RedisRPC` / `ZmqRPC` transport 实现，同一套 dialect 复用 | 3 方言 × 4 传输组合表里 6 个组合全部可实例化 |
| **P4 未来客户端** | `PtradeV1` / `ThsUiV1` 只需新增 dialect（day 级），不涉及编排层改动 | 新客户端接入改动集中在 `connectors/dialects/` 一个文件 |

**风险与对策**

| 风险 | 对策 |
|---|---|
| P0 接线会触及 `ExecutionService` 全链路（波及面广） | 分两步：先让 port 实现**与现有 bridge 同时可用**（功能开关 `QMT_USE_PORTS=0/1`），双跑对比一致后再摘旧路径 |
| 同步方法改 async 波及面 | 只在端口层改；adapter 侧签名不动 |
| 「接线」期间可能踩到已闭环的坑（如连接去重、session 预算 45s vs ready 100s vs 前端 120s） | 沿用现有预算链不动；supervisor 的退避上限 60s 与 `BrokerError.needs_action` 联动，不在本轮调整 |
| 测试计数漂移 | 每阶段结束即跑 `ci_reconcile`（**必须 `run_in_background`**）并重算 `EXPECTED_TESTS` |

---

## 7. 对姊妹文档《大 QMT 兼容方案》的 4 处修正

| # | 原方案 v1.0 | 修正为 | 理由 |
|---|---|---|---|
| 1 | §5.2.4 新建 `bigqmt/status_map.py` 作为新 SSOT | **删除；直接复用 `xtquant_client/order_status.py`**，仅在大 QMT 侧补 `_BIGQMT_RAW_STATUS` 入同一张原始表 | 两者同一整数族群（三方互证），新建 SSOT 会重演「三处各自维护一套状态映射」的根因（见 order_status.py 头部 docstring） |
| 2 | §4.2 新增 `BigQmtBridgeAdapter(BrokerAdapter)` | 改为 `BigQmtV1` dialect + FileSignal/Redis/Zmq transport，经 `QmtConnector` 组合 | 避免第 3 套平行抽象；Transport 可插拔由组合实现而非在 adapter 内 if 分支 |
| 3 | §5.2.4 回调合成写在 adapter 内部 | 回调合成归入 **`EventPort` 实现**（属 transport 层能力），并声明 `POLL_DIFF` + 延迟上界 | 让上层不区分客户端类型；同时避免 file/redis 两种传输各写一遍合成器 |
| 4 | §9.2 action 词表直接作为外部 API | action 词表降为 **dialect 内部 wire 格式**，不进 REST/WS 契约面；对外一律 canonical ports + OrderSnapshot | 防止方言泄漏出端口边界，与 E3 教训一致 |

> 未受影响的部分（继续有效）：路径 A/B 双通道决策、probe 纪律（禁断言终端能力）、注入函数唯一来源捕获与防手抄 CI 闸、py3.6 兼容、安全（token + 目录 ACL + trading_enabled 默认关）、Phase 划分与测试契约同步清单。

---

## 8. 附录：为什么「照抄 easytrader」在 qmt_work 里会退化

1. **它解决的是「脚本作者想快点下单」，我们要解决的是「平台必须保证不变量」。** 前者可以把 `raw object` 交给用户，后者不行。
2. **它是单客户端单进程假设**，没有 lifecycle supervisor 的需求；qmt_work 是**多连接并存**（mini + big + 行情多源链），需要隔离、退避、降级态。
3. **它的能力差异靠「文档 + 运行时崩」表达**，我们在处理「券商随时可能二次收紧 PID 白名单」的不确定性，必须有**运行时能力协商**。
4. **它没有不变量层**（WAL/幂等/TOTP/审计），而这正是 qmt_work 的核心资产——一旦引入扁平动词层作为「新时期主线」，这些资产会被自然绕过（E1）。

**但要向 easytrader 学两件事**：`use()` 形式的工厂（对应我们的 `Registry` 已有）+ **诚实地为原生能力留受控逃生舱**（对应新增的 `CallNativePort`，带审计）。
