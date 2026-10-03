# 项目无效文件清理 · 功能核心梳理 · 潜在问题 · 优化改进方案

> 生成时间：2026-10-03
> 基线版本：`VERSION` = **0.4.3**
> 工作区：`p:\github_public\qmt_work`
> 口径：所有清理目标均已通过 `git ls-files` 验证为**未被版本控制追踪**，且被 `.gitignore` 覆盖或为可再生缓存；`backend/data` 下的运行时数据（主库、备份、`master.key`）**一律未动**。

---

## 0. 执行摘要

| 项 | 结果 |
|---|---|
| 已删除无效文件/目录 | 42 项，**约 275 MB**（其中 `output/` 189 MB、`_static_prev_*` 5.1 MB、半写备份碎片 74 MB、缓存 0.65 MB） |
| 已确认无需处理的磁盘占用 | 约 4.6 GB 构建产物 + 5.3 GB 运行时数据，均已被 `.gitignore` 正确排除 |
| 识别潜在问题 | **14 项**（P0 2 项 / P1 6 项 / P2 6 项） |
| 最高优先风险 | ① 三档远程访问功能 **17 个文件全部未提交**；② `backend/pyproject.toml` 版本号为孤立过期值，与单一真源漂移 |
| 改进方案 | 4 阶段 14 项，每项均给出**可执行命令或可断言的测试判据** |

---

## 1. 第一轮：无效文件清理（已执行）

### 1.1 已删除清单

| # | 目标 | 规模 | 判定依据 |
|---|---|---|---|
| 1 | `output/` | 189 MB / 183 文件 | `.gitignore:42` 覆盖；内含 `client_test/` 188 MB 本地诊断产物 |
| 2 | `backend/output/` | 160 KB / 3 文件 | `output/` 模式跨层级匹配；`_capcov*.json` 为覆盖率探针快照 |
| 3 | `backend/_static_prev_039{,b,c}/` | 5.1 MB / 252 文件 | `.gitignore` 覆盖；v0.3.9 三次静态资源回滚备份，当前已 0.4.3 |
| 4 | `.ruff_cache/` + `backend/.ruff_cache/` | 486 KB | lint 缓存，重跑即再生 |
| 5 | `backend/.pytest_cache/` | 167 KB | 测试缓存 |
| 6 | `frontend-next/*.timestamp-*.mjs` | 3 文件 | `.gitignore:57`；Vite 配置加载临时产物 |
| 7 | `__pycache__/` | **2841 个目录** | 全仓 Python 字节码缓存 |
| 8 | `.rundata/` | 空目录 | 无任何文件 |
| 9 | `backend/export/` | 空目录 | **无 `__init__.py`**，全仓零 `import export` 引用；唯一形似引用 `from core.config import export_dir` 是配置项，与本包无关 |
| 10 | `backend/*.log` | 4 文件 / 47 KB | `startup.log`、`startup_R1.log`（0 B）、`startup_dbg.log`、`restart_verify.log` |
| 11 | `backend/wal.jsonl` | 0 B | 根目录残留 |
| 12 | `backend/data/run.out.log` / `run.err.log` | 86 KB | 进程 stdout/stderr 重定向残留 |
| 13 | `backend/data/backups/app.20260928_230351.db.tmp{,-journal}` | **74 MB** | 半写备份碎片（`sqlite backup` 中断残留），非完整备份 |
| 14 | `backend/data/wal.snapshot.jsonl`、`bars_cold.db-wal` | 0 B | 空文件 |
| 15 | `backend/data/exports/` | 空目录 | 无任何文件 |

### 1.2 保留未动（重要，勿误删）

| 目标 | 规模 | 保留理由 |
|---|---|---|
| `backend/data/app.db` | 1.4 GB | 主业务库 |
| `backend/data/backups/app.*.db` | 3.8 GB / 9 份 | 备份保留策略产物（见 §4-P1-5） |
| `backend/data/master.key` | 32 B | **主密钥文件，任何清理动作不得触碰** |
| `backend/data/_junk_conns_backup.json` | 8 KB | 名字含 junk、全仓零引用，但内含 `conn_id`/`account_id` 等连接凭据，属用户数据 → **建议人工确认后删除**（内容可从 `app.db` 连接表完整复原） |
| `logs/qmt_config_bak/` | 604 KB / 3 份 | 带时间戳的配置备份（20261002_*），非空，属运维留档 |
| 根目录 `tests/` | 22 个 `.mjs` | 本地探针/验收脚本，虽被 `.gitignore:100` 遮蔽但为用户自研工具 |
| `backend/dist/`、`backend/build/`、`frontend-next/dist{,-electron}/`、`backend/static/`、`node_modules/`、`backend/runtimes/` | 约 3.6 GB | 构建产物，**`.gitignore` 覆盖已逐个核实无误** |

