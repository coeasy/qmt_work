# 大 QMT（Big QMT）兼容 —— 架构与详细实现方案

> 版本：v1.0（2026-10-01）
> 定位：本文是 **实施收口方案** —— 基于代码实际现状（而非计划设想），给出大 QMT 兼容的完整架构、剩余缺口、分阶段详细实现与验收门禁。
> 上位文档关系：
> - `docs/UNIVERSAL_BROKER_PLATFORM_FINAL_PLAN_V4.md` —— 平台级纲领（本文是其 Phase 2-3 的执行细化）；
> - `docs/BIG_QMT_COMPAT_PLAN.md`（v1.1）—— 初版方案，其 §4.2/§5.2.4 形态已被修订，**与本文冲突处以本文为准**；
> - `docs/UNIFIED_TRADING_ABSTRACTION.md` §7 —— 对初版方案的 4 处修正（本文全部采纳并已在代码中落地）。
> 参考实现调研结论见 §4（外部证据均标注来源）。

---

## 1. 结论先行

1. **架构已定型且大体落地**：大 QMT 兼容走「双通道」——路径 A（xtquant 直连大客户端，`client_mode=full`）+ 路径 B（大 QMT 内置策略桥接，`Dialect×Transport` 组合）。路径 B 的核心骨架（wire 协议、文件桥 transport、BigQmtV1 dialect、事件差分、py3.6 agent、DB/配置字段、测试替身）**均已在仓库中**，不重绘架构。
2. **但闭环没有完成**：真正阻断用户使用的是三段「最后一公里」——① REST/前端无法创建 bigqmt 桥接连接（字段没透传、表单没有）；② canonical 端口灰度开关 `QMT_USE_PORTS` 默认 0，桥接链路上线路径待固化；③ redis/zmq 传输后端已实现但 **agent 侧只能跑 file**，组合表里两个格子端到端不可用。
3. **外部调研带来 8 个增量设计决策**（§4.3），其中 3 个是正确性问题：`get_trade_detail_data` 必须主线程执行（后台线程返回空）、`m_nDirection` 恒 48 不可信需字段仲裁、行情/委托查询在低轮询间隔下的 drain 时间预算；其余为加固项（编码口径、股票简称泄漏、TTL 防幽灵单等）。
4. 本文给出 **M0~M4 五个里程碑、逐项到文件与验收**的实现计划（§6），并附硬门禁清单（§8）。

---

## 2. 现状基线（以代码为证，2026-10-01 核读）

### 2.1 已落地 ✅

| 能力 | 位置 | 状态 |
|---|---|---|
| BrokerAdapter 统一抽象 + 异常体系（跨进程文案重建 `BrokerSDKError.from_message`） | `backend/xtquant_client/base.py` L88-101/L104+ | 在产线 |
| ABI 子进程桥（行分隔 JSON-RPC，反射转发） | `xtquant_client/bridge_client.py` / `bridge_server.py` | 在产线 |
| canonical 端口层（12 端口：9 窄接口 + EventPort + CapabilityProbePort 等） | `backend/connectors/ports.py`（EventPort L301、CapabilityLevel.UNKNOWN L315-326） | 已实现 |
| Dialect×Transport 正交组合 | `connectors/generic.py`、`connectors/registry.py` L33-40（组合表 6 格）、`connectors/dialects/{xtquant_v1,bigqmt_v1,ptrade_v1}.py` | 已实现 |
| 传输：inprocess / file_signal / redis / zmq / http_gateway | `connectors/transports/*.py`（redis/zmq 见 `lowlatency.py`） | file 端到端可用；redis/zmq 仅后端侧 |
| wire 信封（WireRequest/WireResponse，signal_id uuid、INV-8 超时有界、write 标记串行化） | `connectors/transport.py` L41-104 | 已实现 |
| 事件合成：agent 侧 diff（首轮建基线、废单→order_error）+ 后端翻译（不再二次 diff，状态过 SSOT） | `agent_bigqmt/qmt_api.py` `diff_events`；`connectors/events.py` L189-221；延迟上界集中于 `transports/semantics.py` | 已实现（file 通道） |
| 委托状态归一复用同一 SSOT（未另建 status_map，v1.1 修正 ①兑现） | `xtquant_client/order_status.py` L70-93 | 已实现 |
| QMT 内置 Python 策略端 agent（py3.6、stdlib-only、file 桥） | `backend/agent_bigqmt/`（入口捕获注入函数 L73-85、token 校验、signal_id 文件名一致校验、原子写、`trading_enabled` 默认 false、10MB 事件轮转）+ `DEPLOY.md` + `agent_config.example.json` | 已实现（action 覆盖见 2.2-G4） |
| py3.6 兼容 CI 闸 | `scripts/check_bigqmt_agent_py36.py` | 已实现 |
| DB / 配置字段（connector_key / bridge_dir / auth_token 幂等补列；ConnectionConfig 贯通；manager 装配 bigqmt bridge） | `core/db_migrations.py` L737-739、`xtquant_client/manager.py` L134-151/L266-307 | 已实现 |
| 端口灰度开关 + InProcess 提升（禁静默回退） | `gateway/execution.py` L20/L31-65（`QMT_USE_PORTS`） | 已实现，默认 0 |
| EasyTrader 风格 façade（无逃逸口、空必可解释） | `gateway/easytrader_facade.py` | 已实现 |
| 测试与替身 | `backend/tests/test_bigqmt_file_bridge.py`、`fake_bigqmt_agent.py`、`test_connector_{composition,ports,canonicalize}.py`、`test_connectors_phase8.py` | 已实现 |

