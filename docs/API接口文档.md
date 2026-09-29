# API 接口文档（自动生成 · 反映真实契约）

> 本文档由 `backend/tests/contracts/{rest_endpoints,mcp_tools,ws_events}.json` **自动生成**，是 qmt_work 当前 REST / MCP / WebSocket 接口的**真实清单**。

> 契约基线由 CI 门禁固化（`check_capability_drift.py` / `ci_reconcile.py`）：删除或重命名任一接口即红灯。**计数随代码变化**，以契约文件为准，不要手抄。

> 鉴权、scope、错误语义、payload 形状与跨语言（Python / Node / curl）示例见 [`多语言接入指南.md`](多语言接入指南.md)。

## 规模速览

| 接口面 | 数量 | 来源 |
|--------|------|------|
| REST 端点 | **232** | `rest_endpoints.json` |
| MCP 工具 | **127** | `mcp_tools.json` |
| WebSocket 渠道 | **30** 渠道 / 4 子类型 | `ws_events.json` |

## 通用约定（简明，详细见多语言接入指南）

- **鉴权**：REST / MCP 均带 `X-API-Key` 头（由 `QMT_API_KEY` 配置，生产务必修改）；WebSocket 连接带 `?token=<API-KEY>` 查询参数。
- **零 mock**：未连接券商时行情/交易/账户等端点返回 **HTTP 503** + 可操作引导；券商可用但被拒（风控/柜台拒单/非交易时段）返回 **HTTP 400** + 真实原因。**绝不把失败包成 `code=0`**，否则拒单会被前端显示成「已报」= 假成功。
- **统一响应包裹**：`{ code, message, data }`，`code !== 0` 即错误。健康检查探针（`/live` `/health` `/ready` `/metrics`）额外带 `service/version`。
- **错误归因**：错误 `message` 给出根因分类（未连接券商 / 柜台拒单 / 风控拦截 / 参数错误 / 内部异常），前端按分类给出不同引导，不做「网络错误」兜底。

## REST API（232 端点）

所有路径前缀为 `/api/v1`，按资源分组；组内先按方法（`G`=GET `P`=POST `U`=PUT `D`=DELETE `X`=PATCH）再按路径排序。

### account（账户）· 8 端点

账户总览、盈亏与滑点分析；批量下单 / 撤单 / 重连。

| M | 路径 |
|---|------|
| G | `/api/v1/account/aggregate` |
| G | `/api/v1/account/grid` |
| G | `/api/v1/account/pnl` |
| G | `/api/v1/account/slippage` |
| G | `/api/v1/account/status` |
| P | `/api/v1/account/batch/cancel` |
| P | `/api/v1/account/batch/order` |
| P | `/api/v1/account/batch/reconnect` |

### alerts（告警）· 6 端点

告警规则的增删改查、批量删除、测试推送与历史查询。

| M | 路径 |
|---|------|
| G | `/api/v1/alerts/history` |
| G | `/api/v1/alerts/rules` |
| P | `/api/v1/alerts/rules` |
| P | `/api/v1/alerts/rules/batch-delete` |
| P | `/api/v1/alerts/test` |
| D | `/api/v1/alerts/rules/{rid}` |

### algo（算法单）· 5 端点

算法拆单：提交、暂停、恢复与撤销。

| M | 路径 |
|---|------|
| G | `/api/v1/algo` |
| P | `/api/v1/algo/submit` |
| P | `/api/v1/algo/{algo_id}/cancel` |
| P | `/api/v1/algo/{algo_id}/pause` |
| P | `/api/v1/algo/{algo_id}/resume` |

### api-keys（API 密钥）· 7 端点

API Key 的创建、轮换、启用/禁用、批量删除与清理未使用。

| M | 路径 |
|---|------|
| G | `/api/v1/api-keys` |
| P | `/api/v1/api-keys` |
| P | `/api/v1/api-keys/batch-delete` |
| P | `/api/v1/api-keys/clean-unused` |
| P | `/api/v1/api-keys/{kid}/rotate` |
| D | `/api/v1/api-keys/{kid}` |
| X | `/api/v1/api-keys/{kid}` |

