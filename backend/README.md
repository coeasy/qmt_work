# qmt_work 后端

FastAPI 单进程统一后端，同源托管 REST API + MCP + WebSocket + 前端 SPA。**真实券商模式，无 mock**。

## 快速启动

```bash
# 1. 创建隔离环境（首次）
cd backend
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

# 2. 启动
python run.py    # http://127.0.0.1:21117
```

## 冒烟测试

```bash
python tests/smoke2.py    # 22 项 → PASS
python -m pytest          # 单测 → 121 passed
```

## 打包为 EXE

```bash
python build_exe.py
# 产出：dist/qmt_work/qmt_work.exe
```

## 环境变量

复制 `.env.example` 为 `.env` 按需修改。关键变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `QMT_PORT` | `21117` | 监听端口（被占用自动 +1） |
| `QMT_API_KEY` | `qmt-dev-key` | 鉴权密钥（生产必须修改） |
| `QMT_RISK_MAX_AMOUNT` | `100000` | 单笔最大金额 |

完整配置见 `.env.example`。

## 核心目录

| 目录 | 内容 |
|------|------|
| `app/` | config / db / routes / main / state |
| `xtquant_client/` | BrokerAdapter / Manager / Registry V2 / 桥接 / 各券商适配器 |
| `mcp_server/` | MCP 工具注册 |
| `agent/` | LLM Provider 抽象 + Agent 核心 |
| `gateway/` | 鉴权 / 限流 / 风控 / 审计 / 脱敏 / K 线缓存 / metrics / webhook |
| `backtest/` | 回测作业队列（含向量化 + 参数扫描） |
| `paper/` | 模拟盘引擎 |
| `scheduler/` | 定时任务 + 分布式调度 |
| `tools/` | 因子 / 策略 / 算法 / 涨停 / 条件单 / 参考数据 |
| `tests/` | 单测 + 冒烟测试 |