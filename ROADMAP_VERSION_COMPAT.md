# qmt_work · 版本兼容与开源生态对标分析

> 生成时间：2026-08-15
> 当前发布：**v0.1.1（2026-08-15）— ABI 探测修复版**
> 结论先行：用户本机 Python 版本与打包**完全无关**；真正的约束是「打包时内嵌的 Python 运行时 ABI」必须落在「券商 xtquant 自带的 .pyd ABI 集合」内。最优解不是按版本打多个包，而是**主程序单包 + xtquant 桥接层多运行时（cp38~cp312）自动择机**。

---

## v0.1.1 变更（2026-08-15）— ABI 探测修复

- **根因**：`probe_environment` 在主后端 Python 3.13 上直接 `import xtquant.xtdata` 触发 `No module named 'xtquant.IPythonApiClient'` 误报；`backend/runtimes/` 为空时桥接无候选；`detect_xtquant_abis` 的 ABI 值（raw=38）与 `host_python_minor()`（313）编码不一致，导致 `313 in [38,311]` 误判不兼容。
- **修复**：`runtime.py` 新增 `discover_system_runtimes()`（扫描注册表 / `py` 启动器 / PATH），自动发现系统 Python 3.8~3.13 作为桥接候选；ABI 值统一归一化为 `MAJOR*100+MINOR`（cp38 → 308）；`probe_environment` 先判定 broker ABI 再决定是否尝试进程内导入；`create_adapter` 在 ABI 不兼容时不再默默退回进程内，改由 `require_runtime_or_raise` 抛清晰可操作错误（含三选一方案）。
- **测试**：新增 7 项运行时单测，全量 pytest **128 passed**。
- **实测**：`probe_environment(r'p:\stock\gd_qmt')` 正确输出 broker_abis=[306..311]、host=313、`xtquant_importable="bridge"`，给出「安装 Python 3.11 / 放置 cp3.11/python.exe / 升级 SDK」三选一提示。

---

## 1. 关键事实（已实测 + 官方文档佐证）

### 1.1 本机券商实测：xtquant 自带多 ABI
本地广发 `P:\stock\gd_qmt\bin.x64\Lib\site-packages\xtquant\` 下存在：

```
IPythonApiClient.cp310-win_amd64.pyd
IPythonApiClient.cp311-win_amd64.pyd
IPythonApiClient.cp36-win_amd64.pyd
IPythonApiClient.cp37-win_amd64.pyd
IPythonApiClient.cp38-win_amd64.pyd
IPythonApiClient.cp39-win_amd64.pyd
```

→ 同一客户端目录里**已经打包了 cp36~cp311 共 6 个 ABI 变体**，导入时按「当前 Python 解释器版本」自动选其一（官方文档原文：「不同版本的 Python 导入时会自动切换」）。

### 1.2 官方支持范围
迅投官方 `dict.thinktrader.net`：XtQuant 提供 64 位 **Python 3.6 – 3.12**，不同版本导入时自动切换。各券商定制版略有差异（老客户端可能只到 cp311，新客户端含 cp312）。

### 1.3 我们当前打包状态
- 打包宿主 Python：**3.11.9**（commit a44b783 修复 ABI 根因后切换）。
- 产物：`backend/dist/qmt_work/qmt_work.exe` 内嵌 3.11.9 运行时。
- 现状兼容性：仅对「提供 cp311 的券商」开箱即用；对只提供 cp38/cp39 的老客户端会 `ImportError: DLL load failed`。

---

## 2. Q1：是否不同 Python 版本都要单独打包？

**不需要，而且「按用户本机 Python 打包多份」是一个伪需求。**

| 维度 | 是否需要多个包 | 原因 |
|---|---|---|
| 用户/目标机本机 Python | ❌ 无关 | PyInstaller 把解释器打进 `_internal`，运行不依赖用户本机 Python |
| 券商 xtquant .pyd ABI | ⚠️ 需匹配 | 桥接层运行时 ABI 必须 ∈ 券商提供的 .pyd 集合 |
| 按券商分别打包 | ❌ 不需要 | 多运行时桥接层一套安装包即可通吃 |

**真正约束**：我们的 xtquant 桥接层用的 Python 运行时，其 CPython ABI 标签（cp38/cp39/cp310/cp311/cp312）必须落在目标券商 `xtquant/` 目录里实际存在的 `.cpXXX-win_amd64.pyd` 集合内。

---

## 3. Q2：最多支持 Python 3.8+ 是否可行？

**可行，且是当前推荐目标区间（cp38~cp312）。**

- 下限 cp38：覆盖 2024 年前后主流券商客户端。
- 上限 cp312：覆盖 2025+ 新客户端（部分已含 cp312）。
- cp313：xtquant 目前**未发布** cp313 变体（实测 3.13 必 `DLL load failed`），故 3.13 暂不可作为桥接运行时；但 3.13 可作为**主后端**运行时（主后端不直接 import xtquant）。

---

## 4. Q3：更好的版本兼容完善优化（推荐方案）

### 4.1 现状方案的缺口
当前单包 3.11.9：对「只发 cp38 的老客户端」直接失效 → 兼容性有硬缺口，且无法随券商升级自动扩展。

### 4.2 推荐架构：主程序单包 + xtquant 多运行时 IPC 桥接

```
┌─────────────────────────────────────────────┐
│ qmt_work.exe (主后端, FastAPI + Agent)      │  固定用现代 Python (3.11/3.12)
│  - 路由/鉴权/审计/风控/回测/前端             │
└───────────────┬─────────────────────────────┘
                │ zmq / 命名管道 (IPC)