### 2.2 缺口 ❌（本方案要闭合的对象）

| # | 缺口 | 证据 | 影响 |
|---|---|---|---|
| G1 | **REST 不透传桥接字段**：`app/routes/broker.py` 仅透传 `client_mode`（L102/L244/L293），`connector_key`/`bridge_dir`/`auth_token` 未进请求体 | 代码核读 | 用户无法经 UI/API 建立大 QMT 桥接连接 —— **闭环第一阻断点** |
| G2 | **前端无 bigqmt 表单**：`frontend-next/src/domains/system/Brokers.tsx` 只有 client_mode 展示，全前端无 `connector_key/bridge_dir` 字面量 | grep 空命中 | 同 G1；连接向导、诊断面板缺位 |
| G3 | **redis/zmq 端到端不可用**：agent 仅支持 file（`BIGQMT_AGENT.py` L101-106），后端 `RedisTransport/ZmqTransport.stream_events()` 诚实返回空（`lowlatency.py` L115-121） | 代码核读 | 组合表 `qmt.big.bridge.redis/zmq` 两格：请求可通、**事件流断**（委托/成交回报无处送达），且 agent 侧无对应服务端 |
| G4 | **agent action 覆盖不全**：`QUERY_STOCK_LIST / QUERY_CALENDAR / SUB_QUOTE` 显式拒绝（`qmt_api.py` L171-173）；`QUERY_KLINE` 依赖的下载链路未实机验证 | 代码核读 | 板块列表/交易日历/行情订阅能力面缺，`_BoundBrokerSource` 注册范围受限 |
| G5 | **PROBE→health 未闭环**：`/api/v1/brokers/{conn_id}/health`（`app/routes/broker.py` L405）未接入 connector 的 probe 结果（注入函数清单、transport 可达性、事件延迟上界）；`CapabilityProbePort` 未接进健康判定（DEGRADED vs FAILED） | 代码核读 | 排障全靠看日志，四类根因（路径/token/函数缺失/传输不可达）不可区分 |
| G6 | **路径 A 未完**：`BROKER_PROFILES`（`xtquant_client/registry.py` L51-112）无 `full_client_path`/`userdata` 候选；`diagnose_trade_connect` 无「PID 白名单」根因分支与路径 B 引导 | 代码核读 | client_mode=full 直连体验与「被封后知道怎么办」的引导缺失 |
| G7 | **正确性加固项**（来自外部实测证据，详见 §4.3）：D1 主线程执行 `get_trade_detail_data`；D2 方向字段仲裁；D3 drain 时间预算；D4 入口文件编码口径（agent 现为 `# -*- coding: utf-8 -*-`，参考项目实证 QMT 内置 3.6 用 `#coding:gbk`+源码 ASCII）；D5 中文自由文本走 ensure_ascii=False | `agent_bigqmt/BIGQMT_AGENT.py` L1 | 部分券商终端上**查询静默返空/乱码**级隐患，实机联调前必须按 probe 验证 |
| G8 | **契约与计数收口**：桥接相关新契约（wire 信封、semantics 上界）是否已进 `tests/contracts` 基线、`EXPECTED_TESTS`/README 计数未随本轮新增重算 | 待 `ci_reconcile` 验证 | CI 门禁红/假绿风险 |
| G9 | **实机联调零记录**：全部验证基于 fake agent（协议替身），未在任何真实大 QMT 终端跑通过一笔委托 | — | 最高风险项：所有「已实现」都只在协议层成立 |

#### 2.2.1 缺口闭合状态（2026-10-01 复核；本表随代码更新，别当下面的原始缺口描述已过时）

