# qmt_work · 多券商量化 Agent 平台

基于 QMT / XTQuant 等多券商客户端的量化交易平台。同一进程内提供 **可视化 Web 界面 + MCP + FastAPI REST + WebSocket 实时推送**，可打包为**独立桌面客户端（EXE）**。

所有行情 / 交易 / 账户接口均通过真实券商 SDK 调用，**零 mock**：未连接券商时端点返回 HTTP 503 + 可操作引导，绝不返回假数据、绝不用空列表冒充。

- 版本：`0.1.0`（前端包 `0.1.0-beta.1`）
- 许可证：Apache 2.0
- 平台：Windows 10 / 11（迅投系券商依赖 xtquant 的 Windows 二进制，须与券商客户端**同机**运行）

**支持券商（10 个档案）**

| 类型 | 券商 |
|------|------|
| 迅投系（xtquant 实调，7 家） | 国金 / 华鑫 / 银河 / 中信建投 / 兴业 / 广发 / 恒生 UF 定制版 |
| 接口契约（3 家，待对应 SDK 接入） | 同花顺量化 / 恒生 PTrade / 掘金量化 |

---

## 目录

- [核心能力](#核心能力)
- [界面导航（4 分组 / 17 页）](#界面导航4-分组--17-页)
- [环境要求](#环境要求)
- [安装](#安装)
  - [方式一：下载安装包（推荐普通用户）](#方式一下载安装包推荐普通用户)
  - [方式二：源码开发环境](#方式二源码开发环境)
  - [方式三：从源码构建安装包](#方式三从源码构建安装包)
  - [xtquant 多运行时桥接（ABI 兼容）](#xtquant-多运行时桥接abi-兼容)
- [快速开始](#快速开始)
- [使用示例](#使用示例)
- [配置](#配置)
- [测试与验证](#测试与验证)
- [运维与监控](#运维与监控)
- [项目结构](#项目结构)
- [贡献指南](#贡献指南)
- [常见问题（FAQ）](#常见问题faq)
- [已知限制](#已知限制)

---

## 核心能力

| 模块 | 说明 |
|------|------|
| 可视化界面 | React + Vite + ECharts SPA，纯前端、前后端解耦；4 分组 / 17 页 + keep-alive 多标签工作区 |
| MCP 接口 | FastMCP Streamable HTTP，Cursor / Claude Desktop 直连 |
| REST API | FastAPI `/api/v1/*`，34 个路由模块（账户 / 行情 / 下单 / 回测 / 因子 / 再平衡 / 风控 / 配置…） |
| 实时推送 | WebSocket，活跃券商只订阅一次，多客户端扇出；断线重连补发最近 30s 行情 |
| 多账户网格 | 多券商 / 多账户统一看板，批量下单 / 撤单 / 重连 |
| 回测引擎 | 向量化回测 + 参数扫描（与逐根信号一致），真实 K 线 |
| 因子 / 指标 | SMA / EMA / RSI / MACD / BOLL / ATR / ADX 等，手动 / K 线双模式 |
| 模拟盘 | 虚拟撮合 + 实时真实行情 mark-to-market |
| 条件选股 | 公式 DSL + NL 自然语言选股 |
| 算法交易 | TWAP / VWAP 拆单，暂停 / 恢复 / 取消 |
| 策略运行 | 策略模板生成 + 策略作业运行 |
| 可观测性 | Prometheus 指标 + 统一能力自描述（`/capabilities`，纯配置化） |
| 告警 / Webhook | 自定义条件告警、出站 HMAC-SHA256 签名 Webhook（指数退避重试） |
| 外部信号 | 信号路由（live / dry-run / paused）+ webhook 入站 + HMAC 校验 + 二次确认 |
| 对账核销 | 委托对账核销 + WAL 统计 / 归档 / 轮转 |
| 目标持仓 | 差量同步（dry-run / 实盘），按股数 / 金额 / 比例模式 |
| 风控 / 审计 | 日级风控（金额 / 亏损 / 次数）+ 订单超时自动撤单 + 审计 hash 链 |
| 数据面 | 统一数据源链（QMT → eltdx → baostock → akshare，可显式指定 / 兜底） |

---

## 界面导航（4 分组 / 17 页）

页面注册表 `frontend/src/pagesRegistry.jsx` 是菜单、功能树、命令面板的**单一真相来源**；当前 4 分组 17 页：

| 分组 | 页面 |
|------|------|
| 行情（6） | 报价牌 · 行情分析 · 市场结构 · 板块雷达 · 资金流 · 成交明细 |
| 研究（2） | 条件选股 · 因子研究 |
| 交易（3） | 手动交易 · 算法交易 · 自选股 |
| 系统（6） | 仪表盘 · 连接管理 · 多账户网格 · 系统状态 · 系统日志 · 设置 |

工作区约束（`frontend/src/store/workspace.jsx`）：最多 24 个标签（`MAX_TABS`）、keep-alive 存活上限 8 个（`MAX_ALIVE`）、仅「行情分析」为多实例页（`MULTI_INSTANCE_PAGES`），其余为单例页。旧页面 key（回测 / 策略市场 / 模拟盘 / 信号 / 审计 / 目标持仓等）通过 `KEY_ALIAS` 统一归一到现有关键页，不会导致空白页。

---

## 环境要求

| 用途 | 要求 |
|------|------|
| 运行平台 | Windows 10 / 11（迅投系券商依赖 xtquant） |
| Python（后端开发 / 构建） | 3.11（`backend/pyproject.toml` 约束 `>=3.11,<3.12`；CI 固定 3.11） |
| Node.js（前端开发 / 构建） | 20+（工具链锁定 `22.22.2`） |
| 券商客户端 | 对应券商的 MiniQMT / QMT 客户端已安装并登录 |

> 工具链版本以 `TOOLCHAIN.json` 为 CI 与制品校验的单一读取点：`node 22.22.2` / `python 3.11.9` / `pyinstaller 6.22.0` / `electron ^31`。`PyInstaller` / `Electron` 的实际版本以 `backend/requirements.txt` 与 `frontend/package.json` 为准。

---

## 安装

### 方式一：下载安装包（推荐普通用户）

1. 前往 **GitHub Releases** 页面，下载最新版 `qmt_work-<version>-setup.exe`（NSIS 安装包）或 `-portable.zip`（便携版）。
2. 运行安装包，可自选安装目录（安装时自动创建桌面快捷方式）。
3. 启动 `qmt_work.exe`，桌面壳会自动拉起内置后端（无需另装 Python）。
4. 首次运行在 Web 界面「连接管理」页添加券商客户端路径与资金账号。

> 便携版解压到任意目录后直接运行 `qmt_work.exe` 即可，配置与数据保存在 exe 同目录的 `data/`、`logs/`。
>
> 未配置代码签名证书时，Windows 可能提示「未知发布者」，属正常现象。

### 方式二：源码开发环境

```bash
# 0. 克隆
git clone <本仓库地址> qmt_work && cd qmt_work

# 1. 后端
cd backend
python3.11 -m venv .venv
.venv\Scripts\activate            # Windows PowerShell / CMD
pip install -r requirements.txt   # 可选数据源依赖见 requirements-optional.txt
python run.py                     # http://127.0.0.1:21118

# 2. 前端（另开终端）
cd frontend
npm install
npm run dev                       # http://127.0.0.1:5173，代理 /api /ws /mcp → 21118

# 3. 依赖 xtquant 时，准备桥接运行时（见下节）
cd backend
python tools/fetch_runtimes.py --only cp311
```

开发模式默认端口 `21118`，被占用时自动平滑 `+1`（最多 10 次），实际端口写入 `data/.qmt_work.port`。

### 方式三：从源码构建安装包

**一键构建（推荐）**

```bash
bash build_all.sh                # Linux / Git Bash / macOS
build_all.bat                    # Windows CMD
```

依次执行 **Step 1 前端 build → Step 2 后端 EXE → Step 3 Electron 打包 → Step 4 构建后自检**，默认产出 zip 便携版。脚本内置三道闸门，任一失败即中断而不是产出半包：

1. `verify_static_ready` —— 前端产物必须存在，且 `index.html` 引用的每个资源都真实落地（历史事故：vite 半写入就打包 → 包内 6/32 个静态文件 → 桌面端白屏）
2. 打包后 static 核对 —— 源 ↔ 包内文件数比对，多出即列出**孤儿文件**（上一轮残留）
3. 包内入口核对 —— `index.html` 引用的 js 入口必须在包内，否则明确告警「桌面端可能白屏」

常用参数：

| 参数 | 说明 |
|------|------|
| `--clean-dist` | 打包前彻底删除上一轮 `backend/dist` 产物（默认保留，PyInstaller 覆盖同名文件） |
| `--nsis` | 同时产出 NSIS 安装包（需本机安装 NSIS，否则退回 zip） |
| `--portable` | 仅 zip 便携版（默认） |
| `--desktop-only` | 跳过前端与后端 EXE，仅打包 Electron（需 `backend/dist` 已存在） |
| `--backend-only` | 仅打包后端 EXE |
| `--skip-frontend` | 跳过前端构建（需 `backend/static` 已存在） |
| `--no-verify` | 跳过 Step 4 构建后自检 |
| `--force` | 跳过运行中实例检测（不推荐） |

构建环境变量：

| 变量 | 说明 |
|------|------|
| `QMT_UPDATE_URL` | 自动更新服务器地址（默认 GitHub Releases 占位，**请按实际仓库设置**） |
| `QMT_PYTHON` | 指定后端构建解释器（默认依次探测：WorkBuddy 托管 venv → `backend/.venv` → PATH） |
| `QMT_NODE` / `QMT_NODE_DIR` | 指定前端构建解释器 / Node 安装目录 |
| `CSC_LINK` / `CSC_KEY_PASSWORD` | Windows 代码签名证书路径 / 密码（设置后自动签名，消除 SmartScreen 告警并生成 `latest.yml`） |

> **解释器版本提示**：脚本会校验 Python 版本，若非 3.11 会告警并给出改用方式（本地功能可用，但打包出的 EXE 运行时与 CI 校验矩阵不一致）。建议：`python3.11 -m venv backend/.venv` 后构建，或 `QMT_PYTHON=<你的3.11解释器> bash build_all.sh`。

**手动分步构建**

```bash
cd backend
python build_exe.py              # 后端 EXE → backend/dist/qmt_work/qmt_work.exe
                                # （与 python run.py 同源，托管 API + SPA + MCP）

cd frontend
node node_modules/electron-builder/cli.js --win zip       # zip 便携版
node node_modules/electron-builder/cli.js --win nsis zip  # NSIS + zip
```

> 建议直接调用 `electron-builder`，不要用 `npm run dist` / `dist:portable`：那两条 script 会串跑 `npm run build` 与 `backend:build`，重复一次前端构建 + 一次 PyInstaller。

**（可选）Git 钩子自动构建**

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install-git-hooks.ps1
# 之后每次 commit 自动后台构建客户端；临时跳过：QMT_NO_AUTOBUILD=1 git commit ...
```

### xtquant 多运行时桥接（ABI 兼容）

迅投 xtquant 的 C 扩展按 CPython 小版本编译（cp36 ~ cp312，官方暂未发布 cp313）。当主后端（如 Python 3.13）与券商 SDK ABI 不匹配时，平台自动经「桥接子进程」加载：

1. **自动探测**：扫描本机已安装的兼容 Python（常见安装目录 / 注册表 / `py` 启动器 / PATH / WorkBuddy managed 运行时 / conda / `QMT_PYTHON_DIRS` 与 `QMT_PYTHON_<MINOR>` 环境变量）。
2. **捆绑运行时（推荐，随包分发）**：下载极简嵌入式 Python 到 `backend/runtimes/cp311/`：

   ```bash
   cd backend
   python tools/fetch_runtimes.py --only cp311   # 或全部 cp38~cp312
   ```

   打包时 `runtimes/` 自动打进 EXE 的 `_internal/`，换机器也可用。

- 无匹配解释器时，探测接口（`/brokers/test`）会给出明确可操作的三种修复路径；已发现兼容运行时则自动标记 `runtime_mode: bridge` 并返回运行时来源。

---

## 快速开始

```bash
# 1. 启动后端（源码方式）
cd backend && python run.py          # http://127.0.0.1:21118

# 2. 打开界面
#    浏览器访问 http://127.0.0.1:21118  （后端同源托管前端构建产物）
#    或使用桌面客户端 qmt_work.exe

# 3. 添加券商
#    Web 界面 →「连接管理」页 → 选择券商 → 填写客户端路径 + 资金账号 → 连接

# 4. 验证全链路
python backend/tests/smoke2.py                             # 后端 REST/WS 冒烟
python scripts/client_start_test.py --target client        # 桌面客户端端到端
```

后端启动后即可访问：

| 入口 | 地址 |
|------|------|
| Web 界面 | `http://127.0.0.1:21118/` |
| REST API | `http://127.0.0.1:21118/api/v1/*` |
| MCP（Streamable HTTP） | `http://127.0.0.1:21118/mcp` |
| WebSocket | `ws://127.0.0.1:21118/api/v1/ws` |

> 前端构建产物输出到 `backend/static/`，由 FastAPI 同源托管；开发态 Vite 跑在 `5173` 并反向代理到后端。

---

## 使用示例

### REST API

所有端点需携带 `X-API-Key` 头（由 `QMT_API_KEY` 配置，默认 `qmt-dev-key`，**生产务必修改**）：

```bash
export QMT_API_KEY=qmt-dev-key
BASE=http://127.0.0.1:21118/api/v1

# 健康检查（三个探针语义不同，见「运维与监控」）
curl -s $BASE/live        # 存活探针：进程活着即 200
curl -s $BASE/health      # 综合健康：DB / 引擎 / 券商 / 交易时段
curl -s $BASE/ready       # 就绪探针：DB 可读 + 启动完成（未就绪 503）

# 列出内置券商档案
curl -s -H "X-API-Key: $QMT_API_KEY" $BASE/brokers/profiles

# 账户状态 / 多账户聚合
curl -s -H "X-API-Key: $QMT_API_KEY" $BASE/account/status
curl -s -H "X-API-Key: $QMT_API_KEY" $BASE/account/aggregate

# 行情查询（source=auto 时按数据源链自动降级）
curl -s -H "X-API-Key: $QMT_API_KEY" "$BASE/market/quote?code=000001.SZ"

# 标的检索 / 解析
curl -s -H "X-API-Key: $QMT_API_KEY" "$BASE/market/search?q=平安"
curl -s -H "X-API-Key: $QMT_API_KEY" "$BASE/market/resolve?q=600519"

# 历史 K 线
curl -s -H "X-API-Key: $QMT_API_KEY" \
  "$BASE/market/kline?code=000001.SZ&period=1d&count=100"

# 下单（conn_id 来自「连接管理」页或 /brokers 列表）
curl -s -X POST -H "X-API-Key: $QMT_API_KEY" -H "Content-Type: application/json" \
  $BASE/trade/order \
  -d '{"conn_id":"xxx","code":"000001.SZ","price":10.0,"qty":100,"side":"BUY"}'

# 回测（提交作业，随后轮询 /backtest/jobs/{job_id} 取结果）
curl -s -X POST -H "X-API-Key: $QMT_API_KEY" -H "Content-Type: application/json" \
  $BASE/backtest/jobs \
  -d '{"code":"000001.SZ","strategy":"ma_cross","params":{"fast":5,"slow":20}}'

# 参数扫描
curl -s -X POST -H "X-API-Key: $QMT_API_KEY" -H "Content-Type: application/json" \
  $BASE/backtest/sweep \
  -d '{"code":"000001.SZ","strategy":"ma_cross","grid":{"fast":[5,10],"slow":[20,30]}}'

# 能力自描述（查看全部可用端点）
curl -s -H "X-API-Key: $QMT_API_KEY" $BASE/capabilities
```

> **零 mock 约定**：未连接券商时，行情 / 交易 / 账户等端点返回 **HTTP 503** + 可操作引导；券商可用但被拒（风控 / 柜台拒单 / 非交易时段）返回 **HTTP 400** + 真实原因。**绝不把失败包成 `code=0`** —— 否则拒单会被前端显示成「已报」= 假成功。

### MCP 接入（Cursor / Claude Desktop）

```json
{
  "mcpServers": {
    "qmt_work": {
      "url": "http://127.0.0.1:21118/mcp",
      "headers": { "X-API-Key": "qmt-dev-key" }
    }
  }
}
```

> 支持 FastMCP 标准工具的 Streamable HTTP 客户端。MCP 工具集与 REST `agent-visible` 端点保持同步，CI 有能力漂移门禁校验（`backend/scripts/check_capability_drift.py`）。

### WebSocket 实时推送

```javascript
// 连接时需携带鉴权 token（与 REST 的 X-API-Key 一致）
const ws = new WebSocket('ws://127.0.0.1:21118/api/v1/ws?token=qmt-dev-key');
ws.onopen = () => {
  // 服务端先推一帧全量快照，再补发订阅代码最近 30s 行情（断线重连缺口）
  ws.send(JSON.stringify({ action: 'subscribe', codes: ['000001.SZ', '600519.SH'] }));
};
ws.onmessage = (e) => console.log(JSON.parse(e.data));  // 快照 / tick / 订单事件
// 取消订阅：{ action: 'unsubscribe', codes: [...] }
// 心跳：{ action: 'ping' } → 收到 { type: 'pong', seq: N }
```

### 常用 Python 片段（模拟盘）

```python
import httpx

BASE = "http://127.0.0.1:21118/api/v1"
HEAD = {"X-API-Key": "qmt-dev-key"}

# 模拟盘下单（虚拟撮合 + 实时真实行情 mark-to-market）
r = httpx.post(f"{BASE}/paper/order", headers=HEAD, json={
    "code": "600519.SH", "side": "BUY", "price": 1700.0, "volume": 100,
})
print(r.json())

# 查看模拟盘资金 / 持仓 / 成交 / 指标
print(httpx.get(f"{BASE}/paper/account", headers=HEAD).json())
print(httpx.get(f"{BASE}/paper/positions", headers=HEAD).json())
```

更多专题文档见 [`docs/README.md`](docs/README.md)（完整索引，分「当前有效使用指南 / 项目方案与重构记录 / 归档」三档）。当前有效指南包括：

| 文档 | 内容 |
|------|------|
| `docs/BROKER_ONBOARDING.md` | 券商接入指南（新增券商 / 适配器约定） |
| `docs/G2_公式DSL参考.md` | 公式选股 DSL 语法 |
| `docs/G4_统一数据面使用指南.md` | 统一数据面 / 多数据源 |
| `docs/G6_任务运行时使用指南.md` | 定时任务与作业运行时 |
| `docs/G8_NL选股使用指南.md` | 自然语言选股 |
| `docs/DESIGN_SYSTEM.md` | 前端设计系统 |
| `docs/多语言接入指南.md` | 多语言 / 多客户端接入 |
| `docs/QMT量化Agent平台方案.md` | 平台总体方案 |
| `docs/THIRD_PARTY_LICENSES.md` | 第三方依赖许可说明 |

---

## 配置

复制 `backend/.env.example` 为 `backend/.env` 后按需修改。

**优先级**：环境变量（`QMT_*`）> exe 同目录 `qmt_work_config.json` > `.env` > 内置默认值。

常用项：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `QMT_HOST` | 监听地址 | `0.0.0.0` |
| `QMT_PORT` | 监听端口（被占用自动 +1，最多 10 次） | `21118` |
| `QMT_API_KEY` | 鉴权密钥（**生产必须修改**） | `qmt-dev-key` |
| `QMT_BROKER_ID` | 启动时引导连接的券商档案 id（如 `guojin`） | 空 |
| `QMT_CLIENT_PATH` | 券商客户端 `userdata_mini` 目录 | 空 |
| `QMT_ACCOUNT_ID` | 资金账号 | 空 |
| `QMT_ACCOUNT_TYPE` | `STOCK` / `CREDIT` / `OPTION` / `FUTURES` | `STOCK` |
| `QMT_RISK_MAX_AMOUNT` | 单笔最大金额 | `100000` |
| `QMT_RISK_MAX_ORDERS_PER_MIN` | 每分钟最大下单次数 | `30` |
| `QMT_RISK_DAILY_LOSS_LIMIT` | 日亏损熔断阈值（0 = 不启用） | `0` |
| `QMT_ORDER_WATCHDOG_TIMEOUT` | 订单超时自动撤单（秒） | `60` |
| `QMT_CORS_ORIGINS` | 允许跨域来源（逗号分隔，空 = 仅同源） | 空 |
| `QMT_QUOTE_BUS_BACKEND` | 行情总线后端：`memory` / `redis` | `memory` |
| `QMT_LOG_JSON` | 额外输出结构化 JSON 日志 | `false` |
| `QMT_DB_BACKUP_ENABLED` | 数据库自动备份 | `true` |
| `QMT_DB_BACKUP_KEEP` | 备份保留份数 | `10` |
| `QMT_LOG_ALERT_WEBHOOK` | 日志告警 webhook（支持 `{url}|{secret}` HMAC 签名） | 空 |

完整配置项（共 43 个 `QMT_*` 变量，含 TOTP 二次确认、信号 webhook、预交易风控、限流、K 线缓存 TTL 等）见 `backend/.env.example`。

---

## 测试与验证

| 层级 | 命令 | 覆盖范围 |
|------|------|----------|
| 后端单测 | `cd backend && for f in tests/test_*.py; do python -m pytest "$f" -q -p no:cacheprovider; done` | 86 个 `test_*.py`，778 个用例 |
| 后端冒烟 | `python backend/tests/smoke2.py` | REST 主要端点 + 错误语义（**需先起后端**；默认连 `data/app.db`，检测到真实券商连接时自动跳过 3 条「未连接券商 → 503」断言并提示改用下方客户端测试做权威验证） |
| 前端类型 / lint | `cd frontend && npm run typecheck && npm run lint` | TypeScript strict + ESLint 零告警 |
| 前端单测 | `cd frontend && npm test` | vitest |
| 前端渲染冒烟 | `node tests/render_smoke_all.mjs` | headless 逐页渲染，判定 `.pane-leaf-body` 非空 |
| 客户端端到端 | `python scripts/client_start_test.py --target client\|dev\|backend` | 清理 → 启动 → 就绪 → REST 冒烟 → WS → 窗口截图 → 停机 → 零残留 |
| 契约计数门禁 | `python scripts/ci_reconcile.py` | 测试数 / 组件数 / 注册页数与文档一致 |

**两条重要约定**

1. **pytest 必须逐文件跑**。同进程全量收集在本机 Windows 环境会崩溃（`ci_reconcile.py` 因此改为逐文件 `--collect-only` 汇总）。
2. **`ci_reconcile.py` 是契约门禁，不是通过率旁路**。发现漂移直接失败，`python scripts/ci_reconcile.py --update` 可回写期望值（改代码须同步改文档）。

后端日志探活：`grep -c "quote handler registration failed" logs/qmt_work.log` 应为 `0`（非 0 = 行情回调注册失败）。

---

## 运维与监控

### 健康检查（标准化探针，供外部监控 / 编排接入）

| 端点 | 用途 | 返回 |
|------|------|------|
| `GET /api/v1/live` | 存活探针（进程活着即 200，不检查依赖） | 200 + 基础信息 |
| `GET /api/v1/health` | 综合健康：DB / 引擎 / 券商连接 / 交易时段 + 标准 `checks` 汇总 | 200 |
| `GET /api/v1/ready` | 就绪探针（DB 可读 + 启动完成 + 核心引擎在跑） | 200 / 503 |
| `GET /api/v1/metrics` | Prometheus 文本格式指标（订单 / 行情 / 回测 / WS / 错误计数） | 200 |

所有探针带统一 `code/message/data` 包裹与 `service/version` 字段，可被 Prometheus Blackbox、Uptime Kuma、容器编排健康检查直接消费。

### 端口锁定（多实例防冲突）

后端将实际监听端口持久化到 `data/.qmt_work.port`，下次启动优先复用该端口（仍被占用才 +1），避免多实例部署时端口漂移与冲突。桌面壳通过 `QMT_PORT_FILE` 读取实际端口连接。

### 日志聚合与告警

- 本地：`logs/qmt_work.log` 按天滚动，保留 14 天，含请求链路号与敏感信息脱敏
- 结构化：`QMT_LOG_JSON=true` 时额外输出 `logs/qmt_work.jsonl`（逐行 JSON），供 Loki / ELK / 自建采集器抓取
- 告警：`QMT_LOG_ALERT_WEBHOOK` 配置后，ERROR 及以上日志异步推送 webhook（支持 `{url}|{secret}` HMAC-SHA256 签名、指数退避重试、队列洪峰保护），可对接钉钉 / 企业微信 / 自定义监控

### 数据备份

启动 / 周期 / 关闭前各执行一次 SQLite 备份，保留最近 `QMT_DB_BACKUP_KEEP`（默认 10）份。

### 自动更新

桌面壳集成 electron-updater：启动后静默检查更新，托盘菜单「检查更新」可手动触发；发现新版本提示下载，下载完成退出时自动安装。更新源由构建时 `QMT_UPDATE_URL` 指定。

---

## 项目结构

```
qmt_work/
├─ backend/              # FastAPI 统一后端（V10 重构：core/ 无依赖内核 + engines/ 引擎）
│  ├─ run.py            # 启动入口（端口自动扫描 + 单实例锁 + AppContext 装配）
│  ├─ app/              # 装配层：main / routes / services / gateway
│  │  ├─ routes/        # 34 个 REST 路由模块（account/market/trade/backtest/broker/…）
│  │  └─ gateway/       # 鉴权 / 限流 / 风控 / 审计 / 脱敏 / K 线缓存 / metrics / 日志告警
│  ├─ core/             # 无依赖内核（context / crypto / db …）
│  ├─ engines/          # 交易引擎（signal router / execution / backtest …）
│  ├─ xtquant_client/   # BrokerAdapter / Manager / 桥接子进程 / 各券商适配器 / registry
│  ├─ datasource/       # 统一数据面（多数据源链 + 降级策略）
│  ├─ mcp_server/       # MCP 工具注册
│  ├─ agent/            # 自然语言选股（NL 解析，非 LLM 对话）
│  ├─ connectors/ plugins/ sync/   # 外部连接器 / 插件内核 / WebSocket 同步引擎
│  ├─ tools/ runtimes/  # 因子策略工具 / 捆绑 Python 运行时（cp311）
│  ├─ data/ static/ dist/   # SQLite / 前端构建产物 / PyInstaller 产物
│  ├─ tests/            # 86 个 test_*.py（778 用例）+ 冒烟测试 smoke2.py
│  ├─ scripts/          # 门禁脚本（许可 / 能力漂移 / 契约生成 / 架构校验）
│  └─ build_exe.py      # EXE 打包脚本（含 static 闸门）
├─ frontend/             # React + Vite + Electron
│  ├─ src/
│  │  ├─ App.jsx · main.jsx · pagesRegistry.jsx   # 入口 / 页面注册表（单一真源）
│  │  ├─ api.js                # 统一 REST 客户端（get/post/put/patch/del 5 个通用方法）
│  │  ├─ components/           # 52 个可复用 UI 组件（含 brokers/ marketdata/ ui/ 子目录）
│  │  ├─ features/             # 18 个页面级组件：market/ research/ trading/ accounts/ system/
│  │  ├─ hubs/                 # 6 个聚合 Hub（市场结构 / 因子 / 策略 / 数据 / 运维 / 审计）
│  │  ├─ store/workspace.jsx   # 多标签工作区（MAX_TABS=24 / MAX_ALIVE=8 / keep-alive）
│  │  ├─ hooks/ lib/ shared/   # 自定义 Hook / 工具库 / 事件单一真源
│  │  └─ electron/             # 桌面壳（端口发现 + 托盘 + 开机自启 + 自动更新）
│  ├─ tests/render_smoke*.mjs  # headless 渲染冒烟
│  └─ electron-builder.yml     # 打包配置（extraResources: ../backend/dist）
├─ scripts/              # 构建 / CI / 门禁脚本（ci_reconcile / verify_artifacts / client_start_test …）
├─ .github/workflows/    # ci.yml · build-client.yml · release.yml
├─ TOOLCHAIN.json        # 工具链锁（node / python / pyinstaller / electron）
├─ build_all.sh          # 一键构建（sh，默认 zip 便携版）
├─ build_all.bat         # 一键构建（Windows CMD）
├─ docs/                 # 使用指南 + 方案 / 重构记录（索引见 docs/README.md）
└─ README.md
```

---

## 贡献指南

感谢你愿意为 qmt_work 做出贡献。提交前请先阅读以下约定。

### 开发流程

1. **Fork 本仓库**，从 `main` 创建功能分支：`git checkout -b feat/your-feature`
2. **搭建环境**：
   ```bash
   cd backend && python3.11 -m venv .venv && .venv\Scripts\activate
   pip install -r requirements.txt
   cd ../frontend && npm install
   ```
3. **开发**，保持改动聚焦，避免夹带无关重构。
4. **本地自检**（必须全部通过）：
   ```bash
   # 后端：逐文件运行，规避 Windows 同进程收集崩溃
   cd backend
   for f in tests/test_*.py; do python -m pytest "$f" -q -p no:cacheprovider || exit 1; done
   python -m ruff check . --select=F,E9     # 仅 F + E9 规则

   # 前端
   cd ../frontend
   npm run typecheck
   npm run lint
   npm test
   npm run build

   # 契约与构建
   cd ..
   python scripts/ci_reconcile.py           # 测试数 / 组件数 / 注册页数与文档一致
   ```
5. **提交 PR**，标题格式：`[类型] 简短描述`，类型取 `feat` / `fix` / `docs` / `refactor` / `test` / `chore`：
   - `[feat] 新增条件单功能`
   - `[fix] 修复未连接券商时 K 线端点返回 500`

### 提交前门禁（CI 自动执行，建议本地先跑）

| 门禁 | 脚本 |
|------|------|
| 许可扫描（AGPL/GPL 零容忍） | `python backend/scripts/check_licenses.py` |
| 能力漂移（REST agent-visible ↔ MCP tool） | `python backend/scripts/check_capability_drift.py` |
| 前端 className 差集 | `python backend/scripts/check_frontend_classnames.py` |
| 执行架构校验 | `python scripts/check_execution_architecture.py` |
| 文档数字核对（测试数 / 组件数 / 注册页数） | `python scripts/ci_reconcile.py` |
| 制品校验 | `python scripts/verify_artifacts.py` |

> **改代码必须同步文档**：`ci_reconcile.py` 会核对测试数、前端组件数与注册页数是否与方案文档一致，漂移直接失败。

### 新增券商

绝大多数迅投系券商**无需写新适配器**，只需追加一条 `BrokerProfile`：

1. 在 `backend/xtquant_client/registry.py` 的 `BROKER_PROFILES` 追加 `BrokerProfile`（`id` / `name` / `adapter` / `default_client_path` / `supported_account_types`）
2. 非迅投系券商：在 `backend/xtquant_client/adapters/` 实现 `BrokerAdapter` 子类（继承 `xtquant_client/base.py` 基类）
3. 前端「连接管理」页自动列出，无需改组件

详见 `docs/BROKER_ONBOARDING.md`。

### 编码规范

| 语言 | 规范 |
|------|------|
| Python | PEP 8；行宽 100；类型标注；`pytest` 测试；ruff 仅启用 `F` + `E9` |
| JavaScript | ES Modules；React 函数组件 + Hooks；TypeScript strict；ESLint 零告警 |
| 通用 | 中文注释说明「为什么」而非「是什么」；不提交 `__pycache__` / `node_modules` / `dist` |

### 目录约定

| 位置 | 内容 |
|------|------|
| `backend/core/` | 无依赖内核（禁止反向 import app / engines） |
| `backend/engines/` | 交易引擎（SignalRouter 是唯一意图入口） |
| `backend/app/routes/` | REST 路由（只做参数校验与编排，业务下沉 engines / tools） |
| `backend/gateway/` | 横切关注点（鉴权 / 风控 / 脱敏 / 缓存等） |
| `backend/xtquant_client/` | 券商连接抽象与实现（不得 import app） |
| `backend/tests/` | 单测，文件名 `test_*.py`，逐文件独立可跑 |
| `frontend/src/features/` | 页面级组件（按 market / research / trading / accounts / system 分组） |
| `frontend/src/components/` | 可复用 UI 组件 |
| `frontend/src/pagesRegistry.jsx` | 菜单 / 功能树 / 命令面板的单一真源 |
| `frontend/src/shared/events.ts` | WebSocket 事件类型单一真源 |

### 安全红线

- **禁止提交** `.env`、密钥、证书、真实资金账号、券商客户端路径等敏感信息
- **禁止引入 mock 数据**：未连接券商必须返回 503，不得为「让测试通过」而伪造行情 / 账户
- 引入新依赖前先确认许可证可商用（AGPL/GPL 会被 CI 拒绝），并在 `docs/THIRD_PARTY_LICENSES.md` 登记

### 报告问题

提交 Issue 时请附：版本号（`VERSION`）、复现步骤、期望与实际结果、相关日志（`logs/qmt_work.log`，注意脱敏）、是否已连接券商及券商类型。

---

## 常见问题（FAQ）

<details>
<summary><b>启动后接口返回 503，提示「未连接券商」</b></summary>

这是**预期行为**。平台零 mock，未连接券商时不返回假数据。请到「连接管理」页添加券商，或通过 `QMT_BROKER_ID` / `QMT_CLIENT_PATH` / `QMT_ACCOUNT_ID` 配置引导连接。
</details>

<details>
<summary><b>桌面客户端白屏 / 「前端界面加载失败」</b></summary>

按顺序排查：

1. **包内静态资源是否完整**：`build_all.sh` 打包后会打印 `包内 static: N / M 个文件` 与「入口 xxx 在包内」。若文件数不一致或入口缺失，用 `--clean-dist` 彻底清理后重打包。
2. **是否发生半写入打包**：vite 先清 `assets` 再逐块写入，PyInstaller 若在半写入时冻结清单 → 产出的包只含少量分片，且 **exit code 仍为 0、日志无报错**。判断依据：`backend/build/qmt_work/Analysis-00.toc` 的 mtime 早于前端构建完成时间。`build_all.sh` 的 `verify_static_ready` 与 `build_exe.py` 的同类闸门就是为此加的。
3. **浏览器旧 chunk 缓存**：SPA 重部署后浏览器持有旧 entry chunk，其引用的旧 hash 分片已被清理 → `Failed to fetch dynamically imported module`。`pagesRegistry.jsx` 的 `lazyWithRetry` 会自动整页刷新一次拉取最新构建（`sessionStorage` 守卫防循环）。
4. **截图空白 ≠ 内容为空**：headless Chromium 加 `--disable-gpu` 后滚动容器 `.pane-leaf-body` 不被光栅化，截图 / 像素采样恒空白。必须用 DOM / hit-test（`elementsFromPoint` + `getBoundingClientRect`）交叉验证，这也是 `tests/render_smoke*.mjs` 已去掉 `--disable-gpu` 的原因。
</details>

<details>
<summary><b>`/brokers/test` 提示找不到兼容的 Python 运行时</b></summary>

迅投 xtquant 的 C 扩展按 CPython 小版本编译。执行 `python backend/tools/fetch_runtimes.py --only cp311` 下载捆绑运行时（会随打包进 EXE），或安装本机对应的 Python 版本后重试。接口会返回三条可操作修复路径。
</details>

<details>
<summary><b>端口被占用</b></summary>

后端会自动平滑 `+1`（最多 10 次），实际端口写入 `data/.qmt_work.port`。也可显式设置 `QMT_PORT`。
</details>

<details>
<summary><b>Windows 提示「未知发布者」</b></summary>

未配置代码签名证书时的正常现象。构建时设置 `CSC_LINK` + `CSC_KEY_PASSWORD` 即可自动签名。
</details>

<details>
<summary><b>桌面客户端在服务器 / 无桌面会话下无法启动</b></summary>

Electron 桌面壳需要图形界面会话，属已知限制。另外无 GPU 会话下 Chromium 会 FATAL（`gles2_cmd_decoder`）—— **不要加 `--in-process-gpu` / `--disable-software-rasterizer`**；`frontend/electron/main.cjs` 的 SAFE_MODE 组合已用 swiftshader 兜底。无桌面环境请改用后端 EXE + 浏览器访问。
</details>

<details>
<summary><b>构建脚本被「安全删除保护」拦住</b></summary>

装有安全删除 shim 的机器上，批量删除（阈值 50 个文件）会被拦。`build_all.sh` / `build_all.bat` 默认**不**删除上一轮 `backend/dist`（PyInstaller `--noconfirm` 会覆盖同名文件，产物依然正确），因此不会触发拦截；需要彻底清理时显式加 `--clean-dist`。残留的旧分片属于孤儿文件，打包后会以列表形式上报。
</details>

<details>
<summary><b>前端 `npm run dev` 后接口 404</b></summary>

确认后端已启动。Vite 开发服务器把 `/api` `/ws` `/mcp` 代理到 `http://127.0.0.1:21118`；若后端因端口占用改了口，需同步 `frontend/vite.config.js` 的代理目标。
</details>

<details>
<summary><b>点击菜单打开已打开过的页面（设置 / 连接管理 / 仪表盘）不切换</b></summary>

这类页面是**单例页**（`MULTI_INSTANCE_PAGES` 之外）。`workspaceReducer` 的 `activate(state, tabId)` 必须传入 `tabId`，漏传会静默返回旧 state → 界面停在旧页且零提示。若你新增了 `activate()` 调用点，务必核对参数个数。
</details>

---

## 已知限制

- **Windows 专用**：迅投系券商依赖 xtquant（Windows 二进制），须与 QMT 客户端同机运行
- **真实券商必需**：未连接券商时平台返回 503，不提供假数据
- **同花顺 / PTrade / 掘金**：适配器接口契约已实现，待对应 SDK 接入
- **桌面壳需桌面会话**：Electron 在无图形界面会话中无法启动
- **`eltdx` 为 Research-Only 许可，禁商用**：故保留在 `requirements-optional.txt` 而非主依赖；`commercial_mode=true` 时数据源链自动跳过 eltdx

---

## License

Apache 2.0 —— 详见 [LICENSE](LICENSE)。

第三方依赖许可说明见 [docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md)。
