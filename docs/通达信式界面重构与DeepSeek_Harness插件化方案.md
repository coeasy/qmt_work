# qmt_work · 通达信式界面重构 + DeepSeek Harness 插件化 · 系统性方案

> 版本：v1（合并版）
> 生成时间：2026-08-28
> 关联文档：
> - `docs/基于deepseek-harness的客户端重构方案.md`（dsh 插件化路线，本方案继承其 Phase 0 闸门与风险结论）
> - `docs/竞品对比与优化改进方案.md`（能力差距基线）
> - `docs/功能层问题与改进优化方案（2026-08-26审计版）.md`（P2-2 事件化、前端零基建）
>
> 结论先行：
> 1. **通达信式界面重构**与**DeepSeek Harness 插件化**是同一件事的两面——前者决定"长什么样"，后者决定"怎么被 DeepSeek/任意 LLM 调用"。两者可**双轨并行、先 UI 后插件**，且 UI 在现有 React SPA 即可先行交付，不阻塞 dsh 决策。
> 2. 可行性已确认：前端改造 90% 纯前端；后端仅新增 1 个轻量接口（`POST /market/quotes` 批量快照）+ 1 个聚合 F10 接口即可支撑报价牌/个股分析。dsh 接入依赖已有的 `/mcp`（Streamable HTTP，14 组工具），**零新增后端改造**。
> 3. 风险闸门：dsh 为 developer preview（官方明示会有破坏性变更）。遵循既有方案——**Phase 0 POC 不通过则降级为"路线 A"：仅在现有 SPA 内做通达信式重构，不引入 dsh**。TDX 界面价值不依赖 dsh，始终可交付。

---

## 一、目标与范围

### 1.1 目标
- **界面范式**：把当前 25 页平铺式 SPA，重构为通达信（TDX）经典范式——顶菜单 + 指数跑马灯 + 左侧功能树 + 中央多窗口工作区（MDI，可拆分）+ 底部综合栏 + 报价牌 + 五档盘口 + 键盘流。
- **插件化交付**：把 qmt_work 的交易/行情/研究/运维能力封装为 **DeepSeek Harness（dsh）插件**，使 dsh 宿主内的 Agent（DeepSeek 或任意 LLM）能通过 MCP 直接调用我们的全部功能；同时 UI 以 dsh `ui-*` 插件形态渲染，主题即上述 TDX 风格。
- **不丢功能、不造假**：现有 35 个组件 / 139 个 REST 端点 / 14 组 MCP 工具全部保留并重新挂载；坚持"零 mock、不造假数据"，行情来源（券商/补充源）在前端显式标注。

### 1.2 不在范围
- 后端 FastAPI、xtquant 桥接层、SQLite 存储、风控/审计/WAL/对账逻辑：**不动**（继承既有方案"明确不做的事"）。
- PyInstaller 后端打包流程：不动。

---

## 二、总体策略：双轨并行、收敛于 dsh 插件

```
┌──────────────────────────────────────────────────────────────────┐
│ 轨道 1（UI/UX，可独立交付）                                         │
│   现有 React SPA ──通达信式重构──> TDX 风格 SPA（顶菜单/功能树/      │
│   工作区/综合栏/报价牌/个股分析/键盘流）                            │
│   价值：立即提升可用性，不依赖 dsh 任何决策                          │
└───────────────────────────────┬──────────────────────────────────┘
                                 │  Phase 5 起：把 TDX 风格的 UI 组件
                                 │  平移为 dsh ui-qmt-* 插件（同套代码）
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ 轨道 2（Packaging，dsh 插件化）                                      │
│   DeepSeek Harness 宿主                                             │
│   ├─ host 网关 qmt-bridge：REST/WS 代理 + API Key 注入 + 类型化 RPC  │
│   ├─ mcp 包 → 连接后端 /mcp（14 组工具即 Agent 工具集）             │
│   └─ client：ui-qmt-* 插件（TDX 风格外壳）挂载 slot                 │
│   Agent（DeepSeek）经 MCP 调用我们的功能；UI 经 REST/WS 渲染        │
└──────────────────────────────────────────────────────────────────┘
```

**为什么分两轨**：dsh 决策有不确定性（破坏性变更风险），但 TDX 界面需求确定且高价值。先交轨道 1，避免"等 dsh 定型"导致界面改进停滞；轨道 2 的 POC 通过后再把已写好的 TDX 组件"换壳"进 dsh，复用率极高。

