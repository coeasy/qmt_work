# qmt_work · 多券商量化平台

基于 **QMT / XTQuant 等多券商客户端**的量化交易平台：在同一进程内统一提供 **可视化 Web 界面 + MCP（Streamable HTTP）+ FastAPI REST 接口 + WebSocket 实时数据同步**，可打包为**独立桌面客户端（EXE）**。支持**多券商、多客户端版本**（国金 / 华鑫 / 银河 / 中信建投 / 兴业 / 广发 / 同花顺 / 恒生 PTrade / 掘金），所有行情 / 交易 / 账户接口均为**真实券商 SDK 调用，无任何 mock 实现**；未连接券商时端点返回明确的 503 引导，绝不返回假数据。LLM 供应商可插拔（不锁定任何 SDK，用户在界面配置），并支持「测量效果」对比分析。

功能覆盖参考项目：[EzQmt](https://github.com/LHanLi/EzQmt)（账户/绩效/再平衡/滑点）、[QMT-MCP](https://github.com/guangxiangdebizi/QMT-MCP)（策略生成/写入客户端）、[Rockyzsu/QMT](https://github.com/Rockyzsu/QMT)（HTTP 封装/算法单）、[liqimore/quant-qmt-proxy](https://github.com/liqimore/quant-qmt-proxy)（参考数据/L2/订阅中心）、[123quant/QMT-QuantLimit](https://github.com/123quant/QMT-QuantLimit)（涨停监控/打板）—— 均以**真实接口**落地。

## 核心能力（对应诉求）

| 诉求 | 实现位置 |
|------|----------|
| 可视化界面 | `frontend/`（React + Vite + ECharts SPA，纯前端，**不直接内置任何券商逻辑**），FastAPI 同源托管于 `/` |
| MCP 接口 | `backend/mcp_server/`，挂载于 `/mcp/`（FastMCP Streamable HTTP，Cursor / Claude Desktop 可直连） |
| FastAPI 接口 | `backend/app/routes.py`，REST 网关 `/api/v1/*` |
| 同步数据 | `backend/sync/`，WebSocket `/api/v1/ws` + 订阅聚合（活跃券商只订阅一次，多客户端扇出） |
| 多券商 / 多版本 | `backend/xtquant_client/`（BrokerAdapter 抽象 + BrokerManager 多连接管理器 + 9 个券商档案） |
| 券商连接管理 UI | `frontend/src/components/Brokers.jsx` + 顶部 `BrokerBar`（增 / 连 / 断 / 设活跃 / 删 / 探测） |
| 前后端解耦 | 前端仅透传 `conn_id` / `broker_id`；后端 `BrokerManager` 在运行时解析具体连接，前端不感知券商实现 |
| 全面覆盖参考项目 | 账户/持仓/PNL、行情（含五档快照）、下单/撤单/溢价撤、回测、再平衡、滑点、归因、API Key、LLM Agent 等全量端点 |
| 涨停监控/打板 | `backend/tools/limitup.py` + 前端「涨停监控」页：股票池 + 三因子触发（涨停价/时间窗/tick涨幅）+ 可选自动买入（过风控），借鉴 QMT-QuantLimit |
| 算法交易 TWAP/VWAP | `backend/tools/algo.py` + 前端「算法交易」页：时间等分拆单、暂停/恢复/取消、子单记录，借鉴 Rockyzsu/QMT |
| 策略模板库 | `backend/tools/strategy_gen.py` + 前端「策略库」页：ma_cross/macd/rsi/limitup 模板生成、写入 QMT 客户端 mpython 目录，借鉴 QMT-MCP |
| 参考数据/L2 | `backend/tools/reference.py` + 前端「参考数据」页：交易日历/板块成分/财务摘要/L2 逐笔，借鉴 quant-qmt-proxy |
| 测量效果对比 | 回测 `compare` / `sensitivity` 作业类型 + 前端对比柱状图/表 |
| 可插拔 LLM | `backend/agent/` Provider 抽象（OpenAI 兼容 / Anthropic 适配器），密钥 AES-256-GCM 加密，UI 配置 |
| 多运行时 IPC 桥接（P0） | `xtquant_client/bridge_server.py` + `bridge_client.py` + `discovery.py`：按券商 xtquant `.cpXXX.pyd` ABI 动态拉起桥接子进程，根治 3.8–3.12 全券商兼容，主后端可独立升级到 3.13 |
| 多账户网格 + 批量（P0） | `frontend/src/components/AccountsGrid.jsx` + `GET /account/grid`、`POST /account/batch/order｜cancel｜reconnect`：多券商/多账户统一看板 + 批量下单/撤单/重连 |
| 向量化回测 + 参数扫描（P1） | `tools/backtest.py` `run_backtest_vectorized` / `run_param_sweep`：pandas 向量化信号 + grid search 选优（与 legacy 引擎**逐根信号一致**） |
| 因子/指标库（P1） | `tools/factors.py` + 前端「因子」页：15 类指标（SMA/EMA/RSI/MACD/BOLL/ATR/ADX/CCI/KDJ/OBV/量MA/收益率/ZScore/ROC），支持单标的/批量/基于真实 K 线 |
| 模拟盘（P1） | `paper/paper_engine.py` + 前端「模拟盘」页：虚拟撮合 + 实时**真实行情** mark-to-market（不提供假数据），验证策略后再实盘 |
| 策略市场（P1） | `tools/strategy_market.py` + 前端「策略市场」页：DB 目录 + zip/json 导入导出，吸收 Rockyzsu 范式 |
| 可观测性（P2） | `gateway/metrics.py` + `app/routes/observability.py`：Prometheus 指标 + 内存链路追踪环缓冲 + 运行时画像 |
| 注册表 V2（P2） | `xtquant_client/registry.py` `Registry`：能力协商（negotiate）+ 热插拔券商档案（hotplug），新增券商纯配置化 |
| 定时任务 + 分布式调度（P2） | `scheduler/scheduler.py` + `scheduler/distributed.py` + 前端「定时任务」页：cron/周期任务、关机/重启、可选 Redis 锁 Leader 选举 |
| 可打包独立客户端 | `backend/build_exe.py`（PyInstaller）+ `frontend`（Electron 桌面壳，端口自动发现 + 托盘 + 开机自启） |

## 架构（真实券商 · 多券商 · 前后端解耦）

```
单进程 FastAPI 后端（backend/run.py）—— 启动即「真实券商模式」，无 mock
├─ REST 网关        /api/v1/*      （账户、行情、下单、回测、再平衡、券商连接、配置、API Key、Agent、因子、模拟盘、策略市场、可观测性、定时任务、注册表）
├─ MCP 服务         /mcp/          （FastMCP Streamable HTTP，docket 会话管理）
├─ WebSocket       /api/v1/ws     （实时行情快照 + tick 推送，订阅聚合；跟随「活跃连接」）
├─ 静态前端        /              （React SPA，构建产物在 backend/static）
├─ 券商连接管理     xtquant_client/
│   ├─ base.py        BrokerAdapter 抽象基类（统一接口，所有方法真实调用）
│   ├─ manager.py     BrokerManager（多券商/多账户/多客户端版本连接池，持久化到 SQLite，启动自动重连）
│   ├─ registry.py    券商档案 + Registry V2（能力协商 negotiate + 热插拔 hotplug）
│   ├─ bridge_server / bridge_client   P0 多运行时 IPC 桥接（按 xtquant .pyd ABI 拉起子进程，根治 3.8–3.12 兼容）
│   ├─ xtp.py         XTPQuantAdapter（迅投系 6 家券商，共用 xtquant SDK）
│   └─ adapters/      同花顺 / 恒生 PTrade / 掘金 适配器（接口契约，SDK 就绪即启用）
├─ 回测 / 因子       tools/backtest.py（向量化 + 参数扫描，与 legacy 逐根一致）、tools/factors.py（15 类指标）、backtest/（作业队列，真实 K 线）
├─ 模拟盘 / 策略市场 paper/paper_engine.py（实时真实行情 mark-to-market）、tools/strategy_market.py（DB 目录 + zip/json 导入导出）
├─ 可观测性 / 调度   gateway/metrics.py（Prometheus 指标 + 链路追踪）、scheduler/scheduler.py + scheduler/distributed.py（定时任务 + 可选 Redis 锁 Leader 选举）
└─ 同步引擎         sync/          （行情缓存 market_cache + 账户快照 account_snapshot，遍历所有已连接账户）
```

桌面壳（frontend/electron/main.cjs）—— 全自动运行
└─ 启动 backend/dist 中的 qmt_work.exe（QMT_PORT_FILE 自动发现实际端口，端口被占自动 +1）
   → 等待就绪 → 加载 http://127.0.0.1:<实际端口>/
   → 托盘常驻（关闭最小化）、设置页可开启「开机自启」

> ⚠️ **运行环境要求**：迅投系券商依赖 `xtquant`（Windows 专用），后端必须与 **QMT 客户端运行在同一台 Windows 机器**上；同花顺 / PTrade / 掘金 需各自客户端与 SDK。本平台**已移除全部 mock**：未连接任何券商客户端时，行情 / 交易 / 账户 / 回测端点返回 HTTP 200 + 业务码 `503` 并提示「请到券商连接页添加并连接」，前端据此展示引导态，**不返回任何假数据**。

## 已支持的券商与客户端

| 券商 | 适配器 | 所需 SDK | 账户类型 | 状态 |
|------|--------|----------|----------|------|
| 国金证券 QMT | xtp | xtquant | STOCK/CREDIT/OPTION/FUTURES | ✅ 真实 |
| 华鑫证券 奇点QMT | xtp | xtquant | STOCK/CREDIT/OPTION/FUTURES | ✅ 真实 |
| 银河证券 QMT | xtp | xtquant | STOCK/CREDIT | ✅ 真实 |
| 中信建投 QMT | xtp | xtquant | STOCK/CREDIT | ✅ 真实 |
| 兴业证券 QMT | xtp | xtquant | STOCK/CREDIT | ✅ 真实 |
| 广发证券 QMT | xtp | xtquant | STOCK/CREDIT | ✅ 真实 |
| 同花顺量化 | ths | ths_quant_sdk | STOCK | 🔌 接口契约（待接 SDK） |
| 恒生 PTrade | ptrade | ptrade_sdk | STOCK/CREDIT | 🔌 接口契约（待接 SDK） |
| 掘金量化 | juejin | gm | STOCK/FUTURES | 🔌 接口契约（待接 SDK） |

> 新增券商只需在 `registry.py` 追加一条档案 + 实现对应 `BrokerAdapter`；前端「券商连接」页自动列出，无需改动组件。

## 全面升级（P0 / P1 / P2，均已落地）

### P0 · 根基兼容与多账户

- **多运行时 IPC 桥接（根治 ABI 兼容）**：`xtquant_client/bridge_server.py` 经 zmq 暴露 `xtdata`/`xttrader` 调用；`bridge_client.py` 在运行时按券商 `xtquant/*.cpXXX-win_amd64.pyd` 自动择机（cp38~cp312），用对应嵌入式运行时拉起桥接子进程，`import xtquant` 必然成功。主后端可独立升级（已用 3.13 验证），与桥接 ABI 解耦；选不到匹配 ABI 时明确报错「该券商需 cpXX」。
- **多账户网格 + 批量操作**：前端「多账户网格」页（`AccountsGrid.jsx`）统一展示多券商/多账户持仓、可用资金、当日盈亏；后端 `GET /account/grid` 聚合并行快照，`POST /account/batch/order｜cancel｜reconnect` 支持批量下单 / 撤单 / 重连，单请求多账户并行执行。

### P1 · 研究 / 策略能力深化

- **向量化回测 + 参数扫描**：`tools/backtest.py` 新增 `run_backtest_vectorized`（pandas/numpy 向量化信号，与 legacy `_signals` **逐根信号与指标完全相等**，已有一致性测试锁定）与 `run_param_sweep`（itertools 网格穷举 + 夏普排序选优）；`backtest/` 作业队列新增 `sweep` 类型，结果持久化到 `backtests` 表。
- **因子 / 指标库**：`tools/factors.py` 实现 15 类指标（SMA/EMA/RSI/MACD/BOLL/ATR/ADX/CCI/KDJ/OBV/量MA/收益率/对数收益率/ZScore/ROC），`POST /factors/compute｜compute/many｜from-kline`（真实 K 线经 C1 缓存，无券商返回 503）。
- **模拟盘（Paper Trading）**：`paper/paper_engine.py` 虚拟撮合，持仓市值经 `sync_from_map` 用**实时真实行情** mark-to-market（绝不编造价格）；`POST /paper/order`、`GET /paper/account｜positions｜trades｜metrics` 验证策略后再实盘。
- **策略市场**：`tools/strategy_market.py` + `app/routes/strategy_market.py`，DB 目录（`strategy_market` 表）+ `POST /strategy-market/publish｜install｜export｜import｜export-json｜import-json`（zip/json 导入导出），吸收 Rockyzsu/QMT 范式。

### P2 · 平台化 / 运维

- **可观测性**：`gateway/metrics.py` 扩展 counter/gauge/histogram（`qmt_backtests_total`、`qmt_paper_orders_total`、`qmt_ws_clients`、`qmt_api_latency_ms`、`qmt_runtime_mode`、`qmt_errors_total` 等）；`app/routes/observability.py` 暴露 `GET /observability/metrics-summary｜traces｜runtime`（Prometheus 指标 + 内存链路追踪环缓冲 + 运行时画像）。
- **注册表 V2**：`xtquant_client/registry.py` 的 `Registry` 提供 `negotiate`（能力协商：从账户类型推导 option/credit/futures 能力，反馈不支持项）与 `hotplug_profile`（热插拔券商档案，纯配置化新增券商）；`POST /brokers/registry/negotiate｜profiles｜reload`。
- **定时任务 + 分布式调度**：`scheduler/scheduler.py`（`TaskScheduler`，cron/周期任务、关机/重启标志 + 生命周期看门狗 SIGTERM/SIGHUP）与 `scheduler/distributed.py`（`DistributedScheduler`，可插拔锁后端：MemoryLock 默认，RedisLock 可选并优雅降级）；前端「定时任务」页 `GET/POST /scheduler/tasks`、`/shutdown`、`/restart`、`/status`。

> 测试覆盖：核心单测 `tests/test_unit.py` + 新增 `tests/test_backtest_vectorized.py` 等，pytest **121 passed**；端到端 `tests/smoke2.py` **22 passed**（含 503 引导、API Key、Agent 未配置、WS、MCP 握手）。

## 快速开始（开发模式）

### 1. 后端
```bash
cd backend
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
python run.py                      # http://127.0.0.1:21117  （真实券商模式）
```
冒烟测试（需后端已启动）：
```bash
python tests/smoke2.py            # 全链路：券商档案/连接/503引导/回测/LLM/Agent/WS/MCP握手 → 22 PASS
```

### 2. 前端
```bash
cd frontend
npm install                       # 首次
npm run dev                       # Vite 开发服务器（代理 /api /ws /mcp 到 21117）
# 或构建到后端静态目录（与 EXE 同源托管）：
npm run build                     # 产出 frontend/dist → backend/static
```

### 3. 添加并连接券商
打开 Web 界面「券商连接」页 → 选择券商（如国金证券 QMT）→ 填写客户端 `userdata_mini` 路径与资金账号 → 添加并连接。连接成功后仪表盘 / 行情 / 回测 / 再平衡即对接真实客户端。

## 打包

### 后端 EXE（PyInstaller onedir）
```bash
cd backend
python build_exe.py               # 产出 backend/dist/qmt_work/qmt_work.exe
# 直接运行该 exe 即等同 python run.py（同源托管 API + SPA + MCP，真实券商模式）
```
> 前端需先 `npm run build` 生成 `backend/static`，PyInstaller 会一并打包，使 EXE 在打包后仍能托管前端。

### 桌面客户端（Electron）
```bash
cd frontend
npm install
npm run pack                     # electron-builder --dir → dist-electron/win-unpacked/qmt_work.exe
# 完整安装包（NSIS）：
npm run dist                     # 需可访问 electron-builder 二进制 CDN（国内可用 ELECTRON_MIRROR 镜像）
```
打包后目录结构（已验证）：
```
dist-electron/win-unpacked/
├─ qmt_work.exe                  # Electron 主程序
└─ resources/
   ├─ app.asar                    # 主进程代码（main.cjs / preload.cjs）
   └─ backend/qmt_work/qmt_work.exe      # 内置后端
```

## MCP 客户端配置

任意支持 MCP 的客户端（Cursor / Claude Desktop / VS Code）指向：
```
http://127.0.0.1:21117/mcp
```
（Streamable HTTP；后端已挂载 `mcp.http_app(path="/", transport="streamable-http")` 于 `/mcp`）

## LLM 供应商配置（可插拔）

设置页 → 模型供应商：填写 `provider`（openai / anthropic 等）、`base_url`、`api_key`、`model`。
- 密钥以 **AES-256-GCM** 加密落库，读取时返回脱敏掩码（`sk-********1234`）。
- 未配置时 Agent 返回「尚未配置 LLM Provider」引导提示（SSE 流），不影响其余功能与 MCP/REST 接口。

## 已知限制 / 环境说明

1. **GUI 启动需桌面会话**：Electron 需要 Windows 窗口站（window station）。在无图形界面的无头/服务会话中，`.exe` 会立即静默退出（`--version` 仍可正常输出版本）。在有登录桌面的正常 Windows 环境下可正常启动。
2. **券商依赖**：真实交易/行情需对应券商客户端 + SDK 同机运行（迅投系需 `xtquant` + MiniQMT）。未连接时平台以 503 引导，不提供假数据。
3. **同花顺 / PTrade / 掘金**：适配器已实现统一接口契约，待对应 SDK 接入后即可启用真实调用。
4. **NSIS 安装包**：`npm run dist` 默认从 GitHub CDN 拉取 NSIS 工具链；网络受限时可用 `ELECTRON_MIRROR=https://cdn.npmmirror.com/binaries/electron/`（Electron 二进制）与对应镜像，或直接使用 `--dir` 解压版（已验证可运行）。

## 项目结构

```
qmt_mcp/
├─ docs/QMT量化Agent平台方案.md   # 完整方案设计
├─ backend/                       # FastAPI 统一后端（真实券商模式）
│  ├─ run.py                      # 启动入口
│  ├─ app/                        # config / db / routes / main / state（lifespan 组合 mcp + broker_manager + paper/scheduler）
│  │   └─ routes/                 # REST：accounts/factors/paper/strategy_market/observability/scheduler/registry ...
│  ├─ xtquant_client/             # BrokerAdapter + BrokerManager + registry(V2) + 桥接 + 各券商适配器（无 mock）
│  │   ├─ bridge_server.py / bridge_client.py   # P0 多运行时 IPC 桥接（按 ABI 择机）
│  │   ├─ discovery.py            # 进程/目录自动探测券商
│  │   └─ registry.py             # 券商档案 + Registry V2（negotiate / hotplug）
│  ├─ mcp_server/                 # MCP 工具注册（行情/交易/账户/回测/再平衡/分析/券商）
│  ├─ agent/                      # LLM Provider 抽象 + Agent 核心（ReAct）
│  ├─ sync/                       # WebSocket 同步引擎 + 多连接订阅聚合
│  ├─ backtest/                   # 回测作业队列（事件循环内执行，真实 K 线；含 sweep）
│  ├─ paper/                      # 模拟盘引擎（实时真实行情 mark-to-market）
│  ├─ scheduler/                  # 定时任务（scheduler.py）+ 分布式调度（distributed.py）
│  ├─ tools/                      # REST/MCP/Agent 复用：backtest(向量化) / factors / strategy_market / ...
│  ├─ gateway/                    # 网关 / 限流 / 风控 / metrics / runtime_config / masking / kline_cache / webhook_out
│  ├─ data/                       # SQLite（broker_connections / market_cache / llm_config / api_keys / backtests ...）
│  ├─ static/                     # 构建后的前端 SPA
│  ├─ dist/                       # PyInstaller 产物（qmt_work.exe）
│  └─ build_exe.py                # EXE 打包脚本
└─ frontend/                      # React + Vite + Electron（纯 SPA，前后端解耦）
   ├─ src/
   │   ├─ BrokerContext.jsx       # 券商连接上下文（单一真相来源：后端 BrokerManager）
   │   ├─ api.js                  # 统一 REST 客户端 + 券商/账户网格辅助方法
   │   ├─ components/AccountsGrid.jsx  # 多账户网格 + 批量操作（P0）
   │   ├─ components/Brokers.jsx  # 券商连接管理页
   │   ├─ components/BrokerBar.jsx# 顶部连接选择条
   │   └─ components/             # Dashboard/Quote/Backtest/Factors/Paper/StrategyMarket/Observability/Scheduler/Registry/Settings（均透传 conn_id）
   ├─ electron/                   # 桌面壳 main.cjs / preload.cjs
   ├─ electron-builder.yml        # 产物名 qmt_work（appId com.qmt.work）
   └─ dist-electron/              # 打包产物
```