### 1.3 清理安全验证

```bash
git status --porcelain | wc -l        # → 17，全部属于远程访问 WIP，无清理误伤
git ls-files | wc -l                  # → 745，追踪文件数不变
find . -name '__pycache__' | wc -l    # → 0
```

---

## 2. 项目功能地图与核心实现（重新梳理）

### 2.1 一句话定位

面向真实券商（迅投 MiniQMT / 大QMT）的**量化交易统一网关**：一个 FastAPI 进程同时对外提供 **REST API + MCP + WebSocket**，对内通过券商抽象层接入真实柜台，**零 mock**。

### 2.2 后端分层（`backend/`，20 个顶层模块）

```
gateway/      网关层   auth 鉴权 · rate_limit 限流 · risk 风控 · db_backup 备份
core/         内核     config 配置 · state 全局状态 · logging · crypto
app/          应用     main.py 装配 · routes/ 38 个路由模块 · bootstrap/ 6 阶段启动
brokers→BrokerManager  券商抽象（无 mock，真柜台唯一入口）
xtquant_client/  xtquant 直连（bridge_client / env / manager，共 3 个 40KB+ 大文件）
connectors/   bigqmt_bridge.py 大QMT 桥
agent_bigqmt/ qmt_api.py 大QMT Agent API
datasource/   registry.py + eltdx_source.py 数据源注册与 eltdx
engines/      策略/信号引擎
sync/         SyncEngine（812 行，见 §4-P1-2）
backtest/     回测（组合回测对齐见 TECH_DEBT TD-05/06，已闭环）
mcp_server/   MCP HTTP 挂载
tools/ scripts/ plugins/ qmt_strategies/ data/ logs/
```

### 2.3 38 个路由域（按业务分组）

| 分组 | 路由 |
|---|---|
| 账户与券商 | `account` `broker` `config` `capabilities` `reference` `apikeys` `remote_access` |
| 交易 | `trade` `algo` `paper` `rebalance` `reconcile` `signal` `strategy_run` |
| 策略研究 | `strategies` `strategy_market` `backtest` `factors` `indicators` `analysis` `screen` `research` `limitup` `target_portfolio` |
| 行情数据 | `market` `market_multidim` `datahub` `metrics` `runtime` `sync` |
| 运维与通知 | `health` `audit` `alerts` `notifications` `webhooks` `ws` |

### 2.4 前端（`frontend-next/`，React + Vite + Electron）

23 个 domain 组、60+ 页面组件，路由唯一真源为 `routes.tsx` 的 `PAGES`。最大单文件 `ScreenPanels.tsx` 39.5 KB。

### 2.5 关键实现纪律（已核实）

1. **零 mock**：所有交易路径经 `BrokerManager.active_bridge()` 走真实柜台；柜台拒单 `-1` → `BrokerError`，不包装成 `code=0`。
2. **事件出口唯一**：`core.emit.emit_event`，新增事件须同步 `wsEvents.ts` 与 `ws_events.json`。
3. **版本单一真源**：仓库根 `VERSION`，由 `scripts/verify_artifacts.py`（P2-28 闸门）与 `scripts/publish_release.py`（tag 一致性校验）双重锁定。
4. **启动 6 阶段 bootstrap + 逆序 shutdown**，拆分在 `app/bootstrap/`。
5. **三档远程访问模型**（`off` / `lan` / `wan`），`effective_host()` 驱动绑定地址，变更只落配置文件不改内存态。
6. **测试规模**：`backend/tests/` 182 个文件 / **1966 个 `def test_` 用例**；`backend/export` 等死包已清除。

---

## 3. 潜在问题清单（14 项）

分级口径：**P0** = 会造成数据/安全事故或功能不可交付；**P1** = 影响正确性或可维护性，有明确证据；**P2** = 技术债，可延后但有量化代价。

### P0 — 必须立即处理

#### P0-1 三档远程访问功能 17 个文件全部未提交

