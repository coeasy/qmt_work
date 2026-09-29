# API 接口文档（自动生成 · 反映真实契约）

> 本文档由 `backend/tests/contracts/{rest_endpoints,mcp_tools,ws_events}.json` **自动生成**，是 qmt_work 当前 REST / MCP / WebSocket 接口的**真实清单**。

> 契约基线由 CI 门禁固化（`check_capability_drift.py` / `ci_reconcile.py`）：删除或重命名任一接口即红灯。**计数随代码变化**，以契约文件为准，不要手抄。

> 鉴权、scope、错误语义、跨语言（Python / Node / curl）示例见 [`多语言接入指南.md`](多语言接入指南.md)；本文档只列端点/工具/事件本体。


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

---

## REST API（232 端点）

所有路径前缀为 `/api/v1`。按资源分组；`M` 列方法：`G`=GET `P`=POST `U`=PUT `D`=DELETE `X`=PATCH。


### account（账户）· 8 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/account/aggregate` | ops |
| P | `/api/v1/account/batch/cancel` | ops |
| P | `/api/v1/account/batch/order` | ops |
| P | `/api/v1/account/batch/reconnect` | ops |
| G | `/api/v1/account/grid` | ops |
| G | `/api/v1/account/pnl` | ops |
| G | `/api/v1/account/slippage` | ops |
| G | `/api/v1/account/status` | ops |

### alerts（告警）· 6 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/alerts/history` | ops |
| G | `/api/v1/alerts/rules` | ops |
| P | `/api/v1/alerts/rules` | ops |
| P | `/api/v1/alerts/rules/batch-delete` | ops |
| D | `/api/v1/alerts/rules/{rid}` | ops |
| P | `/api/v1/alerts/test` | ops |

### algo（algo）· 5 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/algo` | ops |
| P | `/api/v1/algo/submit` | ops |
| P | `/api/v1/algo/{algo_id}/cancel` | ops |
| P | `/api/v1/algo/{algo_id}/pause` | ops |
| P | `/api/v1/algo/{algo_id}/resume` | ops |

### api-keys（api-keys）· 7 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/api-keys` | ops |
| P | `/api/v1/api-keys` | ops |
| P | `/api/v1/api-keys/batch-delete` | ops |
| P | `/api/v1/api-keys/clean-unused` | ops |
| D | `/api/v1/api-keys/{kid}` | ops |
| X | `/api/v1/api-keys/{kid}` | ops |
| P | `/api/v1/api-keys/{kid}/rotate` | ops |

### audit（审计）· 2 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/audit` | ops |
| G | `/api/v1/audit/verify` | ops |

### backtest（回测）· 6 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/backtest/jobs` | ops |
| P | `/api/v1/backtest/jobs` | ops |
| P | `/api/v1/backtest/jobs/batch-delete` | ops |
| D | `/api/v1/backtest/jobs/{job_id}` | ops |
| G | `/api/v1/backtest/jobs/{job_id}` | ops |
| P | `/api/v1/backtest/sweep` | ops |

### brokers（券商连接）· 15 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/brokers` | ops |
| P | `/api/v1/brokers` | ops |
| G | `/api/v1/brokers/auto-detect` | ops |
| P | `/api/v1/brokers/batch-delete` | ops |
| G | `/api/v1/brokers/diagnostics` | ops |
| P | `/api/v1/brokers/launch` | ops |
| G | `/api/v1/brokers/profiles` | ops |
| G | `/api/v1/brokers/runtimes` | ops |
| P | `/api/v1/brokers/test` | ops |
| P | `/api/v1/brokers/version-info` | ops |
| D | `/api/v1/brokers/{conn_id}` | ops |
| P | `/api/v1/brokers/{conn_id}/active` | ops |
| P | `/api/v1/brokers/{conn_id}/connect` | ops |
| P | `/api/v1/brokers/{conn_id}/disconnect` | ops |
| G | `/api/v1/brokers/{conn_id}/health` | ops |

