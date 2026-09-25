# qmt_work 维护与功能扩展任务规划

> 目标项目：<https://github.com/coeasy/qmt_work>
> 规划基线：main 分支最新代码（含 v9 重构总纲落地态，2026-09 快照）
> 规划原则：**零 mock**（未连真实券商/数据源一律 503 或显式降级）、**写操作保留人工确认护栏**、**每个任务单 PR 可回滚、带测试与门禁**

---

## 0. 一句话定位

把 qmt_work 从「能跑通 QMT 的多功能工作台」，推进成「**多券商 + 多数据源 + Agent 可安全编排**」的本地量化平台：数据面可脱离 QMT 独立运行，交易面在真实 SDK 上统一收敛，AI 只读能力全开、写能力走两阶段确认。

---

## 1. 现状盘点（基于代码的事实）

### 1.1 已落地（不要重复造）

| 能力 | 落点 |
|---|---|
| REST /api/v1/\*（30+ 路由模块）、统一 `{code,message,data}` 包 | `backend/app/routes/` |
| MCP（FastMCP Streamable HTTP）+ GET 端点自动暴露 | `backend/mcp_server/__init__.py`、`auto_expose.py` |
| 能力自描述注册表 + 能力漂移门禁 | `backend/app/capabilities.py`、`scripts/check_capability_drift.py` |
| 连接器契约（ConnectorPort / OrderRequest / Snapshot） | `backend/connectors/ports.py`，QMT 实现在 `connectors/qmt.py` |
| 多数据源目录与可选源（pytdx / baostock / eltdx / sina / tencent） | `backend/datasource/providers.py`、`optional_sources.py`、`public_sources.py` |
| 本地数据仓（local_bars / local_stock_list / local_sync_meta / dataset_snapshots / exchange_calendar） | `backend/core/db_migrations.py` |
| 任务运行时 JobRuntime（配额 + 优先级 + 取消 + 持久化） | `backend/app/runtime/jobs.py` |
| 风控闸门（单笔/比例/频率/日级熔断/黑白名单/价格偏离） | `backend/gateway/risk.py` |
| 回测（向量化 + 参数扫描 + 佣金/印花税/滑点成本模型） | `backend/tools/backtest.py`、`tools/matching.py` |
| 模拟盘、涨停监控、TWAP/VWAP、条件单、再平衡、目标持仓、对账 WAL、审计 hash 链 | `backend/engines/`、`backend/gateway/` |
| 前端 7 大 Hub（hubs/\*.jsx）+ 页面注册表 + 门禁测试 | `frontend/src/` |
| 打包发布（PyInstaller EXE + Electron NSIS + CI/release workflow） | `backend/build_exe.py`、`.github/workflows/` |

### 1.2 已识别缺口（本规划的任务来源）

1. **旧兼容 shim 未清**：`app/config.py`(12行) / `app/db.py`(9行) / `app/state.py`(5行) / `app/db_migrations.py`(5行) / `app/crypto.py`(62行) 与 `core/*` 并存，全仓仍有 **14 处 `from app.*` 旧引用**（`core.*` 为 85 处），双真源易漂移。
2. **无调度器**：仓库内无 cron / durable scheduler 实现（`grep scheduler` 仅命中 capabilities、main、calendar、providers），`exchange_calendar`、`local_sync_meta` 表已就位但**没有 EOD 日终自动同步与关机补跑**。
3. **可选数据源质量不足**：
   - `BaoStockSource.get_kline` 中 `code.lower().replace(".", ".")` 为空操作，未转成 baostock 要求的 `sh.600000` 格式 → 调用必然失败；
   - `count` 未下推到查询，先拉全历史再截断；
   - `TstdxSource` 仅日线（category=9）、无复权、无分钟线；服务器硬编码 `119.147.212.81:7709`；**每次请求新建 TCP 连接**。