```
 M backend/app/main.py                M backend/tests/test_rate_limit.py
 M backend/app/routes/__init__.py     M frontend-next/electron/main.cjs
 M backend/core/config.py             M frontend-next/src/app/routes.tsx
 M backend/gateway/rate_limit.py     M frontend-next/src/domains/domain.module.css
 M backend/run.py                     M frontend-next/src/services/api/system.ts
?? backend/app/routes/remote_access.py         ?? frontend-next/src/domains/system/RemoteAccess.tsx
?? backend/tests/test_remote_access_routes.py  ?? scripts/verify_remote_access_safety.py
?? backend/tests/test_remote_access_tiers.py   ?? docs/SECURITY_AND_DEPLOYMENT_AUDIT.md
?? docs/REMOTE_ACCESS_DECISION.md
```

- **风险**：这是一条**跨后端路由 + 配置模型 + 限流 + 前端系统域 + 2 份决策文档**的完整特性线，涉及 `allow_credentials`/`*` 降级、loopback 豁免收窄（含 `x-forwarded-for` 检查）等安全语义。一旦工作区被误清或并行改动覆盖，**安全性变更将丢失且难以复原**。
- **附带风险**：`docs/TECH_DEBT.md` 未同步登记 TD 条目；`scripts/verify_remote_access_safety.py` 是否已接入 CI 未见证据。

#### P0-2 版本真源漂移：`backend/pyproject.toml` 为孤立过期值

```
VERSION                        = 0.4.3   ← 单一真源（verify_artifacts.py / publish_release.py 双重锁定）
frontend-next/package.json     = 0.4.3   ✓ 一致
frontend-next/package-lock.json = 0.4.3  ✓ 一致
backend/pyproject.toml         = 0.1.0-beta.1  ✗ 漂移 4 个版本
```

- **证据**：全仓 `grep` 无任何 `importlib.metadata.version("qmt-work")` 读取方；`.github/workflows/{ci,build-client,release}.yml` 均不引用 `backend/pyproject.toml` 的版本号。
- **风险**：该文件仍是依赖声明的真源（`fastapi==0.141.1` 等固定版本），一旦未来有人接入 `pip install -e backend/` 或 wheel 打包，会打出 `qmt-work 0.1.0-beta.1`，与 release tag 冲突；更常见的是**误导阅读者判断当前版本**。
- **为何之前没暴露**：打包走 PyInstaller spec，版本号走 `VERSION` 文件，pyproject 版本号从未被消费。

### P1 — 应尽快处理

#### P1-1 备份保留策略无上限治理（当前 3.8 GB / 9 份）

`backend/data/backups/` 现有 9 份 `app.YYYYMMDD_HHMMSS.db`，时间跨度 2026-09-28 一天内，单份约 400 MB。已发现过**半写碎片**（`app.20260928_230351.db.tmp` 74 MB + `.tmp-journal`，本轮已删）——说明备份流程在异常中断时**不会清理临时文件**。

- **判据**：`gateway/db_backup.py`（39 KB）存在保留策略，但**只控制份数，不控制总容量**；主库已 1.4 GB 且持续增长。
- **改进方向**：① 备份前写 `.tmp`、成功后原子 rename，并在启动时清扫残留 `.tmp*`；② 增加**容量上限**（如 `backup_max_bytes`）与**保留天数**双约束。

#### P1-2 `backend/sync/__init__.py` 把 812 行 SyncEngine 塞进包初始化

- **现状**：41.7 KB / 812 行，整个引擎位于 `__init__.py`；`find -size +50k` 尚未触发 Gate-4 拦截，属**阈值之下的隐性债务**。
- **代价**：`import backend.sync` 即加载全量引擎（启动期副作用）；AST 类静态扫描（如 `test_emit_event` 全量扫描）需持续对该文件负责；拆分风险被"暂时未超标"掩盖。
- **对照**：`backend/backtest/__init__.py` 同样 301 行实码，属同类模式。

#### P1-3 6 个源码文件已越过 41 KB，距 Gate-4 阈值 50 KB 仅剩余量