| # | 状态 | 判据（可复算） |
|---|---|---|
| G1 | ✅ 已闭合 | `app/routes/broker.py` 请求体已含 `connector_key` / `bridge_dir` / `bridge_transport` / `auth_token` / `redis_url` / `zmq_addr`；`test_broker_routes_bigqmt_fields.py` 断言字段落库 |
| G2 | ✅ 已闭合 | `frontend-next/src/domains/system/Brokers.tsx` + `bigqmtProbe.ts`（含 `bigqmtProbe.test.ts`）；连接向导与诊断面板已接 `/brokers/{id}/health` 的 `bigqmt` 段 |
| G3 | ⚠️ **仍开放（已是「诚实开放」）** | `lowlatency.py::stream_events()` 对 redis/zmq 如实返回 `[]`，不伪造事件；2026-10-01 补了**会话回卷检测**（agent `seq` 每进程从 1 重开 ⇒ 消费端不能拿 `seq` 当水位）。要真正可用仍需一个带 redis/pyzmq 的 agent 变体（破 py3.6 stdlib 约束），**明确不在本轮范围** |
| G4 | ✅ 已闭合 | `qmt_api.py::_market_extra` 已实现 `QUERY_STOCK_LIST / QUERY_SECTOR_LIST / QUERY_INSTRUMENT / QUERY_CALENDAR / SUB_QUOTE`（M2.1 解除「桥接模式暂不支持」）；`test_bigqmt_marketdata_e2e.py` 覆盖 |
| G5 | ✅ 已闭合 | `/api/v1/brokers/{conn_id}/health` 已在响应里附 `bigqmt` 段（agent 元数据 + `captured` 注入函数清单 + 传输可达性 + 事件上界） |
| G6 | ✅ 已闭合 | `xtquant_client/registry.py` 的 `BROKER_PROFILES` 增补路径候选；`xtp/diagnostics.py` 增「PID 白名单」根因分支并给出路径 B 引导 |
| G7 | ✅ 已闭合 | D1 主线程执行 `get_trade_detail_data`、D2 方向字段仲裁（`prepare` 缺 `side` 直接 `ValueError`）、D3 `ttl_ms` 幽灵单防护、D4 编码口径（bundle 默认 GBK）、D5 `ensure_ascii=False` |
| G8 | ✅ 已闭合 | `ci_reconcile`：`EXPECTED_TESTS=1891` / 166 个 `test_*.py` / 组件 72 / 页 43 / README 计数 / 行尾；wire 信封与 semantics 上界已进 `tests/contracts` |
| G9 | ⚠️ **仍开放** | 真机取证已覆盖「进程/注册树字节锁/日志/授权串/CEF 调试端口/策略列表」，但**尚未跑通一笔真实委托**。发布口径：首次使用必须走 `DEPLOY.md` §11 清单（最小仓位三条路径） |

> **两条「安静地死掉」的教训（本轮新增）**：A13 —— agent `seq` 回卷导致成交/行情事件被消费端
> 静默丢弃（心跳全绿、指标全绿）；A12 —— 同一家族在 redis 传输上的孪生缺陷。两者均已修并有
> 回归用例，详见 `docs/BIG_QMT_COMPAT_PLAN.md` §9.5。

---

## 3. 目标架构（定型版）

### 3.1 双通道决策（不变）

| 通道 | 组合键 | Dialect | Transport | 适用 |
|---|---|---|---|---|
| 路径 A | `qmt.big.direct` | xtquant.v1 | inprocess（ABI 不合自动切子进程桥） | 券商未封外部直连时优先，延迟最低 |
| 路径 B-文件 | `qmt.big.bridge.file` | bigqmt.v1 | file_signal | 直连被封时的零部署兜底，**当前唯一端到端可用** |
| 路径 B-低延迟 | `qmt.big.bridge.redis` / `.zmq` | bigqmt.v1 | redis / zmq | M3 里程碑补 agent/中继侧后启用 |

### 3.2 分层与不变量

```
REST/WS/MCP ─→ SignalRouter.submit（唯一交易入口：幂等/风控/TOTP/WAL/审计，客户端无关）
                    └→ ExecutionService ── QMT_USE_PORTS 灰度 ──┬→ 旧 bridge 路径（现状默认）
                                                              └→ GenericConnector（Dialect×Transport）
                                                                     ├→ XTPQuantAdapter / BridgeAdapter（路径 A）
                                                                     └→ FileSignal/Redis/Zmq → 大QMT agent（路径 B）
生命周期：ConnectionSupervisor（六态 FSM，probe 结果决定 DEGRADED vs FAILED）
事件面：  EventPort（PUSH | PUSH_WITH_GAP | POLL_DIFF + max_latency_ms 上界 → order_watchdog 消费）
```

