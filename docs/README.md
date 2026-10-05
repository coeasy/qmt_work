# docs/ · 文档索引

本目录只保留**当前有效、面向使用与维护**的文档。开发过程中的轮次日志、历史重构方案、竞品/早期规划等 transient 文档已统一移出版外归档（见本文件末尾说明），不再随仓库分发，避免文档与实现漂移。

> 2026-10-01 复检再清理历史无效文档 **5 份**：`atst*` 三份（基准代码属**另一项目** `coeasy/atst`，外部零引用属误植）、
> `QMT_UNIVERSAL_BROKER_PLATFORM_ARCHITECTURE_V3.md`（自述「已归档，执行口径改用 V4」）、
> `qmt_work_扩展功能任务规划.md`（无日期旧规划，属上一轮清理时漏删）。
> 判据：文档引用热力统计 + 文档自述归档标记 + 本地轮次日志；保留文档中的提及已改写为「内容并入本文 / 实体已删除」，全仓无残留断链。

> **以代码为准**。接口真实清单由 `backend/tests/contracts/*.json` 固化，CI 门禁（`ci_reconcile.py` / `check_capability_drift.py`）拦截漂移。
> 文档中的「REST / MCP / WS / 文件数 / 用例数」一律以 `python scripts/ci_reconcile.py` 与 `backend/tests/contracts/*.json` 为准，不要当作快照手抄。

---

## 一、当前有效使用指南

| 文档 | 内容 | 适用读者 |
|------|------|----------|
| [`API接口文档.md`](API接口文档.md) | **接口使用文档（REST / MCP / WebSocket）**：从契约自动生成的完整端点 / 工具 / 事件清单（REST 239 / MCP 129 / WS 30 渠道），含鉴权、零 mock、错误归因约定 | 集成方 / 全体 |
| [`QMT_大小版本使用说明.md`](QMT_大小版本使用说明.md) | **大小 QMT 使用说明**：概念差异、直连 / 策略桥（路径 B）部署步骤、接口与能力差异对照、故障排查、速查表 | 使用者 / 运维 |
| [`多语言接入指南.md`](多语言接入指南.md) | 接口接入详解：鉴权与 scope 口径、错误归因约定、Python / Node / curl 示例、端点速查表 | 集成方 |
| [`BROKER_ONBOARDING.md`](BROKER_ONBOARDING.md) | 券商接入指南：新增券商只需追加 `BrokerProfile`，适配器约定与桥接运行时 | 想接新券商的开发者 |
| [`G2_公式DSL参考.md`](G2_公式DSL参考.md) | 公式选股 DSL 语法（运算符 / 指标函数 / 例子） | 选股用户 |
| [`G4_统一数据面使用指南.md`](G4_统一数据面使用指南.md) | 统一数据面：数据源链、降级策略、`explicit:` 指定 | 数据接入开发者 |
| [`G6_任务运行时使用指南.md`](G6_任务运行时使用指南.md) | 定时任务与作业运行时 | 运维 / 自动化 |
| [`G8_NL选股使用指南.md`](G8_NL选股使用指南.md) | 自然语言选股（NL 解析，非 LLM 对话） | 选股用户 |
| [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) | 前端设计系统：色彩 / 间距 / 组件约定（对应 `frontend-next/src/design/`） | 前端开发者 |
| [`TECH_DEBT.md`](TECH_DEBT.md) | 技术债 / 未决事项看板（每条可被一条测试或一个 `grep` 证伪，含 TD-09~TD-33 等门禁约束） | 全体 |
| [`REMOTE_ACCESS_DECISION.md`](REMOTE_ACCESS_DECISION.md) | **远程访问三档模型（`off`/`lan`/`wan`）**：对比分析、启动自检分档策略、`effective_host()` 绑定规则、API 层设计（被 `core/config.py` / `app/main.py` / `routes/remote_access.py` / `run.py` 活引用） | 部署者 / 后端 |
| [`SECURITY_AND_DEPLOYMENT_AUDIT.md`](SECURITY_AND_DEPLOYMENT_AUDIT.md) | 部署与安全审计报告 + 改进方案：鉴权 / 加密 / 风控 / 信号路由 / 审计链 / CORS / 限流 / 打包 / 密钥管理逐项源码实查（v0.4.3 基线，与 TECH_DEBT 交叉核对） | 安全 / 发布 |
| [`DATASTORE_SIZE_AND_SPLIT_ANALYSIS.md`](DATASTORE_SIZE_AND_SPLIT_ANALYSIS.md) | 主库体积诊断 · 拆库评估 · 数据瘦身方案：实测结论「1.35 GB 不是膨胀、**不建议拆库**，该回收的是 `local_bars` 二级索引 383.7 MB 中 137.4 MB 冗余」（被 `scripts/optimize_local_bars.py` 活引用为依据） | 数据 / 运维 |
| [`项目规划.md`](项目规划.md) | 后续演进方向（券商广度 / 策略量化 / 交易风控 / 体验部署 / 可观测合规） | 全体 / 产品 |
| [`项目宣传.md`](项目宣传.md) | 产品宣传文档：定位、核心卖点、适用人群、三分钟上手 | 全体 / 市场 |
| [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) | 第三方依赖许可说明（TDX 传输已切换为 `easy_tdx`（MIT，商用安全）；`eltdx` Research-Only 仅作回退且默认不安装） | 合规 / 发布 |