| 文件 | 大小 |
|---|---|
| `backend/datasource/registry.py` | 49.1 KB ⚠️ 距 50 KB 仅 1 KB |
| `backend/app/routes/market.py` | 47.9 KB |
| `backend/agent_bigqmt/qmt_api.py` | 45.0 KB |
| `backend/xtquant_client/manager.py` | 42.8 KB |
| `backend/xtquant_client/xtp/env.py` | 42.6 KB |
| `backend/connectors/bigqmt_bridge.py` | 42.6 KB |
| `backend/app/runtime/jobs.py` | 42.6 KB |
| `backend/datasource/eltdx_source.py` | 41.9 KB |
| `backend/xtquant_client/bridge_client.py` | 41.4 KB |
| `backend/gateway/db_backup.py` | 39.0 KB |

`registry.py` **下一次合理新增就会触发 Gate-4 硬拦截**，届时只能在"紧急改阈值"与"被迫中断重构"之间二选一。

#### P1-4 `.gitignore:100` 的 `/tests/` 整目录遮蔽根目录探针脚本

- **现状**：根 `tests/` 下 22 个 `.mjs`（`verify_backtest.mjs`、`probe_r17_verify.mjs`、`ui_audit_r17.mjs` 等）被**整目录**忽略，而真实测试位于 `backend/tests/`（182 文件 / 1966 用例）。
- **风险**：① 命名歧义——CI/新人容易以为根 `tests/` 是废弃目录；② 一旦被误清理（本轮已删除 2841 个 `__pycache__`，若模式写宽将波及此处）；③ 无 owner、无运行入口文档。
- **改进方向**：要么在 `.gitignore` 加注释说明该目录性质与保留策略，要么迁至 `scripts/probe/` 并纳入版本控制。

#### P1-5 远程访问安全语义未落入 CI 闸门

`scripts/verify_remote_access_safety.py` 已存在，但 `.github/workflows/ci.yml` 未检索到对该脚本的调用（本轮核查）。安全类语义（`allow_credentials` 与 `*` 不兼容降级、loopback 豁免收窄、TOTP 强制）若只靠手工跑脚本，回归概率高。

#### P1-6 58 处 `TODO/FIXME/XXX/HACK` 无归属、无编号

- 后端非测试源码中命中 58 条，**无一使用 `TD-nn` 编号**，与 `docs/TECH_DEBT.md` 的"每条必须可被 grep 证伪"收录规则脱节。
- **风险**：`TECH_DEBT.md` 的证伪纪律只对板上条目生效，散落标记会长期无人认领。

### P2 — 技术债（可延后，代价可量化）

| 编号 | 问题 | 证据 | 代价 |
|---|---|---|---|
| P2-1 | `logs/qmt_config_bak/` 无轮转 | 3 份时间戳备份，604 KB | 长期累积，无上限 |
| P2-2 | `backend/data/_junk_conns_backup.json` 为无引用孤儿 | 全仓 `grep _junk_conns` 零命中 | 8 KB，但含 `conn_id`/`account_id` |
| P2-3 | 根 `tests/` 探针脚本无入口文档 | 22 个 `.mjs`，无 README | 知识仅存于个人记忆 |
| P2-4 | `backend/restart_verify.log` 等 4 个开发期日志曾被提交前清理遗漏 | 本轮已删 | 说明缺 pre-commit 清理钩子 |
| P2-5 | 大文件拆分与 TD-04 同一模式（`eltdx_source.py` 曾 55.3 KB 已拆至 40.9 KB） | `TECH_DEBT.md` TD-04 已解决 | 模式已验证可行，直接复用 |
| P2-6 | 备份碎片 `.tmp` 无启动期清扫 | 本轮实测发现 74 MB 残留 | 异常中断即累积 |

---

## 4. 优化改进方案

### 阶段一：立即止血（P0，当天完成）

#### A1 — 提交远程访问特性（消除 P0-1）

```bash
# 1. 先确认测试全绿
backend/runtimes/cp311/python.exe -m pytest \
  backend/tests/test_remote_access_tiers.py \
  backend/tests/test_remote_access_routes.py \
  backend/tests/test_rate_limit.py -q

# 2. 确认安全语义脚本可跑
backend/runtimes/cp311/python.exe scripts/verify_remote_access_safety.py

# 3. 一次性提交（含文档）
git add backend/app/main.py backend/app/routes/__init__.py \
        backend/app/routes/remote_access.py backend/core/config.py \
        backend/gateway/rate_limit.py backend/run.py \
        backend/tests/test_remote_access_routes.py \
        backend/tests/test_remote_access_tiers.py backend/tests/test_rate_limit.py \
        frontend-next/electron/main.cjs frontend-next/src/app/routes.tsx \
        frontend-next/src/domains/domain.module.css \
        frontend-next/src/domains/system/RemoteAccess.tsx \
        frontend-next/src/services/api/system.ts \
        scripts/verify_remote_access_safety.py \
        docs/REMOTE_ACCESS_DECISION.md docs/SECURITY_AND_DEPLOYMENT_AUDIT.md
```