---

## 三、通达信式界面规格

> 落地形态：轨道 1 写在现有 SPA（新建/改写组件）；轨道 2 把这些组件作为 dsh `ui-qmt-*` 插件入口。组件名一致，仅挂载方式不同。

### 3.1 顶菜单栏 + 指数跑马灯 `MenuBar.jsx` / `Ticker.jsx`
- 菜单：`系统 | 行情 | 分析 | 交易 | 策略 | 研究 | 资讯 | 工具 | 帮助`，右侧常驻**指数跑马灯**（上证指数/深证成指/创业板指/沪深300，红涨绿跌滚动，数据来自 WS `quotes` 帧订阅指数代码）。
- 替代现有 `App.jsx:122` 的 `brand` + `SystemBadge`。

### 3.2 左侧功能树 `FunctionTree.jsx`
可展开树（替代 `App.jsx:125-147` 分组侧栏）：
- **行情**：自选股（分组）/ 沪深A股 / 板块（行业·概念·地域）/ 综合排名 / 涨幅榜
- **分析**：技术分析（分时+K线）/ 个股资料(F10)
- **交易**：手动交易 / 条件单 / 算法交易 / 模拟盘
- **策略**：模板生成 / 策略市场
- **组合**：目标持仓 / 即时再平衡
- **研究**：回测对比 / 因子指标 / 研究深度 / 参考数据
- **信号**：信号路由 / 告警 / 通知 / Webhook
- **账户**：连接管理 / 多账户网格
- **运维**：系统状态 / 审计 / 对账
- **设置**
- 点击叶子 → 在工作区打开（新标签或聚焦已有）；支持拖入分屏。

### 3.3 中央工作区（MDI）`Workbench.jsx` + `Pane.jsx`
- 替代现有 `Hub.jsx`（仅子 tab，不可分屏）。
- 标签式多文档；每标签可**上下/左右拆分**为多个 Pane；Pane 可关闭/置换；布局存 localStorage。
- Pane 内容类型：报价牌 / 个股分析 / 回测 / 策略 / 账户网格 / 任意旧页（全部旧组件可装进 Pane）。

### 3.4 底部综合栏 `BottomDock.jsx`
可折叠的多 tab 栏（全局常驻）：自选股 | 板块 | 预警 | 成交回报 | 资金流向 | 日志。高度可调。

### 3.5 报价牌 `QuoteBoard.jsx`（重构 `MarketData.jsx`）
- 列：代码/名称/最新/涨跌额/涨幅/振幅/换手%/量比/成交量/成交额/市盈。
- 红涨绿跌；点击表头排序；点击行 → 打开个股分析；板块筛选（沪A/深A/创业板/板块/自选）。
- **综合排名视图**：多列并排（涨幅/5分钟涨速/量比/振幅/成交额/换手），仿 TDX `80`。
- 实时：进入某板块即 `subscribe_quote(codes)`，由 WS `quotes` 帧刷新（不轮询）。

### 3.6 个股分析页 `StockAnalysis.jsx`（TDX 招牌，优先级最高）
```
┌──────── 分时图/K线(主图+成交量+指标副窗 MACD/KDJ) ────────┬─ 五档盘口 ─┐
│ 代码 名称 [最新][涨跌][涨幅] 市盈 板块  F5:分时↔K线 F10:资料 │ 卖五~卖一  │
│                                                          │ 最新/昨收  │
│  ECharts 主图（分时/日K/分钟K，复用 Chart.jsx）           │ 买一~买五  │
│                                                          │ 委托/成交(tab)│
├──────────── 财务/F10 资料 / 新闻(可折叠) ─────────────────┴───────────┘
```
- 数据：快照走 WS `quotes` 帧 + `/market/quote` 兜底；K线 `/market/kline`；五档走 quote 的 `bids/asks`。
- **数据源徽标**：标题栏显示 `来源:券商/行情源`（沿用 `source` 字段），符合零 mock 透明原则。

### 3.7 键盘流 `useHotkeys.js` + 命令面板 `CommandPalette.jsx`
- 敲数字+Enter：`60`沪A涨幅/`61`/`63`/`80`综合排名；直接敲 `600519`+Enter → 开个股。
- `F3`上证 `F4`深证 `F5`分时↔K线 `F6`自选 `F10`资料 `F12`交易 `Esc`关当前。
- `Ctrl+K` 命令面板（搜页面/代码/板块/动作；后端 `/market/search` 已有）。

