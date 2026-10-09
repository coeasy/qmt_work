# 大小 QMT 使用说明（qmt_work 量化交易网关）

> 版本：v1.1（2026-10-02）——新增 §10（定时任务的数据新鲜度语义 & 界面默认深色外观）  
> 姊妹文档：
>
> - `docs/archive/BIG_QMT_COMPAT_PLAN.md`（大 QMT 兼容支持方案，可行性论证）
> - `docs/archive/UNIFIED_TRADING_ABSTRACTION.md`（统一交易接口抽象，选型论证）
> - `docs/BROKER_ONBOARDING.md`（券商接入指南，代码层）
> - `docs/API接口文档.md`（REST/WS 接口契约）
>
> 本文定位：**面向使用者 / 运维**的「大小 QMT 怎么区分、各自怎么配、接口与能力差在哪、出问题怎么查」一站式说明。不涉及抽象层设计之争，只讲「你现在该怎么做」。

---

## 0. 先读这一节：你到底用的是哪种 QMT？

绝大多数困惑来自一个事实：**「QMT」不是一个东西，而是两条形态差异极大的产品线**。先对号入座，再跳到对应章节。

| 你手上的客户端               | 俗称         | 进程 / 目录                            | qmt_work 里的叫法                       | 本文章节    |
| --------------------- | ---------- | ---------------------------------- | ----------------------------------- | ------- |
| 券商给的「极速版 / MiniQMT」   | 小 QMT      | `XtMiniQmt.exe` + `userdata_mini/` | **直连（direct）**                      | §3      |
| 券商给的「完整版 QMT / 大 QMT」 | 大 QMT（完整版） | QMT 主程序 + `userdata/`              | **直连 full（direct）** 或 **策略桥（路径 B）** | §4 / §5 |
| 同一安装目录里两套目录都在         | 大小合一安装     | 同根下 `userdata/` + `userdata_mini/` | 见 §2.4 特别说明                         | §2.4    |

**一句话决策树：**

```
你能否从外部 Python 正常 import xtquant 并 connect 成功？
├─ 能 → 用「直连（direct）」：小 QMT 填 userdata_mini，大 QMT 填 userdata（§3 / §4）
└─ 不能（connect 返 rc=-1，日志 "pid X not allowed"）→ 券商已开 PID 白名单
          └─ 你用的是大 QMT 完整版吗？
               ├─ 是 → 走「大 QMT 策略桥（路径 B）」（§5），这是被封直连后的兜底通道
               └─ 否（只有小 QMT）→ 平台当前无法接入，须引导找券商放开授权（§7.1）
```

> 行业背景：券商正逐步收紧 MiniQMT 外部直连，未来主战场是大 QMT 完整版。大 QMT 也正被同一套授权串体系收紧，所以「大 QMT 策略桥（路径 B）」是必须掌握的最终兜底形态。

### 0.1 双模式自动识别与启动（2026-10-09 起）

qmt_work **同时支持大小 QMT**，且**你启动哪个客户端，平台就用哪个模式**——这项判定不再靠猜，而是按本机**实际运行的进程**得出，并在界面上标明依据。

**判定优先级（`discovery.mode_from_proc` + `pick_representative_proc`）：**

| 本机实跑的进程 | 判定模式 | 交易数据目录 |
| --- | --- | --- |
| `XtItClient.exe`（大 QMT 主程序） | `full` | `<根>/userdata` |
| `XtMiniQmt.exe`（小 QMT 主程序） | `mini` | `<根>/userdata_mini` |
| 两者同时运行 | `full`（**确定性**优先，与连接层 `_effective_trade_dir` 口径一致） | `<根>/userdata` |
| 只有 `miniquote.exe`（独立行情子进程） | **不判定**（由目录布局兜底） | 按目录存在性 |

> **`miniquote.exe` 不是小 QMT。** 它是**行情子进程**：既可由极速版拉起，也可作为大 QMT 的「独立行情」子进程运行。它**不决定**交易数据目录，因此**不能**用来推断模式（历史缺陷：被误判为 `mini` ⇒ 大客户端机器被建议 `userdata_mini` ⇒ 交易必然 `rc=-1`）。

**界面上的两条可见证据**（「券商连接」页 · 检测到的本地客户端）：

- 模式徽章：`完整版（大 QMT）` / `极速版（小 QMT）` / `自动识别`；
- 依据徽章：`按运行进程判定`（绿，实测）/ `按目录布局推断`（灰，推测）。
  两者外观**刻意不同**——否则「实测」与「猜的」无法区分，也就无法验收。

**一键启动三个入口**（每个候选都有，各自独立）：

| 按钮 | 启动的 exe | 数据目录 | 何时用 |
| --- | --- | --- | --- |
| 启动大 QMT | `bin.x64/XtItClient.exe` | `userdata` | 要用完整版（含策略桥 B） |
| 启动小 QMT | `bin.x64/XtMiniQmt.exe` | `userdata_mini` | 券商只给了极速版 |
| 仅补行情 | `bin.x64/miniquote.exe` | 不涉及 | 大 QMT 已在跑但 58610 无服务 |

> 三者**互不冒充**：大 QMT 在跑**不会**挡住「启动小 QMT」（历史缺陷：三者共享一个「小窗口在跑」标志 ⇒ 想启动极速版被误报「已在运行」，永远拉不起来）。
> GUI 程序启动后需**你在弹出的窗口里完成登录**；接口只负责拉起进程。

---

## 1. 概念与本质差异

| 维度            | 小 QMT（极速版 / MiniQMT）                                             | 大 QMT（完整版）                                                                                                            |
| ------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| 交易进程          | `XtMiniQmt.exe` 独立进程，生成 `userdata_mini/`                         | QMT 主程序，生成 `userdata/`                                                                                                |
| 外部 xtquant 直连 | 支持（正被 PID 白名单收紧）                                                 | 部分券商同样收紧（同一授权串体系 `mdl_auth_xttrader/xtdata_strict_connection_check`）                                                  |
| 内置 Python     | **无**（外部 Python 直接 `import xtquant` 调 SDK）                       | **有**（内置 Python 3.6.x，策略脚本被挂载执行）                                                                                      |
| 内部交易 API      | 经 xtquant SDK：`XtQuantTrader.order_stock` / `cancel` / `query_*` | 注入策略命名空间的全局函数：`passorder` / `cancel` / `get_trade_detail_data`，以及 `ContextInfo` 方法族                                   |
| 行情            | xtdata 经 miniquote 端口 **58610**                                  | `ContextInfo.get_full_tick` / `get_market_data` / `download_history_data`；**大窗口 58600 仅交易，行情 RPC 仍须 miniquote 58610** |
| 回调            | `XtQuantTraderCallback` 推送（on_order / on_trade）                  | 部分有回调（如两融），普通股票账户常需**轮询 diff 合成**                                                                                     |
| 版本碎片          | xtquant pyd 按 ABI 编译（cp36~cp312）                                 | 内置 Python 老旧（3.6），无 `shared_memory`，部分函数依赖客户端版本                                                                       |
| 「策略」概念        | **无**（不存在策略注册树）                                                  | **有**（客户端持久化注册树，策略须写入注册树才会出现在列表）                                                                                      |

**最关键的三条结论（后续所有配置的前提）：**

1. 小 QMT 只有「外部直连」一条路；大 QMT 有「外部直连 full」+「内置策略桥 B」两条路。
2. 大 QMT 的「策略」不是一个文件，而是一个**客户端持久化注册树**；光把 `.py` 丢进目录**不会出现**在策略列表里（详见 §5.3）。
3. 小 QMT 完全**没有**「策略」概念——任何「策略桥」「注册树」「CEF 诊断」都只属于大 QMT，套到小 QMT 上是错的。

---

## 2. qmt_work 如何抽象两者（使用者视角）

qmt_work 对内用「Dialect（方言）× Transport（传输）」正交拆分统一了大小 QMT，但对使用者而言，**你只需要理解前端「接入模式」这一个开关**。

### 2.1 统一下单逻辑层（大小 QMT 共用，无需你关心）

所有下单（无论来自引擎、手动、批量、回测）都走唯一入口 `SignalRouter.submit`：风控（Mandatory）、幂等、大额二次确认（TOTP）、WAL 前写日志、审计、WS 事件、失败不粉饰——全部与客户端无关。**大小 QMT 共享同一套下单逻辑**，区别只在「指令怎么送达柜台」。

### 2.2 统一客户端契约层（Dialect × Transport）

