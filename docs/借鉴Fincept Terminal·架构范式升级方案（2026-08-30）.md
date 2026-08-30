# qmt_work · 借鉴 Fincept Terminal 的架构范式升级方案

> 日期：2026-08-30　｜　基线：`docs/对标通达信大智慧东方财富·深度重构与优化方案（2026-08-30）`
> 代码基线：backend 33,096 行 Python（177 端点 / 28 路由模块）、frontend/src 11,658 行 JSX（30 页 / 156 个 api 封装）
> 参照对象：[FinceptTerminal](https://github.com/Fincept-Corporation/FinceptTerminal)（15.4k stars，AGPL-3.0，v4 原生桌面金融终端）

---

## 〇、结论先行

**Fincept 值得抄的不是技术栈，而是五个架构范式。** 它的 C++20/Qt6 + 嵌入式 Python 是为"Bloomberg 替代品"这个定位服务的；qmt_work 的定位是"A 股真实券商通道 + Agent 闭环"，照搬它的栈是灾难，照搬它的数据面与生命周期治理则是白捡的成熟经验。

| 判断 | 内容 |
|---|---|
| **最值得借鉴（3 项，且 qmt_work 有真实缺陷）** | ① 统一数据面 DataHub（topic 总线 + 策略表）——直击"22 个组件各自建 fetch 路径"；② 屏幕生命周期纪律（可见性即开关）——直击"keep-alive 8 个后台 Tab 的 18 个定时器常驻打后端"；③ 重量级任务运行时（统一队列 + 并发闸门 + 进度事件）——直击"回测/同步/扫描各有一套并发控制、10 处散落 Semaphore" |
| **可改造借鉴（3 项）** | ④ PythonRunner 式分析脚本契约；⑤ 分析与输出层（组合风险 / 估值 / Excel 报告）；⑥ Node Editor + MCP 可视化编排 |
| **工程纪律借鉴（2 项）** | ⑦ 设计令牌文档化 + 架构纪律进 CI；⑧ 工具链/依赖 pin |
| **明确不借鉴（4 项）** | 见 §3 —— C++/Qt 技术栈、4000+ Python 脚本堆量、100+ 数据连接器、订阅制商业化 |
| **协议红线** | Fincept 为 **AGPL-3.0-or-later**。本方案**只借鉴架构思想与规格设计，不复制任何代码**。qmt_work 是公开仓库，任何copyleft 代码混入都会污染整个仓库的授权。所有落地项均需**独立实现** |

**一句话**：Fincept 用"一个数据面 + 一套纪律"撑起了 50+ 屏；qmt_work 现在 30 屏就已经出现重复取数、后台空转、并发散落三处腐化征兆。**在继续加页面（P2 选股器、P4 图表）之前，必须先把这三层地基补上，否则新页面只会加速腐化。**

---

## 一、Fincept 架构解构（只提取可迁移的部分）

### 1.1 分层与规模

```
screens/  50+ 屏（每个屏一个子目录，register_factory 懒构造）
services/ 18 个服务域（独占 fetching / caching / processing）
datahub/  topic 总线（producer / consumer / TopicPolicy）
trading/  交易核心 + 18 家券商
mcp/      MCP 基础设施（24 个 tool 模块）
python/   嵌入式 Python 3.11.9（4000+ 脚本）
storage/  SQLite + 16 个 repository
ui/       theme / 可复用 widget / table / chart / nav
```

关键约束：**P6 —— 屏幕只负责渲染，服务独占取数/缓存/处理**。屏幕不允许自己 fetch。

### 1.2 DataHub（本次借鉴的核心）

| 机制 | 规格 |
|---|---|
| **topic 命名** | `domain:subdomain:identifier[:modifier]`，如 `market:quote:AAPL`、`news:symbol:NVDA`、`ws:kraken:ticker:BTC-USD`、`econ:fred:GDP`、`broker:*:*:positions` |
| **路由** | `pattern_index_` 按前缀长度排序，**最长前缀匹配的 producer 胜出** |
| **发布权** | 只有 topic 的注册 owner 可 `publish()` |
| **消费 API** | `subscribe()` 拿实时流，`peek()` 拿快照 —— 双通道 |
| **TopicPolicy** | `{ ttl_ms, min_interval_ms, coalesce_within_ms, priority }`（默认 TTL 30s / min_interval 5s） |
| **后台抑制** | 订阅时检查窗口 `active_for_work` 属性；后台标签页 / 最小化 → **抑制投递** |
| **纪律规则（CI 强制）** | **D1**：UI 不得直接调 PythonRunner，必须经 producer；**D4**：消费者不得直连流式 fetch API，必须 `subscribe()` / `peek()` |

**Topic 注册表（这是最值得照抄的一张表）**：

| 域 | topic 模式 | Producer | TTL | 最小间隔 |
|---|---|---|---|---|
| 行情 | `market:quote:` | MarketDataService | 5 s | 1 s |
| 走势缩略 | `market:sparkline:` | MarketDataService | 60 s | 5 s |
| WS 推送 | `ws:kraken:ticker:` | ExchangeSessionManager | Push | 50 ms |
| 新闻 | `news:general` | NewsService | 5 min | 30 s |
| 宏观 | `econ::` | EconomicsService | 1 h | 60 s |
| 券商持仓 | `broker:*:*:positions` | DataStreamManager | 5 s | 3 s |
| 海事 | `maritime:vessel:` | MaritimeService | 1 min | 30 s |
| AI | `agent:output:` | AgentService | Push | — |

> **洞察**：这张表的价值不在于"缓存"，而在于**把"什么数据多久刷一次"从散落在 50 个屏幕里的决定，收敛成一张可审计的表**。qmt_work 的等价物目前散落在 9 个函数签名上（见 §2）。

### 1.3 性能 / 架构纪律（P1–P15 的公开部分）

| 规则 | 内容 | 对 qmt_work 的意义 |
|---|---|---|
| **P1** | 永不阻塞 UI 线程 | 回测同步执行（§2.3）是反面案例 |
| **P2** | 懒构造屏幕（`register_factory()`） | qmt_work 已用 `React.lazy` 达成 ✅ |
| **P3** | 定时器必须在 `showEvent()` / `hideEvent()` 启停，**不得在构造函数里启动** | **qmt_work 完全缺失** ❌ |
| **P4** | Python 子进程统一走 PythonRunner，**最大 3 并发** | qmt_work 的 bridge 无全局闸门 ❌ |
| **P6** | 屏幕只渲染，服务独占取数/缓存/处理 | qmt_work 30 页中 22 页组件内直连 `api.*` ❌ |
| **P14** | 日志统一 `LOG_*` / `logger`，禁 `printf`/`print`/`cout` | qmt_work 残留 38 处 `print(` ❌ |

### 1.4 Python 运行时

- **双 venv 路由**：`venv-numpy1`（vectorbt / PyPortfolioOpt / QuantLib，需 NumPy 1.x）与 `venv-numpy2`（torch / sklearn / agno / langchain，需 NumPy 2.x），PythonRunner **按脚本路径路由**——用一个经典工程手法解决了金融 Python 生态最恶心的新旧依赖冲突。
- **并发闸门**：最大 3 个子进程。
- **进程契约**：JSON in（stdin）→ JSON out（stdout），行协议 `TOKEN:` / `DONE:`，支持流式增量回传（AI persona 的 token 流即基于此）。

### 1.5 其他可迁移点

| 能力 | Fincept | 迁移价值 |
|---|---|---|
| Node Editor + MCP | 拖拽节点编排自动化管线，节点可调用 MCP tool | 高（qmt_work 有 53 个 MCP tool，却无编排面） |
| Excel I/O（QXlsx）+ Report Builder + Notes | 导出/报告/笔记三件套 | 中高（qmt_work 只有 K 线 CSV 导出） |
| LanguageManager + translations | 含 `fincept_zh_TW.ts` | 低（qmt_work 单语用户群，但令牌化改造是前置） |
| DESIGN_SYSTEM.md（Obsidian） | 禁止 ad-hoc 颜色/字号/间距，一律用 token | 高（qmt_work 只有 13 个 CSS 变量） |
| 工具链严格 pin | CMake 3.27.7 / Ninja 1.11.1 / Qt 6.8.3 EXACT / Python 3.11.9，不匹配即 `FATAL_ERROR` | 中（qmt_work 依赖全 `>=` / `^`） |
| Unity build + ccache | 构建加速 | 低（不适用 JS/Python） |

> ⚠️ **一个必须说清的事实**：Fincept 的 README（main 分支）声称 v4 是 **C++20 + Dear ImGui + GLFW/OpenGL** 的纯原生重写，而 v4.1.0 标签的 `docs/CONTRIBUTING.md` 仍描述 **Qt6 Widgets + Qt Charts**。即**该项目正处在 UI 栈迁移的中途**。这恰恰说明：**不要追它的技术栈（它自己都在换），只追它稳定的架构范式**（DataHub / P1–P15 / PythonRunner 契约在三份文档里高度一致）。

---

## 二、现状取证：qmt_work 在这六个维度上的真实位置

### 2.1 数据面

| 考察项 | 现状 | 证据 |
|---|---|---|
| topic 命名空间 | **无**。topic 就是裸股票代码字符串 | `quoteHub.jsx:115` `const token = { code, id: ++_tokenSeq };` |
| TopicPolicy 对象 | **不存在**。ttl / min_interval / coalesce / priority 全无 | 全文无定义，仅 4 个全局常量（`quoteHub.jsx:25-32`） |
| 消费 API | **单通道**：`useSyncExternalStore(getSnapshot)`，无 peek/subscribe 之分 | `quoteHub.jsx:259` |
| 广播 | **每 tick 全量重建 Map + 通知全部 listener** | `quoteHub.jsx:60-68`、`38-40` |
| 消费端过滤 | O(n) 全量挑选（`pickCodes`） | `quoteHub.jsx:71-75`、`:272` |
| 后端节流 | **进程级**微批窗口 100ms（下限 20ms），非 per-topic | `sync/__init__.py:388-396`、`runtime_config.py:25` |
| TTL 分级 | **有，但散落在函数签名上**：indices 3s / boards 10s / overview 10s / constituents 15s / moneyflow 30s / board-kline 60s / etfs 300s / capital 300s / board-lookup 600s | `market.py:335,396,430,460,489,523,604,634,777` |
| 第二/三套缓存 | `eltdx_source`（client 180s / preclose 86400s / board 3600s / etf 3600s）；`kline_cache`、`market_cache`、`moneyflow_cache` 表 | `eltdx_source.py:43,46,578,579` |
| 统一数据层采用率 | **仅 8/30 页**用 `useMarket`/`useQuotes`；组件内 `api.*` 直连 **203 处** | `useMarket.js:12-22,103`；grep 统计 |
| 轮询 | **18 处 `setInterval`**，含 `Backtest.jsx:74` 的 800ms | 全量 grep |

**诊断**：不是"没有缓存"，而是**"没有数据面"**——缓存策略分散在 9 个函数参数 + 3 套独立缓存 + 22 个组件自建路径里，无法集中审计、无法按数据种类调参、无法做后台抑制。

### 2.2 屏幕生命周期（本轮最有价值的发现）

| 考察项 | 现状 | 证据 |
|---|---|---|
| 懒加载 | ✅ **已达成**，30 页全部 `React.lazy` + 工厂 `D()` + chunk 重试 | `pagesRegistry.jsx:37-45` |
| Tab 切换 | **keep-alive 隐藏，不卸载** | `Workbench.jsx:158-159`；`styles.css:793` `visibility:hidden` |
| 常驻上限 | `MAX_ALIVE = 8`、`MAX_TABS = 24` | `workspace.jsx:19,20,533` |
| **可见性联动** | ❌ **0 处**。`visibilityState` / `document.hidden` / `IntersectionObserver` 命中均为 0 | 全量 grep = 0 |
| `dimmed` 传递 | 只传给 **Tab 头**做视觉灰度，**不传给 Pane** | `Workbench.jsx:72,179` |

**这是一个真实缺陷**：keep-alive 让最多 8 个 Tab 的组件常驻，而组件内的 18 个 `setInterval` **没有任何一个感知自己是否被隐藏**。典型组合（Dashboard 3 个定时器 + Trade 30s + SystemStatus 15s + LimitUp 2×30s + MarketData 60s + Algo 30s + Audit 15s）意味着**用户切到"交易"页时，背后 7 个页面的定时器仍在持续打后端**——后端限流、行情源配额、CPU 全在为此付费。

Fincept 用两条规则解决了同一问题：P3（定时器在 show/hide 启停）+ `active_for_work`（后台抑制投递）。**这两条对 qmt_work 是零成本、高收益的直接照搬。**

### 2.3 重量级任务治理

| 设施 | 现状 | 证据 |
|---|---|---|
| Bridge 子进程 | `subprocess.Popen` + JSON 行协议 + `--parent-pid` 看护（3s 探测 / 8s 宽限）+ 30s 握手超时 + ThreadPool(4) + 有界队列 2000 | `bridge_client.py:220-224,35,120-122`；`bridge_server.py:86-92,278` |
| **全局并发闸门** | ❌ **无**。只有"同实例去重锁"，N 个券商 = N 个子进程 | `bridge_client.py:103-105` |
| 回测 | **在事件循环内同步执行**（因依赖主进程的券商连接），`Semaphore(2)`，有 JobQueue + 落库 + 取消 | `backtest/__init__.py:1-7,21-26,43-48,63-72` |
| 回测前端 | **800ms 轮询**（已自停，但仍是最刺眼的一处） | `Backtest.jsx:74-83` |
| 并发设施总数 | **10 处散落**：market 5 个（8/6/8/10/6）、account 4、eltdx BoundedSemaphore、backtest 2、gateway 4+1、bridge 4 | `market.py:20,331,521,629,718`；`account.py:351`；`eltdx_source.py:191`；`backtest/__init__.py:26`；`gateway.py:73-81` |

**诊断**：每处并发控制都是"当时那个功能的局部最优"，没有统一的"这台机器同时能干什么"的全局视图。**P0 的全市场同步（5400 只）、P2 的选股扫描一旦上线，会与回测、桥接争抢同一批资源，届时必然要靠打补丁限流。**

### 2.4 MCP / 编排

| 项 | qmt_work | Fincept |
|---|---|---|
| MCP tool | **53 个 / 15 模块**（market 6、trading 7、research 6、reference 5、limitup 5、algo 5、backtest 3、condition 3、target_portfolio 3、broker 3、analysis 4、account 2、strategy 2、rebalance 1、position 1） | 24 个 tool 模块 |
| 编排面 | ❌ **无**（grep `node.editor\|workflow\|pipeline\|dag\|reactflow` = 0） | Node Editor，节点可调用 MCP tool |

**结论**：**工具面上 qmt_work 领先**（53 vs 24，且覆盖真实下单/算法单/条件单/目标持仓，Fincept 只有回测与纸交易）。**缺的是编排面**——53 个工具只能靠 LLM 单次调用或人肉点击，无法串成可复用的流水线。

### 2.5 分析与输出

| 能力 | 现状 | 证据 |
|---|---|---|
| 回测指标 | 13 项（total_return / annual_return / annual_volatility / sharpe / max_drawdown / calmar / win_rate / trade_count / avg_pnl / **var95** / rating / annualization / period；有基准时 +beta/alpha/IR） | `metrics.py:139-166` |
| 缺失指标 | ❌ **sortino**（0 命中）；VaR 仅 95% 单分位历史法 | `metrics.py:104` |
| 因子库 | 15 个（sma/ema/rsi/bollinger/macd/atr/adx/cci/kdj/obv/volume_ma/returns/log_returns/zscore/roc） | `factors.py:247-266` |
| 内建策略 | 仅 3 个（ma_cross / macd / rsi） | `backtest.py:57` |
| 参数寻优 | ✅ `run_param_sweep` + walk-forward | `factor_research.py:359,379,421,429` |
| 组合风险 / 估值 | ❌ DCF、有效前沿、均值方差、Black-Litterman **全为 0 命中** | grep |
| Excel / 报告 | ❌ 无 xlsx；仅 K 线 CSV/JSON/feather 导出；无 Report Builder | `market.py:1004-1059` |

### 2.6 工程纪律

| 项 | 现状 | 证据 |
|---|---|---|
| 设计令牌 | **13 个 CSS 变量，单个 `:root`，无主题切换**；无 DESIGN_SYSTEM.md | `styles.css:2-14` |
| 统一错误返回 | ✅ `ok()` / `err()`，28 个路由模块中 27 个使用；前端 `api.js:64` 统一解包 | `_common.py:16-23` |
| 日志 | 44 处 `logging.getLogger`（`qmt_work.*` 命名空间）+ **38 处残留 `print(`** | `logging_setup.py`；grep |
| i18n | ❌ 无（中文硬编码，`toLocaleString("zh-CN")` 是唯一"本地化"） | `AccountsGrid.jsx:17` |
| CI | 3 个 workflow（ci / build-client / release），跑 ruff + pytest + eslint + vitest + build + 文档数字核对 | `.github/workflows/` |
| **架构纪律门禁** | ❌ **无**。ruff 仅 `select = ["F","E9"]` / `ignore=["F401"]`；eslint 仅 3 条 bug 规则 | `ruff.toml:18-19`；`eslint.config.js:1-6` |
| 依赖 pin | ❌ `requirements.txt` 全 `>=`（含 `fastmcp>=1.5,<3` 这种宽区间），`package.json` 全 `^` | 两文件 |

---

## 三、明确不借鉴清单（防止盲目对标）

| Fincept 做法 | 不借鉴的理由 |
|---|---|
| **C++20 + Qt6 / Dear ImGui 原生栈** | qmt_work 的价值在"真实券商通道 + Agent + 快速迭代"，Electron 壳 + React 的迭代速度是资产不是负债；且 Fincept 自己正在 ImGui/Qt 之间迁移。重写为 C++ 会丢掉 12.7k 行前端与全部交付节奏 |
| **4000+ 个 Python 脚本的堆量模式** | 脚本间无统一契约、无测试、无版本治理，是典型的"科研代码 landfill"。qmt_work 应当**先定契约再长脚本**（见 F4），宁可 20 个有契约的模块，不要 4000 个野生脚本 |
| **100+ 数据连接器的广度竞赛** | qmt_work 的市场是中国 A 股，数据源收敛在 eltdx + 券商 xtquant + 少量补充源即可。**连接器数量 != 数据质量**，广度竞赛对单一市场产品是负收益 |
| **订阅制商业化 / Guest 24h 会话** | 与 qmt_work（本地部署、自有券商账号）的商业模式无关 |
| **海事追踪 / 地缘政治 / Polymarket** | 宏观另类数据，与 A 股个股量化交易的主线无关 |
| **AGPL-3.0 代码** | 协议红线，见 §8 |

---

## 四、升级方案 F1–F8

> **与既有 P0–P6 的关系**：P 系列是**纵向能力项**（数据仓 / 指标 / 选股 / AI / 图表），F 系列是**横向架构层**。二者不是替代关系——**F1/F2/F3 是 P0/P2 的前置地基，F4 是 P1 的宿主**。若跳过 F 直接做 P，新页面会复用旧的混乱数据路径，加速腐化。

### F1 · 统一数据面（DataPlane）— **借鉴 DataHub，最高优先级**

**目标**：把"谁去取数、多久刷一次、谁被通知"从散落在 22 个组件 + 9 个函数签名里的决定，收敛成**一张可审计的策略表 + 一个总线**。

| 步骤 | 落点 | 内容 |
|---|---|---|
| F1-1 | 新增 `frontend/src/lib/dataPlane.js` | topic 总线。命名 `域:子域:标识`，如 `market:quote:600519.SH`、`market:kline:600519.SH:1d`、`board:list:industry`、`account:positions:*`、`backtest:job:<id>`、`system:status`。API 双通道：`subscribe(topics, cb)`（实时流）/ `peek(topic)`（快照，不订阅） |
| F1-2 | 新增 `frontend/src/lib/topicPolicy.js` | **集中策略表**（照抄 Fincept 的注册表范式）：<br>`market:quote` TTL 5s / 最小间隔 1s / 合并窗口 100ms<br>`market:kline` TTL 60s / 最小间隔 5s<br>`board:list` TTL 10s / 最小间隔 3s<br>`board:constituents` TTL 15s / 最小间隔 5s<br>`board:moneyflow` TTL 30s / 最小间隔 10s<br>`etf:list` TTL 300s / 60s<br>`account:positions` TTL 5s / 3s<br>`system:status` TTL 15s / 10s |
| F1-3 | 改造 `quoteHub.jsx` | 从"行情专用 WS"升级为 DataPlane 的**行情 producer**：① `pushBatch` 改为**按 topic 增量写**（不再整体重建 Map，`quoteHub.jsx:60-68`）；② `notify()` 改为**按 topic 索引投递**（现在无差别通知全部 listener，`:38-40`）；③ 消费者端 `pickCodes` 的 O(n) 全量挑选（`:71-75,272`）改为按 topic 直接取 |
| F1-4 | 新增 producer 层 | 为非行情域补 producer：`restProducer`（REST 轮询，受策略表节流，替代现有 18 处 `setInterval`）、`wsProducer`（业务事件，接 `useSystemWS`）、`localProducer`（本地/缓存数据） |
| F1-5 | 组件收敛 | 30 页中 22 页的组件内 `api.*` 直连（203 处）改为 `useTopic(...)`。**分批迁移，先迁 8 个高频页**，每页迁移后单测 + 冒烟 |
| F1-6 | 后端对齐 | 现有 9 处散落的 `ttl` 函数参数（`market.py:335,396,430,460,489,523,604,634,777`）抽为**集中 TTL 策略表**，与前端 `topicPolicy.js` 同源（可由后端下发 `GET /meta/topic-policy`，前端启动时拉取，避免前后端漂移） |

**关键设计决策**：策略表**由后端下发**而非前后端各写一份。理由：① 单一真相源，杜绝漂移；② 后端可根据行情源配额动态调参（限流时自动放宽最小间隔），前端零改动。

**验收**：① 打任一页面，Network 面板同一 topic 在最小间隔内的重复请求数 = 0；② 3 个页面同时订阅 `market:quote:600519.SH`，后端只收到 1 条订阅；③ `topicPolicy.js` 可被 `GET /meta/topic-policy` 覆盖；④ 后台 Tab 的 topic 投递被抑制（依赖 F2）。

**风险**：22 页迁移是最大工作量，需分批且每批冒烟。建议按"高频 → 低频"排序，允许低频页暂时保留直连。

---

### F2 · 屏幕生命周期治理 — **借鉴 P3 + `active_for_work`，投入产出比最高**

**目标**：消灭"最多 8 个后台 Tab 的 18 个定时器常驻打后端"。

| 步骤 | 落点 | 内容 |
|---|---|---|
| F2-1 | 新增 `frontend/src/hooks/useActive.js` | 页面活跃态 hook。数据源二选一或组合：`IntersectionObserver`（最准，能感知分屏内 Pane 是否真可见）+ `document.visibilityState`（感知窗口最小化）。返回 `isActive` |
| F2-2 | `Workbench.jsx` 传递活跃态 | 现状 `dimmed` 只给 Tab 头（`:72,179`）。改为通过 Context 把 `isActive` 注入 Pane（`Workbench.jsx:158-159`） |
| F2-3 | DataPlane 感知活跃态 | F1 的 `subscribe()` 接收 `{ active }` 选项：非活跃时**抑制投递**（数据照收进缓存，但不触发 re-render），重新活跃时立即 `peek()` 补一次 |
| F2-4 | 定时器登记制 | 新增 `usePolling(fn, intervalMs)`：内部自动随 `isActive` 启停。改造现有 **18 处 `setInterval`**（清单见附录 B），优先 `Backtest.jsx:74`（800ms）、`Boards.jsx:84`（400ms） |
| F2-5 | 窗口级联动 | `document.visibilityState === 'hidden'`（最小化）时，全局暂停所有非关键轮询；恢复时批量补一次 |

**验收**：① 开 8 个 Tab 后切到 1 个，静置 5 分钟，Network 面板非活跃页请求数 = 0；② 切回页面 200ms 内数据可见（走缓存 + 立即补一次，不能闪白）；③ `Backtest.jsx` 的 800ms 轮询消失（改 F3 的事件推送）。

**成本**：约 1.5 人日。**这是本方案性价比最高的一项。**

---

### F3 · 统一任务运行时（JobRuntime）— **借鉴 PythonRunner 并发闸门**

**目标**：把 10 处散落的 Semaphore 与 3 类重量任务收敛为一个**有全局视图、有队列、有进度事件**的运行时。这是 P0 全市场同步与 P2 选股扫描的**承载体**（没有它，这两个功能上线即引发资源争抢）。

| 步骤 | 落点 | 内容 |
|---|---|---|
| F3-1 | 新增 `backend/app/runtime/job_runtime.py` | 统一任务运行时：`submit(kind, payload) -> job_id`；按 **kind 分级配额**（`quote_fill` 8 / `indices` 6 / `etf_quote` 8 / `board_mf` 10 / `rotation` 6 / `broker_bridge` 4 / `backtest` 2 / `sync_full` 1 / `screen_scan` 2 / `python_script` 3）；**全局并发上限** + 优先级队列 + 等待队列长度上限（超限快速失败，返回 `429 + retry_after`） |
| F3-2 | 迁移现有 10 处 Semaphore | `market.py:20,331,521,629,718`；`account.py:351`；`eltdx_source.py:191`；`backtest/__init__.py:26`；`gateway.py:73-81`；`bridge_server.py:278` 全部改为向 JobRuntime 申请配额（保留本地 Semaphore 作为最后防线，但配额由运行时下发） |
| F3-3 | 进度事件化 | 所有长任务（同步 / 回测 / 扫描 / 因子计算）统一发 `job.progress` 事件：`{ job_id, kind, phase, done, total, message, eta }`。经 F1 的 DataPlane 投递到 topic `job:<id>` |
| F3-4 | 前端消灭长任务轮询 | `Backtest.jsx:74`（800ms）改为订阅 `job:<id>`。**这是"轮询改推送"的示范点**，验证成功后推广到同步进度、扫描进度 |
| F3-5 | 任务中心页 | 新增 `Jobs.jsx`：全部任务列表（进行中/排队/历史）、进度条、取消、日志、资源占用视图（当前各 kind 的并发数/队列长度）——**可观测性是治理的前提** |
| F3-6 | 配额可配置 | 接入 `runtime_config`（已有 `sync.batch_window` 等先例，`runtime_config.py:25`），支持运行时调参，无需重启 |

**验收**：① 同时提交 5 个回测 + 1 个全市场同步，回测并发 ≤2、同步 ≤1，其余排队且前端可见排队位置；② 超出全局上限时返回 429 而非超时；③ `Backtest.jsx` 无 800ms 轮询；④ JobRuntime 的资源占用视图与实际并发数一致。

---

### F4 · 分析脚本层 — **借鉴 PythonRunner 契约（先定契约，再长脚本）**

**目标**：为 P1 指标引擎、未来的估值/组合分析提供**统一、可治理、可被 MCP 调用**的扩展宿主，同时**避免 Fincept 4000 个野生脚本的覆辙**。

| 步骤 | 落点 | 内容 |
|---|---|---|
| F4-1 | 定契约 `backend/app/runtime/script_contract.py` | 所有分析脚本统一：JSON in（stdin 或 argv）→ JSON out（stdout），**行协议** `PROGRESS:` / `RESULT:` / `ERROR:` / `DONE:`；统一退出码语义；**强制 schema 校验**（输入输出都要声明 JSON Schema） |
| F4-2 | 依赖环境路由 | 借鉴双 venv：区分 `env-quant`（pandas/numpy/ta 系）与 `env-ml`（torch/sklearn），按脚本声明的 `requires` 字段路由。**当前 qmt_work 依赖单一 venv，暂不拆分，但契约里预留字段** |
| F4-3 | 脚本注册表 | `backend/app/runtime/scripts/registry.py`：脚本元数据（名称/描述/输入 schema/输出 schema/requires/超时/是否可被 MCP 调用）。**注册表是 MCP 工具与 Node Editor 的数据源**（F6 依赖） |
| F4-4 | 并发与超时 | 走 F3 的 JobRuntime（`python_script` 配额 3，对齐 Fincept 的 max 3）；强制超时 + 可取消 |
| F4-5 | 首批脚本 | P1 的指标引擎（19 个因子）、F5 的组合风险/估值模块，全部按契约实现。**不追求数量，追求契约一致 + 有测试** |

**验收**：① 任一脚本可独立命令行调用（echo JSON | python script.py）；② 注册表可列出全部脚本及 schema；③ 超时脚本被强制回收，不泄漏子进程；④ 脚本可经 MCP 调用（F6）。

---

### F5 · 分析与输出能力补齐 — **借鉴 QuantLib 套件 + Report Builder**

**目标**：补 A 股交易者真正会用、而当前完全缺失的分析与输出能力。**注意：只补与 A 股交易主线相关的，不做广度竞赛。**

| 步骤 | 落点 | 内容 |
|---|---|---|
| F5-1 | 回测指标补缺 | `metrics.py` 补 **Sortino**、VaR 的参数化（现仅 95% 单分位历史法，`:104`）、CVaR、胜率/盈亏比、月度收益矩阵、最大回撤区间（起止日期）。成本极低（半天），价值直接 |
| F5-2 | 内建策略扩充 | 现状仅 3 个（`backtest.py:57`）。补：布林带突破、ATR 通道、双均线 + 量能过滤、网格、日内 T+0（A 股特有）。每增 1 个成本约 0.5 天 |
| F5-3 | 组合风险分析 | 新增 `tools/portfolio_risk.py`：持仓集中度（行业/个股/市值分层）、Beta 暴露、波动率贡献分解、压力测试（指数 -5%/-10% 情景）、相关性矩阵。**基于 qmt_work 已有的真实持仓——这是 Fincept 做不到的（它只有纸交易）** |
| F5-4 | 估值模块 | 新增 `tools/valuation.py`：PE/PB/PS 历史分位、股息率、PEG、同业对比、简易 DCF（A 股适用性有限，标注局限）。**必须标注模型局限，不做黑箱输出** |
| F5-5 | Excel / 报告导出 | 新增 `tools/export.py`（openpyxl）：自选股、持仓、成交、回测结果、选股结果 → xlsx（多 sheet + 格式）。前端补"导出"按钮到 6 个列表页 |
| F5-6 | 复盘报告 | 新增"每日复盘"生成：两市概况（C4 已有 `two_city_turnover`）+ 持仓盈亏 + 当日委托/成交 + 板块轮动 + 预警触发。输出 HTML/PDF + 可导出 |

**验收**：① 回测结果页指标 ≥18 项且含 Sortino；② 组合风险页可对真实持仓出压力测试结果；③ 6 个列表页可导出 xlsx；④ 每日复盘一键生成。

---

### F6 · 可视化工作流 + MCP 编排 — **借鉴 Node Editor**

**目标**：把 53 个 MCP 工具从"只能被 LLM 单次调用或人肉点击"，升级为**可编排、可保存、可复用**的流水线。这是 qmt_work 相对 Fincept 的**反超点**（工具更多且能真实下单）。

| 步骤 | 落点 | 内容 |
|---|---|---|
| F6-1 | 工作流数据模型 | 新增 `workflows` 表（DB v16）：节点图（nodes/edges）、触发器（手动 / 定时 / 事件 / MCP）、参数、版本 |
| F6-2 | 后端执行器 | `backend/app/runtime/workflow.py`：DAG 解析 → 拓扑排序 → 按 F3 JobRuntime 配额执行 → 节点级进度事件 → 失败重试/断点续跑 |
| F6-3 | 节点库 | 节点类型：数据源（行情/K线/板块/财务）、变换（指标/过滤/排序/聚合）、分析（回测/因子/风险/估值）、动作（下单/目标持仓/预警/通知/Webhook）、控制（条件/循环/并行）。**节点实现复用 F4 脚本注册表与现有 MCP 工具** |
| F6-4 | 前端 Node Editor | 新增 `Workflow.jsx`，引入画布库（React Flow，约 +50KB gzip）。拖拽建图 + 参数面板 + 运行/调试 + 运行历史 |
| F6-5 | 打通 MCP | 工作流可发布为 MCP 工具（新增 `workflow_run` 工具），或允许 Agent 在对话中生成简单工作流。**这是 P3 AI 闭环的能力放大器** |

**关键约束（与项目铁律一致）**：**任何含"下单/改持仓"动作的节点，必须显式标注危险级别，且执行前必须经风控预检 + 二次确认**。工作流的可编排性放大了误操作风险，必须在数据模型层面就带上 `requires_confirmation` 字段。

**验收**：① 可拖出"选股 → 排序 → 取 Top10 → 生成目标持仓 → 预检 → 待确认"的完整链路并保存；② 定时触发可用；③ 危险节点执行前有强制确认；④ 工作流可作为 MCP 工具被调用。

---

### F7 · 工程纪律门禁 — **借鉴 DESIGN_SYSTEM.md + CI 纪律强制**

**目标**：把"页面数量增长即腐化"从**人治**变成**机器把关**。

| 步骤 | 落点 | 内容 |
|---|---|---|
| F7-1 | 新增 `frontend/DESIGN_SYSTEM.md` | 完整令牌体系：颜色（含涨跌/语义色，A 股红涨绿跌必须写死在令牌里）、spacing、typography、radius、shadow、z-index、motion、图表配色。**规则：禁止 ad-hoc 颜色值**（照抄 Fincept 这条） |
| F7-2 | 令牌落地 | `styles.css:2-14` 的 13 个变量扩充到 ≥60 个，按层组织（primitive / semantic / component）；消灭 `MarketData.jsx` 的 56 处硬编码 hex；顺带支持亮色主题（现 `Chart.jsx:19` 硬编码 `"dark"`） |
| F7-3 | 格式化单点 | **这是一个真实 bug**：`useMarket.js:25` 万档 `.toFixed(2)` vs `lib/format.js:5` 万档 `.toFixed(1)`，全仓 5 份金额格式化口径不一致。统一到 `lib/format.js` 并加单测 |
| F7-4 | 架构纪律 lint | 新增自定义规则（eslint-plugin-local-rules + ruff 自定义检查）：<br>① **组件内禁止裸 `api.*` 调用**（强制走 DataPlane）——CI 阻断新代码，老代码进白名单逐步清<br>② **禁止裸 `setInterval`**（强制 `usePolling`）<br>③ **后端禁止 `print(`**（强制 logger，清 38 处残留）<br>④ **单文件行数上限**（前端 400 / 后端 600），超限告警 |
| F7-5 | 依赖 pin 收紧 | `requirements.txt` 与 `package.json` 收紧为**兼容区间上界**（如 `fastmcp>=1.5,<3` → `<2.1`），并**提交 lock**（`requirements.lock.txt` via `pip-compile`；`package-lock.json` 已存在，CI 加 `npm ci` 校验）。Fincept 的极端 pin 不适用（它是 C++ ABI），但**上界必须有** |
| F7-6 | 日志治理 | 38 处 `print(` 改为 `logger`；统一 `qmt_work.*` 命名空间（已有 44 处先例）；补充关键路径的结构化日志（job_id / conn_id / trace） |

**验收**：① `DESIGN_SYSTEM.md` 存在且 CI 校验无 ad-hoc 颜色值；② 新提交的代码触碰 4 条架构规则即 CI 失败；③ 金额格式化单测覆盖全部 5 种口径；④ `print(` 残留 = 0。

---

### F8 · 可观测性与性能预算

**目标**：让"性能"从主观感受变成可测量的数字。

| 步骤 | 落点 | 内容 |
|---|---|---|
| F8-1 | 前端性能预算 | CI 加 bundle size 门禁（主 chunk < 500KB gzip）；首屏 TTI < 2s；切 Tab < 100ms（依赖 F2） |
| F8-2 | 后端指标 | 每端点 P50/P95/P99 耗时 + 错误率 + 缓存命中率，接入 `/system/status`（现已有 SystemStatus 页，补指标） |
| F8-3 | 配额与限流可观测 | 行情源调用量/配额余量/429 次数可视化——限流是 P0 全市场同步的头号风险，必须可观测 |
| F8-4 | 前端错误采集 | `window.onerror` / `unhandledrejection` → 上报后端 + 前端错误边界兜底（现无错误边界，任一组件抛错白屏整页） |

---

## 五、合并路线图（F 系列 × P 系列）

```
                    ┌──────────── F 系列（横向架构层）────────────┐
                    │                                            │
  F1 统一数据面 ────┼──► F2 生命周期治理 ──► F3 统一任务运行时    │
   (topic+策略表)   │     (可见性即开关)      (队列+配额+事件)     │
        │           │            │                  │            │
        │           │            │                  │            │
        ▼           ▼            ▼                  ▼            │
  ┌─────────────────────────────────────────────────────────┐    │
  │ P0 本地行情数据仓  ─► 依赖 F3（全量同步任务）                │    │
  │ P1 统一指标引擎    ─► 依赖 F4（脚本宿主）                    │    │
  │ P2 选股器+动态板块 ─► 依赖 P0 + F1（结果订阅） + F3（扫描任务）│    │
  │ P3 AI 选股闭环     ─► 依赖 P2 + F6（MCP 编排）               │    │
  │ P4 图表专业度      ─► 依赖 F7（设计令牌，可并行）             │    │
  └─────────────────────────────────────────────────────────┘    │
                                                                  │
  F5 分析输出 / F6 工作流编排 / F7 工程纪律 / F8 可观测 ── 全程并行 │
                    └────────────────────────────────────────────┘
```

**建议执行顺序**：

| 阶段 | 内容 | 依赖 | 工作量 | 收益 |
|---|---|---|---|---|
| **第 1 周（立即项）** | F2 生命周期治理 + F7-3 格式化单点 + P4-1 K线 dataZoom | 无 | **3.5 人日** | 后台空转归零；修掉真实展示 bug；图表体验质变 |
| **第 2–3 周** | F1 数据面（后端策略表 + 前端总线 + 8 个高频页迁移） | 无 | 8 人日 | 重复取数归零；为 P2 铺路 |
| **第 4–5 周** | F3 任务运行时 + 消灭长任务轮询 | F1 | 8 人日 | 为 P0/P2 提供承载；回测轮询消失 |
| **第 6–8 周** | P0 本地数据仓（借 F3 跑全量同步） | F3 | 15 人日 | **总闸门**，解锁全部能力 |
| 并行 | F4 脚本契约 → P1 指标引擎 | — | 12 人日 | 消灭双份指标实现 |
| 并行 | F5 分析输出（先做 F5-1/F5-3/F5-5） | — | 8 人日 | 补齐真实缺口 |
| 后续 | P2 选股器 → P3 AI 闭环 → F6 工作流 | P0/P1 | 25+ 人日 | 商业价值最高 |

---

## 六、工作量 / 收益 / 风险矩阵

| 项 | 工作量 | 收益 | 风险 | 建议 |
|---|---|---|---|---|
| F2 生命周期治理 | **1.5d** | **极高** | 低 | **立即做** |
| F7-3 格式化单点 | 0.5d | 中（修 bug） | 低 | **立即做** |
| P4-1 K线 dataZoom | 0.5d | **极高**（感知强） | 低 | **立即做** |
| F1 统一数据面 | 8d | **极高** | **中**（22 页迁移） | 第 2–3 周，分批 |
| F3 任务运行时 | 8d | **高** | 中（需梳理 10 处并发点） | 第 4–5 周 |
| F5-1 回测指标补缺 | 0.5d | 中高 | 低 | 随时插队 |
| F5-5 Excel 导出 | 2d | 中高 | 低 | 第 2 周插队 |
| F4 脚本契约 | 12d | 高 | 中（契约设计需一次做对） | 与 P1 绑定 |
| F7 工程纪律 | 6d | 高（长期复利） | 低 | 全程 |
| F6 工作流编排 | 15d | 高（差异化） | **高**（危险动作放大） | 最后做，且必须带确认闸门 |
| F8 可观测 | 4d | 中高 | 低 | 全程 |

---

## 七、本周三个立即项（合计 3.5 人日）

1. **F2 屏幕生命周期治理（1.5d）** —— `useActive.js` + Workbench 注入 + `usePolling` 改造 18 处定时器。消灭"8 个后台 Tab 空转打后端"。
2. **F7-3 格式化单点（0.5d）** —— 5 份金额格式化统一到 `lib/format.js` + 单测。这是**已确认的真实展示 bug**（`useMarket.js:25` vs `lib/format.js:5` 口径不一致）。
3. **P4-1 K线 dataZoom（0.5d）** —— `MarketData.jsx:643-767` 加 `dataZoom: [{type:"inside"},{type:"slider"}]`。几行配置，体验质变。

另建议插队 **F5-1 回测指标补缺（0.5d）**——Sortino + 回撤区间是回测页最常被问的缺失项。

---

## 八、风险与红线

| 风险 | 等级 | 应对 |
|---|---|---|
| **AGPL-3.0 协议污染** | **P0 红线** | Fincept 为 AGPL-3.0-or-later。**只借鉴架构思想与规格设计，不复制任何代码行**。所有落地项独立实现；若确需参考其某个具体算法（如指标实现），须确认该算法出处为第三方宽松许可库而非 Fincept 原创。qmt_work 为公开仓库，混入 copyleft 代码将不可逆地污染授权 |
| F1 迁移面过大导致回归 | 高 | 分批（先 8 个高频页），每批冒烟；老代码进 lint 白名单逐步清；保留 `api.*` 直连作为逃生口 |
| F3 全局配额设错导致吞吐下降 | 中 | 配额接入 `runtime_config` 可热调；上线前后对比 P95 耗时；默认配额按现有 10 处 Semaphore 的实测值设定，不拍脑袋 |
| F6 工作流放大误操作风险 | **高** | 危险节点强制 `requires_confirmation`；下单类节点必须经风控预检 + 二次确认；工作流执行全量进审计链 |
| 追 Fincept 技术栈（C++/Qt） | 中 | 已在 §3 明确排除。**它自己正在 ImGui/Qt 之间迁移**，追栈会双重踩坑 |
| 指标计算迁移后端后前端变慢 | 低 | 指标结果与 bars 同生命周期缓存（key 含 indicator 签名）；前端保留最近一次的乐观渲染 |

---

## 附录 A：关键证据索引（qmt_work）

| 结论 | 证据 |
|---|---|
| 无 topic 命名空间，裸 code | `quoteHub.jsx:115` |
| 无 TopicPolicy | `quoteHub.jsx:25-32`（仅 4 个全局常量） |
| 广播全量重建 Map + 通知全部 | `quoteHub.jsx:60-68`、`:38-40` |
| 消费端 O(n) 挑选 | `quoteHub.jsx:71-75`、`:272` |
| 后端微批窗口 100ms（非 per-topic） | `sync/__init__.py:388-396`、`runtime_config.py:25` |
| TTL 散落 9 处函数签名 | `market.py:335,396,430,460,489,523,604,634,777` |
| 第二套缓存 | `eltdx_source.py:43,46,578,579` |
| 仅 8/30 页用统一数据层；203 处直连 | `useMarket.js:12-22,103`；grep |
| 18 处 setInterval | 全量 grep（清单见附录 B） |
| 懒加载已达成 | `pagesRegistry.jsx:37-45` |
| keep-alive 隐藏不卸载 | `Workbench.jsx:158-159`、`styles.css:793` |
| 常驻上限 8 / 24 | `workspace.jsx:19,20,533` |
| **零可见性联动** | grep `visibilityState\|document.hidden\|IntersectionObserver` = 0 |
| dimmed 只给 Tab 头 | `Workbench.jsx:72,179` |
| Bridge 无全局并发闸门 | `bridge_client.py:103-105` |
| Bridge 行协议 + 看护 | `bridge_client.py:220-224,35`；`bridge_server.py:86-92,278` |
| 回测在事件循环内同步执行 | `backtest/__init__.py:1-7,21-26` |
| 回测 800ms 轮询 | `Backtest.jsx:74-83` |
| 10 处散落并发设施 | `market.py:20,331,521,629,718`；`account.py:351`；`eltdx_source.py:191`；`backtest/__init__.py:26`；`gateway.py:73-81` |
| 53 个 MCP tool / 15 模块 | `mcp_server/__init__.py:71-88`；`tools/*.py` |
| 无工作流编排 | grep `node.editor\|workflow\|pipeline\|dag` = 0 |
| 无 xlsx；仅 K 线 CSV 导出 | `market.py:1004-1059` |
| 无 DCF / 组合优化 | grep = 0 |
| 缺 Sortino；VaR 仅 95% 单分位 | `metrics.py:104,139-166` |
| 13 个 CSS 变量，单 `:root` | `styles.css:2-14` |
| 金额格式化口径不一致 | `useMarket.js:25` vs `lib/format.js:5` |
| 38 处 `print(` | grep（app/tools/xtquant_client） |
| ruff 仅 F+E9；eslint 仅 3 条 | `ruff.toml:18-19`；`eslint.config.js:1-6` |
| 依赖无上界约束 | `requirements.txt`、`package.json` |

## 附录 B：18 处 setInterval 清单（F2 改造目标）

| 文件:行 | 间隔 | 内容 |
|---|---|---|
| `hooks/useSystemWS.js:99` | 15s | WS ping 保活（保留，非业务轮询） |
| `lib/quoteHub.jsx:220` | 25s | 行情 WS ping 保活（保留） |
| `BrokerContext.jsx:31` | 15s | 券商连接状态 |
| `AccountsGrid.jsx:61` | 30s | 账户网格（auto 开关控制） |
| `Algo.jsx:19` | 30s | 算法单列表 |
| `Backtest.jsx:74` | **800ms** | 回测任务状态（**优先改事件**） |
| `Audit.jsx:23` | 15s | 审计日志 |
| `Boards.jsx:84` | **400ms** | 深链等待行到位（6s 自停） |
| `Brokers.jsx:90` | 1s | 连接中秒数（动作内自停） |
| `Dashboard.jsx:19` | 15s | runtime config |
| `Dashboard.jsx:30` | 30s | aggregate |
| `Dashboard.jsx:50` | 30s | load() |
| `LimitUp.jsx:51` | 30s | 涨停板 +  breadth |
| `LimitUp.jsx:76` | 30s | limitup status |
| `MarketData.jsx:386` | 60s | 分时数据 |
| `SystemStatus.jsx:27` | 15s | 系统状态 |
| `Trade.jsx:47` | 30s | 委托/成交/持仓 |

## 附录 C：与既有方案的关系

| 既有文档 | 关系 |
|---|---|
| `对标通达信大智慧东方财富·深度重构与优化方案（2026-08-30）` | **主文档**。本方案的 F 系列是其 P 系列的**横向架构前置**；P4-1（dataZoom）、P5-3（格式化单点）直接沿用其编号 |
| `前后端逻辑全链路梳理与优化改进计划（2026-08-29）` | A1–A4 / B1–B4 / C1–C4 / D1–D5 **已全部落地并提交**（commit `0c62d4c` + `0eb7f60`）。本方案在其基础上向架构层推进 |
| `多源行情数据源重构与优化改进计划` | F1 的后端策略表与其数据源抽象互补 |
| `竞品对比与优化改进方案` | 侧重量化框架（qlib/QUANTAXIS），与本方案的客户端/终端范式互补 |