### 3.8 视觉 `styles.css`
- 红涨绿跌（A股习惯）、信息密度高（紧凑行、小字号）、深/浅主题切换（TDX 默认浅蓝，给浅色主题）、指数跑马灯滚动。

---

## 四、作为 DeepSeek Harness 插件的形态

> 本节约等于既有 `docs/基于deepseek-harness的客户端重构方案.md` 的路线 B，但把"UI 插件"明确重定向为上文的 TDX 风格，并补强"Agent 如何调用我们的功能"。

### 4.1 插件定位
| 包 | 作用 |
|---|---|
| `host` 侧 `qmt-bridge` | REST/WS 代理 + API Key 注入 + 错误码解包 + 类型化 RPC（OpenAPI → TS）；**不含任何交易逻辑** |
| `host` 侧 `mcp` 接入 | dsh `mcp` 包直连后端 `/mcp`（Streamable HTTP），14 组工具即 Agent 工具集；X-API-Key 透传 |
| `client` 侧 `ui-qmt-*` | TDX 风格外壳（§3 全部组件）挂载 dsh `slot`；行情走单 WS service（引用计数订阅） |

### 4.2 页面 → ui-qmt-* 插件映射（TDX 式重分组）
| 插件包 | 承载（TDX 树节点） | 优先级 |
|---|---|---|
| `ui-qmt-shell` | MenuBar/Ticker/FunctionTree/Workbench/BottomDock/热键 | P0 |
| `ui-qmt-quote` | 报价牌、个股分析、Dashboard、参考数据 | P0 |
| `ui-qmt-broker` | 连接管理、多账户网格 | P0 |
| `ui-qmt-trade` | 手动交易、条件单、算法交易、模拟盘、信号路由 | P0 |
| `ui-qmt-backtest` | 回测对比、因子指标、研究深度 | P1 |
| `ui-qmt-strategy` | 策略模板、策略市场 | P1 |
| `ui-qmt-ops` | 审计、系统状态、对账、告警、通知、Webhook、设置 | P1 |
| `ui-qmt-agent` | Agent 会话（dsh 原生会话 + 审批中心） | P0 |

### 4.3 Agent 如何"调用我们的各种功能"
- **工具即功能**：后端 `/mcp` 已注册 14 组工具（行情 `get_quote/get_kline/get_tick`、交易 `place_order/cancel_order`、账户 `monitor_account`、策略/回测/分析/对比等）。dsh `mcp` 包连接后，DeepSeek 等 LLM 经标准 `tools/call` 直接驱动——**这就是"调用我们的各种功能"的契约面**，无需为 dsh 重写任何工具。
- **审批缝（交互安全）**：所有下单/撤单类 MCP 工具经 dsh `interaction` 缝走审批卡片；大额联动后端 TOTP 二次确认；审计链记录 LLM 决策摘要（继承原方案 Phase 4）。
- **数据源透明**：Agent 返回结果携带 `source` 字段；UI 与对话均显式标注数据来自券商还是补充行情源，杜绝"AI 编造行情"。
- **多智能体决策流**（对标 TradingAgents，嫁接真实通道）：研究 Agent（行情/因子）→ 决策 Agent（目标持仓）→ 风控 Agent（规则+人工闸门）→ 经 SignalRouter 三模式执行。

### 4.4 三条 Seam（能力缝，dsh 约定）
- `BrokerSeam`（连接/能力协商，提供方 = qmt-bridge REST）
- `QuoteSeam`（订阅/快照，提供方 = 单 WS service，引用计数 + 断线补发）
- `TradeSeam`（下单/撤单/审批，提供方 = MCP 工具 + interaction 审批缝）
未来接同花顺/PTrade 或换数据源只换提供方，UI 插件不动——同时落实"多券商从承诺变现实"。

---

## 五、后端依赖（最小改动）

| 接口 | 必要性 | 说明 |
|---|---|---|
| `POST /market/quotes` `{codes:[]}` | **必需（报价牌/排名命脉）** | 返回 `SyncEngine.latest_quotes` 中已缓存快照（零新增网络调用）；缺失项经 `DataSourceManager.get_quote` 补齐（受 8s 超时/熔断保护）。纯读、可缓存。 |
| `GET/POST /market/stock-profile` | 推荐（个股分析页） | 聚合 基础信息 + `/reference/financial`(财务) + eltdx industry/concepts，避免前端并发 3~4 请求。 |
| `subscribe_quote(list)` | 已具备 | `xtquant_client/base.py:192`，前端直接复用驱动实时报价牌/盘口。 |
| `/market/search`、`/reference/sector-stocks`、`/market/kline` | 已具备 | 命令面板、功能树板块节点、K线直接复用。 |