4. **第二家真实连接器缺失**：`connectors/` 只有 QMT，同花顺 / PTrade / 掘金仍停留在 README 的「契约待接」。
5. **MCP 只暴露只读 GET**：`_EXEC_DENY_DOMAINS` 明确不自动暴露写操作 → Agent 侧无法完成「下单」闭环（目前只能看不能做）。
6. **插件内核停在声明式**：`plugins/kernel.py` 只做 manifest 校验与状态机，注释中写明「执行委托给未来的 subprocess/RPC host」，尚无真正加载。
7. **回测为单标的**：`tools/backtest.py` 单 symbol，无组合级（多标的权重 + 资金曲线 + 换手/成本归因）。
8. **文档负债**：`docs/` 下 20+ 份按日期命名的方案稿（v1~v9 + archive），缺少面向使用者的结构化站点。

---

## 2. 任务总览

| ID | 任务 | 阶段 | 工作量 | 优先级 | 依赖 |
|---|---|---|---|---|---|
| A1 | CI 真绿 + 四大门禁进 pre-commit | 治理 | 1d | P0 | — |
| A2 | 清除 app/\* 兼容 shim，统一到 core/\* | 治理 | 2d | P0 | A1 |
| A3 | 关键路径单测补齐 + 覆盖率门槛 | 治理 | 2d | P1 | A2 |
| B1 | 可选数据源质量修复（baostock/pytdx） | 数据面 | 2d | P0 | — |
| B2 | 数据仓增量同步 + 断点续传 + 缺口补齐 | 数据面 | 2d | P0 | B1 |
| B3 | 调度器 + EOD 日终同步 + 关机补跑 | 数据面 | 3d | P0 | B2 |
| B4 | 多源质量对账（跨源差异 / 缺失 / finality） | 数据面 | 2d | P1 | B3 |
| C1 | 第二真实连接器（契约驱动 + 契约测试） | 交易 | 5d | P0 | A2 |
| C2 | MCP 两阶段确认写工具（dry-run → token → 执行） | 交易 | 2d | P0 | A1 |
| C3 | 风控增强（行业集中度 / 移动止损 / 涨跌停 / 分级熔断） | 交易 | 3d | P1 | A2 |
| C4 | 算法单扩展（IS / 冰山 / 随机化 / 参与度上限） | 交易 | 3d | P2 | A2 |
| D1 | 组合级回测（多标的 + 权重 + 归因） | 研究 | 4d | P1 | A3 |
| D2 | 过拟合诊断（walk-forward / PBO / deflated Sharpe） | 研究 | 2d | P2 | D1 |
| D3 | 因子平台化（持久化 + 合成 + 衰减监控） | 研究 | 3d | P2 | B3 |
| D4 | 策略市场安全加固（签名 + 静态扫描 + 沙箱） | 研究 | 2d | P1 | — |
| E1 | Plugin Host（子进程 + RPC + 权限落地） | 平台 | 5d | P2 | A2 |
| E2 | 多账户网格增强（组合视图 + 批量原子性） | 平台 | 3d | P1 | C1 |
| E3 | 可观测性补全（数据源 SLI + OTel trace + trace_id） | 平台 | 2d | P2 | B1 |
| F1 | 前端 Hub 对齐收尾 + i18n + 键盘流 | 前端 | 3d | P2 | A1 |
| F2 | 发布流水线（版本号统一 + 签名 + 自动更新） | 前端 | 2d | P1 | A1 |
| F3 | 文档站重建（快速开始 / 架构 / API / MCP / 数据源 / 券商接入） | 前端 | 2d | P2 | — |

合计约 **51 人日**（AI 承担 60%~70% 编码，人负责验收与真机演练）。

---

## 3. 任务卡

### A1 · CI 真绿 + 四大门禁进 pre-commit（1d，P0）

- **背景**：v9 Phase 0 要求「恢复并锁定 CI 真绿」，已有 4 个门禁脚本但未强制绑定开发流程。
- **改动**：
  - `.github/workflows/ci.yml`：后端 `ruff check` + `pytest`（含 `tests/smoke2.py`）与前端 `npm run build` + `vitest` 并行 job，任一失败即红。
  - 门禁脚本：`backend/scripts/check_capability_drift.py`（能力漂移）、`check_execution_architecture.py`（执行主链不得绕过 gateway）、`check_frontend_classnames.py`（样式类名白名单）、`check_licenses.py`（依赖许可证）。
  - `scripts/install-git-hooks.ps1` + `post-commit.hook`：本地提交前跑 ruff + 4 门禁（秒级）。