### audit（审计）· 2 端点

审计日志查询与完整性校验。

| M | 路径 |
|---|------|
| G | `/api/v1/audit` |
| G | `/api/v1/audit/verify` |

### backtest（回测）· 6 端点

回测作业的增删改查与参数扫描（sweep）。

| M | 路径 |
|---|------|
| G | `/api/v1/backtest/jobs` |
| G | `/api/v1/backtest/jobs/{job_id}` |
| P | `/api/v1/backtest/jobs` |
| P | `/api/v1/backtest/jobs/batch-delete` |
| P | `/api/v1/backtest/sweep` |
| D | `/api/v1/backtest/jobs/{job_id}` |

### brokers（券商连接）· 15 端点

券商连接全生命周期：配置、自动检测、启动、连接/断开/激活、健康与诊断。

| M | 路径 |
|---|------|
| G | `/api/v1/brokers` |
| G | `/api/v1/brokers/auto-detect` |
| G | `/api/v1/brokers/diagnostics` |
| G | `/api/v1/brokers/profiles` |
| G | `/api/v1/brokers/runtimes` |
| G | `/api/v1/brokers/{conn_id}/health` |
| P | `/api/v1/brokers` |
| P | `/api/v1/brokers/batch-delete` |
| P | `/api/v1/brokers/launch` |
| P | `/api/v1/brokers/test` |
| P | `/api/v1/brokers/version-info` |
| P | `/api/v1/brokers/{conn_id}/active` |
| P | `/api/v1/brokers/{conn_id}/connect` |
| P | `/api/v1/brokers/{conn_id}/disconnect` |
| D | `/api/v1/brokers/{conn_id}` |

### capabilities（能力自描述）· 3 端点

运行期能力自描述：REST / MCP 能力总览（agent-visible 口径）。

| M | 路径 |
|---|------|
| G | `/api/v1/capabilities` |
| G | `/api/v1/capabilities/mcp` |
| G | `/api/v1/capabilities/summary` |

### config（配置）· 20 端点

路径 / 风控 / 运行时 / UI 配置的读写、导入导出，及备份、迁移、熔断、回滚等运维动作。

| M | 路径 |
|---|------|
| G | `/api/v1/config/paths` |
| G | `/api/v1/config/risk` |
| G | `/api/v1/config/risk/daily` |
| G | `/api/v1/config/runtime` |
| G | `/api/v1/config/runtime/history` |
| G | `/api/v1/config/ui` |
| G | `/api/v1/config/ui/export` |
| P | `/api/v1/config/paths/db-backups/prune` |
| P | `/api/v1/config/paths/db-backups/run` |
| P | `/api/v1/config/paths/migrate-cold` |
| P | `/api/v1/config/paths/validate` |
| P | `/api/v1/config/risk/circuit` |
| P | `/api/v1/config/runtime/reset` |
| P | `/api/v1/config/runtime/rollback` |
| P | `/api/v1/config/ui/import` |
| P | `/api/v1/config/ui/reset` |
| U | `/api/v1/config/paths` |
| U | `/api/v1/config/risk` |
| U | `/api/v1/config/runtime` |
| U | `/api/v1/config/ui` |

### data（数据源）· 4 端点

数据源清单、健康检查与链路诊断。

| M | 路径 |
|---|------|
| G | `/api/v1/data/providers` |
| G | `/api/v1/data/providers/health` |
| G | `/api/v1/data/source/diagnostics` |
| P | `/api/v1/data/chain` |

### datahub（数据策略）· 1 端点

数据面策略（policies）查询。

| M | 路径 |
|---|------|
| G | `/api/v1/datahub/policies` |

### factors（因子）· 4 端点

因子计算：单标的、批量、从 K 线衍生。

| M | 路径 |
|---|------|
| G | `/api/v1/factors` |
| P | `/api/v1/factors/compute` |
| P | `/api/v1/factors/compute/many` |
| P | `/api/v1/factors/from-kline` |

### health（健康）· 1 端点

健康探针。

| M | 路径 |
|---|------|
| G | `/api/v1/health` |

### limitup（涨停监控）· 6 端点

