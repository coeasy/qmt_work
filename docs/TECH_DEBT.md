# TECH_DEBT.md — 技术债 / 未决事项看板

> 建立于第 26 轮（2026-09-25），依据 `docs/2026-09-25_项目功能架构梳理与优化改进方案.md` §5.2 P1-2。
> **收录规则**：每个条目必须**可被一条测试或一个 `grep` 证伪**；无法证伪的条目不许入板。
> **状态口径**：`已锁测试`（有回归测试守着）/ `待真券商`（需真实券商环境才能验证）/ `需拍板`（产品决策）。
> **维护规则**：新发现问题一律追加，已解决条目**不删除**，改标 `已解决` 并注明解决轮次。

---

## 一、当前未决（活跃）

### TD-01 `datasource/registry.py::_broker_batch` 对 N 只标的发 N 次 RPC
- **现状**：批量取行情走逐只 RPC；桥接层已具备 `get_full_tick`（一次 RPC 取多只）。
- **风险**：标的数线性放大 RPC 次数，批量选股/同步场景下延迟与限流风险。
- **为何没改**：**必须真券商环境验证**「`get_full_tick` 返回结构 / 字段完整性 / 单次上限」才能改；盲改会把「一批标的」变成「静默丢字段」。
- **状态**：`待真券商`
- **证伪方式**：`grep -n "def _broker_batch" backend/datasource/registry.py` 定位；改造后需一条「N 只标的只触发 1 次 bridge RPC」的计数断言（mock bridge 计数）。
- **关联**：方案文档 §4.2-1 / §5.3 P2-1。

### TD-02 `test_db_backup_policy::test_run_endpoint_skip_chain_survives_its_own_audit_row` 为已知 flake
- **现状**：WAL 检查点时序敏感，**全量跑偶红、单文件复跑必绿**。
- **风险**：若用 `pytest.mark.flaky` 掩盖，则真实回归被吞。
- **已采取措施**：**明确禁止** `pytest.mark.flaky`；CI 报红时先单文件复跑再判定。
- **状态**：`已锁测试`（测试本身在，只是时序敏感）
- **证伪方式**：`backend/runtimes/cp311/python.exe -m pytest backend/tests/test_db_backup_policy.py -q` 单文件复跑。
- **后续**：根因在「WAL 检查点后 `-wal` 大小归零的时序」；正确修法是让 `_covered_by()` 先读 `_wal_size()` 再归一化（见 HARD_CONSTRAINTS A11），但需先复现稳定。

### TD-03 `state.bridge` / `state.gateway` 只写状态（**R29 已闭环**）
- **原状**：业务读写统一走 `broker_manager.active_bridge()`；但交易日历刷新路径仍在读 `state.bridge` ⇒ 晚到连接不补同步时日历会停在 fallback。
- **处置**：日历刷新改为直接问 `broker_manager.active_bridge()`（动态求值）；两个槽位语义明确为**只写快照**，写入唯一入口 `AppContext.cache_active_bridge()`（它不再读回自己刚写的值）。
- **状态**：`已解决（R29）`，全仓 **0 个读点**。
- **证伪方式**：`backend/tests/test_state_writeonly_guard.py`（AST 扫 Load 上下文，已变异验证）；全仓 `grep` 亦可：`state.bridge` / `state.gateway` 只应出现在赋值左侧与测试里。

### TD-04 `datasource/eltdx_source.py` 超 50KB（**R28 已闭环**）
- **原状**：55.3KB / 1107 行，连接治理 + 基础行情 + 板块指数 ETF 三族挤在一个类里（方案文档 §5.2 P1-1 未点名本文件，是实测发现的）。
- **处置**：板块/指数/ETF 一族（12 个方法）拆到 `datasource/eltdx_boards.py`（mixin），55.3KB → 40.9KB。
- **状态**：`已解决（R28）`
- **证伪方式**：`find backend -name "*.py" -not -path "*/tests/*" -size +50k` 应为空；`check_execution_architecture.py` Gate 4 常驻拦截。

### TD-05 组合回测按**索引**加总净值（**R29 已闭环**）
- **原状**：`tools/factor_backtest.py::run_portfolio_backtest` 把各标的袖套净值**按索引**相加
  （`tot += eq[t] if t < len(eq) else eq[-1]`）。标的日期范围不一致时（次新股上市晚、
  长期停牌、取数根数不同）会把**不同日期**混在同一个下标上，得到一条真实日历上
  **不存在**的组合净值曲线；而夏普/最大回撤区间/月度分布全部建立在它上面。
  更糟的是 `eq[-1]` 填充：把「这段没有数据」静默表述成「净值不变」。
- **处置**：新增 `tools/portfolio_engine.py::align_panel()` 按交易日**交集**对齐；
  `run_portfolio_backtest` 改为先对齐再跑袖套，并输出 `alignment`（每只标的被排除
  多少根、排除区间）。袖套求和契约（`Σ w_i × 袖套收益`）**保持不变**。
- **状态**：`已解决（R29）`
- **证伪方式**：`tests/test_portfolio_engine.py::test_legacy_portfolio_backtest_aligns_dates_not_indices`
  （含行为级判据：把 A 单独喂**对齐切片**重算，收益必须与组合里的 A 分项一致）；
  变异验证见 `scripts/verify_portfolio_falsifiable.py` M7（组合回退 ⇒ 变红）。

### TD-06 `align_panel` 曾把分钟线压成一天（**R29 已闭环**，属自检发现）
- **原状**：日期键取 `str(v)[:10]`，`"2024-01-05 09:31:00"` 被截成 `"2024-01-05"` ⇒
  一天内 240 根 bar 塌缩成同一天、只剩最后一根，净值曲线凭空少 239 个点而**无任何报错**。
- **处置**：`_date_key()` 对带时间的值**保留到分钟**；同键多根的数量以
  `n_collapsed_same_key` 显式报出，不再静默。