- **验收**：故意引入一处「新增 GET 路由但未注册 MCP tool」的改动 → CI 漂移门禁失败；撤销后全绿。
- **风险**：Windows/macOS 钩子差异 → 提供 `pre-commit` (Python) 版本兜底。

### A2 · 清除 app/\* 兼容 shim，统一到 core/\*（2d，P0）

- **改动**：把 14 处 `from app.config/db/state/crypto/db_migrations import ...` 改为 `core.*`；删除 `app/config.py`、`app/db.py`、`app/state.py`、`app/db_migrations.py`、`app/crypto.py`（确认为纯 shim 后）；删除重复引擎入口 `paper/paper_engine.py`（6 行，真身在 `engines/paper_engine.py` 479 行）与 `engines/` 与 `paper/` 的二选一。
- **配套**：新增测试 `tests/test_no_legacy_imports.py`，扫描全仓禁止 `from app.config` 等旧路径；加进 A1 门禁。
- **验收**：`grep -rn "from app\.\(config\|db\|state\|crypto\|db_migrations\)"` 返回 0；`pytest` 全绿；EXE 打包（`build_exe.py`）后启动 `run.py` 冒烟通过。
- **风险**：PyInstaller hiddenimports 引用了旧路径 → `qmt_work.spec` 同步更新。

### A3 · 关键路径单测补齐 + 覆盖率门槛（2d，P1）

- **范围（按风险排序）**：`gateway/risk.py`（方向归一化、熔断只许卖出、黑白名单）、`gateway/execution.py`（幂等 + 风控 + 审计顺序）、`gateway/idempotency.py`、`gateway/order_watchdog.py`、`engines/algo.py`（TWAP/VWAP 拆分与暂停恢复）、`engines/limitup.py`（三因子触发）、`datasource/degrade.py`（降级不得伪造数据）、`app/runtime/jobs.py`（配额/取消/持久化）。
- **验收**：`pytest --cov=backend --cov-fail-under=70`（先设 70%，随 A/B 阶段推进提到 80%）；每个用例禁止网络与真实 SDK，用 `tests/fake_bridge_server.py` 模式。
- **纪律**：命中真实 SDK 的用例统一标记 `@pytest.mark.broker`，CI 默认跳过、真机演练时手动执行。

### B1 · 可选数据源质量修复（2d，P0）

- **B1-1 baostock 代码格式**：新增 `_to_bs_code(code)`：`600000.SH → sh.600000`、`000001.SZ → sz.000001`、`8xxxxx.BJ → bj.xxxxx`；`get_stock_list` 返回的 `code` 统一回 `XXXXXX.EX` 规范。单测断言映射表（纯函数，无需装 baostock）。
- **B1-2 count 下推**：`query_history_k_data_plus` 增加 `start_date/end_date`（按 count 与交易日历反推区间），不再全量拉取后截断。
- **B1-3 复权三态对齐**：`adjust ∈ {none, qfq, hfq}` → baostock `adjustflag ∈ {3,2,1}`；pytdx 无复权能力则**显式声明不支持并降级**（`datasource/degrade.py` 记录 reason），禁止返回未经复权却标记为 qfq 的数据。
- **B1-4 pytdx 分钟线**：category 映射 `9=日线 / 7|8=1min / 0=5min / 1=15min / 2=30min / 3=60min`，`get_kline(period)` 支持 `1m/5m/15m/30m/60m/1d`；不支持周期返回明确错误而非空列表。
- **B1-5 连接治理**：pytdx 服务器列表可配置（`QMT_TDX_SERVERS`），启动时连通性探测择优 + 失败轮转；连接复用（`asyncio.Lock` 保护的长连接池）+ 单次调用超时（默认 5s）+ 指数退避重试 2 次；每次失败写 `metrics`。
- **验收**：`pytest -m optional`（装了可选依赖才跑）通过；未装依赖时 `providers.describe()` 状态为 `unavailable` 且 `/api/v1/datahub/providers` 能给出可执行修复提示（不发假数据）。

### B2 · 数据仓增量同步 + 断点续传（2d，P0）