涨停监控的启停、状态查询与打板池管理。

| M | 路径 |
|---|------|
| G | `/api/v1/limitup/status` |
| P | `/api/v1/limitup/pool` |
| P | `/api/v1/limitup/reset` |
| P | `/api/v1/limitup/start` |
| P | `/api/v1/limitup/stop` |
| D | `/api/v1/limitup/pool` |

### live（存活探针）· 1 端点

存活探针（进程活着即 200）。

| M | 路径 |
|---|------|
| G | `/api/v1/live` |

### market（行情）· 52 端点

行情全景：K线 / 分时 / tick、板块与成分、资金流、选股（表达式 / 经典 / 自然语言）、指标计算、导出与同步。

| M | 路径 |
|---|------|
| G | `/api/v1/market/analysis` |
| G | `/api/v1/market/analysis/scripts` |
| G | `/api/v1/market/board/constituents` |
| G | `/api/v1/market/board/kline` |
| G | `/api/v1/market/board/lookup` |
| G | `/api/v1/market/board/moneyflow` |
| G | `/api/v1/market/boards` |
| G | `/api/v1/market/breadth` |
| G | `/api/v1/market/capital` |
| G | `/api/v1/market/chart-spec` |
| G | `/api/v1/market/coverage` |
| G | `/api/v1/market/datasets/snapshots` |
| G | `/api/v1/market/etfs` |
| G | `/api/v1/market/indicators` |
| G | `/api/v1/market/indicators/calc` |
| G | `/api/v1/market/indices` |
| G | `/api/v1/market/kline` |
| G | `/api/v1/market/kline/cache` |
| G | `/api/v1/market/kline/export` |
| G | `/api/v1/market/kline/sync-status` |
| G | `/api/v1/market/l2` |
| G | `/api/v1/market/limitup` |
| G | `/api/v1/market/minutes` |
| G | `/api/v1/market/moneyflow` |
| G | `/api/v1/market/moneyflow/replay` |
| G | `/api/v1/market/overview` |
| G | `/api/v1/market/periods` |
| G | `/api/v1/market/providers` |
| G | `/api/v1/market/quote` |
| G | `/api/v1/market/resolve` |
| G | `/api/v1/market/rotation` |
| G | `/api/v1/market/screen` |
| G | `/api/v1/market/screen/boards` |
| G | `/api/v1/market/screen/classic/picks` |
| G | `/api/v1/market/screen/strategies` |
| G | `/api/v1/market/search` |
| G | `/api/v1/market/session` |
| G | `/api/v1/market/sources` |
| G | `/api/v1/market/stock-info` |
| P | `/api/v1/market/analysis/run` |
| P | `/api/v1/market/crawl` |
| P | `/api/v1/market/export` |
| P | `/api/v1/market/kline/export` |
| P | `/api/v1/market/kline/sync` |
| P | `/api/v1/market/moneyflow/snapshot` |
| P | `/api/v1/market/portfolio/aggregate` |
| P | `/api/v1/market/quotes` |
| P | `/api/v1/market/screen/boards` |
| P | `/api/v1/market/screen/classic` |
| P | `/api/v1/market/screen/expr` |
| P | `/api/v1/market/screen/nl` |
| D | `/api/v1/market/kline/cache` |

### metrics（指标暴露）· 1 端点

Prometheus 指标暴露。

| M | 路径 |
|---|------|
| G | `/api/v1/metrics` |

### notifications（通知）· 6 端点

通知渠道的增删改查、测试发送与投递日志。

| M | 路径 |
|---|------|
| G | `/api/v1/notifications` |
| G | `/api/v1/notifications/logs` |
| P | `/api/v1/notifications` |
| P | `/api/v1/notifications/batch-delete` |
| P | `/api/v1/notifications/test` |
| D | `/api/v1/notifications/{nid}` |

### paper（模拟盘）· 6 端点

模拟盘：账户 / 持仓 / 成交 / 绩效查询，下单与重置。