### capabilities（能力自描述）· 3 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/capabilities` | ops |
| G | `/api/v1/capabilities/mcp` | ops |
| G | `/api/v1/capabilities/summary` | ops |

### config（配置）· 20 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/config/paths` | ops |
| U | `/api/v1/config/paths` | ops |
| P | `/api/v1/config/paths/db-backups/prune` | ops |
| P | `/api/v1/config/paths/db-backups/run` | ops |
| P | `/api/v1/config/paths/migrate-cold` | ops |
| P | `/api/v1/config/paths/validate` | ops |
| G | `/api/v1/config/risk` | ops |
| U | `/api/v1/config/risk` | ops |
| P | `/api/v1/config/risk/circuit` | ops |
| G | `/api/v1/config/risk/daily` | ops |
| G | `/api/v1/config/runtime` | ops |
| U | `/api/v1/config/runtime` | ops |
| G | `/api/v1/config/runtime/history` | ops |
| P | `/api/v1/config/runtime/reset` | ops |
| P | `/api/v1/config/runtime/rollback` | ops |
| G | `/api/v1/config/ui` | ops |
| U | `/api/v1/config/ui` | ops |
| G | `/api/v1/config/ui/export` | ops |
| P | `/api/v1/config/ui/import` | ops |
| P | `/api/v1/config/ui/reset` | ops |

### data（data）· 4 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/data/chain` | ops |
| G | `/api/v1/data/providers` | ops |
| G | `/api/v1/data/providers/health` | ops |
| G | `/api/v1/data/source/diagnostics` | ops |

### datahub（datahub）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/datahub/policies` | ops |

### factors（factors）· 4 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/factors` | ops |
| P | `/api/v1/factors/compute` | ops |
| P | `/api/v1/factors/compute/many` | ops |
| P | `/api/v1/factors/from-kline` | ops |

### health（health）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/health` | ops |

### limitup（涨停监控）· 6 端点

| M | 路径 | 标签 |
|---|------|------|
| D | `/api/v1/limitup/pool` | ops |
| P | `/api/v1/limitup/pool` | ops |
| P | `/api/v1/limitup/reset` | ops |
| P | `/api/v1/limitup/start` | ops |
| G | `/api/v1/limitup/status` | ops |
| P | `/api/v1/limitup/stop` | ops |

### live（live）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/live` | ops |

### market（行情）· 52 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/market/analysis` | ops |
| P | `/api/v1/market/analysis/run` | ops |
| G | `/api/v1/market/analysis/scripts` | ops |
| G | `/api/v1/market/board/constituents` | ops |
| G | `/api/v1/market/board/kline` | ops |
| G | `/api/v1/market/board/lookup` | ops |
| G | `/api/v1/market/board/moneyflow` | ops |
| G | `/api/v1/market/boards` | ops |
| G | `/api/v1/market/breadth` | ops |
| G | `/api/v1/market/capital` | ops |
| G | `/api/v1/market/chart-spec` | ops |
| G | `/api/v1/market/coverage` | ops |
| P | `/api/v1/market/crawl` | ops |
| G | `/api/v1/market/datasets/snapshots` | ops |
| G | `/api/v1/market/etfs` | ops |
| P | `/api/v1/market/export` | ops |
| G | `/api/v1/market/indicators` | ops |
| G | `/api/v1/market/indicators/calc` | ops |
| G | `/api/v1/market/indices` | ops |
| G | `/api/v1/market/kline` | ops |
| D | `/api/v1/market/kline/cache` | ops |
| G | `/api/v1/market/kline/cache` | ops |
| G | `/api/v1/market/kline/export` | ops |
| P | `/api/v1/market/kline/export` | ops |
| P | `/api/v1/market/kline/sync` | ops |
| G | `/api/v1/market/kline/sync-status` | ops |
| G | `/api/v1/market/l2` | ops |
| G | `/api/v1/market/limitup` | ops |
| G | `/api/v1/market/minutes` | ops |
| G | `/api/v1/market/moneyflow` | ops |
| G | `/api/v1/market/moneyflow/replay` | ops |
| P | `/api/v1/market/moneyflow/snapshot` | ops |
| G | `/api/v1/market/overview` | ops |
| G | `/api/v1/market/periods` | ops |
| P | `/api/v1/market/portfolio/aggregate` | ops |
| G | `/api/v1/market/providers` | ops |
| G | `/api/v1/market/quote` | ops |
| P | `/api/v1/market/quotes` | ops |
| G | `/api/v1/market/resolve` | ops |
| G | `/api/v1/market/rotation` | ops |
| G | `/api/v1/market/screen` | ops |
| G | `/api/v1/market/screen/boards` | ops |
| P | `/api/v1/market/screen/boards` | ops |
| P | `/api/v1/market/screen/classic` | ops |
| G | `/api/v1/market/screen/classic/picks` | ops |
| P | `/api/v1/market/screen/expr` | ops |
| P | `/api/v1/market/screen/nl` | ops |
| G | `/api/v1/market/screen/strategies` | ops |
| G | `/api/v1/market/search` | ops |
| G | `/api/v1/market/session` | ops |
| G | `/api/v1/market/sources` | ops |
| G | `/api/v1/market/stock-info` | ops |

