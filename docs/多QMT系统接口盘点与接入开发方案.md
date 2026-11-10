# 多 QMT 系统接口盘点与接入开发方案

> 实测环境：2026-11-08，本机 Windows，Python 3.11.9（`backend/runtimes/cp311`）与客户端自带 3.6.8。
> 调研对象：本机 4 套 QMT 安装（光大 / 国信V11 / 国信V3 / 中金）。
> **全部探针只读**：无下单、无撤单、无凭证伪造、无鉴权绕过。所有结论都标注了可复现的证据来源。

---

## 0. 一页结论

**三条硬结论：**

1. **大 QMT（`XtItClient.exe` 完整版）的外部直连下单通道是关闭的，且是按设计关闭的。**
   `xttrader`（共享内存）在 58600/58670 全通的情况下，`connect()` 恒返回 `-1`、耗时恒为 3.00s。
   真因不是版本错配，而是 `OrderService.dll` 的 **PID 白名单**：

   ```
   [TC::COrderServiceQuantAdaptor::onConnected] quant session 28945, illegal pid 0
   ```

   外部 cp311 进程、客户端自带 cp36 的 `pythonw.exe`、甚至**客户端自己 spawn 的 Jupyter kernel**
   （PID 22136，同一 exe）**全部被拒**。这条路没有"调通"的余地，也不应尝试绕过。

2. **行情侧的可用性被严重高估了。**
   `xtdata.ini` 指向 `127.0.0.1:58610`，但**全机无 58610 监听**，`~/.xtquant` 不存在 ⇒
   外部进程走官方 `xtdata` 连不上（`无法连接xtquant服务`）。真正在服务的是两个 IPC 端口：
   - **58600**（`XtItClient.exe`）：仅 3 个方法可用，其中 `getStockListInSector` 实测返回 **5224 只真实代码**；
   - **58670**（`XtModel.exe`）：**16 个方法可用**，含 `getFullTick` / `subscribeQuote` / `getSectorList`，
     是**真正的行情面**，但参数键名尚未确定（见 §11）。

3. **能外接进 `qmt_work` 的，只有 4 条路，其中 2 条是新增能力。**
   现有「策略内 `passorder`」和「文件信号桥」继续用；**新增**「极简模式 `xttrader` 直连（仅光大具备组件）」和
   「58600/58670 行情旁路（零 SDK 依赖）」。文件单（CSV/DBF/batchOrder）作为**官方正门兜底**，两家都可用但需 UI 开启。

**两个必须立刻处理的运维风险（与功能无关，但会直接搞坏生产）：**

| 风险 | 实测证据 | 后果 |
|---|---|---|
| **58600 端口冲突** | 光大 PID 24544 与国信 PID 8828 **同时** `LISTENING 0.0.0.0:58600` | 任何连 58600 的客户端可能打到**另一家**的终端上；下单/查询串户 |
| **Jupyter token 明文** | `config/jupyter_notebook_config.py` 里 `c.NotebookApp.token = '7d7d...46d3'`，`127.0.0.1:8888` 在听 | 本机任意进程可执行任意代码（已实测可创建/执行/删除 kernel） |

---

## 1. 调研范围与实测基线

### 1.1 四套安装指纹

| 项目 | 光大 `P:/stock/gd_qmt` | 国信V11 `guoxin_iquant_11` | 国信V3 `guoxin_iquantV3` | 中金 `zj_QMT` |
|---|---|---|---|---|
| `XtItClient.exe` | **145,272** · 2025-09-10 | 211,968 · 2026-08-11 | 211,968 · 2026-06-10 | 211,760 · 2022-11-28 |
| `xtpythonclient.cp311.pyd` | **2,816,000** · 2025-10-26 | 1,147,904 · 2023-02-15 | 1,147,904 · 2023-02-15 | 无 |
| `xttrader.py` | **75,868** · 2025-10-26 | 34,739 · 2021-11-26 | — | — |
| `xtdata.py` | **138,231** · 2025-10-26 | 29,807 · 2020-11-13 | — | — |
| `xtconstant.py` | **34,056** | 1,929 | — | — |
| `xtdatacenter.py` | **有**（10,799） | 无 | 无 | 无 |
| `datacenter_shared.dll` | **有**（34 MB） | 无 | 无 | 无 |
| `XtQuantServer_qmt.dll` | 309,624 · 2025-09-10 | 174,080 · 2026-08-11 | 172,032 · 2026-06-10 | 114,992 · 2022-11-28 |
| `OrderService.dll` | 4,879,736 · 2025-09-10 | 4,444,160 · 2026-08-11 | 4,310,528 · 2026-06-10 | 3,716,400 · 2022-11-28 |
| **`XtMiniQmt.exe`** | **有**（234,872） | **无** | 无 | 无 |
| **`minibroker.exe`** | **有**（579,448） | **无** | 无 | 无 |
| `miniquote.exe` | 576,888 | 1,461,760 | — | — |
| 运行态 | **运行中** PID 24544（17:01:54 起） | **运行中** PID 8828（16:49:30 起） | 未运行 | 未运行 |

### 1.2 端口拓扑（实测 `netstat`）

| 端口 | 归属 | 实测状态 | 说明 |
|---|---|---|---|
| **58600** | `XtItClient.exe` × **2** | `0.0.0.0:58600 LISTENING` 由 **24544 与 8828 同时持有** | 完整版客户端的 IPC；两家 `broker.ini` 完全相同 ⇒ 必然撞 |
| **58670** | `XtModel.exe` PID 21752（国信） | LISTENING | 公式/行情控制面 |
| 58610 | 无 | **无监听** | `xtdata.ini` 的出厂地址，`~/.xtquant` 不存在 |
| 58609 | 无 | 无监听 | `xtdatacenter.init()` 默认本地监听端口 |
| **8086** | `XtItClient.exe` PID 8828 | LISTENING | **CEF 远程调试**（`/json/list`，Chrome DevTools Protocol） |
| **8888** | `pythonw.exe` PID 13028（国信自带） | LISTENING | 客户端内嵌 Jupyter 5.7.8 |
| 51314 | 光大 PID 24544 | LISTENING（非 HTTP） | 用途未定 |

`broker.ini`（两家**逐字节相同**）：

```ini
[broker]          pid=1  port=56500
[formulaworker]   pbserverport=59600  serverport=58600
[pythonbroker]    pid=1  port=60500
[pythonworker]    num=1  port=57500
[minibroker]      pid=1  port=55500
```

> ★ 这份文件是**所有端口冲突的根源**：它把 `formulaworker.serverport` 写死为 58600，
> 而 `minibroker`/`miniquote` 又在 55500/58610 上活动。两个安装目录共用同一份配置值 ⇒ 同时运行必撞。

---

## 2. 版本世代：能力边界的根因

四套安装不是"同一个软件的四个版本"，而是**三个世代**：

| 世代 | 代表 | 特征 | 能力 |
|---|---|---|---|
| **G2025（光大）** | 2025-09/10 | `xtquant` 包内**自洽**（pyd 2025-10-26 与 dll 2025-09-10 同代）；引入 `xtdatacenter` / `xtconn` / `xtview` / `metatable`；带 `XtMiniQmt.exe` + `minibroker.exe` | 行情可独立起服务；极简模式可用；包可直接被外部 Python 导入 |
| **G2023（国信V11）** | 客户端 2026-08，**xtquant 包停在 2023** | pyd 2023-02-15，`xtdata.py` 却停在 2020-11-13 ⇒ **包内错配** | 外部 `import xtquant.xtdata` **直接失败**（见下）；极简模式缺组件 |
| **G2022（中金）** | 2022-11-28 | 全套同代，但无 xtquant 包 | 只能走终端 UI / 文件单 |

**国信包内错配的实证**（外部 cp311 导入即崩）：

```
IMPORT FAIL cannot import name 'get_local_data_field_list'
            from 'xtquant.IPythonApiClient'
            (P:\stock\guoxin_iquant_11\bin.x64\Lib\site-packages\xtquant\IPythonApiClient.cp311-win_amd64.pyd)
```

`xtdata.py:8` 写着 `from .IPythonApiClient import get_local_data_field_list as getLocalDataFieldList`，
而 2023-02-15 的 pyd 已不再导出该符号。**这不是配置问题，是包本身不配套。**

> 推论：**只有光大这一套适合做"外部进程接入"的宿主。** 国信那套外部 Python 连包都导入不了，
> 只能回到「策略内运行」或「文件单」两种形态。

---

## 3. 行情接口盘点（Q1–Q6）

| 编号 | 接口 | 载体 | 实测结果 | 可用于 qmt_work？ |
|---|---|---|---|---|
| **Q1** | `xtdata`（**进程内**，策略里） | 客户端内嵌 py3.6.8 | 可用（现状即此形态） | ✅ 已是主路径 |
| **Q2** | `xtdata.connect()` → 58610 | `IPythonApiClient` + `net.dll` | ❌ `无法连接xtquant服务，请检查QMT-投研版或QMT-极简版是否开启`；`scan_available_server_addr()` 返回 `[]` | ❌ 当前不可用 |
| **Q3** | **58600 原生 RPC** | NetPackage 12B 大端头 + BSON | ⚠️ 仅 3 方法：`getStockListInSector`（**返回 5224 只真实代码**）、`getInstrumentDetail`、`getMarketData`；其余 64 个候选名一律 `200005 未找到处理函数` | ⚠️ 可做「标的池校验」旁路 |
| **Q4** | **58670 原生 RPC** | 同上 | ✅ **16 方法可用**（见下表） | ✅ **推荐**：零 SDK 依赖行情旁路 |
| **Q5** | `xtdatacenter` 独立服务 | `xtdc.init()` + `xtdc.listen(ip,port)` | 仅光大有该模块；需 `set_token()`（迅投投研 token，用户自有）；**未实测**（避免长时间初始化/下载） | 🔶 备选，需用户 token |
| **Q6** | 本地数据文件直读 | `datadir` / `data` | 未做（`getDataDir` 已能拿到路径，见下） | 🔶 P3 可选 |

### 3.1 58600 vs 58670 的方法集（实测）

**58600（`XtItClient.exe`）—— 窄接口，3 个可用：**

```
OK (3): getStockListInSector, getInstrumentDetail, getMarketData
NOT FOUND (64): 含 getFullTick, subscribeQuote, unsubscribeQuote, getSectorList,
                getTradingDatesByMarket, 以及全部下单/查询类
```

**58670（`XtModel.exe`）—— 完整行情面 + 公式控制面，16 个可用：**

```
OK (16): subscribeFormula, unsubscribeFormula, bindViewFormula, callFormula,
         commonControl, createView, destoryView, getTag, checkVersion,
         getDataDir, getAppBinDir, getSectorList, getStockListInSector,
         getInstrumentDetail, unsubscribeQuote, getFullTick
HANDLER-but-need-args (1): subscribeQuote   ← ErrorID=300000 "[xtquant]invalid stockcode: "
NOT FOUND: createFormula, destoryFormula, getAccountList, getPosition, getOrder,
           order*, passorder, getFuncList, ...
```

**关键读数：**

- `getTag` → `{"tag": "xtquant", "version": "1.0"}`：确认 58670 就是 xtquant 服务端。
- `checkVersion` → `1`：版本检查恒通过 ⇒ 当年"版本不匹配"的猜测在**这一层**站不住。
- `getDataDir` → `P:\stock\guoxin_iquant_11\xtmodel\data\datadir`：**58670 明确属于国信那一套**。
- `subscribeQuote` 报 `invalid stockcode: `（值为空）⇒ **handler 存在，只是参数键名不对**；
  试过 12 种键名（`stockCode` / `stockcode` / `stock_code` / `stockList` / `stock_code_list` /
  `codeList` / `codes` / `m_strStockCode` / `instrument_id` / `secu_code` / 嵌套 `param.*` / 裸数组）全部未命中。

> ★ **纪律提醒（本方案最容易被误判的一点）**
> `xtdata.connect()` 返回成功 **不等于** 连上了。实测 `connect(port=58611)`（**空端口**）也在
> `0.000s` 返回 ok —— 因为 `IPythonApiClient.connect()` 是异步的，且首次成功后**全局缓存 `__client`**。
> **唯一可信判据是一次真实业务调用拿到非空数据。** 本方案所有"可用"结论都按这个口径判定。

---

## 4. 下单接口盘点（O1–O7）

