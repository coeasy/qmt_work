# qmt_work · 基于 QMT 的量化 Agent 平台 · 项目方案（v7 设计 / v8 实现现状）

> **实现状态（2026-08-15）**：P0（多运行时 IPC 桥接 + 多账户网格/批量）、P1（向量化回测+参数扫描 / 因子库 / 模拟盘 / 策略市场）、P2（可观测性 / Registry V2 / 定时任务+分布式调度）**已全部落地**，详见 `README.md` 的「全面升级」章节与 `ROADMAP_VERSION_COMPAT.md`。
> 目标：开发一套「**基于 QMT 的 MCP + FastAPI 接口 + 可视化 Web 应用**」三位一体的量化平台。
> 对内提供 Web 界面给用户做**快速回测 / 下单 / 数据分析**，对外通过 MCP(SSE) 与 REST API 支持**第三方系统/AI 助手调用**，并内建**数据同步引擎**保证多端数据一致。
> **Agent 引擎不强制依赖任何特定 SDK**：采用「LLM 提供商可插拔抽象层」，默认支持 OpenAI 兼容协议、CodeBuddy、DeepSeek、Anthropic、Ollama 等任意后端；用户可在 Web 界面自助配置 API Key / Base URL / 模型，无需改动代码。
> 后端统一用 **FastAPI（Python 单进程）** 承载 REST 网关 + MCP 服务 + Agent + 数据同步；前端用 React（可打包为 Electron/Tauri 桌面 exe）。
>
> 本文为方案文档，**不含代码实现**。

---

## 1. 项目背景与目标

### 1.1 痛点
- 迅投/国金 QMT 的交易与行情能力（XTQuant）**只能运行在 Windows 本机**，且与 QMT 客户端同进程，缺乏统一、可远程调用的服务化封装。
- 现有开源项目（EzQmt、QMT-MCP）已具备账户分析、回测、下单、MCP 接口等能力，但**没有面向终端用户的 Web 操作界面**，也没有统一的第三方接入网关与数据同步能力。
- 用户期望用自然语言/Agent 的方式驱动交易操作，同时保留「填表即可回测/下单」的传统界面，并要求**数据在多个界面/设备间实时一致**。

### 1.2 目标
1. **QMT 适配层（MCP 服务）**：用 FastMCP 把 XTQuant 的行情、交易、账户能力标准化封装为 MCP 工具（参考 QMT-MCP）。
2. **FastAPI 接口网关**：对外暴露 REST API 与 MCP（SSE），支持 API Key 鉴权、限流、风控、审计，供第三方调用；**REST 与 MCP 复用同一套 Python 工具实现**。
3. **可视化 Web 应用**：React 前端提供
   - **Agent 对话式操作**（自然语言回测/下单/分析）：后端自研轻量 Agent Core + LLM 提供商可插拔抽象层，LLM 后端由用户在界面配置，不写死。
   - **传统可视化页面**：回测表单、下单面板、数据分析看板、策略对比/测量效果看板（ECharts / lightweight-charts / Plotly / AntV），支持系统托盘迷你面板与大屏投屏。
4. **数据同步引擎（新增核心）**：订阅 QMT 行情/账户/成交，实时写入本地缓存并经 WebSocket 广播到所有界面，保证多端一致、断线可恢复、离线可查。
5. **全功能覆盖**：交易执行、行情爬虫、分仓再平衡、账户绩效、滑点/成本分析、策略生成、回测、多层风控、MCP 接入——两个参考项目的全部功能均须在本平台落地（见 §2.4 覆盖核对矩阵）。

### 1.3 用户角色
| 角色 | 诉求 |
|------|------|
| 终端用户（交易员） | Web/桌面端快速回测、下单、看绩效，多界面数据一致 |
| 开发者 / 第三方系统 | 通过 MCP 或 REST API 调用交易与行情能力 |
| 平台管理员 | 账户、风控参数、API Key、限流、LLM Provider 管理 |

---

## 2. 参考项目分析

### 2.1 EzQmt（LHanLi/EzQmt）—— 全功能清单
- **定位**：基于 XTQuant 的自动化多因子策略交易执行、账户状态监控、多策略自动分仓、行情爬虫、账户绩效与交易成本分析。
- **全量功能（需在本平台 100% 覆盖）**：
  - **自动化多因子策略交易执行**：`BuySell.py` 买卖执行、`NormFunc.py` 归一化函数。
  - **账户状态监控**：实时持仓、资金、委托状态监控。
  - **多策略自动分仓 / 再平衡**：`Reblance.py` 自动拆单、挂撤单，将持仓市值占比调整至目标值；支持 lude 格式篮子文件、阈值调仓。
  - **行情爬虫脚本**：`launch.py` / `setup.py` 行情数据采集。
  - **账户绩效分析**（`Summary.py` → `account()`）：
    - 基于持仓/交割单备注，分析策略持仓、各策略盈亏、分标的盈亏。
    - 总账户净值、月度收益、收益的标的贡献（`contri['all']`）。
    - 分策略仓位与表现（`displaystrats_pos` / `displaystrats_pnl`）。
    - 支持外部转入转出资金（`outcash_list`）、业绩比较基准（`benchmark`）、转债转股条款（`conv_stk`）、策略合并（`renamestrat`）、隐藏金额（`if_hide`）。
  - **交易滑点分析（单边）**：`cal_deal_comm`，基于分钟线数据计算成交滑点（开盘/收盘/中间价对比）。
  - **交易成本分析**：佣金、印花税等成本拆解。
  - 依赖 `FreeBack`（回测框架）与 `EzQmt` 库。
- **可借鉴**：绩效/滑点/分仓/成本分析逻辑，封装为「分析类 MCP 工具」。

### 2.2 QMT-MCP（guangxiangdebizi/QMT-MCP）—— 全功能清单
- **定位**：基于 **FastMCP + XTQuant** 的模块化量化交易 MCP 服务。
- **全量功能（需在本平台 100% 覆盖）**：
  - **MCP 协议集成**：SSE（推荐，远程友好）/ stdio 双传输，可被 Claude Desktop、Cursor、Cline 等直接接入。
  - **智能策略生成**：`generate_ma_strategy`（双均线策略并回测）、`save_qmt_strategy`（自定义策略，含 `init`/`handle_bar`）。
  - **实时交易执行**：`place_order`（买卖）、`cancel_order`（撤单），支持 XTQuant/QMT 实盘与模拟。
  - **模块化架构**：`tools/`（trading_tool、qmt_tool）、`strategies/`（ma_strategy、strategy_generator）、`utils/`（xtquant_client、data_handler）。
  - **回测分析**：
    - 收益指标：总收益率、年化收益率、夏普比率。
    - 风险指标：最大回撤、年化波动率、VaR。
    - 交易统计：交易次数、胜率、平均盈亏。
    - 绩效评估：策略评级与优化建议。
  - **多层风险控制**：
    - 交易层面：单笔最大金额、单标的最大持仓、最小下单量、市价单价差保护。
    - 策略层面：最大回撤限制、最小夏普要求、杠杆率控制、止损比例。
    - 系统层面：交易状态控制、连接状态监控、异常处理、日志追踪。
  - 配置：`.env`（QMT_PATH、QMT_SESSION_ID、QMT_ACCOUNT_ID、风控参数等）。
- **可借鉴**：MCP 工具定义、SSE 传输、风控参数体系、模块目录结构。