### metrics（指标）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/metrics` | ops |

### notifications（通知）· 6 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/notifications` | ops |
| P | `/api/v1/notifications` | ops |
| P | `/api/v1/notifications/batch-delete` | ops |
| G | `/api/v1/notifications/logs` | ops |
| P | `/api/v1/notifications/test` | ops |
| D | `/api/v1/notifications/{nid}` | ops |

### paper（模拟盘）· 6 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/paper/account` | ops |
| G | `/api/v1/paper/metrics` | ops |
| P | `/api/v1/paper/order` | ops |
| G | `/api/v1/paper/positions` | ops |
| P | `/api/v1/paper/reset` | ops |
| G | `/api/v1/paper/trades` | ops |

### platform（platform）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/platform/status` | ops |

### quote-bus（quote-bus）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/quote-bus/stats` | ops |

### ready（ready）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/ready` | ops |

### rebalance（再平衡）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/rebalance` | ops |

### reconcile（reconcile）· 4 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/reconcile` | reconcile |
| G | `/api/v1/reconcile/last` | reconcile |
| P | `/api/v1/reconcile/wal/checkpoint` | reconcile |
| G | `/api/v1/reconcile/wal/stats` | reconcile |

### reference（reference）· 4 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/reference/calendar` | ops |
| G | `/api/v1/reference/financial` | ops |
| G | `/api/v1/reference/sector-stocks` | ops |
| G | `/api/v1/reference/sectors` | ops |

### research（research）· 6 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/research/attribution` | ops |
| P | `/api/v1/research/correlation` | ops |
| P | `/api/v1/research/factor-ic` | ops |
| P | `/api/v1/research/portfolio-backtest` | ops |
| P | `/api/v1/research/quantile` | ops |
| P | `/api/v1/research/walk-forward` | ops |

### runtime（运行时/任务）· 10 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/runtime/jobs` | ops |
| P | `/api/v1/runtime/jobs` | ops |
| G | `/api/v1/runtime/jobs/{job_id}` | ops |
| P | `/api/v1/runtime/jobs/{job_id}/cancel` | ops |
| G | `/api/v1/runtime/schedules` | ops |
| P | `/api/v1/runtime/schedules` | ops |
| D | `/api/v1/runtime/schedules/{schedule_id}` | ops |
| G | `/api/v1/runtime/schedules/{schedule_id}` | ops |
| U | `/api/v1/runtime/schedules/{schedule_id}` | ops |
| P | `/api/v1/runtime/schedules/{schedule_id}/trigger` | ops |

### scheduler（scheduler）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/scheduler/shutdown` | ops |

### signal（signal）· 5 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/signal/confirm` | ops |
| G | `/api/v1/signal/mode` | ops |
| P | `/api/v1/signal/mode` | ops |
| P | `/api/v1/signal/submit` | ops |
| P | `/api/v1/signal/webhook` | ops |