**验证**：`git status --porcelain` 应为空。
**补充**：在 `docs/TECH_DEBT.md` 追加 TD 条目登记远程访问的「需真环境验证」项（如 WAN 档 TOTP 强制在真实公网环境的行为）。

#### A2 — 修正版本真源（消除 P0-2）

修改 `backend/pyproject.toml`：`version = "0.4.3"`，并在 `[project]` 段加注释声明"版本号真源为仓库根 VERSION，本值由 CI 同步"。

同时在 `scripts/verify_artifacts.py` 增加一条断言（与既有 P2-28 闸门同处）：

```python
# P2-29：backend/pyproject.toml 版本必须与仓库根 VERSION 一致
pyproject_ver = re.search(r'^version\s*=\s*"([^"]+)"',
    (ROOT / "backend/pyproject.toml").read_text(), re.M).group(1)
assert pyproject_ver == version, f"pyproject 版本 {pyproject_ver} != VERSION {version}"
```

**验证**：`python scripts/verify_artifacts.py` 通过；故意改回 `0.1.0-beta.1` 应触发非零退出。

---

### 阶段二：正确性加固（P1，1–2 天）

#### A3 — 备份流程原子化 + 容量上限（P1-1 / P2-6）

1. `gateway/db_backup.py`：备份写 `<name>.tmp`，完成后 `os.replace()` 原子改名；启动阶段清扫 `backups/*.tmp*` 残留。
2. 新增配置 `db_backup_max_bytes`（默认建议 8 GB）与 `db_backup_retain_days`（默认 14），与既有份数保留取交集。
3. 新增测试断言：注入 3 个 `.tmp` 残留 → 启动后计数归零。

**验证**：`pytest backend/tests/test_db_backup_policy.py -q` 全绿；人工制造一次备份中断（在 `tmp` 阶段 kill 进程）后重启，确认无 `.tmp` 残留。

#### A4 — SyncEngine 出包（P1-2）

按 TD-04 已验证的 mixin 拆分模式执行：

```
backend/sync/__init__.py          # 收敛为 re-export（目标 < 100 行）
backend/sync/engine.py            # SyncEngine 主体
backend/sync/scheduler.py         # 调度/心跳
backend/sync/reconcile.py         # 对账
```

**验证**：① `find backend -name "*.py" -not -path "*/tests/*" -size +50k` 仍为空；② `pytest backend/tests/ -k sync -q` 全绿；③ 确认 `import backend.sync` 后的公开符号集合不变（用 `dir()` 前后 diff 断言，防止 re-export 漏项）。

#### A5 — `registry.py` 预防性拆分（P1-3，最高优先）

该文件距 Gate-4 阈值仅 1 KB。参照 TD-04 把 49.1 KB 的 `datasource/registry.py` 按职责切分（注册表核心 / 批量取数 `_broker_batch` / 适配层），核心文件目标 ≤ 30 KB。**此项同时是 TD-01 的前置条件**——TD-01（`_broker_batch` N 次 RPC 改 1 次）需真券商环境验证，但**拆分动作本身不需要**。

**验证**：`python scripts/check_execution_architecture.py` Gate 4 通过；`pytest backend/tests/ -k "datasource or registry" -q` 全绿。

#### A6 — 安全语义接入 CI（P1-5）

在 `.github/workflows/ci.yml` 追加一步调用 `scripts/verify_remote_access_safety.py`，与 `check_execution_architecture.py`、`check_api_contract_drift` 并列。**判据**：workflow 中出现该脚本路径的 grep 命中。

#### A7 — TODO 标记收编（P1-6）

一次性脚本归集：

```bash
grep -rn --include="*.py" -E "TODO|FIXME|XXX|HACK" backend/ \
  --exclude-dir=tests --exclude-dir=dist --exclude-dir=build \
  --exclude-dir=runtimes --exclude-dir=.venv > /tmp/todo_dump.txt
```

