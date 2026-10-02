# TECH_DEBT.md — 技术债 / 未决事项看板

> 建立于第 26 轮（2026-09-25），依据当时「项目功能架构梳理与优化改进方案」§5.2 P1-2（该方案稿已于 2026-09-29 文档精简时移出版外归档）。
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

### TD-23 safe-delete 护栏把**应用自身的合法删除**拦成 `SystemExit` ⇒ pytest 伪装成大面积 ERROR（**环境问题，非代码回归**）

- **现象**：逐文件跑后端全量时，从某个文件起突然变成 `N passed, M errors`，
  `short test summary` 清一色 `ERROR at setup of ...`，并伴随一长串
  `AssertionError: assert not self._finalizers`（`_pytest/fixtures.py:1221`）。
  单文件复跑**必绿**；换 `--basetemp` 目录、改并发、清干净环境**都不消失**，
  极易被判定成「代码坏了 / 夹具泄漏」。
- **真实成因（已定位到行）**：应用启动阶段会执行
  `app/bootstrap/phase_misc.py → gateway/db_backup.py::backup_once() → _prune() → _unlink() → Path.unlink()`，
  这是**产品正常的备份保留策略**（删旧备份）。而本机 Python 经 `PYTHONPATH`
  注入了宿主安全护栏 `shim/sitecustomize.py`，它把 `pathlib.Path.unlink`
  替换为 `_safe_path_unlink` → `_try_trash` → `_check_bulk_delete_guard` →
  `_exit_bulk_guard_control` → **`raise SystemExit(1)`**。该计数器
  **按 turn 累计、阈值 50**（本轮实测 `count: 86`），于是**第一次 `unlink` 就炸**。
- **为什么表现为「全部用例 ERROR」**：`SystemExit` 是 `BaseException`，不属 `Exception`。
  它在 lifespan 里抛出 ⇒ 打挂启动 ⇒ session 级 `app_client` 夹具在
  `client.__enter__()` 处崩 ⇒ **该文件余下所有用到 `app_client` 的用例**全部
  `ERROR at setup`。`assert not self._finalizers` 是**级联噪声**，不是根因；
  顺着它去查夹具泄漏会彻底跑偏。
- **判据（三条同时出现即可认定）**：
  1. `ERROR at setup`（而非 `FAILED`）；
  2. traceback 里有 `sitecustomize.py` / `_safe_path_unlink` / `SystemExit: 1`；
  3. stderr 有 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":N>50,"threshold":50,"scope":"turn"}`。
- **处置（正解）**：跑后端测试时显式关掉该护栏 ——
  `CODEBUDDY_SAFE_DELETE_ENABLED=0`（`sitecustomize.py:35` 读它；`:1274` 处
  `if _SAFE_DELETE_ENABLED:` 才去 patch `os.remove/os.unlink/os.rmdir`、
  `shutil.rmtree`、`pathlib.Path.unlink/rmdir`；置 `0` 即完全不 patch）。
  实测：同一条逐文件全量循环，关闭前该文件 `3~8 errors`，关闭后 **0 errors**。
- **同行踩到的第二个坑**：`--basetemp=<X>` 内部的 `mkdir` **不是递归的**。
  父目录不存在时报 `FileNotFoundError: [WinError 3] 系统找不到指定的路径`，
  表现**同样是**所有 `tmp_path` 用例 `ERROR at setup`。传 `--basetemp` 前
  必须先 `mkdir -p` 出父目录。
