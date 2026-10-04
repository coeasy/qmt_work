# qmt_work 大 QMT（完整版 QMT）兼容支持方案

> 版本：v1.1（2026-10-01）
> **实施收口见 `docs/BIG_QMT_IMPLEMENTATION_PLAN.md`**（基于代码现状的差距盘点 + M0~M4 详细实现计划；其 §4.3 含参考项目实测证据带来的 8 项增量决策，与本文冲突处以收口文档为准）。
> **执行口径见 `docs/UNIVERSAL_BROKER_PLATFORM_FINAL_PLAN_V4.md` §9**（首个落地案例）；V4 是本方案的上位纲领，落地次序以 V4 Phase 2-3 为准。
> 参考：[xtquant_big_convert](https://github.com/litaolemo/xtquant_big_convert)、[easytrader](https://github.com/shidenggui/easytrader)、[元宵大师《替换MiniQMT，大QMT桥接全方位跑通》](https://mp.weixin.qq.com/s/YfK1a6yzL0iL_1vK0cCwSQ)

> ⚠️ **v1.1 修订**：本文落地形态已被姊妹文档 `docs/UNIFIED_TRADING_ABSTRACTION.md` 修订，请先读它的第 7 节。
> 4 处改动：① 取消新建 `bigqmt/status_map.py`，改为复用既有 `xtquant_client/order_status.py` SSOT（大 QMT `m_nOrderStatus` 与 xtquant `OrderStatus` 同族群，三方互证）；
> ② `BigQmtBridgeAdapter(BrokerAdapter)` 改为 `BigQmtV1` dialect + 可插拔 transport，经既有 `QmtConnector` 组合，避免引入第三套平行抽象；
> ③ 回调 diff 合成挪入新增的 `EventPort`（属 transport 层能力），不再写在 adapter 内；
> ④ action 词表降级为 dialect 内部 wire 格式，不进 REST/WS 契约面。
> 未受影响：路径 A/B 双通道决策、probe 纪律、注入函数唯一来源捕获、py3.6 兼容、安全设计、Phase 划分与测试契约同步清单。

---

## 1. 背景与问题定义

### 1.1 miniQMT 为何「不能用了」

2026-09-28 已在 qmt_work 中实测定位根因：券商下发授权串
`mdl_auth_xttrader/xtdata_strict_connection_check=1` 且 `no_pid_check=0` 后，
QMT 客户端对**外部 xtquant 进程做 PID 白名单校验**，交易连接返回
`connect ret error-1`（日志特征 `quant session N, pid X not allowed`）。
该授权串在客户端内部，平台无法改写，只能引导用户找券商。

行业背景（微信文章同证）：券商正逐步收紧 miniQMT 外部直连，「大 QMT 开户后面会逐步收紧」。未来主战场是**大 QMT（完整版 QMT）**。

### 1.2 大 QMT 与 miniQMT 的本质差异

| 维度 | miniQMT（极简模式） | 大 QMT（完整版） |
|---|---|---|
| 交易进程 | `XtMiniQmt.exe` 独立进程，生成 `userdata_mini/` | QMT 主程序，生成 `userdata/` |
| 外部 xtquant 直连 | 支持（正被 PID 白名单收紧） | 部分券商同样收紧（同一授权串体系） |
| 内置 Python | 无（外部 Python 直接 import xtquant） | **有**（内置 Python 3.6.x，策略脚本被挂载执行） |
| 内部交易 API | 经 xtquant SDK（`XtQuantTrader.order_stock`） | 注入策略命名空间的全局函数：`passorder` / `cancel` / `get_trade_detail_data` 等，以及 `ContextInfo` 方法族 |
| 行情 | xtdata 经 miniquote 端口 58610 | `ContextInfo.get_full_tick` / `get_market_data` / `download_history_data`；**大窗口 58600 仅交易，行情 RPC 仍须 miniquote 58610** |
| 回调 | `XtQuantTraderCallback` 推送（on_order/on_trade） | 部分有回调（两融），普通账户常需**轮询 diff 合成** |
| 版本碎片 | xtquant pyd 按 ABI 编译（cp36~cp312） | 内置 Python 老旧（3.6），无 `shared_memory`，部分函数依赖客户端版本 |

### 1.3 现状盘点（qmt_work 已具备的底子）

- **`BrokerAdapter` 抽象完备**（`backend/xtquant_client/base.py`，328 行）：行情/账户/交易/参考数据全套抽象方法 + `BrokerError`/`BrokerNotConnectedError`/`BrokerSDKError` 异常体系（跨进程文案重建）。
- **`client_mode`（auto/mini/full）已贯穿**：`ConnectionConfig` → `registry.create_adapter` → `XTPQuantAdapter.start()` → `_effective_trade_dir()`（`userdata` vs `userdata_mini` 候选目录互备降级）。**即「xtquant 直连大 QMT」路径已预留，只差做成一等公民。**
- **子进程 ABI 桥**（`bridge_client.py` / `bridge_server.py`）：解决主后端 py3.13 加载 cp311 xtquant 的问题，协议为行分隔 JSON（stdin/stdout），反射转发任意 BrokerAdapter 方法。
- **行情多源链**：`DataSourceManager` auto 链 broker → eltdx → 公共源，大 QMT 模式下 broker 行情缺失可自动降级。
- **503 语义统一**：券商不可用 → HTTP 200 + `code=503` + `MSG_NO_BROKER` 引导；柜台拒单（`-1`）→ `BrokerError`（400/真因）。零 mock 约束。

**缺口**：当券商同时封掉 miniQMT 与大 QMT 的外部直连时（未来的普遍形态），qmt_work 无任何交易通道可用。这是本方案要补的核心能力。

---

## 2. 参考方案调研结论

### 2.1 xtquant_big_convert（最接近生产级，核心借鉴对象）

- **思路**：在大 QMT 内置 Python 里部署一个 RPC **服务端**（作为策略脚本被 QMT 挂载运行），把大 QMT 内部 API 封装成**与 miniQMT xtquant 兼容的接口**；外部客户端 `BigQmtRpcClient` 像用 xtquant 一样用它。
- **可插拔传输层**：`RpcTransport` 接口（`send_request` / `start_receiving` / `send_response` / `stop`），实现 redis（默认，rpush/blpop + pub/sub 推送）、zmq（ROUTER/DEALER，同机低延迟）、mysql（轮询兜底）、shm（3.6 无 `shared_memory`，仅 stub）、pipe。**一个配置键切换，不改协议。**
- **关键教训（必须吸取）**：
  1. **QMT 只往被挂载的那个入口文件的命名空间注入全局函数** —— 入口必须 `capture_qmt_injected_funcs(globals())` 从唯一来源捕获，**严禁手抄函数名单**（该项目曾因手抄表漏了 `query_credit_account`，把桥的 bug 误报成「终端没有两融接口」）。
  2. **`global_namespace=False` 只说明「这条解析路径上没有」，不能断言终端没有该能力** —— 正确的判断办法是在大 QMT 策略里直接调一次。诊断文案必须避免误导性结论。
  3. `call_formula` 一族是注入的全局函数而非 `ContextInfo` 方法 —— 查找顺序应为「先全局、后 ContextInfo」。
  4. 无推送通道时的兜底：客户端查询轮询器按 sysid+status diff 委托、按 trade_id diff 成交，**合成与推送同形的 on_stock_order / on_stock_trade / on_order_error 事件**（首轮打底不补发，间隔 1s）。
  5. 异步下单回执：`order_stock_async` 返回递增 seq，异常时 fire `on_order_error`；废单（状态 57）也要发布 order_error。
- **规模参考**：全量 2379 passed，说明该架构可在生产环境长期稳定运行。

### 2.2 easytrader（接口设计借鉴）

- 统一 `buy/sell/cancel/position/balance` 抽象 + 多券商 adapter（同花顺客户端 UI 自动化 / miniqmt / 雪球）。
- 借鉴点：**严格参数检查**（曾因 follower 传 `entrust_prop` 而严格签名检查崩掉的 #520 修复）、`connect()/use()` 工厂用法、多通道共存时按券商特性隔离实现。
- **不建议**走同花顺客户端 UI 自动化路径（pywinauto 脆弱、无法通过 qmt_work 的零 mock 与可观测性验收），仅在附录列为备选路线。

### 2.3 元宵大师文件桥（最简可运行形态）

- **「文件即接口」**：外部端写 `signal.json` → 大 QMT 端 `OrderExecutor` 轮询 → 写回 `result.json`，`signal_id` 防重复执行。
- 三方案对比结论：文件监控（零部署、秒级延迟）→ Redis 队列（<50ms、多策略并发、需运维）→ HTTP API（功能最全、开发量最大）。
- action 路由：`BUY/SELL/CANCEL_ORDER/QUERY_ASSET/QUERY_POSITION/QUERY_ORDER/QUERY_TRADE/QUERY_QUOTE/GET_DATA`，**每加一个功能只加一个 if 分支**。
- 硬前提：**两边路径必须完全一致**。

### 2.4 综合判断

| 方案 | 延迟 | 部署成本 | 稳定性 | 适配 qmt_work |
|---|---|---|---|---|
| xtquant 直连大 QMT（已有 client_mode=full） | 最低（同 SDK） | 零 | 高 | ★★★★★ 优先 |
| 文件桥 | 秒级（轮询间隔） | 零 | 高（组件最少） | ★★★★ 兜底首选 |
| Redis/ZMQ 桥 | <50ms | 需装 Redis 或 pyzmq | 高 | ★★★ Phase 3 升级 |
| HTTP API 桥 | 低 | 大 QMT 内起 server | 中（鉴权/端口） | ★★ 不做（3.6 生态差） |
| 同花顺 UI 自动化 | 慢 | 中 | 低（UI 脆弱） | ✗ 明确排除 |

**决策：双通道架构。** 路径 A（xtquant 直连 full 模式）做成一等公民并补全诊断；路径 B（大 QMT 内置策略桥接）作为直连被 PID 白名单封死时的兜底，传输层可插拔（文件 → Redis → ZMQ），协议与 BrokerAdapter 映射一次定义、三种传输共用。

---

## 3. 目标与非目标

### 3.1 目标

1. **G1**：`client_mode=full`（xtquant 直连大 QMT）成为一等公民：券商档案默认路径补 `userdata` 候选、前端连接表单暴露模式选择、连接诊断区分「路径/session/PID 白名单/行情端口」四类根因。
2. **G2**：新增 `BigQmtBridgeAdapter(BrokerAdapter)`，通过大 QMT 内置策略桥接提供完整交易能力：下单/撤单/资金/持仓/当日委托/当日成交/实时行情/历史 K 线下载。
3. **G3**：桥接传输层可插拔：`file`（默认，零部署）/ `redis`（低延迟）/ `zmq`（同机极速），单一配置键切换，协议不变。
4. **G4**：回调合成：桥接模式下按轮询 diff 合成 `on_order` / `on_trade` 事件，复用现有 `core.emit.emit_event` 出口与 WS 契约（`wsEvents.ts` + `tests/contracts/ws_events.json` 同步）。
5. **G5**：能力探测 probe：连接时自动探测 `global_namespace` / `callback_bound` / 各 action 可用性 / Python 版本 / 传输可达性，结果进入 `/api/v1/broker/health` 诊断面，文案遵守「不得据单点探测断言终端能力」教训。
6. **G6**：零 mock 约束全量保持：未连券商 → 200 + `503` + 引导；柜台拒单 → 400 + 真因；绝不返假数据。

### 3.2 非目标

- 不支持同花顺客户端 UI 自动化。
- 不支持大 QMT 两融专项接口（`query_credit_account` 等）首版落地，仅在能力词表与 probe 中预留（`CREDIT_ACCOUNT` capability）。
- 不重写行情体系：大 QMT 桥接模式下 broker 行情源缺位时沿用现有多源降级链（eltdx/公共源）；`qmt_only` 钉死策略行为不变。
- 不做 HTTP API 传输。

---

## 4. 总体架构

### 4.1 架构图

```
                         qmt_work 后端 (FastAPI, py3.13)
                                      │
                        BrokerManager / Connection 持久化
                                      │
                          BrokerAdapter 统一抽象 (base.py)
              ┌───────────────────────┼─────────────────────────┐
              │                       │                         │
      XTPQuantAdapter          BigQmtBridgeAdapter         (ths/ptrade/juejin
   (xtquant 直连, 路径 A)         (路径 B, 新增)              占位, 不动)
   client_mode=mini|full            │
   userdata|userdata_mini    ┌──────┴──────────────────────┐
                             │   BigQmtRpcClient (外部端)   │
                             │   transport: file/redis/zmq │
                             └──────┬──────────────────────┘
                                    │  统一桥协议 (行分隔 JSON 信封)
                     ┌──────────────┼──────────────────┐
                     │ 文件: signal/result 目录          │
                     │ redis: rpush/blpop + pub/sub      │
                     │ zmq: ROUTER/DEALER (tcp)          │
                     └──────────────┬──────────────────┘
                                    │
                    大 QMT 内置 Python 3.6.x (QMT 挂载执行)
                          BigQmtAgent 策略脚本 (服务端)
                    ┌───────────────────────────────────┐
                    │ capture_qmt_injected_funcs(globals)│ ← 唯一来源, 严禁手抄
                    │ passorder / cancel                 │
                    │ get_trade_detail_data(account/     │
                    │   position/order/deal)             │
                    │ ContextInfo.get_full_tick /        │
                    │   get_market_data / download_...   │
                    │ 轮询 diff 合成 on_order/on_trade    │
                    └───────────────────────────────────┘
```

### 4.2 目录规划（新增/修改文件清单）

```
backend/xtquant_client/
├── base.py                        # [改] capabilities 词表补 bigqmt_* 项
├── registry.py                    # [改] BROKER_PROFILES 增 full 路径候选; 工厂分支 bigqmt
├── bigqmt/                        # [新增] 路径 B 包
│   ├── __init__.py
│   ├── protocol.py                # 桥协议常量/信封构造/版本协商 (SSOT)
│   ├── actions.py                 # action 词表: BUY/SELL/CANCEL/QUERY_*/GET_DATA
│   ├── status_map.py              # 大QMT m_nOrderStatus 数字 → order_status.py SSOT 映射
│   ├── transports/
│   │   ├── base.py                # RpcTransport 抽象 (借鉴 xtquant_big_convert)
│   │   ├── file_transport.py      # signal/result 文件轮询, 原子写(temp+rename)
│   │   ├── redis_transport.py     # rpush/blpop + pub/sub
│   │   └── zmq_transport.py       # ROUTER/DEALER
│   ├── rpc_client.py              # BigQmtRpcClient: 请求-响应/超时/重试/probe
│   ├── event_synthesizer.py       # 轮询 diff → on_order/on_trade 回调合成
│   └── adapter.py                 # BigQmtBridgeAdapter(BrokerAdapter)
├── agent_bigqmt/                  # [新增] 部署到大QMT内置Python的脚本包 (py3.6 兼容!)
│   ├── BIGQMT_AGENT.py            # 入口: 捕获注入函数 + handle_init + 定时器轮询
│   ├── qmt_api.py                 # action 路由执行器 (OrderExecutor 演进版)
│   └── DEPLOY.md                  # 部署说明 (拷贝到 QMT 策略目录)
backend/tests/
├── test_bigqmt_protocol.py        # 协议信封/版本协商
├── test_bigqmt_status_map.py      # 状态数字映射 → SSOT
├── test_bigqmt_transports.py      # 三传输一致性 (fake agent)
├── test_bigqmt_adapter.py         # BrokerAdapter 契约面 (下单/拒单/503)
├── test_bigqmt_event_synth.py     # 回调合成 (diff/去重/首轮打底)
└── fake_bigqmt_agent.py           # 模拟大QMT端 (仿 fake_bridge_server.py)
frontend-next/                     # 连接表单模式选择 + 桥接配置 + 诊断面板
scripts/ci_reconcile.py            # EXPECTED_TESTS 重算 (--update)
```

**★ 实际落地（as built，2026-10-01 复核）** —— 上面是**规划稿**，实机实现时按本仓既有分层
收敛到了 `connectors/`（而不是新建 `xtquant_client/bigqmt/` 包），以免出现第二套传输层：

| 规划名 | 实际落地 |
|---|---|
| `xtquant_client/bigqmt/{protocol,actions,status_map}.py` | `connectors/dialects/base.py`（canonical `Ops` 词表 + `is_write_op`）、`connectors/dialects/bigqmt_v1.py`（`_OP_TO_ACTION` 方言映射）、`connectors/canonicalize.py`（状态/字段 canonical 化） |
| `xtquant_client/bigqmt/transports/*` | `connectors/transports/{base,file_signal,http_gateway,lowlatency,inprocess,semantics}.py`（`_TRANSPORTS` 为唯一注册表；redis 变体见 `scripts/bigqmt_relay.py`） |
| `xtquant_client/bigqmt/rpc_client.py` | `connectors/generic.py::GenericConnector`（请求-响应/超时/重试/probe） |
| `xtquant_client/bigqmt/adapter.py` | `connectors/bigqmt_bridge.py`（桥本体）+ `connectors/bigqmt_gateway.py`（gateway 面，见 §9.6.3） |
| `agent_bigqmt/{BIGQMT_AGENT,qmt_api}.py` | **同规划**（`BIGQMT_AGENT.py` 入口壳 + `qmt_api.py` action 执行器）；另有 `scripts/gen_qmt_agent_bundle.py` 生成可一键部署的**单文件 bundle** |
| `backend/tests/test_bigqmt_*.py` | `test_bigqmt_bridge_face.py` / `test_bigqmt_bridge_integration.py` / `test_bigqmt_file_bridge.py` / `test_bigqmt_relay_redis.py` / `test_bigqmt_marketdata_e2e.py` / `test_bigqmt_agent_bundle.py` + `fake_bigqmt_agent.py` |

**py3.6 兼容铁律**：`agent_bigqmt/` 下代码不得使用 3.7+ 语法/库（无 `dataclasses`、
无 walrus、无 `shared_memory`、`asyncio` 能力受限）。该目录由
`scripts/check_bigqmt_agent_py36.py` **AST 独立把关**（py3.6 语法白名单 + 不用 3.7+
特性 + **入口文件不得手抄注入函数名**），已接进 CI 前的本地验收链。

---

## 5. 详细设计

### 5.1 路径 A：xtquant 直连大 QMT（client_mode=full 一等公民）

改动小、收益快，Phase 0 完成：

1. **券商档案**（`registry.py BROKER_PROFILES`）：每个 active 档案增加 `full_client_path` 字段（指向大 QMT 安装目录，交易目录 `userdata`），前端档案表单可选「极简（mini）/ 完整（full）/ 自动」。`_effective_trade_dir` 已有互备降级，档案层只补默认值与文案。
2. **诊断增强**（`xtp/env.py::probe_environment` + `diagnose_trade_connect`）：
   - 区分 `ret error-1` + 日志 `pid not allowed` → 明确输出「券商启用 PID 白名单，外部直连被拒；请配置大 QMT 桥接模式（路径 B）」并给出引导，不再泛化为「找券商」；
   - 探测 `58610`（miniquote）未监听时，输出「大窗口仅交易，行情需开启独立行情/极简模式或运行 XtMiniQmt.exe」既有文案（保留）。
3. **前端**：连接表单 `client_mode` 下拉（auto/mini/full）+ 诊断页展示 probe 结果。

### 5.2 路径 B：大 QMT 内置策略桥接

#### 5.2.1 桥协议（`protocol.py`，SSOT）

借鉴 qmt_work 现有 bridge_server 行分隔 JSON 风格与 xtquant_big_convert 信封设计：

```jsonc
// 请求 (外部端 → 大QMT端)
{
  "v": 1,                       // 协议版本
  "signal_id": "bq-20261001-000123",  // 全局唯一, 幂等键
  "action": "BUY",              // actions.py 词表
  "ts": 1791888090123,          // 发起时间戳 ms (时钟偏移诊断)
  "auth": "<token>",            // 共享令牌 (见 5.2.6 安全)
  "params": { "stock_code": "588200.SH", "price_type": "fix",
              "price": 1.171, "volume": 100,
              "strategy_name": "algo01", "remark": "" }
}

// 响应 (大QMT端 → 外部端), 同 signal_id
{
  "v": 1, "signal_id": "bq-20261001-000123",
  "ok": true,
  "result": { "order_id": "7160", "seq": 12, "status": "submitted" },
  "error": "", "error_type": "",        // ok=false 时: "BrokerNotConnected" | "BrokerError" | ...
  "ts": 1791888090999, "agent": {"ver": "1.0.0", "py": "3.6.8", "funcs": ["passorder", "..."]}
}
```

- **error_type 对齐 qmt_work 异常体系**：agent 端把内部失败归类为 `BrokerNotConnected` / `BrokerError`（柜台拒单，含真因文案）/ `Unsupported`（注入函数缺失）；rpc_client 跨进程重建对应异常对象（复用 `BrokerSDKError.from_message()` 模式）。**失败绝不包 ok=true**。
- **版本协商**：连接握手 action=`PROBE`，返回 agent 元数据（版本/Python/可用函数清单/probe 结果）；客户端不认识的高版本字段一律忽略（向后兼容）。

#### 5.2.2 传输层（`transports/`）

统一抽象（与 xtquant_big_convert 的 `RpcTransport` 同形，适配文件轮询场景）：

```python
class RpcTransport:                      # 外部端视角
    def send_request(self, envelope: dict) -> None: ...
    def recv_response(self, signal_id: str, timeout: float) -> dict: ...   # 阻塞取回
    def subscribe_events(self, callback) -> None: ...
    def close(self) -> None: ...
```

| 传输 | 机制 | 延迟 | 适用 |
|---|---|---|---|
| `file`（默认） | 外部端写 `bridge_dir/req/<signal_id>.json`（临时文件 + `os.replace` 原子落盘）→ agent 轮询（`ContextInfo.run` 定时器，间隔可配 0.3~1s）→ 写 `bridge_dir/resp/<signal_id>.json`；事件经 `bridge_dir/events.ndjson` 追加 + 外部端 tail | 0.3~1s | 兜底/低频策略，零部署 |
| `redis` | 请求队列 `bigqmt:req:{sysid}`（rpush/blpop），响应 `bigqmt:resp:{sysid}`，事件频道 `bigqmt:events:{sysid}`（pub/sub） | <50ms | 多策略并发/实盘主力 |
| `zmq` | ROUTER/DEALER over tcp，回包用原生 identity 路由 | 同机最低 | 单机极致延迟（Phase 3） |

选择逻辑：`transport` 配置键（`bigqmt.transport`，env `QMT_BIGQMT_TRANSPORT`）；redis 不可达且非 zmq 时自动退回 file 并在健康面提示（借鉴 xtquant_big_convert #372 的「连续失败退回外层重选」）。

#### 5.2.3 action 词表与 BrokerAdapter 映射（`actions.py` / `adapter.py`）

| BrokerAdapter 方法 | action | 大 QMT 端实现 | 备注 |
|---|---|---|---|
| `test_connection` / 握手 | `PROBE` | 返回 agent 元数据 + 注入函数清单 | 对应 G5 |
| `get_account` / `get_cash` | `QUERY_ASSET` | `get_trade_detail_data("ACCOUNT", ...)` → `m_dBalance/m_dAvailable/m_dFrozenCash/m_dInstrumentMarketValue` | 字段中文输出映射 |
| `get_positions` | `QUERY_POSITION` | `get_trade_detail_data("POSITION", ...)` → 持仓量/可用量/成本价/市值 | |
| `get_orders` | `QUERY_ORDER` | `get_trade_detail_data("ORDER", ...)` | 当日委托 |
| `get_deals` | `QUERY_TRADE` | `get_trade_detail_data("DEAL", ...)` | 当日成交 |
| `place_order` | `BUY` / `SELL` | `passorder(stock_code, op, volume, price_type, price, account_id, ...)` | 异步回执：passorder 后立即回 `submitted(seq)`，真实 order_id 由回调合成补齐（与 XTP adapter 的 `_wait_order_response` 同思路，但超时记 `unknown` 不伪报） |
| `cancel_order` | `CANCEL_ORDER` | `cancel(order_id, account_id, ...)` | 返回码必须捕获（借鉴 P0-12） |
| `get_quote` / `get_full_tick` | `QUERY_QUOTE` | `ContextInfo.get_full_tick([codes])` | 五档盘口 |
| `get_kline`（增量场景） | `GET_DATA` + `QUERY_KLINE` | `download_history_data` + `get_market_data` → CSV/直传 | 历史补数走多源链，不依赖桥 |
| `get_trading_calendar` / `get_stock_list` | `QUERY_CALENDAR` / `QUERY_STOCK_LIST` | `ContextInfo.get_trading_dates` / `get_stock_list_in_sector` | capabilities 按探测结果注册 |
| `subscribe_quote` | `SUB_QUOTE` | **意图式订阅**：agent 记入 `subscribed` 集，由 `handlebar` 轮询 `get_full_tick` 做差分，变化写成 `quote` 事件进 `events.ndjson`（外部端只翻译不再 diff）；若终端另注入 `subscribe_quote` 则尽力调一次，失败静默回落轮询（`mode` 字段如实上报）。**回调本身不过线** |
| （回调合成） | — | 见 5.2.4 | |
| `get_financial` / L2 | — | 首版抛 `BrokerSDKError("桥接模式不支持")` | capabilities 不注册 |

**参数防线**（agent 端 + 客户端双侧）：限价单 `price<=0` 拦截、`volume` 正整数、`price_type` 白名单（借鉴 easytrader 严格参数检查的 #520 教训：**follow 链路上多余的扩展参数不能崩在签名检查**，客户端侧对桥协议字段做「未知忽略」）。

#### 5.2.4 委托状态映射与回调合成

- **状态 SSOT**：大 QMT `m_nOrderStatus` 数字（48 未报 / 50 待报 / 51 已报 / 52 部成 / 53 部撤 / 54 已成 / 55 撤单 / 56 已拒 / 57 废单 …，以 `xtconstant` 词表为准在 `status_map.py` 建表并加注释钉版本）→ 映射到 qmt_work `order_status.py::normalize_order_status` 既有词表。**映射表是新增 SSOT，进契约基线**（`tests/contracts/introspect.py` 已扫 place_order 签名与状态映射，需扩展扫 `bigqmt/status_map.py`）。
- **回调合成**（`event_synthesizer.py`，借鉴 xtquant_big_convert #372 + 废单 #57 教训）：
  1. agent 端每轮询周期 diff `QUERY_ORDER`（按 order_sys_id+status）与 `QUERY_TRADE`（按 trade_id），产出增量事件（首轮打底不补发）；
  2. 废单（57）/拒单（56）→ 合成 `order_error` 事件（error_id/error_msg/order_sys_id）；
  3. 外部端 `event_synthesizer` 按事件序号去重、排序，直推现有 `on_order` / `on_trade` 回调钩子 → `core.emit.emit_event`（三参标准形态）→ WS 推送前端；
  4. file 传输下事件走 `events.ndjson` tail + 通道状态健康面暴露；redis/zmq 下走推送通道，file 仅作兜底。
  5. **契约同步**：新事件类型若有（`broker.bigqmt.event_synth_lag` 之类遥测）必须同步 `frontend-next/src/services/wsEvents.ts` + `tests/contracts/ws_events.json`。

#### 5.2.5 大 QMT 端 Agent（`agent_bigqmt/`，py3.6 兼容）

```
BIGQMT_AGENT.py (QMT 策略入口, 用户拷入 QMT 策略目录)
├── capture_qmt_injected_funcs(globals())   # 唯一来源捕获, 严禁手抄名单
├── handle_init(ContextInfo):
│     读取同目录 agent_config.json (bridge_dir/transport/auth/token)
│     校验路径存在与可写 → 初始化 qmt_api 执行器
│     ContextInfo.run 周期任务: 轮询请求 + diff 事件
├── qmt_api.execute_order(signal):          # action 路由 (if 分支族)
│     PROBE/QUERY_*/BUY/SELL/CANCEL/GET_DATA...
│     异常归类 error_type, 中文真因文案
└── write_result(signal_id, ...)            # 原子写 + 事件追加
```

要点：

- **不手抄注入函数名单**（闸门：CI 检查入口文件中函数名字面量 >5 即红，仿 xtquant_big_convert 的防回归闸）；
- **probe 结论纪律**：`global_namespace=False` 只能报「该解析路径未捕获」，禁止输出「终端没有 XX 接口」类断言文案；
- 防重复执行：以 `resp/<signal_id>.json` 存在性为准（不依赖内存状态，重启安全）；
- 时钟：信封双向带 `ts`，客户端计算偏移，超阈值在健康面告警（file 传输下 result 时间戳排序不可靠，以 signal_id 与序号为准）。

#### 5.2.6 安全设计

1. **文件桥无鉴权传输面的风险**：`bridge_dir` 本机任意进程可写 `req/*.json` 即可下单。对策：
   - 信封带 `auth` 共享令牌（`agent_config.json` 与 qmt_work 配置各持一半派生值，复用 `core/crypto.py` 派生，**勿动 `_NONCE`**）；
   - `bridge_dir` 默认落在用户目录（如 `%USERDATA%/qmt_work/bigqmt_bridge/`），并校验 ACL 仅当前用户可写；
   - agent 端逐请求校验 token，失败静默丢弃 + 事件面计数告警。
2. **运行期可写文件纪律（TD-26）**：桥文件、agent 日志属于「两端共享」的特例 —— qmt_work 侧配置存储跟随 `settings.db_path.parent`，`bridge_dir` 本体是用户显式配置的共享路径（必须在 QMT 可访问的固定位置），两端路径一致性校验放进 `PROBE`（目录 inode/创建时间戳交叉核对）。
3. **下单默认关闭**：agent 首次部署 `trading_enabled=false`（仿 xtquant_big_convert 免责条款），qmt_work 前端连接向导显式开启。

#### 5.2.7 连接生命周期与 503 语义

- `ConnectionConfig` 扩展（`broker_connections` 表迁移走 `_EXTRA_COLUMNS` 幂等补列，**勿追加已应用版本的 DDL**）：`bridge_transport` / `bridge_dir` / `redis_url` / `zmq_addr` / `auth_token_id`。
- `registry.create_adapter` 分支：`broker_id=bigqmt`（或既有档案 + `mode=full_bridge`）→ `BigQmtBridgeAdapter`。
- `start()` 顺序：`PROBE` 握手（10s 预算）→ `trading_enabled` 校验 → 能力注册 → `mark_ready`。**启动路径禁止无界等待（TD-25 纪律）**：任何传输 IO 必须有界（file 轮询单次 `timeout=5s`），超时 → `deferred` 后台重连，不阻断 `/ready`。
- 错误映射（`app/routes/_common.py` 既有机制复用）：
  - agent 不可达 / PROBE 失败 → `BrokerNotConnectedError` → `503` + 引导（含「大 QMT 端策略未运行/路径不一致/token 不匹配」三类排查项）；
  - 柜台拒单（passorder 返回失败/废单回调）→ `BrokerError`（400 + 真因）；
  - 注入函数缺失 → `BrokerSDKError`（跨进程文案重建）。

#### 5.2.8 行情策略

大 QMT 桥接模式下 broker 行情源天然缺位（58610 未监听）。沿用现有架构，不新增行情桥优先级：

- `_BoundBrokerSource` 在 bigqmt bridge 连接下仅注册 `capabilities` 中探测通过的 quote 项（`QUERY_QUOTE` 可用时注册实时报价）；
- K 线/补数继续走 eltdx / 公共源 / 本地缓存链；
- `source_policy.py` 的 `qmt_only` 钉死策略不变（选择该策略且仅连 bigqmt bridge → 503 引导，不静默降级）。

---

## 6. 分阶段实施计划

### Phase 0 — 路径 A 一等公民 + 诊断增强（先行，约 2~3 天工作量当量）

| # | 任务 | 验收 |
|---|---|---|
| 0.1 | `BROKER_PROFILES` 补 `full_client_path`；前端模式选择（auto/mini/full） | 连接大 QMT（未封 PID 的券商）直连成功，交易目录 `userdata` 命中日志可见 |
| 0.2 | `diagnose_trade_connect` 增加 PID 白名单根因分支（日志 `pid not allowed` → 输出路径 B 引导） | 人为构造授权串场景，诊断文案命中 |
| 0.3 | 契约同步：`EXPECTED_TESTS` 重算、契约基线重生成、README 计数 | `ci_reconcile` 全绿（`run_in_background` 跑） |

### Phase 1 — 路径 B 骨架：协议 + 文件传输 + 交易核心（MVP）

| # | 任务 | 验收 |
|---|---|---|
| 1.1 | `protocol.py` / `actions.py` / `status_map.py`（SSOT）+ 单测 | 信封/映射全覆盖，状态词表进契约基线 |
| 1.2 | `agent_bigqmt/`（py3.6 兼容）+ `DEPLOY.md` + 防手抄 CI 闸 | AST 白名单脚本通过；入口无 >5 函数名字面量 |
| 1.3 | `file_transport.py` + `rpc_client.py`（超时/重试/幂等） | `fake_bigqmt_agent.py` 全链路单测 |
| 1.4 | `BigQmtBridgeAdapter`：PROBE/QUERY_*/BUY/SELL/CANCEL + 错误映射 | BrokerAdapter 契约测试（503/400 语义、零 mock）通过 |
| 1.5 | `ConnectionConfig` 幂等补列 + `registry` 分支 + 前端连接表单桥接配置 | 连接去重回归（`test_broker_dedupe`）通过 |

### Phase 2 — 回调合成 + 行情 + 能力探测

| # | 任务 | 验收 |
|---|---|---|
| 2.1 | `event_synthesizer.py`（diff/去重/首轮打底/order_error 合成） | 合成事件契约测试；WS 事件契约同步 |
| 2.2 | `QUERY_QUOTE` / `GET_DATA` action + `subscribe_quote` 轮询降级 | 行情探针通过；`_BoundBrokerSource` capabilities 动态注册 |
| 2.3 | probe 结果接入 `/api/v1/broker/health` + 前端诊断面板 | 四类根因（路径/token/函数缺失/传输不可达）文案可区分，无误导性断言 |

### Phase 3 — 低延迟传输 + 部署体验

| # | 任务 | 验收 |
|---|---|---|
| 3.1 | `redis_transport.py`（rpush/blpop + pub/sub 事件） | 与 file 传输同协议回归（同一套 fake agent 测试换 transport 参数） |
| 3.2 | `zmq_transport.py`（ROUTER/DEALER） | 同上；传输切换单配置键生效 |
| 3.3 | 一键部署：qmt_work 生成 agent 包 + 路径校验 + `bridge_dir` 一致性检查 | 部署向导端到端走通 |

### Phase 4 — 收尾

- 两融能力预留（`CREDIT_ACCOUNT` capability + probe 项，不实现业务）；
- 文档：`docs/BIG_QMT_DEPLOY.md` 用户向导（含券商 PID 白名单现状说明）；
- 技能沉淀：构建/验收流程入 `smart-qmt-build-verify` skill 修订。

---

## 7. 测试与契约同步清单（硬门禁）

| 项 | 要求 |
|---|---|
| `scripts/ci_reconcile.py::EXPECTED_TESTS` | 新增测试后必须重算（`--update`），README 两处计数同步；该脚本前台必被 SIGTERM，**改 `run_in_background` 跑** |
| 契约基线 | `bigqmt/status_map.py`、place_order 桥协议签名纳入 `tests/contracts/introspect.py` 扫描面 → `gen_contracts.py` 重生成 → `test_contracts_baseline.py` 回归 |
| 测试环境 | 后端 pytest：`CODEBUDDY_SAFE_DELETE_ENABLED=0` + `--basetemp` 放 OS 临时目录 + 逐文件跑；`QMT_DB_BACKUP_ENABLED=0`；汇总用 JUnit XML（不信 shell 重定向） |
| fake agent | `fake_bigqmt_agent.py` 仿 `fake_bridge_server.py`，覆盖：正常链路 / 柜台拒单(-1→BrokerError) / agent 不可达(503) / 废单 57→order_error / signal_id 幂等 / token 校验失败 |
| emit 事件 | 任何新事件走 `core.emit.emit_event` 三参标准形态；护栏 `tests/test_emit_event.py` AST 全量扫描（含 `engines/`）会自动覆盖 |
| 前端测试 | `npm run test:serial`（并行会静默丢文件） |
| py3.6 兼容 | `agent_bigqmt/` 独立 lint（ast 白名单或 ruff target-version），禁 3.7+ 特性 |
| 行尾 | 新文件进 `.gitattributes` 门禁（`check_line_endings`） |

---

## 8. 风险清单与对策

| # | 风险 | 等级 | 对策 |
|---|---|---|---|
| R1 | 券商未来对大 QMT 内置策略也加限制（文章已提示「开户收紧」） | 高 | 架构上保留 BrokerAdapter 多通道：eltdx 行情 + 未来 PTrade/其他通道占位（`adapters/` 已留位） |
| R2 | 大 QMT 内置 Python 版本碎片（3.6.x 各券商定制差异） | 中 | agent 零第三方依赖（标准库 only）；PROBE 上报版本；AST 兼容闸 |
| R3 | 注入函数跨客户端版本差异（`query_credit_account` 教训） | 中 | 唯一来源捕获 + probe 实调验证 + capabilities 动态注册；诊断禁绝对断言 |
| R4 | file 传输秒级延迟影响条件单/打板引擎 | 中 | 明确文档：桥接模式下低延迟策略建议 redis/zmq 传输；`engines/` 下单链路对 `submitted(unknown)` 状态的兼容（现有 order_watchdog 已处理 unknown 对账） |
| R5 | 时钟偏移/文件系统延迟导致事件乱序 | 低 | signal_id + 序号排序；偏移超阈值健康面告警 |
| R6 | 桥目录被第三方进程写入（安全） | 中 | token 校验 + 目录 ACL + `trading_enabled` 默认关闭 |
| R7 | 打包环境（PyInstaller）无法携带 agent_bigqmt | 低 | agent 本来就是「拷给用户放进 QMT」的独立脚本包，不进打包体；从只读资源 `exe_dir()` 释放（TD-26：只读资源可用 exe_dir） |
| R8 | 「绿灯假象」回归（TD-23/24 教训） | 中 | 验收判据以契约门禁 + 后台日志为准，不只看测试汇总 |

---

## 9. 附录

### 9.1 signal/result 协议示例（file 传输，元宵大师风格对齐）

```jsonc
// 请求: bridge_dir/req/bq-000123.json
{"v":1,"signal_id":"bq-000123","action":"BUY","auth":"…",
 "params":{"stock_code":"588200.SH","price_type":"fix","price":1.171,"volume":100}}

// 响应: bridge_dir/resp/bq-000123.json
{"v":1,"signal_id":"bq-000123","ok":true,
 "result":{"order_id":"","seq":12,"status":"submitted"},
 "agent":{"ver":"1.0.0","py":"3.6.8"}}

// 事件流: bridge_dir/events.ndjson (追加)
{"seq":101,"type":"order","data":{"order_id":"7160","status":"已报"}}
{"seq":102,"type":"trade","data":{"trade_id":"50016562","order_id":"7160","volume":100,"price":1.171}}
```

### 9.2 action 词表（v1）

`PROBE / BUY / SELL / CANCEL_ORDER / QUERY_ASSET / QUERY_POSITION / QUERY_ORDER / QUERY_TRADE / QUERY_QUOTE / QUERY_KLINE / GET_DATA / SUB_QUOTE / UNSUB_QUOTE / QUERY_CALENDAR / QUERY_STOCK_LIST`

### 9.3 明确排除项

- 同花顺客户端 UI 自动化（easytrader 路线，脆弱、不可观测）；
- HTTP API 传输（3.6 内置生态开发量大，收益不及 redis/zmq）；
- mysql/shm/pipe 传输（xtquant_big_convert 有实现可后补，首版不做）；
- 两融业务首版实现。

### 9.4 大小 QMT 方法/接口对等清单（2026-10-01 实测）

尺寸口径：**两套词汇表**不是缺陷，是历史形成的两个世界 —— 把它们混为一谈才是缺陷。

| 世界 | 抽象基类 | 词汇示例 | 谁在用 |
|---|---|---|---|
| **网关侧**（`b.gateway.*`） | `XTQuantGateway` | `query_cash` / `query_position` | `sync/`、`gateway/execution.py`、大小 QMT 共用同一套调用点 |
| **适配器侧**（`b.adapter.*`） | `BrokerAdapter` | `get_cash` / `get_positions` | miniQMT 直连（`XTPQuantAdapter`）、大 QMT 桥兼任 adapter 槽位 |

实测（`inspect.signature` + `__abstractmethods__`，脚本 `scripts/check_execution_architecture.py`
与 `tests/test_bigqmt_bridge_face.py` 把它们固化成门禁）：

| # | 检查项 | 实测结果 | 判据位置 |
|---|---|---|---|
| A | `XTQuantGateway.__abstractmethods__` 里的方法在 `_BigQmtGateway` 上是否齐全 | 齐全；仅 `start`/`close` **豁免**（生命周期在 `BigQmtBridge` 上） | `test_bigqmt_gateway_covers_mini_adapter_abc` |
| B | 同名方法的**形参名/顺序**是否一致 | 与 ABC **零差异** | `test_bigqmt_gateway_accepts_the_same_keywords_as_mini` |
| C | 产线里出现的每个 `<x>.gateway.<name>` 是否都在网关上 | 全在（AST 扫产线调用点，非手抄） | `test_gateway_covers_every_name_used_in_production` |
| D | 产线里出现的每个 `<x>.adapter.<name>` 是否都在桥上 | 全在 | `test_adapter_covers_every_name_used_in_production` |
| E | `BrokerAdapter.__abstractmethods__` 是否都被 `BigQmtBridge` 实现 | 全实现 | `test_broker_adapter_abstract_surface_is_covered` |
| F | 网关 vs mini **具体适配器**的形参加宽是否已登记 | 仅 `subscribe_quote` 多 `period`，已登记 | `test_bigqmt_gateway_widening_over_the_mini_contract_is_declared` |

**已登记的唯一差异**：`XTPQuantAdapter.subscribe_quote(codes, on_tick, period)`
比网关侧 `subscribe_quote(codes, on_tick)` 多一个可选 `period`。

- 大 QMT 桥**故意不接**：agent 侧落地形态是轮询 `get_full_tick`（tick 粒度），
  周期传过去只会被**无声忽略** —— 「收下却不生效」比「根本没有这个参数」更糟。
- 与 **ABC**（真正的契约）签名完全一致；产线唯一调用点 `sync/__init__.py` 只传 2 个位置参数。
- 该差异由 `test_gateway_never_receives_the_mini_only_period_kwarg`（AST 扫产线调用点）
  守住：谁哪天照着 mini 的签名去调网关，门禁立刻变红，而不是等到运行期 TypeError。

### 9.5 跨进程链路上「安静地死掉」的两个坑（A13 / A12，均已有回归用例）

这两个坑的共同形态：**所有健康指标全绿，但那条链路已经死了**。它们是本项目
「绿灯被另一个 bug 遮出来」的第 N 次复现，故单列成文。

1. **事件游标不能用 agent 的 `seq`**（`connectors/transports/file_signal.py`）。
   `seq` 是 agent **进程内**计数器（`BIGQMT_AGENT._STATE["seq"]` 从 0 起），
   策略重启后**从 1 重来**；而 `events.ndjson` 是 **append** 的 ⇒ 文件里出现
   「旧会话 1..42」紧跟「新会话 1..」的**回卷**。旧实现 `last = max(last, seq)`
   包住水位 ⇒ 新会话里 `seq ≤ 42` 的事件**全被吞掉**，且只要新会话还没跑过 42 条，
   整条事件通道就是死的（前端委托/成交永不刷新、行情停在种子值）。
   ⇒ 改为**字节偏移游标**（文件只增不减，天然免疫回卷），并用**文件身份**
     （`st_ino`）识别轮转 —— 轮转后新文件可能**比旧游标还大**，只看体积回缩会漏。
   redis/中继路径同理：`RedisTransport.stream_events` 识别「seq down 拐点」并
   从拐点重放（不是从 0 重放，否则上个会话的事件会再投一遍）。

2. **WAL 指纹的两种口径不可直接比**（`gateway/db_backup.py`）。
   `wal_checkpoint(TRUNCATE)` 折不动（有活跃读者 / 磁盘满）时，指纹会退化成
   **文件形态**口径；下一次折叠成功又是**归一化**口径。两者形状必然不等，
   旧代码 `cur != old` **恒为不等** ⇒ 每次「立即备份」白复制一份整库（实测主库 1.4 GB）。
   ⇒ 指纹带 `norm` / `pre_wal` 标记，先分形态再比较；同时 `pre_wal` 这类**标记**
     绝不能进同形态的 `==`（否则跳过永远失效，比原缺陷更糟）。

### 9.6 wire action 词表五方对账 + 桥的存活判据（2026-10-01 第二轮）

#### 9.6.1 wire action：一根纯约定字符串，两侧各有手抄表 ⇒ 现在有门禁

走大 QMT 桥时，一次业务的完整链路是：

```
BrokerAdapter 方法 → canonical Ops → dialect.op_of() → **wire action**
  → agent execute() 路由 → 真 QMT 注入函数
```

中间那根 `wire action` 是**纯字符串约定**，而它同时存在于五处：

| # | 位置 | 角色 | 现状 |
|---|---|---|---|
| 1 | `connectors/dialects/bigqmt_v1.py::_OP_TO_ACTION` | 后端「会发什么」 | 14 个动作 |
| 2 | `connectors/dialects/base.py::Ops` | canonical 词表（17 个） | 多对一映射到 1 |
| 3 | `agent_bigqmt/qmt_api.py::_ACTIONS` | agent **自述**（PROBE 上报） | 14 个 |
| 4 | `agent_bigqmt/qmt_api.py::execute()` 的 if 链 | agent **真实路由** | 14 个 |
| 5 | `tests/fake_bigqmt_agent.py::_ACTIONS` + 分支 | 契约替身 | 14 个 |

这五处**每一处的注释都写着「必须与 X 保持同步」**，但没有任何东西在核。漏一个的后果
不是报错，而是运行期回一句 `未知 action`（业务挂、测试全绿），或者 probe 把「已支持」
谎报成「不支持」（能力面反向假阴性）。⇒ `tests/test_bigqmt_bridge_face.py` §6 新增
**AST 对账门禁**（5 条用例）：

* 自述 vs 真实路由（两个方向都判）；
* 方言映射 vs agent 路由（两个方向都判）；
* 替身 vs 真身（替身**不得**比真身宽或窄 —— 否则测试证明的是替身的行为）；
* 扫描器自证（不得把 `error_type` 之类的字符串误当 action）；
* 三个方言的 canonical op 覆盖率：两个产线方言必须全覆盖，模板方言
  （`ptrade.v1`，docstring 明说未经实机验证）的缺口必须**登记**在
  `_TEMPLATE_DIALECT_GAPS` 里 —— 补齐即红，逼着维护登记。

#### 9.6.2 桥的「已连接」曾经永不转假：一个典型假绿灯

```python
# 旧实现
def is_connected(self):
    return self._connected      # 只在 start() 置真、只在 stop()/close() 置假
```

agent 进程死掉（QMT 被关 / 策略被停止）之后，这个标志**永远是真**：

* 前端一直显示「已连接」；
* `gateway.health._check` 恒判 `ok=True` ⇒ `health_status` 恒为 `connected`，
  **`_reconnect` 永远不会被调度**（自愈能力形同不存在）；
* 用户每一次操作都要自己撞上 503 才发现「其实早就断了」。

文件桥没有「连接」可言（它只是两个目录），所以唯一诚实的判据是**一次真实的请求/应答
往返**。修复口径（`connectors/bigqmt_bridge.py`）：

| 机制 | 取值 | 理由 |
|---|---|---|
| 存活戳刷新点 | `call` / `call_locked` / 订阅对账 / 握手 / `test_connection` 成功 | 业务流量本身就是证据 ⇒ 有流量时**零额外请求** |
| 空闲阈值 `_LIVENESS_IDLE_SECONDS` | 90 s | 略大于 `_QUOTE_RESYNC_SECONDS`(60 s)：正常有订阅时那 60 s 的盲重发就是证据，永不触发探活 |
| 探活超时 `_LIVENESS_PROBE_TIMEOUT` | 5 s（远小于 `MAX_TIMEOUT` 30 s） | agent 真死时每次探活都要等满超时，用 30 s 会把同轮的事件派发一起拖住 |
| 退避 `_LIVENESS_RETRY_SECONDS` | 30 s | 与订阅重试同思路：不风暴，且 agent 恢复后能自动接上 |
| 判死阈值 `_LIVENESS_FAILURES_TO_DIE` | 2 次连续无应答 | 1 次就判死会把「agent 正在写大响应」误报成掉线 |

**为什么不用 `agent_status.json` 心跳新鲜度**：`handlebar` 只在**行情推进**时被 QMT
调用，收盘后心跳自然过期 —— 拿它当判据会在非交易时段把一切正常的连接误报成掉线，
进而触发重连风暴（比假绿灯更糟）。往返判据与时段时间无关。

诊断面同时上浮 `agent_unresponsive` / `liveness_failures` / `last_agent_ok_age_s` /
`available`，把「曾经连上」与「现在可用」在 `/brokers/{id}/health` 的 `bigqmt` 段里
分成两个量。回归用例见 `tests/test_bigqmt_bridge_face.py` §7（含**接线判据**：
泵循环里必须真的调用探活 —— 本项目多次栽在「逻辑写好了但没人调」）。

#### 9.6.3 模块按职责拆分：`bigqmt_bridge.py` → `+ bigqmt_gateway.py`（2026-10-01 第三轮）

上面 9.6.2 那份存活判据（4 个常量 + 探测方法 + 一段不短的说明）把
`connectors/bigqmt_bridge.py` 顶到了 **51.8 KB**，越过
`scripts/check_execution_architecture.py` 的 **Gate 4**（非测试 `*.py` ≤ 50 KB）——
该门禁**接在 CI 上**，于是这一条会让整条流水线变红。

拆分的判据不是行数，而是**依赖方向**；本文件当时确实装着两类关注点：

| 模块 | 关注点 |
|---|---|
| `connectors/bigqmt_bridge.py` | 双槽位本体（`Connection.bridge` + `Connection.adapter` 同一对象）、事件泵、行情订阅对账、`is_connected()` 存活判据 |
| `connectors/bigqmt_gateway.py` | **gateway 面**：canonical op → 旧 `XTQuantGateway` shape 的适配（`_BigQmtGateway`） |

能干净切开是因为两边的私有助手**互不相干**（已 grep 证实）：

* `_split_instrument` / `_snapshot_to_legacy` / `_raw_of` —— 只被 gateway 面用；
* `_call_with_optional_timeout` / `_canonical_to_ws` —— 只被桥本体用；
* 依赖是**单向**的：`bigqmt_gateway` 对桥只做**鸭子类型**调用
  （`is_connected()` / `want_quotes()` / `_stamp_agent_ok()`），**不 import**，
  因此不产生 `bridge → gateway → bridge` 的环。

拆完 **51.8 KB → 40.6 KB**，**零行为变化**：`tests/test_bigqmt_bridge_face.py` 仍把
**两套面放在同一个文件里对账**（从 `connectors.bigqmt_gateway` 取 `_BigQmtGateway`），
因为它们是**同一个 `Connection` 对象的两套契约** —— 分开测就失去了「两套面互不矛盾」
这个断言的意义。

★ 刻意**不**采用「把注释删短一点绕过上限」：上限是「关注点混杂」的**代理指标**，
删注释只会把指标刷绿、把问题留下，下一次加功能又被顶穿。