| 形态                       | Dialect（方言）  | Transport（传输）                                      | 对应前端接入模式                                   |
| ------------------------ | ------------ | -------------------------------------------------- | ------------------------------------------ |
| 小 QMT 直连 / 大 QMT 直连 full | `xtquant.v1` | InProcess（进程内 SDK）/ SubprocessBridge（ABI 不匹配时子进程桥） | `direct`（空 connector_key）                  |
| 大 QMT 策略桥（路径 B）          | `bigqmt.v1`  | `file`（文件）/ `redis`（队列）/ `zmq`（同机极速）               | `bridgeFile` / `bridgeRedis` / `bridgeZmq` |

> 技术细节（可跳过）：`xtquant.v1` 把 canonical op 映射到 `XtQuantAdapter` 方法名（`place_order` 等）；`bigqmt.v1` 把 canonical op 映射到大 QMT agent 的 wire action（`PLACE` / `CANCEL_ORDER` / `QUERY_*` 等）。两者最终都汇入同一套 `SignalRouter` / `ExecutionService` / 账户页 / 事件泵，上层代码零改动。

### 2.3 前端「接入模式」开关

在「券商连接」页，接入模式 `accessMode` 的取值与后端 `connector_key` 映射如下：

| 前端接入模式          | 提交到后端的 connector_key   | 含义                                                    |
| --------------- | ---------------------- | ----------------------------------------------------- |
| `direct`（默认，留空） | \`\`（空）                | xtquant 直连：小 QMT 填 `userdata_mini`，大 QMT 填 `userdata` |
| `bridgeFile`    | `qmt.big.bridge.file`  | 大 QMT 策略桥，文件传输（零部署，秒级）                                |
| `bridgeRedis`   | `qmt.big.bridge.redis` | 大 QMT 策略桥，Redis 传输（<50ms，需装 Redis）                    |
| `bridgeZmq`     | `qmt.big.bridge.zmq`   | 大 QMT 策略桥，ZMQ 传输（同机极速）                                |

### 2.4 大小合一安装的特别说明

有些券商给的「大 QMT 完整版」安装目录下**同时**有 `userdata/`（大）和 `userdata_mini/`（小）。这是「大小合一」安装，但：

- **注册树只有一个，且属于大 QMT**。`userdata_mini/` 下没有 `config/user/root`、也没有 `python/` 策略目录。
- 因此即便目录里能看到 `userdata_mini`，小 QMT 在此安装里**仍没有「策略」概念**；你想用「策略桥（路径 B）」必须走大 QMT 的 `userdata/` + 注册树，不能套用小 QMT 的 `userdata_mini`。
- 直连时，小 QMT 永远指向 `userdata_mini`，大 QMT 直连（full）指向 `userdata`——目录不同，不要指错。

---

## 3. 小 QMT（直连）使用说明

### 3.1 适用场景

券商的「极速版 / MiniQMT」客户端，且**外部 xtquant 直连未被 PID 白名单封死**（即 `import xtquant; XtQuantTrader().connect()` 能成功，交易连接不返 `rc=-1`）。

### 3.2 快速上手（3 步，约 1 分钟）

小 QMT 不需要跑 .bat 脚本，也不需要 QMT 里手工导入策略——它本身就是直连模式，前端填 3 个字段就完事。

1. 打开前端「券商连接」页 → 点模板按钮 **「小 QMT 极速版」**（自动填接入模式 = `direct`）
2. 填 4 个字段：
   - **券商** = 你的券商（下拉里选）
   - **客户端路径** = 极速版根目录（如 `C:/QMT/userdata_mini`）
   - **资金账号** + **账户类型**
3. 点「添加连接」→ 观察「已有连接」列表出现 `连接成功` 记录

> **前置**：券商的「极速版」必须已安装且能正常登录。若 `connect` 返 `rc=-1`（PID 白名单封死），小 QMT **在 qmt_work 里无法接入**——只能找券商放开，或改用大 QMT 完整版走 §5 的策略桥路径。

### 3.3 详细部署步骤（展开版）

1. 安装券商极速版客户端，记下 `userdata_mini` 所在根目录（例如 `C:/QMT/userdata_mini`）。
2. 打开 qmt_work 前端 →「券商连接」页。
3. 接入模式选 **`direct`（直连）**。
4. 填写：
   - **券商**：在档案下拉里选对应券商（迅投系一般复用 `xtp` 适配器，无需新建）。
   - **客户端路径**：`C:/QMT/userdata_mini`（极速版）。
   - **资金账号** / **账户类型**：`STOCK`（普通）/ `CREDIT`（信用）/ `OPTION`（期权）/ `FUTURES`（期货）。
5. 点「添加连接」——后端默认建连即自动连接（autoconnect），连接会持久化，之后每次启动自动重连。

### 3.4 能力与限制

- **支持**：实时行情、历史 K 线、交易（下单/撤单）、资金、持仓、当日委托、当日成交；经 `XtQuantTraderCallback` **回调推送**委托/成交事件到前端。
- **限制**：受券商授权串 `mdl_auth_xttrader/xtdata_strict_connection_check` + `no_pid_check=0` 的 PID 白名单约束。一旦券商收紧，外部直连 `connect` 返 `rc=-1`（日志 `quant session N, pid X not allowed`），此时小 QMT **在 qmt_work 里无法接入**（小 QMT 没有策略桥兜底），只能引导找券商放开，或改用大 QMT 完整版走路径 B。

---

## 4. 大 QMT 直连（client_mode=full）使用说明

### 4.1 适用场景

你拿到的是「完整版 QMT」，且希望像小 QMT 一样从外部 Python 直连它。qmt_work 已把 `client_mode=full` 做成一等公民：`ConnectionConfig` → `registry.create_adapter` → `XTPQuantAdapter.start()` → `_effective_trade_dir()` 会在 `userdata` 与 `userdata_mini` 候选目录间互备降级。

> **⚠️ 但绝大多数券商已封禁大 QMT 直连**：`connect` 返 `rc=-1`（日志 `quant session N, pid X not allowed`）。这条路径通常只在 3–5 家券商上还活着，实测请先试 30 秒，失败则**立刻切到 §5 策略桥路径**，别浪费时间。

### 4.2 快速上手（3 步，约 1 分钟）

1. 打开前端「券商连接」页 → 点模板按钮 **「大 QMT 直连」**（自动填接入模式 = `direct`）
2. 填 4 个字段：
   - **券商** = 你的券商
   - **客户端路径** = 完整版根下的 `userdata`（**不是 `userdata_mini`**，如 `C:/QMT/userdata`）
   - **资金账号** + **账户类型**
3. 点「添加连接」→ 若 30 秒内未连接成功（多为 `rc=-1` PID 白名单）→ 切换到 §5.2.0 大 QMT 桥接路径

### 4.3 详细部署步骤（展开版）

与小 QMT 直连几乎一致，唯一区别是**客户端路径指到 `userdata`**（不是 `userdata_mini`）：

1. 接入模式选 **`direct`（直连）**。
2. **客户端路径**填 `C:/QMT/userdata`（完整版根下的 `userdata`）。
3. 其余（券商 / 资金账号 / 账户类型）同 §3.3。

### 4.4 限制

大 QMT 直连与小 QMT 直连**共用同一套授权串体系**。若券商对大 QMT 也开启了 PID 白名单，`connect` 同样返 `rc=-1`，此时应切换到 §5 的「策略桥（路径 B）」。

---

## 5. 大 QMT 策略桥（路径 B）使用说明（重点）

这是被券商封掉外部直连后的**最终兜底通道**：在大 QMT 内置 Python 里挂载一个策略脚本（我们叫它 agent），它捕获 QMT 注入的交易函数，经文件 / Redis / ZMQ 与 qmt_work 后端通信，从而把大 QMT 的内部 API 桥接成 qmt_work 能驱动的统一接口。

### 5.1 架构总览

```
qmt_work 后端（py3.11+）
   │  GenericConnector(bigqmt.v1 + file/redis/zmq transport)
   ▼
文件 / Redis / ZMQ  ←─────── 桥 ───────→  大 QMT 内置 Python（py3.6）
                                         │  python/<注册名>.py（bundle，被 QMT 挂载执行；本机实测 QMT_WORK_AGENT.py）
                                         │  capture_qmt_injected_funcs(globals())  ← 捕获 passorder 等
                                         │  轮询 diff 合成委托/成交事件 → events.ndjson
                                         ▼
                                    QMT 柜台（passorder / cancel / get_trade_detail_data）