### 2.3 综合结论
- **功能覆盖原则**：本平台必须覆盖上述两个参考项目的**全部功能**（交易执行、行情爬虫、分仓再平衡、账户绩效、滑点/成本分析、策略生成、回测、多层风控、MCP 接入），不允许裁剪（见 §2.4 核对矩阵）。
- **统一 Python 后端（关键架构决策）**：MCP 服务（FastMCP）、分析层（EzQmt）、Agent 引擎（Python LLM SDK）**全部是 Python**。因此后端采用 **FastAPI 单进程**，同时承载：
  - REST API 网关（鉴权/限流/风控/审计）
  - MCP SSE 服务（FastMCP 挂载进同一 ASGI 应用，工具即 Python 函数）
  - Agent 路由（Agent Core + LLM Provider 抽象）
  - 数据同步引擎（QMT 订阅 → 缓存 → WebSocket 广播）
  - 静态托管构建后的 React 前端（桌面壳场景下同源）
  → **一套工具实现，REST/MCP/Agent 三方复用**，无需 TS 跨进程再调 Python MCP，消除双语言割裂。
- **LLM 可插拔**：Agent Core 只依赖统一的 `LLMProvider.chat()` 接口，背后经适配器接任意 OpenAI 兼容/各家 API；默认不预置密钥、用户在界面自助配置。

### 2.4 参考项目功能覆盖核对矩阵（证明「全面实现全部功能」）
| 参考项目功能 | 本平台映射模块 | MCP 工具 / 页面形态 |
|------|------|------|
| EzQmt·多因子策略执行 | §4.1 策略类 | `save_qmt_strategy`(多因子模板) + `run_backtest` + `place_order`；策略页/回测页/下单页 |
| EzQmt·账户状态监控 | §4.1+§4.13 | `monitor_account` + 账户看板 + WebSocket 实时推送 |
| EzQmt·多策略分仓/再平衡 | §4.1 | `rebalance_position`（拆单/挂撤/篮子/阈值）；分仓页 + 调仓任务 |
| EzQmt·行情爬虫 | §4.1 | `crawl_market_data`；行情页 |
| EzQmt·账户绩效(净值/月度/标的贡献/分策略/外部资金/基准/转债/合并/隐藏) | §4.6 | `account_pnl` / `account_status` / `strategy_contribution`；绩效看板（下钻） |
| EzQmt·滑点分析(分钟线) | §4.6 | `slippage_analysis`；分析页 |
| EzQmt·交易成本分析 | §4.6 | `cost_analysis`；分析页 |
| QMT-MCP·MCP 双传输(SSE/stdio) | §4.1 | FastAPI 内 FastMCP SSE；保留 stdio |
| QMT-MCP·策略生成(双均线+自定义) | §4.1 | `generate_ma_strategy` + `save_qmt_strategy` + 模板(MA/BOLL/RSI/GRID) |
| QMT-MCP·实时交易 | §4.5 | `place_order` / `cancel_order`（限价/市价/条件/批量） |
| QMT-MCP·模块化架构 | §4.1 | `tools/` `strategies/` `utils/` 目录结构沿用 |
| QMT-MCP·回测全指标(收益/年化/夏普/回撤/波动/VaR/胜率/评级) | §4.4 | `run_backtest` 全量返回 |
| QMT-MCP·三层风控 | §4.5+§8 | 工具入口校验 + 网关风控（交易/策略/系统三层固化） |
| QMT-MCP·.env 配置 | §4.11 | SQLite `config` 表 + `.env`（端口/风控） |
| （本平台新增）多方案测量效果对比 | §4.7 | `compare_backtests` / `sensitivity_analysis`；对比看板 |
| （本平台新增）数据同步 | §4.13 | WebSocket `/ws/account` `/ws/quote` `/ws/notify` |
| （本平台新增）LLM 可插拔 | §4.8 | Provider 抽象 + 设置页 |

---

## 3. 整体架构设计

```
┌──────────────────────────────────────────────────────────────────────┐
│  用户终端 / 第三方系统                                                 │
│   - 浏览器/桌面壳 (React 可视化 Web 应用)   - 第三方 AI 助手/程序      │
│   - 系统托盘迷你面板 / 大屏投屏                  (MCP SSE / REST API)   │
└───────────────┬───────────────────────────────┬──────────────────────┘
                │ 聊天(WS/SSE) / 页面 / WS 推送    │ MCP(SSE) / REST API
                └────────────────┬──────────────────┘
                                 ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  统一后端：FastAPI（Python 单进程 ASGI 应用，部署于 QMT Windows 主机）│
   │  ┌─ REST API 网关 (/api/v1)：API Key 鉴权 / 限流 / 风控 / 审计      │
   │  ┌─ MCP SSE 服务 (FastMCP 挂载)：工具 = 同一套 Python 函数          │
   │  ┌─ Agent 路由：Agent Core + LLM Provider 抽象层（可插拔）          │
   │  │     ├ OpenAI 兼容 / CodeBuddy / DeepSeek / 通义 / 智谱 / Kimi     │
   │  │     ├ Anthropic(Claude)      └ Ollama / 自定义端点               │
   │  ┌─ 数据同步引擎：QMT 订阅 → 内存缓存 + SQLite → WebSocket 广播     │
   │  ┌─ 静态资源：构建后的 React 前端（桌面壳场景同源托管）              │
   │  └─ SQLite：会话/消息/回测/对比/审计/缓存/配置（AES 加密密钥）      │
   └───────────────────────────────┬──────────────────────────────────┘
                                    │ in-process（仅 Windows）
                                    ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  QMT 引擎层（Windows 本机）                                          │
   │   XTQuant  ↔  QMT 客户端(已登录)   行情 / 交易 / 账户                 │
   └──────────────────────────────────────────────────────────────────┘
   桌面壳 Electron/Tauri：拉起 FastAPI 进程(或 qmt_work.exe) + WebView → localhost
```

> **关键约束**：XTQuant 仅支持 Windows，必须与 QMT 客户端同机。因此 **FastAPI 后端（内含 MCP 服务）必须部署在运行 QMT 的 Windows 主机上**；浏览器/桌面壳/第三方可经 SSE/REST 远程访问。统一后端消除了「Node 网关 + Python MCP 双进程」的割裂。

### 3.1 分层职责
| 层 | 技术 | 职责 |
|----|------|------|
| QMT 引擎层 | XTQuant + QMT 客户端 | 实际行情/交易/账户执行（Windows 本机） |
| 统一后端层 | **FastAPI（Python）** | 单进程承载：MCP SSE 服务 + REST 网关 + Agent 路由 + 数据同步引擎 + 静态托管 |
| MCP 工具实现 | FastMCP + XTQuant（同进程） | 行情/交易/账户/分仓/策略/回测/对比工具，即 Python 函数，被 MCP 与 Agent 共用 |
| 可视化前端 | React 18 + Vite + TDesign + 图表矩阵 | 回测/对比/账户/行情看板、托盘迷你面板、Agent 对话、设置向导 |
| LLM Provider 抽象层 | 统一 `chat()` 接口 + OpenAI 兼容/Anthropic/自定义适配器 | 任意 LLM 可插拔，密钥/模型界面配置，可热切换、可回退 |
| 数据同步引擎 | QMT 订阅 + 内存/SQLite 缓存 + WebSocket | 实时同步账户/行情/成交到所有界面，多端一致、断线可恢复 |
| 桌面壳 / 分发层 | Electron 或 Tauri（WebView）+ NSIS/绿色版 | 封包为独立客户端 exe，拉起 FastAPI 进程，系统托盘常驻 |

---

## 4. 核心模块设计