> 不新增任何行情连接、不改风控/审计。批量接口只读缓存，对后端零压力。

---

## 六、分阶段实施（整合双轨）

| 阶段 | 主题 | 关键交付 | 验收 | 是否依赖 dsh |
|---|---|---|---|---|
| **Phase 0** | dsh POC 闸门 | vendor dsh；MCP 连通 `/mcp`；最小 ui 插件；Electron 加载。产出《裁剪清单+锁定 commit》 | 三项 POC 通过 → 继续；否则降级路线 A | 是（闸门） |
| **Phase 1** | 工程基线（无后悔） | pnpm monorepo + TS strict + vitest + oxlint + lefthook；OpenAPI→TS 类型化 RPC 流水线（旧 `api.js` 冻结只减不增） | lint/类型/单测全绿 | 否（路线 A/B 都做） |
| **Phase 2** | TDX 界面骨架（现有 SPA 先行） | MenuBar/Ticker/FunctionTree/Workbench/Pane/BottomDock；改写 `App.jsx`、弃用 `Hub.jsx`；旧 25 页全部经树可打开；布局持久化 | 旧功能零丢失；TDX 外壳可演示 | 否（**可独立交付**） |
| **Phase 3** | TDX 核心页 | 报价牌（排序/板块/综合排名）+ 个股分析（分时+五档+K线+F10）；新增后端 2 接口 | 点报价牌开个股；WS 实时；来源徽标 | 否 |
| **Phase 4** | 键盘流 + 命令面板 | `useHotkeys` + `CommandPalette`；F3/F5/F6/F10/Ctrl+K 全通；代码定位 | 键盘完成 90% 导航 | 否 |
| **Phase 5** | dsh 插件化 | qmt-bridge host 网关；ui-qmt-* 插件挂入 dsh slot；单 WS service 替代轮询 | 新壳跑通 券商→行情→下单 闭环；Agent 经 MCP 调通工具 | 是 |
| **Phase 6** | Agent-first | 审批流 + 会话落库(后端 sessions/messages) + 多智能体决策流演示 | 大额单走 TOTP 审批；对话可续聊 | 是 |
| **Phase 7** | 集成切换 + 桌面打包 | Electron 定稿（端口发现/更新/托盘）；构建流水改 dsh client build→static；功能对等 100% + E2E 后切默认入口；旧 SPA 留一个版本周期回退 | 性能/内存基线达标 | 是 |

> **路线 A 兜底**：若 Phase 0 不通过，跳过 Phase 5–7，Phase 1–4 直接在现有 SPA 完成 TDX 重构（投入约 3–5 周），dsh 择机再议。

---

## 七、风险与回退

| 风险 | 等级 | 缓解 |
|---|---|---|
| dsh 破坏性变更（官方明示） | 高 | 锁定 commit + vendor 副本 + 适配层隔离；仅引用 POC 裁剪后的包；兼容 CI 常态化 |
| dsh 停更/转向 | 中 | MIT 可自持；核心依赖实质只有 Cordis；UI 组件已在 SPA 落地，最差仅不换壳 |
| Windows 缺口（sandbox/PTY） | 中 | qmt_work 不需要 sandbox；PTY 用日志流替代（继承原方案 Phase 0 排查） |
| 功能回归 | 中 | 双轨运行 + 每页对等清单 + 旧 SPA 一个版本周期回退窗口 |
| 轮询回潮 | 中 | 强制 WS 事件驱动（`useSystemWS.subscribeEvent`），保留 ≥30s 兜底；CI 禁新增 `setInterval` 高频轮询（继承 P2-2） |
| 后端契约漂移 | 低 | OpenAPI 类型生成进 CI，后端改接口编译期报错 |

---

## 八、验收总览