- **状态**：`已解决（R29）`
- **证伪方式**：`test_align_panel_keeps_intraday_distinct` / `test_align_panel_reports_collapsed_same_key`；
  变异 M1（去掉时间分支 ⇒ 变红）。

### TD-07 沙箱下 vitest 偶发「丢文件」（**环境问题，非代码回归**）
- **现象**：`npx vitest run` 输出 `Test Files 47 passed (47)` 而 `frontend-next/tests/` 下有
  **48** 个 `*.test.*`；同时报 1 条 unhandled error：
  `EPERM: operation not permitted, open 'C:\...\Temp\...\web\<hash>'`
  （栈里是 `shim/node-brokered-fs-shim.cjs` → `vitest resolveConfig`）。
- **成因**：本机 vitest 的**转换缓存写入被沙箱文件代理拦截**，配置解析阶段丢一个模块 ⇒
  该测试文件根本没进入执行。**与源码无关**。
- **为什么必须记下来**：它表现为「绿灯里少了一条」，很容易被当成「某个测试被删了」或
  「计数对不上」而误查半天。**flaky 的绿灯本身就是缺陷信号**。
- **判定方法（两条一起看）**：
  1. 比对 `Test Files N passed` 与 `ls frontend-next/tests/*.test.*` 的数量；
  2. 单文件复跑缺失的那个：`cd frontend-next && npx vitest run tests/<缺失>.test.tsx` —— 必绿。
  - 实测：缺 `dbBackupPanel.test.tsx`（19 用例），单跑 19 passed ⇒ 439 + 19 = **458**，与基线一致。
- **状态**：`已锁测试`（不可在源码里修；只能靠上述判定流程识别）
- **禁止**：不得为了让计数对上而删除/跳过测试文件。

### TD-08 裸 `except: pass` 水位线**零余量**（**R29 已消除放大条件**）
- **现象**：全量回归中 `test_no_bare_except_pass.py` 报 `1 failed, 2 passed`；
  单文件复跑 **10/10 全绿**，且后续 425 次并发扫描 + 连续 60s 高频扫描均未复现。
- **真实成因（已定位）**：水位线 `MAX_BARE_EXCEPT_PASS = 151` 是历史快照，而
  **工作区恰好 151**（`git show HEAD:` 口径为 **150**）。多出的那 1 处来自
  **未提交的 R26 改动**：`app/services/market/kline_io.py::stop_moneyflow_collector`
  里的 `except (asyncio.CancelledError, Exception): pass`。
  ⇒ 水位线**零余量**：任何一次瞬时扰动（多一个 `.py`、半写状态被读到、shim 抖动）
  都能把 151 顶成 152 而变红，**且与真实回归无法区分**。这才是本次要修的东西。
- **处置**：按护栏自身给出的处方把那处改为
  `swallow(exc, why="停机取消资金流采集循环（取消/循环异常均属预期终态）")`
  —— 停机路径仍然不阻断，但**静默失败变成可查日志**。工作区回到 **150**，恢复 1 格余量。
- **已排除的假设（都做了实验，不是猜）**：
  1. 并发跑变异脚本把水位推过上限 ⇒ 425 次并发扫描最大仍 151；
  2. 影子模块 `app/quote_fields.py` 存在 ⇒ 计数不变（151）；
  3. 注入块追加到 `alerts.py` ⇒ 计数不变（151）；
  4. 文件读放大（shim 返回重复内容）⇒ 连续扫描未观测到任何单文件计数超过真值。
- **残留（诚实记录）**：那次瞬时超限的**具体触发者未复现**。已消除「零余量」这一
  放大条件；若再次出现，判定流程 = 先看 `MAX_BARE_EXCEPT_PASS` 与实测差多少
  （差 1 且单文件复跑绿 ⇒ 瞬时扰动；差得多 ⇒ 真回归）。
- **状态**：`已锁测试`
- **证伪方式**：`pytest backend/tests/test_no_bare_except_pass.py`；
  水位 = `_scan()` 去重计数，当前 **150 ≤ 151**。
- **禁止**：不得为了让计数对上而调高 `MAX_BARE_EXCEPT_PASS`（该文件 docstring 已明确
  「只降不升，调高必须给出理由并改这里」）。

### TD-09 发布包内缺 openpyxl ⇒ Excel 导出**静默降级**（**R30 已闭环**）
- **原状**：`app/export.to_excel` 用 `df.to_excel(..., engine="openpyxl")` —— `engine="openpyxl"` 是
  **字符串**，PyInstaller 静态分析看不见；PyInstaller 6.22.1 也**没有** `hook-openpyxl`
  （实测只有 `hook-pandas*`）。源码 / dev 环境 openpyxl 在 site-packages 里一切正常，
  **用户装的包**里没有它 ⇒ 导出永远走 CSV 降级。
- **风险**：最典型的「只有用户装的包才出问题」——开发机**永远复现不出来**。
  且叠加上 TD-10 的诚实降级后更难发现：信封说得对，但用户还是拿不到 xlsx。