### 4.1 QMT 适配与 MCP 服务层（FastMCP，同进程）
- **位置**：与 QMT 同 Windows 主机，运行在 FastAPI 进程内（或同机独立进程经 SSE）。
- **MCP 工具规划（覆盖参考项目全部功能，见 §2.4）**：
  - **行情类**：`get_quote`（快照）、`get_kline`（历史 K 线）、`get_tick`；行情爬虫 `crawl_market_data`。
  - **交易类**：`place_order`、`cancel_order`、`query_position`、`query_orders`、`query_cash`。
  - **账户监控类**：`monitor_account`（持仓/资金/委托实时快照）。
  - **分仓再平衡类**：`rebalance_position`（拆单、挂撤单、目标市值占比调仓，支持 lude 篮子文件与阈值调仓）。
  - **策略类**：`save_qmt_strategy`、`generate_ma_strategy`（及模板：布林、RSI、网格、多因子）。
  - **回测类**：`run_backtest`（标的/区间/策略/初始资金 → 收益、回撤、夏普、VaR、胜率、成交明细、评级与优化建议）。
  - **分析类（EzQmt）**：`account_pnl`、`strategy_contribution`、`slippage_analysis`（分钟线）、`cost_analysis`、`account_status`（策略盈亏/分标的/基准/外部资金/转债/合并/隐藏）。
  - **对比类（测量效果）**：`compare_backtests`、`sensitivity_analysis`。
- **工具复用**：同一组 Python 函数既注册为 FastMCP 工具，也被 Agent Core 直接调用（同一进程），REST 端点内部也调用它们——**三端一份实现**。
- **传输**：默认 **SSE**（`/mcp/sse`），远程友好；保留 stdio 供本地 AI 客户端。
- **风控**：QMT-MCP 三层风控参数体系固化，在工具入口统一校验。

### 4.2 FastAPI 接口网关与第三方接入
- 单进程内同时提供两类外部接口：
  1. **MCP（SSE）**：第三方 AI 助手直接把 `http://host:21117/mcp/sse` 配置为 MCP Server。
  2. **REST API**：供非 AI 程序调用（下单、查持仓、触发回测、拉分析）。
- **鉴权**：API Key（Bearer Token）+ 按 Key 绑定账户/权限/限流配额。
- **统一风控闸门**：所有外部/内部调用先过风控（金额、持仓、频率）再下发 QMT。
- **审计日志**：调用方、参数、结果全量留痕。
- **WebSocket 同步**：`/ws/account`、`/ws/quote`、`/ws/notify` 实时推送（见 §4.13）。

### 4.3 可视化 Web 应用（React 前端 + FastAPI 后端，不锁死 SDK）
> **核心思路**：后端只保留一个**自研轻量 Agent Core**（工具编排 + 多轮对话），LLM 调用全部经由「LLM Provider 抽象层」。CodeBuddy 只是抽象层的一个可选适配器，默认不预置任何密钥。

- **FastAPI 后端（Agent 路由）**：
  - **Agent Core（Python，与 SDK 解耦）**：连接本进程 MCP 工具（直接 import 函数）→ 注入 LLM → 解析 `tool_calls` → 执行 → 回填推理，标准 ReAct 循环。
  - 流式：LLM 文本与工具调用经 **SSE/WebSocket** 推送到前端（工具调用可视化、逐步可见）。
  - 多会话：会话/消息持久化在 SQLite；支持中断、继续、历史回溯。
  - 不强依赖任何 Agent 框架；仅需一个 `chat()` 函数 + FastMCP 工具即可跑通最小闭环。
- **LLM Provider 抽象层（详见 4.8）**：统一 `chat(messages, tools)` 接口，切换 CodeBuddy / OpenAI 兼容 / DeepSeek / Anthropic / Ollama / 自定义；前端「设置→模型」配置，无代码改动。
- **React 前端（可视化页面）**：
  - 回测页：表单选标的/区间/策略 → 调 REST → 指标 + 收益曲线。
  - 下单页：代码/方向/价格/数量 → 风控校验 → 下单；持仓/委托实时刷新（WebSocket）。
  - 分析看板：净值、月度收益、标的贡献、滑点等（ECharts/AntV）。
  - **对比/测量效果看板**：指标矩阵 + 曲线叠加 + 敏感性热力图（见 4.7）。
  - **托盘迷你面板**：常驻显示净值/持仓/告警，不开主窗也能盯盘。
  - **设置页**：Provider 配置（厂商/Base URL/Key/模型/温度/超时）、全局默认与每会话覆盖、连接测试。
- **权限与主题**：实盘下单走二次确认；深/浅主题，图表配色同步。

### 4.4 回测引擎
- **方案 A（推荐起步）**：复用 QMT-MCP 内置回测 + EzQmt 的 `FreeBack`，快速出指标（收益/回撤/夏普/VaR/胜率/成交明细/评级）。
- **方案 B（进阶）**：引入 `backtrader`/`vnpy` 做更严谨回测（未来函数、生存偏差、交易成本），QMT 仅做实盘/模拟下单。
- **产出**：统一回测报告结构；支持多方案批量回测与参数/指标敏感性扫描，供「测量效果」对比消费（见 4.7）。

### 4.5 下单与交易执行
- 复用 `place_order` / `cancel_order`，增强：市价/限价、条件单、批量下单。
- 前端下单页 + Agent 对话下单，统一走网关风控。
- 状态：委托/成交/持仓/资金经 WebSocket 实时同步刷新（见 4.13）。

### 4.6 数据分析与绩效
- 复用 EzQmt：`account().pnl()`、`pnl_monthly()`、`contri`、`cal_deal_comm`（滑点）、交易成本分析、`displaystrats_pos/pnl`（分策略）。
- **元数据/策略标注管理（v7，全功能覆盖细节）**：EzQmt 的策略归类依赖**交割单备注**、转债转股依赖**条款标注**、业绩基准/外部资金/策略合并均需人工元数据。补一个「元数据管理」页/工具录入这些标注（策略→标的映射、备注关键词、转债条款、基准代码、外部资金流水、合并规则），否则这部分全功能无法运行。
- 封装为分析类工具与看板页，支持按策略/按标的下钻，接入「测量效果」对比（4.7）。

### 4.7 策略对比与「测量效果」分析（核心模块）
> 需求点：**支持分析不同测量效果**——横向对比不同策略/参数/评价指标口径下的回测与实盘表现，找稳健可复制方案。

- **多方案横向对比（`compare_backtests`）**：一组回测配置 → 指标矩阵并排 + 收益/回撤/资金曲线叠加；按夏普/回撤排序定位最优。
- **参数/指标敏感性扫描（`sensitivity_analysis`）**：网格扫描策略参数 → 参数-指标热力图（识别稳健区间、防过拟合）；同策略在不同基准/费率/再平衡频率下的指标变化，量化「测量口径」影响。
- **实盘 vs 回测一致性**：EzQmt 分策略实盘盈亏与回测结果并排校验。
- **呈现**：Agent 对话（"对比(5,20)与(10,30)哪个夏普高"）+ 对比看板（ECharts 指标矩阵 + 曲线叠加 + 热力图，CSV/PDF 导出）。

### 4.8 LLM 提供商可插拔设计（不依赖特定 SDK 的关键）
> 需求点：是否可不依赖 CodeBuddy SDK、支持任意 API、由界面配置、非强制。结论——可以，且架构更优。

- **统一接口契约**：`LLMProvider.chat(req) → AsyncIterable<{delta?; toolCall?; finishReason?}>`（流式）；所有后端翻译成这套输入输出，Agent Core 不感知厂商。
- **内置适配器**：
  - **OpenAI 兼容**（默认首选）：`/v1/chat/completions`，Base URL + Key + model 即可——覆盖 CodeBuddy、DeepSeek、通义/百炼、智谱 GLM、Kimi、Ollama、vLLM、LM Studio、OneAPI 等。
  - **Anthropic（Claude）**：独立适配器处理 `tool_use` 格式差异。
  - **自定义**：任意兼容 OpenAI 协议端点即视为自定义。