```

- **入口必须自己就是实现**：QMT 只把 `passorder` 等函数注入**被挂载的那一个文件**的命名空间。薄壳 `from X import *` 会让 `globals()` 指向被导入模块，导致注入函数一个都捕获不到。所以产物是**单文件 bundle**（生成器默认落盘名 `qmt_work_agent.py`，但**上线时以注册树条目指向的文件名为准**）。
- **★ 落盘文件名铁律（R27）**：文件名必须 = **注册树里那条策略指向的文件名**。
  本机实测注册条目指向的是**大写** `QMT_WORK_AGENT.py`；若实际落盘的是小写
  `qmt_work_agent.py`，就会出现「列表里有、点运行报错/立刻停止」这类最难查的形态。
  用 `--filename` 显式指定（`deploy_qmt_work_agent.bat` 已内置该变量，见 §5.6）。
- **★ 源码编码铁律（R27）**：bundle **必须 UTF-8**（`deploy` 默认即是）。QMT 内置
  `bin.x64/pythonw.exe` 是 **Python 3.6.8**，读 `#coding:gbk` cookie 的源文件会在
  特定内容下报 `SyntaxError: encoding problem` / `invalid token`，进程 `return code:1`
  且**一条自检都不落盘**。详见 §5.7 / §5.8。
- **自动验证**：bundle 启动即写 `probe_result.json`（自检）+ `agent_status.json`（心跳，每 10s）。**心跳新鲜度才是「策略在跑」的判据**——文件残留 ≠ 活着（可能已崩，见 §7.2）。
- **自动拉起**：在 QMT 客户端开启 `tryAutoRunStrategy`，每次登录自动拉起策略。

### 5.2 部署步骤

#### 5.2.0 快速上手（Windows 双击 + 前端连桥，约 5 分钟）

Windows 用户**推荐走这条路径**：脚本自动找 QMT 目录、生成 bundle、生成 config 模板、打开资源管理器选中 bundle，前端有「大 QMT 桥接」模板按钮一键填模式。你只需在 QMT 里点一下「导入本地策略」，其它字段照着 `agent_config.json` 抄。

1. **双击 `deploy_qmt_work_agent.bat`**（仓库根目录）
   - **通用探测 QMT 目录（不写死任何券商/盘位）**：先看 `QMT_DIR` 环境变量 → 再看命令行参数 →
     再交给 `qmt_agent_deploy.py discover` 逐盘符扫 1~2 层（判据 = 该目录下有 `python\` 策略目录，
     且命中 `bin.x64` / `userdata` / `config/user/root` / `user.dat` / `userdata_mini` / `xtitdata` 任一）
     → 都没命中才提示你手填。**换券商、换盘位、换机器都不用改脚本。**
   - 生成单文件 bundle（文件名由脚本里的 `AGENT_FILE` 决定，**本机实测注册树条目 = 大写 `QMT_WORK_AGENT.py`**）+ 一份 `.txt` 副本（备用路径 B 粘贴用）
   - **写盘前先备份旧文件**为 `.bak.<epoch>.py`，再**原子替换**（先写临时文件再 `os.replace`）——
     半写入不会留下截断的 agent，随时可回滚
   - **写出后会立刻用 QMT 内置解释器把 bundle 真跑一遍**（前置门禁）：跑不起来就中止部署并给出三条常见原因，不会让你带着一份「跑不起来的东西」去点运行
   - 若 `agent_config.json` 不存在则从模板拷贝，存在则保留用户原配置
   - 打开资源管理器并**直接选中** bundle 文件，方便下一步拖选
   - **同时记下 `agent_config.json` 里的两个字段**（下一步要用）：`bridge_dir` 和 `auth_token`
2. **在 QMT 里导入策略**（约 30 秒，唯一不可自动化的步骤）
   - **路径 A（推荐）**：「模型研究」→ 策略区 → 右键 → 「导入本地策略」/「本地.rzrk导入」→ 选上一步打开的 `QMT_WORK_AGENT.py`
   - **路径 B（备用，QMT「导入本地策略」被券商禁时才用）**：「我的」→ 新建策略 → Python 策略 → 全选删除模板 → 记事本双击 `QMT_WORK_AGENT.txt` 全选复制粘贴 → 点「编译」保存（编译/保存才会登记进注册树）
3. **关闭并重启 QMT**（注册树落盘要重启才生效）
4. **双击 `diag_qmt_work_agent.bat`** 验证 QMT 端
   - 应显示：`已注册: 是`、`心跳新鲜`、`registered: true`、`alive: true`
   - 想让策略随 QMT 自动拉起：在「模型交易」里选中策略 → 勾选「自动运行」（记进 `UiSettingConfig`，QMT 每次启动会自动拉起）
   - 报告自动写入 `output/diag_report.json`（排障日志分享 / 开发者定位用）
5. **在前端连桥**（约 1 分钟）
   - 打开前端「券商连接」页 → 点顶部模板按钮 **「大 QMT 桥接（推荐）」**（自动填接入模式 = `bridgeFile`）
   - 填 4 个字段：
     - **券商** = QMT（下拉里选）
     - **桥接参数** = `agent_config.json` 里的 `bridge_dir`（**完整绝对路径**，两端必须一致）
     - **桥接令牌** = `agent_config.json` 里的 `auth_token`
     - **资金账号** + **账户类型**（股票/信用/期权/期货）
   - 点「添加连接」→ 后端启动桥接子进程握手，「已有连接」列表里出现一条 `连接成功` 记录即完成

> **为什么这 5 步里的第 2 步不能自动化？** —— 「导入本地策略」是 QMT 客户端 GUI 对话框（原生 Win32，不是 CEF webview，CDP 摸不到）；注册树是加密容器（XTF1），逆向写有写坏现有 35 条策略的风险。这三重证据见 §5.2.1 步骤 3。

---

#### 5.2.1 详细展开版（6 步）

> 前置：你已安装大 QMT 完整版，且能用它的客户端打开「策略」相关界面。  
> Windows 用户走上面「快速上手 5 步」即可，本节是展开版供排障参考。

**步骤 1 · 生成 bundle**

```bash
# 生成器真源 backend/agent_bigqmt/，产物是单文件 bundle
python scripts/gen_qmt_agent_bundle.py
# 或直接用部署工具（推荐，一步到位含校验 + 前置真跑门禁）
#   --filename 必须 = 注册树里那条策略指向的文件名（本机实测为大写 QMT_WORK_AGENT.py）
#   --encoding 默认且必须 utf-8（gbk/gb18030 在 QMT 内置 py3.6 上会「启动即停止」）
python scripts/qmt_agent_deploy.py deploy --qmt-dir <你的大QMT根> \
    --filename QMT_WORK_AGENT.py --encoding utf-8 --txt
