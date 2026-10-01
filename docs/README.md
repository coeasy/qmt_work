# docs/ · 文档索引

本目录只保留**当前有效、面向使用与维护**的文档。开发过程中的轮次日志、历史重构方案、竞品/早期规划等 transient 文档已统一移出版外归档（见本文件末尾说明），不再随仓库分发，避免文档与实现漂移。

> **以代码为准**。接口真实清单由 `backend/tests/contracts/*.json` 固化，CI 门禁（`ci_reconcile.py` / `check_capability_drift.py`）拦截漂移。
> 文档中的「REST / MCP / WS / 文件数 / 用例数」一律以 `python scripts/ci_reconcile.py` 与 `backend/tests/contracts/*.json` 为准，不要当作快照手抄。

---

## 一、当前有效使用指南

| 文档 | 内容 | 适用读者 |
|------|------|----------|
| [`API接口文档.md`](API接口文档.md) | **接口使用文档（REST / MCP / WebSocket）**：从契约自动生成的完整端点 / 工具 / 事件清单（REST 232 / MCP 127 / WS 30 渠道），含鉴权、零 mock、错误归因约定 | 集成方 / 全体 |
| [`多语言接入指南.md`](多语言接入指南.md) | 接口接入详解：鉴权与 scope 口径、错误归因约定、Python / Node / curl 示例、端点速查表 | 集成方 |
| [`BROKER_ONBOARDING.md`](BROKER_ONBOARDING.md) | 券商接入指南：新增券商只需追加 `BrokerProfile`，适配器约定与桥接运行时 | 想接新券商的开发者 |
| [`G2_公式DSL参考.md`](G2_公式DSL参考.md) | 公式选股 DSL 语法（运算符 / 指标函数 / 例子） | 选股用户 |
| [`G4_统一数据面使用指南.md`](G4_统一数据面使用指南.md) | 统一数据面：数据源链、降级策略、`explicit:` 指定 | 数据接入开发者 |
| [`G6_任务运行时使用指南.md`](G6_任务运行时使用指南.md) | 定时任务与作业运行时 | 运维 / 自动化 |
| [`G8_NL选股使用指南.md`](G8_NL选股使用指南.md) | 自然语言选股（NL 解析，非 LLM 对话） | 选股用户 |
| [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) | 前端设计系统：色彩 / 间距 / 组件约定（对应 `frontend-next/src/design/`） | 前端开发者 |
| [`TECH_DEBT.md`](TECH_DEBT.md) | 技术债 / 未决事项看板（每条可被一条测试或一个 `grep` 证伪，含 TD-09~TD-25 等门禁约束） | 全体 |
| [`项目规划.md`](项目规划.md) | 后续演进方向（券商广度 / 策略量化 / 交易风控 / 体验部署 / 可观测合规） | 全体 / 产品 |
| [`项目宣传.md`](项目宣传.md) | 产品宣传文档：定位、核心卖点、适用人群、三分钟上手 | 全体 / 市场 |
| [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) | 第三方依赖许可说明（含 `eltdx` Research-Only 禁商用声明） | 合规 / 发布 |

项目入口与完整说明见仓库根 [`README.md`](../README.md)。

---

## 二、发布说明（release-notes）

| 版本 | 内容 |
|------|------|
| [`v0.4.1.md`](release-notes/v0.4.1.md) | R41：**大 QMT 全量接入**（路径 B 策略桥：下单/撤单/账户/持仓/委托/成交/K线/板块/日历全可用，与 mini 同形 `BrokerAdapter` 面）+ 三轮接口与逻辑审计（14 + 6 + 2 项）+ 新增 wire action 五方对账与指标接线门禁 |
| [`v0.4.0.md`](release-notes/v0.4.0.md) | R40：公开发布版本线 —— 基于 v0.3.10 安全加固线，汇集 R35–R39 全部修复（含 TD-26 安装包密钥泄漏根因修复）+ 构建流水线运行期状态硬门禁 |
| [`v0.3.10.md`](release-notes/v0.3.10.md) | R39：主密钥改为跟随主库目录（修「安装包内嵌构建者密钥」+ 只读安装首次启动崩）+ 构建流水线运行期状态清理与包内硬门禁 |
| [`v0.3.9.md`](release-notes/v0.3.9.md) | R38：三遍深度审计（孤儿配置/悬空任务/存量 e2e 失败）+ 参数级契约门禁 + CI 门禁补齐 |

---

## 三、资源目录

- [`screenshots/`](screenshots/) —— 打包态桌面客户端实拍截图（被根 `README.md` 界面预览引用，不随仓库分发含真实账户数据的页面）。

---

## 文档约定

- **改代码必须同步文档**：`scripts/ci_reconcile.py` 核对测试数 / 前端组件数 / 注册页数与文档一致，漂移即失败；`--update` 可回写期望值。
- **新建文档按主题命名**，不引入带日期的轮次工作日志；新建后同步更新本索引。
- **接口清单不要手抄**：REST / MCP / WS 的完整端点来自 `backend/tests/contracts/*.json`，更新接口文档请重跑 `python backend/tests/contracts/gen_api_doc.py`。

---

## 历史 transient 文档的处理说明

开发过程中的轮次日志、历史重构方案、早期规划稿等 transient 文档已于 2026-09-29 统一移出仓库，
物理副本转存至**版外归档目录**（仓库之外，可恢复）：`P:/github_public/_qmt_work_removed_20260929*`。
如确需回看某篇，从对应归档目录取回；需重新纳入版本控制时再 `git add` 并补本索引条目。