- **处置**：`backend/build_exe.py` 的 `HIDDEN` 显式声明 `"openpyxl", "et_xmlfile"`。
- **状态**：`已解决（R30）`
- **★ R30 收尾时的补记 —— 原来写的证伪方式是错的，必须记下来**：
  本条原先写「构建后核对 `backend/dist/qmt_work/_internal/openpyxl/` 存在」。
  这个判据 **对纯 Python 包不成立**：PyInstaller 把纯 Python 模块作为
  `PYMODULE` 收进 **PYZ**，PYZ 内嵌在 `qmt_work.exe` 里，
  **不会**在 `_internal/` 下生成 `openpyxl/` 目录（只有带 `.pyd` 的包如 `numpy` /
  `pandas` 才会以目录形式出现）。按目录判定会把**正常打包**误判成「缺陷仍在」，
  反过来也可能把真缺的包误判成「在」（只要任何地方恰有一份同名目录 —— 例如
  `--add-data=backend/runtimes;runtimes` 带进来的 `runtimes/cp311/Lib/site-packages/openpyxl`，
  它是**客户端运行时的数据副本，不在冻结应用的 `sys.path` 上**）。
  正确判据（唯一可信）：**跑一次真实导出**，看信封里的 `format`。
  本轮实测（打包态后端、`POST /api/v1/market/export` `format=xlsx`）：
  `{"format": "xlsx", "path": "...\\data\\exports\\_verify_xlsx.xlsx", "count": 1}`，
  产物 4905 bytes 且前两字节为 `PK`（zip 容器）⇒ **openpyxl 真的可用**。
  另可用 `python -m PyInstaller.utils.cliutils.archive_viewer` 或
  `backend/build/qmt_work/Analysis-00.toc` 确认条目类型为 `PYMODULE`。
  同时记录一个**构建顺序**教训：`--desktop-only` 会跳过 Step 2，
  直接用**上一轮的 `backend/dist`**。若 HIDDEN 是后来才补的，那次桌面打包
  打进去的就是**旧的、缺 openpyxl 的**后端 EXE —— 发布前必须跑**完整**构建
  （`build_all.bat --nsis`），不能只跑 `--desktop-only`。
- **证伪方式**：`python scripts/verify_packaged_backend_api.py` —— 内含两条断言
  「Excel 导出产出真 xlsx（openpyxl 已随包）」（`data.format == "xlsx"`）与
  「xlsx 是 zip 容器（首两字节 `PK`）」。**不要**改回按 `_internal/` 目录存在性判定。

### TD-10 导出端点信封描述了不存在的产物（**R30 已闭环**）
- **原状**：`to_excel` 只 `try` 了 pandas 导入；pandas 在、openpyxl 缺时
  `df.to_excel` 在**写入阶段**抛 `ImportError` ⇒ 端点走 `except` 报 500「导出失败」，
  与文档承诺的「回退 CSV」不符；且调用方按 `path` 取文件必然失败。
- **处置**：`to_excel` 把写入调用纳入 `try`，降级时**不落盘**（绝不写伪 xlsx）；
  `routes/analysis.py::market_export` 改为**以真实产物为信封**
  （`path.exists() and st_size > 0` 才报 xlsx，否则报 `format="csv"` + 内容）。
- **状态**：`已解决（R30）`
- **证伪方式**：`tests/test_analysis_export.py`（10 用例），含
  「monkeypatch `to_excel` 抛 `ImportError` ⇒ 信封必须是 csv 且不含 path」。

### TD-11 自动更新**永远装不上**（**R30 已闭环**）
- **原状**：`electron/main.cjs::fullShutdown()` 用 `app.exit(0)` 收尾。Electron 文档明确
  `app.exit()` 立即终止且**不发出** `before-quit` / `will-quit` / `quit` 事件；而
  electron-updater 的「退出自动安装」正挂在 `app.once("quit", …)`
  （`ElectronAppAdapter.onQuit`）⇒ 更新下载完成后**永远装不上**：
  用户点了「重启安装」，程序退了，版本号没变。
- **处置**：收尾改 `app.quit()`（此时 `quitting` / `shuttingDown` 已置位，
  `close` 与 `before-quit` 都会放行），另加 8s 兜底 `app.exit(0)`
  防「正常退出序列被异常阻塞」时卡住不退。JS 单线程 ⇒ 安装包同步拉起期间
  兜底定时器不会插进去打断安装。
- **状态**：`已解决（R30）`
- **证伪方式**：`grep -n "app.exit(0)" frontend-next/electron/main.cjs` —— 只应出现在
  8s 兜底那一处；打包后装旧版 → 手动检查更新 → 退出 → 版本号必须变。

### TD-12 `updater.cjs` 孤儿 IPC + 托盘「检查更新」无反馈（**R30 已闭环**）
- **原状**：(a) 用 `webContents.send("update-status"/"update-progress")` 通知渲染层，
  而 `preload.cjs` 从未暴露、渲染层从未订阅 ⇒ **发进真空的孤儿逻辑**；
  (b) 托盘「检查更新」在无更新时只写日志 ⇒ 用户点了**界面毫无反应**，
  无法区分「已是最新」和「功能坏了」。
- **处置**：`updater.cjs` 整体重写——去掉全部 `webContents.send`，改**原生对话框**；
  新增 `_manual` 标记（只有托盘手动检查才弹窗，启动静默检查绝不打扰）；
  `_manual = true` 必须在 `if (_checking) return;` **之前**置位
  （否则「正有一次静默检查在飞」时用户点菜单会被早退吞掉）。
- **状态**：`已解决（R30）`
- **证伪方式**：`grep -rn "webContents.send" frontend-next/electron/` 应无更新相关命中；
  打包后托盘点「检查更新」，无更新时必须弹出「已是最新版本」。

### TD-13 `QMT_UPDATE_URL` 死旋钮 + `build_all.bat` Node 探测硬编码（**R30 已闭环**）
- **原状**：(a) 更新源真源是 `frontend-next/electron-builder.yml` 的 `publish` 段
  （据此生成包内 `resources/app-update.yml`，运行期只读它），`QMT_UPDATE_URL`
  环境变量**根本不起作用** ⇒ 改它的人以为改了源；
  (b) `build_all.bat` 硬编码 `node\versions\22.22.2`，而实际托管目录是 `22.22.2-3`
  （带补丁后缀）⇒ 探测不到托管 node ⇒ 静默退回 PATH 上任意 node（乃至没有 node）
  ⇒ 构建结果与 `build_all.sh` 不一致。
- **处置**：`.bat` / `.sh` 删除该旋钮，改校验 `electron-builder.yml` 存在并在汇总打印
  `update src`；README 同步说明；`.bat` 改**通配探测**（与 `.sh` 一直以来的做法对齐）。
- **状态**：`已解决（R30）`
- **证伪方式**：`grep -rn "QMT_UPDATE_URL" build_all.* README.md` 归零；
  删掉 `QMT_NODE_DIR` 后跑 `build_all.bat`，日志里应打印解析到的托管 Node 路径。