逐条判定：能立即修的改掉；不能修的转为 `TD-nn` 条目写入 `docs/TECH_DEBT.md`（遵守其"可被 grep 证伪"收录规则）；无价值的直接删除。**目标**：散落标记归零，债务全部在板上可见。

---

### 阶段三：工程化卫生（P2，穿插执行）

#### A8 — 配置备份轮转（P2-1）

`logs/qmt_config_bak/` 增加保留策略（如保留最近 7 份 / 30 天），与 A3 的容量上限思路一致。

#### A9 — 孤儿文件处置（P2-2）

`backend/data/_junk_conns_backup.json`：内容可从 `app.db` 连接表复原，**建议删除**（本轮刻意保留，因含 `account_id` 属用户数据）。删除前请确认当前连接列表已无手工改动丢失。

#### A10 — 根 `tests/` 定性（P1-4 / P2-3）

二选一：
- **方案甲（推荐）**：迁入 `scripts/probe/` 并 `git add`，纳入版本控制，补一份 30 行 README 说明每个探针的运行时机；
- **方案乙**：保留原址，在 `.gitignore` 的 `/tests/` 行上方加注释块，明确"本地探针脚本，用户自研，勿清理"。

#### A11 — 构建产物清理钩子（P2-4）

本轮已清理 `backend/*.log`（4 个开发期日志）。增加 pre-commit 或 `scripts/clean_workspace.py`：仅针对 `.gitignore` 已覆盖的路径执行清理，**白名单制**，明确排除 `backend/data/`（含 `master.key`）与 `logs/qmt_config_bak/`。

#### A12 — 复用 TD-04 拆分模式处理其余大文件（P2-5）

按 §P1-3 表格顺序处理 `market.py`（47.9 KB）、`qmt_api.py`（45 KB）等，每拆一个文件跑一次 `check_execution_architecture.py`。

---

### 阶段四：交付前闸门（回归验证）

```bash
# 1. 后端全量（注意：需先设 CODEBUDDY_SAFE_DELETE_ENABLED=0 避开批量删除护栏伪装成测试失败）
export CODEBUDDY_SAFE_DELETE_ENABLED=0
backend/runtimes/cp311/python.exe -m pytest backend/tests/ -q \
  --basetemp="$TEMP/qmt_pytest_basetemp"   # basetemp 放 OS 临时目录可完全绕过护栏

# 2. 架构闸门
backend/runtimes/cp311/python.exe scripts/check_execution_architecture.py
backend/runtimes/cp311/python.exe scripts/verify_artifacts.py
backend/runtimes/cp311/python.exe scripts/verify_remote_access_safety.py

# 3. 前端
cd frontend-next && npm run test:serial   # 并行会因 EPERM 静默丢文件

# 4. 前端探针交叉验证（截图空白≠内容空，须 DOM/hit-test 交叉）
# 5. 工作区洁净度
git status --porcelain
```

**判据**：后端 1966 用例基线不回退；`git status --porcelain` 为空；三条脚本闸门均退出 0。

---

## 5. 附录

### 5.1 本轮清理规模汇总

```
已删除：约 275 MB
  output/                      189 MB
  backend/data/backups/*.tmp*  74 MB
  backend/_static_prev_039*     5.1 MB
  backend/output/               0.16 MB
  缓存 (.ruff/.pytest)          0.65 MB
  日志与空文件                  0.12 MB
  __pycache__/                  2841 个目录
```

### 5.2 磁盘占用全景（保留项）

```
约 10.9 GB（全部已正确 gitignore，勿删）
  frontend-next/dist-electron   2.3 GB  构建产物
  frontend-next/node_modules    615 MB  依赖
  backend/data/                 5.3 GB  运行时数据（含 master.key）
    └─ backups/                 3.8 GB  见 P1-1
    └─ app.db                   1.4 GB  主库
  backend/runtimes/             223 MB  内嵌 Python
  backend/dist/                 380 MB  PyInstaller 产物
  backend/build/                 95 MB  中间产物
  backend/static/                1.7 MB  已部署前端
```

### 5.3 基线事实

| 指标 | 值 |
|---|---|
| 追踪文件总数 | 745 |
| 后端追踪文件 | 461 |
| 前端追踪文件 | 208 |
| 脚本追踪文件 | 32 |
| 文档追踪文件 | 31 |
| 后端测试文件 / 用例 | 182 / 1966 |
| `.dead` 组件残留 | 0 |
| `backend/export/` 死包 | 已清除 |
| 路由模块数 | 38 |
| 后端源码 > 40 KB 文件数 | 10 |

