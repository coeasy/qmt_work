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

### 多运行时桥接（xtquant ABI 兼容，开箱即用）

迅投 xtquant 的 C 扩展按 CPython 小版本编译（cp36 ~ cp312，官方暂未发布 cp313）。
当主后端（如 Python 3.13）与券商 SDK ABI 不匹配时，平台自动经「桥接子进程」加载：

1. **自动探测**：扫描本机已安装的兼容 Python（常见安装目录 / 注册表 / `py` 启动器 /
   PATH / WorkBuddy managed 运行时 / conda / `QMT_PYTHON_DIRS` 与 `QMT_PYTHON_<MINOR>` 环境变量）。
2. **捆绑运行时（推荐，随包分发）**：下载极简嵌入式 Python 到 `backend/runtimes/cp311/`：

   ```bash
   cd backend
   python tools/fetch_runtimes.py --only cp311   # 或全部 cp38~cp312
   ```

   打包（`build_exe.py`）时 `runtimes/` 自动打进 EXE 的 `_internal/`，换机器也可用。

- 无匹配解释器时，探测接口（`/brokers/test`）会给出明确可操作的三种修复路径；
  已发现兼容运行时则自动标记 `runtime_mode: bridge` 并给出运行时来源。

### 开发模式

```bash
# 1. 后端
cd backend
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python run.py        # http://127.0.0.1:21118

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
npm run pack           # 解压版 → dist-electron/win-unpacked/qmt_work.exe
npm run dist           # NSIS 安装包 + zip（默认，覆盖普通用户）
npm run dist:portable  # 仅 zip 便携版
```

### 一键构建

```bash
./build_all.sh         # Linux / Git Bash
build_all.bat          # Windows CMD
```

一键构建依次执行：前端 build → 后端 EXE → Electron 打包，默认产出 **NSIS 安装包**（可安装到任意目录 + 桌面快捷方式）与 zip 便携版。

常用参数：

| 参数 | 说明 |
|------|------|
| `--portable` | 仅产出 zip 便携版（不做 NSIS 安装包） |
| `--desktop-only` | 跳过后端 EXE，仅打包 Electron |
| `--backend-only` | 仅打包后端 EXE |
| `--skip-frontend` | 跳过前端构建 |
| `--force` | 跳过运行中实例检测（不推荐） |

环境变量：

| 变量 | 说明 |
|------|------|
| `QMT_UPDATE_URL` | 自动更新服务器地址（默认 GitHub Releases 占位，按实际仓库设置） |
| `CSC_LINK` / `CSC_KEY_PASSWORD` | Windows 代码签名证书路径/密码（设置后自动签名，消除 SmartScreen 告警） |

> 代码签名：未配置证书时跳过签名（安装/运行时 Windows 可能提示未知发布者）；配置后 electron-builder 自动签名并生成 `latest.yml` 供自动更新使用。

---

## 运维与监控

### 健康检查（标准化探针，供外部监控 / 编排接入）

| 端点 | 用途 | 返回 |
|------|------|------|
| `GET /api/v1/live` | 存活探针（进程活着即 200，不检查依赖） | 200 + 基础信息 |
| `GET /api/v1/health` | 综合健康：DB / 引擎 / 券商连接 / 交易时段 / 标准 `checks` 汇总 | 200 |
| `GET /api/v1/ready` | 就绪探针（DB 可读 + 启动完成 + 核心引擎在跑） | 200 / 503 |
| `GET /api/v1/metrics` | Prometheus 文本格式指标（订单/行情/回测/WS/错误计数） | 200 |

所有探针带统一 `code/message/data` 包裹与 `service/version` 字段，可被 Prometheus Blackbox、Uptime Kuma、容器编排健康检查直接消费。

### 端口锁定（多实例防冲突）

后端将实际监听端口持久化到 `data/.qmt_work.port`，下次启动优先复用该端口（仍被占用才 +1），避免多实例部署时端口漂移与冲突。桌面壳通过 `QMT_PORT_FILE` 读取实际端口连接。

### 日志聚合与告警

- 本地：`logs/qmt_work.log` 按天滚动，保留 14 天，含请求链路号与敏感信息脱敏
- 结构化：`QMT_LOG_JSON=true` 时额外输出 `logs/qmt_work.jsonl`（逐行 JSON），供 Loki / ELK / 自建采集器抓取
- 告警：`QMT_LOG_ALERT_WEBHOOK` 配置后，ERROR 及以上日志异步推送 webhook（支持 `{url}|{secret}` HMAC-SHA256 签名，指数退避重试，队列洪峰保护），可对接钉钉/企业微信/自定义监控

### 自动更新

桌面壳集成 electron-updater：启动后静默检查更新，托盘菜单「检查更新」可手动触发；发现新版本提示下载，下载完成退出时自动安装。更新源由构建时 `QMT_UPDATE_URL` 指定。

---

## 使用示例

### REST API

所有端点需携带 `X-API-Key` 头：

```bash
# 账户信息
curl -H "X-API-Key: $QMT_API_KEY" http://127.0.0.1:21118/api/v1/account

# 行情查询
curl -H "X-API-Key: $QMT_API_KEY" "http://127.0.0.1:21118/api/v1/market/quote?code=000001.SZ"

# 下单
curl -X POST -H "X-API-Key: $QMT_API_KEY" -H "Content-Type: application/json" \
  http://127.0.0.1:21118/api/v1/trading/order \
  -d '{"conn_id":"xxx","code":"000001.SZ","price":10.0,"qty":100,"side":"BUY"}'

# 回测
curl -X POST -H "X-API-Key: $QMT_API_KEY" http://127.0.0.1:21118/api/v1/backtest/run
```

### MCP 配置

```
http://127.0.0.1:21118/mcp
```

（Streamable HTTP；支持 FastMCP 标准工具的 Cursor / Claude Desktop 客户端）

### 环境变量

复制 `backend/.env.example` 为 `backend/.env` 后按需修改：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `QMT_PORT` | 监听端口（被占用自动 +1） | `21118` |
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
│  ├─ gateway/          # 鉴权/限流/风控/审计/脱敏/K线缓存/metrics/日志告警
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
│  │  ├─ electron/         # 桌面壳（端口发现 + 托盘 + 开机自启 + 自动更新）
│  │  └─ electron-builder.yml
├─ build_all.sh          # 一键构建（sh，默认 NSIS 安装包）
├─ build_all.bat         # 一键构建（Windows，默认 NSIS 安装包）
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