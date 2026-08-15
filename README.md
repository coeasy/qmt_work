# qmt_work · 多券商量化平台

基于 QMT / XTQuant 等多券商客户端的量化交易平台。同一进程内提供 **可视化 Web 界面 + MCP + FastAPI REST + WebSocket 实时推送**，可打包为**独立桌面客户端（EXE）**。所有行情/交易/账户接口均通过真实券商 SDK 调用，**无 mock**；未连接券商时端点返回 503 引导，绝不返回假数据。

支持券商：国金 / 华鑫 / 银河 / 中信建投 / 兴业 / 广发（迅投系，xtquant）；同花顺 / 恒生 PTrade / 掘金（接口契约，待接 SDK）。

---

## 核心能力

| 模块 | 说明 |
|------|------|
| 可视化界面 | React + Vite + ECharts SPA，纯前端，前后端解耦，24 个页面覆盖全部后端端点 |
| MCP 接口 | FastMCP Streamable HTTP，Cursor / Claude Desktop 直连 |
| REST API | FastAPI `/api/v1/*`（账户/行情/下单/回测/再平衡/风控/配置） |
| 实时推送 | WebSocket，活跃券商只订阅一次，多客户端扇出 |
| 多账户网格 | 多券商/多账户统一看板，批量下单/撤单/重连 |
| 回测引擎 | 向量化回测 + 参数扫描（与逐根信号一致），真实 K 线 |
| 因子/指标 | 15 类指标（SMA/EMA/RSI/MACD/BOLL/ATR/ADX 等），手动/K 线双模式 |
| 模拟盘 | 虚拟撮合 + 实时真实行情 mark-to-market |
| 涨停监控 | 股票池 + 三因子触发（涨停价/时间窗/tick涨幅），可选自动买入 |
| 算法交易 | TWAP / VWAP 拆单，暂停/恢复/取消 |
| 策略模板 | ma_cross/macd/rsi/limitup 模板生成，写入 QMT 客户端 |
| 策略市场 | DB 目录 + zip/json 导入导出 |
| 定时任务 | cron/周期任务 + 可选 Redis 锁分布式调度 |
| 可观测性 | Prometheus 指标 + 链路追踪 + 运行时画像 |
| 注册表 V2 | 能力协商（negotiate）+ 热插拔券商（hotplug），纯配置化 |
| 告警规则 | 自定义条件告警 + 自动匹配事件 + 历史告警记录 |
| 出站 Webhook | HMAC-SHA256 签名 + 指数退避重试，事件自动分发 |
| 外部信号 | 信号路由（live/dry-run/paused）+ webhook 入站 + HMAC 签名校验 + 二次确认 |
| 对账核销 | 委托对账核销 + WAL 统计/归档/轮转 |
| 目标持仓 | 差量同步（dry-run/实盘），按股数/金额/比例模式 |
| 风控/审计 | 日级风控（金额/亏损/次数）+ 订单超时撤单 + 审计 hash 链 |
| LLM Agent | 可插拔 Provider（OpenAI/Anthropic 兼容），AES-256-GCM 加密密钥 |

---

## 快速开始

### 环境要求

- Windows（迅投系券商依赖 xtquant，须与 QMT 客户端同机运行）
- Python 3.12+（后端），Node.js 20+（前端）

### 开发模式

```bash
# 1. 后端
cd backend
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python run.py        # http://127.0.0.1:21117

# 2. 前端（另开终端）
cd frontend
npm install
npm run dev          # 代理 /api /ws /mcp 到后端

# 3. 添加券商
打开 Web 界面 →「券商连接」页 → 选择券商 → 填写客户端路径 + 资金账号 → 连接
```

### 冒烟测试

```bash
python tests/smoke2.py    # 22 项全链路 → PASS
```

---

## 安装（生产模式）

### 后端 EXE

```bash
cd backend
python build_exe.py
# 产出：backend/dist/qmt_work/qmt_work.exe
# 直接运行即等同 python run.py（同源托管 API + SPA + MCP）
```

### 桌面客户端

```bash
cd frontend
npm run pack          # 解压版 → dist-electron/win-unpacked/qmt_work.exe
npm run dist          # NSIS 安装包（需 electron-builder 工具链）
```

### 一键构建

```bash
./build_all.sh        # Linux / Git Bash
build_all.bat         # Windows CMD
```

一键构建依次执行：前端 build → 后端 EXE → Electron 打包。

---

## 使用示例

### REST API

所有端点需携带 `X-API-Key` 头：

```bash
# 账户信息
curl -H "X-API-Key: $QMT_API_KEY" http://127.0.0.1:21117/api/v1/account

# 行情查询
curl -H "X-API-Key: $QMT_API_KEY" "http://127.0.0.1:21117/api/v1/market/quote?code=000001.SZ"

# 下单
curl -X POST -H "X-API-Key: $QMT_API_KEY" -H "Content-Type: application/json" \
  http://127.0.0.1:21117/api/v1/trading/order \
  -d '{"conn_id":"xxx","code":"000001.SZ","price":10.0,"qty":100,"side":"BUY"}'

# 回测
curl -X POST -H "X-API-Key: $QMT_API_KEY" http://127.0.0.1:21117/api/v1/backtest/run
```

### MCP 配置

```
http://127.0.0.1:21117/mcp
```

（Streamable HTTP；支持 FastMCP 标准工具的 Cursor / Claude Desktop 客户端）

### 环境变量

复制 `backend/.env.example` 为 `backend/.env` 后按需修改：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `QMT_PORT` | 监听端口（被占用自动 +1） | `21117` |
| `QMT_API_KEY` | 鉴权密钥 | `qmt-dev-key`（生产必须修改） |
| `QMT_RISK_MAX_AMOUNT` | 单笔最大金额 | `100000` |
| `QMT_ORDER_WATCHDOG_TIMEOUT` | 订单超时自动撤单（秒） | `60` |