```

产物（默认名）须放到大 QMT 的：

```
<大QMT根>/python/<注册树条目指向的文件名>        # 本机实测：QMT_WORK_AGENT.py
```

**步骤 2 · 配置 agent**

配置查找顺序（找到第一个即用）：

1. `python/agent_config.json`
2. `python/agent_bigqmt/agent_config.json`
3. `python/../agent_config.json`
4. `EMBEDDED_CONFIG`（`--embed-config` 内嵌，用 `pprint` 渲染 Python 字面量；注意 JSON 的 `true/false` 在 py 源码里非法，必须用 `pprint` 渲染）

至少包含：桥接模式（file/redis/zmq）、对应参数（file=桥目录 / redis=连接串 / zmq=tcp 地址）、token（与前端填的一致）。

**步骤 3 · 在大 QMT 客户端写注册树（GUI 动作，无法自动）**

这是最容易被忽略的一步：**bundle 放进 `python/` 不会自动出现在策略列表**。策略列表来自客户端持久化注册树，不是目录扫描。你必须在大 QMT 客户端里做「导入本地策略 / 新建策略 + 编译」这类**写注册树的 UI 动作**，把 `<注册树条目指向的文件名>`（本机实测 `QMT_WORK_AGENT.py`）登记进去。

> 为什么不能自动注册？三重证据：①注册树运行期整文件字节区间锁（`ReadFile` 返 Win32 33 / Python `PermissionError`），运行期无法安全改写；②全 `config/` 文本文件按已知策略名 GBK 字节搜 = 零命中（无明文后门）；③`.rzrk` 导入包同为加密容器。逆向写入 = 写坏 35 条策略的风险，故不支持。


**步骤 4 · 启用自动运行**

在大 QMT 客户端开启 `tryAutoRunStrategy`（登录自动拉起），或直接手动「运行」该策略。观察日志出现 `automatic run` 即表示已拉起。

**步骤 5 · 前端登记桥接连接**

1. 前端「券商连接」页 → 接入模式选 **`bridgeFile` / `bridgeRedis` / `bridgeZmq`**。
2. 填**桥接参数**：
   - `file`：桥目录（与 `agent_config.json` 里的 file 路径一致）
   - `redis`：Redis 连接串
   - `zmq`：tcp 地址
3. 填 token（与 agent 端一致）、资金账号、账户类型。
4. 点「添加连接」→ 后端启动桥接子进程握手（add_broker 默认 autoconnect）。

### 5.3 三种传输对比与选型

| 传输         | 延迟       | 部署成本             | 稳定性     | 何时用                     |
| ---------- | -------- | ---------------- | ------- | ----------------------- |
| `file`（默认） | 秒级（轮询间隔） | 零（只需共享目录）        | 高（组件最少） | **首选兜底**，没装 Redis/ZMQ 时 |
| `redis`    | <50ms    | 需装 Redis 或 pyzmq | 高       | 多策略并发、低延迟需求             |
| `zmq`      | 同机极速     | 需装 pyzmq         | 高       | 单机同进程、极致低延迟             |

单一配置键切换，**协议不变**，三种传输共用同一份 `BrokerAdapter` 映射。

### 5.4 存活判据与诊断（前端可见）

大 QMT 桥的「连没连上」不能只看文件残留，必须看**一次真实往返**。后端 `is_connected()` 语义是「现在可用」：

```
is_connected() = self._connected and not self._agent_unresponsive
```

前端「券商连接」页对每条桥接连接展示存活标签（来自后端 `connector_probe()` 的字段）：

| 字段                       | 含义                 | 前端呈现      |
| ------------------------ | ------------------ | --------- |
| `available`              | 桥整体可用              | 基础连通      |
| `agent_unresponsive`     | agent 心跳超时（策略可能已崩） | warn / 断开 |
| `liveness_failures`      | 连续存活探测失败次数         | 重试计数      |
| `last_agent_ok_age_s`    | 距上次成功往返的秒数         | 「X 秒前正常」  |
| `livenessLabel` / `hint` | 综合判定的可读标签与建议       | 连接卡片上的状态条 |

> 典型「假绿灯」陷阱：文件残留 `agent_status.json` 还在，但策略进程已崩、心跳不再刷新 → 旧文件让面板显示「已连接」，实际已死。以 `last_agent_ok_age_s` 与真实往返为准，不要只看文件存在。

### 5.5 CEF 远程调试（高级诊断）

大 QMT 的 CEF 远程调试端口 `127.0.0.1:8086`（属 `XtItClient.exe`，`Chrome/102`）可脚本化驱动**已打开**的网页型面板（`/webstrategyedit/*`、`/innerApi/*`、`webwidget/*`）。但 `/json/new` 与 `Target.createTarget` 被拒，只能驱动已开的页，不能自己开页。

工具：`scripts/qmt_cef_cdp.py {find|list|watch|inspect|eval|inject-file|read-editor}`。

### 5.6 工具链速查

| 工具                                                                                          | 用途                                                                           |
| ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| **`deploy_qmt_work_agent.bat`**                                                             | **Windows 双击**：一键部署（**通用探测 QMT 目录** → 备份 + 原子替换生成 bundle + config → **用 QMT 内置解释器实跑一遍** → 打开资源管理器 → 打印下一步指引）。可传参：`--txt` `--reveal` `--no-run-check` `--force`，首个非选项参数 = QMT 目录 |
| **`diag_qmt_work_agent.bat`**                                                               | **Windows 双击**：一键诊断（跑 probe + verify + inspect + 结构化报告四合一）。同样**不写死任何券商路径**；可传首个参数指定 QMT 目录，或设 `QMT_DIR` 环境变量 |
| `scripts/qmt_agent_deploy.py {deploy\|register\|check\|config\|inspect\|discover} [--reveal] [--txt] [--filename X.py] [--encoding utf-8]` | 部署 / 登记 / 检查 / 配置 / 检视 bundle / **`discover` 只读探测 QMT 安装目录**（打印 `QMT_DIR=` / `QMT_STRATEGY_DIR=` / `QMT_RUNNING=`，未找到打 `(none)`，**退出码恒 0**，供 .bat 与自动化消费）。`--txt` 额外产一份 .txt 副本供路径 B 粘贴；`--filename` **必须与注册树条目指向的文件名一致**，本机实测为大写 `QMT_WORK_AGENT.py`；`--encoding` **默认且必须 utf-8** |
| `scripts/gen_qmt_agent_bundle.py [--out X.py] [--txt] [--embed-config CFG] [--encoding utf-8]`                 | 从 `backend/agent_bigqmt/` 生成单文件 bundle（**默认 utf-8**）                            |
| `scripts/qmt_agent_local_run.py --bundle X.py --mode {process,framework,auto}`              | **本地跑通验证器**（R27 起）：`process` = 用真解释器按 `python -u <策略.py> <userdata> <ts>` 拉起；`framework` = 复刻终端 `exec` 加载（**故意不给 `__file__`**）。`deploy` 已把它接成前置门禁 |
| `scripts/qmt_strategy_list_probe.py --target X [--qmt-dir ...]`                             | 探查策略注册树里是否已登记                                                                |
| `scripts/qmt_agent_verify.py [--json]`                                                      | 注册态 + 心跳一起判（是否真在跑），JSON 输出可直接给程序消费                                           |
| `scripts/qmt_cef_cdp.py`                                                                    | CEF 面板 CDP 诊断                                                                |
| `scripts/check_bigqmt_agent_py36.py`                                                        | G3 校验：bundle 入口捕获的注入函数名字面量是否 ≤3（必须走 `capture_qmt_injected_funcs(globals())`） |
| `qmt_agent_verify.py --json` → `bundle.*` 字段                                              | **P0 护栏**（R17 起语法/污染，**R27 起加编码**）：AST 语法 + 污染签名 + **源码编码是否 QMT 内置 py3.6 能读** |

> **同一批能力也有两种非脚本入口**（三者共用同一套通用探测逻辑，都不写死券商路径）：
>
> - **Web 界面**：「系统 → 大 QMT 部署」页（`QmtAgentDeploy.tsx`）可视化状态 / 部署 / 诊断 / 配置。
> - **REST API**：`GET /api/v1/qmt-agent/status` · `GET|POST /api/v1/qmt-agent/config` ·
>   `POST /api/v1/qmt-agent/deploy` · `POST /api/v1/qmt-agent/diagnose` ·
>   `GET /api/v1/qmt-agent/bundle` · `GET /api/v1/qmt-agent/tools` ·
>   `GET /api/v1/qmt-agent/distribute/status` · `POST /api/v1/qmt-agent/distribute/{check,pull}`。
>   字段口径与 curl 示例见 [`多语言接入指南.md`](多语言接入指南.md) §7「大 QMT Agent 部署」。

### 5.7 常见部署错误 → 解决方案

| 症状                                         | 根因                                                         | 解决                                                                                                                        |
| ------------------------------------------ | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 「模型交易」里看不到 `qmt_work_agent`                | 注册树未登记（bundle 已拷贝但未在 QMT GUI 里做导入动作）                       | 走 §5.2.0 步骤 2：QMT「模型研究」右键「导入本地策略」→ 选实际落盘的那个文件（本机实测 `QMT_WORK_AGENT.py`）→ 重启 QMT                                                         |
| `qmt_agent_verify` 报「心跳已过期 Xs（阈值 45s）」     | 策略未在跑（未启动 / 已崩 / 未启动 autorun）                              | ①在 QMT「模型交易」手工点「运行」启动一次；②想自动拉起就勾「自动运行」（记进 `UiSettingConfig`）；③确认 `agent_config.json` 路径正确                                 |
| `qmt_agent_verify` 报 `registered: false`   | 同上（注册树未登记）                                                 | 同「模型交易里看不到」                                                                                                               |
| `qmt_agent_verify` 报 `probe_stale: true`   | 上面结论只是「陈旧快照里未见」，不代表当前缺失                                    | 先让策略真正启动一次（生成新的 `probe_result.json`），再跑 verify                                                                            |
| 桥连不上但前端无错                                  | `agent_config.json` 里 `bridge_dir` 与前端桥接参数不一致（**日常排障第一名**） | 两处必须**完全一致**，包括绝对/相对路径与斜杠方向                                                                                               |
| `trading_enabled: false`                   | 默认下单关闭（安全默认）                                               | 编辑 `agent_config.json` 改为 `true`，重启策略                                                                                     |
| `inspect` 报「配置文件被独占锁定」/ `PermissionError`  | QMT 正在运行持有文件锁                                              | 关 QMT 再跑 inspect；`--force` 只跳过运行判定，**不做锁规避**                                                                              |
| 「导入本地策略」菜单灰色 / 找不到                         | 券商禁用了 .rzrk 导入                                             | 改走路径 B：新建策略 → 粘贴实际落盘的 `.txt`（本机实测 `QMT_WORK_AGENT.txt`）全部内容 → 编译                                                                           |
| 策略启动但报 `NameError` / `ModuleNotFoundError` | bundle 生成失败或编码问题                                           | 跑 `python scripts/check_bigqmt_agent_py36.py`；确保用 `gen_qmt_agent_bundle.py` 生成的 bundle 而非手改                               |
| 策略启动即退出、无日志                                | `agent_config.json` JSON 语法错                               | 用 `python -c "import json; json.load(open('agent_config.json'))"` 校验；模板见 `backend/agent_bigqmt/agent_config.example.json` |
| 端口 `8086` 拒绝连接（跑 CEF CDP 时）                | QMT 未开「CEF 调试」或客户端版本不支持                                    | 关闭 CEF 诊断路径，改用 `qmt_agent_verify`；`qmt_cef_cdp.py` 需要 QMT 客户端以调试模式启动                                                      |
| 心跳一直 stale 但「模型交易」里显示运行中                   | handlebar 未触发（无行情推进，常见于收盘后或策略未订阅）                          | 这是**误判活死**的常见来源 —— 心跳新鲜度只是判据之一，不能单独作为唯一判据（TD 系列根因）。用 `is_connected()` 的「现在可用」语义判                                          |
| 策略「运行」时 `IndentationError` 或 `SyntaxError`     | **R17 真实事故**：bundle 头部被拼了另一支策略代码（如 `import pandas/numpy/talib`） | `qmt_agent_verify --json` → 看 `bundle.syntax_ok` / `bundle.pollution_ok`；重新 `deploy_qmt_work_agent.bat` 覆盖 |
| `bundle.pollution_hits` 非空                           | bundle 前 20 行出现 pandas/numpy/talib/sklearn/torch 等非标准库 import           | 同上，重新部署；手工改过的 bundle **禁止**再往顶部塞任何代码（会污染 agent 入口）                          |
| 策略在列表里、点「运行」**立刻停止**（`return code:1`），且 **bridge 里连 `probe_result.json` 都没有** | **R27 真实事故**：bundle 源码编码是 `gb18030`/`gbk`。QMT 内置 `bin.x64/pythonw.exe` 是 **Python 3.6.8**，它的 tokenizer 读带 `#coding:gbk` cookie 的源文件会在特定内容下报 `SyntaxError: encoding problem` / `invalid token`。**开发机的 Python 3.11 编译得过去，所以极难联想** | 跑 `python scripts/qmt_agent_deploy.py deploy --encoding utf-8 --filename QMT_WORK_AGENT.py` 重新部署；体检判据见 `qmt_agent_verify --json` 的 `bundle.encoding_ok` / `bundle.encoding_declared` |
| 策略在列表里、点「运行」立刻停止，但**有** `probe_result.json` | 缺 `if __name__ == "__main__":` 自举。QMT 的「运行」= `pythonw.exe -u <策略.py> <userdata> <ts>`（**独立进程**），没有自举 = 起来没代码可跑就退出 | 用生成器重新构建（`gen_qmt_agent_bundle.py` 会自检此条）；本地跑通验证见 `scripts/qmt_agent_local_run.py --mode process` |
| `bundle.encoding_ok: false`（但 `syntax_ok: true`）      | 同上编码事故的**结构化判据** —— 语法过 ≠ QMT 能跑                                   | 按上面那条重部署；**不要**因为开发机 `python X.py` 能跑就以为没问题（两边解释器版本不同）                                |
| probe 里 `injected: []`（一个注入函数都没有）               | 独立进程模式的**常态**：终端只把 `passorder`/`get_trade_detail_data` 等注入**被挂载的那一个文件**的命名空间；以外部进程形态运行时拿不到 | 这是事实、不是 bug。probe 会写 `runtime_mode: standalone_process` 如实标注。要有下单能力，需让策略以**公式策略**形态被终端 in-process 挂载 |

### 5.8 Bundle 完整性护栏（P0 · R17 引入 · R27 扩充「编码 + 运行期自举」）

**背景**：2026-10-02 用户实测点「运行」报 `IndentationError`，根因是 `QMT_WORK_AGENT.py` 头部被拼了 18 行另一支 CCI 策略（`import pandas/numpy/talib` + `init/handlebar` stub），Python 解释到 docstring 边界就爆。R17 引入语法/污染两道护栏；**R27 追加「源码编码」与「运行期自举」两项**（源于 2026-10-08 事故：bundle 能编译、文件就位、注册也正常，但点运行立刻 `return code:1` 且零自检 —— 根因只是编码）：

| 检查 | 函数 | 判据 | 何时触发 |
|---|---|---|---|
| **语法校验** | `check_bundle_syntax()` | AST `parse()` 是否通过 | 每次 `qmt_agent_verify.py` 运行时 |
| **污染签名** | `check_bundle_pollution()` | 前 20 行是否出现 `pandas/numpy/talib/sklearn/scipy/matplotlib/seaborn/plotly/torch/tensorflow` | 同上 |
| **源码编码** | `check_bundle_encoding()` | PEP263 cookie（或默认 utf-8）是否属于 `QMT_SAFE_ENCODINGS = {utf-8, utf8, ascii, us-ascii}`。**gbk/gb18030 ⇒ 判红** | 同上 |

> ★ 编码这条要单独拎出来说：它是**唯一一个「开发机绿灯、QMT 红灯」的判据**。
> `bin.x64/pythonw.exe` 是 Python **3.6.8**，其 tokenizer 对 `#coding:gbk` cookie
> 的容忍度**取决于内容长度**（实测 706 字节过、707 字节挂），不可依赖。
> 所以部署口径定为 **UTF-8**，`deploy` 默认即是，且写出后会用 QMT 内置解释器
> **实跑一遍**（跑不起来就中止部署并给出三条常见原因）。
> 生成器 `recode()` 仍保留 `gbk`/`gb18030` 选项，但会强制打一条说清后果的告警。

结构化输出（`diag_report.json` → `bundle` 字段）：

```json
{
  "path": "P:\\stock\\gd_qmt\\python\\QMT_WORK_AGENT.py",
  "size_bytes": 88948, "line_count": 1974, "encoding": "utf-8",
  "syntax_ok": true, "syntax_error": null,
  "encoding_ok": true, "encoding_declared": "utf-8", "encoding_read_as": "utf-8",
  "encoding_note": null,
  "pollution_ok": true, "pollution_hits": [], "pollution_first_line": -1,
  "ok": true
}
```

`ok = syntax_ok AND pollution_ok AND encoding_ok`。任一为假 → `diag_report.json` 的 `problems[]` 里会带具体化建议（语法/污染 → 「重跑 `deploy_qmt_work_agent.bat` 覆盖为干净版本」；编码 → 「用 `--encoding utf-8` 重新部署」）。

**运行期自举（R27）**：`deploy` 在宣布成功前，会用 QMT 内置解释器按
`pythonw.exe -u <策略.py> <userdata> <ts>` 把刚写出的 bundle **真跑一遍**
（`scripts/qmt_agent_local_run.py --mode process`），要求「活过启动 + 心跳新鲜 + PROBE 往返 ok」。
另有一条 `--mode framework` 复刻终端 `exec` 加载（**故意不给 `__file__`**），
断言注入函数面**恰好等于**声明的那几个替身。两道验证都绿才叫部署成功。

### 5.9 多标的账户类型支持（P1 · R18 引入）

**升级前**：`do_place` 只支持 A 股 `passorder` 标准 11-arg 签名，前端只能选「股票」一个 account_type，柜台一旦返 `invalid instrument` 就无从定位。

**升级后**：完整覆盖 5 类标的 × 3 级签名降级：

| account_type | 中文名 / 别名 | opAccountType | 走签名 | 代码格式 |
|---|---|---|---|---|
| `stock`     | 股票 / A股 / A 股 | 0 | 11-arg 标准 | `600036.SH` / `000001.SZ` / `688981.SH`（科创板）/ `300059.SZ`（创业板） |
| `etf`       | ETF / LOF | 1 | 12-arg 扩展 | `510300.SH` / `159915.SZ` |
| `future`    | 期货 / 商品 | 3 | 12-arg 扩展 | `IF2312.SHF`（股指期货）/ `AU2402.SHF` / `M2401.DCE` / `SR2405.CZCE` |
| `option`    | 期权 | 2 | 12-arg 扩展 | `10005847.SH` / `02000031.SZ` |
| `credit`    | 融资融券 / 两融 | 4 | 12-arg 扩展 | `600036.SH`（与 A 股同 code 体系） |

**下单示例**：

```python
# 股票（默认，无需指定 account_type）
do_place({"stock_code": "600036.SH", "side": "buy", "price": 35.0, "volume": 100})

# ETF
do_place({"stock_code": "510300.SH", "side": "buy", "price": 3.5, "volume": 100,
          "account_type": "etf"})

# 期货（支持中文别名）
do_place({"stock_code": "IF2312.SHF", "side": "buy", "price": 3800.0, "volume": 1,
          "account_type": "期货"})
```

**三级签名降级**（`qmt_api.Executor.do_place` 内部）：

1. 非 stock → 12-arg 扩展签名（末位带 `opAccountType`）
2. 12-arg 被 `TypeError` 拒 → 11-arg 标准签名（含 ContextInfo 形参）
3. 11-arg 也被拒 → 10-arg 兜底（老券商版本无 ContextInfo 形参）
4. 三层全失败 → 抛最后一个 `TypeError` → 外层 `execute()` 转成 `BrokerSDKError` 返回

结果里会带 `extended_signature_fallback: true` 标记（仅当降级发生时），前端可据此提示用户「券商版本不支持扩展签名，已自动降级」。**注意**：降级后 opAccountType 参数**不会**传给柜台——柜台会根据代码格式自己判定。绝大多数场景（ETF/期货/期权）柜台都能从代码识别，问题不大。

**代码格式校验**（下单前）：格式不匹配直接 `BrokerError` 拒绝，避免柜台返难懂的错。

| 场景 | 前端/后端行为 |
|---|---|
| `stock_code="IF2312.SHF"` + `account_type="stock"` | ✅ 通过（stock 正则匹配 A 股，但代码是期货格式）→ **应该被拒** |

_注：当前实现对 stock 类型的正则较宽松，未来会加更严格的账户类型 × 代码交叉校验。目前主要靠用户手动选择正确的 `account_type`，或从代码后缀自动推断。_

**配置**（`agent_config.json` 可覆盖）：

```json
{
  "default_account_type": "stock",
  "passorder": {
    "opAccountType_stock": 0,
    "opAccountType_etf": 1,
    "opAccountType_option": 2,
    "opAccountType_future": 3,
    "opAccountType_credit": 4
  }
}
```

不同券商版本的枚举可能不同（比如某些版本的 `opAccountType_future` 是 `5` 而不是 `3`），PROBE 诊断正常但下单失败时先改这里的覆盖值。

**Probe 能力面**（`probe_result.json` → `account_types` 字段）：

```json
{
  "account_types": {"stock": 0, "etf": 1, "option": 2, "future": 3, "credit": 4},
  "default_account_type": "stock"
}
```

前端可据此渲染下拉框、动态显示支持的账户类型（不用硬编码 5 个）。

---

### 5.10 行情字段契约：所有出口恒定携带 `price`（P0 · R19 第 3 轮修复）

qmt_work 的行情出口有三个：批量 `GET /market/quotes`、单只 `GET /market/quote`、指数条 `GET /market/indices`。
三者都以**界面契约名**返回——`price`（最新价）与 `pre_close`（昨收）为**必填**。

**修复前**：只有批量出口做了「原生名 → 契约名」归一，单只与指数条直接透传数据源原始字段。
而不同数据源字段名不一致：`eltdx` / 公开源只给 `last`（无 `price`），券商行情两套名都给。
于是单只 / 指数行情**可能缺 `price`**，前端读到 `undefined`，K 线 / 概览面板**静默空白**（不报错，最难查）。

**修复后**：归一收敛到唯一入口 `core.quote_fields.apply_ui_quote_contract()`，三个出口共用：

| 行为 | 说明 |
|---|---|
| 原生名优先序 | `lastPrice` → `last` → `price` → `close`（唯一键序入口，不再各处各写一份） |
| 取不到怎么办 | **删键，绝不写 0**——0 会被误读成「价格为零」，属粉饰 |
| `pre_close` | 与 `price` 成对处理，同规则 |
| 共享对象 | 单只 / 指数条**复制再改**（`{**q}`），不污染券商 `latest_quotes` 里的共享 dict |

> 使用者侧影响：**无需任何操作**；单只与指数行情现在恒定携带 `price`，K 线 / 概览面板不再可能因缺字段而空白。

---

## 6. 接口与能力差异对照表

qmt_work 对上层暴露的接口（行情 / 交易 / 账户 / 持仓 / 订阅 / 历史 K 线）在大小 QMT 下的支撑情况：

| 能力                  | 小 QMT 直连 / 大 QMT 直连 full      | 大 QMT 策略桥（路径 B）                                                                                         |
| ------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------- |
| 下单 / 撤单             | ✅ xtquant `order_stock`       | ✅ `passorder` / `cancel`                                                                                |
| 资金 / 持仓 / 委托 / 成交查询 | ✅ `query_*`                   | ✅ `get_trade_detail_data` 族                                                                             |
| 实时行情                | ✅ xtdata（58610）               | ✅ `ContextInfo.get_full_tick`（58610）                                                                    |
| 历史 K 线              | ✅ `download_history_data`     | ✅ `ContextInfo.get_market_data` / `download_history_data`                                               |
| 委托/成交事件推送           | ✅ 回调（`XtQuantTraderCallback`） | ⚠️ **文件桥不支持回调式订阅**：`subscribe_quote` 只记录意图，由事件泵每秒对账（`SUB_QUOTE` → agent 轮询 diff → `events.ndjson` → WS） |
| 订阅行情                | ✅ 原生回调                        | ⚠️ 同上，经事件泵对账合成（秒级，非毫秒级）                                                                                 |
| 两融 / 期权 / 期货        | 取决于券商档案 `capabilities`        | 取决于 agent 注入函数集（能力协商）                                                                                   |

**关于「大 QMT 文件桥的行情订阅」要特别注意**（曾是最典型的「假绿灯」）：文件桥没有回调式订阅通道，所以 `subscribe_quote` 只把意图写进内存、**真正的 `SUB_QUOTE` 下发由事件泵每秒对账完成**。若对账逻辑没发出 `SUB_QUOTE`，agent 的订阅集恒为空，`quote_events` 恒返回 0，事件泵每秒空转，界面价格永远停在种子值——而连接状态 / 健康检查 / 订阅日志**全绿**。判活务必以「真实行情是否在动」+ `last_agent_ok_age_s` 为准，不要只看状态灯。

---

## 7. 故障排查

### 7.1 交易连不上：先分清**两种**日志特征（2026-10-09 细化）

`XtQuantTrader.connect()` 返 `rc=-1` 有**两种**互不相同的根因，处置方向**完全相反**。平台会读客户端日志自动分流（`xtquant_client/xtp/diagnostics.py::diagnose_trade_connect`），并在失败文案的**首句**给出对应结论与「别往哪查」。

| 日志特征 | 授权串 | 真实含义 | 处置方向 |
| --- | --- | --- | --- |
| `The XtQuantServer is not allowed to start.` | `mdl_auth_gt_ipc_pair=0`、`mdl_auth_xtquant=0` | 券商**未给该资金账号下发 xtquant 模块授权** ⇒ 客户端**根本不启动**量化服务 | **申请 xtquant / 程序化交易模块授权**（**不是**加白名单！） |
| `quant session N, pid X not allowed, return` + `connect ret error-1` | `mdl_auth_xttrader_strict_connection_check=1` | 严格连接校验按 **PID** 拉黑了调用进程 | 把 `qmt_work.exe` 加进客户端白名单，或请券商关闭严格校验 |
| 以上均无，仅 `mdl_auth_xtquant=0` | `mdl_auth_xtquant=0` | 纯模块授权未下发，量化通道未开放 | 同第一种 |

- **行情 vs 交易**：两种情况行情 `xtdata` 通常都正常，**仅交易 connect 返 `rc=-1`**。
- **平台无法改写**：授权由券商服务端下发，属客户端内部行为。处理：
  - 小 QMT → 引导找券商放开授权（或换支持外部直连的客户端）。
  - 大 QMT → 切到 §5 策略桥（路径 B），绕开外部直连（**不受 PID 白名单限制**）。
- **⚠️ 别按错方向排查**：把第①种当成第②种，就会去「加白名单 / 重装 SDK / 检查行情登录 / 会话冲突」——**全部无效**。旧版正是这么误报的（原因与修复见 §7.10）。
- **授权串读法**：它在客户端日志里，**偏移随当天日志增长而增大**（实测 17.2MB 的日志里在 **1.57MB** 处），因此读取必须是**全文件扫描**而非固定头/尾窗口；日志按 `_log_rank` 选（交易主 2 > 辅助 1 > 行情 0），不能只按 mtime 取最新。

### 7.2 心跳假存活（文件残留 ≠ 在跑）

- 现象：面板显示「已连接」，但无行情、无委托回执。
- 判据：看 `agent_status.json` 的**时间戳**是否还在刷新（10s 一次），以及前端的 `last_agent_ok_age_s`。残留旧文件会让状态灯绿，但 `age_s` 持续增大 = 已崩。
- 处理：去大 QMT 客户端确认策略是否真在运行（`qmt_agent_verify.py` 注册态 + 心跳一起判）。

### 7.3 策略放进 `python/` 却不在列表

- 原因：策略列表来自**注册树**，不是目录扫描。两个 md5 相同的 `.py`，重启后可能一个在列表一个不在——证明不是「扫目录」。
- 处理：走 §5.3 的 GUI 动作（导入本地策略 / 新建策略 + 编译）写注册树。`--reveal` 只开资源管理器，不碰 QMT。

### 7.4 预算链超时（连接卡住）

- 链：`_CONNECT_RETRY_BUDGET=45s` < `bridge_client._READY_TIMEOUT=100s` < 前端 `timeout(120_000)`；`http.ts DEFAULT_TIMEOUT=15_000` 会掩盖根因。
- 征兆：前端按钮无反馈、反复点击、后台反复起 45s 重试。
- 处理：前端已加 loading + 禁用 + 连接中状态；后端 `adapter.start()` 走异步线程，子进程先发状态再做 IO（实测 300s → 41.87s）。若仍卡，查桥接子进程是否真起来（`qmt_agent_verify.py`）。

### 7.5 「校验工具说缺某个注入函数」——先看是不是陈旧快照

`qmt_agent_verify.py` 读的是**上一次策略运行时**写下的 `probe_result.json`。策略当前没在跑时，这份快照必然过时：

- 工具会输出 `probe_stale: true`，并把相关行写成「陈旧快照里未见 X」的**提示**，而不是致命问题（不计入 `problems`）；
- **不能据此断言当前终端没有该函数**。注入函数是在 QMT 里运行时由 `capture_qmt_injected_funcs(globals())` 真实捕获的——它遍历命名空间取所有 callable，**不做任何「应该有哪些函数」的假设**。这正是入口文件里严禁手抄函数名单的原因：手抄表漏一个，就会把「桥的 bug」误报成「终端没这个接口」（历史上 `xtquant_big_convert` 曾因此把 `query_credit_account` 的缺失误判为终端无两融）。
- 正确顺序：**先注册并运行策略（§5.3）→ 心跳新鲜 → 再跑 `qmt_agent_verify.py`**。此时若仍缺函数，才是真的缺。

### 7.6 三步自测大 QMT（推荐流程）

1. **发版**：`gen_qmt_agent_bundle.py`（生成单文件 bundle）或 `qmt_agent_deploy.py deploy`（部署 + 校验一步到位），落到 `<QMT>/python/<注册树条目指向的文件名>`（本机实测 `QMT_WORK_AGENT.py`）。
2. **看文件**：`qmt_strategy_list_probe.py --target <QMT>/python/<注册树条目指向的文件名>`  
   —— `--target` 既可传**策略名**（`qmt_work_agent`）也可传**文件名 / 路径**（过去拼 `.py` 时会误报「文件就位:否」，现已修正）。  
   正常应见「文件就位: **是**」；「已注册」通常为**否**，因为拷贝文件不会写注册树。
3. **注册并运行**：在 QMT 客户端做一次写注册树的 UI 动作（§5.3），运行策略。  
   再跑 `qmt_agent_verify.py`，当它显示心跳新鲜且 `probe_stale: false` 时，才算**真正闭环**。

> 这三步是判定「大 QMT 到底能不能跑」的唯一可靠口径：**文件在 ≠ 已注册，注册了 ≠ 在运行，运行过 ≠ 现在还在跑**。

### 7.7 大 QMT 直连 `rc=-1`：先看「极简模式」勾没勾（2026-10-09 细化）

> 先读 §7.1 做**根因分流**：本节的「极简模式」是**环境侧**的常见诱因，而「模块未授权 / PID 拉黑」是**券商侧**的授权问题。客户端的**权威结论**以 `diagnose_trade_connect` 的分流结果为准，本节只作补充解释。

`XtQuantTrader.connect()` 恒返 `-1`，而 `~/.xtquant` 不存在、58610 也没有监听——这是**大 QMT 未以「极简模式」登录**的**特征签名**（迅投 FAQ 第①条）。
此时平台给出的诊断已改为**锚在首选判定**上，例如：

```
3) 配置客户端模式：client_mode=auto，**首选判定 @full**（...\userdata），回退后停在 @mini。
   ↳ 首选模式才是「按实际运行客户端解析出来的事实」；回退只说明另一模式的目录也试过，不代表解析结论变了。
```

**注意读法**：`回退后停在 @mini` 只表示「另一个目录也试过」，**不代表**你的客户端是小 QMT。
历史缺陷：旧版把这一行直接打成「解析判定 @mini」，把用户指向极简版配置，方向被带偏。

**对策（按成本从低到高）**：

1. 在大 QMT 登录界面**勾选「极简模式」**重新登录 —— 数据目录仍是 `userdata`，最省事；
2. 启动 `bin.x64/XtMiniQmt.exe`（点界面上的「启动小 QMT」）—— 数据目录变为 `userdata_mini`，等于换一套登录体系；
3. 走「大 QMT 策略桥（路径 B）」（§5）—— 完全绕开外部 `XtQuantTrader`。

### 7.8 大 QMT 有行情吗？——大窗口 58600 **只管交易**（2026-10-09）

| 端口 | 由谁监听 | 提供什么 |
| --- | --- | --- |
| 58600 | `XtItClient.exe`（大 QMT 主程序） | **仅**客户端自有 IPC / 交易，**不提供** xtdata 的行情 RPC |
| 58610 | `miniquote.exe` | xtdata 行情 RPC（`get_full_tick` / `get_market_data`…） |

所以「大 QMT 已登录」**不等于**「行情可用」。`get_full_tick` 抛 `Exception: 无法连接行情服务！` 时，agent 会在自检里如实报 `quote_call` 异常并打印根因/出路；平台会把 `quote` 能力判为**不可用**（不再是「函数在就算支持」的假绿灯）。

**出路**：在大 QMT 里开「独立行情 / 极简模式」拉起 `miniquote`（界面上的「仅补行情」按钮就是干这个），或让策略以**公式模式**挂载（走 `ContextInfo` 的行情方法，不经本地 58610 服务）。

### 7.9 策略进程「启动即停止」却没有报错 —— 先查宿主 `PYTHONPATH`（2026-10-09）

QMT 内置解释器是 **Python 3.6.8**。若调用方（conda / pyenv / IDE / 各类工具 shim）设置了 `PYTHONPATH`，该路径下的 `sitecustomize.py` / `usercustomize.py` 会被 **QMT 的 py3.6 一并导入**。

实测症状：策略日志只到「agent 就绪」就断，随后

```
[safe-delete][...] __init__() got an unexpected keyword argument 'capture_output'
return code: 1
```

（一个为 py3.7+ 写的 `sitecustomize` 用了 `subprocess.run(capture_output=...)`，py3.6 没有该参数。）

**平台已修**：`qmt_agent_local_run.py` 拉起 QMT 解释器时会剥掉 `PYTHONPATH` / `PYTHONHOME` / `PYTHONSTARTUP` / `PYTHONEXECUTABLE`（**只剥这四个**，`PATH` / `SystemRoot` 一律保留——整体丢弃 `os.environ` 会让 `pythonw.exe` 根本起不来）。

> 为什么必须剥而不是保留：**QMT 客户端自己拉起策略时用的是它自己的环境**，根本不带用户 shell 的 `PYTHONPATH`。保留它 = 验证环境与真实运行环境不一致，验出来的「通过/不通过」都不作数。
> 手工排查：`set PYTHONPATH=`（cmd）或在 PowerShell 里 `Remove-Item Env:PYTHONPATH` 后再启动策略。

### 7.10 诊断报「无明确证据」，其实是**读不到**（2026-10-09 修）

- **症状**：客户端日志里白纸黑字写着 `The XtQuantServer is not allowed to start.`，可 `/brokers/test` 的失败文案只说「无明确证据」，且 `auth.found=false`。
- **根因（已修）**：旧实现只读**固定窗口** —— 授权串读「头 512KB」，拒绝行只在「尾 256KB 的末 1200 行」里找。而长跑日志里标记的偏移**会涨**：

  | 标记 | 当日日志（17.2MB / 103592 行）位置 | 旧窗口能否覆盖 |
  | --- | --- | --- |
  | `receive module auth string` | 第 9719 行 / 偏移 **1,573,933 B** | ❌ 头 512KB 只到第 3242 行 |
  | `The XtQuantServer is not allowed to start.` | 偏移 1,810,790 / 15,543,174 B | ❌ 尾窗口从 16.9MB 起 |

  于是**同一台机器早上诊断正确、晚上静默失效** —— 不报错、也不判错，只是**不判**。这是本项目「假绿灯」家族里最难查的一类。
- **现行为**：`_scan_marker_lines` **按块全文件扫描**（1MiB/块、上限 64MiB、跨块残行拼接、utf-8↔gb18030 容错），并记录**绝对偏移**再按窗口读原文（不假定授权串永远单行）。读日志失败时走 `core.errors.swallow` 记因并退化返回已扫到的部分，**绝不抛错**（诊断自身崩溃比诊断不到更糟）。
- **自证**：`python -c "from xtquant_client.xtp import read_client_auth_flags as f; print(f(r'<QMT>\userdata'))"` 应返回 `found: true` 与 **649** 个 `mdl_auth_*` 键（键数随券商版本不同，量级如此）。

---

## 8. 安全与硬约束（务必知道）

- **零 mock**：所有行情 / 交易 / 账户接口都走券商**真实 SDK**，平台**不返回任何模拟数据**。未连券商 → HTTP 200 + 业务码 `503` + 引导；券商不可用 → `503`，可用但被拒 → `400` + 真因。失败**绝不包 `code=0`**（前端靠 `code!==0` 抛错）。
- **`SignalRouter.submit` 唯一交易入口**：任何下单都经它，风控 / 幂等 / TOTP / 审计不可绕过。大 QMT 的 `passorder` 也是经桥回到这条入口，不会绕过。
- **运行期状态不进安装包**：`master.key` / `app.db` / `qmt_work_config.json` / `data/` / `logs/` 不会打进发布包（构建脚本有 `purge + verify` 硬门禁）。干净安装首次启动自行生成密钥。

---

## 9. 一页速查（Cheat Sheet）

| 我要…      | 选哪种           | 前端接入模式                      | 客户端路径 / 桥参数      |
| -------- | ------------- | --------------------------- | ---------------- |
| 极速版能直连   | 小 QMT 直连      | `direct`                    | `userdata_mini`  |
| 完整版能直连   | 大 QMT 直连 full | `direct`                    | `userdata`       |
| 完整版被封直连  | 大 QMT 策略桥（兜底） | `bridgeFile`（零部署）           | 桥目录              |
| 完整版要低延迟  | 大 QMT 策略桥     | `bridgeRedis` / `bridgeZmq` | Redis 串 / ZMQ 地址 |
| 只有极速版且被封 | **无法接入**      | —                           | 引导找券商放开授权        |

> 记住三条铁律：①小 QMT 只有直连、没有策略桥；②大 QMT 策略须写注册树（GUI 动作），丢文件不生效；③连没连上以真实往返 + 心跳新鲜度为准，不以文件残留为准。

---

## 10. 定时任务的数据新鲜度语义 & 界面默认外观（2026-10-02 起）

本章只讲**平台本体**的两个默认行为——不依赖大 QMT 或小 QMT，任何接入模式（含离线只读）都成立。

### 10.1 界面默认深色 + 极夜黑背景

首次启动（`localStorage` 里没有 `qmt.ui.v1`）时，平台默认呈现 **深色主题 + 「极夜黑」皮肤（#000000）**，并在 HTML 标签挂载之前由 `public/theme-boot.js` 预热 `data-theme`/`data-skin`，**不会先闪一屏白**（no-FOUC）。

- 皮肤 id 在 2026-10 做过一次中性化更名：`tongdaxin → midnight`、`dazhihui → graphite`、`ths → obsidian`。**旧 id 仍可用**——`skins.ts` 的 `LEGACY_SKIN_IDS` 与 `theme-boot.js` 里各有一份映射，老用户磁盘上的 `localStorage` 与服务器上的「外观配置」不会被零件改名打断，读回时自动迁移到新 id。
- 改主题、改皮肤后按 `Ctrl/Cmd+S` 或界面提示保存；下次启动即沿用。

### 10.2 「期望 K 线日期」是数据可得日，不是日历今日

定时选股（`classic_screen`）判断「数据是否落后」用唯一口径 `app.sync.calendar.expected_bar_date()`：

| 当前时刻                           | 期望的 K 线日期                            |
| ------------------------------ | ------------------------------------ |
| 交易日 且 已过 `ready_hour`（默认 18 点） | **今天**（当日收盘数据已发布）                    |
| 交易日 但 未过 `ready_hour`          | **上一交易日**（当日数据源约 17:00–18:00 才稳定，不等） |
| 非交易日（周末/节假日）                   | **上一交易日**                            |

**为什么必须有 `ready_hour`**：收盘后数据源并不立即吐出当日数据。若口径写成「日历今日」，那么每天 16:15 的定时选股都会判「落后」→ 触发一次全市场补数 → 补完拿到的仍是昨天的数据 → 第二天重复。**这是每天一次的全市场无效 RPC**。`ready_hour` 让判断落在「数据真已经可得」的那一天上，补数从「每天一次」降为「只在真的落后时」。

`ready_hour` 可在触发参数里覆盖（`{"ready_hour": 16}` 更保守、`19` 更宽松）。

### 10.3 增量同步的两种「跳过」

`sync_bars` 任务（含「定时更新日线」）的返回体里有两个**含义不同**的跳过计数，分别呈现、不可相加：

| 字段                 | 含义                       | 何时非 0                                       |
| ------------------ | ------------------------ | ------------------------------------------- |
| `skipped_complete` | 历史已补到起点、**本次压根不用问源**     | `backfill_from` 有值时，`bars` 已存在该标的且起点不晚于回补起点 |
| `skipped_fresh`    | 本次的增量目标日 **已经是最新**，不用再问源 | 任务参数带 `skip_fresh: true`（默认「定时更新日线」已开启）     |

两者共同的效果：**全市场已经最新时，返回 `total=0` 且两个跳过数之和覆盖全部标的——这是成功，不是失败。** 判定任务是否空转必须同时看这三个字段；只判 `total == 0` 会把刚省下来的几万次 RPC 误报成「未获取到任何股票」。

`skip_fresh` 的实现是**只读一次 SQLite**（`LocalStore.latest_dt_map` 一次聚合查询，返回 `{code: 最新dt}`），与 `expected_bar_date()` 比对；查询失败时**不做跳过、全部重跑**——宁可多问源几次，不可漏同步。

### 10.4 任务级失败不再吞掉整批

行情补数与多策略选股都改成了「单个失败只影响自己」：

- **补数**：一只标的的任务级异常（含 `CancelledError`）只记为它自己的 `failed=1`，其余标的的 `ok`/`bars` 全部保留落库。
- **多策略选股**：N 个策略并行计算，失败的那个从结果里剔除、在 `strategy_failures` 里**点名**（形如 `ma_volume: 策略内部错误：除零`），其余策略结果照常落库。只有**全部**失败才向上抛错。

> 历史教训（R15）：这两处原来是「整批一损俱损」——一只标的抛 `CancelledError` 会让 `asyncio.gather` 把整批结果连同已完成的部分一起丢弃。