- **改动**：`datasource/local_store.py` + `app/sync/bars.py` + `app/sync/calendar.py`
  - 以 `local_sync_meta` 记录每个 (provider, symbol, period) 的 `last_bar_time` 与 `checksum`；
  - 同步任务按「缺口区间」请求：`last_bar_time → 今日`，缺失段批量补（`local_bars` 唯一索引去重 upsert）；
  - 大任务切分为 200 标的/批，JobRuntime（`sync` 配额=1）串行执行，支持断点续跑（重启后从 `local_sync_meta` 续）；
  - 每次批量写入生成一条 `dataset_snapshots`（coverage 起止、row_count、checksum、quality_state）。
- **验收**：人为删除中间 10 天数据后触发同步 → 只补缺口 10 天（日志可证），`local_bars` 无重复行；杀进程重启后续跑完成。

### B3 · 调度器 + EOD 日终同步 + 关机补跑（3d，P0）

- **新增**：`backend/app/scheduler/`（`models.py` 任务定义 / `cron.py` 5 段或 6 段 cron 解析 / `engine.py` 基于 asyncio 的 tick + 持久化 / `routes.py` `/api/v1/scheduler/*`），表 `scheduled_jobs(id, name, cron, kind, params_json, enabled, last_run_at, next_run_at, last_status, owner)`。
- **规则**：
  - 触发条件 = cron 命中 **且** `exchange_calendar` 当日为交易日（非交易日跳过并记 skip）；
  - 内置系统任务：`eod_daily_sync`（每交易日 15:30，全市场日线入库）、`calendar_refresh`、`kline_cache_vacuum`、`db_backup`；
  - 关机补跑：启动时比对 `last_run_at` 与交易日历，缺失交易日按序补跑（并发遵循 JobRuntime 配额）；
  - 所有调度执行经 JobRuntime，前端「任务中心」可见历史与下次执行时间。
- **验收**：单测覆盖 cron 解析（含月末/时区）、交易日过滤、补跑顺序；集成测试用假时钟跑完 `eod_daily_sync` 并断言 `local_bars` 行数；`GET /api/v1/capabilities` 出现 scheduler 域且 MCP 自动暴露 GET 端点（漂移门禁不报缺失）。

### B4 · 多源质量对账（2d，P1）

- **改动**：新增 `datasource/reconcile.py` + 路由 `/api/v1/datahub/quality`：
  - 同一 (symbol, period, bar_time) 跨源比对 close/volume，差异超阈值（默认 0.5%）写入 `quality_state=dubious` 并触发告警规则；
  - 停牌 / 长期无成交 / 缺失交易日检测 → `quality_state=gap`；
  - 数据 finality：当日收盘后 N 分钟（默认 15:40）标记 `final`，此前为 `provisional`，回测默认只用 `final`（可显式覆盖）。
- **验收**：构造两源不一致 fixture → 告警与 `quality_state` 正确；回测使用 provisional 数据时返回显式提示。

### C1 · 第二真实连接器（5d，P0）

- **策略**：不改 `ports.py` 契约，只加实现 + 注册。首选**通用 HTTP 券商网关**（复用 `connectors/http.py` + `tests/fake_bridge_server.py` 的协议形态），把「迅投之外」的第二类接入做成可插拔适配器 `connectors/http_broker.py`；easytrader 等 GPL 依赖**只做可选适配器且默认不打包**（先过 `check_licenses.py`）。
- **实现要点**：
  - 类 `HttpBrokerConnector` 实现 `ConnectorPort` 全方法：`start/close/is_connected/place_order/cancel_order/quote/positions/orders/account`（契约以 `connectors/ports.py` 现有方法为准，缺什么补什么并同步补 `ports.py`）；
  - 状态机 `ConnectorState`（DISCONNECTED→STARTING→CONNECTED→DEGRADED→FAILED），心跳 30s、指数退避重连（1s→30s）、连续 3 次失败转 DEGRADED 并推送通知；
  - client_order_id 生成 + 幂等（复用 `gateway/idempotency.py`），订单状态机映射（pending/partial/filled/canceled/rejected）；
  - 账户类型与能力在 `ConnectorDescriptor.capabilities` 声明，未支持能力走 501 + 明确文案（不允许静默降级）。