- **工具协议对齐**：MCP `tools/list` 自动映射为 LLM `tools`；LLM `tool_calls` 反向映射回 MCP `tools/call`。换任何支持 function calling 的模型都能驱动 QMT 工具。
- **用户自助配置（界面，非强制、非预置）**：设置页选厂商填 Base URL/Key/模型/温度/超时；全局默认 + 每会话覆盖；连接测试；**零强制**——不配 LLM 时传统页面与 MCP/REST 照常工作。
- **密钥安全**：LLM Key 用 AES-256-GCM 加密落库，主密钥来自 env/OS keychain，前端脱敏回显。
- **健壮性**：单 Provider 失败回退备用；超时/限流重试；可观测日志（token/耗时/次数）。

### 4.9 可视化界面体系（支持「各种可视化界面」）
- **界面形态**：主窗口看板（回测/对比/账户/行情四大页）、系统托盘迷你面板（常驻盯盘/告警）、设置向导、Agent 对话面板（图表内联）。
- **图表引擎矩阵（按场景选型）**：
  - **ECharts**：看板、指标矩阵、热力图、雷达图、树图（标的贡献）。
  - **lightweight-charts**：K 线/分时/成交标记（TradingView 风格，买卖点叠加）。
  - **Plotly**：强交互散点、参数敏感性 3D 曲面。
  - **AntV G2 / G6**：关系图、资金流向、策略依赖编排。
- **增强能力**：可配置看板（拖拽布局）、大屏投屏、深/浅主题、WebSocket 实时增量刷新。

### 4.10 桌面壳与 EXE 可执行打包（Windows 一键安装）
- **桌面壳选型**：Electron（起步，React 直接渲染，坑少，包 ~150MB+）或 Tauri（体积优先，~10–20MB，需 Rust）。前端一致，切换成本低。
- **进程模型**：桌面壳启动 → 后台拉起 **FastAPI 进程**（监听 `localhost`，PyInstaller 出的 `qmt_work.exe`）→ WebView 指向 `http://localhost:port`；关闭壳优雅退出子进程。
- **Python 端打包**：FastAPI + QMT-MCP 用 **PyInstaller** 打成 `qmt_work.exe`（XTQuant 专有依赖打入），随桌面壳 resources 分发。
- **安装分发**：electron-builder/tauri build → `qmt_work-Setup.exe`（NSIS）；开始菜单 + 桌面图标；系统托盘常驻；可选开机自启（配合 QMT 自启）；数据落 `%APPDATA%/qmt_work`，不写 Program Files；可选 electron-updater 增量更新。
- **对架构影响**：桌面壳只是承载层，不改变下层（FastAPI/MCP/Agent/同步引擎）；浏览器 localhost 与桌面壳体验一致，第三方仍走 MCP(SSE)/REST。
- **独立客户端形态细化（v7，用户强调「独立客户端」）**：
  - **单机一体模式（默认）**：FastAPI + SQLite + 前端全部打包在 exe 内，**无外部服务依赖**，双击即跑，开箱即用；同时保留「连远程服务端模式」开关，供多端共享场景。
  - **两种发行包**：① **NSIS 安装版** `qmt_work-Setup.exe`（开始菜单/桌面图标/托盘/可选开机自启）；② **免安装绿色版** zip（解压即用，`qmt_work.exe` 直接运行，适合临时机器/沙箱）。
  - **QMT 依赖检测与引导**：启动自检 QMT 客户端是否运行、XTQuant 可用性、账户是否登录；缺失则弹**首次启动向导**（绑定 QMT_PATH/账户 → 风控参数 → 可选配 LLM → 连接测试），不强制装额外运行时。
  - **代码签名**：用自签/机构证书签名 exe，减少 Windows SmartScreen 拦截与「未知发行者」警告。
  - **自动更新**：electron-updater / Tauri updater，更新包签名校验防篡改；版本号语义化。
  - **数据迁移**：SQLite schema 用 alembic/手写 migration 管理，版本升级自动迁移 `%APPDATA%/qmt_work/app.db`，旧数据兼容。

### 4.11 数据存储与数据模型（SQLite）
- **引擎**：`sqlite3`（Python 标准库，零运维），库文件落 `%APPDATA%/qmt_work/app.db`（桌面壳）或服务端 `./data/app.db`。
- **核心表**：
  - `users`(id, username, role, qmt_account_id, created_at)
  - `api_keys`(id, key_hash, user_id, name, scopes, rate_limit, status, created_at)——**只存哈希**
  - `llm_config`(id, scope, provider, base_url, api_key_enc, model, temperature, timeout_ms, is_default, updated_at)——`api_key_enc` 用 **AES-256-GCM** 加密
  - `sessions` / `messages`(会话与对话历史)
  - `backtests` / `comparisons`(回测与对比结果)
  - `risk_config`(风控参数)
  - `audit_log`(全量调用与交易留痕)
  - **`market_cache`**(code, dtype, ts, payload_json)——行情/快照本地缓存（同步引擎落库，支持离线查询）
  - **`account_snapshot`**(account_id, ts, net_value, positions_json, cash_json)——账户定时归档，构成绩效数据仓库
  - **`sync_state`**(stream, last_seq, last_ts, status)——订阅/同步状态与断点续传游标
- **密钥与加密**：API Key 存 SHA-256 哈希；LLM Key AES-256-GCM 加密，内存解密，前端脱敏回显。

### 4.12 接口契约（第三方接入的明确边界）
- **REST API（Bearer 鉴权，统一走网关风控，`/api/v1` 前缀，统一 `{code,message,data}`）**：
  - 认证：`POST /api/auth/login`
  - 回测：`POST /api/backtest`、`GET /api/backtest/:id`、`GET /api/backtests`
  - 对比：`POST /api/compare`、`GET /api/comparisons`
  - 交易：`POST /api/orders`、`DELETE /api/orders/:id`、`GET /api/orders`、`GET /api/positions`、`GET /api/cash`
  - 账户：`GET /api/account/status`、`GET /api/account/pnl`、`GET /api/account/slippage`、`GET /api/account/cost`
  - 分仓：`POST /api/rebalance`
  - 行情：`GET /api/quote/:code`、`GET /api/kline/:code`、`POST /api/market/crawl`
  - 配置：`GET|PUT /api/config/llm`、`GET|POST /api/api-keys`
  - Agent：`POST /api/agent/chat`（SSE/WS 流式）
  - 健康：`GET /api/health`
  - 风控拦截返回 `403 + 原因`。
- **MCP 工具（SSE `/mcp/sse`）**：行情 `get_quote`/`get_kline`/`get_tick`/`crawl_market_data`；交易 `place_order`/`cancel_order`/`query_position`/`query_orders`/`query_cash`；账户 `monitor_account`/`account_status`/`account_pnl`/`strategy_contribution`/`slippage_analysis`/`cost_analysis`；分仓 `rebalance_position`；策略 `save_qmt_strategy`/`generate_ma_strategy`；回测对比 `run_backtest`/`compare_backtests`/`sensitivity_analysis`。参数/返回值用 JSON Schema 描述。
- **WebSocket（数据同步）**：`/ws/account`（持仓/资金/委托增量）、`/ws/quote`（订阅行情）、`/ws/notify`（风控/成交告警）；连接即发全量快照，之后增量广播，断线重连补发。
- **版本演进**：REST Breaking 升版本；MCP 工具名稳定，新增不删旧。

### 4.13 数据同步引擎（新增核心模块）
> 需求点：**支持同步数据**——让可视化界面看到的与 QMT 真实的始终保持一致，减少对 QMT 实时接口依赖，支持多端一致与离线查询。