完整配置见 `.env.example`。

---

## 项目结构

```
qmt_work/
├─ backend/              # FastAPI 统一后端
│  ├─ run.py            # 启动入口（端口自动扫描 + 单实例锁）
│  ├─ app/              # config / db / routes / main / state
│  ├─ xtquant_client/   # BrokerAdapter / Manager / Registry V2 / 桥接
│  ├─ mcp_server/       # MCP 工具注册
│  ├─ agent/            # LLM Provider 抽象 + Agent 核心
│  ├─ sync/             # WebSocket 同步引擎
│  ├─ backtest/         # 回测作业队列（含向量化 + 参数扫描）
│  ├─ paper/            # 模拟盘引擎
│  ├─ scheduler/        # 定时任务 + 分布式调度
│  ├─ tools/            # 因子/策略/算法/涨停/条件单/参考数据
│  ├─ gateway/          # 鉴权/限流/风控/审计/脱敏/K线缓存/metrics
│  ├─ data/             # SQLite 数据库
│  ├─ static/           # 前端构建产物（同源托管）
│  ├─ dist/             # PyInstaller 产物
│  ├─ tests/            # 单测 + 冒烟测试
│  └─ build_exe.py      # EXE 打包脚本
├─ frontend/             # React + Vite + Electron（24 个页面覆盖全部后端端点）
│  ├─ src/
│  │  ├─ App.jsx         # 路由入口 + 14 个核心页面
│  │  ├─ api.js          # 统一 REST 客户端（60+ 方法）
│  │  ├─ components/     # 页面组件
│  │  │  ├─ Dashboard.jsx       # 仪表盘
│  │  │  ├─ Quote.jsx            # 实时行情
│  │  │  ├─ Trade.jsx            # 手动交易
│  │  │  ├─ LimitUp.jsx          # 涨停监控（P0）
│  │  │  ├─ Algo.jsx             # 算法交易（P0）
│  │  │  ├─ Backtest.jsx         # 回测 + 参数扫描（P1）
│  │  │  ├─ Factors.jsx          # 因子/指标（P1）
│  │  │  ├─ Paper.jsx            # 模拟盘（P1）
│  │  │  ├─ Strategies.jsx       # 策略模板库（P1）
│  │  │  ├─ StrategyMarket.jsx   # 策略市场（P1）
│  │  │  ├─ Rebalance.jsx        # 分仓再平衡（P0）
│  │  │  ├─ Reference.jsx        # 参考数据（P0）
│  │  │  ├─ Audit.jsx            # 审计日志（P1）
│  │  │  ├─ Agent.jsx            # Agent 对话（P0）
│  │  │  ├─ AccountsGrid.jsx     # 多账户网格（P0）
│  │  │  ├─ Brokers.jsx          # 券商连接（P0）
│  │  │  ├─ Scheduler.jsx        # 定时任务（P2）
│  │  │  ├─ Observability.jsx    # 可观测性（P2）
│  │  │  ├─ Registry.jsx         # 注册表 V2（P2）
│  │  │  ├─ Alerts.jsx           # 告警规则
│  │  │  ├─ Webhooks.jsx         # 出站 Webhook
│  │  │  ├─ Signal.jsx           # 外部信号
│  │  │  ├─ Reconcile.jsx        # 对账核销
│  │  │  └─ TargetPortfolio.jsx  # 目标持仓
│  │  ├─ electron/         # 桌面壳（端口发现 + 托盘 + 开机自启）
│  │  └─ electron-builder.yml
├─ build_all.sh          # 一键构建（sh）
├─ build_all.bat         # 一键构建（Windows）
├─ docs/                 # 方案设计 / 多语言接入
└─ README.md
```

---

## 贡献指南

### 开发流程

1. Fork 本仓库，从 `main` 分支创建功能分支
2. 后端：`python -m venv .venv && pip install -r requirements.txt`
3. 前端：`npm install`
4. 开发完成后运行测试：`pytest`（后端）+ `npm run build`（前端）
5. 提交 PR，标题格式：`[类型] 简短描述`（如 `[feat] 新增条件单功能`）

### 新增券商

1. 在 `backend/xtquant_client/registry.py` 添加 `BrokerProfile`
2. 实现对应 `BrokerAdapter`（继承 `xtquant_client/base.py` 基类）
3. 前端「券商连接」页自动列出，无需改组件

### 编码规范

- Python：遵循 PEP 8，类型标注，`pytest` 测试
- JavaScript：ES Modules，React 函数组件 + Hooks
- 提交前运行 `pytest` 和 `npm run build` 确保无破坏

### 目录约定

| 位置 | 内容 |
|------|------|
| `backend/app/routes/` | REST 路由 |
| `backend/tools/` | 可复用业务逻辑（因子/策略/算法等） |
| `backend/gateway/` | 横切关注点（鉴权/风控/脱敏/缓存等） |
| `backend/xtquant_client/` | 券商连接抽象与实现 |
| `frontend/src/components/` | 页面级与可复用 UI 组件 |

---

## 已知限制

- **Windows 专用**：迅投系券商依赖 xtquant（Windows 二进制），须与 QMT 客户端同机运行
- **真实券商必需**：未连接券商时平台返回 503，不提供假数据
- **同花顺/PTrade/掘金**：适配器接口契约已实现，待对应 SDK 接入
- **桌面壳需桌面会话**：Electron 在无图形界面会话中无法启动

---

## License

Apache 2.0