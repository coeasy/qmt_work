# 大 QMT 客户端接入 —— 设置与配置完全指南（V4 全面升级版）

> 本文是 **客户端（被连接方：大 QMT 内置 Python 策略端 + QMT 客户端软件）** 的接入与配置手册。
> 适用：券商已收紧外部直连（miniQMT `connect ret error-1` / 日志含 `pid X not allowed`），
> 需要用**跑在大 QMT 内置 Python 里的策略脚本**作为交易通道的场景（方案里的「路径 B」桥接）。
> 架构背景见 `docs/archive/UNIVERSAL_BROKER_PLATFORM_FINAL_PLAN_V4.md` §9，决策细节见 `docs/archive/BIG_QMT_COMPAT_PLAN.md`。

------------------------------------------------------------------------

## 0. 这份文档回答什么

接入大 QMT 时，**客户端侧需要完成两类设置**：

1. **QMT 客户端软件本身的前置条件**（登录、柜台、行情窗口、授权串/PID 白名单认知）—— §2；
2. **`agent_bigqmt` 策略端的配置文件 `agent_config.json`**（目录、令牌、安全阀、轮询、账户、下单枚举）—— §3、§4。

外加一项**两侧对称约束**：客户端配置里有若干字段必须与 qmt_work 后端新建连接时的配置**逐字符一致**，否则表现为「一直超时」—— §5。

------------------------------------------------------------------------

## 1. 先判断走哪条路径

| 路径 | 形态（V4 组装） | 何时用 | 客户端要做的配置 |
|---|---|---|---|
| **A：xtquant 直连** | `XtQuantV1` + `InProcess`，`client_mode=full` | 券商**未**启用 PID 白名单时**优先**（延迟最低、零部署） | 客户端正常运行极速/完整版 QMT 并登录柜台即可；qmt_work 侧选券商档案的 `full` 模式 |
| **B：内置策略桥** | `BigQmtV1` + `FileSignal` | PID 白名单封死外部直连时的**兜底**（未来主流形态） | **本文全部内容**：部署 `agent_bigqmt` + 配置 `agent_config.json` |

> 如何判断被封？qmt_work 连接诊断若出现 `ret error-1` 且 QMT 极速版日志含
> `quant session N, pid X not allowed, return`，即授权串
> `mdl_auth_xttrader/xtdata_strict_connection_check=1` 且 `no_pid_check=0` 生效
> —— 平台侧无法改写该授权串，只能走路径 B。详见 `docs/archive/BIG_QMT_COMPAT_PLAN.md` §1.1。

------------------------------------------------------------------------

## 2. QMT 客户端软件前置条件

在部署 agent 之前，请确认客户端满足：

| 项目 | 要求 | 不满足的后果 |
|---|---|---|
| **交易账号登录** | 大 QMT 主窗口（交易端口 58600）已登录、柜台连接正常、可手动下单 | agent 调 `passorder` 会因未登录而失败（能力面会标 UNKNOWN / 下单返回 BrokerError） |
| **行情窗口（58610）** | `get_full_tick` / `get_market_data` 依赖 miniquote 端口 **58610**；大窗口 58600 **只做交易** | 行情/ K 线查询失败。**对策**：开启独立行情或极简模式，或让数据面走 eltdx / 公共源降级链（qmt_work 侧自动处理） |
| **Python 策略目录可写** | QMT 的 `python/` 策略目录存在且 QMT 能挂载运行 Python 策略 | agent 无法被加载运行 |
| **授权串 / PID 白名单认知** | 知晓是否被 `mdl_auth_xttrader` 收紧（仅影响外部直连，不影响内置策略） | 误以为「QMT 坏了」；其实内置策略桥（路径 B）不受影响 |
| **Python 版本** | 内置 **Python 3.6.x**（stdlib only，无第三方库） | agent 代码必须 py3.6 兼容（见 §10），不可引入 redis/pyzmq |

------------------------------------------------------------------------

## 2.1 大 QMT 与 miniQMT（小 QMT）在「策略」上的差异 —— 本机实测

两者**同包不同进程**，各有独立数据根，策略机制**不通用**。本机（金阳光
QMT 2.1.29.0，`P:\stock\gd_qmt`）的实际形态：