### strategies（策略）· 11 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/strategies/generate` | ops |
| G | `/api/v1/strategies/run` | ops |
| P | `/api/v1/strategies/run` | ops |
| P | `/api/v1/strategies/run/batch-delete` | ops |
| P | `/api/v1/strategies/run/precheck` | ops |
| D | `/api/v1/strategies/run/{run_id}` | ops |
| G | `/api/v1/strategies/run/{run_id}` | ops |
| G | `/api/v1/strategies/run/{run_id}/logs` | ops |
| P | `/api/v1/strategies/run/{run_id}/start` | ops |
| P | `/api/v1/strategies/run/{run_id}/stop` | ops |
| P | `/api/v1/strategies/save` | ops |

### strategy-market（strategy-market）· 9 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/strategy-market/catalog` | ops |
| P | `/api/v1/strategy-market/export` | ops |
| P | `/api/v1/strategy-market/export-json` | ops |
| P | `/api/v1/strategy-market/import` | ops |
| P | `/api/v1/strategy-market/import-json` | ops |
| P | `/api/v1/strategy-market/install` | ops |
| G | `/api/v1/strategy-market/market` | ops |
| G | `/api/v1/strategy-market/market/{id}` | ops |
| P | `/api/v1/strategy-market/publish` | ops |

### sync（sync）· 1 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/sync/subscribe` | ops |

### target-portfolio（目标持仓）· 5 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/target-portfolio/plans` | ops |
| P | `/api/v1/target-portfolio/plans` | ops |
| P | `/api/v1/target-portfolio/plans/batch-delete` | ops |
| D | `/api/v1/target-portfolio/plans/{pid}` | ops |
| P | `/api/v1/target-portfolio/sync` | ops |

### trade（交易）· 10 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/trade/cancel` | ops |
| G | `/api/v1/trade/conditions` | ops |
| P | `/api/v1/trade/conditions` | ops |
| P | `/api/v1/trade/conditions/{cid}/cancel` | ops |
| G | `/api/v1/trade/deals` | ops |
| P | `/api/v1/trade/order` | ops |
| G | `/api/v1/trade/orders` | ops |
| G | `/api/v1/trade/positions` | ops |
| P | `/api/v1/trade/precheck` | ops |
| P | `/api/v1/trade/target` | ops |

### wal（wal）· 2 端点

| M | 路径 | 标签 |
|---|------|------|
| P | `/api/v1/wal/checkpoint` | reconcile |
| G | `/api/v1/wal/stats` | reconcile |

### webhooks（Webhook）· 6 端点

| M | 路径 | 标签 |
|---|------|------|
| G | `/api/v1/webhooks` | ops |
| P | `/api/v1/webhooks` | ops |
| P | `/api/v1/webhooks/batch-delete` | ops |
| G | `/api/v1/webhooks/deliveries` | ops |
| D | `/api/v1/webhooks/{sid}` | ops |
| P | `/api/v1/webhooks/{sid}/test` | ops |

---

## MCP 工具（127 个）

FastMCP Streamable HTTP，接入点 `http://<host>:<port>/mcp`，携带 `X-API-Key`。工具集与 REST 的 `agent-visible` 端点保持同步（能力漂移门禁校验）。

按前缀分组（前缀 = 能力域）：


#### account_* · 1 个

| 工具 |
|------|
| `account_status` |

#### algo_* · 5 个

| 工具 |
|------|
| `algo_cancel` |
| `algo_list` |
| `algo_pause` |
| `algo_resume` |
| `algo_submit` |

#### analyze_* · 2 个

| 工具 |
|------|
| `analyze_contribution` |
| `analyze_slippage` |

#### attribute_* · 1 个

| 工具 |
|------|
| `attribute_performance` |

#### broker_* · 1 个

| 工具 |
|------|
| `broker_status` |

#### cancel_* · 2 个

| 工具 |
|------|
| `cancel_order` |
| `cancel_order_price` |

#### compare_* · 1 个

| 工具 |
|------|
| `compare_backtests` |

#### condition_* · 3 个

| 工具 |
|------|
| `condition_cancel` |
| `condition_list` |
| `condition_submit` |