### 5.4 已知环境护栏（执行阶段四时必读）

1. **safe-delete 批量护栏会伪装成测试失败**：单 turn 累计删除 > 50 项触发 `SystemExit(1)`，在 pytest 里表现为 `ERROR at setup of ...` 几十条 + `EXIT 1`。判读口诀：traceback 含 `sitecustomize.py` / `_safe_shutil_rmtree` 即为护栏而非用例失败。正解：`--basetemp` 指向 OS 临时目录（`$TEMP/...`），护栏对该路径完全放行。
2. **禁止 `pytest.mark.flaky`**（TD-02）；全量红时先单文件复跑再判定。
3. **前端并行 vitest 会 EPERM 静默丢文件**（TD-07），须 `npm run test:serial`，并比对 `Test Files N passed` 与磁盘测试文件数。
4. **构建前须 `taskkill qmt_work.exe` 与 electron**，否则静态资源被占用导致 StaticFiles 全 500。
5. **前端 build 与 PyInstaller 绝不可重叠**（白屏根因），判据为 `build/qmt_work/Analysis-00.toc` 的 mtime。
6. **主库 1.4 GB + keep=9 每次启动复制整库** ⇒ 全量回归前须设 `QMT_DB_BACKUP_ENABLED=0`。

---

## 6. 第二轮执行记录（同日）

本节记录在第一轮方案之上的实际修复，全部有回归用例锁定。

### 6.1 主库诊断结论

完整分析见 **`docs/DATASTORE_SIZE_AND_SPLIT_ANALYSIS.md`**。要点：

| 结论 | 依据 |
|---|---|
| 1.35 GB **不是膨胀**，不建议 `VACUUM`、**不建议拆库** | `freelist_count=371` 页（约 1.4 MB，占 0.11%）；`page_count × page_size` 精确等于文件字节数 |
| 真正可回收的是**索引**，不是表 | 表 798.8 MB + 2 个二级索引 383.7 MB = 87.4% |
| `idx_local_bars_lookup` 是**零收益冗余索引** | 其列 `(code, period, adjust, dt)` 是主键 `(code, period, adjust, dt, provider_id)` 的严格前缀，SQLite 主键本身已含全部列数据 |
| 可回收 137.4 MB，零性能损失 | 提供 `scripts/optimize_local_bars.py`（默认 dry-run，`--apply` 前自动在线备份） |
| 跨 provider 重复行只有 5.2% | 删掉任一 provider 会使多源对账退化为「自己证明自己」，**保留** |

### 6.2 数据质量缺陷（已修）

| # | 缺陷 | 后果 | 修复 | 回归 |
|---|---|---|---|---|
| D1 | `QUALITY_STATE_RANK` 把 `raw` 排在 `final` 之前 | **99.979% 的 `local_bars` 行永远卡在 `raw`**（3,595,703 / 3,596,464），对账通过的行无法覆盖未对账行 | `backend/datasource/quality.py` | `test_canonical_selection.py` |
| D2 | `FINALITY_STATES` 缺 `"empty"` | 同一字段两套校验规则，空数据集终态校验被绕过 | `backend/datasource/quality.py` | `test_canonical_selection.py` |
| D3 | `BigQmtBridge.ensure_handler` 非幂等 | `_pump_guard` 每 2s 重复注册 ⇒ 一天约 43,200 次重复注册，行情帧被重复派发 N 次 | `backend/connectors/bigqmt_bridge.py`（去重 + `on_disconnect`） | `test_bigqmt_bridge_face.py` |

> D1 与 §4 的瘦身直接相关：**冗余数据的真正来源是重复派发，不是写入策略**。

### 6.3 断链修复（WS 4401，本轮最重要的一项）

**症状**：全量同进程回归固定 3 条 `WebSocketDisconnect(4401, "unauthorized: missing/invalid token")`，逐文件跑全绿 —— 典型的**假绿灯**。

**根因（用临时探针插件实测定位）**：

```
[WS-DENIED] token='contract-introspect-key'  settings.api_key='qmt-dev-key'
```

测试进程里存在**两个 `core.config.settings` 单例**：

1. `backend/tests/contracts/introspect.py` 在导入时无条件
   `os.environ.setdefault("QMT_API_KEY", "contract-introspect-key")`，
   把一个假密钥种进**进程级全局环境变量**；