| 维度 | 大 QMT（本机运行中） | 小 QMT（miniQMT / 极速版） |
|---|---|---|
| 主进程 | `bin.x64\XtItClient.exe`（PID 21120 在跑） | `bin.x64\XtMiniQmt.exe`（本机**未运行**）+ `miniquote.exe` / `minibroker.exe` |
| 数据根 | `userdata\` | `userdata_mini\`（本机有 `datadir/ datas/ down_queue_*`，但 **`userdata_mini\python\` 不存在 ⇒ 策略目录未初始化**） |
| 日志 | `userdata\log\XtClient_<日期>.log`（**注册树行只在这里**） | `userdata_mini\log\XtMiniQmt_<日期>.log`（工具已兼容该前缀，但本机无文件） |
| 策略列表来源 | **客户端持久化注册树** `config\user\root\{configFormula,lua,UiSettingConfig}`，运行期整文件字节锁；拷贝文件不注册（见 §3） | `_mini` 变体模块（本机 47 个 `*mini*.dll`，如 `Setting_mini.dll` / `BusinessCenter_mini.dll`）。具体是否扫描自身策略目录 **本机无法验证**（mini 未登录、目录不存在）——不要照搬大 QMT 的结论 |
| 本机授权状态 | `m_nShowModelResearchPanel=1`、`m_nShowModelTarderPanel=1`、`mdl_auth_forbidden_python=0`、`mdl_auth_hidemodel=0`（Python 与模型面板**未被禁用**） | `m_nLoginMiniQmt=0` ⇒ **未启用极速版登录** |
| 与路径 A/B 的关系 | 路径 B（内置策略桥）**唯一可用**：本机 `mdl_auth_xtquant=0` ⇒ `XtQuantServer` 未启动，58610 不监听，外部 xtquant 直连被封 | 若启用 mini 登录，`XtQuantServer` 可能起来，届时路径 A（`XtQuantV1`+`InProcess`）才有机会 |

> **结论（对本项目的影响）**：本部署手册面向**大 QMT 的内置 Python 策略**。
> 小 QMT 不是「同一套机制换个壳」——它的策略加载/注册逻辑由 `_mini` 模块实现，
> 且本机未启用。若将来要接小 QMT，必须以**该环境**的日志与配置文件重新取证，
> 不能沿用本手册 §3 的注册树结论。
>
> 另有两条**仅存在于本地默认表**、未出现在服务端下发授权串里的键，值得记一笔
> （`config\modules.lua`）：`mdl_auth_local_python=0`、`mdl_auth_close_python_md5=0`。
> 「本地 Python」是否另有开关，属于**待验证**项；当前实测的注册失败**不是**它造成的
> —— 因为 md5 相同的两个文件一个注册一个不注册，与 python 授权无关。

### 2.1.1 为什么「自动注册」做不到 —— 三重证据（2026-10-01 真机）

用户反复要求「全自动注入策略」，因此这里把「不可自动注册」的判据钉死，
免得下次又去试一遍：

1. **注册树整文件字节锁**：QMT 运行期间，`config\user\root\{configFormula,
   lua,UiSettingConfig}` 三个文件 `open()` 一律 `PermissionError`
   （`ERROR_LOCK_VIOLATION 33`）。连只读都读不到（要用卷影 VSS 绕过），
   更别提写。
2. **没有「明文后门」**：全量扫描 `config\` 下 58 个文本类文件
   （`.xml/.ini/.lua/.json/.txt/.cfg/.conf`），用注册树里已知策略名的
   **GBK 字节**去搜 —— **零命中**。策略名只存在于加密注册树与日志里，
   没有任何「可写的明文名单」能顺带把策略登记进去。
3. **导入包也是加密的**：`template\pythonSingleModel.rzrk` / `SingleModel.rzrk`
   共享同一个 32 字节魔数 `5e210e647f15973d8f5fba3a8d132e9142c5863f72231c504d1335ffe1cbd9e0`
   后接密文。即「导入本地策略」吃的 `.rzrk` 也不是可自行生成的纯文本包。

> 结论：自动注册需要**逆向 `XTF1`/`.rzrk` 的写入算法**，且必须在 QMT
> **关闭**时改写注册树 —— 一旦写错，用户现有的 35 个策略条目可能一起损坏。
> 收益（省一次右键）与风险（毁掉整个策略库）严重不成比例，因此**明确不做**。
> 正确的降债方式是：把「一次人工」压缩到最短（`register` 子命令给出精确菜单
> 路径 + `--reveal` 直接定位文件），并用 `check` / `--wait` 自动确认结果。

> 本机安装是**大 + 小合一**：同一根 `P:\stock\gd_qmt` 下同时有 `userdata\`
> （大）与 `userdata_mini\`（小），另有 `xtmodel\XtModel.exe`（模型引擎）和
> `mpython\pythonbalance.py`。但**注册树只有一个**（`config\user\root\`），
> 属于大 QMT；`userdata_mini\` 下**没有** `config\user\root\`，也**没有**
> `python\` 策略目录 ⇒ **小 QMT 根本没有「策略」这个概念**，
> 「在模型交易里看到策略」天然只是大 QMT 的事。

------------------------------------------------------------------------

## 2.2 内置 Chromium（CEF）远程调试：网页型面板可全量脚本化

**实测发现（2026-10-01）**：大 QMT 的若干面板是**内嵌 Chromium(CEF) 网页**
（帮助页 `/innerApi/*`、AI 策略编辑器 `/webstrategyedit/index.html`、
`webwidget/*`），且该 Chromium **开着 DevTools 远程调试端口** ——
本机 `127.0.0.1:8086`（属 `XtItClient.exe` PID 21120）：

```
GET http://127.0.0.1:8086/json/version
-> {"Browser":"Chrome/102.0.5005.115","User-Agent":"XtItClient",
    "webSocketDebuggerUrl":"ws://127.0.0.1:8086/devtools/browser/..."}
```

这给出一条**不使用鼠标坐标自动化**的路径，同时也避开了此前用 `ShowWindow`
把 Qt 主窗口弄黑屏的那类风险。工具：**`scripts/qmt_cef_cdp.py`**
（需托管 venv，它带 `websockets`）。

| 能力 | 实测结果 |
|---|---|
| CDP 握手（`origin=None`）| ✅ 成功 |
| `Target.getTargets` 枚举页面 | ✅ |
| `Runtime.evaluate` 在页面里跑 JS | ✅ |
| `GET /json/new?url=...` 开新页 | ❌ `Could not create new page` |
| CDP `Target.createTarget` | ❌ `{"code":-32000,"message":"Not supported"}` |

⇒ **我们无法自己打开面板**，只能驱动客户端**已经打开**的面板。
所以 `watch` 是核心子命令：等面板出现，再对它下手。

**策略编辑器页的桥接契约**（从 `aistrategyedit.zip` 的 JS 逆向得到；
该 zip 由 `config/clientEnv.lua` 里的 `m_aistrategyedit_url` 指向的更新服务下发）：
页面用**双桥**之一与原生通信 ——

```js
window.CefViewQuery({request: JSON.stringify({label: <名>, value: {...}})})
window.QMTQueryBridge.invokeMethod(<名>, JSON.stringify({type: <名>, value: {...}}))
```

宿主（客户端）还会把 `getFileContent()` / `setRequestEnabled()` 注入页面
（经 `__resolveGetFileContent__` 等 hook）。已知消息名：
`初始化`、`文件内容`、`修改代码`、`读取编辑器内容`、`状态`、`内置Python`、
`原生Python`、`网络策略`、`用户`、`已用`、`剩余`、`余量`、`单股趋势策略`、
`多因子策略`。

> **⚠️ 边界（别误读）**：这个页面是 **AI 写策略编辑器**，它**没有**
> "保存进注册树"的桥接 —— 登记那一步仍是**原生动作**。本工具能自动化的是
> **编辑器内容**（`inject-file` 一次性灌入 52 KB 源码、`read-editor` 读回核对），
> **不能**替客户端点「保存 / 编译」。不要把"能写编辑器"当成"能注册策略"。

```bash
python scripts/qmt_cef_cdp.py find        # 找调试端口（硬编码 8086/9222… + 客户端监听端口兜底）
python scripts/qmt_cef_cdp.py list        # 列出已打开页面
python scripts/qmt_cef_cdp.py watch --timeout 120   # 等面板出现并 dump 桥接面
python scripts/qmt_cef_cdp.py inject-file --file P:\stock\gd_qmt\python\qmt_work_agent.py
python scripts/qmt_cef_cdp.py read-editor --out logs/editor_dump.py
```

**下一步（需一次人工配合）**：在 QMT 里打开**模型研究 / 模型交易**面板
（以及「AI 写策略」），然后跑 `watch`。**若策略列表本身也是 CEF 页面**，
就能用 DOM 点击「新建策略」把最后一步也自动化；若是原生 Qt，则登记这一步
无法绕开（见 §2.1.1）。

------------------------------------------------------------------------

## 3. 部署步骤（推荐：单文件 bundle + 一次性注册）

> **⚠️ 先纠正一个直觉错误（真机实测结论，2026-10-01）**
>
> **策略列表不是「策略目录扫描」，而是客户端持久化的注册树。**
> 所以「把 `.py` 拷进 `python/` 它就会出现在「模型交易」里」——**是错的**。
>
> 决定性实验（可复算）：在策略目录里放两个 **md5 完全相同**
> （`3b69572732f16d4a0ef96033b93f9601`，同为 3562 B）的文件
> `尾盘闲置资金自动逆回购.py` 与 `尾盘闲置资金自动通用回购逆回购.py`，
> 重启客户端后**前者在列表里、后者不在**。⇒ 与文件内容、文件名、目录位置
> 统统无关，只与「客户端有没有把这条登记进注册树」有关。
>
> 注册树落在 `<QMT>/config/user/root/{configFormula,lua,UiSettingConfig}`
> （`XTF1` 容器），运行期被**整文件字节区间锁**（读会拿到 Win32
> `ERROR_LOCK_VIOLATION=33`，Python 侧表现为 `PermissionError`）。
> 写注册树的唯一入口是客户端自己的 `CFormulaOperateManager::{importFormula,
> stratImportFormula}` —— 也就是**界面上的「新建策略 / 导入策略」动作**。
>
> 两条**独立**成立的旧结论仍然有效：子目录不扫、`_` 前缀被排除
> （`_PyContextInfo.py` 在目录里却不在列表，即为判据）。
>
> 另外，QMT 只把 `passorder`/`get_trade_detail_data` 等函数注入**被挂载的那一个
> 文件**的命名空间。入口若只是 `from BIGQMT_AGENT import *`，`globals()` 指向的是
> 被导入模块 ⇒ **一个注入函数都捕获不到**，能力面全灭。所以入口必须自己就是实现。

**一键部署（推荐）**：

```bash
python scripts/qmt_agent_deploy.py deploy     # 生成单文件 bundle(默认GBK) 放进 <QMT>/python/ 顶层
python scripts/qmt_agent_deploy.py register   # 打印唯一可行的注册路径 + 判据
python scripts/qmt_agent_deploy.py config --bridge-dir <桥目录> --token <令牌>
python scripts/qmt_agent_deploy.py check      # 体检：目录/QMT进程/配置/**是否已注册**
```

`deploy` 会产出 `python/qmt_work_agent.py`，源码编码默认 **GBK**（QMT 官方口径；
遇到 GBK 表示不了的字符会列出并退回 utf-8，绝不静默写坏）。生成器是
`scripts/gen_qmt_agent_bundle.py`，**单一真源仍是本目录的 `qmt_api.py` +
`BIGQMT_AGENT.py`**，bundle 只是产物 —— 改代码后必须重新 deploy，否则 QMT 里
跑的是旧文件（这类静默漂移正是本工具要消灭的）。

<details>
<summary>手工部署（等价操作，便于理解）</summary>

1. 生成：`python scripts/gen_qmt_agent_bundle.py --out <QMT>/python/qmt_work_agent.py --encoding gbk`
2. 配置：把 `agent_config.example.json` 放到 `<QMT>/python/agent_config.json`
   （或保持旧的 `<QMT>/python/agent_bigqmt/agent_config.json`——bundle 会按序回退查找）；
3. 注册（必做，见 §3.1）→ 在「模型交易」中选中 `qmt_work_agent` 运行。
</details>

### 3.1 注册策略 + 设置自动运行（唯一需要人工的一步，约 30 秒）

**拷贝文件到此为止都不算完成。** 必须做一次会写注册树的动作，二选一：

**路径 A · 导入本地策略**
1. 左侧进 **「模型研究」**（找不到就走「模型交易」）；
2. 在策略区（图标/列表）**空白处右键**；
3. 选 **「导入本地策略」/「本地.rzrk导入」**，文件类型切到 `*.py`；
4. 选中 `<QMT>/python/qmt_work_agent.py`。

**路径 B · 新建 + 粘贴**
1. 「我的」页 → **新建策略** → **Python 策略**；
2. 全选删掉模板代码，粘贴 `qmt_work_agent.py` 的全部内容；
3. 点 **「编译」**（编译/保存才会登记进注册树）。

**注册后**：在「模型交易」里找到它 → 右键 → **设置自动运行**
（`startupAutorun`，QMT 记在 `config/user/root/UiSettingConfig`）→ 点运行
（若弹出「以下策略设置为自动运行，是否立即运行？」选确定）。

完成后 QMT **每次启动都会自动拉起该策略**，之后无需再点任何东西。

> 自证注册成功（无需看界面）：
> ```bash
> python scripts/qmt_strategy_list_probe.py --target qmt_work_agent
> ```
> 输出 `已注册: 是`，并列出 `startupAutorun`。它读的是客户端日志里的注册树
> 还原结果，**与文件在不在目录里无关**。
>
> 日志判据：`[TC::CStrategyTradeManager::loadSettings] [CStrategyLoadSetting]Account:<账号>,
> FomrlaName: qmt_work_agent, startupAutorun: true, ID:N`，随后出现
> `[TC::CMainWindow::tryAutoRunStrategy] ... automatic run`。

> **想把「导入」也自动化？** 目前不可能，§2.1.1 给了三重证据（注册树整文件
> 字节锁 + 全 `config\` 无明文名单 + `.rzrk` 亦为加密容器）。补充格式细节：
> 注册树内容是 `XTF1` 容器（表头 + 块表 + 压缩/加密数据体），
> `CFormulaTree::importFormula` 只认客户端自己序列化的格式；逆向写入的风险
> （写坏=35 个策略条目一起损坏）远大于点一次右键。
> 若要**免关闭客户端**地读取该文件做核查，可用卷影副本（管理员）：
> `([wmiclass]"root\cimv2:Win32_ShadowCopy").Create("P:\","ClientAccessible")`，
> 然后从 `\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN\...` 读，用完
> `vssadmin delete shadows /shadow={...}`。

> **把这一步压到最短**：`register` 子命令直接打印上面两条路径的精确菜单；
> 加 `--reveal` 会**打开资源管理器并选中** `qmt_work_agent.py`，导入框里直接
> 选它即可（只开 explorer，不触碰 QMT 进程）。注册并重启后：
> ```bash
> python scripts/qmt_agent_deploy.py check        # 一眼看「已登记/未登记」
> python scripts/qmt_agent_deploy.py register --wait 120   # 轮询到登记成功为止
> ```

### 3.2 自动验证（agent 自己交证据，不用人回报）

bundle 启动时会**自动**在 `bridge_dir` 下写两份证据，外部端直接读：

| 文件 | 内容 | 用途 |
|---|---|---|
| `probe_result.json` | 注入函数清单、ContextInfo 方法面、bridge_dir 写权限、config 来源 | 一次性环境自检（等价于单独跑 ENV_PROBE.py，但带上了真实 executor 画像） |
| `agent_status.json` | 心跳：`uptime_s` / `trading_enabled` / `injected` / 主循环最近异常 | 判定「策略到底在不在跑」——**心跳新鲜度**才是判据，文件存在不算 |

外部端一条命令给结论：

```bash
python scripts/qmt_agent_verify.py --bridge-dir <桥目录> --wait 60
```

它按「未通过 + 下一步动作」输出，退出码 0/2，可直接进 CI 或巡检脚本。

### 3.3 接通后端并开封下单

1. qmt_work 端新建连接，key 选 `qmt.big.bridge.file`，填**同一个** `bridge_dir` 与 `auth_token`；
2. 前端连接诊断显示 **PROBE 通过**（`captured` 含 `passorder/cancel/get_trade_detail_data`）；
3. 最后才把 `trading_enabled` 置 `true`（改 `agent_config.json` 后**重启策略**，或
   `python scripts/qmt_agent_deploy.py config --trading`），再按 §11 清单做最小仓位验证。


------------------------------------------------------------------------

## 4. 配置项完整参考（`agent_config.json`）

| 字段 | 类型 | 默认 | 必填 | 对称* | 含义 | 填错/遗漏的后果 |
|---|---|---|---|---|---|---|
| `bridge_dir` | string(绝对路径) | — | **是** | ✓ | 桥接目录；`req/ resp/ events.ndjson` 的父目录 | 缺失→启动即报错；与后端不一致→一直超时 |
| `transport` | string | `"file"` | 否 | ✓ | 传输形态。本 stdlib agent **仅支持 `file`** | 配非 `file`→agent 警告并按 file 运行（redis/zmq 需独立变体） |
| `auth_token` | string | `""` | 建议 | ✓ | 对称令牌 | 留空→不校验（任何本机进程可写请求）；两侧不同→`auth token 不匹配` 拒绝 |
| `trading_enabled` | bool | `false` | 否 | — | 下单安全阀 | `false` 时一切下单被拒（`Rejected: trading_enabled=false`） |
| `poll_interval_ms` | int | `500` | 否 | — | 轮询间隔（毫秒）；越小延迟越低 | 过小→QMT 主循环占用高；过大→成交回报延迟高 |
| `account_id` | string | `""` | 可选 | ✓ | 默认查询账户；请求带 `account_id` 时以请求为准 | 多账户且两侧都不带→查询可能落到非预期账户 |
| `passorder` | object | 见下 | 否 | — | 下单枚举覆盖（按券商版本） | 枚举不符→下单参数错误（PROBE 正常也照错） |

`passorder` 子字段（常见默认，**以你的券商 QMT 实际枚举为准**）：

| 子字段 | 默认 | 说明 |
|---|---|---|
| `opType_buy` | `23` | 买入 opType |
| `opType_sell` | `24` | 卖出 opType |
| `orderType` | `1101` | 单股/单账号/普通/股票 |
| `prType_limit` | `11` | 限价 |
| `prType_market` | `5` | 市价（对手方最优） |
| `quickTrade` | `2` | 快捷交易标记 |
| `strategyName` | `"qmt_work"` | 策略名（部分版本用于柜台路由） |

> \* **对称字段**：必须与 qmt_work 后端新建连接时的同名配置**逐字符一致**，否则两端握手失败。

------------------------------------------------------------------------

## 5. 与 qmt_work 后端连接配置的对称关系

客户端配置 ↔ 后端 `ConnectionConfig`（key=`qmt.big.bridge.file`）必须对齐的字段：

| 客户端 `agent_config.json` | 后端连接配置项 | 一致性要求 |
|---|---|---|
| `bridge_dir` | `bridge_dir` | **逐字符一致**（含盘符大小写、中文、结尾斜杠） |
| `auth_token` | `auth_token` | **逐字符一致**（无换行/空格） |
| `transport` | `bridge_transport` | 客户端恒为 `file`；后端也须为 `file` |
| `account_id` | 请求/连接默认账户 | 至少保证「请求带 account_id」或「两侧默认账号」其一成立 |

> 后端侧还有 `redis_url` / `zmq_addr` / `auth_token_id` 等字段——**仅当后端选用
> `qmt.big.bridge.redis` / `.zmq` 时才需要**，且对应的客户端 agent 必须是支持该
> 通道的独立变体（本 `agent_bigqmt` stdlib 版做不到）。默认文件桥无需关心。

------------------------------------------------------------------------

## 6. 目录协议（排障必读）

```
bridge_dir/
├── req/             qmt_work → agent       <signal_id>.json   （先写 .tmp 再 os.replace 原子落盘）
├── resp/            agent → qmt_work       <signal_id>.json
└── events.ndjson    agent → qmt_work       追加型事件流（委托/成交/错误），超 10MB 轮转为 .1
```

- 请求文件名必须等于其内容的 `signal_id`（`.json`）；不一致一律拒绝执行（`Rejected`）；
- 响应由 agent 原子写（`tmp + replace`），外部端不会读到半份 JSON；
- `events.ndjson` 每行一条 JSON，按 `seq` 递增。

> **⚠️ `seq` 不是全局水位（2026-10-01 修正，A13）**
>
> agent 的 `seq` 是**进程内**计数器（`_STATE["seq"]`，从 1 重新开始）。而
> `events.ndjson` 是**追加**文件 ⇒ 策略重启后 `seq` **回卷**。消费端若按
> `last = max(last, seq)` 去重，新会话的所有事件都会被**静默丢弃**——症状是
> 「心跳正常、PROBE 正常、指标全绿，但成交回报永远不到前端」。
>
> 外部端（`backend/connectors/transports/file_signal.py`）现在的口径是
> **字节偏移游标 + 文件身份（`st_dev`/`st_ino`）**：
> 按行读取、只推进到最后一个完整换行；文件被轮转（含轮转到**更大**文件）、
> 截断或删除重建时游标归零重读。轮转的 `.1` 是旧内容，无需消费。
> 换句话说：**消费端不依赖 `seq` 单调**，`seq` 仅供人工排查排序。

------------------------------------------------------------------------

## 7. 低延迟通道（redis / zmq）的现实

V4 组合表里 `qmt.big.bridge.redis` / `.zmq` 标记为「可用」，但**那是后端侧 transport**——
它要求客户端有一个能推送到 redis/zmq 的 agent 变体。当前 `agent_bigqmt` 跑在
**Python 3.6 标准库环境**，无法 `import redis` / `pyzmq`，因此：

- **本 agent 只能走 `file`**；在 `agent_config.json` 里把 `transport` 配成非 `file`
  会被明确警告并按 `file` 运行（不会静默失败）。
- 低延迟通道属于「未来形态」：需要单独维护一个带第三方库的客户端 agent 变体，
  不在本仓库 stdlib 约束内。默认部署请坚持 `file`。

------------------------------------------------------------------------

## 8. 常见故障对照表（V4 口径）

| 现象 | 根因 | 处置 |
|---|---|---|
| **「模型交易」里看不到策略** | ① **没做注册**——列表是客户端持久化注册树，拷贝文件永远不进去（实测：md5 完全相同的两个文件，一个在列表一个不在）；② 文件名以 `_` 开头 ⇒ 被排除；③ 入口放在子目录 ⇒ 子目录不扫 | 跑 `... deploy.py register` 按指引做一次「导入本地策略 / 新建策略」；用 `python scripts/qmt_strategy_list_probe.py --target qmt_work_agent` 确认 `已注册: 是` |
| 已导入但重启后仍看不到 | 导入时只「保存」没「编译/确定」，注册树没落盘；或客户端未重启（注册树在启动时加载） | 重新导入并在弹窗里**确定**；关掉客户端再开 |
| 策略重启后不会自动跑 | 未勾选「自动运行」 | 「模型交易」右键策略 → 设置自动运行；用客户端日志 `startupAutorun: true` 自证 |
| 心跳文件存在但诊断说没在跑 | 心跳过期（>45s）—— QMT 已关闭或策略被停止 | 看 QMT 里策略状态；关闭 QMT 后残留的心跳文件是「假存活」的主要来源 |
| 一直超时，诊断无内容 | 两端 `bridge_dir` 不一致 | 逐字符比对，注意中文/空格/盘符大小写 |
| `auth token 不匹配` | token 两侧不同 | 复制同一份，别带换行空格 |
| `未在该解析路径捕获到函数 X` | **本文件不是 QMT 挂载的入口**，或该版本的确没此函数 | 确认策略选中的是 `qmt_work_agent.py`（单文件态）；在大 QMT 里直接调用一次该函数验证。**不要据这条提示断定终端没有此接口**——能力面会诚实标 UNKNOWN |
| 下单返回 `trading_enabled=false` | 安全阀未开 | 配置置 `true` 后**重启策略** |
| 下单参数错误 | `passorder` 枚举与客户端版本不符 | 改 `passorder.*`，查 QMT 日志里的真实报错 |
| 有委托/成交但前端不刷新 | 事件缺失 | 查 `events.ndjson` 是否有新行；无则说明 `get_trade_detail_data("ORDER"/"DEAL")` 在该客户端不可用（能力面标 UNKNOWN） |
| 行情/ K 线查不到 | 大 QMT 主窗口 58600 只做交易，行情 RPC 需 miniquote **58610** | 开启独立行情/极简模式，或让数据面走 eltdx/公共源降级链 |
| 能力面 `realtime` 显示 DEGRADED | 文件桥是轮询 diff（`POLL_DIFF`），有轮询延迟 | 正常现象；`max_latency_ms≈1500` 已在契约里声明 |

> **能力探测纪律（零 mock）**：agent 只如实上报它**实际捕获到的注入函数**；
> 某能力未捕获 ⇒ 诚实 **UNKNOWN**，绝不输出「终端没有该接口」（那会掩盖桥的 bug）。
> 详见 `docs/archive/UNIVERSAL_BROKER_PLATFORM_FINAL_PLAN_V4.md` §6.2。

------------------------------------------------------------------------

## 9. 安全

- `bridge_dir` 默认是本机任意进程可写目录 ⇒ **必须**配 `auth_token`，
  并确认目录 ACL 仅当前用户可写（建议落在用户目录，如 `%USERPROFILE%/qmt_work/bigqmt_bridge/`）；
- token 校验失败的请求会被丢弃并在事件面计数；
- `signal_id` 与文件名不一致的请求一律拒绝执行（`Rejected`）；
- 首次部署保持 `trading_enabled=false`，仅在 PROBE 通过、确认连通后再开启。

------------------------------------------------------------------------

## 10. py3.6 兼容约束（改代码必读）

运行在 QMT 内置 Python 3.6.x，**禁用**：

- `dataclasses`、`:=`（walrus）、`match`、`asyncio`、`multiprocessing.shared_memory`；
- 任何第三方库（连 redis/pyzmq 都不行，标准库 only）；
- 手抄 QMT 注入函数名单（只能 `capture_qmt_injected_funcs(globals())` 捕获）。

CI 有闸门：仓库根目录执行

```bash
python scripts/check_bigqmt_agent_py36.py
```

------------------------------------------------------------------------

## 11. 免责与首次验证清单

真实下单由本 agent 直接调用柜台接口完成，资金风险自担。首次使用请按以下清单验证：

- [ ] QMT 已登录柜台、可手动下单；
- [ ] `python/qmt_work_agent.py` 已在**策略目录顶层**（不是子目录），编码 GBK；
- [ ] **已做注册动作**（导入本地策略 / 新建+编译），且
      `python scripts/qmt_strategy_list_probe.py --target qmt_work_agent`
      输出 `已注册: 是`（这一步不做，界面上永远不会出现）；
- [ ] 策略已勾选「自动运行」（客户端日志出现 `startupAutorun: true` / `automatic run`）；
- [ ] `agent_config.json` 的 `bridge_dir` / `auth_token` 与 qmt_work 后端连接配置逐字符一致；
- [ ] `python scripts/qmt_agent_verify.py --wait 60` 返回**通过**（心跳新鲜 + 自检无异常项）；
- [ ] 策略运行后日志出现 `agent 就绪: ... funcs=N trading=False`；
- [ ] qmt_work 前端连接诊断 **PROBE 通过**，`captured` 函数清单含 `passorder/cancel/get_trade_detail_data`；
- [ ] **最小仓位**下验证三条路径：① 买单 ② 卖单 ③ 撤单 均正常；
- [ ] 确认事件流 `events.ndjson` 有新增行（委托/成交回报能回到前端）；
- [ ] 以上全部通过后再把 `trading_enabled` 置 `true` 并重启策略投入实盘。