- **补充事实**：护栏对 `tempfile.gettempdir()` 之下的路径豁免，但 pytest 默认
  basetemp 下的 `pytest-of-*/garbage-*` 路径带 `\\?\` 扩展前缀，**会破坏该判定**
  —— 所以「把 basetemp 放进系统临时目录」并不能可靠绕开它。
- **状态**：`已锁环境`（**不可在源码里修** —— 生产打包态没有该 shim；
  也不应为了让测试通过而给正常删除路径加 `except BaseException`）
- **禁止**：①不得把这类 `ERROR at setup` 当成代码回归去改业务代码；
  ②不得给 `_prune()` 之类正常删除路径加 `except BaseException` / `except SystemExit`
  「兼容」护栏 —— 那会把真实的进程退出语义一起吞掉。

---

### TD-24 vitest 并行 worker 在受限 temp 环境下**静默丢测试文件** ⇒ 汇总仍报 `passed`（**环境问题，非用例失败**）

- **现象**：前端 `npx vitest run` 汇总写 `Test Files 45 passed (45)` / `47 passed (47)` /
  `48 passed (48)` —— **每次数字都不一样**，`Tests` 随之在 375 / 415 / 434 之间跳动，
  而且**全是 passed、0 failed**。但 `vitest.config.ts` 的
  `include: ["tests/**/*.test.{ts,tsx}", "src/**/*.test.{ts,tsx}"]` 实测匹配
  **49** 个文件 —— **少掉的文件既不判 failed 也不判 skipped，只是根本不存在**。
- **真实成因（已定位到行）**：vitest 在 jsdom 环境下取
  `transformMode = "web"`（`vitest/dist/chunks/resolveConfig.*.js:6546`、`:7966`），
  并把转换结果写进 `join(project.tmpDir, "web", sha1(id))`（同文件 `:6628~6643`）。
  本机宿主把 `%LOCALAPPDATA%\Temp\<随机>\` 设为**写入受限**，`writeFile` 直接
  `EPERM: operation not permitted`；该错误发生在 **worker 启动期**，vitest 仅把它
  记为 `unhandled errors`，**该 worker 负责的文件被整份丢弃**。
- **判据（三条同时出现即可认定）**：
  1. `Test Files N passed (N)` 中的 `N` **小于** `include` 实际匹配的文件数；
  2. 汇总底部有 `Vitest caught K unhandled errors`，栈里是
     `Proxy.fetch (vitest/dist/chunks/resolveConfig.*.js)` → `writeFile` → `EPERM`；
  3. 同一条命令重跑，`N` 与用例数**每次都不同**（随机丢）。
- **处置（正解）**：串行执行 —— `npm run test:serial`
  （即 `vitest run --no-file-parallelism`）。实测 **49 文件 / 461 用例全通过、0 errors**
  （耗时 24s → 122s；只有本地受限环境才需要）。
- **为什么不能只看「是不是全绿」**：这类失败**永远不会**把汇总变红，它靠
  「少跑几个文件」把红灯吃掉 —— 与 TD-23 同族：**环境故障把自己伪装成「一切正常」**。
  凡看到「文件数与 `include` 不符」，先怀疑本机环境，再怀疑代码。
- **状态**：`已锁环境`（CI 与常规开发机是普通 temp 目录，并行正常；故**不**改默认
  `npm test`，以免在正常环境白付 5 倍耗时）
- **待办**：给前端补一条与后端 `ci_reconcile.EXPECTED_TESTS` 对等的
  「用例数/文件数护栏」，让「少跑」也能红灯（见 §一 遗留）。

---

### TD-25 「有界阻塞 + 持全局锁」的启动备份 ⇒ **应用永远打不开**（**R31 已闭环**，★ 最严重的一次「静默冻死」）

- **现象 A（用户可见）**：双击应用长时间无反应 / 永远打不开。就绪广播、前端首屏一起等。
- **现象 B（开发侧，同一个 bug 的另一种脸）**：逐文件后端回归**卡死在某一个文件上**
  > 10 分钟不推进；`pytest` 已把汇总写进日志（说明用例其实跑完了），但进程不退出。
- **现场证据（py-spy dump 真实进程，非推测）**：7 个线程里 **6 个排队在
  `core/db.py:98` 的 `_rw.write()`**，其中包含：
  ```
  Thread 10152  backup_to (core\db.py:455) → self._conn.backup(dst_conn)   ← 持有写锁
  Thread  7356  upsert → _persist → reap_expired → jobs._loop              ← 等锁（lifespan 主协程）
  Thread 15080/15320/14772/16328  query → query_one                        ← 等锁
  MainThread    TestClient.__enter__ (starlette/testclient.py:698)         ← 等 portal = 永不返回
  ```
  另有一个独立进程在做 `BEGIN IMMEDIATE` 时得到 `database is locked` —— 证实**确实有写锁被占**；
  杀掉该进程后**立刻**恢复。`data/backups/` 里 8/30、9/11、9/12、9/25 各留一份 0 字节/半截的
  `.db.tmp`，正是历次被强杀的化石。
- **根因（两个缺陷叠加）**：
  1. **`core/db.py::DB.backup_to()` 把 `Connection.backup()` 包在 `with _rw.write():` 里。**
     CPython 的 `Connection.backup()` 一旦源库被**任何**其它连接持锁，就按 `SQLITE_BUSY`
     **每 0.25 s 无限重试，且不回调 `progress`** —— 调用方永远拿不回控制权（Python API
     不暴露 `SQLITE_BUSY`）。于是「一次卡住的备份」= 持有进程级写锁无限期 = **全进程读写一起排死**。
  2. **`app/bootstrap/phase_misc.py` 在启动阶段 `await asyncio.to_thread(backup_once, "startup")`。**
     虽不占用事件循环，但 **lifespan 仍在等它的结果** ⇒ 备份卡住 ⇒ lifespan 永不完成 ⇒
     `/ready` 永远 503、窗口永远白着。
- **放大条件（本机实测，说明为何偏偏这次爆）**：主库 **585 MB**，`keep=10`、
  `max_total_mb=4096`，**每次应用启动**都复制一份整库 ⇒ 11 分钟内连写 8 份（**4.4 GB**），
  卷已 **98% 满（剩 15 GB）**。此外 `_r3d` 那轮「全绿」是假象：safe-delete 护栏（TD-23）
  把 `_prune()` 拦成 `SystemExit` ⇒ **备份根本没真的落盘**；一旦按 TD-23 正解关掉护栏，
  备份真的开始写，这个缺陷才现形。
- **处置（已落地，含 3 道静态护栏）**：
  1. `backup_to()` **不再持进程级写锁**（一致性由 SQLite 备份 API 自身保证），
     分块 `pages` + `progress` 按墙钟中止（尽力而为）、**磁盘可用空间预检**
     （仅对 ≥64 MB 大库：`free < 1.5×库 + 64 MB` 就放弃）、中止/失败**清掉半份 `.tmp`**；
  2. 启动备份改 `spawn_startup_backup()`：**独立 daemon 线程 + 不等结果**；
  3. 周期/停机备份改 `_run_backup_bounded()`：**自建 daemon 线程**，不用
     `asyncio.to_thread`。
- **★ 第 3 点的独立坑（务必记住）**：`asyncio.to_thread` 用的是默认
  `ThreadPoolExecutor`，其线程是**非 daemon** 的，`concurrent.futures` 在解释器退出时
  会 `atexit` join 它们 ⇒ 备份卡死时 `asyncio.wait_for` **能**超时返回，
  但**进程仍然退不出去**（实测 `timeout 300` 强杀前一直不退出 = 用户点「关闭」关不掉）。
  凡是「可能无界阻塞」的活都**不能**丢给 `asyncio.to_thread`。
- **验收（可复现）**：
  - 正常：`lifespan 9.40s` → `GET /ready` **200**，后台线程照常产出 585 MB 备份；
  - **把 `backup_once` 换成永久阻塞**（模拟卡死）：`lifespan 9.65s` → `/ready` **200** ✔，
    卡死线程为 `daemon=True`，进程可正常退出。
- **护栏（静态，防回归）**：`tests/test_db_backup_policy.py`
  `test_backup_to_never_holds_the_process_write_lock` /
  `test_startup_backup_never_blocks_readiness`（含「禁用 `asyncio.to_thread(backup_once)`」）。
  ⚠️ 护栏扫的是**剥掉注释与字符串后的代码**（`_code_only()`）—— 否则「解释里引用被禁写法」
  会让护栏自己误报（这次就踩了）。
- **状态**：`已闭环`
- **遗留（均已闭环，2026-09-28 收尾）**：
  - **#7 测试默认关备份**：已在 `backend/tests/conftest.py::pytest_configure` 内置
    `settings.db_backup_enabled = False`（与既有的 `broker_auto_connect = False` 同模式），
    回归套件不再复制 585 MB 主库，也消除了「大库 + 持锁」对 TD-25 的放大路径。
  - **#8 历史 `.db.tmp` 残片**：已清理 `backend/data/backups/` 下 8 个 TD-25 化石
    （8/30、9/11、9/12、9/25、9/28 的 `*.db.tmp` / `*.db.tmp-journal`，约 571 MB；
    其中 9/25 那份正是 585 MB 的「冻结中」备份）。实时 `app.db` 与 7 份已完成 `*.db`
    备份保留不动。

---

### TD-26 安装包内嵌**构建者主密钥**，且掩盖了「只读安装首次启动崩」（**R39 已闭环**，★ 发布阻断）

- **现象**：`backend/dist/qmt_work/data/master.key` 出现在打包前的产物目录里，
  electron-builder 把它一并封进 `qmt_work-Setup-0.3.9.exe`。同批还有 `app.db`、
  `logs/`、`qmt_work_config.json`。
- **根因 A（运行期状态混入产物）**：写这些文件的**不是构建**，而是**构建后自检**
  （`build_all.sh` Step 4 / `client_start_test.py --target client`）——它在
  `backend/dist/qmt_work/` 里**原地跑了一次后端**，而后端按 `exe_dir()` 在 exe 同目录
  写 data/logs/config。**同一棵 dist 再打一次包**，上一轮自检的产物就被收进安装包。
  本轮正是这样触发的：首次全量构建（09:18 自检）→ `latest.yml` 版本闸门未过、**只重跑
  Step 3**（09:28）→ 09:18 写下的密钥被第二次打包带走。
- **根因 B（被 A 掩盖的崩溃）**：`core/crypto.py` 把主密钥固定在 `exe_dir()/data/master.key`。
  打包态 `exe_dir()` = `resources/backend/qmt_work/`，而桌面壳是**按资源目录只读**设计的
  （`electron/main.cjs` 把 `QMT_DB_PATH` / `QMT_LOG_DIR` / `QMT_PORT_FILE` 全部重定向到
  `userData`）。装到 `Program Files` 时该目录不可写 ⇒ 写密钥抛 `OSError` ⇒
  **后端崩在 `core.crypto` 导入期**。此路径**从未被走通**，只因为根因 A 顺手把密钥
  「带」进了包、命中的是「读回」分支 —— 典型的**「绿灯是另一个 bug 遮出来的」**。
- **口径判据（一眼看穿）**：`run.py` 的单实例锁、`.qmt_work.port`、`wal.jsonl`、
  `bars_cold.db` **全部跟随 `settings.db_path.parent`**，只有主密钥是例外。
- **处置**：
  1. `core/crypto.py`：主密钥改为 `<settings.db_path 父目录>/master.key`（口径统一）；
     `:memory:` / 相对路径退回旧位置；旧位置存在时**复制**迁移（老密文可解、可降级回退）。
  2. `build_all.sh` / `build_all.bat`：新增 `purge_dist_runtime_state`（打包前清
     `backend/dist/qmt_work` 与上一轮 `win-unpacked` 的 `data/ logs/ export/ kline_data/
     qmt_work_config.json`）+ `verify_no_runtime_state_in_package`（打包后**硬 fail**）。
  3. `tests/test_crypto.py` 增 4 例护栏（路径跟随主库目录 / `:memory:` 回退 / 升级迁移 /
     短文件重生成）。
- **★ 历史教训**：同一缺陷 2026-08-14 已手工清过一次（当时把密钥从 `<exe>/_internal/data`
  挪到 `exe_dir()/data`，以为「与 app.db 同目录」），但**只清产物、没在流水线设防** ⇒ 复发。
  凡是「靠人记得手动清」的构建卫生规则，都必须落成脚本里的门禁。
- **状态**：`已闭环`
- **验证**：门禁 `.sh` / `.bat` **双分支实测**（污染→清理后通过；跳过清理→`exit 1`；
  干净→通过）；产物目录仅剩 `_internal/` + `qmt_work.exe`；`test_crypto` 14 passed。

---

### TD-27 指标「生产者缺失」：6 个 `record_*` 从未被调用，`/metrics` 照样输出 0（**R40 已闭环**）

- **症状**：`/metrics` 里 `qmt_paper_orders_total` / `qmt_api_latency_ms_*` /
  `qmt_runtime_mode` / `qmt_errors_total` / `qmt_ws_messages_total` **永远为空或 0**。
  监控面板上这些是「一切正常」的样子，而真相是**没有任何代码写过它们**。
- **根因**：`gateway/metrics.py` 的 `record_*`（生产者）与 `render()`（消费者）之间
  没有任何契约约束。模块 docstring 只写了一句「用法：在关键路径调用 `record_*`」——
  **约定写在注释里，就没有人核**。实测 `record_paper_order` / `record_request_duration_ms` /
  `record_runtime_mode` / `record_error` / `record_ws_message` / `record_ws_clients`
  六个方法在整个仓库（含 `.venv`）**只出现一次**，即定义处。
- **同族的另一半**：一个「内存 trace 环形缓冲」（`record_trace` + `recent_traces`）
  生产者和消费者**都**缺席（没有任何路由/前端读它），属于**半落地功能** ——
  留着等于承诺一个不存在的排障能力。
- **处置**：
  1. **接线**（各 1~3 行，均放在既有分支之后，不改任何业务判定）：
     `record_paper_order` → `signal_router._paper`；`record_runtime_mode` →
     `BrokerManager._safe_start` / `connect`（经 **注入回调** `runtime_mode_fn`，
     维持「`xtquant_client` 不得反向依赖 `gateway`」的分层纪律）；
     `record_error("http")` → 全局 500 处理器；`record_ws_message` → WS 接收循环；
     `record_request_duration_ms` → 新增 `api_latency_middleware`（**只统计 `/api/v1/`**，
     否则 p95 会被前端静态资源下载带偏）。
  2. **删除半落地/冗余成员**：trace 环形缓冲整体移除（`record_trace` /
     `recent_traces` / `_recent_traces` / `_TRACE_CAP`）；`record_ws_clients` 移除
     （与 `/metrics` 路由传 live snapshot 重复 ⇒ 同一个 gauge 两个来源迟早不一致）。
  3. **门禁**：新增 `tests/test_metrics_wiring.py`（8 例）—— 从 `Metrics` 上取**全部**
     `record_*`，在**产线**代码（**排除 `tests/`**：测试自己调用一次就把门禁刷绿，
     是这类护栏最容易犯的错）里逐个核对；反向用 `render()` 的**真实输出**核对
     「每个被渲染的 `qmt_*` 都必须声明生产者」。另有自证用例防止扫描器静默失效。
- **同类清理**：`connectors/transports/__init__.py::register_transport`（零调用的
  第二注册入口，已删除并声明 `_TRANSPORTS` 为唯一注册表）、
  `gateway/db_backup.py::_size_str`（零调用私有方法，已删除）。
- **状态**：`已闭环`
- **验证**：`test_metrics_wiring.py` 8 passed；相关 9 个测试文件 155 passed；
  `ruff --select=F,E9` 全绿。

---

### TD-28 新功能把 `bigqmt_bridge.py` 顶过 50KB ⇒ **CI 上的 Gate 4 变红**（**R41 已闭环**）

- **症状**：`scripts/check_execution_architecture.py` 退出码 1：
  `backend\connectors\bigqmt_bridge.py: 51.8KB (> 50KB，请按职责拆分)`。
  该门禁**接在 `.github/workflows/ci.yml:85`** —— 也就是说这一条会让整条流水线变红。
- **根因**：R40 给桥加「存活判据」（4 个常量 + 探测方法 + 一长段说明）时，
  只想着把**假绿灯**讲清楚，没有回头量文件体积。Gate 4 的上限正是为「多个关注点
  挤在一个文件里」设的（历史上 `registry.py` 64KB、`market.py` 54KB 都是这么来的），
  而本文件当时确实同时装着**两类关注点**：
  ① 双槽位契约 / 事件泵 / 行情订阅对账 / 存活判据（**桥本体**）；
  ② canonical op → 旧 `XTQuantGateway` shape 的适配（**gateway 面**）。
- **处置**：按关注点拆出 `connectors/bigqmt_gateway.py`（`_BigQmtGateway` +
  它专属的三个 shape 助手 `_split_instrument` / `_snapshot_to_legacy` / `_raw_of`）。
  拆分判据不是行数而是**依赖方向**：那三个助手在桥本体内**零引用**（已 grep 证实），
  而桥本体自己的两个助手（`_call_with_optional_timeout` / `_canonical_to_ws`）
  在 gateway 面里同样零引用 —— 两个模块可以**单向依赖**（bridge → gateway），
  不产生环。`bigqmt_bridge.py` 51.8KB → **40.6KB**。
- **★ 为什么不是「把注释删短一点绕过」**：上限是「关注点混杂」的代理指标，
  删注释只会把指标变绿、把问题留下；下一次加功能又被顶穿，且失去一次真正的分层。
- **状态**：`已闭环`
- **验证**：Gate 4 退出码 0（`execution architecture gate: OK`）；
  `test_bigqmt_bridge_face.py` **33 passed**、`test_bigqmt_bridge_integration.py`
  **19 passed**（拆分**零行为变化** —— 测试文件只把 `_BigQmtGateway` 的 import
  指向新的规范模块，两套面仍由同一个文件一起对账）；
  `verify_arch_gates_falsifiable.py` 14/14 证伪通过、源码字节级还原。

---

### TD-29 「冻结基线」门禁的**匹配过宽** + 基线腐烂：常量/类型导入被算成「单例引用」（**R41 已闭环**）

- **症状**：`backend/scripts/check_appcontext.py` 退出码 1，报 12 个「NEW direct
  state import」。其中 `tests/test_trade_chain_contract.py` **根本没碰单例** ——
  它只写了一句 `from core.state import MSG_NO_BROKER`（一个消息**常量**）。
- **根因（两层）**：
  1. **匹配过宽**：实现是正则 `from core\.state import .*`，「从 `core.state`
     取任何东西」都算违规 ⇒ `AppState`（类型注解）、`MSG_NO_BROKER`（常量）
     一并被点名。过宽匹配有两重害：把无关文件逼进基线（基线里于是混着
     「其实没碰单例」的条目，**谎话化**）；真出现**单例**漂移时反而不显眼。
     本仓既有标准是「**AST 全量扫描，无正则糊弄**」（同目录的
     `check_execution_architecture.py` 开篇即此语），此门禁是唯一的例外。
  2. **基线腐烂**：门禁**不在 CI 上**，于是没人跑 ⇒ 基线停在很早以前。
     12 条新增里**有 1 条是产线代码**：`gateway/easytrader_facade.py` 直接
     `from core.state import state`。又一次「不接在 CI 上的门禁等于没有门禁」。
- **处置**：
  1. 改为 **AST 判定**：只认「取到可变单例 `state` 这个名字」（含 `as` 别名、
     含多名字列表），其余 `core.state` 成员一律不算。副产品：3 条**假阳性**
     基线条目（`app/bootstrap/lifecycle.py`、`tests/test_lifecycle*.py`）自动收敛。
  2. 产线那一条**真违规**改为走规约访问器：`router` 属性改用
     `core.context.active_context_or_none()`，与路由层的 `Depends(get_ctx)`
     **同源**（不再把「装配已完成的单例」当隐式假设），`FacadeError` 行为不变。
  3. 其余 11 条全是**测试**文件（含 `tests/conftest.py` 要快照/还原单例的 48 个槽位）
     ⇒ `--update` 显式接纳，并把「扫描范围**刻意含 `tests/`**」写进模块 docstring，
     避免下一个人以为那是 bug 又去放宽。
- **状态**：`已闭环`
- **验证**：门禁 `baseline=42 current=42 new=0 converged=0` → **PASS**；
  `test_connector_composition.py` 17 passed、`test_state_context_unity.py` 12 passed；
  `ruff` 全绿。

---

### TD-30 版本一致性闸门**漏掉 `package-lock.json`**：根包版本漂了 4 个版本没人发现（**R41 已闭环**）

- **症状**：升版到 `0.4.0` 时，`frontend-next/package.json` 是 `0.4.0`，而
  `frontend-next/package-lock.json` 的根包版本仍停在 **`0.3.6`** —— 差了 4 个版本，
  跨了 `0.3.7 / 0.3.8 / 0.3.9 / 0.3.10 / 0.4.0` 五次升版都没人发现。
- **根因**：`release.yml` 的版本一致性闸门核的是
  **`tag = VERSION = package.json`** 三处（`VERSION` 为单一真源），
  `package-lock.json` **不在**核查范围内。而 `npm ci`（CI 与构建脚本都用它）
  **不会**因根包 `version` 与 `package.json` 不一致而失败 —— 于是这条漂移既不报错、
  也没有任何门禁会看见，属于「**没人核 = 迟早说错话**」的同族。
- **处置**：
  1. 升 `0.4.1` 时把 `package-lock.json` 的两处根包 `version`（顶层 + `packages[""]`）
     与 `VERSION` / `package.json` 一并对齐；
  2. 记入本板，作为「版本一致性闸门应扩面到 lock 文件」的依据。
- **★ 为什么不顺手改 `release.yml`**：Release 工作流的改动只能在真实发布时验证，
  而本轮不具备该验证条件（本地无 `gh`、PAT 缺 Contents 写权限）。
  **宁可在文档里诚实标注缺口，也不要塞一条没被验证过的 CI 检查进去** ——
  本仓已有「没接 CI 的门禁会腐烂」（TD-29）与「绿灯是另一个 bug 遮出来的」（TD-26）
  两次教训，加一条未验证的闸门只会制造第三例。
- **状态**：`已闭环`（漂移已纠正；闸门扩面作为**待办**留在本节）
- **证伪方式**：`grep -n '"version"' frontend-next/package-lock.json | head -2` 与
  `cat VERSION`、`grep -n '"version"' frontend-next/package.json | head -1` 三处必须相等。

---

### TD-31 后端上浮的诊断字段**前端一个都没消费**：「现在可用吗」在界面上问不出来（**R41 已闭环**）

- **症状**：R40 为修「假绿灯」在 `connector_probe()` 里专门上浮了
  `available` / `agent_unresponsive` / `liveness_failures` / `last_agent_ok_age_s`
  四个字段（注释写着「让『曾经连上』与『现在可用』在诊断里可区分」），
  但 `frontend-next/` 里 **grep 命中数为 0** —— 连 TypeScript 类型都没声明它们。
  用户在诊断面板上看到的仍是「已连接」，问不出「**现在**还可用吗」。
- **根因**：与 **TD-27 同族**的「生产者/消费者不配对」，只是换了一条链路：
  - 后端：`connector_probe()` 是**生产者**；
  - 前端：`bigqmtProbe.ts::describeBigQmtProbe` 是**消费者**（它把探针输出压成界面结论）；
  - 两者之间**没有任何门禁**。`check_api_contract_drift.py` 只核**路径 + 查询参数**
    （后端路由签名 ↔ 前端 `query:{}`），**不核响应载荷的字段**；于是「后端多算了几个
    字段、前端不知道」这种漂移全绿通过。
  - 对比：TD-27 是因为「约定写在注释里」，这一条是因为「**载荷字段不在任何门禁的
    视野里**」—— 同一个病，两种成因。
- **处置**：
  1. `services/api/broker.ts` 的 `BigQmtProbe` 补上四个字段的**类型声明**（含 `null`
     语义：`last_agent_ok_age_s = null` 表示**从未成功**，与「掉线」不是一回事）；
  2. `bigqmtProbe.ts` 新增 `livenessTone` / `livenessLabel` / `available` /
     `lastAgentOkAgeS`，判定分三态并写清为什么：
     `warn`（连续无应答 ⇒ 不可用，并区分「曾经可用」与「从未可用」）/ `ok` / `unknown`
     （**agent 从未应答时不能报红**，否则刚打开面板或本来就没启动策略时一片假红）；
  3. `Brokers.tsx` 在「根因」行后新增「可用性」行，且 `bigQmtProbeHint` 必须兜底
     —— 否则提示行为空，用户以为一切正常（这一点由用例锁死）；
  4. 新增 8 条用例（`tests/bigqmtProbe.test.ts`），其中一条专门断言
     **「agent 元数据看着完全正常（目录一致、两个泵都在跑）但 `available=false` 时
     必须判 warn 而不是 ok」** —— 那一格就是假绿灯本体。
- **★ 未做的部分（诚实登记）**：**没有**给 `check_api_contract_drift.py` 加
  「载荷字段级」对账。原因：响应体是运行期构造的 dict（`connector_probe` 里几十个
  分支），静态推导字段集合需要引入一整套类型标注约定，成本远超本轮收益；
  故只用**测试**锁住这一个面板，并把「载荷字段级对账」登记为待办
  （与 TD-30 的「闸门扩面」同性质：宁可不做，不做未经验证的闸门）。
- **状态**：`已闭环`（本面板已对齐；载荷字段级门禁作为待办留在本节）
- **证伪方式**：`grep -rn "agent_unresponsive" frontend-next/src` 必须有命中；
  `npx vitest run tests/bigqmtProbe.test.ts` 全绿。

### TD-32 构建自检在「包内运行期状态硬门禁」之后跑，把污染写回发布包（**R42 已闭环**）

- **症状**：`build_all.sh` 的 `verify_no_runtime_state_in_package`（line 433）在**打包后、
  自检前**执行并通过；但 Step 4 自检 / Step 4.5 MCP 冒烟会**原地再启动一次打包态后端**，
  把 `data/`、`qmt_work_config.json` 等运行期状态写回
  `dist-electron/win-unpacked/resources/backend/qmt_work`。于是「433 行 verify 通过」
  **≠**「最终发出去的包干净」——中间目录 `win-unpacked` 被污染，而真正发布的 `zip`
  因在打包阶段（425 行）就已生成，反而**没有被污染**（侥幸干净）。
- **根因**：`verify` 的时序放在了**自检之前**，没有覆盖「自检自身产生的污染」。
  这正是 TD-26「绿灯是另一个 bug 遮出来的」的构建侧翻版：433 行的绿灯把
  「自检写回的污染」遮掉了，且只有当有人直接抽查 `win-unpacked` 时才暴露。
- **处置**：在自检块结束（`fi`）之后、「构建完成」之前，补一道
  `purge_dist_runtime_state` + `verify_no_runtime_state_in_package`（见 build_all.sh
  尾部注释）。这样 FINAL 产物在落盘前一定被再清一次并复验，自检污染不再逃逸。
- **状态**：`已闭环`（脚本已加尾部清场+复验；本次构建的 `qmt_work-0.4.1.zip` 经
  全量扫描确认无 `master.key`/`app.db`/`qmt_work_config.json`/应用 `data/`/`logs/`；
  中间目录 `win-unpacked` 已手动清场）。
- **证伪方式**：`grep -n "purge_dist_runtime_state" build_all.sh` 应在自检块之后还有
  **第二处**调用（尾部）；重跑 `bash build_all.sh --portable` 后
  `find dist-electron/win-unpacked -name master.key -o -name qmt_work_config.json` 应为空。

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
| 三个引擎 `_emit` 同步调用 `ws_manager.broadcast`(async) ⇒ 10 类事件丢推送 | R34 (A/B) | `tests/test_emit_event.py`（AST 扫描全量 + engines 范围，含注入探针自证） |
| 停机漏停 `algo_engine` / `strategy_runtime`（两者都能真实下单） | R34 (C) | `app/bootstrap/shutdown.py` 于 `db.close()` 前显式 stop |
| 冷仓搬移 `while True` 无上限 + 启动期线程 ⇒ 起不来也退不出 | R34 (D) | `datasource/cold_store.py` 有上限 + `phase_db` daemon 线程有界等待 |
| 账户快照循环 sleep 在 `try` 外 ⇒ 配置非法即打死循环 | R34 (E) | 间隔与 sleep 入 `try`，非有限值回退默认 |
| 批处理缓冲无界 + 循环单点异常即死 ⇒ OOM | R34 (F/M) | `deque(maxlen=20000)`；循环体 `try/except` 续跑；清理块移出 `continue` |
| `bridge.call` 无超时 ⇒ 挂死调用占满 4 线程池级联冻结 | R34 (G) | `QMT_BRIDGE_CALL_TIMEOUT=30` 硬超时 |
| POV 算法单无 `max_idx` ⇒ 无成交量回报时无限重复下单 | R34 (H) | 补同源有界退出 `max(slices*5, 20)` |
| 手动备份 `asyncio.to_thread` 非 daemon 无超时（TD-25 同族） | R34 (I) | daemon 线程 + `QMT_MANUAL_BACKUP_TIMEOUT` |
| `AlertEngine` 创建时 `ws_manager` 未就绪 ⇒ `alert` 死路径 | R34 (J) | `phase_engines` 就绪后补接 `_on_event` |
| WAL checkpoint / 回放 **静默吞** 异常 ⇒ 恢复失败却报成功 | R34 (K) | 补计数与告警；`_corrupt_lines` 计数供读路径告警 |
| 任务派发循环无保护 + 悬垂 id `KeyError` ⇒ 定时任务永久 queued | R34 (L) | 派发体 `try/except` + 退避；剔除悬垂 id |
| `limitup._triggered` 不按交易日重置 ⇒ 昨日触发票今日永不再报 | R34 (N) | `_roll_day()` 跨日清空触发记录 + K 线缓冲 |
| `limitup` 轮询 `interval` 无下限（用户 POST 直传）⇒ CPU 打满 | R34 (O) | `sleep(max(0.1, interval))` |
| 对账循环异常路径可能空转（sleep 位置） | R34 (P) | 循环体统一 `except` + sleep，`CancelledError` 上抛 |
| `alert` 走 dict 形态 `emit_event` ⇒ 内省认不出 ⇒ 漏出 WS 基线 | R34 (Q) | 调用点改三参标准形态；内省兼容 dict 字面量（基线 29→30 键） |
| `signal_confirm_ttl` 未声明 ⇒ 用户不可配置（配置漂移） | R34 (R) | 补入 `Settings` 与默认值 |
| `_RWLock.write()` 无超时 ⇒ `db.close()` 停机永久卡住（TD-25 同族） | R34 (S) | `write(timeout=…)`；`close()` 有界获取，超时跳过 checkpoint |
| `account_snapshot` / `moneyflow_cache` 无保留策略 ⇒ 主库无界膨胀 | R34 (T) | 90 天 / 30 天保留 + 迁移 28 补 `ts` 索引 |
| 前端 `sectorStocks` 参数名 `code` ≠ 后端 `sector` ⇒ 静默错误数据 | R34 (U) | 参数名对齐；记入门禁盲区 |
| CI 缺嵌入 Python 运行时却「构建成功」 | R34 (C1/C2) | `QMT_BUILD_REQUIRE_RUNTIMES=1` 硬闸门 + `fetch_runtimes --with-deps --strict` |
| `build-client.yml` 上传 `release/*`（实际产物在 `dist-electron`）⇒ 资产恒空 | R34 (C3) | 上传路径修正 |

---

## 五、通用纪律（由 TD 归纳，逐条可 grep 验证）

| 纪律 | 由来 | 验证方式 |
|------|------|----------|
| **停机路径禁止无界等待**：任何 `db.close()` / 线程 join / 锁获取 / `subprocess.wait` 必须有超时；拿不到就跳过优化步骤，绝不阻塞进程退出 | TD-25、R34 (S)(D) | `grep -n "\.wait()\|_rw.write()\|to_thread" backend/core/db.py backend/app/bootstrap/` 应无裸无超时获取 |
| **事件派发只走 `core.emit.emit_event`**，且调用点用**三参标准形态**（第二参字符串常量），否则契约内省会漏记 | R33 (F)、R34 (Q) | `tests/test_emit_event.py`（AST 全量扫描） |
| **所有常驻循环体必须整体 `try/except`**（`CancelledError` 上抛），并在**异常路径上也 sleep**；否则单点异常会静默打死整条数据链 | R34 (E)(F)(P) | `grep -n "while True" -A 3` 逐条人工核（无自动门禁） |
| **hot path 的缓冲/缓存必须有上限**（`deque(maxlen=…)` 或显式保留策略） | R34 (F)(T) | `grep -n "\.append(" backend/sync/__init__.py` 只应出现在有界容器上 |
| **「零引用」≠「孤儿」**：判定前必须回查历史决策（`docs/*_第NN轮_*.md`），项目里存在**刻意的扩展点** | R34 (Y) | 报告 `PagePlaceholder.tsx` 即为反例 |
| **契约门禁只核路径存在性，不核参数名/取值** —— 参数漂移是盲区，须人工比对签名 | R34 (U) | 尚无自动门禁（见 §建议 1） |
| **靠人记得手动清 = 迟早复发**：构建卫生规则（尤其是「产物里不许有运行期状态」）必须落成脚本里的**清理 + 硬门禁**两步，不接受口头约定 | TD-26、TD-25 | `grep -n "purge_dist_runtime_state\|verify_no_runtime_state_in_package" build_all.sh build_all.bat` |
| **`exe_dir()` 只用于「只读资源」定位**：任何**运行期可写**文件（密钥 / 库 / 锁 / 端口 / 日志）都必须跟随 `settings.db_path.parent` 或桌面壳注入的 userData 路径 | TD-26 | `grep -rn "exe_dir()" backend --include=*.py` 逐条确认不是可写文件 |
| **绿灯要问「这条路径真的被执行到了吗」**：某个 bug 可能把另一条路径的失败遮住（TD-26 的只读写失败被「包里已带密钥」遮了整轮发布） | TD-26、TD-25 | 修复后必须**构造失败场景**再验一次，而不是只看通过 |
| **没接在 CI 上的门禁会腐烂**：基线停更、假阳性堆积，最后连「它是红的」都没人知道。要么挂进 `ci.yml`，要么明确标注为本地工具 | TD-29、Gate 1（曾恒红数月） | `grep -n "python .*scripts/" .github/workflows/ci.yml` 逐个核对是否都在 |
| **触碰「阈值/计数」类门禁后，必须实测门禁本身**：加功能要量文件体积，加用例要同步 `EXPECTED_TESTS` + README 两处计数 —— 这类门禁的红不是「代码坏了」，却同样会让流水线红 | TD-28、TD-27 | `python scripts/check_execution_architecture.py` + `python scripts/ci_reconcile.py` 均退出码 0 |
| **诊断工具不能拿「陈旧快照」断言当前能力**：probe / 心跳类证据必须带**新鲜度**；陈旧时只能说「陈旧数据里未见」，绝不可写成当前缺陷（应有 `probe_stale` 标记并降级为提示） | TD-33（`qmt_agent_verify` 曾用 6.4h 前的 probe 断言「缺 `cancel`」） | `python scripts/qmt_agent_verify.py --json` 的 `probe_stale` 字段；与 `qmt_api._need` 措辞纪律同源 |
| **路径 / 文件名参与比对前必须归一**：后缀重复拼接（`.py.py`）会把「文件已就位」误判成「文件缺失」 | TD-33（`qmt_strategy_list_probe` 曾把已部署 bundle 报为缺失） | `scripts/qmt_strategy_list_probe.py --target <xxx.py>` 应显示「文件就位: 是」 |

---

*最后更新：2026-09-30（R39 发布阻断缺陷闭环 —— 安装包内嵌构建者主密钥 + 只读安装首次启动崩（两者互相掩盖）；主密钥口径统一为跟随主库目录；构建流水线新增运行期状态清理与包内硬门禁；新增 §五 三条纪律——构建卫生必须落成门禁、`exe_dir()` 只用于只读资源、绿灯要问路径是否真被执行。**R40：以 v0.4.0 作为新的公开发布版本线（代码同源 v0.3.10），与带缺陷的 0.3.9 切割；发布本身验证「版本一致性闸门 + 包内无运行期状态硬门禁 + CI 自动构建上传资产」链路可用**；**R41：修复「新功能顶穿文件体积上限」导致 CI Gate 4 变红（`bigqmt_bridge` 按职责拆出 `bigqmt_gateway`），并修掉 `check_appcontext` 的过宽匹配（正则→AST）与基线腐烂 —— 后者暴露出一条产线代码仍在直取 `core.state.state`，已改走 `core.context` 规约访问器；升版 v0.4.1（v0.4.0 的 tag 已推在更早提交上，且其说明声明「非功能新增」），并发现版本一致性闸门漏核 `package-lock.json`（根包版本漂在 0.3.6 已久之）**）*

*最后更新：2026-10-01（**R42**：构建脚本 `build_all.sh` 的「包内运行期状态硬门禁」原本跑在自检之前，被自检原地重启后端产生的污染遮出绿灯（TD-32）；已在自检块之后补尾部 `purge_dist_runtime_state` + `verify_no_runtime_state_in_package`，确保 FINAL 产物落盘前复验；本次重构建的 `qmt_work-0.4.1.zip` 经全量扫描确认无 `master.key`/`app.db`/`qmt_work_config.json`/应用 `data/`/`logs/`，发布条件达成。）*

*同期补充（**TD-33 · 2026-10-01**：两个「**诊断工具本身会说谎**」的缺陷，与假绿灯同族，都是在未有鲜活运行时强行下结论）
- ① `scripts/qmt_strategy_list_probe.py` 无条件给 `--target` 拼接 `.py` ⇒ 传入已是 `.py` 的路径会变成 `xxx.py.py`，
  把「已部署的 bundle（52 KB）」误报成「文件就位：**否**」；同时用带后缀的名字去注册列表比对 ⇒ 「已注册」恒为否。
  已改：后缀归一 + 查表一律用**基名**，两种传参（策略名 / 文件路径）均正确。
- ② `scripts/qmt_agent_verify.py` 在**策略根本没在跑**（心跳过期 6.4h）时，仍拿那份陈旧 `probe_result.json`
  断言「未捕获交易函数: `cancel`」并计入致命 `problems` —— 违反本项目自己写下的纪律
  （`qmt_api._need`：只能说「未捕获」，**不能断言终端没有**；`capture_qmt_injected_funcs(globals())` 只对鲜活运行时有效）。
  已改：引入 `heartbeat_age` / `probe_stale`，陈旧时降级为 `[!]` 提示并显式标注「不代表当前缺失」。
- ③ 清理历史无效文档 5 份：`atst*` 三份（基准代码属**另一项目** `coeasy/atst`，外部零引用）、
  `ARCHITECTURE_V3`（自述已被 V4 取代）、`qmt_work_扩展功能任务规划`（上轮漏删的无日期旧规划）；
  V4 内两处提及已改写为「内容并入本文 / 实体已删除」，其余全仓无残留引用。
- ④ 结构性结论：**大 QMT 能否运行取决于「注册树」而非文件是否拷贝**——本轮实测 bundle 文件就位 52 KB 但
  「已注册:否 / 心跳过期」，桥链路无法启动。这是 QMT 客户端侧限制（注册树需 GUI 动作，自动注册三重证据不可行），
  非平台代码缺陷；平台侧 readiness 已由 1938 用例 + 29/29 客户端自检 + `probe_stale` 诚实标注共同保证。*

### TD-34（R15，2026-10-02）：定时补数的三种「隐形浪费」+ 界面默认值与预热脚本漂移
- **① 每天一次的全市场无效补数（根因：判「数据落后」用的是日历今日，不是数据可得日）**。
  收盘后数据源约 17:00–18:00 才稳定吐出当日 K 线；`classic_screen` 原先的 freshness 判据写的是
  `bar_date or prev_trading_day`（即「今天，若不是交易日就退回上一交易日」）。于是交易日 16:15 触发定时选股时
  `expect_bar_date` = 今天，而本地最新 dt 永远是昨天 → `data_lag=True` → 触发 `auto_backfill` 全市场重扫 →
  数据源还没吐当日数据 → 补完仍是昨天 → 第二天重复。这是一次**每天必现、且永不可能成功**的全市场 RPC。
  已修：新增唯一口径 `app.sync.calendar.expected_bar_date(ready_hour=18)` —— 交易日且已过 `ready_hour` 才是今天，
  否则一律是上一交易日；周末/节假日由 R26 日历保证。`ready_hour` 可经任务参数覆盖。
  口径收敛后 `system_jobs.py` 删掉了三处内联局部导入（`from datetime import date` / `prev_trading_day` / `bar_date`），
  调用点全仓唯一 —— 口径再分叉只能靠 grep 抓得到一处。
- **② 增量同步没有「已最新」短路，每天把全市场问一遍源**。已修：`BarsSyncer(skip_fresh=True)` +
  `LocalStore.latest_dt_map()`（单次聚合查询 `MAX(dt) GROUP BY code`，500 码分批）+ `expected_bar_date` 比对。
  返回体新增 `skipped_fresh`，与历史补断点续传的 `skipped_complete` **含义分离、分别呈现**（不可相加、不可合并），
  并在 `system_jobs._detail()` 里两个都透出。默认「定时更新日线」schedule 已开启 `skip_fresh=True`。
  失败语义：`latest_dt_map` 查询抛错 → 返回 `{}` → 全部当作「不知道」→ **全部重跑**（宁可多问，不可漏同步）。
- **③ 单点任务级异常吞掉整批成果（`asyncio.gather` 的默认行为）**。已修两处，都用 `return_exceptions=True` +
  异常点名单列：`BarsSyncer.sync_many`（一只标的抛 `CancelledError` 只记它自己 `failed=1`，其余 `ok`/`bars` 全保留落库）、
  `system_jobs.classic_screen` 的多策略计算（改为并行 + 失败策略从 `results` 剔除并在 `strategy_failures` 点名，
  **只有全部失败**才向上抛）。附带修掉一个连带误报：`sync_runner` 的空股票护栏原来只判 `total`，
  「全市场已最新 → total=0」会被报成「未获取到任何股票」——惩罚刚省下的 RPC；现改为三个字段同时为空才算失败。
- **④ 界面默认值三处漂移（同一字段三个默认值，其中两处是错的）**。已修：
  `ui.ts` fallback 与 JSON 分支的 `customBg` 不一致（`#f2f4f8` vs `#0a0a0a`，后者才对，浅色背景不该出现在深色兜底里）；
  `theme-boot.js` 注释声称「回退到新默认（深色）」而代码实际回退 `"light"`；皮肤 id `tongdaxin`/`dazhihui`/`ths`
  用的是第三方行情软件名，与 `skins.ts` 自己的注释「不使用第三方行情软件名称」自相矛盾。
  现统一为：默认 `theme=dark` + `skin=midnight`（极夜黑 #000000），皮肤 id 中性化更名（`tongdaxin→midnight`、
  `dazhihui→graphite`、`ths→obsidian`），并保留 `LEGACY_SKIN_IDS` 映射（`skins.ts` 与 `theme-boot.js` 各一份，
  必须同步）——**老用户 localStorage 与服务器外观配置里的旧 id 仍可读回并自动迁移**，不为零件改名买单。
  `normalizeSkinId()` 同时用在读取与 `setSkin` 写入两侧；`resolveSkin` 出口也归一到新 id，
  否则旧 id 会让 `skins.css` 的 `[data-skin="midnight"]` 选择器全部落空 → 「选了皮肤但背景没变」。
- **⑤ 教训：给 `sync_many` 加 `skipped_fresh` 参数会打断所有替身**。首次实现时我把它加进了函数签名，
  立刻打断 `test_sync_bars_broker_fallback.py` 里 `async def _fake_sync_many(codes, progress_cb=None, skipped_complete=0)`
  这个测试替身（`TypeError`，1 failed / 77 passed）。改走**实例状态** `self._skipped_fresh`
  （`sync_stock_list` 入口复位、`sync_many` 汇总时取用），签名零改动。**这是本项目反复出现的「假绿灯家族」
  的对称面：接口加参数看似无害，实际是全局契约变更；替代方案永远是「自己的派生值自己携带」**
  （与 TD-31「后端多算字段前端零消费」同族，都是「改一面不看另一面」）。
- 新增用例 10 个（后端 1912 → 1922，已同步 `scripts/ci_reconcile.py` + README 两处 + V4 §10.5）：
  `test_sync_bars.py` ×4（整批韧性 / `_filter_fresh` 三种分支 / `skip_fresh` 默认关闭不改变既有语义）、
  `test_classic_screen_runner.py` ×2（策略失败点名不废其余结果 / `expect_date` 用数据可得日不触发补数）、
  `test_session_contract.py` ×4（`expected_bar_date` 四个边界：ready 前 / ready 后 / 周末 / `ready_hour=0` 严格模式）。
  前端 `theme.test.ts` 新增 2 例锁定旧 id 迁移（`tongdaxin → midnight` 且 `document.documentElement.dataset.skin` 同步），
  `tsc --noEmit` 与 `npm run test:serial` 50 文件 498 用例全绿。*