1. **实时订阅层（QMT → 缓存）**：启动即对 XTQuant 订阅行情(`subscribe_quote`)、账户(`subscribe_account_status`)、持仓/委托/成交(`subscribe_push`)；回调写入进程内内存缓存（高频秒级刷新）+ 本地 `market_cache`/`account_snapshot`（持久化、断网可用）。
2. **本地数据缓存（历史落库）**：K 线/分钟线定时落库（滑点分析依赖分钟线）；账户净值/持仓/资金定时归档，构成绩效数据仓库，支持净值曲线/月度收益/标的贡献离线重算。首启全量拉取 → 之后增量事件驱动；心跳保活。
3. **多端一致同步（服务端 → 前端/第三方）**：FastAPI WebSocket 推送；Web 标签页、桌面托盘、第三方任一端连接收到全量快照 + 增量广播；订单成交/风险触发即时推送所有端；断线重连服务端基于 `sync_state` 游标补发缺失快照。
4. **配置/状态同步**：Web 端 Provider、风控、账户绑定存服务端 SQLite，任一客户端登录即拉取，跨设备一致；会话/对话历史服务端持久化，多端续聊一致。
5. **跨机同步（进阶）**：QMT 在 A 机、用户远程 B 机，数据经 FastAPI REST/WS 同步；多 QMT 账户做数据汇聚层（按 account_id 路由）。
6. **一致性/可靠性**：QMT 为唯一真相源，平台缓存为只读镜像 + 操作回执；下单写操作直达 QMT，回执再同步；事件带唯一 ID 防重复；监控订阅连接状态/推送延迟/断线告警。
7. **订阅聚合与按需推送（v7，性能/带宽）**：多客户端订阅同一标的时，服务端**只向 QMT 订阅一次**，再扇出广播；客户端按需订阅/退订（`subscribe`/`unsubscribe` 消息），降低后端与 QMT 负载与带宽。
8. **缓存淘汰**：`market_cache` 设 TTL + 容量上限（如最近 N 日分钟线），超出按时间淘汰；热标的常驻、冷标的过期重拉。

### 4.14 XTQuant 并发与线程模型（v7 关键技术风险）
> **风险**：FastAPI 跑在 asyncio 事件循环，而 XTQuant 是**同步阻塞**调用，且其订阅回调来自 QMT 自己的工作线程。若在 async 路由里直接同步调 XTQuant，会**卡死整个事件循环**（WebSocket/Agent/回测全部停摆）；若多协程并发下单还会竞态。

- **同步调用隔离**：所有 XTQuant 同步调用（下单/查询/历史数据）统一经 `loop.run_in_executor(thread_pool)` 或专用工作线程执行，**绝不阻塞事件循环**；工具函数对外仍是 async 接口。
- **回调线程→事件循环**：QMT 订阅回调（来自其内部线程）通过**线程安全队列**（`janus.Queue`/`asyncio.run_coroutine_threadsafe`）投递到事件循环，再写缓存/广播 WebSocket——回调线程内禁止直接 await。
- **下单串行化**：`xt_trader` 连接是单例，并发下单加 `asyncio.Lock`（或 threading.Lock）防竞态与撤单/下单乱序。
- **连接生命周期**：启动建连、断线重连、QMT 关闭检测；连接状态暴露给 `/api/health` 与托盘迷你面板。
- **结论**：这是落地前必须固化的并发约束，否则线上必出"卡死/串单"事故。

### 4.15 回测任务编排与并行（v7，测量效果扫描必需）
> **场景**：敏感性扫描/多方案对比需跑几十~几百次回测，单次 QMT 回测较慢，不能在请求里同步等。

- **回测任务队列**：`backtest_jobs` 表（id, params, status[pending|running|done|failed], progress, result_id, created_at）；FastAPI 起**有限并发**的回测工作池（CPU 密集回测放 `ProcessPoolExecutor` 避免阻塞事件循环）。
- **组合任务**：`compare_backtests` / `sensitivity_analysis` 作为「组合任务」聚合多个子回测，整体进度经 WebSocket 推送（已完成 N/M）。
- **结果缓存**：相同参数（标的+区间+策略+参数+资金+费率）命中缓存不重跑，扫描加速。
- **可取消**：长任务支持取消；中断后状态落库可续跑。

### 4.16 工程目录结构（v7，可落地骨架）
```
qmt_mcp/
├─ backend/                 # FastAPI 统一后端（Python 单进程）
│  ├─ app/                  # main.py 启动、路由聚合、生命周期
│  ├─ gateway/              # REST 网关：鉴权/限流/风控/审计中间件
│  ├─ mcp_server/           # FastMCP 装配 + tools 注册（避免与官方 mcp 库同名冲突）
│  ├─ tools/                # 工具实现（行情/交易/账户/分仓/策略/回测/分析/对比）
│  ├─ agent/                # Agent Core + LLM Provider 抽象 + 适配器
│  ├─ sync/                 # 数据同步引擎：订阅/缓存/WebSocket 广播
│  ├─ backtest/             # 回测引擎 + 任务队列/进程池
│  ├─ analysis/             # EzQmt 绩效/滑点/成本复用
│  ├─ xtquant_client/       # XTQuant 封装（线程模型、单例、锁）
│  ├─ db/                   # SQLite 模型、migration（alembic）
│  └─ core/                 # 配置、日志、加密、健康检查
├─ frontend/                # React + Vite + TDesign + 图表矩阵
│  ├─ pages/                # 回测/对比/账户/行情/下单/设置/Agent 对话
│  ├─ components/           # 托盘面板、看板组件、图表封装
│  └─ ws/                   # WebSocket 客户端（自动重连/退避/订阅）
├─ desktop/                 # Electron/Tauri 桌面壳
│  ├─ main/                 # 进程拉起、托盘、单例锁、自检向导
│  └─ build/                # NSIS/绿色版打包脚本、签名
├─ scripts/                 # 构建/打包/迁移脚本
├─ docs/                    # 方案与 API 文档
└─ build/                   # 产物（exe/安装包）
```

### 4.17 可观测性与配置热更新（v7，运维必需）
- **日志**：按日滚动（`logs/app-YYYY-MM-DD.log`），分级；交易/下单/风控拦截单独留痕。
- **指标埋点**：LLM 调用 token/耗时、订阅健康（连接状态/延迟/断线次数）、回测耗时、任务队列深度。
- **健康检查**：`/api/health` 扩展为综合健康（QMT 连接/订阅状态/任务队列/磁盘/LLM 可用性），托盘迷你面板据此显示红绿灯。
- **配置热更新**：风控参数、LLM 配置改后**内存热生效**（工具入口即时读取最新值），无需重启；改风控写审计日志。
- **前端 WebSocket 客户端**：自动重连 + 指数退避 + 订阅/退订 + 断线提示 + 重连后按游标补拉。

---

## 5. 技术选型