### TD-14 A 股交易日历把「周末休市」当交易日（**R30 已闭环**）
- **原状**：内置 `WORKDAYS` 列入了交易所休市通知里的调休补班日（如 2026-01-04 周日、
  2026-10-10 周六）并判定为**交易日**。这些日期全社会上班，但证券交易/清算照休 ⇒
  `is_trading_day()` 周末返回 True（`/market/session` 声称「今天是交易日」）、
  `TradingSession.is_active()` 周末全天为 True（引擎高频空转打源）、
  `prev/next_trading_day()` 参照日算错。
- **处置**：`WORKDAYS` 内置清空（仅保留 runtime_config `market.calendar.workdays`
  作为非常规扩展口）；按沪深北交易所 2025-12-22 通知补齐 2025/2026 休市段
  （★ 2026-02-23、2026-05-04/05 原缺）；`_CALENDAR_MAX_YEAR` 2027→2026
  （2027 年安排尚未公布，「精确」属虚假精度）。
- **状态**：`已解决（R30）`（R26 修复，R30 复核确认）
- **证伪方式**：`python -c "from app.sync.calendar import is_trading_day;
  print(is_trading_day('2026-01-04'))"` 必须为 `False`；
  `tests/test_session_contract.py`（34 用例）锁「非交易日 ⇒ `phase=="holiday"`」。

### TD-15 客户端 `site-packages` 长期污染后端进程 import（**R30 已闭环**）
- **原状**：`_load_xtquant_from()` 把客户端 `bin.x64\Lib\site-packages`
  插进 `sys.path` 后**再也不摘除**。该目录属于**另一个 Python 运行时**
  （客户端内嵌解释器），捆着按那个版本编译的旧库。实测症状：客户端捆绑的
  `defusedxml` 在 py3.11 上构造 `XMLParser` 直接
  `TypeError: __init__() takes 1 positional argument`；而 openpyxl 是**首次导出 Excel
  时才 import** ⇒ **「连过券商之后，Excel 导出开始报错」**，前因后果完全看不出来。
  更甚者一次环境探测（`probe_environment`）就**永久**改变整个后端进程的
  import 解析顺序。
- **处置**：注入改为**临时**——`_added` 标记 + `finally` 摘除（成功失败都摘）。
  xtquant 子模块经包 `__path__` 解析，不依赖 `sys.path`；客户端 `bin` 目录的
  **PATH** 强化保留（DLL 解析靠 PATH）。
- **状态**：`已解决（R30）`（R26 修复，R30 复核确认）
- **证伪方式**：`tests/test_unit.py` 覆盖 `_load_xtquant_from` 后
  `sys.path` 不含客户端目录；`grep -n "sys.path.remove" backend/xtquant_client/xtp/_common.py`。

### TD-16 `build_all.bat` 不产出任何产物却退出码 0（**R30 已闭环**，★ 本轮最严重）
- **原状**：Windows 侧一键构建脚本**从来没能构建过任何东西**。实测 `build_all.bat --nsis`
  在几秒内 exit 0，日志只有 6 行（横幅 + `[build] keeping previous dist output`），
  「build complete」横幅、Step 1/2/3 的 echo、产物路径全部没有。
- **根因（两层叠加）**：
  1. **LF 行尾**：`git ls-files --eol` 显示该文件一直是 `i/lf w/lf`。`cmd.exe` 逐行读取
     .bat，LF-only 会让括号块（`for` / `if`）解析错位，报
     `'x' is not recognized as an internal or external command` /
     `) was unexpected at this time`。**实测对照**：同一份内容转 CRLF 后在 `chcp 936`
     与 `chcp 437` 下都正常 ⇒ 编码（UTF-8 中文注释）不是变量，**行尾才是**。
  2. **顶层流程落进子程序**：`:clean_dist` / `:clean_static` / `:verify_static` /
     `:verify_packaged_static` 四个 helper 放在文件中部、以 `goto :eof` 结尾
     （语义 = 返回调用点），却**没有任何 `goto` 跳过它们**。顶层自上而下落进第一个
     helper 体内并执行其 `goto :eof` —— 顶层 `goto :eof` = **结束整个脚本**。
  自 HEAD 起即如此 ⇒ 历次 v0.3.x 的包实际都由 `build_all.sh`（Git Bash）产出。
- **处置**：(a) 文件整体转 **CRLF**，头部写明「**禁止对本文件做 LF 归一化**」（附症状原文）；
  (b) 第一个 helper 前加 `goto :main`、最后一个 helper 后加 `:main`
  ⇒ 子程序只能经 `call` 进入；(c) 补 `.sh` 有而 `.bat` 缺的两道发布闸门 ——
  `call :verify_static` 的失败必须 `if !errorlevel! neq 0 (... exit /b 1)`
  （原来 `exit /b 1` 在 `call` 语境下只是返回调用点，残缺前端会被打进 EXE 且 exit 0）、
  `latest.yml` 硬核对（存在性 + `version` == 仓库根 `VERSION` + `path` 的安装包在位）、
  MCP 协议端到端（Step 4.5）。
- **状态**：`已解决（R30）`
- **证伪方式**：`git check-attr text eol -- build_all.bat` 应为 `text: set` / `eol: crlf`，
  且 `git ls-files --eol build_all.bat` 的**工作区端必须是 `w/crlf`**
  —— 行尾由仓库表示（`.gitattributes`）强制，不能只靠本地工作区，详见 TD-17；
  `grep -n "goto :main" build_all.bat` 命中；跑 `build_all.bat --nsis` 必须打印
  Step 1/2/3 与 `build complete` 横幅。
  **禁止**：不得对该文件做 LF 归一化，不得删除 `goto :main` / `:main` 这一对，
  也不得删除 `.gitattributes` 里的 `build_all.bat text eol=crlf`。

### TD-17 CRLF 修复只落在工作区：仓库 blob 仍是 LF（**R30 已闭环**，TD-16 的第二层）