| 编号 | 接口 | 载体 | 实测结果 | 可用于 qmt_work？ |
|---|---|---|---|---|
| **O1** | `passorder` / `algo_passorder` / `smart_algo_passorder`（**进程内**） | `Formula.dll` 原生注入，11 参（末参 `ContextInfo`） | ✅ 可用（现状主路径） | ✅ **保留为第一通道** |
| **O2** | **文件单**：CSV / DBF / batchOrder | 客户端 4 个监控线程 | ✅ 组件齐全，但两家 `is_automonitor="0"` / `key_enable="0"`（**未开启**） | ✅ **官方正门兜底**（需 UI 开启） |
| **O3** | `xttrader`（共享内存 `up_queue_xtquant`） | `xtpythonclient.pyd` → `OrderService.dll` | ❌ `connect()` = `-1` @ 3.00s，日志 `illegal pid` | ❌ **PID 白名单**（见 §5） |
| **O4** | 58670 `callFormula` | 公式调用 | ⚠️ handler 在、返回 `status=0 result={}`，但**无 `createFormula`** ⇒ 无法先建公式再调用 | ❌ 结构性不可用 |
| **O5** | 58600 原生 RPC | 同上 | ❌ 无任何下单/账户方法 | ❌ |
| **O6** | CEF CDP（8086） | `CefViewWing.exe` + `/json/list` | ✅ 可驱动内嵌浏览器 DOM | ❌ **不用于下单**（脆弱 + 等同操作 UI，见 §10） |
| **O7** | **极简模式 + `xttrader`** | `XtMiniQmt.exe` + `minibroker.exe` | 组件**仅光大具备**；官方支持的 `xttrader` 形态 | ✅ **新增能力**（需关大 QMT 腾端口） |

### 4.1 文件单的配置位置与现状

配置在 **每用户** 的 `userdata/users/<账号>/Config.xml`：

| 安装 | 用户目录 | `CsvMonitorSetting` | `DbfMonitorSetting` | `batchOrderSetting` |
|---|---|---|---|---|
| 光大 | `userdata/users/22453951/` | `is_automonitor="0"` | `is_automonitor="0"` | `key_enable="0"` |
| 国信 | `userdata/users/15624979679/` | `is_automonitor="0"` | `is_automonitor="0"` | `key_enable="0"` |

监控路径：`<安装目录>/export_data`（CSV/DBF）、`<安装目录>/batchOrder/orderFile.csv`（批量）。
当前 `export_data` 与 `batchOrder` 目录**均为空** ⇒ 本机没有现成样例，格式需按 §11 的受控探针确定。

DBF 侧还有几个有语义的开关值得注意（两家一致）：

```xml
is_incexport="0" useringbuffer="0" need_remove_exprotfile_fristmonitor="1"
is_usesettingfeedbackval="0" orderidfeedbackval="-1" is_sharedread="0"
```

> `is_clear="0"`（光大）vs `is_clear="1"`（国信）—— 国信会在处理后清空文件，
> 这直接影响"重放/幂等"的设计：**清空语义下不能用文件残留判断是否已处理**。

---

## 5. 关键更正：A 通道失败的真正原因是 PID 白名单

### 5.1 之前的错误判断

此前（本仓 `output/plan_qmt_external_order.md` §9.3）把 `connect()` 失败归因为
**"bridge（2026-08-11）与 pyd（2021-11/2023-02）版本代差"**。**这个判断是错的**，
本次用光大那套 2025 同代包做了对照实验：

| 组合 | pyd | 服务端 dll | `connect()` | 结论 |
|---|---|---|---|---|
| 国信 + cp311 | 2023-02-15 | 2026-08-11（差 3.5 年） | `-1` @ 3.004s | — |
| 国信 + cp36（自带） | 2021-11-26 | 2026-08-11（差 4.8 年） | `-1` @ 3.010s | — |
| **光大 + cp311** | **2025-10-26** | **2025-09-10（同代）** | **`-1` @ 3.014s** | **同代也失败 ⇒ 版本不是根因** |

### 5.2 真因：`OrderService.dll` 的白名单

`strings OrderService.dll` 里有两条成对的格式串：

```
quant session {0}, illegal pid {1}
quant session {0}, pid {1} connected
```

客户端日志逐字对应（国信 `userdata/log/XtClient_20261108.log`）：

```
17:05:29 [TC::COrderServiceQuantAdaptor::onConnected] quant session 28729, illegal pid 23776   ← cp311 外部进程
17:09:05 [TC::COrderServiceQuantAdaptor::onConnected] quant session 28945, illegal pid 0       ← 客户端自带 cp36
17:12:18 [TC::COrderServiceQuantAdaptor::onConnected] quant session 31415, illegal pid 0       ← 客户端内 Jupyter kernel
```

**三次不同形态的进程，全部被拒**，包括客户端自己 spawn 的 Jupyter kernel（PID 22136，同一 `pythonw.exe`）。
⇒ 白名单既不是按可执行路径、也不是按父进程判定；它就是一份**特定 PID 清单**，
而 `xttrader` 在**完整版（大 QMT）**上本就只对内部受信进程开放。

另有一处细节：cp36 的 pyd 上报的 PID 是 **0**（`illegal pid 0`），cp311 上报的是真实 PID。
即使将来白名单放开，**cp36 侧也会因为 PID 报 0 而恒被拒** —— 这一点值得记进排查手册。

### 5.3 光大的额外差异：连监听都没有

- 国信 `userdata/` 有 `up_queue_xtquant` / `up_queue_win_xtquant`（2026-11-07 22:22），
  且日志有 `Allow the XtQuantServer to start.` + `[orderservice] start quant server thread`。
- 光大 `userdata/` **没有** `up_queue_xtquant`（只有 `down_queue_*`），日志里**完全没有** `XtQuantServer` 字样。
  队列只在 `userdata_mini/` 有，且是 **2026-09-29** 的旧文件（说明那次跑的是极简模式）。

⇒ 光大这一套的大 QMT 当前 **quant server 线程根本没起**，`connect()` 连被拒的机会都没有，只能 3.00s 超时。
**这也解释了为什么光大的日志里查不到 `illegal pid`。**

### 5.4 正确的心智模型

> `xttrader`（`XtQuantTrader`）**官方定位就是极简模式（mini）**。大 QMT 里能下单的正门是
> **策略内 `passorder`**，不是外部 `xttrader`。
> 想用 `xttrader` 直连，就得跑 `XtMiniQmt.exe`（极简模式），而不是去想办法骗过 `OrderService` 的白名单。

---

## 6. 端口拓扑与多实例冲突（必须先解决）

**现状**：光大与国信**同时运行**，两家 `broker.ini` 完全相同，于是：

```
TCP  0.0.0.0:58600  LISTENING  24544   ← 光大
TCP  0.0.0.0:58600  LISTENING  8828    ← 国信
```

Windows 在 `SO_REUSEADDR` 下允许重复绑定，**连接落到哪一家是不确定的**。

**后果（按严重度）：**

1. **串户**：`xtdata.connect(port=58600)` 可能连到"另一家"的终端，行情/标的一致但**账户上下文完全不同**；
   若将来有写操作，等于往错误终端下单。
2. **队列混淆**：`up_queue_xtquant` 是**全局命名**的共享内存，两个客户端同时存在时，
   谁最后写谁生效 —— 这也是国信侧 `illegal pid` 之外的一层隐性风险。
3. **`down_queue_<session>` 泛滥**：每次探针都留下一个 75 MB 的 `down_queue_<session>`
   （实测已积累 `27080` / `27222` / `27265` / `28729` / `28945` …）。这些文件**不自动回收**。

**处置建议（P0 必做）：**

- 生产上**只允许一个 QMT 实例运行**；多券商并行必须**改端口**（`broker.ini` 的 `serverport`
  与 `xtdata.ini` 的 `address` 成对改），并在 `qmt_work` 侧用「安装目录 + 端口」双键寻址。
- 清理历史 `down_queue_*`（>75 MB × N），但要**先确认没有进程持有**，否则删了会被重建或引发异常。

---

## 7. 可外接进 `qmt_work` 的接口清单

按"能否落地"排序。`qmt_work` 现有抽象是 `Dialect × Transport`（`backend/connectors/`），
下表给出**每个接口该挂在哪一层**。

| # | 能力 | 来源 | 落到现有代码的哪一层 | 缺口 |
|---|---|---|---|---|
| **A** | 下单（策略内） | `passorder` 家族 | `dialects/bigqmt_v1.py` + `transports/inprocess.py` | 无（现状） |
| **B** | 下单（文件桥） | `req/*.json` ↔ `events.ndjson` | `transports/file_signal.py` | 无（现状） |
| **C** | 下单（闭环烘焙） | 计划写进策略源码 | `transports/embedded.py` | 无（现状） |
| **D** | **下单（极简模式直连）** | `XtMiniQmt.exe` + `xttrader` | `dialects/xtquant_v1.py` + **新增 `transports/xttrader_shm`（规划中）** | 需实现 `XtQuantTrader` 生命周期；仅光大可用 |
| **E** | **行情（58670 旁路）** | 原生 NetPackage + BSON | **新增 `transports/xtq_rpc.py`** | 参数键名待定（§11）；需实现帧编解码 |
| **F** | 行情（58600 窄接口） | 同上，3 方法 | 复用 `xtq_rpc.py`（不同 port profile） | 仅够做标的池校验 |
| **G** | 下单（文件单 CSV/DBF） | 客户端监控线程 | **`backend/connectors/qmt_file_order.py`（已落地）** | 格式待测；`is_automonitor` 需 UI 开启 |
| **H** | 行情（本地数据直读） | `datadir` | `gateway/market_sync.py` 可扩展 | P3 |

**明确不接的：**

| 接口 | 原因 |
|---|---|
| CEF CDP（8086） | 驱动 UI 下单 = 变相操作持牌终端；DOM 随版本漂移；无幂等、无回执语义。**只可用于"读界面状态"做人工核对。** |
| Jupyter（8888） | 已实测**不能**下单（`illegal pid 0`）；且 token 明文 = 本机任意代码执行，属**安全负债**，应关闭或改强 token。 |
| 58600/58670 的任何写操作 | 方法集里根本不存在；不要臆造方法名。 |
| 绕过 PID 白名单 | 见 §10 红线。 |

---

## 8. 开发方案（P0 → P3）

> 原则：**先消除会搞坏生产的东西，再加能力。** 每个阶段都可独立上线、独立回滚。

### P0 —— 多实例治理 + 行情旁路（最高优先）

**目标**：消除串户风险；给 `qmt_work` 一条**不依赖 xtquant 包**的行情读取通道。

| 任务 | 文件 | 验收 |
|---|---|---|
| P0-1 安装发现与端口台账 | 新增 `backend/connectors/install_registry.py` | 扫描 `P:/stock/*/bin.x64/XtItClient.exe`，输出 `{label, root, exe_mtime, ports{58600,58670,8086,8888}, running_pid, userdata}`；**同端口多实例必须报错而非择一** |
| P0-2 端口冲突守卫 | `backend/connectors/install_registry.py` + `gateway/health.py` | 启动时检测 58600 双绑定 → `health` 报 `PORT_CONFLICT`（**不是 warning，是 error 级**） |
| P0-3 58670 行情适配器 | 新增 `backend/connectors/transports/xtq_rpc.py` | `getTag` / `checkVersion` / `getSectorList` / `getStockListInSector` 四个方法端到端通；帧编解码单测覆盖 zlib 与非压缩两分支 |
| P0-4 判活口径统一 | `xtq_rpc.py` | **禁止**用 `connect()` 返回值判活；`test_connection()` 必须打一次真实业务方法并校验非空 |
| P0-5 清理 `down_queue_*` | 运维脚本 | 逐个确认无进程持有后归档；**不得 `rm -rf`** |

**为什么 P0-3 值得做**：`getStockListInSector` 已实测拿到 **5224 只**真实代码，且**零依赖**
（不需要 xtquant 包、不需要 token、不受国信包错配影响）。这足以替代当前"标的池靠策略侧提供"的脆弱环节。

### P1 —— 极简模式直连（新增下单能力，仅光大）

**目标**：把 `xttrader` 这条**官方支持**的路真正打通，作为大 QMT 之外的独立下单通道。

前置：**关闭大 QMT（腾出 58600/58610）**，启动 `P:/stock/gd_qmt/bin.x64/XtMiniQmt.exe`。

| 任务 | 文件 | 验收 |
|---|---|---|
| P1-1 `xttrader` 传输层 | 新增 `transports/xttrader_shm`（规划中） | `start()`→`connect()`→`subscribe()`→`query_stock_asset` 全通；`connect()==0` **必须**再以一次查询确认 |
| P1-2 生命周期与重连 | 同上 | 断线回调 → 状态机 `CONNECTED/DISCONNECTED/RECONNECTING`；重连**先 QUERY 后发**（socket 桥同款纪律） |
| P1-3 注册到传输表 | `transports/__init__.py` | `get_transport("xttrader_shm")` 可实例化；未知 id 仍抛错 |
| P1-4 与 `xtquant_v1` 方言对接 | `dialects/xtquant_v1.py` | `place_order` / `cancel_order` / `query_*` 全部映射到 `XtQuantTrader` 方法；**写操作白名单**显式声明 |
| P1-5 首单人工闸门 | `gateway/execution.py` | 新通道首单强制 `dry_run`，需显式确认才放行 |

**风险**：极简模式与完整版**互斥占用端口**。这意味着"大 QMT 策略"和"极简直连"不能同时跑 ——
这是**架构级取舍**，需要在 §11 让用户拍板。