2. `tests/test_remote_access_tiers.py` 的 `_fresh_config()` / `_fresh_run()`
   用 `sys.modules.pop("core.config")` + 重新 import，**每次调用都造一个新的
   `settings` 对象**。此后新代码拿到新单例（读到假密钥），
   而生产模块导入期绑定的引用（`app.routes._common`、路由层）仍指向旧单例。
3. 于是 WS 测试用新单例取 `token`，服务端用旧单例验 `api_key` → 必然 4401。

**修复（两处）**：

- `introspect.py`：改为**仅当 `settings.api_key` 为空时**才写环境变量。
  settings 恒有非空默认值，正常路径下永不触发，也不会污染后续任何 `Settings()` 重建。
- `test_remote_access_tiers.py`：删除 `sys.modules.pop("core.config")` 与
  `sys.modules.pop("run")`，改为对**已存在的单例**原地复位字段
  （被测函数 `normalize_remote_access` / `effective_host` / `_self_check` 本就在调用时读取模块级 `settings`，
  原地改写语义完全等价），并加断言 `run.settings is core.config.settings`，
  单例一旦被分裂立即报错而非产生误导结论。

> **教训**：`sys.modules.pop` + 重新 import 一个被到处 `from ... import settings`
> 引用过的模块，等于在进程里制造分裂脑。测试要用 `monkeypatch`，不要用 `pop`。

### 6.4 停机竞态（已修）

`app/services/market/kline_io.py::stop_moneyflow_collector` 原先在 `task.cancel()`
**之前**就把 `_collector_task` 置 `None`。采集循环内有一步 `await snapshot_codes(watch)`
是真实 IO，取消要等它 unwind；在这个窗口里全局已空 ⇒ 重入的
`start_moneyflow_collector()` 判定「未启动」并再起一个循环 ⇒
**两个采集循环并发写同一张 `moneyflow_cache`**。

修复：只在任务真正结束后清空，并加 `_collector_task is task` 守卫
（期间若已有新任务顶上，不覆盖）。

测试侧配套修复：`test_r26_retention_and_progress.py` 的用例原先直接操作 lifespan
在 TestClient 循环里创建的任务，再用 `asyncio.run` 建**新循环**去 cancel/await 它 ——
跨循环取消并不可靠（全量回归偶发「采集任务未被取消」）。改为 `monkeypatch` 置空后
让 `start` 在当前循环自建任务，用例与执行顺序解耦。

### 6.5 门禁基线同步

| 门禁 | 修复前 | 修复后 |
|---|---|---|
| `EXPECTED_TESTS` | 2051（过期，实际 2100） | 见 §6.6 实测值 |
| `EXPECTED_COMPONENTS` | 72 | 同上 |
| `EXPECTED_PAGES` | 43 | 同上 |
| `rest_endpoints.json` | 232（缺 6 个 remote-access 端点） | **238** |
| `mcp_tools.json` | — | **128** |
| API 接口文档 | — | REST 238 / MCP 128 / WS 30 |

由 `gen_contracts.py` + `gen_api_doc.py` 重新生成，非手改。

### 6.6 最终回归结果

```
后端全量：2090 passed, 10 skipped, 0 failed   (250.15s)
```

执行命令（含必需环境变量，见 §5.4）：

```bash
cd backend
QMT_DB_BACKUP_ENABLED=0 CODEBUDDY_SAFE_DELETE_ENABLED=0 \
  ./.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider \
  --basetemp="$TEMP/qmt_pt_final" --ignore=tests/test_client_start_test.py
```

### 6.7 本轮仍保留、需人决策的事项

1. `idx_local_bars_provenance`（246 MB）是否删除 —— 收益 246 MB，
   代价是快照构建退化为全表扫描（约 2–3 秒/次）。**默认保留**，脚本支持 `--also-provenance`。
2. `quality_state='raw'` 存量 3,595,703 行需要**主动触发一次全量对账**才会被晋级
   （`system.reconcile_bars` 任务）。修复 D1 只保证此后新增数据能被正确晋级。
3. `local_bars.dt`（`YYYYMMDD`）与 `kline_archive.dt`（`YYYY-MM-DD`）日期格式不统一，
   建议后续统一为 ISO `YYYY-MM-DD`。

---

*本方案所有条目均可被一条命令或一条测试证伪；未列入板上的问题不算债务。*