- **原状**：TD-16 把工作区的 `build_all.bat` 转成 CRLF，但仓库 blob 没变：
  `git ls-files --eol build_all.bat` ⇒ `i/lf w/crlf`，而 `core.autocrlf=false`、**无 `.gitattributes`**
  ⇒ **新克隆 / CI / GitHub 源码包 / 协作方**拿到的仍是 **LF-only** 的 `.bat`
  ⇒ TD-16 的整个缺陷（构建静默空转、退出码 0）**原样复现**。
  也就是说只改工作区，等于把「Windows 一键构建不可用」留在了发布物里。
- **处置**：新增 `.gitattributes`，只针对「行尾决定脚本能否运行」的文件，不做全仓 eol 策略：
  `build_all.bat text eol=crlf`、`build_all.sh text eol=lf`、
  `scripts/verify_packaged_mcp_boot.sh text eol=lf`（CRLF 会让 shebang 变成 `/bin/bash\r`）。
  **为什么用 `text eol=crlf` 而不是 `-text` 存 CRLF 字节**：`eol=crlf` 由 git 在**检出时强制**
  写成 CRLF，即使将来有人用 LF 编辑器提交也能自我纠正；`-text` 只有「存什么给什么」，
  一旦被 LF 污染就再没有机制纠正。这类缺陷的代价是「静默无产物」，必须选能自我纠正的那种。
- **状态**：`已解决（R30）`
- **证伪方式**：`git check-attr text eol -- build_all.bat` ⇒ `text: set` / `eol: crlf`；
  `git ls-files --eol build_all.bat` 工作区端为 `w/crlf`；
  `git check-attr eol -- build_all.sh` ⇒ `eol: lf`。
- **禁止**：不得删除 `.gitattributes`，也不得把这几行改成 `-text` 或全仓通配 eol 规则。

### TD-18 CI 环境下 electron-builder 自行开启 GitHub 发布 ⇒ `exit 1` 且 `latest.yml` 丢失（**R30 已闭环**）

- **原状**：`Setup.exe`(236 MB) 与 `zip`(297 MB) **都已落地**，脚本却 `exit 1`，
  且 `dist-electron/latest.yml` **不存在**：
  `⨯ Cannot cleanup: Error: GitHub Personal Access Token is not set, neither programmatically,
  nor using env "GH_TOKEN"`。
- **根因（读 electron-builder 源码确认）**：`app-builder-lib/out/publish/PublishManager.js`
  L46~L63 —— 发布策略**未显式指定**且环境变量 `CI` 为真、当前提交又无 git tag 时，
  electron-builder **自行**推断为 `onTagOrDraft` 并置 `isPublish = true`
  （日志 `artifacts will be published if draft release exists  reason=CI detected`），
  于是在**产物全部生成之后**去 `new GitHubPublisher(...)` ⇒ 缺 `GH_TOKEN` 抛错 ⇒ 收尾任务整段跳过
  ⇒ **`latest.yml` 不落盘**。本机构建环境 `CI=true` ⇒ 每次 `build_all.bat --nsis` 都必踩。
- **处置**：`build_all.bat` / `build_all.sh` 的 electron-builder 调用**一律显式 `--publish never`**。
  依据（同文件 L158~L163）：`latest.yml` 的写出条件是 `event.isWriteUpdateInfo && target != null && …`，
  **与 `isPublish` 无关**；包内 `resources/app-update.yml`（L86~L89）同理
  ⇒ `--publish never` 只关掉上传，不削自动更新清单、不削更新源配置。
  上传资产是 `scripts/publish_release.py` 的职责，构建脚本一律不发布。
- **状态**：`已解决（R30）`
- **证伪方式**：`grep -n "publish never" build_all.bat build_all.sh` 各命中 2 处；
  `CI=true`、无 `GH_TOKEN` 时跑 `build_all.bat --desktop-only --nsis`，日志中**不得**出现
  `artifacts will be published`，且必须落地 `dist-electron/latest.yml` 并以 `exit 0` 结束。
- **禁止**：不得从构建脚本里删掉 `--publish never`（构建脚本不承担发布职责）。

---

### TD-19 `echo` 里的**裸括号**把 `if` 块提前闭合 ⇒ 脚本在最后一步中止（**R30 已闭环**）

- **原状**：TD-18 修好后重跑 `build_all.bat --desktop-only --nsis`，Step 3 与 `latest.yml` 硬核对
  **全部通过**，随后脚本停在第 379 行的 Step 4 块之前，退出码 **255**：

  ```
  [build] electron done
    latest.yml: version=0.3.5 path=qmt_work-Setup-0.3.5.exe

  . was unexpected at this time.
  ```

  代价：Step 4（构建后自检）与 Step 4.5（MCP 协议端到端）**一次都没跑**，
  而这两步正是「产物是否真能用」的判据 —— 与 TD-16 同类：**构建看着跑完了，把关的那一段没跑**。

- **根因（cmd 的块语法）**：`cmd.exe` 在**括号块内**遇到 `(` 会当作**嵌套块开始**、`)` 当作其结束，
  而 `)` 之后只允许接 `else` 或换行。Step 4 自检块里原来这一行：

  ```bat
  echo [warn] self-check did not fully pass (see output). artifacts exist, please review.
  ```

  于是：`(` 开块 → `)` 闭块 → 紧随其后的 `. artifacts exist…` 成了**块外多余 token** ⇒
  `. was unexpected at this time.`，整只 `.bat` 就地中止。
  注意这个写法在**块外**完全合法（`echo` 只是打印括号），**只有落在括号块内才炸** ——
  所以全靠「这一行在不在块里」决定成败，肉眼极难发现。
  同文件其余同类行（`^(…^)`、`"C:\Program Files (x86)\…"`、全角 `（）`）都恰好躲开了：
  转义、被引号包住、或用了全角字符（非 ASCII 括号，cmd 不认）。