不可动摇的不变量（全部已有护栏，新增代码必须继续满足）：
- INV-1 交易只经 SignalRouter；INV-3 状态判定只过 `order_status.py` SSOT；INV-8 一切 IO 超时有界；
- 零 mock：未连券商 → 200+`code=503`+引导；柜台拒单 → 400+真因；失败绝不包 `ok=true`/`code=0`；
- 能力诚实：单点探测只能得 `UNKNOWN`，禁止「终端没有 XX 接口」类断言文案。

### 3.3 wire 协议（SSOT，现状已实现，冻结为 v1）

```jsonc
// 请求（外部端 → agent）：req/<signal_id>.json 或 redis 队列 bigqmt:req
{"v":1,"signal_id":"<uuid16>","op":"PLACE|CANCEL_ORDER|QUERY_*|PROBE","ts":<ms>,"auth":"<token>","params":{...}}
// 响应：resp/<signal_id>.json 原子写；error_type ∈ BrokerError|BrokerNotConnected|Unsupported
{"v":1,"signal_id":"…","ok":true,"result":{...},"error":"","error_type":"",
 "ts":<ms>,"agent":{"ver":"…","py":"3.6.x","funcs":[...已捕获注入函数...]}}
// 事件流：events.ndjson 追加（agent 侧 diff 后），{"seq":n,"type":"order|trade|order_error","data":{...}}
```

action/op 词表是 **dialect 内部 wire 格式**，不进 REST/WS 契约面（v1.1 修正 ④，已兑现）。

---

## 4. 参考项目增量决策（外部证据 → 本仓库动作）

### 4.1 已验证一致、无需改动的判断

| 判断 | 本仓库现状 | 外部互证 |
|---|---|---|
| 不新建大 QMT 状态映射表，复用 SSOT | `connectors/events.py` L136-151 显式注释禁硬编码 | 三方文档对 50/56/57 语义记载**互相矛盾**（xtquant: 56=已成；大 QMT 文档有版本写 56=已拒）→ 更加证明只能过 SSOT+实机校准 |
| 注入函数唯一来源捕获、禁手抄名单 | `BIGQMT_AGENT.py` L73-85 + CI 闸 | xtquant_big_convert 同模式 + 测试闸禁手抄 |
| 轮询 diff：首轮建基线不回放、废单合成 order_error | `qmt_api.diff_events` + `events.py` L17-20 | SecondaryExecPoller 同纪律（sysid 键 + status/成交量变化才发；trade_id 去重） |
| 传输可插拔不改协议 | registry 组合表 | RpcTransport 一配置键切换 |
| 下单默认关闭 | `trading_enabled` 默认 false | `rpc_allow_order_methods=False` 默认关单 |

### 4.2 参考项目有、本仓库刻意不采用的

- mysql/shm 传输（3.6 无 shared_memory，参考项目自身也只是 stub）；
- 整套 Redis 键空间设计（本仓库信封更薄，够用）；
- easytrader 同花顺 UI 自动化路线（脆弱、不可观测，维持排除）。

### 4.3 新增决策（外部实测证据驱动，落入 M2/M3）