| 阶段 | 周期 | 关键验收 |
|---|---|---|
| 0 POC | 1–2 周 | MCP 连通 + 最小插件 + Electron 加载通过，产出裁剪清单 |
| 1 工程基线 | 1 周（并行） | monorepo + TS strict + vitest + lint 钩子全绿；类型化 RPC 流水线跑通 |
| 2 TDX 骨架 | 2–3 周 | 旧 25 页零丢失挂入 TDX 外壳；布局持久化 |
| 3 TDX 核心页 | 2–3 周 | 报价牌/个股分析可用；WS 实时；来源徽标 |
| 4 键盘流 | 1 周 | 键盘+命令面板覆盖 90% 导航 |
| 5 dsh 插件化 | 2–3 周 | 新壳闭环 + Agent 经 MCP 调通工具 |
| 6 Agent-first | 2–4 周 | 审批流 + 会话落库 + 多智能体演示 |
| 7 集成切换 | 2 周 | 默认入口切新壳；性能/稳定基线达标；回退预案验证 |

总计：纯 UI 路线（A）约 3–5 周即可交付"像通达信"的可演示版本；全量插件化（含 dsh）约 12–18 周，受 Phase 0 闸门约束。

---

## 九、与既有文档关系

- 本方案 = **《基于deepseek-harness的客户端重构方案》的 UI 具体化 + 闸门保留版**：把原方案的 `ui-qmt-*` 抽象插件落地为 §3 的 TDX 规格，并补 §4.3「Agent 调用我们的功能」的契约说明。
- 继承《竞品对比》的"前端零基建"与《功能层审计》P2-2「事件化改造」作为 Phase 1/2 的硬性约束。
- 后端接口缺口（§5）为现有文档未覆盖项，是本次新增的最小改动清单。

---

## 十、实施进度（截至 2026-08-28）

> 路线 A（现有 SPA 直接做 TDX 重构）已先行落地 **Phase 2 + Phase 3 + Phase 4**，前端 `npm run lint`（`max-warnings=0`）与 `npm run build` 全绿，产物写入 `backend/static`。

### Phase 2 · TDX 界面骨架 ✅ 已完成
- 新增 `pagesRegistry.jsx`：页面树 + 扁平映射单一真相，菜单/左树/工作区共用；保留 `dashboard`/`brokers` 硬引用 key。
- `MenuBar.jsx` + `Ticker.jsx`：顶菜单 + 指数跑马灯（WS 订阅核心指数，断线重连）。
- `FunctionTree.jsx`：左侧可折叠功能树，跟随当前标签高亮。
- `Workbench.jsx`：中央多标签 MDI，标签持久化到 localStorage，监听 `nav` 事件开窗。
- `BottomDock.jsx`：可折叠底部综合栏（自选股/板块/预警/成交/日志），成交与日志经 `useServerEvents` 实时推送。
- `App.jsx` 重写装配外壳；`Hub.jsx` 已弃用（保留文件，不再引用）。
- `styles.css` 新增 TDX 外壳/报价牌/个股分析/命令面板样式。

### Phase 3 · TDX 核心页 ✅ 已完成
- 后端 `app/routes/market.py` 新增 `POST /market/quotes`：优先用 `SyncEngine.latest_quotes` 缓存（零新增网络调用），缺失代码经 `DataSourceManager.get_quote` 兜底补齐（单只 5s 超时、整体 `gather`、失败静默跳过），不伪造数据。
- `QuoteBoard.jsx`：报价牌/综合排名，板块/自选股切换、列排序、涨幅/跌幅/振幅/成交额榜快捷筛选；REST 批量快照 + WS 增量刷新。
- `StockAnalysis.jsx`：个股分析页（K线+MA+成交量、五档 `QuotePanel`、F10 概况），代码从报价牌双击带入，支持实时 tick。

### Phase 4 · 键盘流 + 命令面板 ✅ 已完成
- `hooks/useHotkeys.js`：`Ctrl/Cmd+K` 命令面板、`Alt+1…9` 跳第 N 分组首个页、`F1` 帮助层、`Esc` 关闭浮层。
- `CommandPalette.jsx`：模糊跳页；并支持**输入股票代码直达个股分析**（如 `600519` → `600519.SH`）。
- `HelpOverlay.jsx`：F1 列出快捷键与功能分组。

### 说明 / 与方案差异
- 方案 §4 原列 `F3/F5/F6/F10` 快捷键，实现中改为更通用的 `Ctrl+K` + `Alt+数字` + `F1`，覆盖等价导航能力；如需严格对齐 F-key 映射可再补。
- Phase 0/5/6/7（dsh 插件化、Agent-first、桌面打包）尚未启动，依赖 dsh POC 闸门，按路线 A 可暂不阻塞。