项目入口与完整说明见仓库根 [`README.md`](../README.md)。

---

## 二、发布说明（release-notes）

| 版本 | 内容 |
|------|------|
| [`v0.4.9.md`](release-notes/v0.4.9.md) | R26：**大小 QMT 贯通审计**——逐条走读大 QMT（XtItClient + agent 文件桥）与小 QMT（MiniQMT + `userdata_mini`）的「识别 → 判活 → 取数 → 下单 → 重连 → 停机」，再全仓前后端对账。修 **8 处**，其中 1 处真缺陷：**策略取 K 线漏传 `broker_id` ⇒ 走活跃连接而非 run 绑定连接**（多连接下「信号用 A 券商数据算、订单下到 B 券商」）；其余为「形参传了但没用」族（`_infer_capabilities.client_type` / `kline_cache._where` 三形参 / `_order_status.oid` / `_normalize_params.codes` / `_order_status_cache` 孤儿属性）+ 涨跌额**缺正负号**口径不一致（`fmtSigned` 补接到 4 处展示位）+ 默认页三处各写一份（`DEFAULT_PAGE` 归一）。零 mock 契约实证：`except` 内返回成功形状命中 **0** |
| [`v0.4.8.md`](release-notes/v0.4.8.md) | R25：**断链归零 + 架构门禁回绿**——新增文档断链门禁（`audit_doc_links`，17 用例）与无出口循环/无超时等待门禁（`check_unbounded_waits`，11 用例），两条都接进 CI；Gate 4（单文件 ≤50KB）回绿靠两次 P1-1 拆分（`market.py`→`market_export.py`、`eltdx_source.py`→`eltdx_industry.py`）；过程中修掉**工具自身的两个假警报**（`.tsx` 被正则截成 `.ts` 致 20 处误报 / `read_text()` 通用换行把 CRLF 写回成 LF）；文档索引补 2 份被 5 处活代码引用却漏登记的文档；删 1 份零活引用的过期一次性计划；手抄接口计数 232/127 → **239/129** |
| [`v0.4.7.md`](release-notes/v0.4.7.md) | R24：**数据完整性专项**——新增字段级空值率审计脚本量化 15 族接口 14 处恒空，逐项根因修复：**QMT 代码形态 `.SH/.SZ` 被拒导致行业/题材静默恒空**（含 `.SZ` 后缀被丢弃的真 bug）+ **深市股票被当沪市查询**（传裸代码）+ **空结果永久污染缓存**（加 6h 重试窗口，实测旧缓存近半数为陈旧假空）+ MAC 快照 33 字段补齐（此前仅暴露 11 个，估值/市值/换手率离线全空）+ 字段映射单点化杜绝 `get_quote`/`get_instrument_detail` 漂移 + `limit_status`/`trade_date` 恒空修复 + 前端补「主力净流入」孤儿字段展示 + 市值口径统一 + 后端 2128 → **2154** 用例，审计 FAIL 0 / 未知恒空 0 |
| [`v0.4.6.md`](release-notes/v0.4.6.md) | R23：**公式执行效率优化**——求值热点消除（raw numpy 直出 + 字段列缓存，5000×250 求值 -32%）+ 公式选股结果 TTL 缓存（同公式重复执行秒级→毫秒级，`cached_result` 诚实标注）+ **修 EMA「目录可见、一算就炸」存量 bug**（新增全指标反射守卫）+ 孤儿参数清理 + 历史规划文档归档 5 份 + 后端 2115 → **2128** 用例 |
| [`v0.4.5.md`](release-notes/v0.4.5.md) | R22：**TDX 传输层切换 easy_tdx（MIT，商用安全）**——eltdx 降级为可选回退且不再随包分发；源 ID `eltdx`→`tdx`；枚举 8 分钟→2 秒（MAC 分类枚举）；ETF 清单后台清扫持久化（2199 只）；修 numpy 标量炸 API 编码器 / 名称表残表永久跳过重建两处真缺陷；商用许可按后端动态判定 | 合规 / 数据源 |
| [`v0.4.4.md`](release-notes/v0.4.4.md) | R21：**行情工作台深度重构**——成交流换真源（新增 `GET /market/ticks` 本地 TDX 当日逐笔，替代实为「本账户成交回报」的 WS `deal`）+ 五档盘口加 REST 兜底（未连券商不再恒空）+ 右栏按用途分三块（资料 / 盘口与成交 / 下单，中列底部坞移除）+ 涨跌色**三套可选**（默认红涨蓝跌）并修 K 线成交量柱未接令牌 + 清零 2 个假绿灯（能力覆盖扫描器多行泛型假报红 / 「主题切换时重建」是条不存在的路径）+ 后端 2100 → **2114**、前端 508 → **519** 用例 |
| [`v0.4.3.md`](release-notes/v0.4.3.md) | R19：**逻辑审计三轮收敛**（前后端契约贯通：界面行情契约名归一唯一入口，修「单只/指数行情缺 `price`」潜伏断链 + **3 个假绿灯清零**（能力覆盖判据改「声明不算入口、接线才算」+ 豁免反腐烂；API 契约门禁补齐 `http.del` 与嵌套泛型，可见端点 160 → **191**）+ **29 个零调用前端 API 方法**删除 + 2 条**可证伪守卫**）+ R17 bundle 完整性护栏 + R18 多标的账户类型全链贯通 + 用例 1922 → **2050** |
| [`v0.4.2.md`](release-notes/v0.4.2.md) | R15：**定时补数去浪费**（`expected_bar_date` 唯一口径 + `skip_fresh` 已最新跳过 + 单点异常不再吞掉整批 + 多策略并行/失败点名）+ **界面默认深色 + 极夜黑**（皮肤 id 中性化更名，旧 id 自动迁移）+ 用例 1912 → **1922** |
| [`v0.4.1.md`](release-notes/v0.4.1.md) | R41：**大 QMT 全量接入**（路径 B 策略桥：下单/撤单/账户/持仓/委托/成交/K线/板块/日历全可用，与 mini 同形 `BrokerAdapter` 面）+ 三轮接口与逻辑审计（14 + 6 + 2 项）+ 新增 wire action 五方对账与指标接线门禁 |
| [`v0.4.0.md`](release-notes/v0.4.0.md) | R40：公开发布版本线 —— 基于 v0.3.10 安全加固线，汇集 R35–R39 全部修复（含 TD-26 安装包密钥泄漏根因修复）+ 构建流水线运行期状态硬门禁 |
| [`v0.3.10.md`](release-notes/v0.3.10.md) | R39：主密钥改为跟随主库目录（修「安装包内嵌构建者密钥」+ 只读安装首次启动崩）+ 构建流水线运行期状态清理与包内硬门禁 |
| [`v0.3.9.md`](release-notes/v0.3.9.md) | R38：三遍深度审计（孤儿配置/悬空任务/存量 e2e 失败）+ 参数级契约门禁 + CI 门禁补齐 |