| M | 路径 |
|---|------|
| G | `/api/v1/paper/account` |
| G | `/api/v1/paper/metrics` |
| G | `/api/v1/paper/positions` |
| G | `/api/v1/paper/trades` |
| P | `/api/v1/paper/order` |
| P | `/api/v1/paper/reset` |

### platform（平台状态）· 1 端点

平台运行状态。

| M | 路径 |
|---|------|
| G | `/api/v1/platform/status` |

### quote-bus（行情总线）· 1 端点

行情总线（quote-bus）统计。

| M | 路径 |
|---|------|
| G | `/api/v1/quote-bus/stats` |

### ready（就绪探针）· 1 端点

就绪探针（关键依赖就绪才返回 200）。

| M | 路径 |
|---|------|
| G | `/api/v1/ready` |

### rebalance（再平衡）· 1 端点

按目标持仓生成并执行再平衡。

| M | 路径 |
|---|------|
| P | `/api/v1/rebalance` |

### reconcile（对账）· 4 端点

委托对账核销与 WAL 检查点。

| M | 路径 |
|---|------|
| G | `/api/v1/reconcile/last` |
| G | `/api/v1/reconcile/wal/stats` |
| P | `/api/v1/reconcile` |
| P | `/api/v1/reconcile/wal/checkpoint` |

### reference（参考数据）· 4 端点

静态参考数据：交易日历、财务摘要、板块列表与板块成分。

| M | 路径 |
|---|------|
| G | `/api/v1/reference/calendar` |
| G | `/api/v1/reference/financial` |
| G | `/api/v1/reference/sector-stocks` |
| G | `/api/v1/reference/sectors` |

### research（研究分析）· 6 端点

研究分析：归因、相关性、因子 IC、分位分析、组合回测与 walk-forward。

| M | 路径 |
|---|------|
| P | `/api/v1/research/attribution` |
| P | `/api/v1/research/correlation` |
| P | `/api/v1/research/factor-ic` |
| P | `/api/v1/research/portfolio-backtest` |
| P | `/api/v1/research/quantile` |
| P | `/api/v1/research/walk-forward` |

### runtime（任务运行时）· 10 端点

作业与定时任务的增删改查、手动触发与取消。

| M | 路径 |
|---|------|
| G | `/api/v1/runtime/jobs` |
| G | `/api/v1/runtime/jobs/{job_id}` |
| G | `/api/v1/runtime/schedules` |
| G | `/api/v1/runtime/schedules/{schedule_id}` |
| P | `/api/v1/runtime/jobs` |
| P | `/api/v1/runtime/jobs/{job_id}/cancel` |
| P | `/api/v1/runtime/schedules` |
| P | `/api/v1/runtime/schedules/{schedule_id}/trigger` |
| U | `/api/v1/runtime/schedules/{schedule_id}` |
| D | `/api/v1/runtime/schedules/{schedule_id}` |

### scheduler（调度器）· 1 端点

调度器优雅关停。

| M | 路径 |
|---|------|
| P | `/api/v1/scheduler/shutdown` |

### signal（交易信号）· 5 端点

交易信号提交 / 二次确认、模式切换（dry_run / paper / live）与 webhook 接入。

| M | 路径 |
|---|------|
| G | `/api/v1/signal/mode` |
| P | `/api/v1/signal/confirm` |
| P | `/api/v1/signal/mode` |
| P | `/api/v1/signal/submit` |
| P | `/api/v1/signal/webhook` |

### strategies（策略）· 11 端点

策略生成、保存、预检与运行生命周期（启动 / 停止 / 日志 / 删除）。

| M | 路径 |
|---|------|
| G | `/api/v1/strategies/run` |
| G | `/api/v1/strategies/run/{run_id}` |
| G | `/api/v1/strategies/run/{run_id}/logs` |
| P | `/api/v1/strategies/generate` |
| P | `/api/v1/strategies/run` |
| P | `/api/v1/strategies/run/batch-delete` |
| P | `/api/v1/strategies/run/precheck` |
| P | `/api/v1/strategies/run/{run_id}/start` |
| P | `/api/v1/strategies/run/{run_id}/stop` |
| P | `/api/v1/strategies/save` |
| D | `/api/v1/strategies/run/{run_id}` |