- **处置**：该行改为转义写法 `^(see output^).`；并在文件头部写下这条标尺（TD-19 注释块）。
  标尺：**括号块内的 `echo` 文本，ASCII 括号必须 `^(` / `^)` 转义，或改用全角 `（）`**。
- **状态**：`已解决（R30）`
- **证伪方式**：`grep -n "echo" build_all.bat | grep "("` —— 逐行确认：块内出现 ASCII 括号的行，
  必须 `^` 转义、被双引号包住、或括号后不再有正文；
  端到端判据是 `build_all.bat --desktop-only --nsis` **以 `exit 0` 跑完并打印 `build complete` 横幅**
  （本次实测通过，未再出现 `. was unexpected at this time.`）。
- **禁止**：不得把 `^(see output^)` 改回裸括号；新增块内 `echo` 文本时先检查括号。

---

### TD-20 `where bash` 命中 WSL 启动器 ⇒ MCP 端到端**假报失败**，构建 exit 1（**R30 已闭环**）

- **原状**：TD-19 修好后脚本能跑到尾，Step 4 自检 **28/28 通过**，Step 4.5 却失败：

  ```
  [build] Step 4.5: MCP end-to-end (packaged backend, real handshake)
  适用于 Linux 的 Windows 子系统没有已安装的分发版。
  [warn] MCP end-to-end failed. Agent may not connect; please review.
  ```

  ⇒ `VERIFY_RC=1` ⇒ 整个构建 `exit 1`，**而产物完全正常**。
  这是**假失败**：闸门没测到任何真实问题，却把一次成功的构建判为失败。

- **根因**：`C:\Windows\System32\bash.exe` 在 Windows 10/11 上是 **WSL 启动器**，
  未安装 WSL 发行版时**依然存在于 PATH**，因此
  `where bash` / `command -v bash` 都会命中它，但一执行就打印上句并返回非 0。
  原实现只用 `where bash` 判断「有没有 bash」⇒ 定位到 WSL 桩 ⇒ 把桩的报错
  当成「MCP 端到端失败」。本机 PATH 上同时还有 `P:\db\PortableGit\bin\bash.exe`
  和 `WindowsApps\bash.exe`（同为 WSL 桩），只有 `C:\Program Files\Git\bin\bash.exe` 是真 bash。

- **处置**：不再信 `where`，改为**实测**——按已知安装位置依次探
  `%ProgramFiles%\Git\bin\bash.exe`、`%ProgramFiles(x86)%\Git\...`、`%LOCALAPPDATA%\Programs\Git\...`，
  都没有再遍历 `where bash` 的每一行并执行 `--version`，**必须回 `GNU bash`** 才采纳；
  一个都没有则打印 `[skip] no usable bash (Git Bash) found` 并**说明 WSL 桩为何不算**。
  `build_all.sh` 无此问题（它本身就跑在 bash 里，`bash` 必然解析到真 bash）。
- **状态**：`已解决（R30）`
- **证伪方式**：`grep -n "GNU bash" build_all.bat` 命中；
  在**未装 WSL 发行版**但装了 Git for Windows 的机器上跑
  `build_all.bat --desktop-only --nsis`，日志中**不得**出现 WSL 的
  「没有已安装的分发版」，且须打印 `MCP end-to-end passed` 并 `exit 0`。
- **禁止**：不得退回 `where bash >nul` 这种「命中即可」的判据 ——
  WSL 桩在 PATH 上是**常态**，据此判定等于把闸门交给一个必然失败的桩执行。
- **教训**：**「工具存在」与「工具可用」是两件事**。用 `where` 判命令可用性时，
  凡是有「同名桩 / 启动器」历史的命令（`bash`、`python`、`node` 皆有其例），
  都必须实跑一次版本探测再采信。

---

### TD-21 `Edit` 工具只把**改动行**写成 LF ⇒ `.bat` 行尾混排 ⇒ 括号块被 cmd 撕裂（**R30 已闭环**，★ 与 TD-16 同族、最隐蔽的一次）

- **原状**：TD-20 修好后重跑构建，脚本**全程跑通**、产物**全部产出**、
  `BUILD_EXIT=0`、Step 4 自检 `28/28`、Step 4.5 `MCP end-to-end passed` ——
  但 stderr 里多出 3 行**没人能解释**的解析噪声：

  ```
  '），' is not recognized as an internal or external command,
  '）才是执行期取值。同理' is not recognized as an internal or external command,
  '）.' is not recognized as an internal or external command,
  ```

  这三行正是 `build_all.bat` 里**我刚用 `Edit` 工具改过**的那几行块内中文注释的**后半段**。

- **根因**：`build_all.bat` 在我编辑前是「绝大多数行 CRLF」，而 `Edit` 工具
  **只把它实际改动的那几行写成裸 LF**，其余行保持不变 ⇒ 文件变成**混排行尾**。
  cmd.exe 是**逐字节**读 `.bat` 的：读到 `\n`（裸 LF）就认为该行结束，于是
  `if (...)` / `for (...)` 的**括号块在 LF 处被撕裂**，块内后半段文本被当成新命令
  去执行 ⇒ 报 `'xxx' is not recognized`。逐字节统计可复现：

  ```
  编辑后： CRLF=478 bareLF=6   （裸 LF 落在第 458~463 行 —— 恰是刚改动的块内行）
  归一化： CRLF=488 bareLF=0 bytes=24398
  ```

  对照「失败那次构建」噪声指向第 450~453 行 —— 与当时的编辑落点完全吻合。

- **为什么此前没发现**：这个缺陷的症状**极其反直觉** ——
  **脚本照跑、产物照出、退出码仍是 0**，被撕裂的只是**注释**（无副作用），
  所以没有任何一条闸门会红。一旦被撕裂的是**有效语句**（`if` 条件、
  `set`、`call`），后果就是 TD-16 的翻版：静默空转、退出码 0。
  肉眼看不出来，`git diff` 也不显眼（多数 diff 视图不显示行尾）。
  同一个「行尾」坑本项目已踩三次：TD-16（入库即 LF）/ TD-17（检出未生效）/
  TD-21（编辑引入混排）—— **三种不同的引入方式，同一个后果**。