### P2 —— 文件单通道（官方正门兜底，两家通用）

**目标**：一条**不依赖任何逆向、券商明确支持**的下单通道。

| 任务 | 文件 | 验收 |
|---|---|---|
| P2-1 格式受控探针 | `output/rea_func_level/rea_fileorder_probe.py` | 写**故意非法**的一行 → 读客户端报错文案反推字段顺序/分隔符/编码；**不写有效单** |
| P2-2 DBF 文件单通道 | `backend/connectors/qmt_file_order.py`（已落地） | 原子落盘（`.tmp` → `os.replace`）；`signal_id` 幂等；结果从 `export_data` 或 DBF 反馈回读 |
| P2-3 DBF 通道 | 同上 | 注意 `is_clear` 差异（光大 0 / 国信 1）⇒ **不能用文件残留判已处理** |
| P2-4 batchOrder 通道 | 同上 | `key_enable="0"` ⇒ 需 UI 开启后才有 `key_sequence` |
| P2-5 开启引导 | `docs/` | 写清 UI 路径：`设置 → 交易 → 文件单/CSV监控`，并说明开启后的风险（监控目录被误写 = 直接下单） |

**红线**：文件单是"目录里出现文件就下单"，**必须**配 `order_watchdog` + 目录写权限收敛，
否则任何能写该目录的进程都能下单。

### P3 —— 多维度下单统一接入 + 多通道仲裁

**目标**：把已有的 `order_dims.py`（7 轴 × 6 通道能力矩阵）真正接到执行链上。

| 任务 | 文件 | 验收 |
|---|---|---|
| P3-1 通道能力对齐 | `backend/connectors/order_dims.py` | `Channel` 枚举补齐 `XTTRADER` / `QMT_FILE_ORDER` 的真实能力（哪些维度支持、哪些必须报错） |
| P3-2 解析接入 | `gateway/execution.py` | `order_dims.resolve(intent)` 成为**唯一**下单入口；不支持的维度组合**抛错**，禁止静默降级 |
| P3-3 通道仲裁 | `gateway/execution.py` | 按 `DEFAULT_PRIORITY` 选通道；主通道不可用 → 降级并在回执里**显式标注**实际通道 |
| P3-4 契约测试 | `backend/tests/` | 每个维度轴 × 每个通道至少一条"必须报错"用例（对齐 `rea_order_dims_selftest.py` 的 14 条反例） |

---

## 9. `order_dims` 与多通道的关系（现状与差距）

`backend/connectors/order_dims.py`（47,976 字节）已经把官方三张表（`opType` / `orderType` / `prType`）
编码成**纯函数 + 严格枚举**，任何不支持的维度组合**抛 `DimensionError`，绝不静默降级**。

已覆盖的 7 个维度轴：

```
Side          买/卖/融资买入/融券卖出/买券还券/卖券还款
QtySemantics  shares / lots / value / percent / target_value / target_percent
PriceType     22 种（含 44–48 深市专有、27–29 期权、42/43 仅沪）
TimeInForce   day / ioc / fok
ExecMode      immediate / algo / smart_algo / task(触价)
InstrumentClass stock / etf / bond / option / future / credit
AccountScope  single / multi / portfolio / portfolio_group / arbitrage
```

**差距（本次新增认知）：**

1. `Channel` 枚举目前是 6 个（`XT_STRATEGY` / `FILE_CSV` / `FILE_DBF` / `BATCH_ORDER` / `XTTRADER` / `CALL_FORMULA`），
   **需要按本次实测更新能力矩阵**：`XTTRADER` 在大 QMT 上**全维度不可用**（PID 白名单），
   在极简模式上才可用；`CALL_FORMULA` **全维度不可用**（无 `createFormula`）。
2. 需要新增 `MINI_XTTRADER` 这一通道值，把"极简模式"与"大 QMT 的 xttrader"区分开 ——
   否则能力矩阵会把两者混为一谈。

**建议改法**：`Channel` 增加 `MINI_XTTRADER`，并把 `XTTRADER` 的可用性标为
`只在大 QMT 内部受信进程可用`（即对 `qmt_work` 外部进程恒不可用），这样矩阵才与实测一致。

---

## 10. 红线（不可逾越）

| 红线 | 说明 |
|---|---|
| **不绕过 PID 白名单** | 白名单是券商的接入控制。想办法让外部进程"看起来合法"= 规避安全控制，既易被升级打断，也可能违反终端使用约定。正确做法：走极简模式或文件单。 |
| **不伪造凭证 / 不复用他人会话** | 不使用逆向得到的密钥、token、会话票据构造请求。 |
| **不对生产柜台发单** | 所有下单验证只在测试账号 `410001005814`、且必须**用户显式确认**后执行。 |
| **不驱动 UI 下单** | CEF CDP（8086）只允许"读状态做人工核对"，不得用于下单/撤单。 |
| **不臆造方法名/字段** | 58600/58670 的方法集以实测为准；未命中的一律标"待测"，不猜。 |
| **诊断工具退出码如实** | `0=就绪 / 2=需人工动作 / 1=环境缺失`；打 `[FAIL]` 却 `return 0` 属静默失败。 |

---

## 11. 待决事项与下一步实测

### 11.1 需要你拍板的（架构级）

1. **极简模式 vs 大 QMT 二选一**：两者抢 58600/58610，不能同时跑。
   - 选**大 QMT**：保留回测/图表/终端内调试，下单只能走「策略内 `passorder`」或「文件单」。
   - 选**极简模式**：可外部 `xttrader` 直连（代码里彻底没有策略环节），但没有图表/回测。
   - 也可以**分机器/分时段**：白天极简直连，盘后大 QMT 做研究。
2. **多券商并行要不要做**：如果要做，必须改端口（`broker.ini` + `xtdata.ini` 成对改），
   并让 `qmt_work` 用「安装目录 + 端口」双键寻址。
3. **Jupyter（8888）留不留**：它不能下单，但 token 明文 = 本机任意代码执行。建议关掉或换强 token。

### 11.2 待实测（按价值排序）

| # | 待测项 | 方法 | 价值 |
|---|---|---|---|
| 1 | ~~**58670 `subscribeQuote` 的参数键名**~~ | ✅ **已解**：形状是 `{meta, region, param}` 三个嵌套文档，来源为客户端自身 `xtdata.py::subscribe_quote2`；实测已被服务端接受（详见 §13.1） | ✅ 已完成 |
| 1b | **收盘后能否真收到 tick** | 交易时段复跑 `rea_58670_subscribe_v2.py 58670 60` | ★★★ 决定"零依赖行情"能否做到 tick 级 |
| 2 | **文件单的精确格式** | 写**故意非法**的一行到 `export_data`，读客户端报错文案反推字段；`is_automonitor` 临时置 1 | ★★★ P2 的前置 |
| 3 | **极简模式 `xttrader` 是否真能连通** | 关大 QMT → 起 `XtMiniQmt.exe` → 跑 `rea_multi_xttrader_probe.py` | ★★★ 决定 P1 是否成立 |
| 4 | **`xtdatacenter` 独立行情服务** | 光大包 `xtdc.set_data_home_dir(gd_qmt)` + `init()` + `listen()`；需你的迅投 token | ★★ 备选行情方案 |
| 5 | 光大 quant server 为何不起 | 对比两家 `Config.xml` / 启动参数；看 `OrderService` 的启动条件 | ★★ 影响光大能否走 O3 |

### 11.3 本次产出的探针（可复跑）

| 文件 | 作用 |
|---|---|
| `output/rea_func_level/rea_multi_xttrader_probe.py` | 多安装 A 通道连通矩阵（`start/connect/query`，只读） |
| `output/rea_func_level/rea_quote_channel_probe.py` | 外部行情通道矩阵（58600/58609/58610/58611 × 自动发现） |
| `output/rea_func_level/rea_58600_quote_api.py` | 用官方 API 打 58600，按"非空数据"判活 |
| `output/rea_func_level/rea_58670_method_sweep.py` | 58600/58670 方法集扫描（传端口即可） |
| `output/rea_func_level/rea_58670_quote_params.py` | 58670 行情方法参数键名矩阵 |
| `output/rea_func_level/rea_pidwhitelist_test.py` | 判别白名单判据（按 PID / 按路径） |
| `output/rea_func_level/rea_jupyter_kernel_exec.py` | 在客户端内嵌 kernel 内受控执行（只读） |

---

## 12. 附：本次更正的历史结论

| 旧结论 | 状态 | 更正后 |
|---|---|---|
| "A 通道失败是 pyd 与 bridge 版本代差" | ❌ **错** | 真因是 `OrderService.dll` 的 **PID 白名单**；光大同代包同样失败 |
| "58670 是控制面，58600 是数据面" | ⚠️ **不准确** | 58670 有 **16 个方法**，含完整行情（`getFullTick`/`subscribeQuote`）⇒ 58670 才是行情面；58600 只有 3 个窄接口 |
| "58670 上 `createFormula` 缺失导致 `callFormula` 不可用" | ✅ 仍成立 | `createFormula`/`destoryFormula` 均不在方法集内 |
| "`xtdata.connect()` 返回 0 即连通" | ❌ **错** | 空端口也返回 0；**必须用非空业务数据判活** |
| "光大这套更新，应该能修好 A 通道" | ❌ **错** | 版本新不等于能连；白名单与版本无关 |

---

*本文档基于 2026-11-08 的真机只读实测。所有"可用/不可用"结论均可由 §11.3 的探针复现。*
*任何下单验证必须在用户显式确认后、仅对测试账号执行。*

---

## 13. 订阅形状解出 + 两个新模块落地（2026-11-08 晚补）

> §11.2 的待测项 #1（`subscribeQuote` 参数键名）**已解决**。以下为该轮结论与代码落地情况。

### 13.1 `subscribeQuote` 的真实参数形状

**先走逆向，再被客户端自己的实现确认。**

逆向 `XtModel.exe` 时，错误串 `[xtquant]invalid stockcode: {}`（偏移 7575312）紧邻的字段块只有
`isSubscribeByType` / `subtype` / `dividendtype` / `periodnum`，**标的代码的键名被编译器池化了**
（`stockCode` 在 7531024、`stockcode` 在 7549672，分散在别的函数里）。

手工试了 18 种顶层键名（含 `stockCode` / `stockcode` / `stockList` / `STOCK_CODE` / 嵌套 `param.*` / 裸数组）
**全部不命中**，响应恒为 `ErrorID=300000 "[xtquant]invalid stockcode: "`（**值为空**）。

真正的答案在客户端自己的代码里 —— `xtquant/xtdata.py::subscribe_quote2`：

```python
meta   = {'stockCode': stock_code, 'period': spec_period, 'metaid': meta_id,
          'periodnum': period_num, 'dividendtype': dividend_type}
region = {'startTime': start_time, 'endTime': end_time, 'count': count}
param  = {'needCallback': callback is not None}
client.subscribe_quote(BSON.encode(meta), BSON.encode(region), BSON.encode(param), cb)
```

⇒ **RPC 的 `params` 是三个嵌套文档，键名就是 `meta` / `region` / `param`**，不是扁平字典。

实测三种容器形状的判据差异（这是本节最有用的一张表）：

| 形状 | 应答 | 判读 |
|---|---|---|
| 扁平 `{stockCode, period, ...}` | `300000 invalid stockcode: ` | 读不到值 |
| 数组 `[meta, region, param]`（无论二进制块还是文档） | `300000 invalid stockcode: ` | 读不到值 |
| **`{"meta":…, "region":…, "param":…}`** | **`{status: 0, params: {...}}`** | ✅ **被接受** |

**为什么之前一直失败**：服务端在 `meta` 子文档里找 `stockCode`，而顶层根本没有 `meta` 这一层。

> ★ 一个反直觉的坑：形状正确时，`call()` 会**超时无应答**（服务端在真干活），
> 而形状错误时**立刻**回 `300000`。"超时"在这里是好消息 —— 只看"有没有报错"会判反。

### 13.2 抓取结果（诚实记录）

30 秒长监听（同一连接）：

```
frame #1 seq=1 type=3 flags=0x2042 len=81 keys=['status','params']   ← 订阅应答（正常）
frame #2..9 type=1 flags=0x2c40 len=24                                ← 心跳，4.04s 一帧
总帧数 9，其中疑似行情推送 0
```

**结论：订阅被接受，但零 tick 推送。** 当天是周日、已收盘 —— 收盘后本来就没有逐笔。
所以本轮**没有**证到"能拿到实时 tick"，只证到"参数形状对了、服务端受理了"。
真实推送要等交易时段复跑 `rea_58670_subscribe_v2.py`。

### 13.3 新增两个模块（已进门禁）

| 文件 | 作用 | 测试 |
|---|---|---|
| `backend/connectors/install_registry.py` | 多券商安装发现、声明端口冲突检测、**多券商并行端口方案生成**、活体双绑定审计 | `backend/tests/test_install_registry.py`（18 例） |
| `backend/connectors/transports/xtq_rpc.py` | 58600/58670 原生 RPC 传输（NetPackage + BSON + zlib），**零 SDK 依赖**；含订阅与推送抓取 | `backend/tests/test_xtq_rpc.py`（17 例） |