### strategy-market（策略市场）· 9 端点

策略市场：目录浏览、导入导出、安装与发布。

| M | 路径 |
|---|------|
| G | `/api/v1/strategy-market/catalog` |
| G | `/api/v1/strategy-market/market` |
| G | `/api/v1/strategy-market/market/{id}` |
| P | `/api/v1/strategy-market/export` |
| P | `/api/v1/strategy-market/export-json` |
| P | `/api/v1/strategy-market/import` |
| P | `/api/v1/strategy-market/import-json` |
| P | `/api/v1/strategy-market/install` |
| P | `/api/v1/strategy-market/publish` |

### sync（行情订阅）· 1 端点

行情订阅注册。

| M | 路径 |
|---|------|
| P | `/api/v1/sync/subscribe` |

### target-portfolio（目标持仓）· 5 端点

目标持仓计划的增删改查与同步执行。

| M | 路径 |
|---|------|
| G | `/api/v1/target-portfolio/plans` |
| P | `/api/v1/target-portfolio/plans` |
| P | `/api/v1/target-portfolio/plans/batch-delete` |
| P | `/api/v1/target-portfolio/sync` |
| D | `/api/v1/target-portfolio/plans/{pid}` |

### trade（交易）· 10 端点

下单 / 撤单 / 预检、条件单管理与委托 / 成交 / 持仓查询。

| M | 路径 |
|---|------|
| G | `/api/v1/trade/conditions` |
| G | `/api/v1/trade/deals` |
| G | `/api/v1/trade/orders` |
| G | `/api/v1/trade/positions` |
| P | `/api/v1/trade/cancel` |
| P | `/api/v1/trade/conditions` |
| P | `/api/v1/trade/conditions/{cid}/cancel` |
| P | `/api/v1/trade/order` |
| P | `/api/v1/trade/precheck` |
| P | `/api/v1/trade/target` |

### wal（WAL）· 2 端点

WAL 统计与手动检查点。

| M | 路径 |
|---|------|
| G | `/api/v1/wal/stats` |
| P | `/api/v1/wal/checkpoint` |

### webhooks（Webhook）· 6 端点

出站 Webhook 订阅的增删改查、测试与投递记录。

| M | 路径 |
|---|------|
| G | `/api/v1/webhooks` |
| G | `/api/v1/webhooks/deliveries` |
| P | `/api/v1/webhooks` |
| P | `/api/v1/webhooks/batch-delete` |
| P | `/api/v1/webhooks/{sid}/test` |
| D | `/api/v1/webhooks/{sid}` |

---

## MCP 工具（127 个）

FastMCP Streamable HTTP，接入点 `http://<host>:<port>/mcp`，携带 `X-API-Key`。工具集与 REST 的 `agent-visible` 端点保持同步（能力漂移门禁校验）。

按前缀分组（前缀 = 能力域）：

#### account_* · 1 个

账户状态。

| 工具 |
|------|
| `account_status` |

#### algo_* · 5 个

算法单生命周期。

| 工具 |
|------|
| `algo_cancel` |
| `algo_list` |
| `algo_pause` |
| `algo_resume` |
| `algo_submit` |

#### analyze_* · 2 个

交易分析（贡献度 / 滑点）。

| 工具 |
|------|
| `analyze_contribution` |
| `analyze_slippage` |

#### attribute_* · 1 个

绩效归因。

| 工具 |
|------|
| `attribute_performance` |

#### broker_* · 1 个

券商状态。

| 工具 |
|------|
| `broker_status` |

#### cancel_* · 2 个

撤单（按单号 / 价格）。

| 工具 |
|------|
| `cancel_order` |
| `cancel_order_price` |

#### compare_* · 1 个

回测结果对比。

| 工具 |
|------|
| `compare_backtests` |

#### condition_* · 3 个

条件单管理。

| 工具 |
|------|
| `condition_cancel` |
| `condition_list` |
| `condition_submit` |

#### factor_* · 3 个

因子分析（相关矩阵 / IC / 分位）。

| 工具 |
|------|
| `factor_correlation_matrix` |
| `factor_ic_analysis` |
| `factor_quantile_analysis` |

