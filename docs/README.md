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
| [`QMT量化Agent平台方案.md`](QMT量化Agent平台方案.md) | 平台总体方案（架构与能力定位的长期参考） | 新成员 |
| [`qmt_work_扩展功能开发计划_修订版_2026-09-27.md`](qmt_work_扩展功能开发计划_修订版_2026-09-27.md) | 维护与功能扩展任务规划（按「零 mock / 写操作人工确认 / 单 PR 可回滚」拆分） | 全体 / 产品 |
| [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) | 第三方依赖许可说明（含 `eltdx` Research-Only 禁商用声明） | 合规 / 发布 |

项目入口与完整说明见仓库根 [`README.md`](../README.md)。

---

## 二、发布说明（release-notes）

| 版本 | 内容 |
|------|------|
| [`v0.3.8.md`](release-notes/v0.3.8.md) | R34：GitHub 发布→自动构建三处断链闭环 + 三遍深度审计 30 处修复 + 文档校准 |
| [`v0.3.7.md`](release-notes/v0.3.7.md) | R32–R33：三遍收尾审计 6+1 处修复，五道门禁全绿 |
| [`v0.3.6.md`](release-notes/v0.3.6.md) | R31：QMT 客户端升级后交易连不上 —— 严格连接校验根因闭环 + 前后端贯通 |
| [`v0.3.5.md`](release-notes/v0.3.5.md) | R30：三遍链路审计与发布 |
| [`v0.3.4.md`](release-notes/v0.3.4.md) | 功能架构梳理落地 |
| [`v0.3.3.md`](release-notes/v0.3.3.md) | 早期发布说明 |

---

## 三、资源目录

- [`screenshots/`](screenshots/) —— 打包态桌面客户端实拍截图（被根 `README.md` 界面预览引用，不随仓库分发含真实账户数据的页面）。
- `archive/` —— 已被取代的旧版文档（当前为空）。

---

## 文档约定

- **改代码必须同步文档**：`scripts/ci_reconcile.py` 核对测试数 / 前端组件数 / 注册页数与文档一致，漂移即失败；`--update` 可回写期望值。
- **新建文档请按日期或主题命名**，并同步更新本索引。
- **接口清单不要手抄**：REST / MCP / WS 的完整端点来自 `backend/tests/contracts/*.json`，如需更新接口文档，重跑生成脚本或 `python backend/scripts/gen_contracts.py` 后同步 `docs/API接口文档.md`。

---

## 历史 transient 文档的处理说明

2026-09-29 文档精简：带日期的轮次开发日志（`2026-09-18` ~ `2026-09-29` 共 22 篇）、
以及 `2026-08-26/08-27` 的早期规划稿（`二轮复查`、`功能层问题`、`基于deepseek-harness 的客户端重构`、`竞品对比` 共 4 篇）
已从仓库移除。这些文件属于开发过程中的 working log / 已被取代的方案，不构成使用者或维护者需要的参考文档。

其物理副本统一转存至**版外归档目录**（仓库之外，可恢复）：

```
P:/github_public/_qmt_work_removed_20260929b/   # 本轮（22 跟踪 + 4 未跟踪）
P:/github_public/_qmt_work_removed_20260929/    # 上轮（42 篇）
```

如确需回看某篇，从对应归档目录取回即可；需要重新纳入版本控制时再 `git add` 并补本索引条目。