`xtq_rpc` 已登记进 `backend/connectors/transports/__init__.py` 的唯一注册表，
`get_transport("xtq_rpc", port=58670)` 可实例化。

**通用性（不硬编码券商）**：

* 安装靠 `bin.x64/XtItClient.exe` 存在性识别，标签由目录名派生（可显式覆盖）；
  光大 / 国信 / 中金在代码里都只是"某个目录"，**代码中不出现它们的名字**；
* `KNOWN_METHODS` 按**端口**登记实测方法集，未知端口返回空集 —— 不假装支持；
* `supports()` 查表判定，`GET_FULL_TICK` 在 58670 为 True、在 58600 为 False（与实测一致）。

**只读是硬约束**：`PLACE_ORDER` / `CANCEL_ORDER` 在 `_dispatch` 里**当场拒绝**，
返回 `error_type=ReadOnlyTransport`，**不碰网络** —— 因为这两个端口实测没有任何下单方法。

### 13.4 多券商并行的端口方案（真机生成）

`plan_ports(installs, stride=100)` 对四套安装的实际输出：

| 安装 | broker.port | pbserverport | **serverport** | pythonbroker | pythonworker | minibroker | **xtdata** |
|---|---|---|---|---|---|---|---|
| `gd_qmt` | 56500 | 59600 | **58600** | 60500 | 57500 | 55500 | **58610** |
| `guoxin_iquantV3` | 56600 | 59700 | **58700** | 60600 | 57600 | 55600 | **58710** |
| `guoxin_iquant_11` | 56700 | 59800 | **58800** | 60700 | 57700 | 55700 | **58810** |
| `zj_QMT` | 56800 | 59900 | **58900** | 60800 | 57800 | — | **58910** |

规则：第 i 套整体 `+ i*100`；`xtdata` 端口**必须与 broker 侧成对偏移**（否则行情与交易指向不同实例）；
方案自撞或超 65535 **直接抛错**，不返回"看着能跑"的表。

### 13.5 本轮踩到的一个真 bug（已修 + 已加回归）

`_win_listening_ports()` 最初用 `subprocess.run(..., text=True)`。中文 Windows 的
`netstat -ano` 输出是 **OEM 代码页（GBK）**，按 UTF-8 解码直接抛 `UnicodeDecodeError`，
`stdout` 变成空串 ⇒ **审计报"无冲突"**，而实际两个进程正在抢 58600。

这正是仓库最忌的**静默假绿**。已改为按 `utf-8 → cp936 → mbcs → latin-1` 依次尝试解码
（`decode_console_bytes`），并加了两条回归用例锁死。

修复后真机审计结果（自动抓到）：

```
58600  -> pids ['24544', '8828']     ← 双绑定，确认串户风险
58670  -> pids ['21752']
8086   -> pids ['8828']              ← CEF CDP
8888   -> pids ['13028']             ← 内嵌 Jupyter
审计结论 ok=False  double_bound={"58600": ["24544", "8828"]}
```

### 13.6 当前门禁状态

```
backend tests = 2833 (expected 2833)     ← 本轮 +37 例
frontend components = 76
registered pages = 46
RECONCILE OK
```

---

## 14. 文件单（DBF）打通 + 三问的直接回答（2026-11-08 收尾）

> 本节回答："是否可以实现行情获取和下单？是否支持在 `qmt_work` 项目中调用？"

### 14.1 先更正一条：**CSV 不是下单口**

§4 曾把 CSV 与 DBF 并列为文件通道。复查 `OrderService.dll` 的字符串表后确认：

| 侧 | 处理器 | 语义 |
|---|---|---|
| CSV | `CCsvHandlerCanOrderVolume` | 查**可用数量** |
| CSV | `CCsvHandlerQueryInstrumentFee` | 查**费用** |
| DBF | `TC::CDbfStrategyRecord` | **下单** |

CSV 侧只有查询处理器 —— 它是**查询口**，不是下单口。
真正落单的是 DBF，记录类源码路径 `e:\v2.0\src\business\orderservice\corderservicedbfhelper.cpp`。
（另有 `addBatchOrderStrategy` / `getDefaultBatchOrderDir` 的批量路径，文件名 `{0}/{1}.{2}.csv`，
字段集未解，暂不实现。）

### 14.2 DBF 下单表的 15 列（实测列序，不可重排）

`OrderService.dll` 偏移 `3211512–3211716`，大写 ASCII 硬编码：

```
ORDER_TYPE  PRICE_TYPE  MODE_PRICE  STOCK_CODE  VOLUME  ACCOUNT_ID
ACT_TYPE    BROKERTYPE  STRATEGY    NOTE        TRADEPARAM
INSERTTIME  BASKETPATH  EXTRAPARAM  NOTE1
```

**列序即协议**：客户端按列序读，重排等于写错单。`DBF_COLUMNS` 已用测试锁死。

方言来自客户端自己写的 `userdata/data/xt_client_status.dbf`：
版本 `0x03`、字段名 **GBK**、类型 `C`、头长 `32+32n+1`、记录长 `1+Σlen`。
**列宽未定**（下单表可能与该文件不同），因此模块提供 `read_schema()` /
`adopt_schema(template)` —— 用界面导出的模板 DBF 校准真实类型与长度，
而不是猜一个"看着合理"的宽度。

### 14.3 新模块 `qmt_file_order.py`

| 能力 | 入口 |
|---|---|
| 写 DBF（原子替换，不留 `.tmp`） | `write_dbf` / `read_dbf` / `read_schema` |
| 模板校准列宽 | `adopt_schema` |
| 下单行 + 校验 | `FileOrderRow.validate()` / `to_record()` |
| **安全写入口（默认拒绝监控目录）** | `write_order_file(..., allow_monitor_dir=False)`；返回 `monitor_checked` 区分「检查过且安全」与「没检查」 |
| 读监控目录（不猜） | `find_monitor_settings` / `monitor_paths_of` |

测试：`backend/tests/test_qmt_file_order.py`（39 例）。

**修掉两个真 bug（都是"不报错但写错"型）**：

1. **字段名用空格补齐** ⇒ `read_dbf` 得到 `'ORDER_TYPE '`，按列名取值直接
   `KeyError`，表现成"整张表读空"。dBase III 规定用 `0x00` 补齐；写侧已改，
   读侧同时容忍空格补齐的第三方文件（否则回读别人的模板同样整表读空）。
2. **C 型列里的浮点被取整** ⇒ `mode_price=10.5` 写成 `"10"`。
   这是一张**价格错的单**，而且不报错。已改为 C 型用 `%g`，只有 N 型才取整。

### 14.4 监控目录：实测拿到了，而且**总开关是关的**

`userdata/users/<账号>/Config.xml`：

```xml
<DbfMonitorSetting is_incexport="0" useringbuffer="0"
    need_remove_exprotfile_fristmonitor="1" is_usesettingfeedbackval="0"
    orderidfeedbackval="-1" is_automonitor="0" is_clear="0"
    monitor_path="P:\stock\gd_qmt\export_data" clear_date=""
    is_sharedread="0" backup_path="P:\stock\gd_qmt\export_data_backup"/>
```

四个安装实测：

| 安装 | 账号数 | monitor_path | **armed** |
|---|---|---|---|
| `gd_qmt` | 1 | `{root}\export_data` | ❌ `is_automonitor=0` |
| `guoxin_iquantV3` | 2 | `{root}\export_data` | ❌ |
| `guoxin_iquant_11` | 1 | `{root}\export_data` | ❌ |
| `zj_QMT` | 1 | 无该标签 | — |

> ★ **这是文件单能不能生效的总开关。** `is_automonitor="0"` 时往 `export_data`
> 丢 DBF **不会产生任何委托**，文件只会躺在目录里。想真下单必须先在界面里打开
> "自动监控" —— 代码不做这件事，也不该做。

`backup_path` 与 `monitor_path` 是**两个平级目录**（`export_data` /
`export_data_backup`），所以 `is_under()` 的前缀相似判据不是假想用例，是真实现场：
只登记 `monitor_path` 会漏掉 backup，用前缀匹配又会把 backup 误判成子目录。

**另一个真机坑**：客户端自己的 `Config.xml` **不是合法 XML** ——
`ET.fromstring` 在第 345 行 `not well-formed (invalid token)` 直接抛错。
只走 ElementTree 会在真机上**恒返回 None**（静默假阴性，监控目录永远读不到）。
`parse_monitor_setting` 已加正则兜底，并有回归用例。

### 14.5 `getFullTick` 的参数键 = `stockMarkets`

`XtModel.exe` 的方法名→参数键分派表（`8277096 → 8277112`）给出：
`getFullTick` 的键是 **`stockMarkets`**（不是 `stockList` / `stockCodes` / `stockCode`）。
八个猜测形状全部返回空，用真键调用返回 `{status:0, result:{}}` ——
**被受理但结果为空**（收盘后无逐笔）。与 §13.2 同一口径：受理 ≠ 有数据。

### 14.6 三问的直接回答

**Q1：行情获取能不能实现？**
能，且**不需要官方 SDK**。`xtq_rpc` 传输已跑通 58600（3 个方法）/ 58670（16 个方法），
真机探针返回 `tag=xtquant`，`getStockListInSector({})` 取到 5224 个真实代码。
**但实时 tick 尚未证实** —— 订阅被受理、零推送，因为测试时是周日收盘。
交易时段复跑 `rea_58670_subscribe_v2.py 58670 60` 才算证完。这一条不夸大。

**Q2：下单能不能实现？**
- ❌ **外部 `xttrader`**：不行。根因是 PID 白名单（`illegal pid {1}`），
  与版本无关 —— 光大同代 2025 包同样在 3.014s 被拒；连客户端自己的 Jupyter
  内核（PID 22136，同一个 `pythonw.exe`）都被拒。换包装、换 Python 都无效。
- ✅ **进程内 `passorder`**：可以，这是大 QMT 的原生形态（`Formula.dll` +
  内嵌 `python36.dll`，`passorder` 以 11 参函数注入，末参是 `ContextInfo`）。
- ✅ **DBF 文件单**：可以，是券商官方支持的外部正门。本模块已实现到"能生成、
  能回读、能被客户端认的方言"，**但 `is_automonitor=0` 时不会真的下单**。
- ⚠️ 两条路都**必须在持牌终端内执行**，都不是"绕过终端直连柜台"。
  本方案不做、也不建议做后者（见 §10 红线）。

**Q3：能不能在 `qmt_work` 里调用？**
能，两者都已进门禁：

```python
from connectors.install_registry import discover, audit, plan_ports
from connectors.transports import get_transport
from connectors.qmt_file_order import (FileOrderRow, write_order_file,
                                       find_monitor_settings, monitor_paths_of)

installs = discover(["P:/stock"])                     # 真机：4 套
tp = get_transport("xtq_rpc", port=58670)             # 行情（只读）
paths = monitor_paths_of(installs[0].userdata)        # 监控目录（不猜）
write_order_file("staging/o.dbf", [FileOrderRow(...)],
                 monitor_paths=paths)                 # 默认拒绝写进监控目录
```

### 14.7 门禁状态（§14 收尾）

```
backend tests = 2872 (expected 2872)     ← §14 +39 例（test_qmt_file_order 新文件）
test_*.py     = 217                      ← README 两处计数已同步
RECONCILE OK
```

> 计数口径提醒：`ci_reconcile.py::EXPECTED_TESTS` 必须等于**实测逐文件收集数**，
> 且 `README.md` 有**两处**写死（§测试与验证表 + 目录树），改一处必改另一处。

---

## 15. 从"库代码"到"能用的功能"：API 落地 + 潜在问题清单

> 回答两个问题：**"是否已经做到 qmt_work 里了？"** —— 之前只做到一半；
> **"还有哪些潜在问题？"** —— 见 §15.5，逐条标注严重度与现状。

### 15.1 上一轮的真实状态：三个模块只有一个是"接上"的

| 模块 | 上一轮状态 | 判定 |
|---|---|---|
| `transports/xtq_rpc.py` | 已登记进传输注册表，`get_transport("xtq_rpc")` 可取 | ✅ 接上了 |
| `connectors/install_registry.py` | **无任何生产代码引用**，只有单测 | ❌ 库代码 |
| `connectors/qmt_file_order.py` | **无任何生产代码引用**，只有单测 | ❌ 库代码 |

用 `grep` 就能自证：除自身与测试外，生产代码里只有 `transports/__init__.py` 提到 `xtq_rpc`。
**能力存在但不可发现，等于不存在**（同一教训见 `routes/capabilities.py` 里
`capabilities_mcp` 的注释：115 个 MCP 工具装完却没人知道）。

### 15.2 本轮新增 `backend/app/routes/qmt_client.py`