---

## 三、资源目录

- [`screenshots/`](screenshots/) —— 打包态桌面客户端实拍截图（被根 `README.md` 界面预览引用，不随仓库分发含真实账户数据的页面）。

---

## 四、审计 / 验证脚本（按需手跑，非 CI 门禁）

CI 上跑的门禁（`ci_reconcile` / `check_*` / `audit_doc_links` / `check_unbounded_waits` /
`audit_orphan_modules`）见 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)。
下表是**需要真实数据或真机、不适合放 CI** 的按需工具 —— 列出是为了避免「写了脚本
没人知道它存在」：

| 脚本 | 何时跑 | 说明 |
|---|---|---|
| `backend/scripts/audit_data_completeness.py` | 数据源改造后 | 15 族接口逐字段非空率；输出 `FAIL / 未知恒空 / 部分缺失 / 已知协议限制` 四类 |
| `backend/scripts/audit_api_payloads.py` | 改 REST 响应后 | 进程内 `TestClient` 全量打点，报字段级空值率；`--json` 导出叶子路径快照。**裁定表 `KNOWN` 是仓库唯一的「这个字段空着是对的」台账**，别处不要再造第二张 |
| `backend/scripts/audit_payload_orphans.py` | 配合上一条 | 拿 `--json` 快照当「生产者清单」，反查前端零消费的叶子（载荷漂移 / TD-31）。输出分「已判定（继承 `KNOWN`）/ 待处理」两段；`--strict` 可把「待处理非空」变成非零退出 |
| `backend/scripts/audit_orphan_modules.py` | 重构 / 删文件后（**已接 CI**） | 零引用模块扫描（产品目录内）。判据含**非 import 的引用方式**：CI `.yml` / `.bat` / `.sh` / 注释 / 文档里出现 `<产品目录>/<模块>.py` 也算被引用 —— 只认 import 会把 `tools/fetch_runtimes.py` 这类**纯命令行入口**误报成孤儿。**前端对应物**：`scripts/check_frontend_orphans.py`（零引用导出，**含 tests 消费者**；裁定表反腐烂），已接 CI |
| `backend/scripts/bench_formula_scan.py` | 改公式求值后 | 公式选股扫描基准（`--codes/--bars/--repeat`），结论由 `test_formula_perf.py` 锁定 |
| `scripts/verify_*_falsifiable.py` | 改对应门禁后 | 门禁的**证伪三例**：把实现改回旧写法，门禁必须变红 |
| `scripts/verify_gpu_safe_mode_persistence.py` | 改 Electron 启动逻辑后 | 真启三次客户端验 GPU 安全模式判定不被改写（需打包产物） |
| `scripts/verify_remote_access_safety.py` | 改鉴权/限流后 | 远程访问三档的边界校验 |
| `scripts/qmt_diag_report.py` | 现场排障 | 大 QMT 环境一键诊断报告 |