#### financial_* · 1 个

财务摘要。

| 工具 |
|------|
| `financial_summary` |

#### generate_* · 2 个

生成（策略 / 再平衡方案）。

| 工具 |
|------|
| `generate_rebalance` |
| `generate_strategy` |

#### get_* · 76 个

只读查询（行情 / 账户 / 配置 / 任务 / 系统状态等）。

| 工具 |
|------|
| `get_alerts_history` |
| `get_alerts_rules` |
| `get_audit` |
| `get_audit_verify` |
| `get_config_paths` |
| `get_config_risk` |
| `get_config_risk_daily` |
| `get_config_runtime` |
| `get_config_runtime_history` |
| `get_config_ui` |
| `get_config_ui_export` |
| `get_data_providers` |
| `get_data_providers_health` |
| `get_data_source_diagnostics` |
| `get_datahub_policies` |
| `get_full_tick` |
| `get_kline` |
| `get_live` |
| `get_market_analysis` |
| `get_market_analysis_scripts` |
| `get_market_board_constituents` |
| `get_market_board_kline` |
| `get_market_board_lookup` |
| `get_market_board_moneyflow` |
| `get_market_boards` |
| `get_market_breadth` |
| `get_market_capital` |
| `get_market_chart_spec` |
| `get_market_coverage` |
| `get_market_datasets_snapshots` |
| `get_market_etfs` |
| `get_market_indicators` |
| `get_market_indicators_calc` |
| `get_market_indices` |
| `get_market_kline_cache` |
| `get_market_kline_export` |
| `get_market_kline_sync_status` |
| `get_market_l2` |
| `get_market_limitup` |
| `get_market_minutes` |
| `get_market_moneyflow` |
| `get_market_moneyflow_replay` |
| `get_market_overview` |
| `get_market_periods` |
| `get_market_providers` |
| `get_market_resolve` |
| `get_market_rotation` |
| `get_market_screen` |
| `get_market_screen_boards` |
| `get_market_screen_classic_picks` |
| `get_market_screen_strategies` |
| `get_market_session` |
| `get_market_sources` |
| `get_market_stock_info` |
| `get_notifications` |
| `get_notifications_logs` |
| `get_paper_account` |
| `get_paper_metrics` |
| `get_paper_positions` |
| `get_paper_trades` |
| `get_platform_status` |
| `get_quote` |
| `get_quote_bus_stats` |
| `get_ready` |
| `get_reconcile_last` |
| `get_reconcile_wal_stats` |
| `get_runtime_jobs` |
| `get_runtime_jobs_by_job_id` |
| `get_runtime_schedules` |
| `get_runtime_schedules_by_schedule_id` |
| `get_stock_list` |
| `get_strategies_run` |
| `get_strategies_run_by_run_id` |
| `get_strategies_run_by_run_id_logs` |
| `get_tick` |
| `get_wal_stats` |

#### l2_* · 1 个

Level-2 逐笔成交。

| 工具 |
|------|
| `l2_transactions` |

#### limitup_* · 5 个

涨停监控与打板池。

| 工具 |
|------|
| `limitup_pool_add` |
| `limitup_pool_remove` |
| `limitup_start` |
| `limitup_status` |
| `limitup_stop` |

#### list_* · 2 个

券商列举。

| 工具 |
|------|
| `list_broker_profiles` |
| `list_brokers` |

#### monitor_* · 1 个

账户监控。

| 工具 |
|------|
| `monitor_account` |

#### monthly_* · 1 个

月度盈亏。

| 工具 |
|------|
| `monthly_pnl` |

#### net_* · 1 个

净值序列。

| 工具 |
|------|
| `net_value_series` |

#### order_* · 1 个

按目标仓位下单。

| 工具 |
|------|
| `order_target_position` |

#### place_* · 1 个

下单。

| 工具 |
|------|
| `place_order` |

#### portfolio_* · 1 个

组合回测。

| 工具 |
|------|
| `portfolio_backtest` |

#### query_* · 4 个

账户查询（资金 / 委托 / 成交 / 持仓）。