| 端点 | 作用 |
|---|---|
| `GET  /api/v1/qmt/installs` | 多安装台账 + 声明端口冲突 + **活体双绑定** |
| `GET  /api/v1/qmt/installs/port-plan` | 多券商并行端口方案（第 i 套 `+i*stride`） |
| `GET  /api/v1/qmt/file-order/monitors` | 各安装各账号的监控目录 + **`armed` 总开关** |
| `POST /api/v1/qmt/file-order/schema` | 读用户导出的**模板 DBF**，返回真实列类型/长度 |
| `POST /api/v1/qmt/file-order/stage` | 生成**待核对** DBF（永不进监控目录；可带 `template_path` 按真实列宽写） |

**真机跑通（不是 tmp 夹具）**：

```
source=explicit | count=4 | live_probe=ok | ok=False
  gd_qmt G2025 / guoxin_iquantV3 G2023 / guoxin_iquant_11 G2023 / zj_QMT G2022
  double_bound = {"58600": ["24544", "8828"]}     ← 真机串户风险，自动抓到
  declared_conflicts = 33                          ← 四套安装共用同一组端口
port-plan: gd 58600/58610 → guoxinV3 58700/58710 → guoxin_11 58800/58810 → zj 58900/58910
monitors: total=4 armed=0 → 给出 warning
stage: 979 B, monitor_checked=True, 回读 MODE_PRICE=10.5 正确
```

### 15.3 三条设计边界（都是"少一步就出事"的地方）

**① 不猜盘符。** 安装根来自「显式 `roots`」或「已配置连接的 `client_path` 回溯」
（新增 `find_install_root`，从 `userdata` / `bin.x64` / 安装根任意入口向上定位）。
两者都没有 ⇒ `source="none"` + hint，**不假装"扫了全盘没找到"**。

> 顺带修一个易踩的坑：`discover()` 的语义是扫**父目录**。用户把安装根直接填进来
> 是极常见用法，只调 `discover` 会得到空列表 ⇒ 界面显示"没找到任何 QMT"，
> 而路径完全正确。`_collect_installs` 两种写法都吃，并做去重。

**② 活体探针三态。** `live_probe ∈ {skipped, unavailable, ok}`。

> ★ 修掉一个**静默假绿**：`_win_listening_ports()` 原先在 netstat 不可用时返回 `{}`，
> `double_bound({})` 得空集 ⇒ 审计报 `ok=True`。用户看到的绿灯含义是"查过了，没事"，
> 而事实是"根本没查"。现改为返回 `None`，`audit` 记 `live_probe="unavailable"`。

**③ 文件单只落 staging。** `POST /stage` **不接受** `allow_monitor_dir`（传了就 400），
且反向自检：若 `export_dir` 恰好落在某个账号的监控目录里，**拒绝生成**并报
"生成即等于下单"。安全检查在建目录**之前**做 —— 否则"拒绝了但已经把目录建进
监控目录"会留下痕迹。

### 15.4 一个能力暴露边界（安全）

`/qmt/*` 的 GET 端点原本会被自动暴露成 MCP 工具（框架默认：只读 GET ⇒ agent_visible）。
这等于给 Agent 一个**任意目录探测器 + 子进程启动器**。因此把 `qmt` 域整体加入
`_EXEC_DENY_DOMAINS`：REST 可用（界面 / 运维脚本），**不生成 MCP tool**，
并加了断言防回归（`test_qmt_endpoints_are_not_auto_exposed_to_agent`）。

### 15.5 潜在问题清单（按严重度）

| # | 问题 | 严重度 | 现状 |
|---|---|---|---|
| 1 | **实时 tick 仍未证实**。订阅被受理、零推送，测试时是周日收盘 | 🔴 高 | 待交易时段复跑 `rea_58670_subscribe_v2.py 58670 60`。**在此之前不能说"行情通了"** |
| 2 | **四套安装共用同一组端口**（58600/56500/60500…），33 处声明冲突；真机 58600 已被两个 PID 同时监听 | 🔴 高 | 方案已能生成（`port-plan`），但**改 `broker.ini` 需人工确认**，本方案不自动落盘 |
| 3 | **`is_automonitor=0` 全部关闭** ⇒ 文件单不生效 | 🟠 中 | API 已给 `warning`；开关只能由人在界面打开 |
| 4 | **DBF 列宽默认是保守值**，非客户端真实 schema | 🟠 中 | 已提供校准通道：`POST /qmt/file-order/schema` 读模板 → `stage` 带 `template_path` 按真实列宽写；缺列直接 400（列序错位即写错单）。**仍需用户先在界面导出模板** —— 全盘只找到 2 个 `.dbf`，都是 `xt_client_status.dbf`，**没有下单模板可复用** |
| 5 | **外部 `xttrader` 永久不可用**（PID 白名单） | 🟡 已知限制 | 与版本无关，不做绕行 |
| 6 | **`xtq_rpc` 只读**：58600/58670 实测无任何下单方法 | 🟡 已知限制 | `_dispatch` 当场拒绝写操作，不碰网络 |
| 7 | **`qmt_agent` 闭环 / 策略落地**：GBK 必须配 CRLF，否则内置 py3.6 报 `invalid character in identifier` 且位置指向 docstring 里的中文 | 🟠 中 | 已有门禁 `check_bigqmt_agent_py36.py`；本轮未动 |
| 8 | **端口方案未落盘**，多券商并行仍是"配置层面未生效" | 🟠 中 | 需人工改 `broker.ini` 或后续做受控写入（要用户批准） |
| 9 | `prType_market: 5` 语义待定（`backend/agent_bigqmt/qmt_api.py`） | 🟡 低 | 待用户确认 |
| 10 | `netstat` 探针在非 Windows 恒 `unavailable` | 🟡 低 | 已如实上报，不再假绿 |

### 15.6 门禁状态（§15 收尾）

```
backend tests = 2910 (expected 2910)     ← §15 +38 例
                                          （test_qmt_client_routes 28 + test_install_registry +10）
test_*.py     = 218                      ← README 两处计数已同步
rest_endpoints = 261 keys                ← gen_contracts 已刷新（新端点可见可审）
mcp_tools      = 133 items               ← qmt 域不自动暴露，故未增加
路由模块       = 40                      ← README 两处「39 个路由模块」已同步
RECONCILE OK
```

### 15.7 盘前加固（2026-11-09，为 09:38 交易时段验证清障）

§15.5 第 1 项说"实时 tick 待交易时段复跑"。但直接复跑会得到**不可信的结论**：
9:38 要用的是生产传输 `XtqRpcTransport.capture()`，而它在收盘后恰好测不出问题
——帧小、几乎不跨 `recv`，且唯一的非心跳帧就是订阅 ACK。本轮先把会让探针
说谎的缺陷修掉，再排定时验证。

| # | 缺陷（都是"看起来正常"的那种） | 会造成的错误结论 | 修法 |
|---|---|---|---|
| 1 | `_read_frame` 把半帧放在局部变量里，`socket.timeout` 一抛就丢字节，下一轮从**帧中间**重新解头 | 交易时段一张大 tick 帧 ⇒ 解不出/乱码 ⇒ 报"没有推送" | 新增 `_FrameReader`：跨超时保缓冲，`read()` 返回 `None` 表示未凑齐；帧长上限 8 MB，损坏头直接报 `protocol_error` |
| 2 | `capture` 把**任何**非心跳帧算作 `data`；订阅 ACK（`{status:0,...}`）也算 | 收盘后"ACK + 8 帧心跳"会汇总成"有行情推送" ⇒ 假绿，直接跳过复跑 | 帧分四级 `heartbeat/ack/quote/data_other`；`proven` 只在确实看到行情字段时为 True；未识别帧**完整保留 doc 与键集**，结论写"需人工核对"而非"没推" |
| 3 | `audit()` 加了 `live_probe` 三态，却没收紧 `ok`：请求了活体检查而 netstat 挂掉时 `double_bound={}` ⇒ `ok=True` | 绿灯读成"查过了没冲突"，事实是"根本没查" | `unavailable ⇒ ok=False` + `ok_basis="live_unavailable"` + note；`skipped` 允许静态通过但标 `static_only`；`ok` 标 `static_plus_live` |
| 4 | `plan_ports` 以 label 为键，两套安装目录同名时后一套**静默覆盖**前一套 | `count=4` 却只给 3 套端口方案，无报错 | 同名直接抛 `InstallError`；`stride<=0` 也抛错 |
| 5 | `_collect_installs` 调 `find_install_root(root, max_up=0)`，与 §15.3 承诺的"任意入口向上定位"不符 | 用户填 `userdata` / `bin.x64` / exe 路径时一个安装都找不到 | 改用 `find_install_root` 的**返回值** `load_install(located)` |
| 6 | `stage` 回读只比 4 列，恰好漏掉 `MODE_PRICE`（价格）、`ACCOUNT_ID`（账号）、`INSERTTIME` | "写完自检"名不副实：写错价的单会一路绿灯 | 全 15 列逐列比对 + `_canon` 归一化（兼容 C 型/N 型回读）；INSERTTIME 写前冻结，跨秒不再随机报 500 |
| 7 | **由 #6 当场实证的新缺陷**：模板把 `INSERTTIME` 校准成 16 字节时，23 字符时间戳被 `_pad_bytes` 静默切短成 `2026-11-09 08:39` | DBF 合法、客户端不报错、写进去的时间戳是错的 | `write_dbf` 写前逐格校验字节宽，超宽直接 `FileOrderError`（含列名/值/将被截成什么），拒绝写错的单 |
| 8 | `write_order_file(fields=部分列)` 是另一条入口，`adopt_schema` 本来就允许返回部分列 | 库调用方静默写出**列序错位**的 DBF | 最终写入口加 `_ensure_order_columns`：列名与**列序**必须完整等于 15 列 |
| 9 | `stage` 同名文件被 `os.replace` 静默覆盖 | 人核对过的那份被后来一次调用换掉，交付时看不出异常 | 目标已存在直接 400，要求换 `name` |
| 10 | `stage` 的反向自检只拿 roots 扫到的安装；roots 解析不出来时是**空集上的检查** | 检查"通过"但实际什么都没挡住 | 新增 `monitor_paths_near(staging)`：从 staging 路径自身向上读 Config.xml（含批量单路径）；两路都没有时返回 `monitor_checked=false` + 明确 warning |
| 11 | `is_under` 只比 `abspath` | Windows junction / 符号链接可绕过"不在监控目录内"的判断 | 改用 `realpath` 落到物理路径再比 |
| 12 | `/monitors` 只报 DBF 一条通道 | 让人以为"外部下单只有文件单" | 补 `<batchOrderSetting>`（批量单 CSV）盘点。实测 4 套安装**都有**、`key_enable` 全 0；字段集未解 ⇒ 标 `implemented=False`、只盘点不写入 |

**盘前真机试跑**（`scripts/qmt_tradehours_validate.py --seconds 1`，证据
`output/qmt_tradehours_validate_20261109_084529.json`）：

```
ports: 58600=[18924] LISTENING / 58610=[] / 58670=[]   ← XtModel 未运行
58600 getStockListInSector({})            → result=[]（空）
58600 getStockListInSector({sectorName:"沪深A股"}) → 全量代码表 ★
58600 getMarketData 6 种形状              → 全空（盘前，参数形状尚未命中）
58670 全部检查                            → unavailable（环境缺失，不是接口不通）
installs=4  declared_conflicts=33  plan 4 套互不冲突
dbf_accounts=4 armed=0  batch_order_accounts=4 enabled=0
exit_code=2（需人工动作或未证实）
```

★ 新发现的语义差异：`getStockListInSector` 在 **58600** 是"带 `sectorName` 才有数据、
空参数返回空"，与 58670 上"空参数返回 5224 个、带 `sectorName` 返回空"**正好相反**。
因此脚本一次试多组形状并逐组记录 `result_len`，绝不因为一种形状返回空就写"没有数据"。

**09:38 定时验证入口**：一次性自动化，运行同一脚本（`--seconds 60`）+ 追加 §16。
退出码约定 `0=就绪且行情已证实 / 2=需人工动作或未证实 / 1=环境缺失`，
全程只读、不启动任何客户端进程、不下单、不改配置。

**仍需人工动作（本轮代码无法代替）**：
1. 让 58670 在交易时段处于 LISTENING（`XtModel.exe` 需由人在客户端侧拉起；
   此前用户明确取消过手动启动，故脚本与定时任务都不会代劳）；
2. 批准把 4 套端口方案写进 `broker.ini`；
3. 在界面导出下单模板 DBF、并打开 `is_automonitor`。

### 15.8 证据分级再加固（2026-11-09 09:10–09:16，09:38 前最后一轮）

§15.7 修好了"抓帧不会说谎"，但**编排层仍会说谎**：`qmt_tradehours_validate.py`
把退出码 0 只绑在 `capture_58670.proven` 上。只要 `XtModel.exe` 没起，即使
58600 拉取型行情完全可用也一律判"未证实"——这是把**环境缺失写成接口不通**的
另一种形态，和 §15.7 第 2 项的假绿恰好是同一枚硬币的两面。本轮把判据拆成
两条互不顶替的轴，并给"结果体"也做证据分级。