| # | 决策 | 证据（来源） | 本仓库动作 |
|---|---|---|---|
| D1 | **`get_trade_detail_data` 类查询必须在大 QMT 主线程（handlebar/run_time tick）内执行**；后台线程调用返回空列表 | xtquant_big_convert「inline/deferred 两档」实测 | 审计 `qmt_api.poll_once` 的调用线程模型：确认全部 QMT API 调用发生在 `handlebar`/`ContextInfo.run` 回调线程；加回归断言（fake agent 记录调用线程标记）。若未来引入后台线程，必须改为「主线程执行+线程池只做文件 IO」 |
| D2 | **买卖方向字段仲裁**：`m_nDirection` 实测恒 48 不可信，按 `offsetFlag → direction → opType` 链仲裁 | 同上 | agent `QUERY_ORDER/DEAL` 字段映射处实现仲裁链，输出归一化为 `buy/sell`；无法判定时如实输出 `unknown` 并计入 probe 诊断，不猜 |
| D3 | **drain 时间预算**：QMT 终端 C++ 主循环尾延迟可达 ~490ms，桥接任务必须带预算执行，超预算让出、下轮续跑；配合请求 `ttl` 过期拒绝防「幽灵单」（客户端已放弃的迟到的响应不得再触发下单） | 同上（`run_time("adjust", 100-200ms)` drain、RequestExpired） | wire 信封 `params` 可选 `ttl_ms`（向后兼容：缺省不过期）；agent 执行 PLACE 前核对 `ts+ttl_ms >= now`，过期回 `error_type=Expired`；后端 file_signal 已有超时，超时后**必须经 QUERY_ORDER 对账**而非盲目重发（复用 order_watchdog 的 unknown 对账语义） |
| D4 | **入口文件编码口径实机校准**：agent 入口现为 UTF-8 声明，参考项目实证部分券商内置 Python 需 `#coding:gbk` + 源码 ASCII | xtquant_big_convert 部署约束 | 保持 UTF-8 为默认（现有 py36 闸基于此），在 DEPLOY.md 增加「乱码/SyntaxError 时改 `#coding:gbk` 重存」排障条目；PROBE 上报解码自检结果 |
| D5 | **自由文本中文的传输安全**：agent 事件/错误文案走 `ensure_ascii=False`，跨进程/跨编码环境有乱码前科（本机控制台 GBK 曾实测抛错） | 外部实测 + 本仓库环境记忆 | 后端读 `events.ndjson`/`resp/*.json` 显式 `encoding="utf-8"`（审计 file_signal 现有读路径）；若实机发现券商审计拦截（参考项目曾以 b64 规避 "Sensitive Data Detected"），再启用 `QMT_BIGQMT_ASCII_ONLY=1` 强制 `ensure_ascii=True`（一个开关，协议不变） |
| D6 | **broker 侧回调注册双轨**：部分券商普通账户有 `order_callback/deal_callback` 可绑 | xtquant_big_convert 有推送走回调、无推送走轮询 | agent 增加 `try_bind_callbacks()`：绑定成功→PROBE 上报 `callback_bound=true`，diff 仅作兜底对账；失败→维持纯轮询。事件延迟上界据此在 `semantics.py` 动态修正 |
| D7 | **命令字典参数裁剪在网关层集中做** | easytrader #520（follower 透传多余 key `entrust_prop` 崩在严格签名） | `bigqmt_v1.prepare()` 产出的 params 即白名单（现状已按 op 构造）；补一条契约测试：未知/多余键必须在 dialect 层被丢弃而非到达 agent |
| D8 | **股票/账户字段混淆** | 参考项目对代码字段 b64 混淆以过终端关键字审计 | **暂不采用**（无证据表明 qmt_work 目标券商有此拦截）；列为 M4 实机联调的应急预案 |

---

## 5. 架构不变式与防退化护栏（新增/扩展）

| 护栏 | 形态 | 落点 |
|---|---|---|
| 禁绕过端口/SignalRouter | 扩展现有 `scripts/check_execution_architecture.py`：`app/routes`、`engines`、`gateway`（白名单除外）禁止直接 import `connectors.transports.*` | M1 |
| 禁手抄注入函数名单 | 已有：入口函数字面量 >5 即红 + `check_bigqmt_agent_py36.py` | 保持 |
| 事件语义单一来源 | `max_latency_ms` 只从 `transports/semantics.py` 读取；watchdog/health/前端三处消费同数 | 已有，M2 接 health 时补断言 |
| 状态判定过 SSOT | INV-3 已钉；`normalize_order_status` 是唯一入口 | 保持 |
| 协议向后兼容 | wire 信封「未知字段忽略」双侧契约测试（后端 fake agent + agent 侧样例响应文件） | M1 |
| 零假绿灯 | `QMT_USE_PORTS=1` 装配失败必须显式抛错（现状已实现）；桥接 transport 缺依赖（redis/pyzmq）抛 `MissingDependency` 不静默降级（现状已实现） | 保持 |

---

## 6. 分阶段实现计划（M0~M4）

### M0 — 配置闭环：让大 QMT 连接「建得出来」（预计 2~3 个工作日当量）