- **处置**：
  1. **整文件行尾归一化**为全 CRLF（`CRLF=488 bareLF=0`）；
  2. 把**块内**的中文 `REM` 全部改为**纯 ASCII 短注释**，或**提到块外**
     （块外不受括号块撕裂影响），并在文件头写下这条标尺；
  3. **新增第 5 项门禁**：`scripts/ci_reconcile.py::check_line_endings()` ——
     解析 `.gitattributes` 的 `eol=crlf` / `eol=lf` 规则，**逐字节**扫描工作区文件，
     一旦出现与声明不符的行尾（含混排）即报红，并**打印确切行号**（最多 20 个）。
     这样「谁必须 CRLF、谁必须 LF」不再只靠约定，而是**每次门禁都核一遍**。
- **状态**：`已解决（R30）`
- **证伪方式**：
  1. `python scripts/ci_reconcile.py` 打印 `[✓] .gitattributes 声明的行尾与工作区一致`；
  2. 人为把 `build_all.bat` 任意一行改成裸 LF，门禁**必须报红并给出该行号**
     （实测报 `build_all.bat 要求 eol=crlf，但第 100 行 不符 → ...`）；
  3. 跑 `build_all.bat --desktop-only --nsis`，stderr **不得**出现任何
     `is not recognized as an internal or external command`。
- **禁止**：编辑 `build_all.bat` 之后**必须复核行尾**再运行；不得「看到脚本跑通了
  就当行尾没问题」——本缺陷的定义就是「跑通且退出码 0，但已经坏了」。
  块内注释**只写纯 ASCII**。
- **教训**：**「退出码 0」不等于「脚本被完整解析」**。当一段脚本的语义由
  **行边界**决定（`.bat`、`Makefile`、YAML 缩进块）时，行尾就是语法的一部分，
  必须与内容一样进门禁 —— 靠人眼和 `git diff` 一定会漏。

---

### TD-22 窗口被遮挡时 `PrintWindow` 取到空白合成层 ⇒ 渲染判据**假失败**，构建 exit 1（**R30 已闭环**）

- **原状**：同一份产物，首个完整构建时「窗口已实际渲染」检查 **PASS**
  （83 色 / 40298 bytes），此后两次 **FAIL**（1 色 / 6406 bytes）⇒ 构建 `exit 1`。
  窗口截图恒为**纯色**，看上去就像「白屏」，但报告里 27/28 其余全绿、
  客户端日志显示页面资源全部 200、WebSocket 已握手。

- **根因**：截图走 `PrintWindow(hwnd, memDC, PW_RENDERFULLCONTENT)` ——
  它取的是**窗口的 GDI 合成层**。当窗口被**其它窗口完全遮挡**时，
  Chromium 判定不可见、**停止出帧**、不再维护合成层，于是拿到的就是
  未合成的空白客户区（恒 1 种颜色）。而**同一时刻**渲染进程内部完全正常：

  ```
  CDP  -> {"nodes":200,"textLen":473,"rootKids":1,"ready":"complete",
           "text":"...平安银行 000001.SZ 11.30 -0.44% / 贵州茅台 600519.SH..."}
  窗口  -> {'distinct_colors': 1, 'dominant_share': 1.0}  bytes=6406
  整屏  -> {'distinct_colors': 405, 'dominant_share': 0.5495} bytes=243678
  ```

  整屏图目视确认：客户端窗口被浏览器 / 记事本 / 资源管理器**完全盖住**。
  即：**判据本身失效**，闸门没测到任何真实问题，却把一次成功的构建判为失败
  —— 与 TD-20 同类的**假失败**。

- **处置**（判据从「OS 位图」换成「渲染进程自证」）：
  1. `frontend-next/electron/main.cjs` 新增 `startRenderProof()`（仅 `TEST_MODE`）：
     由**渲染进程自己**周期性 `executeJavaScript` 取
     `{nodes, textLen, rootKids, readyState}` 写入 `<userData>/render-proof.json`
     （取样 30s，覆盖懒加载分片就绪）。**渲染结论必须由渲染进程给出**，
     而不是一张受遮挡影响的 OS 位图。
  2. `scripts/client_start_test.py` 新增权威判据
     **「页面 DOM 已实际渲染（非空壳）」**：`nodes >= 50 && textLen >= 100`；
     同时新增 `window_occluded()`（`WindowFromPoint` + `GetAncestor(GA_ROOT)`），
     遮挡时**明确跳过**像素判据并打印
     `[skip] 窗口被其它窗口遮挡，像素判据不适用`，且不再空等 30s。
  3. 像素判据保留为**第二条独立证据**（不遮挡时仍然要 50 色）。
- **状态**：`已解决（R30）`
- **证伪方式**：`python scripts/client_start_test.py` 报告中必须出现
  `[PASS] 页面 DOM 已实际渲染（非空壳） -> 节点 N / 正文 M 字符 / readyState=complete`；
  且总数为 `28/28 通过`、退出码 0。反向证伪：把窗口用别的窗口完全盖住再跑，
  应打印 `[skip] 窗口被其它窗口遮挡，像素判据不适用` 而**不是** FAIL。
- **禁止**：不得把「像素颜色数」恢复成唯一渲染判据 —— 它测的是**可见性**，
  不是**正确性**；两者在被遮挡时必然分叉。自动化用例也不得依赖
  `SetForegroundWindow` 之类「把窗口抢到前台」的手段来绕开遮挡。
- **教训**：**「取到的位图」不等于「页面渲染了」**。任何经由 OS 合成层
  观察 GUI 的判据，都必须先回答「此刻该层是否被维护」；否则它测的是
  窗口管理器的状态，而不是被测程序的健康度。渲染类断言应尽量走
  **渲染进程自己的接口**（CDP / `executeJavaScript`），那是唯一不受遮挡影响的事实来源。