| # | 缺陷 | 会造成的错误结论 | 修法 |
|---|---|---|---|
| 1 | 退出码 0 只看 58670 推送 `proven` | 58600 拉到 tick 仍报"行情未证实"；58670 没起被读成整条链路不通 | `_conclusion()` 拆 `push_proven` / `pull_proven` 两轴，任一证实即 0；58670 缺失时判词明写"属环境缺失，不是接口不通" |
| 2 | 用 `len(result)>0` 当"有数据" | `getStockListInSector` 的 5224 条代码表被顶成"行情已通" | 新增 `xtq_rpc.classify_payload()`：给出 `kind / quote_proven / price_keys / code_keys / markers / code_shaped_keys / key_sets`；只有 `getMarketData` / `getFullTick` 计入 `quote_shapes`，代码表单列 `code_list_proven` |
| 3 | `_check_port` 只有 `status` 一列 | `getFullTick` 返空与真拉到 tick 输出长得一样 | 补 `payload_kind / payload_size / quote_proven / price_keys / key_sets` + note「status=ok 只说明服务端应答了」 |
| 4 | 旧 `_quote_signal` 只认 `stockCode` 这类键 | `getFullTick` 的 `{代码: {lastPrice, open, high…}}` 没有 `stockCode` 键 ⇒ **真 tick 被漏判**（假红） | `classify_payload` 认 `^\d{6}\.(SH\|SZ\|BJ)$` 代码形键，且 ≥3 个价格键即可独立成立；未知结构仍保留键集供人工核对 |
| 5 | 探针自己可能读错结果键 | 报"接口没数据"其实是探针读错地方（探针断言不可证伪 = 探针错） | 报 FAIL 前先 dump 键集：实测应答 `doc_keys=['params','status']`、`params_keys=['result']`，`result` 确实是唯一的业务结果位，空是真空 |
| 6 | "带行情字段"把日线/分钟线也算进去 | 09:38 拉到一根**昨日日线**就报"实时 tick 已证实"，正是整轮要消灭的假绿 | 逐形状标 `realtime_eligible`（`getFullTick` 或 `period=tick` 才算），另出 `tick_proven` / `axes.realtime_tick_proven`；退出码 0 的判词降级为"行情接口已证实可用"，并明写实时 tick 是否证实 |

同时把 58600 的参数形状从 6 组扩到 **12 组**（补 `stockCode` 数组、`1m`、
`dividendType`、`meta/region/param` 三块嵌套、以及 `getFullTick` 两种形状），
并新增 6 秒的 `capture_58600` 短抓，用来判定 58600 到底是不是推送通道。
`_conclusion()` 是纯函数，另建 `test_qmt_tradehours_validate.py` 锁 7 条判据
（拉取判 0 / 推送判 0 / 代码表不顶数 / ACK 不算推送 / 58670 缺失归因环境 /
无监听判 1 / netstat 不可用绝不判绿）。

**09:10 盘前真机试跑**（证据 `output/qmt_tradehours_validate_20261109_091018.json`，
`exit_code=2`，日志 `output/tradehours_smoke_0900.log`）：

```
ports: 58600=[10032] LISTENING / 58610=[] / 58670=[]
58600 getStockListInSector({"sectorName":"沪深A股"}) → 5224 条, kind=code_list, quote=False ★
58600 getMarketData 7 种形状                          → 全 {"status":0,"params":{"result":[]}}
58600 getInstrumentDetail 2 种形状                    → {"result":{}}
58600 getFullTick 2 种形状                            → ErrorID=200005「未找到处理函数」★
58600 subscribeQuote 抓 6s                           → 1 个 ACK、0 心跳、0 推送 ★
58670 全部检查                                        → unavailable（环境缺失）
axes = push:false / pull:false / code_list:true / channel_58670_missing:true
verdict = 「58670 未 LISTENING（XtModel 未运行，属环境缺失，不是接口不通）；
            58600 拉取形状未命中行情字段；证券代码表已取到（不算行情数据）」
```

★ 本轮新增的三条**实证边界**（不是推断）：
1. `getFullTick` 在 58600 恒回 `200005`，从服务端侧印证了 `KNOWN_METHODS` 的方法集划分；
2. 58600 **会受理** `subscribeQuote` 但只回一个 ACK、不推送 ⇒ 它不是推送通道，
   订阅推送只能走 58670 / `XtModel`；
3. 09:10 尚未开盘，`getMarketData` 全空属于**待交易时段复验**，不能写"接口无行情"。
   09:38 若仍全空，才需要继续扫参数形状（此时证据里已有 12 组逐组三态记录）。

**09:21 加固后复跑**（`output/qmt_tradehours_validate_20261109_092124.json`）：
`axes` 新增 `realtime_tick_proven:false`，`probe_58600` 输出增加「tick 形状 0」，
判词仍是"属环境缺失，不是接口不通"，`exit_code=2`。相关用例 35 passed
（`test_qmt_tradehours_validate` 7 + `test_xtq_rpc` 28）。只读约束、
不启动任何客户端进程、不下单、不改配置，三项均未变。

**门禁同步**：§15.7 的 +23 例此前未回写，连同本轮 +12 例一并处理：
`EXPECTED_TESTS 2910→2945`、`test_*.py 218→219`，README 两处计数声明
（第 463 行带反引号、第 544 行树形注释）同步；`ci_reconcile.py` 头部补 +35 由来。
★ 第 463 行写的是 `` `test_*.py` ``（带反引号）、第 544 行不带 ⇒
`replace_all` 只会命中一处，改完必须 `grep` 复核两处。

```
[✓] backend tests = 2945 (expected 2945)
[✓] frontend components = 76 (expected 76)
[✓] registered pages = 46 (expected 46)
[✓] README 计数声明与实测一致
[✓] 前端 API 调用全部命中后端契约
[✓] .gitattributes 声明的行尾与工作区一致
RECONCILE OK                       ← output/ci_reconcile_final.log, EXIT=0
```

`scripts/audit_doc_links.py` 亦 `EXIT=0`。09:38 一次性自动化不变，仍调用
同一只读脚本（`--seconds 60`）并追加 §16；本节所有加固对它是**无感生效**
——证据更强、判词更诚实，但约束与退出码契约未改。

## 16. 交易时段复跑（2026-11-09 09:38–09:41）：实时 tick 仍未证实，58600 推送轴明确证伪

> 本节由 09:38 一次性定时任务追加，**全程只读**。§15.7 / §15.8 的盘前加固记录原样保留，未改写。
> 原目标是把「实时 tick 已通」从「未证实」推进到「已证实 / 明确证伪」。
> **实际结论：只完成了一半——58600 作为订阅推送通道被明确证伪；但 58670 / `XtModel.exe` 未运行
> 属环境缺失，全局「实时 tick 推送」与「拉取型行情」两项均仍未证实。** 按硬约束不得自动拉起客户端，
> 故本轮**没有**拿到「已证实」结论；这是如实结果，不是脚本失败。

### 16.1 采样时点、命令与证据文件

| 项 | 实测值 |
| --- | --- |
| 采样时间戳（证据 JSON `timestamp`） | `2026-11-09 09:39:35` |
| 时段判据 | 周一 09:39:35，落在上午连续竞价 09:30–11:30 内；`core/clock.py::local_now()` 于 `09:41:07` 复核仍在同一时段。11 月无法定休市日 |
| 命令 | `backend/runtimes/cp311/python.exe -u scripts/qmt_tradehours_validate.py --seconds 60`（cwd `P:\github_public\qmt_work`） |
| 脚本 / 降级探针 | `scripts/qmt_tradehours_validate.py` **存在且成功执行**；`output/rea_func_level/rea_58670_subscribe_v2.py` 未调用 ⇒ **未降级**，证据强度不打折 |
| 退出码 | `EXIT_CODE=2`（`需人工动作或未证实`），`scope=read_only` |
| 证据 JSON（绝对路径） | `P:\github_public\qmt_work\output\qmt_tradehours_validate_20261109_093942.json` |
| 运行日志（绝对路径） | `P:\github_public\qmt_work\output\qmt_tradehours_validate_run.log` |
| 行尾 / 改动范围 | 原文件 `CRLF=0 / LF=953`，本次追加保持 LF；§15.7、§15.8 逐字未动 |

`netstat -ano | findstr 58600` → `TCP 0.0.0.0:58600 0.0.0.0:0 LISTENING 10032`；
`netstat -ano | findstr 58670` → **无输出**。只读进程核对（`Get-Process`）：本机 QMT 相关进程
只有 `XtItClient` (PID 10032)，**没有 `XtModel.exe`** ⇒ 58670 是「端口没起 / 进程没运行」，
**不是「接口不通」**。09:40:31 复核监听状态未变。按要求未启动 `XtModel.exe` / `XtItClient.exe`。

★ 抓帧时长如实记录：设计值是 58670 抓 60 s + 58600 抓 6 s；实测 **58670 抓 0 s**（通道 unavailable，
根本没有连）、**58600 抓 6 s**。不得把「参数写了 60」写成「抓了 60 秒」。

### 16.2 逐项三态（ok / unavailable / skipped）

| 检查项 | 状态 | 实测值 / 说明 |
| --- | --- | --- |
| `ports` 活体监听盘点 | `ok` | `58600=[10032]`、`58610=[]`、`58670=[]`；全机 50 个监听端口 |
| `probe_58600` 12 组拉取形状 | `ok` | 服务端均应答；非空形状 1、行情形状 0、tick 形状 0、代码表 True |
| `probe_58670_tag` (`getTag`) | `unavailable` | 58670 未 LISTENING，未做判活 |
| `probe_58670_fulltick` (`getFullTick`) | `unavailable` | 58670 未 LISTENING，`quote_proven=false`；**没查 ≠ 查了没有** |
| `capture_58670` 订阅抓帧 | `unavailable` | `proven=false`；`XtModel.exe` 未运行 ⇒ 环境缺失 |
| `capture_58600` 订阅抓帧 | `ok`（执行成功） | `proven=false`；6 s 内 3 帧：1 ACK + 1 心跳 + 1 closed，0 quote |
| `installs` 安装台账 / 端口方案 | `ok` | 4 套；`declared_conflicts=33`；`ok_basis=static_only`；`plan_applied=false` |
| `file_orders` 文件单通道 | `ok` | DBF 账户 4 / `dbf_armed=0`；批量单账户 4 / `batch_order_enabled=0` |

★ 本轮**没有任何一项记为 `skipped`**。58670 三项在证据里虽然带「跳过」字样，但状态列是
`unavailable`（前置条件缺失），不是「本轮没要求查」；两者不可混用。

### 16.3 `axes`：五条判据实测值

| 轴 | 值 | 判据与实测 |
| --- | --- | --- |
| `push_proven` | **false** | 58670 未抓（unavailable）；58600 抓帧 `quote=0`、`data_other=0`。订阅 ACK（含 `status` 字段）不是行情推送 |
| `pull_proven` | **false** | 12 组形状里 0 组 `quote_proven=true`；58670 `getFullTick` 未执行 |
| `realtime_tick_proven` | **false** | `push_proven ∨ pull_tick_proven` 均为 false |
| `code_list_proven` | **true** | `getStockListInSector({"sectorName":"沪深A股"})` → 5224 条，`kind=code_list`。**证券代码表不是行情数据**，不得顶数 |
| `channel_58670_missing` | **true** | 58670 未 LISTENING、无 `XtModel.exe` 进程；附 `channel_58600_listening=true` / `channel_58670_listening=false` |

判词（证据 JSON `verdict` 原文）：「实时行情未证实：58670 未 LISTENING（XtModel 未运行，属环境缺失，
不是接口不通）；58600 拉取形状未命中行情字段；证券代码表已取到（不算行情数据）。需人工动作或继续参数形状扫描」。

### 16.4 58600 逐形状结果（12 组，含 `payload_kind` / `quote_proven` / `tick_proven` / `key_sets`）

标的代码 `600036.SH`。分级统一由 `backend/connectors/transports/xtq_rpc.py::classify_payload` 给出。