| 维度 | 选型 | 说明 |
|------|------|------|
| 可视化前端 | React 18 + Vite + TDesign + Tailwind | 回测/对比/账户/行情看板 + 托盘面板 + 设置向导 |
| 统一后端 | **FastAPI（Python 单进程 ASGI）** | 同时承载 REST 网关 + MCP SSE + Agent 路由 + 数据同步引擎 + 静态托管 |
| MCP 服务 | FastMCP（挂载进 FastAPI） | 工具即同进程 Python 函数，REST/MCP/Agent 三方复用 |
| Agent 引擎 | 自研轻量 Agent Core（ReAct + 工具调用） | 不依赖特定 SDK；工具来自同进程 MCP 函数 |
| LLM Provider 抽象 | 统一 `chat()` + OpenAI 兼容/Anthropic/自定义适配器 | 任意 LLM 可插拔，界面配置，非预置、非强制 |
| QMT 适配 | XTQuant（Windows） | 必须的本地依赖 |
| 分析/回测 | EzQmt + FreeBack（进阶 backtrader） | 参考 EzQmt |
| 实时同步 | FastAPI WebSocket + 内存缓存 + SQLite | 账户/行情/成交多端实时一致、断线重连补发 |
| 图表引擎 | ECharts + lightweight-charts + Plotly + AntV G2/G6 | 看板/热力图(ECharts)、K线成交(lightweight-charts)、3D(Plotly)、关系图(AntV) |
| 可视化形态 | 主窗口看板 + 托盘迷你面板 + 设置向导 + Agent 对话 + 可配置看板 + 大屏投屏 | 多表面、深/浅主题、实时推送 |
| 桌面壳 / 打包 | Electron（起步）或 Tauri + NSIS | 拉起 FastAPI 进程，托盘常驻，开机自启可选 |
| Python 端打包 | PyInstaller → qmt_work.exe | XTQuant 专有依赖打入，随桌面壳分发 |
| 鉴权/网关 | FastAPI 中间件（API Key + 限流） | 第三方接入 |
| 配置/存储 | SQLite（AES-256-GCM 加密）+ `.env`（端口/风控） | 会话/消息/回测/对比/审计/缓存/配置；API Key 仅存哈希 |
| 并发/线程模型 | asyncio + `run_in_executor` + 线程安全队列 + 下单锁 | XTQuant 同步调用隔离，不阻塞事件循环、防竞态 |
| 回测任务队列 | `ProcessPoolExecutor` + `backtest_jobs` 表 + 结果缓存 | 测量效果扫描的批量/并行回测，进度推送、可取消 |
| 可观测性 | 按日日志 + 指标埋点 + 综合健康检查 + 配置热更新 | 运维与托盘红绿灯 |
| 数据迁移 | alembic（SQLite schema migration） | 版本升级自动迁移，旧数据兼容 |
| 独立客户端 | 单机一体模式 + NSIS 安装版 + 免安装绿色版 + 代码签名 + 自动更新 | 双击即跑、零外部依赖、防 SmartScreen 拦截 |

---

## 6. 关键设计决策与权衡

1. **统一 FastAPI 后端（v6 关键改进）**：MCP 服务、分析层、Agent 引擎本就全是 Python，用 FastAPI 单进程承载 REST+MCP(SSE)+Agent+同步引擎，工具即同进程函数，三端复用一份实现；消除「Node 网关 + Python MCP 双进程 + TS 跨进程调 Python」的割裂，部署与打包更简单。
2. **XTQuant Windows 约束**：FastAPI 后端（含 MCP）必须随 QMT 部署在 Windows；浏览器/桌面壳/第三方经 SSE/REST 远程访问。
3. **Agent 调用交易**：后端 Agent Core 直接调用同进程 MCP 工具函数，避免交易逻辑写死前端；LLM 仅作「可选大脑」，可插拔。
4. **LLM 不锁死厂商**：Agent Core 只依赖 `LLMProvider.chat()`，背后接任意 OpenAI 兼容/各家；默认不预置密钥、界面配置、可热切换、可回退；不配 LLM 传统页面与 MCP/REST 照常可用。
5. **数据同步引擎（v6 新增）**：QMT 订阅 → 缓存 → WebSocket 广播，保证多端一致、断线可恢复、离线可查；QMT 为唯一真相源。
6. **回测引擎选型**：先用 QMT-MCP+FreeBack，再视严谨度引入 backtrader。
7. **风控双闸**：MCP 工具入口校验 + 网关统一校验，双保险。
8. **桌面壳只是承载层**：EXE 仅拉起 FastAPI + 承载前端，不改变下层；图表引擎按场景选型。
9. **XTQuant 线程模型（v7 关键风险）**：XTQuant 同步阻塞，必须经线程池隔离、回调经线程安全队列投递、下单串行化——否则线上必出卡死/串单事故。
10. **独立客户端单机一体（v7）**：默认 FastAPI+SQLite+前端全打包、双击即跑、零外部依赖；同时支持连远程服务端模式。
11. **鉴权分层（v7）**：本地 loopback 免 Key、远程第三方必 Key，兼顾易用与安全。
12. **回测任务编排（v7）**：敏感性扫描需任务队列+并行+缓存+可取消，不能在请求里同步等。

---

## 7. 功能规划（按角色）
- **回测**：标的选择、区间、策略模板、参数；输出收益/回撤/夏普/胜率 + 曲线 + 成交明细（覆盖 QMT-MCP 全量指标与评级）。
- **对比 / 测量效果分析**：多方案横向对比、参数敏感性扫描、评价口径对比、实盘 vs 回测一致性。
- **下单**：限价/市价/条件/批量；风控校验；委托/持仓/资金 WebSocket 实时刷新（覆盖 QMT-MCP 全能力）。
- **账户监控与分仓**：实时监控；多策略自动分仓/再平衡（覆盖 EzQmt Reblance）。
- **行情**：快照/历史 K 线/逐笔；行情爬虫（覆盖 EzQmt 爬虫）。
- **分析**：净值、月度收益、标的贡献、分策略盈亏、滑点、成本、基准对比（覆盖 EzQmt 全部分析）。
- **Agent 对话**：自然语言回测/下单/分析/对比，工具调用可视化。

---

## 8. 安全与风控体系
- **账户隔离**：API Key 绑定具体 QMT 账户，禁止越权。
- **鉴权分层（v7）**：本机桌面壳连 `localhost` **免 API Key**（loopback 校验 + 本地令牌），降低使用门槛；远程/第三方**必须 API Key** + 风控 + 限流。网关中间件按来源（loopback vs 远程）分层。
- **交易风控**：单笔上限、单标的持仓上限、最小下单量、市价价差保护、最大回撤/夏普/杠杆/止损阈值。
- **操作确认**：实盘下单二次确认（Agent 走 permission 对话确认）。
- **审计与日志**：全量调用与交易留痕。
- **限流**：按 Key 限频。
- **密钥安全**：API Key 仅存哈希；LLM Key AES-256-GCM 加密，主密钥来自 env/OS keychain，前端脱敏回显。
- **同步安全**：WebSocket 鉴权（同 API Key）；事件唯一 ID 防重放；断线补发带游标校验。

---

## 9. 多租户与第三方调用方案
- **API Key 体系**：每第三方/用户一对 Key，绑定账户、权限、限流。
- **MCP 远程接入**：第三方 AI 助手配置 `http://host:21117/mcp/sse` 即发现工具。
- **Web 登录**：平台用户账号体系（可先单账户，后续多用户）。
- **隔离边界**：共享 QMT 账户时靠风控与审计兜底；多账户按 Key 路由。

---