- **测试**：扩展 `tests/test_connectors_phase8.py`，把「契约一致性测试集」参数化到所有已注册 connector，新增实现零改动即可复用；真机演练单单独归档（不进 CI）。
- **验收**：`tests/test_connectors_phase8.py` 对 QMT 与新连接器同时通过；前端「券商连接」页无需改组件即可出现新券商；`/api/v1/capabilities` 的 connectors 域列出双连接。

### C2 · MCP 两阶段确认写工具（2d，P0）

- **动机**：`auto_expose.py` 只暴露 GET，Agent 侧「只能看不能做」。补一条安全闭环，而不是粗暴放开写操作。
- **设计**：
  1. `mcp_server/` 新增手写工具 `propose_order(...)`：只做**校验 + 风控预检 + 生成 dry-run 预览**（预计金额、可用资金、触发的风控规则、滑点估计），返回 `confirm_token`（HMAC-SHA256 签名，载荷 = 参数 hash + 账户 + 过期时间，TTL 90s，一次性）；
  2. `confirm_order(confirm_token)`：二次校验 token 有效 + 参数未篡改 + 幂等键未使用 → 走 `gateway/execution.py` 正式下单；
  3. token 无效/过期/参数变更一律拒绝并审计（写入 `audit_log` 与 `metrics`）。
- **配套**：扩展 `check_capability_drift.py` → 新增断言：任何 MCP 写工具必须名含 `propose_/confirm_` 且经 token 校验；禁止直接暴露裸 `place_order`。
- **验收**：单测覆盖 token 过期、参数篡改、重放；`pytest` + 漂移门禁通过；Cursor/Claude 侧实测「问一句 → 拿到预览 → 确认 → 成交」闭环（真机演练单记录）。

### C3 · 风控增强（3d，P1）

- **新增规则**（全部可运行期配置，落 `risk_config`）：
  - 单票/单行业/单板块集中度上限（行业分类取 `datasource/board.py` + `reference.py` 现有映射，缺失行业显式拒绝买入而非放行）；
  - 移动止损 / 止盈：持仓高水位回撤 pct 触发，dry-run 与实盘双模式，触发后经 `engines/condition_order.py` 或告警推送；
  - 涨跌停、停牌、上市首日、ST 标识过滤（下单前用最新 tick 判定，取不到行情**拒绝下单**而非放行）；
  - 分级熔断：账户日回撤达 warn 阈值 → 告警 + 降额；达 halt 阈值 → 只许卖出（沿用 `risk.py` 现有熔断语义，扩成多级）；
  - T+1 可用数量校验与融券/两融标记位预留。
- **验收**：每条规则 2~4 个单测（含边界与「数据不可用时必须拒绝」）；`/api/v1/config/risk` 读写与热生效链路测试；真机演练单补一节「风控拒绝场景」。

### C4 · 算法单扩展（3d，P2）

- 在 `engines/algo.py` 现有 TWAP/VWAP 之上新增：IS（Implementation Shortfall，按到达价偏差动态调整激进/被动）、冰山单（可见量 + 随机补量）、随机化时间/数量抖动、参与度上限（不超过区间成交量 pct）；统一抽象为 `ChildOrderPolicy`，调度器共用一套「暂停/恢复/取消/超时」控制面与审计事件。
- **验收**：用历史 tick/分钟线做事件驱动回放测试，断言成交均价优于/不劣于基线且参与度和时间窗口受控。

### D1 · 组合级回测（4d，P1）

- **现状**：`tools/backtest.py` 为单 symbol（成本模型、撮合内核 `tools/matching.py` 已可用）。
- **新增** `tools/portfolio_backtest.py`：标的池 + 目标权重序列（支持等权/市值加权/IC 加权/自定义 CSV）、再平衡频率、现金账户与分红送转预留位、换手与成本归因（佣金/印花税/滑点分项）、组合净值 + 回撤 + 换手 + 暴露度；复用 `tools/matching.py` 保证与单标的回测口径一致（已有 `test_backtest_vectorized.py` 的「向量化≡逐根」一致性纪律要扩展到组合）。
- **路由**：`/api/v1/backtest/portfolio`，前端回测页增加「组合模式」tab。
- **验收**：一致性测试（组合按 100% 单一标的 ≡ 单标的回测结果，误差 < 1e-6）；参数扫描复用现有 JobRuntime backtest 配额。