> 「脚本存在但没人知道」与「代码里的孤儿逻辑」是同一类问题：**没有消费者的生产者**。
> 新增按需脚本时请在本表补一行；确实用不上的一律删除，不要留在仓库里积灰。

---

## 文档约定

- **改代码必须同步文档**：`scripts/ci_reconcile.py` 核对测试数 / 前端组件数 / 注册页数与文档一致，漂移即失败；`--update` 可回写期望值。
- **新建文档按主题命名**，不引入带日期的轮次工作日志；新建后同步更新本索引。
- **接口清单不要手抄**：REST / MCP / WS 的完整端点来自 `backend/tests/contracts/*.json`，更新接口文档请重跑 `python backend/tests/contracts/gen_api_doc.py`。计数（当前 REST 239 / MCP 129 / WS 30）**以契约为准**，改接口后别忘同步本页与 `多语言接入指南.md` 的散落数字。

---

## 历史 transient 文档的处理说明

- **2026-10-04 复检归档 4 份历史规划**（已完成使命的方案，移入 [`archive/`](archive/)，仓库内 git 可追溯）：
  `BIG_QMT_COMPAT_PLAN.md`、`BIG_QMT_IMPLEMENTATION_PLAN.md`（大 QMT 兼容与实施——v0.4.1 已交付）、
  `UNIVERSAL_BROKER_PLATFORM_FINAL_PLAN_V4.md`、`UNIFIED_TRADING_ABSTRACTION.md`（统一券商平台 V4 规划与交易抽象选型——已实现）。
  判据：文档引用热力统计 + 交付状态核对。
- **2026-10-05 删除 1 份已过期的一次性计划**：`PROJECT_OPTIMIZATION_PLAN_2026-10-03.md`
  （基线 `VERSION`=0.4.3 的「无效文件清理 + 14 项潜在问题」任务清单，**全部条目已闭环**，
  且全仓**零活引用**——只被归档目录内部和本索引本身提到过）。
  判据与 `archive/` 保留项的区别：**保留「为什么这么设计」的论证/决策类文档**（被活代码
  注释引用为理由，删了就断链），**删除「什么时候做什么」的一次性执行计划**（使命完成即失效）。
- 归档目录内部沿用**归档前的路径**（即：把路径里的 `archive/` 段去掉，才是它当时的位置），
  这是当时布局的真实记录，不再回改；找文件请以本索引与 [`archive/README.md`](archive/README.md) 为准。

> 门禁：`python scripts/audit_doc_links.py` 扫描全仓**产品面**（docs / backend / frontend-next /
> scripts / .github）对仓库内文件的路径引用，断链即非零退出。它同时维护一份「已知有意缺失」
> 清单（运行期生成物、仓外部署名、讣告式引用、删除记录），逐条带理由——所以「0 断链」不是
> 靠放宽匹配换来的。**冻结历史源**（`docs/release-notes/`、`docs/archive/`）不参与判定：
> 发布记录与归档件是历史事实，不该用今天的目录树去判真伪。

开发过程中的轮次日志、历史重构方案、早期规划稿等 transient 文档已于 2026-09-29 统一移出仓库，
物理副本转存至**版外归档目录**（仓库之外，可恢复）：`P:/github_public/_qmt_work_removed_20260929*`。
如确需回看某篇，从对应归档目录取回；需重新纳入版本控制时再 `git add` 并补本索引条目。
