# qmt_work 后端（Phase 1 骨架）

FastAPI 单进程统一后端：REST 网关 + MCP（Streamable HTTP）+ 工具层 + XTQuant 线程模型（Mock 网关）。

## 快速启动

```bash
# 1. 创建隔离环境（首次）
python -m venv <venv路径>
<venv>/Scripts/pip install -r requirements.txt

# 2. 启动（默认 QMT_MODE=mock，无需真实 QMT）
cd backend
<venv>/Scripts/python run.py
```

## 验证

```bash
# REST
curl http://127.0.0.1:21117/api/v1/health
curl http://127.0.0.1:21117/api/v1/quote/600519.SH
curl -X POST http://127.0.0.1:21117/api/v1/orders -H "Content-Type: application/json" \
  -d '{"code":"600519.SH","direction":"buy","volume":100,"price":280.0}'

# MCP（Streamable HTTP，Claude Desktop/Cursor 可配置 http://127.0.0.1:21117/mcp）
<venv>/Scripts/python tests/smoke.py   # 全链路冒烟：REST 7 项 + MCP 4 项
```

## 端点

| 端点 | 说明 |
|------|------|
| `GET /api/v1/health` | 综合健康（QMT 连接/订阅数） |
| `GET /api/v1/quote/{code}` `GET /api/v1/kline/{code}` | 行情 |
| `POST /api/v1/orders` `GET /api/v1/positions` `GET /api/v1/cash` | 交易（过风控） |
| `POST /api/v1/backtest` | 回测（落库） |
| `/mcp` | MCP Streamable HTTP 端点（12 个工具） |
| `/api/docs` | Swagger 文档 |

## 配置（.env，前缀 QMT_）

- `QMT_MODE=mock|real`：mock 为无 QMT 环境的开发模式；real 需在 `xtquant_client/real_gateway.py` 实现真实网关
- `QMT_API_KEY`：远程调用鉴权 Key（本机 loopback 免 Key）
- `QMT_RISK_MAX_AMOUNT` / `QMT_RISK_MIN_QTY`：风控默认值

## 关键技术点

- **线程模型（§4.14）**：XTQuant 同步调用经 `run_in_executor` 线程池隔离；回调经线程安全队列投递到事件循环；下单加锁串行化——见 `xtquant_client/gateway.py`
- **fastmcp 2.x + FastAPI 集成**：`http_app(path="/", transport="streamable-http")` + `mount("/mcp")`，且必须把 `mcp_app.router.lifespan_context(mcp_app)` 组合进父应用 lifespan（否则 SessionManager 未初始化）
- **包名冲突**：本地包不得命名为 `mcp`（会遮蔽官方 mcp 库）