| 工具 |
|------|
| `query_cash` |
| `query_deals` |
| `query_orders` |
| `query_position` |

#### run_* · 1 个

运行回测。

| 工具 |
|------|
| `run_backtest` |

#### save_* · 1 个

保存策略。

| 工具 |
|------|
| `save_qmt_strategy` |

#### search_* · 1 个

标的搜索。

| 工具 |
|------|
| `search_stocks` |

#### sector_* · 2 个

板块与成分股。

| 工具 |
|------|
| `sector_list` |
| `sector_stocks` |

#### sensitivity_* · 1 个

参数敏感性分析。

| 工具 |
|------|
| `sensitivity_analysis` |

#### target_* · 3 个

目标持仓管理。

| 工具 |
|------|
| `target_portfolio_list` |
| `target_portfolio_save` |
| `target_portfolio_sync` |

#### trading_* · 1 个

交易日历。

| 工具 |
|------|
| `trading_calendar` |

#### walk_* · 1 个

walk-forward 滚动验证。

| 工具 |
|------|
| `walk_forward_analysis` |

---

## WebSocket 事件（30 渠道 / 4 子类型）

连接 `ws://<host>:<port>/api/v1/ws?token=<API-KEY>`（本机回环免 token）。服务端先推全量快照，再补发订阅代码最近 30s 行情（断线重连缺口）。客户端动作：`subscribe` / `unsubscribe` / `ping`（→ `pong`）。

payload 的 `data.type` 子类型（如 `order`→`order_event`）**不是**独立频道名；`order` / `deal` / `account` / `risk` 四类频道事件同时投递出站 Webhook。

| 渠道 | 子类型 | 说明 |
|------|--------|------|
| `account` | `account_snapshot` | 账户净值周期快照（SyncEngine 周期任务广播）。 |
| `alert` | — | 告警规则命中。 |
| `algo_alert` | — | 算法单异常告警。 |
| `algo_slice` | — | 算法单拆单进度。 |
| `broker.connected` | — | 券商连接建立（含重连成功）。 |
| `broker.disconnected` | — | 券商连接断开 / 进入退避重试（`health_status=needs_action` 表示需人工处理）。 |
| `condition_created` | — | 条件单创建。 |
| `condition_expired` | — | 条件单到期。 |
| `condition_failed` | — | 条件单失败（含原因）。 |
| `condition_order` | — | 条件单已转委托。 |
| `condition_settled` | — | 条件单结算完成。 |
| `condition_triggered` | — | 条件单触发。 |
| `deal` | `deal_event` | 新成交。 |
| `heartbeat` | — | 心跳。 |
| `limitup` | — | 涨停监控状态变化。 |
| `limitup_order` | — | 涨停打板下单。 |
| `order` | `order_event` | 委托新增 / 状态变化。 |
| `order.timeout` | — | 委托超时自动撤单。 |
| `pong` | — | ping 应答。 |
| `quotes` | — | 行情微批帧（默认 100ms 聚合）。 |
| `quotes_replay` | — | 断线重连后的补发帧。 |
| `reconcile` | — | 委托对账核销结果。 |
| `risk` | `risk_circuit` | 风控触发 / 熔断状态变化。 |
| `risk.blocked` | — | 风控拦截（附原因）。 |
| `signal_dry_run` | — | 信号路由结果（预演模式）。 |
| `signal_live` | — | 信号路由结果（实盘模式）。 |
| `signal_paper` | — | 信号路由结果（模拟盘模式）。 |
| `signal_pending` | — | 信号等待二次确认。 |
| `snapshot` | — | 账户快照（净值 / 持仓 / 资金）。 |
| `system` | — | 系统级消息。 |

---

## 端点/工具/事件完整机读清单

- REST：`backend/tests/contracts/rest_endpoints.json`
- MCP：`backend/tests/contracts/mcp_tools.json`
- WebSocket：`backend/tests/contracts/ws_events.json`

能力自描述运行期端点：`GET /api/v1/capabilities`（REST 总览）、`GET /api/v1/capabilities/mcp`（MCP 工具分组计数）。桌面客户端「系统 → MCP 工具」页可浏览并一键复制。