┌───────────────┴─────────────────────────────┐
│ xtquant-bridge 子进程（按 ABI 动态拉起）      │
│  runtimes/  cp38/ cp39/ cp310/ cp311/ cp312   │  ← 随包附带的极简嵌入式运行时
│  启动时扫描券商 xtquant/*.cpXXX.pyd → 选匹配  │
└───────────────┬─────────────────────────────┘
                │ sys.path 注入券商目录
        券商 xtquant (.pyd, ABI 自动匹配)
```

**核心机制**
1. 打包时把 `cp38/cp39/cp310/cp311/cp312` 五套**极简嵌入式 Python**（仅含标准库 + 桥接脚本所需）放进 `runtimes/`，体积合计约 30–40MB，可接受。
2. 启动时 `discovery.discover()` 已能列出券商候选；新增一步：扫描其 `xtquant/` 下存在的 `.cpXXX-win_amd64.pyd`，取最高可用 ABI。
3. 用对应 `runtimes/cpXXX/python.exe` 拉起 `bridge_main.py` 子进程，把券商 `bin.x64/Lib/site-packages` 注入 `sys.path`，`import xtquant` 必然成功（ABI 命中）。
4. 子进程通过 zmq `DEALER/ROUTER` 暴露 `xtdata`/`xttrader` 调用；主后端经 IPC 转发，业务层无感知。

**收益**
- 一套安装包通吃 3.8–3.12 全部券商，无需多包、无需用户配环境。
- 主后端可独立升级到 3.12/3.13，与桥接 ABI 解耦。
- 券商升级 xtquant（新增 cp312）时，只要包里预置 cp312 运行时即自动获得支持。

**落地步骤（建议作为 P2 专项）**
1. `build_exe.py` 增加 `collect_runtimes()`：下载/拷贝 5 套 embed Python 进 `runtimes/`。
2. 新增 `xtquant_client/bridge.py`：zmq 服务端 + ABI 选择；`xtquant_client/xtp.py` 改为经 bridge 调用（保留直连回退以兼容当前 3.11.9 单包）。
3. `discovery.py` 增加 `abi_variants()` 返回可用 ABI 列表。
4. 端到端冒烟：用 gd_qmt（cp311）验证 bridge 拉起成功；再构造 cp38 虚拟目录验证择机。

---

## 4. Q4：是否具备 GitHub 所有 QMT 开源项目的优点？

**诚实结论：没有任何单项目能「集齐所有优点」；qmt_work 在「多券商统一 + Agent/MCP + 自动探测 + 运维安全」组合上明显领先，但在「研究/回测深度、多资产广度、社区」上与头部开源项目有真实差距。**

### 4.1 生态对标矩阵

| 项目 | ⭐ | 核心优势 | 我们的差距/互补 |
|---|---|---|---|
| **vn.py (vnpy)** | ~39k | 事件驱动交易框架；`vnpy_qmt` 网关；覆盖 CTP 期货/期权/IB/美股，社区极大 | 我们仅 A 股 xtquant 系；缺期货/期权/全球多资产。但我们有 MCP Agent 与前端，vnpy 偏框架无 UI/无 Agent |
| **QUANTAXIS** | ~10k | 全栈研究：数据→因子→回测→实盘→可视化；Rust 加速 100×；MongoDB+Docker+分布式 | 我们回测/因子/存储层较轻；无 Rust 加速、无分布式调度。但我们有真实多券商+Agent，QUANTAXIS 实盘偏单券商 |
| **xtquant (官方)** | — | 权威行情/交易 SDK | 我们已是良好封装层（兼容旧 `xttrader`/`xttype`） |
| **Rockyzsu/QMT** | — | 涨停打板、TWAP/VWAP 算法、实操策略模板 | 我们已借鉴实现 `limitup.py`/`algo.py`；可继续吸收其策略范式 |
| **akshare / tushare / pytdx** | — | 免费数据源（腾讯/东财/新浪等） | 我们行情走券商 xtquant 真实通道（零 mock），可作为补充数据源接入 `reference.py` |
| **Backtrader / Backtesting.py** | — | 成熟回测引擎 | 我们回测为功能级；可对接其引擎提升深度 |
| **easytrader / 同花顺系** | — | 同花顺/雪球自动化（非 xtquant） | 我们走 xtquant 合同，未覆盖同花顺 GUI 自动化 |
| **Ptrade(恒生)/聚宽** | — | 云端运行、零代码模板 | 我们本地优先，未覆盖云端托管 |

### 4.2 qmt_work 的差异化优势（生态里少见）
- ✅ **多券商统一接入**：迅投系 6 家共用 `XTPQuantAdapter` + 同花顺/PTrade/掘金合同，前端自动列出——多数开源项目只对接单一客户端。
- ✅ **零 mock 真实券商架构**：所有行情/交易/账户经 `BrokerAdapter` 真实调用，未连券商返回 503 引导而非假数据。
- ✅ **MCP + Agent 驱动**：LLM/智能体可直接下单、回测、查询——开源生态基本没有原生 MCP。
- ✅ **自动探测（discovery）**：扫描运行进程 + 目录推断券商，前端一键填入——独家能力。
- ✅ **运维/安全闭环**：运行时配置中心、审计 hash 链、脱敏、出站 webhook、日级风控熔断——通常只在商业/机构系统出现。
- ✅ **前端 SPA + 桌面壳**：深色金融终端风格统一，开箱即用。

---

## 5. Q5：可持续改进优化清单（按优先级）

### P0（立竿见影，含版本兼容）✅ 已全部落地（2026-08-15）
1. **多运行时 IPC 桥接**（见 §4.2）——打通 3.8–3.12 全券商，根治 ABI 兼容性。`xtquant_client/bridge_server.py` + `bridge_client.py` + `discovery.py` 已实现：按 `xtquant/*.cpXXX.pyd` 自动择机拉起桥接子进程。
2. **桥接层 ABI 自动择机 + 回退**：选不到匹配 ABI 时明确报错并提示「该券商需 cpXX」。

### P1（能力深化）✅ 已全部落地（2026-08-15）
3. **回测引擎升级**：向量化快路径（`run_backtest_vectorized`，与 legacy 逐根信号一致，已有一致性测试锁定）+ 参数扫描（`run_param_sweep` grid search）+ 复用 `kline_cache`。
4. **因子/指标库**：`tools/factors.py` 实现 15 类指标（非 pandas-ta，自研零依赖），支持单标的/批量/基于真实 K 线。
5. **模拟盘（Paper Trading）**：`paper/paper_engine.py`，实时**真实行情** mark-to-market（不编造价格）。
6. **策略市场/导入导出**：`tools/strategy_market.py`，DB 目录 + zip/json 导入导出（吸收 Rockyzsu 范式）。

### P2（平台化）✅ 已全部落地（2026-08-15）
7. **可观测性**：`gateway/metrics.py` Prometheus 指标 + 链路追踪环缓冲；`/observability/metrics-summary｜traces｜runtime`。
8. **多账户网格视图 + 批量操作**：`AccountsGrid.jsx` + `GET /account/grid`、`POST /account/batch/order｜cancel｜reconnect`。
9. **插件/适配器注册表 V2**：`registry.py` `Registry` 提供 `negotiate`（能力协商）+ `hotplug_profile`（热插拔）。
10. **分布式/异步调度**：`scheduler/scheduler.py` + `scheduler/distributed.py`（可选 Redis 锁 Leader 选举，内存锁默认，优雅降级）。

### P3（生态）
11. **补充数据源接入** `reference.py`：akshare/pytdx 作为 xtquant 之外的备选行情。（🔌 待做）
12. **文档站 + Broker 接入指南**：README、API 文档、各券商路径/权限说明。（✅ 本回合补齐）
13. **契约测试**：用 mock xtquant 做 CI，pytest 当前 **121 passed** + smoke2 **22 passed**；继续扩充。（🔄 进行中）

---

## 6. 一句话总结

- **打包**：用户 Python 无关；一套安装包 + 多运行时桥接即可覆盖 3.8–3.12 全部券商，**无需按版本打多个包**。
- **兼容优化**：把 xtquant 交互抽成「按 ABI 自动择机的 IPC 桥接子进程」是最优解。
- **生态**：qmt_work 在「多券商统一 + Agent/MCP + 自动探测 + 运维安全」组合领先；差距在回测/因子深度、多资产广度（期货/期权/全球）、社区规模——按 P0~P3 路线补齐即可。