### D2 · 过拟合诊断（2d，P2）

- 在 `tools/factor_research.py` / `routes/research.py` 之上补：purged walk-forward（含 embargo）、PBO（过拟合概率）、deflated Sharpe；输出「参数敏感性热力面 + 样本内外衰减」图表数据。
- **验收**：对已知随机信号（无预测力）跑诊断，PBO 需判定为过拟合（回归测试防退化）。

### D3 · 因子平台化（3d，P2）

- 因子注册表持久化（表 `factor_defs`：`id / dsl / category / owner / version / 依赖字段`），支持 `indicators/dsl.py` 定义的公式因子入库；因子合成（等权 / IC 加权 / 施密特正交化）；因子衰减监控（滚动 IC/ICIR 曲线 + 衰减告警）；因子值缓存到 `local_bars` 同构表，避免重复计算。
- **验收**：因子从 DSL 定义 → 落库 → 计算 → IC 评估 → 合成 → 回测调用全链路单测；`check_capability_drift` 覆盖新端点。

### D4 · 策略市场安全加固（2d，P1）

- zip/json 导入流程（`tools/strategy_market.py`）：强制 manifest（name/version/sdk 版本/声明的权限）、导入前静态扫描（禁止 `import os/subprocess/socket/requests` 与动态 `exec/eval`、`open` 写路径），扫描器用 AST 实现并单测覆盖绕过样例；可选作者签名（HMAC，公钥在配置中）；市场页展示版本、依赖、评分与安全扫描结论。
- **验收**：构造 5 个恶意/边界样例 zip，全部被拒并给出可读原因；合法样例可导入并在模拟盘运行。

### E1 · Plugin Host（5d，P2）

- 把 `plugins/kernel.py` 从「声明式」升级为真正执行：子进程 + 本地 RPC（Unix socket / Windows named pipe 或 stdio JSON-RPC），权限由 `plugins/permissions.py` 在 host 侧强制（插件拿不到 DB 与 broker 对象）；首批只开放「策略插件」一类（输入：行情 DataFrame + 参数；输出：目标权重/信号），超时与内存上限、崩溃隔离与自动重启、插件 API 版本协商。
- **验收**：一个样例插件在沙箱中被故意 `os.system` / 无限循环 / 崩溃 → host 侧安全拦截或隔离重启，主进程不受影响。

### E2 · 多账户网格增强（3d，P1）

- 跨账户组合视图（合并持仓、行业暴露、合并净值曲线）；批量下单的原子性语义明确化（all-or-nothing 预检 + 部分失败生成「补偿建议」而非静默跳过，落 `rebalance_orders` 与审计）；账户组权限（API key 绑定账户白名单，见 `gateway/apikey.py`）。
- **验收**：批量下单 3 账户中 1 个失败 → 状态、补偿建议、审计链路正确；无脏单。

### E3 · 可观测性补全（2d，P2）

- `gateway/metrics.py` 增加数据源 SLI（每 provider 的 p50/p95 延迟、失败率、降级次数）、订单生命周期直方图、WS 扇出连接数；请求 `trace_id`（`middleware/request_id.py` 已有）贯穿 REST / WS / MCP / 调度任务；可选 OpenTelemetry 导出（默认关闭）。
- **验收**：`/api/v1/metrics` 新增指标可读；一次下单在日志中可用同一 trace_id 串起 REST→风控→执行→审计。

### F1 · 前端 Hub 对齐收尾 + i18n + 键盘流（3d，P2）

- 按 `pagesRegistry.jsx` 单一真源核对 7 个 Hub 的 31 个能力入口与后端端点一一对应（用 `check_capability_drift.py` 的反向校验：前端有页面但后端无端点 → 报警）；`lib/i18n.js` 中英切换补齐未翻译串；全局命令面板（`CommandPalette.jsx`）与快捷键覆盖主要操作。
- **验收**：`npm run build` + `vitest` 通过，`classNameGate.test.jsx` 门禁通过；人工走查 21 个顶层页无空白页。

### F2 · 发布流水线（2d，P1）