## 10. 开发路线图（分阶段）
- **Phase 0 调研与对齐**：确认 QMT 环境、账户类型、LLM 端点（可选）。
- **Phase 1 统一后端骨架（FastAPI）**：搭建 FastAPI 单进程，挂载 FastMCP SSE，定义工具 JSON Schema，跑通本地 SSE 调用；接入 XTQuant 行情/交易/账户函数；**先固化 §4.14 线程模型（线程池隔离 + 队列 + 下单锁）**。
- **Phase 2 数据同步引擎**：QMT 订阅 → 内存/SQLite 缓存 → WebSocket 广播；`market_cache`/`account_snapshot`/`sync_state` 落库；断线重连补发；**订阅聚合与缓存淘汰**；前端 WS 客户端（自动重连/退避）。
- **Phase 3 API 网关**：API Key、限流、统一风控、审计；**鉴权分层（loopback 免 Key / 远程必 Key）**；落地 §4.12 REST + WebSocket 端点；可观测性与综合健康检查；配置热更新；**§4.6 元数据/策略标注管理页**。
- **Phase 4 Agent Core + LLM Provider 抽象**：自研 Python Agent Core，OpenAI 兼容/Anthropic/自定义适配器，注册同进程工具，跑通对话式回测/下单/分析；「模型设置」界面（自配置、连接测试）。
- **Phase 4.5 回测任务编排**：`backtest_jobs` 队列 + 进程池 + 结果缓存 + 可取消 + 进度推送；`compare_backtests`/`sensitivity_analysis` 组合任务跑通。
- **Phase 5 可视化前端**：React 看板（回测/对比/账户/行情）、下单页、分析看板、托盘迷你面板（红绿灯）、设置向导、Agent 对话面板；图表矩阵接入；深/浅主题与实时推送。
- **Phase 6 独立客户端打包**：Electron/Tauri 桌面壳拉起 FastAPI 进程；PyInstaller 打 `qmt_work.exe`；NSIS 安装版 + 免安装绿色版；代码签名；自动更新；**首次启动向导 + QMT 依赖检测**；SQLite schema 迁移（alembic）。
- **Phase 7 文档与交付**：部署脚本、README、API 文档、安装包、演示视频；功能覆盖核对（§2.4）。

---

## 11. 部署与运行环境
- **必须**：Windows 主机 + 已登录 QMT 客户端 + XTQuant；Python 3.10+（FastAPI 后端）。
- **建议**：QMT 与 FastAPI 同机；浏览器/桌面壳/第三方经 SSE/REST 远程访问。
- **打包交付**：桌面壳将构建后前端 + FastAPI(`qmt_work.exe`, PyInstaller) 一并封包为 `qmt_work-Setup.exe`（NSIS）；数据落 `%APPDATA%/qmt_work`，不写 Program Files。
- **配置**：`.env`（QMT_PATH、SESSION_ID、ACCOUNT_ID、风控参数、加密主密钥）。

---

## 12. 风险与合规
- **交易风险**：实盘下单不可逆，强制二次确认 + 风控上限。
- **依赖风险**：XTQuant 仅 Windows、需 QMT 客户端常驻登录。
- **合规**：量化接口需符合券商与监管要求，对外 API 做好鉴权与审计。
- **数据权限**：回测/行情依赖 XTQuant 数据权限，需确认历史数据权限。
- **同步风险**：WebSocket 断线期间以下单回执为准，重连后快照校准，避免界面与实际持仓不一致。

---

## 13. 下一步建议
1. 确认 QMT 运行环境（Windows 主机、账户类型、是否已装 XTQuant）。
2. （可选）准备一个 OpenAI 兼容 LLM 端点与 Key；**不强制**——平台可在无 LLM 下先跑通传统页面与 MCP/REST。
3. 初始化 FastAPI 后端骨架（命名如 `qmt-agent-server`）+ React 前端骨架。
4. 拉取 QMT-MCP、EzQmt 作为 MCP/分析层基线，进入 Phase 1（先打通 FastAPI + QMT-MCP 工具与同步引擎）。

> 待你确认方案后，再进入代码实现（Phase 1 起）。LLM 后端默认不预置、由界面配置。

---

## 14. 方案评审与改进清单（合理性检查）

> 本轮对照用户核心诉求「可视化界面 + MCP + FastAPI 接口 + 同步数据 + 全面覆盖参考项目」做合理性审查。

### 14.1 原方案（v5）不合理/待改进项
| 问题 | 原状态 | 改进（v6） |
|------|--------|-----------|
| **后端技术栈与诉求不符** | 用 Express + TypeScript，而你明确要求 **FastAPI** | 改为 **FastAPI 单进程**承载 REST+MCP+Agent+同步，与 MCP/分析层同语言，消除割裂 |
| **双进程/双语言割裂** | Node 网关经 TS MCP 客户端跨进程调 Python MCP 服务，工具逻辑两份 | 工具即同进程 Python 函数，REST/MCP/Agent **三端复用一份实现** |
| **缺数据同步设计** | 仅「SSE 实时推送」一句，无系统化同步 | 新增 **§4.13 数据同步引擎**：订阅→缓存→WebSocket 广播、断线补发、离线缓存、多端一致 |
| **实时通道单一** | 仅 SSE（单向） | 增 **WebSocket**：双向、断线重连、多端广播（`/ws/account` `/ws/quote` `/ws/notify`） |
| **全功能覆盖无证据** | 口头声称「全部覆盖」 | 新增 **§2.4 覆盖核对矩阵**，逐条映射参考项目功能→模块→工具/页面 |
| **打包对象不一致** | 桌面壳拉起 Express + 独立 Python MCP | 统一拉起 **FastAPI 进程**（已含 MCP），PyInstaller 打单个 `qmt_work.exe` |

### 14.2 合理性结论
- 技术栈收敛为 **Python(FastAPI) 后端 + React 前端 + Electron/Tauri 壳**，与 XTQuant/FastMCP/EzQmt 同生态，**最合理、最易落地**。
- 三组外部接口（MCP SSE / REST / WebSocket）由同一 FastAPI 进程提供，**边界清晰、鉴权风控统一**。
- 「LLM 可插拔 + 零强制」保留，Agent 仍是可选增强层，不影响传统页面与第三方接口。
- 可视化体系、EXE 打包、全功能覆盖、数据同步均已落实，方案具备直接实施条件。

### 14.3 仍建议后续确认的风险点
- QMT 账户是否具备足够历史行情/分钟线数据权限（影响滑点与回测）。
- 实盘 vs 模拟账户的下单风控阈值需按用户实际风险偏好配置。
- 若需多 QMT 账户聚合，数据同步引擎需扩展「按 account_id 汇聚」层（已在 §4.13 预留）。

### 14.4 第二轮评审（v7，用户强调「独立客户端」后补强）
> 对照「可视化界面 + MCP + FastAPI + 同步数据 + 全面覆盖 + 可打包独立客户端」再审一次，发现 v6 仍缺 6 项落地关键技术细节，已补：

| 第二轮发现 | v6 状态 | v7 补强 |
|------|--------|---------|
| **XTQuant 阻塞事件循环** | 未提，落地会卡死服务 | 新增 §4.14 线程模型：线程池隔离 + 队列投递 + 下单锁 |
| **回测批量扫描无编排** | 提了批量但无队列 | 新增 §4.15 任务队列 + 进程池 + 缓存 + 可取消 |
| **独立客户端形态模糊** | 只说"桌面壳拉起 exe" | §4.10 细化：单机一体模式、绿色版、QMT 检测向导、签名、自动更新、schema 迁移 |
| **鉴权一刀切** | 桌面壳连自己也要 Key | §8 鉴权分层：loopback 免 Key、远程必 Key |
| **EzQmt 元数据无录入途径** | 绩效/转债靠标注但没管理 | §4.6 补元数据/策略标注管理页 |
| **缺工程骨架/可观测/热更新** | 无目录、无日志/健康、配置改要重启 | 新增 §4.16 目录结构 + §4.17 可观测与配置热更新 |

### 14.5 合理性结论（更新）
- v7 后方案在「并发安全、任务编排、独立客户端、鉴权分层、可落地骨架」上闭合，**已具备直接开工条件**。
- 剩余唯一硬性前置仍是：确认 QMT 运行环境（Windows + XTQuant + 账户数据权限）。其余（LLM/可视化/打包）均可渐进交付。

---

## 15. 实现现状（v8：真实接口 · 多券商 · 前后端解耦）