---

## 二、本轮（R26–R29）闭环情况

| 条目 | 对应方案项 | 验收判据 | 状态 |
|------|-----------|----------|------|
| `routes/` 直写 SQL 收敛 | P0-1 | `grep -rn "ctx.db.execute\|fetchall" backend/app/routes/*.py` 归零 | ✅ **已闭环**（R27） |
| routes 不得直连 DB 门禁 | P0-1 | `check_execution_architecture.py` Gate 2；8 类变异全红 | ✅ **已闭环**（R27） |
| `app/*` ↔ `core/*` 双命名空间冻结 | P0-2 | `check_execution_architecture.py` Gate 3 报 0 处；影子模块亦拦 | ✅ **已闭环**（R27） |
| 作业队列保留上限 + 进度非幽灵键 | P0-3 | `test_r26_retention_and_progress.py` 7 passed | ✅ **已闭环**（R26） |
| 超大文件拆分 | P1-1 | 非测试文件无 >50KB；Gate 4 常驻拦截 | ✅ **已闭环**（R28） |
| 已知 flake / 未决事项看板 | P1-2 | 本文件；每条可被一条测试或一个 `grep` 证伪 | ✅ **已闭环**（R28） |
| `state.bridge/gateway` 单点读取 | P1-4 | 全仓 0 读点 + `test_state_writeonly_guard.py` | ✅ **已闭环**（R29） |
| 组合级回测预研 | P1-3 | `tools/portfolio_engine.py` + `tests/test_portfolio_engine.py`（31 用例）；`bars_cold.db` 真实历史验证；7 类变异全红 | ✅ **已闭环**（R29） |
| 前端 API 契约自动生成 | P1-5 | `scripts/check_api_contract_drift.py` 入库 + 挂进 `ci_reconcile.py` 第 4 项；4 类变异全红 | ✅ **已闭环**（R29） |

### R27–R29 实际落点（便于回滚定位）

| 动作 | 文件 |
|------|------|
| 新增仓储层 | `app/services/{alerts,apikeys,audit,backtest,account}_store.py` |
| 路由改调仓储 | `app/routes/{alerts,apikeys,audit,backtest,account,health,config,market}.py` |
| DB 探活下沉 | `core/db.py::DB.ping()`（原为路由内 `ctx.db.query("SELECT 1")`） |
| 冷仓只读数下沉 | `datasource/cold_store.py::count_rows_readonly()`（原为路由内 `sqlite3.connect`） |
| 数据集快照查询下沉 | `datasource/snapshots.py::DatasetSnapshotStore.list_recent()` |
| 门禁四规则 | `scripts/check_execution_architecture.py`（Gate 1~4） |
| 文件拆分 | `datasource/{bars_util,bound_broker,manager_quotes,manager_kline,eltdx_boards}.py`、`app/routes/market_multidim.py` |
| 只写槽位收敛 | `core/context.py::AppContext.cache_active_bridge()`、`app/bootstrap/phase_{broker,watchdogs}.py` |
| 新增守卫 | `tests/test_state_writeonly_guard.py` |
| **组合回测引擎（P1-3）** | `tools/portfolio_engine.py`（新增）、`tools/factor_backtest.py`（日期对齐修复）、`tests/test_portfolio_engine.py`（新增） |
| **契约漂移守卫（P1-5）** | `scripts/check_api_contract_drift.py`（新增）、`scripts/ci_reconcile.py`（新增第 4 项检查） |
| **可证伪性守卫常驻化** | `scripts/verify_{arch_gates,api_contract,portfolio}_falsifiable.py`（由 `output/_*_mutation_check.py` 提升入库） |

---

## 三、P2 能力扩展（单独立项，本板只登记）

| 条目 | 依赖 | 状态 | 说明 |
|------|------|------|------|
| TD-P2-01 `_broker_batch` 批量 RPC | 真券商环境 | `待真券商` | 同 TD-01 |
| TD-P2-02 第二家真实连接器（同花顺 / PTrade / 掘金） | 对应 SDK | `需拍板` | 当前仅接口契约 |
| TD-P2-03 MCP 写能力两阶段确认闭环 | 产品拍板 | `需拍板` | 目前只读；如需 Agent 下单走「拟稿→人工确认」，复用现有风控/幂等 |
| TD-P2-04 插件内核真正加载 | 单独迭代 | `待排期` | `plugins/kernel.py` 目前声明式，执行委托未落地 |
| TD-P2-05 docs 结构化站点化 | 文档专项 | `待排期` | `docs/` 下 40+ 份按日期命名方案稿，使用者入口分散 |

---

## 四、已解决（归档，不删除）

| 条目 | 解决轮次 | 判据 |
|------|---------|------|
| `mode` 别名不匹配 ⇒ 权重被当股数 ⇒ 全仓清仓 | R25 (H1) | 未知 mode 直接报错，有回归测试 |
| 持仓查询失败返回 `0.0` ⇒ 被当「无持仓」重复建仓 | R25 (H2) | 改 `float \| None`，`None`=查不到 |
| 启动补跑早于工厂注册 ⇒ 残留任务永久判死 | R25 (H3) | `defer_unknown=True` 延后判定 |
| EOD 读不存在的键 ⇒ 假 `final` | R25 (H4) | 改读 `failed` |
| 作业进度恒 0（幽灵键 `_job_id`） | R26 | `test_r26_retention_and_progress.py` |
| 作业记录只增不减 | R26 | 同上 |
| 组合净值按索引加总（日期混叠） | R29 (TD-05) | `test_portfolio_engine.py::test_legacy_portfolio_backtest_aligns_dates_not_indices` |
| 分钟线被 `[:10]` 压成一天 | R29 (TD-06) | `test_align_panel_keeps_intraday_distinct` |

---

*最后更新：2026-09-26（R26–R29 方案 §5 全量落地；P0 三项 + P1 五项均已闭环）*