#### factor_* · 3 个

| 工具 |
|------|
| `factor_correlation_matrix` |
| `factor_ic_analysis` |
| `factor_quantile_analysis` |

#### financial_* · 1 个

| 工具 |
|------|
| `financial_summary` |

#### generate_* · 2 个

| 工具 |
|------|
| `generate_rebalance` |
| `generate_strategy` |

#### get_* · 76 个

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

| 工具 |
|------|
| `l2_transactions` |

#### limitup_* · 5 个

| 工具 |
|------|
| `limitup_pool_add` |
| `limitup_pool_remove` |
| `limitup_start` |
| `limitup_status` |
| `limitup_stop` |

#### list_* · 2 个

| 工具 |
|------|
| `list_broker_profiles` |
| `list_brokers` |

#### monitor_* · 1 个

| 工具 |
|------|
| `monitor_account` |

#### monthly_* · 1 个

| 工具 |
|------|
| `monthly_pnl` |

#### net_* · 1 个

| 工具 |
|------|
| `net_value_series` |

#### order_* · 1 个

| 工具 |
|------|
| `order_target_position` |

#### place_* · 1 个

| 工具 |
|------|
| `place_order` |

#### portfolio_* · 1 个

| 工具 |
|------|
| `portfolio_backtest` |

#### query_* · 4 个

| 工具 |
|------|
| `query_cash` |
| `query_deals` |
| `query_orders` |
| `query_position` |

#### run_* · 1 个

| 工具 |
|------|
| `run_backtest` |

#### save_* · 1 个

| 工具 |
|------|
| `save_qmt_strategy` |

#### search_* · 1 个

| 工具 |
|------|
| `search_stocks` |

#### sector_* · 2 个

| 工具 |
|------|
| `sector_list` |
| `sector_stocks` |

#### sensitivity_* · 1 个

| 工具 |
|------|
| `sensitivity_analysis` |

#### target_* · 3 个

| 工具 |
|------|
| `target_portfolio_list` |
| `target_portfolio_save` |
| `target_portfolio_sync` |

#### trading_* · 1 个

| 工具 |
|------|
| `trading_calendar` |

#### walk_* · 1 个

| 工具 |
|------|
| `walk_forward_analysis` |

---

## WebSocket 事件（30 渠道 / 4 子类型）

连接 `ws://<host>:<port>/api/v1/ws?token=<API-KEY>`。服务端先推全量快照，再补发订阅代码最近 30s 行情（断线重连缺口）。客户端动作：`subscribe` / `unsubscribe` / `ping`（→ `pong`）。

| 渠道 | 子类型（payload 形状见多语言接入指南） |
|------|------------------------------------------|
| `account` | `account_snapshot` |
| `alert` | — |
| `algo_alert` | — |
| `algo_slice` | — |
| `broker.connected` | — |
| `broker.disconnected` | — |
| `condition_created` | — |
| `condition_expired` | — |
| `condition_failed` | — |
| `condition_order` | — |
| `condition_settled` | — |
| `condition_triggered` | — |
| `deal` | `deal_event` |
| `heartbeat` | — |
| `limitup` | — |
| `limitup_order` | — |
| `order` | `order_event` |
| `order.timeout` | — |
| `pong` | — |
| `quotes` | — |
| `quotes_replay` | — |
| `reconcile` | — |
| `risk` | `risk_circuit` |
| `risk.blocked` | — |
| `signal_dry_run` | — |
| `signal_live` | — |
| `signal_paper` | — |
| `signal_pending` | — |
| `snapshot` | — |
| `system` | — |

---

## 端点/工具/事件完整机读清单

- REST：`backend/tests/contracts/rest_endpoints.json`
- MCP：`backend/tests/contracts/mcp_tools.json`
- WebSocket：`backend/tests/contracts/ws_events.json`

能力自描述运行期端点：`GET /api/v1/capabilities`（REST 总览）、`GET /api/v1/capabilities/mcp`（MCP 工具分组计数）。桌面客户端「系统 → MCP 工具」页可浏览并一键复制。