| # | 改动 | 文件 | 验收 |
|---|---|---|---|
| M0.1 | REST 透传桥接字段：create/update/test 三个入口 body 增加 `connector_key/bridge_dir/auth_token`（默认为空=旧行为不变）；校验规则：`connector_key` 非空时必须命中 `connectors/registry.describe` 已知组合，缺失必填项（如 file→bridge_dir）返回 400+明确文案 | `app/routes/broker.py` L102/L244/L293 附近 | 新增 `test_broker_routes_bigqmt_fields.py`：非法 key 400、缺 bridge_dir 400、成功创建后 `load_persisted` 回放一致 |
| M0.2 | 前端连接表单：类型+表单增加「接入模式」三选（xtquant 直连(默认) / 大QMT桥接-文件 / 大QMT桥接-Redis / 大QMT桥接-ZMQ → 映射 connector_key），选桥接时显示 bridge_dir/auth_token/agent 部署说明折叠面板；stores/broker.ts 提交体补字段 | `frontend-next/src/services/api/broker.ts`、`src/stores/broker.ts`、`src/domains/system/Brokers.tsx` | `npm run test:serial` 通过；手动走查：新建→连接失败时 503 引导文案含「检查 agent 运行/两边路径一致/token」三类排查项 |
| M0.3 | 连接去重回归：bigqmt 连接纳入 `dedupe_identical`（键含 connector_key+bridge_dir+account_id） | `xtquant_client/manager.py` | `test_broker_dedupe` 扩展用例通过 |
| M0.4 | DEPLOY.md 与前端表单字段口径对齐（对称字段清单：bridge_dir/transport/auth_token/account_id） | `backend/agent_bigqmt/DEPLOY.md` | 文档 diff 审查 |

### M1 — 端口固化：`QMT_USE_PORTS` 灰度收口（预计 3~5 天当量）

| # | 改动 | 文件 | 验收 |
|---|---|---|---|
| M1.1 | 桥接连接**强制走端口层**：`manager._build_bigqmt_bridge` 产出的 BigQmtBridge 本身即 GenericConnector 包装，SignalRouter→ExecutionService 对其调用路径逐条核账，确保不存在「半端口半 adapter」混跑 | `xtquant_client/manager.py` L266-307、`gateway/execution.py` | 新增 `test_bigqmt_port_path.py`：connector_key 连接的下单必须经过 `GenericConnector._call`（以 fake agent 收到的 op 序列为证） |
| M1.2 | 直连连接双跑对比：`QMT_USE_PORTS=0/1` 各跑一遍现有交易回归（`test_risk_regression` 等），断言下单参数/回执/审计逐条一致 | `backend/tests/` | 对比脚本输出一致报告；差异清单为零或已解释 |
| M1.3 | 灰度开关默认值评估：若 M1.2 一致率 100%，把默认翻为 1（保留 =0 逃生通道一个版本周期）；否则列出阻断项转缺陷修复 | `gateway/execution.py` L20 | CI 全绿 + README/环境变量文档更新 |
| M1.4 | 未知键丢弃契约测试（D7） | `tests/test_bigqmt_protocol*.py` | 通过 |

### M2 — agent 能力补全 + 正确性加固（预计 4~6 天当量）

| # | 改动 | 文件 | 验收 |
|---|---|---|---|
| M2.1 | 补齐 action：`QUERY_STOCK_LIST`（`get_stock_list_in_sector`）、`QUERY_CALENDAR`（`get_trading_dates`）、`GET_DATA` 历史 K 线下载（`download_history_data` + `get_market_data` 分页回传，单响应体积上限+分块游标） | `agent_bigqmt/qmt_api.py` L171-173 解除拒绝；`connectors/dialects/bigqmt_v1.py` 映射 | fake agent 全覆盖单测；capabilities 按 PROBE 结果动态注册进 `_BoundBrokerSource` |
| M2.2 | 方向字段仲裁链（D2）+ 无法判定输出 unknown | `qmt_api.py` 映射层 | 单测：三字段齐缺各种组合的判定表 |
| M2.3 | 主线程执行审计（D1）：确认/固化「QMT API 只在 handlebar tick 内调用」，文件 IO 才允许出线程 | `BIGQMT_AGENT.py` / `qmt_api.py` | fake agent 线程标记断言 |
| M2.4 | `ttl_ms` 过期拒绝 + 超时对账不盲重发（D3） | `connectors/transport.py`（透传字段）、`file_signal.py`、`qmt_api.py` | 用例：过期 PLACE 拒执行回 Expired；file 超时后发起 QUERY_ORDER 对账路径 |
| M2.5 | 回调双轨（D6）：尝试绑定 `order_callback/deal_callback`，PROBE 上报 `callback_bound`；semantics 动态上界 | `qmt_api.py`、`transports/semantics.py`、`connectors/bigqmt_bridge.py` | 有/无回调两种 fake 场景的事件延迟断言 |
| M2.6 | 编码口径（D4/D5）：读路径显式 utf-8；`QMT_BIGQMT_ASCII_ONLY` 开关；DEPLOY.md 排障条目 | `file_signal.py`、`qmt_api.py`、`DEPLOY.md` | GBK 控制台环境下回归不再出现 UnicodeEncodeError（用本机实测） |
| M2.7 | PROBE→health 闭环（G5）：`/brokers/{conn_id}/health` 输出 `funcs/callback_bound/transport/max_latency_ms/clock_offset_ms`；supervisor 据 CapabilitySet 判 DEGRADED；文案禁绝对断言 | `app/routes/broker.py` L405、`connectors/registry|supervisor`、前端诊断面板 | 四类根因（路径不一致/token 错/函数缺失/agent 未运行）在 health 面可区分；契约基线更新 |