- `app/version.py` 作为唯一版本源，注入后端 `/api/v1/health` 与前端/ Electron 包版本；`.github/workflows/release.yml` 产出 EXE + NSIS + zip + `latest.yml`，签名（CSC）可选但流程固化；产物校验脚本 `scripts/verify_artifacts.py`（校验和 + 版本号一致性 + 启动冒烟）。
- **验收**：tag 触发一次 dry-run release，产物齐全、`verify_artifacts.py` 全绿；未配置签名时跳过签名并在发布说明中标注。

### F3 · 文档站重建（2d，P2）

- 将 `docs/` 下按日期命名的方案稿（v1~v9 与 archive/*）收敛到 `docs/archive/planning/`，新建 `docs/site/`：快速开始 / 架构总览 / 数据面（数据源与同步）/ 交易面（执行主链与风控）/ MCP 用法 / 券商接入指南（新增券商的 3 步）/ FAQ / 真机演练单。`BROKER_ONBOARDING.md`、`G4/G6/G8` 指南迁入对应章节。
- **验收**：新人按「快速开始」在干净 Windows 机器上 30 分钟内跑通行情查询（文档准确性由真机演练单背书）。

---

## 4. 推荐执行顺序与里程碑

| 里程碑 | 时间盒 | 内容 | 出口标准 |
|---|---|---|---|
| **M1 治理与数据正确性** | 第 1–2 周 | A1 → A2 → B1 → B2 | CI 全绿 + 门禁进 pre-commit；旧 shim 清零；可选源可用且不再发假数据 |
| **M2 数据自驱** | 第 3–4 周 | B3 → B4 → A3 | 每交易日自动落库、关机补跑、质量可观测；覆盖率 ≥ 70% |
| **M3 交易面扩边界** | 第 5–7 周 | C1 → C2 → C3 | 第二连接器过契约测试；Agent 能安全下单（两阶段确认）；风控规则齐备 |
| **M4 研究与平台** | 第 8–11 周 | D1 → D4 → E2 → C4 → F2 | 组合回测可用、策略市场安全、多账户批量可控、发布流水线固化 |
| **M5 长期** | 第 12 周+ | E1 / E3 / D2 / D3 / F1 / F3 | 插件可隔离运行、可观测性完整、文档站上线 |

---

## 5. 交给 AI 执行的协作规范

1. **任务粒度**：一个任务 = 一个 PR；PR 描述写清「改动文件 / 行为变化 / 验证方式」。
2. **先测试后代码**：给 AI 的指令顺序固定为「读这几个文件 → 写/改测试 → 实现 → 跑 ruff + pytest + 门禁」。
3. **零 mock 红线写进指令**：任何时候不得为「让测试通过」返回伪造行情或假成交；未连接真实源一律返回错误码 + 修复提示。
4. **验证闭环（每个 PR 必跑）**：
   ```
   cd backend && ruff check . && pytest -q
   python scripts/check_capability_drift.py
   python scripts/check_execution_architecture.py
   python scripts/check_licenses.py
   python tests/smoke2.py            # 全链路冒烟
   cd ../frontend && npm run build && npx vitest run
   ```
5. **真机项单独管理**：涉及真实券商/行情源的验证不进 CI，用 `docs/` 中的真机演练单记录（操作人、截图、结论），`@pytest.mark.broker` 手动执行。
6. **AI 产出必查项**：是否绕过 `gateway/execution.py` 直接调适配器下单；是否在数据源不可用时静默返回空/假数据；是否新增了未注册的 GET 端点（漂移门禁会拦）。

---

## 6. 风险与红线

- **合规**：接入第三方 SDK/数据源前先过 `check_licenses.py`，GPL/AGPL 依赖默认不进主包（可选安装 + 独立进程）。
- **资金安全**：任何新增写能力默认 `dry-run`，实盘开关需显式配置 + 二次确认 + 审计；MCP 侧永不自动执行。
- **数据正确性优先于功能数量**：B 组任务全部完成前，不做 D 组研究功能的推广。
- **Windows 依赖**：xtquant 只能在 Windows + QMT 同机运行，任何改动必须保留「无 QMT 纯研究模式」可通过（v9 第 9 节验收场景 A）。