> 本章记录 **v7 方案落地后的真实实现状态**（版本 0.1.0），标注与 v7 方案的差异、已交付能力与待办项。**全部为真实券商接口，无任何 mock**。

### 15.1 架构落地概览

| 层 | 落地方式 |
|----|---------|
| **券商接入层** | `BrokerAdapter` 抽象基类（`xtquant_client/base.py`）→ `XTPQuantAdapter`（迅投系 6 家：国金/华鑫/银河/中信建投/兴业/广发，共用 xtquant SDK）；同花顺/恒生 PTrade/掘金为**接口契约**（`adapters/ths.py|ptrade.py|juejin.py`，SDK 就绪即启用） |
| **连接管理** | `BrokerManager`（`xtquant_client/manager.py`）：多券商 / 多账户 / 多客户端版本并存，SQLite 持久化，运行时解析具体连接，是连接状态的**唯一真相来源** |
| **券商档案** | `xtquant_client/registry.py`：9 家 `BrokerProfile`（默认客户端路径、账户类型、SDK 要求、最低版本、说明），前端「券商连接」页自动列出 |
| **后端服务** | FastAPI 单进程承载 REST（`/api/v1/*`）+ MCP（Streamable HTTP，`/mcp`）+ WebSocket（`/ws`）+ 回测队列 + Agent + 静态托管 |
| **前端** | 纯 SPA（React + Vite），**不内置任何券商实现逻辑**，仅透传 `conn_id`（REST）/ `broker_id`（回测）；券商连接管理页 + 顶部连接选择条 |
| **错误语义** | 未连接券商时统一 HTTP 200 + 业务码 `503`，提示「到券商连接页添加」，**绝不返回假数据** |
| **打包分发** | 后端 PyInstaller → `qmt_backend.exe`（onedir，真实券商模式）；桌面 Electron 壳内嵌后端 EXE → `QMT量化Agent平台.exe`（便携版） |

### 15.2 与 v7 方案的关键差异

| v7 方案 | v8 实现 |
|---------|---------|
| 回测 CPU 密集任务放 `ProcessPoolExecutor` | **事件循环内执行**（券商连接在主进程，子进程无法共享），K 线经 `fetch_kline_async(broker_id, ...)` 取真实历史 |
| 单一 QMT 客户端 + mock 网关 | 多券商多版本真实接入，删除 `MockXTQuantGateway`，全链路真实 SDK 调用 |
| MCP SSE 传输 | MCP **Streamable HTTP**（`GET /mcp/` 返回 406 为协议正常行为，`POST /mcp/` initialize 返回 200） |
| FastMCP 工具迭代为 list | FastMCP 2.14.7 `get_tools()` 返回 `dict[str, Tool]`，`Tool` 暴露 `.parameters`（JSON schema dict，**无 `.input_model`**） |
| 前端内置券商逻辑 | 纯 SPA + `BrokerContext`，券商连接增/连/断/设活跃/删/探测全部走 REST，`active_bridge()` 决定实时行情跟随 |
| 参考项目覆盖 | EzQmt：五档快照/账户持仓委托成交/限价买卖/撤单/超价撤单/等权篮子再平衡/日度 CSV/绩效归因与滑点分析；QMT-MCP：下单/撤单/策略生成/MA 策略回测；Rockyzsu/QMT：HTTP 封装/算法单；quant-qmt-proxy：参考数据/L2/订阅流；QMT-QuantLimit：涨停监控/打板 —— **均以真实接口落地**（各项目 README 声称经核实有出入，以实际代码为准） |

### 15.3 已交付能力清单

- **券商管理**：`GET /brokers/profiles`、`GET/POST /brokers`、`POST /brokers/test`、`POST /brokers/{id}/connect|disconnect|active`、`DELETE /brokers/{id}`；支持 client_path / account_id / account_type / session_id / min_version 多版本参数。
- **行情**：get_quote / get_full_tick（五档快照）/ get_kline（含起止）/ get_tick / get_stock_list / search_stocks；WebSocket 实时推送跟随活跃连接。
- **交易**：place_order / cancel_order / cancel_order_price（超价撤单），下单过风控（`gateway/risk`：金额/数量/持仓比例）。
- **账户**：资产/持仓/资金/委托/成交实时查询，`/account/status`、`/account/pnl`（净值序列）、`/account/slippage`（对开盘/收盘/均价/VWAP 的滑点 bps）。
- **回测**：ma_cross / macd / rsi 三策略，真实 K 线，指标含总收益/年化/最大回撤/年化波动/夏普/胜率/盈亏比/VaR95/评级；支持对比与敏感性分析。
- **涨停监控/打板**（借鉴 QMT-QuantLimit）：股票池增删、三因子触发（last≥涨停价 且 时间≤截止 且 近25 tick 涨幅≥阈值）、WS 事件推送、可选自动涨停价买入（过风控）；`/limitup/*` + MCP 工具。
- **算法单**（借鉴 Rockyzsu/QMT）：TWAP/VWAP 时间等分拆单、暂停/恢复/取消、子单实时记录、WS 事件推送；`/algo/*` + MCP 工具。
- **策略模板库**（借鉴 QMT-MCP）：ma_cross/macd/rsi/limitup 四类 QMT 可运行策略代码生成、写入 QMT 客户端 mpython 目录；`/strategies/*` + MCP 工具。
- **参考数据/L2**（借鉴 quant-qmt-proxy）：交易日历、板块列表/成分、财务摘要、L2 逐笔成交；`/reference/*`、`/market/l2` + MCP 工具。
- **再平衡**：等权篮子按阈值调仓、按手拆单、涨跌停跳过、可 `do_trade` 实盘。
- **Agent**：FastMCP 工具自动暴露给 LLM（`AgentCore` 用 `list(tools.values())` + `t.parameters`）；LLM 提供商可插拔（OpenAI 兼容/Anthropic 等）；未配置时优雅 503 引导。
- **网关**：loopback 免 API Key、远程必 Key；限流（token/ip）；API Key 生成与管理。
- **同步引擎**：账户快照周期入缓存 + WebSocket 广播，多端一致。

### 15.4 待办与依赖

| 待办 | 依赖 | 说明 |
|------|------|------|
| 同花顺 / 恒生 PTrade / 掘金真实 SDK 接入 | 对应 SDK 安装（ths_quant_sdk / ptrade_sdk / gm） | 接口契约已就绪（`ExternalBrokerAdapter` 基类），SDK 就绪后仅需在 `start()` 内实现真实调用 |
| NSIS 安装包（`npm run dist`） | NSIS 工具链 | 便携版 `--dir` 已验证可运行；安装包/开始菜单/托盘/开机自启待打 |
| electron-updater 自动更新 | 更新服务器 + 签名证书 | v7 §4.10 预留 |
| 多 QMT 账户聚合层 | 按 account_id 汇聚（§4.13 预留） | 单账户已验证 |
| 历史行情数据权限 | QMT 账户分钟线/日线权限 | 影响滑点分析与回测深度（§14.3 遗留） |

### 15.5 验证与打包入口

- 后端冒烟：`python run.py` 后 `python tests/smoke2.py`（18 项，含 503 引导、API Key、Agent 未配置、WS）。
- 前端构建：`cd frontend && npm run build`（产物 `backend/static`，后端同源托管）。
- 打包：后端 `python build_exe.py` → `backend/dist/qmt_backend/qmt_backend.exe`；桌面 `cd frontend && npx electron-builder --dir`（跳过 `npm run pack` 的 vite rebuild）。
- 端口：`run.py 默认 21117，**被占用时自动 +1 平滑改口**（最多 10 次，`QMT_PORT` 指定起始端口）。