### M3 — 低延迟通道端到端（预计 3~5 天当量）

| # | 改动 | 文件 | 验收 |
|---|---|---|---|
| M3.1 | **决策：采用「本地中继 relay」而非往 QMT 内置 Python 塞第三方库**。`scripts/bigqmt_relay.py`（用户环境任意 py3.8+，可选开机自启）：对外 redis pub/sub + `blpop bigqmt:req` / 对内读写 `bridge_dir` req/resp/events —— agent 零改动，`transport=redis/zmq` 即获得**含事件流**的低延迟通道 | 新文件 + `connectors/registry.py` 组合表不变 | relay 单测（仿 fake_bigqmt_agent）；redis 链路端到端事件延迟 <100ms 实测记录 |
| M3.2 | `RedisTransport/ZmqTransport.stream_events()` 从「诚实空」升级为消费 relay 推送频道（`bigqmt:events:<ns>`）；`PUSH_WITH_GAP` 语义下断线重连补拉 ndjson tail 去重 | `lowlatency.py`、`events.py` | 断连重连用例：事件不重、不漏（seq 去重）|
| M3.3 | 健康面区分「redis 可达但 relay 未启动」（请求超时/事件为空的组合症状 → 明确诊断文案） | M2.7 的 health 面 | 场景用例 |
| M3.4 | （备选，仅当实机证明券商封 socket）**pipe transport**：kernel32 命名管道，双端 stdlib+ctypes 可实现，专治「禁 pip、禁 socket」终端 | `connectors/transports/pipe*.py`、agent 变体 | 触发条件：M4 实机联调发现封禁证据；否则不做 |
| M3.5 | 传输自动退回策略：仅允许**上层显式选择**（配置 `bigqmt.fallback=file`），默认保持零静默降级；退回事件计入 health 提示 DEGRADED | `manager.py` 装配处 | 用例：未开 fallback 时 redis 不可达直接 503 语义 |

### M4 — 路径 A 收尾 + 实机联调 + 收口（预计 2~4 天当量 + 实机档期）

| # | 改动 | 文件 | 验收 |
|---|---|---|---|
| M4.1 | `BROKER_PROFILES` 各 active 档案补 `full_client_path`（大 QMT 安装目录，交易目录 `userdata`），`_effective_trade_dir` 候选互备复用现状 | `xtquant_client/registry.py` L51-112 | 直连大客户端（未封 PID 券商）连接成功日志命中 userdata |
| M4.2 | `diagnose_trade_connect` 增加 PID 白名单根因分支：`ret error-1` + 客户端日志 `pid not allowed` → 文案输出「券商启用 PID 白名单，外部直连被拒 → 请改用大 QMT 桥接（路径 B）」并附入口 | `xtp/env.py` 等诊断面 | 构造场景用例命中 |
| M4.3 | **真机 runbook**（本方案最高优先验收）：`docs/BIG_QMT_FIELD_TEST.md` —— ①`capture` 函数清单实录；②每 action 一笔记价委托（trading_enabled 先 false 全查询、后 true 单笔委托+撤单）；③50/56/57 状态实测校准（对齐 SSOT）；④D1 线程模型/D4 编码/D5 审计拦截复验；⑤file 桥下单-成交-事件全链路计时 | 新文档 + 实录数据 | **在至少一台真实大 QMT 终端走完并留档**，任何「已实现」结论必须有该记录支撑，否则文档标 UNKNOWN |
| M4.4 | 一键部署体验：后端生成 agent 包（配置 bridge_dir/token 预填）+ 两端路径一致性预检（PROBE 时目录创建时间戳交叉核对） | `app/routes/broker.py` 新端点 + `agent_bigqmt` 释放逻辑（只读资源经 `exe_dir()` 释放，遵守 TD-26） | 部署向导端到端 |
| M4.5 | 收口：契约基线重生成、`EXPECTED_TESTS` 重算、README 两处计数、`docs/BIG_QMT_COMPAT_PLAN.md` 头部加「实施收口见 BIG_QMT_IMPLEMENTATION_PLAN.md」指针 | `scripts/ci_reconcile.py --update`（**run_in_background 跑**）等 | CI 全绿 |

---

## 7. 测试计划汇总（对齐现状资产）