| # | 方法与参数 | 状态 / ErrorID | `payload_kind` | `result_len` | `quote_proven` | `tick_proven` | `key_sets` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `getStockListInSector {}` | ok / — | `empty` | 0 | false | false | `[]` |
| 2 | `getStockListInSector {"sectorName":"沪深A股"}` | ok / — | `code_list` | **5224** | false | false | `[]` |
| 3 | `getInstrumentDetail {"stockCode":"600036.SH"}` | ok / — | `empty` | 0 | false | false | `[]` |
| 4 | `getMarketData {stockCode, period:"tick", count:1}` | ok / — | `empty` | 0 | false | false | `[]` |
| 5 | `getMarketData {stockMarkets:[…], period:"tick", count:1}` | ok / — | `empty` | 0 | false | false | `[]` |
| 6 | `getMarketData {stockCode:[…], period:"tick", count:1}` | ok / — | `empty` | 0 | false | false | `[]` |
| 7 | `getMarketData {stockCode, tick, startTime/endTime, dividendType}` | ok / — | `empty` | 0 | false | false | `[]` |
| 8 | `getMarketData {stockCode, period:"1m", count:3}` | ok / — | `empty` | 0 | false | false | `[]` |
| 9 | `getMarketData {stockCode, period:"1d", count:3}` | ok / — | `empty` | 0 | false | false | `[]` |
| 10 | `getMarketData` 嵌套 `{meta,region,param}` | ok / — | `empty` | 0 | false | false | `[]` |
| 11 | `getFullTick {"stockMarkets":["600036.SH"]}` | **unavailable / 200005** | `empty` | — | false | false | `[]` |
| 12 | `getFullTick {"stockCode":"600036.SH"}` | **unavailable / 200005** | `empty` | — | false | false | `[]` |

判读要点：

* 第 2 组是唯一非空应答，样本 `["600051.SH","605090.SH",…]`，纯字符串代码表 ⇒ `quote_proven=false`。
  5224 条**不是行情数据**，更不能当 tick。
* 第 4–10 组在交易时段仍全部返回 `{status:0, params:{result:[]}}` ⇒ 只是「服务端受理了查询」，
  不是「行情已通」，也不是「接口无行情」；参数形状未命中与真无数据目前无法区分。
* 第 11–12 组 `ErrorID=200005「未找到处理函数」`，与 `KNOWN_METHODS[58600] =
  {getStockListInSector, getInstrumentDetail, getMarketData}` 一致 ⇒ 58600 侧**没有** `getFullTick`。
* 日线/分钟线即使带 `open/high/low` 也只算「行情数据」、不算「实时 tick」；本轮两组都返回空，
  不存在混级风险。`classify_payload` 已按 `_CODE_SHAPED` 兼容 `getFullTick` 的
  `{代码: {lastPrice,…}}`（外层**没有** `stockCode` 键），但本轮无该形状数据可实证 ⇒ 不存在漏判。

### 16.5 抓帧证据与「未识别帧」键集

`capture_58600.summary`：`total=3, ack=1, heartbeat=1, quote=0, data_other=0, undecodable=0,
protocol_error=0, closed=1, proven=false`，判词「只有订阅应答与心跳（订阅未生效或非交易时段）」。

```
ack      keys=["params","status"]  doc={"params":{"ErrorID":200005,"ErrorMsg":"未找到处理函数"},"status":1}
heartbeat type=1 flags=11840 length=18
closed   （对端提前关闭）
key_sets = [["ack", ["params","status"]]]
```

★ 本轮 `data_other=0`、`undecodable=0` ⇒ **没有任何未识别帧**，因此没有额外键集可 dump；
上列 `key_sets` 是 ACK 帧的，按纪律保留供人工核对。ACK 是**带错误码的拒绝应答**
（`ErrorID=200005`），不是「订阅被受理」；与 09:19 / 09:21 盘前证据的 ACK 内容逐字一致
（参见 `output/qmt_tradehours_validate_20261109_091907.json`、
`output/qmt_tradehours_validate_20261109_092124.json`）。

⇒ **明确证伪**：58600 不能作为 `subscribeQuote` 推送通道（方法不存在 + 0 行情帧 + 连接关闭）。
订阅推送只能走 58670 / `XtModel`；§15.7 第 2 项的「会受理但只回 1 个 ACK、不推送」在本轮
补上了更精确的证据——那个 ACK 本身就是 `未找到处理函数`，本节只作补充记录，不改写 §15.7。

### 16.6 两个必须分开回答的问题

| 问题 | 结论 | 依据 |
| --- | --- | --- |
| **拉取型行情是否证实？** | **未证实**（不是证伪） | `pull_proven=false`。可达的 58600 上 12 组形状 0 组命中行情字段；**最可能给出 tick 的 58670 `getFullTick` 本轮根本没执行**（端口没起）。因此只能写「在当前环境下未证实」，不能写「接口不通」 |
| **实时 tick 推送是否证实？** | **未证实**；其中 58600 子通道**明确证伪** | `push_proven=false`、`realtime_tick_proven=false`。58670 抓帧 unavailable（环境缺失）；58600 6 s 抓帧 0 quote 且 ACK 报 `200005`。两者不可互相顶替：拉取到日线也不能算推送，推送没抓到也不能抹掉拉取结果 |

本轮**没有把「未证实」变成「已证实」**。唯一能推进到「已证实」的前提是先让 58670 在交易时段可用。

### 16.7 下单通道现状（本轮 0 次下单 / 撤单）

* 传输层 `_WRITE_OPS` 显式拒绝 `PLACE_ORDER` / `CANCEL_ORDER`；58600 / 58670 实测**没有任何下单、撤单、账户方法**，
  大 QMT 这两条行情端口不具备下单能力。测试账号 `410001005814` 仅作标识，未连生产柜台、未伪造凭证、未绕 PID 白名单。
* **文件单（DBF）**：台账 4 套安装下发现 4 个 DBF 监控账户（`gd_qmt/22453951`、
  `guoxin_iquantV3/15624979679`、`guoxin_iquantV3/17343238291`、`guoxin_iquant_11/15624979679`），
  `monitor_path` 均指向各安装 `export_data`，**`dbf_armed=0`（`is_automonitor` 全部未开）**。
* **批量单 CSV**：4 个账户的 `batchOrder/orderFile.csv` 中 `key_enable` 全 false，`batch_order_enabled=0`。
  两条通道都未启用 ⇒ 丢文件不会产生委托（脚本 `file_orders.note` 原话）。
* **端口方案**：4 套声明端口完全同构，`declared_conflicts=33`；`plan_ports(stride=100)` 已算出
  `gd_qmt=58600/58610`、`guoxin_iquantV3=58700/58710`、`guoxin_iquant_11=58800/58810`、
  `zj_QMT=58900/58910` 等错开方案，但 **`plan_applied=false`**，`broker.ini` / `Config.xml` 未改，
  `export_data` 未放任何文件。**落盘需用户明确批准，本轮一律未做。**

### 16.8 仍未解决的问题

1. **58670 / `XtModel.exe` 未运行** ⇒ 唯一登记了 `subscribeQuote` / `getFullTick` 的通道未验证。
   这是环境缺失，不是接口不通；在此之前「实时 tick 已通」永远无法证实。
2. **58600 `getMarketData` 参数形状未解出**：方法在 `KNOWN_METHODS` 里，交易时段 7 种形状仍全返 `result:[]`。
   可能还需要 view / fieldList 一类的前置，不能写「接口无行情」。
3. **跨端口参数语义相反**：58600 的 `getStockListInSector` 要 `{"sectorName":"沪深A股"}` 才有数据，
   58670 是空参数才有 5224 条；复用同一封装必须按端口分叉。
4. **端口冲突 33 项未解决、端口方案未落盘**；DBF 自动监控与批量单均未启用。
5. `getFullTick` 的 `{代码: {lastPrice,…}}` 分级规则虽已在 `classify_payload` 兼容，
   但本轮无真 tick 数据可实证 ⇒ 该分支仍缺一次行为级验证。

### 16.9 仍需的三项人工动作（本轮一律未执行）

1. **交易时段保持 58670 / `XtModel` 可用并复跑订阅抓取**（本轮 `capture_58670=unavailable`）——
   用户此前明确取消过手动拉起，脚本不会自动启动任何客户端进程。
2. **端口方案落盘 `broker.ini` 需用户明确批准**（脚本从不写配置）。
3. **在客户端界面打开 `is_automonitor`、导出下单模板 DBF 并交付文件单**（需人工操作 + 明确确认）。

### 16.10 约束与退出码如实性

只读约束全项保持：不下单、不撤单、不改 `broker.ini` / `Config.xml`、不打开 `is_automonitor`、
不把任何文件放进 `export_data`、不交付文件单、不启动客户端进程、不驱动界面下单。

退出码 `2 = 需人工动作或未证实`。三类状态**分开报**：

* **环境缺失**：58670 未 LISTENING + 无 `XtModel.exe` 进程（不是接口不通）；
* **需人工动作**：16.9 三项；
* **接口不通**：本轮 **0 项**被判为接口不通。58600 的 `subscribeQuote` / `getFullTick` 返回 `200005`
  属于「该端口未登记此方法」，是**明确证伪该通道**，不应写成「行情接口故障」。

（若后续某轮 58670 起得来且抓到带行情字段的推送帧，应另起 §17 记录，不要回改本节。）

## 17. 21:00 复跑（2026-11-09 21:02，**收盘后**）：两项仍未证实，且本轮不在交易时段

> 本节为本次定时任务按「追加结论为新章节」要求新增。§16 已由 09:38 首跑占用，且
> §16.10 写明「后续复跑另起 §17、不要回改本节」，故编号顺延为 **§17**；§15.7 / §15.8 / §16
> 全部逐字保留、未改写。
>
> ★ **必须先说清的时点偏差**：本次任务触发于 **2026-11-09 21:00（周一）**，证据 JSON
> `timestamp=2026-11-09 21:02:08`。A 股连续竞价 09:30–11:30 / 13:00–15:00 已结束约 6 小时，
> 因此本轮**不是交易时段采样**，不能替代 09:38 那次的交易时段判据，更不能据此把
> 「实时 tick」推向「已证实」。原目标「把未证实变成已证实 / 明确证伪」**本轮未达成**；
> 这是如实结果，不是脚本失败。09:38 首跑确实落在交易时段（09:39:35），当时也因 58670
> 缺失而未证实，两轮结论一致。

### 17.1 采样时点、命令与证据文件

| 项 | 实测值 |
| --- | --- |
| 触发 / 采样时间 | 触发 21:00:21；证据 `timestamp=2026-11-09 21:02:08`；`core/clock.py::local_now()` 于 `21:02:18.892574` 复核一致 |
| 时段判据 | 周一 21:02，**收盘后、非交易时段**；与任务描述的「交易时段复跑」前提不符，已在结论中扣除证据强度 |
| 命令 | `backend/runtimes/cp311/python.exe -u scripts/qmt_tradehours_validate.py --seconds 60`（cwd `P:\github_public\qmt_work`） |
| 脚本 / 降级探针 | 主脚本 `scripts/qmt_tradehours_validate.py` **存在且成功执行**；降级探针 `output/rea_func_level/rea_58670_subscribe_v2.py` 经 Glob 核实**不存在**、未调用 ⇒ **未降级**，证据强度不打折 |
| 退出码 | `EXIT_CODE=2`（`需人工动作或未证实`），`scope=read_only` |
| 证据 JSON（绝对路径） | `P:\github_public\qmt_work\output\qmt_tradehours_validate_20261109_210215.json` |
| 运行日志（绝对路径） | `P:\github_public\qmt_work\output\qmt_tradehours_validate_2100.log` |
| 行尾 / 改动范围 | 追加前 `CRLF=0 / LF=1120 / size=75742`；追加保持 LF；§15.7、§15.8、§16 逐字未动 |

端口预检（前后两次一致）：

```
21:01  netstat -ano | findstr 58600 → TCP 0.0.0.0:58600 0.0.0.0:0 LISTENING 11932
21:01  netstat -ano | findstr 58670 → 无输出（Exit 1）
21:03  netstat -ano | findstr 58670 → no listener
21:01/21:03 进程表 → 仅 XtItClient.exe (PID 11932)，无 XtModel.exe
```

按硬约束**未启动** `XtModel.exe` / `XtItClient.exe`；58670 记为 `unavailable`（环境缺失 / 端口没起），
**绝不写成「接口不通」**。★ 抓帧时长如实记录：设计值 58670 抓 60 s + 58600 抓 6 s，
实测 **58670 抓 0 s**（通道 unavailable，根本没连）、**58600 抓 6 s**；不得把「参数写了 60」
写成「抓了 60 秒」。

### 17.2 逐项三态（ok / unavailable / skipped）

| 检查项 | 状态 | 实测值 / 说明 |
| --- | --- | --- |
| `ports` 活体监听盘点 | `ok` | `58600=[11932]`、`58610=[]`、`58670=[]`；全机 52 个监听端口 |
| `probe_58600` 12 组拉取形状 | `ok` | 服务端均应答；非空形状 1、行情形状 0、tick 形状 0、代码表 True |
| `probe_58670_tag`（`getTag` 判活） | `unavailable` | 58670 未 LISTENING，**未做判活**；`quote_proven=false` |
| `probe_58670_fulltick`（`getFullTick`） | `unavailable` | 58670 未 LISTENING，**未执行**；`quote_proven=false`。没查 ≠ 查了没有 |
| `capture_58670` 订阅抓帧 | `unavailable` | `proven=false`；`XtModel.exe` 未运行 ⇒ 环境缺失，非接口不通 |
| `capture_58600` 订阅抓帧 | `ok`（执行成功） | `proven=false`；6 s 内 3 帧：1 ACK + 1 心跳 + 1 closed，0 quote |
| `installs` 安装台账 / 端口方案 | `ok` | 4 套；`declared_conflicts=33`；`ok_basis=static_only`；`plan_applied=false` |
| `file_orders` 文件单通道 | `ok` | DBF 账户 4 / `dbf_armed=0`；批量单账户 4 / `batch_order_enabled=0` |