| 层 | 载体 | 覆盖 |
|---|---|---|
| 协议 | `test_bigqmt_file_bridge.py` + `fake_bigqmt_agent.py`（扩展） | 信封版本协商/未知字段忽略、token 校验失败静默丢弃+计数、signal_id 与文件名一致、幂等、原子写、Expired |
| 组合 | `test_connector_composition.py`（扩展） | 6 组合可实例化、必填项缺失报错文案、redis/zmq+relay 事件流 |
| 端口契约 | `test_connector_ports.py` | EventPort 语义声明、CapabilityProbePort（UNKNOWN 一等公民）、12 端口 isinstance |
| 状态/事件 | `test_connector_canonicalize.py` + 新增仲裁表 | SSOT 归一、废单→order_error、首轮基线、seq 去重、D2 仲裁 |
| 路由面 | M0.1 新测试 | REST 字段透传/校验、503 语义、去重回归 |
| 灰度 | M1.2 双跑脚本 | 端口路径与旧路径行为逐条一致 |
| 门禁 | py36 AST 闸 / 手抄名单闸 / `check_execution_architecture` 扩展 / emit AST 全扫 / 行尾 `.gitattributes` | 全量保持并覆盖新增文件 |
| 前端 | `npm run test:serial`（并行会静默丢文件） | 表单三选、诊断面板 |
| 实机 | M4.3 runbook 留档 | 唯一能证伪「协议层已实现」的手段 |

---

## 8. 契约与 CI 同步清单（硬门禁，每里程碑收口一次）

1. `scripts/ci_reconcile.py::EXPECTED_TESTS` 重算（`--update`），README 两处计数同步；**前台必被 SIGTERM → 用 run_in_background**；
2. `tests/contracts/introspect.py` 扫描面扩展：wire 信封字段、`transports/semantics.py` 上界值、dialect op 映射 → `gen_contracts.py` 重生成 → 基线回归；
3. 新增/变更 WS 事件必须同步 `frontend-next/src/services/wsEvents.ts` + `tests/contracts/ws_events.json`；
4. 后端测试环境纪律：`CODEBUDDY_SAFE_DELETE_ENABLED=0`、`--basetemp` 放 OS 临时目录、`QMT_DB_BACKUP_ENABLED=0`、汇总信 JUnit XML 不信 shell 重定向；
5. emit 事件走 `core.emit.emit_event` 三参标准形态（AST 全量扫描自动覆盖新代码）。

---

## 9. 风险清单

| # | 风险 | 等级 | 对策 |
|---|---|---|---|
| R1 | **实机差异**：全部结论目前仅协议层成立（G9） | 高 | M4.3 前置到 M2 结束即穿插执行，不留到最后 |
| R2 | 券商对大 QMT 内置策略也收紧（行业趋势） | 高 | 多通道并存架构不动；行情侧 eltdx/公共源降级链保持 |
| R3 | 50/56/57 语义在券商间漂移（外部文档已现互相矛盾） | 中 | 过 SSOT + M4.3 实测校准表，校准结果记入 field-test 留档 |
| R4 | relay 进程成为新的运维负担/单点 | 中 | 默认路径 B-file 零依赖仍可用；relay 崩溃时健康面明确报「relay 未运行」，事件自动回落 tail |
| R5 | `QMT_USE_PORTS` 翻默认引回归 | 中 | M1.2 双跑一致率 100% 才翻；保留 =0 逃生一个版本周期 |
| R6 | 桥目录被本机其他进程写入下单 | 中 | auth token（现状）+ 目录 ACL 建议写入 DEPLOY + trading_enabled 默认关 + 未来加 account_id 逐请求核对（参考项目行为，列为 M4 加固可选项） |
| R7 | file 轮询延迟影响条件单/打板引擎 | 低 | 文档明确：低延迟策略用 redis+relay；watchdog 已消费 max_latency_ms 上界 |
| R8 | 券商终端关键字审计拦截自由文本 | 低 | D5 的 `ASCII_ONLY` 开关 + D8 应急预案，不预先复杂化协议 |

---

## 10. 里程碑顺序与依赖

```
M0（配置闭环）──┐
M1（端口固化）──┼─→ M2（agent 补全+加固，穿插首轮实机）─→ M3（低延迟端到端）─→ M4（路径A+实机 runbook+收口）
                └─ M0/M1 可并行；M2.7 依赖 M1 完成（health 面数据来自端口层）
```

预估总当量：14~23 个工作日（不含实机券商档期）。首个用户可见成果在 **M0 完成**：即可在前端建立并连上「大 QMT 文件桥」连接。