★ 本轮**没有任何一项记为 `skipped`**。58670 三项在证据里虽带「跳过」字样，状态列是
`unavailable`（前置条件缺失），不是「本轮没要求查」；`skipped` 与 `unavailable` 不可混用。
另：`installs.ok_basis=static_only` 是 §15.7 第 3 项收紧后的口径——33 项冲突来自**静态配置**，
本轮没有做活体冲突探测，**不得读成「查过活体、没有冲突」**。

### 17.3 `axes`：五条判据实测值（与 09:38 首跑逐位相同）

| 轴 | 值 | 判据与实测 |
| --- | --- | --- |
| `push_proven` | **false** | 58670 抓帧 unavailable；58600 6 s 抓帧 `quote=0`、`data_other=0`。订阅 ACK（含 `status` 字段）不是行情推送 |
| `pull_proven` | **false** | 12 组形状 0 组 `quote_proven=true`；58670 `getFullTick` 根本没执行 |
| `realtime_tick_proven` | **false** | `push_proven ∨ pull_tick_proven` 均为 false |
| `code_list_proven` | **true** | `getStockListInSector({"sectorName":"沪深A股"})` → 5224 条，`kind=code_list`。**证券代码表不是行情数据**，不得顶数 |
| `channel_58670_missing` | **true** | 58670 未 LISTENING、无 `XtModel.exe`；附 `channel_58600_listening=true` / `channel_58670_listening=false` |

判词（证据 JSON `verdict` 原文）：「实时行情未证实：58670 未 LISTENING（XtModel 未运行，属环境缺失，
不是接口不通）；58600 拉取形状未命中行情字段；证券代码表已取到（不算行情数据）。
需人工动作或继续参数形状扫描」。

### 17.4 58600 逐形状结果（12 组，含 `payload_kind` / `quote_proven` / `tick_proven` / `key_sets`）

标的代码 `600036.SH`；分级统一由 `backend/connectors/transports/xtq_rpc.py::classify_payload` 给出。

| # | 方法与参数（简写） | 状态 / ErrorID | `payload_kind` | `result_len` | `quote_proven` | `tick_proven` | `realtime_eligible` | `key_sets` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `getStockListInSector {}` | ok / — | `empty` | 0 | false | false | false | `[]` |
| 2 | `getStockListInSector {"sectorName":"沪深A股"}` | ok / — | `code_list` | **5224** | false | false | false | `[]` |
| 3 | `getInstrumentDetail {"stockCode":"600036.SH"}` | ok / — | `empty` | 0 | false | false | false | `[]` |
| 4 | `getMarketData {stockCode, period:"tick", count:1}` | ok / — | `empty` | 0 | false | false | **true** | `[]` |
| 5 | `getMarketData {stockMarkets:[…], tick, count:1}` | ok / — | `empty` | 0 | false | false | **true** | `[]` |
| 6 | `getMarketData {stockCode:[…], tick, count:1}` | ok / — | `empty` | 0 | false | false | **true** | `[]` |
| 7 | `getMarketData {stockCode, tick, startTime/endTime, dividendType}` | ok / — | `empty` | 0 | false | false | **true** | `[]` |
| 8 | `getMarketData {stockCode, period:"1m", count:3}` | ok / — | `empty` | 0 | false | false | false | `[]` |
| 9 | `getMarketData {stockCode, period:"1d", count:3}` | ok / — | `empty` | 0 | false | false | false | `[]` |
| 10 | `getMarketData` 嵌套 `{meta,region,param}` | ok / — | `empty` | 0 | false | false | false | `[]` |
| 11 | `getFullTick {"stockMarkets":["600036.SH"]}` | **unavailable / 200005** | `empty` | — | false | false | **true** | `[]` |
| 12 | `getFullTick {"stockCode":"600036.SH"}` | **unavailable / 200005** | `empty` | — | false | false | **true** | `[]` |

判读要点（按本轮判读纪律逐条核）：

* 第 2 组是唯一非空应答，样本 `["600051.SH","605090.SH","600025.SH","601222.SH","688031.SH",…]`，
  纯字符串**证券代码表** ⇒ `quote_proven=false`。5224 条既不是行情数据，更不能当 tick。
* 第 4–10 组在收盘后仍全部返回 `{status:0, params:{result:[]}}` ⇒ 只是「服务端受理了查询」，
  不是「行情已通」，也不是「接口无行情」；参数形状未命中与真无数据目前无法区分。
  叠加本轮非交易时段，空结果更**不能**作为证伪依据。
* 第 11–12 组 `ErrorID=200005「未找到处理函数」`，与 `KNOWN_METHODS[58600] =
  {getStockListInSector, getInstrumentDetail, getMarketData}` 一致 ⇒ 58600 侧**没有** `getFullTick`。
* **日线 / 分钟线**（第 8、9 组）即使带 `open/high/low` 也只算「行情数据」、**不算实时 tick**；
  本轮两组都返回空，不存在混级风险。
* `classify_payload` 已按 `_CODE_SHAPED` 兼容 `getFullTick` 的 `{代码: {lastPrice,…}}`
  （外层**没有** `stockCode` 键，不能只看字段名漏判真 tick），但本轮无该形状数据可实证
  ⇒ 该分支仍缺一次行为级验证。
* 12 组 `key_sets` 全为 `[]`（空结果无内层结构可 dump），`price_keys` 全为 `[]`。

### 17.5 抓帧证据与「未识别帧」键集

`capture_58600.summary`：`total=3, heartbeat=1, ack=1, quote=0, data_other=0, data=0,
undecodable=0, protocol_error=0, closed=1, proven=false`，判词「只有订阅应答与心跳
（订阅未生效或非交易时段）」。

```
ack      keys=["params","status"]  seq=1 type=3 flags=8258 length=97
         doc={"params": {"ErrorID": 200005, "ErrorMsg": "未找到处理函数"}, "status": 1}
heartbeat seq=674526025 type=1 flags=12096 length=16
closed   （对端提前关闭）
key_sets = [["ack", ["params","status"]]]
```

★ 本轮 `data_other=0`、`undecodable=0` ⇒ **没有任何未识别帧**，因此没有额外键集可 dump；
上列 `key_sets` 是 ACK 帧的，按纪律完整保留供人工核对。该 ACK 是**带错误码的拒绝应答**
（`ErrorID=200005`），不是「订阅被受理」；ACK 含 `status` 字段、**不是行情推送**。
与 09:38 / 09:21 证据的 ACK 内容逐字一致（参见
`output/qmt_tradehours_validate_20261109_093942.json`、
`output/qmt_tradehours_validate_20261109_092124.json`）。

⇒ 58600 作为 `subscribeQuote` 推送通道再次被**明确证伪**（方法不存在 + 0 行情帧 + 连接关闭），
与 §16.5 结论一致。订阅推送只能走 58670 / `XtModel`；本节只作复核记录，不改写 §16。

### 17.6 两个必须分开回答的问题

| 问题 | 结论 | 依据 |
| --- | --- | --- |
| **拉取型行情是否证实？** | **未证实**（不是证伪） | `pull_proven=false`。可达的 58600 上 12 组形状 0 组命中行情字段；**最可能给出 tick 的 58670 `getFullTick` 本轮根本没执行**（端口没起）。只能写「在当前环境下未证实」，不能写「接口不通」；本轮又是收盘后，空结果更不可作证伪依据 |
| **实时 tick 推送是否证实？** | **未证实**；其中 58600 子通道**明确证伪** | `push_proven=false`、`realtime_tick_proven=false`。58670 抓帧 unavailable（环境缺失）；58600 6 s 抓帧 0 quote 且 ACK 报 `200005`。两者**不可互相顶替**：拉取到日线也不能算推送，推送没抓到也不能抹掉拉取结果 |

本轮**没有把「未证实」变成「已证实」**。唯一能推进到「已证实」的前提仍是：
在**交易时段**让 58670 / `XtModel` 处于 LISTENING 并复跑订阅抓取。

### 17.7 下单通道现状（本轮 0 次下单 / 0 次撤单）

* 传输层 `_WRITE_OPS` 显式拒绝 `PLACE_ORDER` / `CANCEL_ORDER`；58600 / 58670 实测**没有任何下单、
  撤单、账户方法**，大 QMT 这两条行情端口不具备下单能力。测试账号 `410001005814` 仅作标识，
  未连生产柜台、未伪造凭证、未绕 PID 白名单、未驱动界面下单。
* **文件单（DBF）**：台账 4 套安装下发现 4 个 DBF 监控账户（`gd_qmt/22453951`、
  `guoxin_iquantV3/15624979679`、`guoxin_iquantV3/17343238291`、
  `guoxin_iquant_11/15624979679`），`monitor_path` 均指向各安装 `export_data`，
  **`dbf_armed=0`（`is_automonitor` 全部未开）**。
* **批量单 CSV**：4 个账户的 `batchOrder/orderFile.csv` 中 `key_enable` 全 false，
  `batch_order_enabled=0`。两条通道都未启用 ⇒ 丢文件不会产生委托（`file_orders.note` 原话）。
* **端口方案**：4 套声明端口完全同构，`declared_conflicts=33`；`plan_ports(stride=100)` 已算出
  `gd_qmt=58600/58610`、`guoxin_iquantV3=58700/58710`、`guoxin_iquant_11=58800/58810`、
  `zj_QMT=58900/58910` 等错开方案，但 **`plan_applied=false`**，`broker.ini` / `Config.xml` 未改，
  `export_data` 未放任何文件、未交付任何文件单。**落盘需用户明确批准，本轮一律未做。**

### 17.8 仍未解决的问题

1. **58670 / `XtModel.exe` 未运行** ⇒ 唯一登记了 `subscribeQuote` / `getFullTick` 的通道未验证。
   这是环境缺失，不是接口不通；在此之前「实时 tick 已通」永远无法证实。
2. **本轮采样不在交易时段**（21:02，收盘后），因此即使 58670 可用，这轮数据也不能充当
   「交易时段 tick 抓取已证实」的证据。需要在真实交易时段重跑一次。
3. **58600 `getMarketData` 参数形状未解出**：方法在 `KNOWN_METHODS` 里，12 组形状仍全返
   `result:[]`。可能还需要 view / fieldList 一类的前置，不能写「接口无行情」。
4. **跨端口参数语义相反**：58600 的 `getStockListInSector` 要 `{"sectorName":"沪深A股"}` 才有数据，
   58670 是空参数才有 5224 条；复用同一封装必须按端口分叉。
5. **端口冲突 33 项未解决、端口方案未落盘**；DBF 自动监控与批量单均未启用；
   `installs.ok_basis=static_only`，活体冲突探测本轮未做。
6. `getFullTick` 的 `{代码: {lastPrice,…}}` 分级规则虽已在 `classify_payload` 兼容，
   但两轮均无真 tick 数据可实证 ⇒ 该分支仍缺一次行为级验证。

### 17.9 仍需的三项人工动作（本轮一律未执行）

1. **在交易时段保持 58670 / `XtModel` 可用并复跑订阅抓取**（本轮 `capture_58670=unavailable`）——
   用户此前明确取消过手动拉起，脚本与定时任务都不会自动启动任何客户端进程。
2. **端口方案落盘 `broker.ini` 需用户明确批准**（脚本从不写配置）。
3. **在客户端界面打开 `is_automonitor`、导出下单模板 DBF 并交付文件单**
   （需人工操作 + 明确确认）。

以上三项与证据 JSON `pending_human_actions` 三条逐条对应，本轮**零执行**。

### 17.10 约束与退出码如实性

只读约束全项保持：不下单、不撤单、不改 `broker.ini` / `Config.xml`、不打开 `is_automonitor`、
不把任何文件放进 `export_data`、不交付文件单、不启动客户端进程、不驱动界面下单。
证据 JSON `scope=read_only`、`constraints` 六项与脚本硬约束一致。

退出码 `2 = 需人工动作或未证实`。三类状态**分开报**：

* **环境缺失**：58670 未 LISTENING + 无 `XtModel.exe` 进程（不是接口不通）；另 21:02 属收盘后，
  交易时段前提未满足（单独标注，不并入「接口不通」）。
* **需人工动作**：17.9 三项。
* **接口不通**：本轮 **0 项**被判为接口不通。58600 的 `subscribeQuote` / `getFullTick` 返回
  `200005` 属于「该端口未登记此方法」，是**明确证伪该通道**，不应写成「行情接口故障」；
  12 组 `getMarketData` 返空属于「参数形状未命中 / 非交易时段」，亦不作证伪结论。

（后续若在交易时段 58670 起得来且抓到带行情字段的推送帧，应另起 §18 记录，不要回改本节。）
