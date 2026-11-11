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

### 16.11 授权升级后的现场复核（2026-11-10 08:2x，人工轮）

> 用户本轮明确授权：可拉起 `XtModel`、可对 `guoxin_iquant_11` 与 `gd_qmt` 的
> `XtItClient.exe` 做相关操作、可自动执行分析、可做下单测试与快速撤单。
> 本节记录**授权后实际能做到什么**——授权解除的是「允许尝试」，不是「物理前置」。
> 全程未改任何配置、未结束任何客户端进程、未向真实柜台发单。

#### 16.11.1 现场实测（08:2x）

| 项 | 实测值 | 判读 |
| --- | --- | --- |
| 后端服务 | `127.0.0.1:21118` health 200，v0.5.4 | 由本轮启动；`Application startup complete` |
| 58600 | `LISTENING`，**两个 PID 10796 / 15072**（均 `XtItClient`） | 多套安装同绑一端口，探针连到哪个实例不确定 |
| 58670 | **未监听** | `XtModel` 启动即退出 |
| 文件桥 broker `90c700f265d6` | `connected=true, active=true` | agent 在跑，桥可用 |
| `23019cce2bda`（gd_qmt / 22453951） | 未连 | 启动期 32s 硬超时，后台重连仍 failed |
| `dd1822009f64`（guoxin / 410001005814） | 未连 | `connect rc=-1`（6 个 session 全试），提示**未开通程序化交易权限** |
| `agent_status.json` | `alive=true`、`runtime_mode=standalone_process`、`injected=[]`、`trading_enabled=false` | **无 passorder ⇒ 真实下单/撤单不可用** |
| 信号模式 | `paper` | 下单进本地账本，零真实资金 |

#### 16.11.2 阻塞链（四层，逐层独立）

1. **策略以独立进程运行**：QMT 日志 `[TC::CTradeStrategyData::doRun] execude cmd: -u
   "P:\stock\gd_qmt\python\QMT_WORK_AGENT.py" "P:\stock\gd_qmt\userdata"` ⇒ 脚本走
   `__main__` → `run_standalone()` → `standalone=True` ⇒ `injected=[]`。
   解法只有 runbook Phase 2：在 QMT GUI 内把策略改为公式模式（注册树 `configFormula`
   加密，**不可自动化**）。
2. **账户不可用**：`gd_qmt` 账户 22453951 的 `accountdetail` 恒为
   `balance 0.0 / available 0.0 / marketvalue 0.0 / status 0`，`tradeday 20261109`。
3. **行情账号过期**：`XtModel` 日志对所有行情服务器返回
   `resp:{ result: 4, reason: "account is expired" }`，进程启动后自行退出 ⇒ 58670 起不来。
4. **程序化交易权限**：国信 410001005814 直连 `rc=-1`，官方四步排查指向
   「资金账号未开通 QMT 程序化交易/策略交易权限」。

#### 16.11.3 行情三源实测：全部不可用

| 源 | 结果 |
| --- | --- |
| `/market/quote?source=broker` | 503「券商连接异常或未连接」 |
| `/market/quote?source=tdx` | 503「TDX 行情源不可用」（网络不可达） |
| `/market/quote`（auto） | 503（两源皆失败） |
| `/market/ticks` | 503「本地 TDX 行情源未就绪」 |
| `/market/kline` | 200，但 `source=cache_stale`、`stale=true`，缓存止于 **2025-04** |

⇒ **实时行情不可用**；陈旧 K 线不得当实时行情。`xtquant` 侧报
「无法连接 xtquant 服务，请检查 QMT-投研版或 QMT-极简版是否开启」。

#### 16.11.4 paper 下单 + 撤单实测

新增 `scripts/qmt_order_smoke.py`（默认 paper；`--live` 需四道前置全绿才放行）。

| 步骤 | 实测 |
| --- | --- |
| health | OK v0.5.4 |
| precheck | `allowed=true, reason=ok` |
| 下单 513090 × 100 @1.500 | OK → `PAPER-35` / `PAPER-36` / `PAPER-37` |
| 委托可见 | `status=filled, filled=100`（**paper 按限价立即全额成交**） |
| 撤单 | 503「该委托号属于「模拟盘」（本地撮合），券商柜台不存在这笔委托」 |
| 判读 | 这是**刻意契约**，不是「撤单失败留挂单」；paper 下 `cancel_applicable=false` |

⇒ **paper 能验证下单链路，验证不了真实撤单**（模拟盘无柜台挂单）。真实撤单必须在
live 且 agent 拿到 passorder 时才可测。

#### 16.11.5 live 护栏实测（拒绝发单，缺口逐条列出）

`--live` 实测 exit 1、**未发任何请求到 `/trade/precheck` 与 `/trade/order`**，缺口：

1. `/signal/mode='paper'`，需先切 live；
2. `agent trading_enabled=false`；
3. `agent injected` 缺 `passorder`（`runtime_mode=standalone_process`）⇒ 需 QMT 内切公式模式。

#### 16.11.6 本轮修掉的缺陷（都是「诊断自己说谎」家族）

| # | 缺陷 | 修法 |
| --- | --- | --- |
| 1 | `_finish` 用 `int(report.get("exit_code") or 1)`，把合法的 `0` 吞成 `1`，把「下单成功」谎报成「环境不满足」 | 显式判 `None`，并加回归锁 |
| 2 | paper 已成交时撤单 503 被报成 FAIL「可能留有挂单」，诱导人工去追不存在的挂单 | 判 `cancel_applicable=false`、步骤记 SKIP、verdict 明写「真实撤单未验证」 |
| 3 | 缺 paper/live 语义隔离 | `--live` 四道前置，任一不满足直接 exit 1 且不发单 |

回归锁：`backend/tests/test_qmt_order_smoke.py`（7 例，纯函数 + 假 HTTP，不触真服务）。

#### 16.11.7 仍需的人工动作（本轮一律未做）

1. **QMT GUI 内把 `QMT_WORK_AGENT` 改为公式模式运行**（runbook Phase 2，约 30 秒，
   注册树加密不可自动化）——这是 `passorder` 的唯一来源；
2. **确认账户 22453951 状态**（balance/available 全 0）并**开通程序化交易权限**
   （国信 410001005814 当前 rc=-1）；
3. **行情账号续期**（`account is expired`）或换可用行情源，否则 58670 永远起不来。

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

## 17. 2026-11-10 交易时段自动分析第 1 次（09:42–09:52，共 3 次）

> ★ **编号说明**：本文档已存在 2026-11-09 21:00「收盘后复跑」的 §17（上一系列，
> §17.1–§17.10），本节按本系列任务约定仍编号 §17，两节以**日期/标题**区分，
> 旧节逐字未动。本节子标题统一用 `17.R*` 以免与旧 `17.1–17.10` 混淆；
> 是否重排编号属「待人工处理」，本轮不擅自改动。
> ★ **时点**：全部采样落在 09:30–11:30 交易时段内（09:42:32 首测 / 09:52:19 收尾），
> 与 21:00 那轮「收盘后」不同，证据强度不打折。

### 17.R0 采样时间、命令与证据文件（绝对路径）

| 时刻（+08） | 动作 | 命令 / 端点 |
| --- | --- | --- |
| 09:42:32 | 只读预检 | `netstat -ano \| findstr 58600/58610/58670`；`Get-Process XtItClient,XtModel`；读 `agent_status.json` |
| 09:42:3x | 基线复核 | `curl --noproxy '*' -H 'X-API-Key: qmt-dev-key' .../api/v1/health`、`.../signal/mode`、`.../brokers` |
| 09:43:20 | 授权拉起 | `Start-Process P:\stock\guoxin_iquant_11\xtmodel\XtModel.exe -WorkingDirectory <其所在目录> -PassThru` |
| 09:44:40 / 09:45:47 | 行情三源 | `backend/runtimes/cp311/python.exe -u output/qmt_market_probe_20261110.py` |
| 09:46:19–09:47:11 | 45 秒探针 | `backend/runtimes/cp311/python.exe -u scripts/qmt_tradehours_validate.py --seconds 45` |
| 09:47:34 | 下单/撤单 | `backend/runtimes/cp311/python.exe -u scripts/qmt_order_smoke.py --code 513090 --volume 100 --price 1.500`；再 `--live` |
| 09:48:17 | 形状补扫 | `backend/runtimes/cp311/python.exe -u output/qmt_shape_audit_20261110.py` |
| 09:51:14–09:51:53 | 修复验证 | `py_compile` + 6 文件 pytest + `output/qmt_autofix_falsify_20261110.py` + `scripts/audit_doc_links.py` |
| 09:52:19 | 收尾复核 | 再查端口/进程/日志/agent/mode |

证据文件（全部绝对路径，`output/` 已被 `.gitignore` 忽略）：

* `P:\github_public\qmt_work\output\qmt_xtmodel_launch_20261110_094320.json`（拉起三态 + 日志尾部）
* `P:\github_public\qmt_work\output\qmt_market_probe_20261110_094440.json`（首跑，探针有缺陷，见 17.R7）
* `P:\github_public\qmt_work\output\qmt_market_probe_20261110_094547.json`（**修正后正式证据**）
* `P:\github_public\qmt_work\output\qmt_market_probe_20261110_run.log`
* `P:\github_public\qmt_work\output\qmt_tradehours_validate_20261110_094711.json`（**本轮主证据**）
* `P:\github_public\qmt_work\output\qmt_tradehours_validate_0940.log`、`...\qmt_tradehours_validate_20261110_095125.json`（修复后复跑）
* `P:\github_public\qmt_work\output\qmt_shape_audit_20261110_094817.json`
* `P:\github_public\qmt_work\output\qmt_order_smoke_paper_20261110_0948.json`
* `P:\github_public\qmt_work\output\qmt_order_smoke_live_20261110_0948.json`
* `P:\github_public\qmt_work\output\qmt_order_smoke_live_afterfix2_20261110.json`
* `P:\github_public\qmt_work\output\qmt_autofix_falsify_20261110.json`
* `P:\github_public\qmt_work\output\_autofix_backup_20261110_0940\*.bak`（修复前备份，sha256 已比对）
* 外部只读来源：`P:\stock\guoxin_iquant_11\xtmodel\data\log\xtquantservice_20261110.log`、
  `C:\Users\Administrator\qmt_work\bigqmt_bridge\agent_status.json`

### 17.R1 端口 / 进程 / 后端基线（只读预检，09:42:32）

| 项 | 实测 | 三态 |
| --- | --- | --- |
| 后端 | `127.0.0.1:21118` health **HTTP 200 / code 0 / v0.5.4**，uptime 4713s | ok |
| 58600 | **两个 PID 同时 LISTENING**：`20752`（`P:\stock\gd_qmt\bin.x64\XtItClient.exe`，08:50:37 起）、`22344`（`P:\stock\guoxin_iquant_11\bin.x64\XtItClient.exe`，08:53:21 起） | ok（但探针连到哪个实例不确定，是可重复性变量） |
| 58610 | **无监听**，只有 12+ 条 `SYN_SENT`（PID 2796/19336）⇒ 连接方在重试 | unavailable（通道缺失） |
| 58670 | 预检时无监听、无 `XtModel.exe` | unavailable（**当时**属环境缺失） |
| broker | `90c700f265d6` 文件桥 `connected=true, active=true`；`23019cce2bda`（gd_qmt/22453951）、`dd1822009f64`（guoxin/410001005814）均 `connected=false` | 与 08:2x 基线一致 |
| `/signal/mode` | `{"mode":"paper"}` | ok（**本轮未改**） |
| agent_status | `alive=true`、`runtime_mode=standalone_process`、`injected=[]`、`trading_enabled=false` | ok（状态如实记录，未粉饰） |
| git | 干净；HEAD `6b53e70`；文档 `i/lf w/lf`（CRLF=0，LF=1408） | ok |

两个客户端**只读**：未结束进程、未改 `Config.xml` / `broker.ini` / `agent_config.json` /
`is_automonitor` / 任何下单配置。

### 17.R2 XtModel 拉起结果（授权范围内 1 次，未重启）

| 时刻 | 观测 |
| --- | --- |
| 09:43:20 | `Start-Process` 成功，PID `22152`，工作目录 = `P:\stock\guoxin_iquant_11\xtmodel` |
| 09:43:28（+8s） | 进程**仍存活**、exit code 空；58670 **尚未** LISTENING；日志已出现 `resp:{ result: 4, reason: "account is expired" }` |
| 09:43:36（+16s） | 日志 `[xtdc]start ... portStart:58670` → `IPythonApiServer::createServer listen, finished:true, ip:0.0.0.0, port:58670` → `[try_listen] [xtdc]finished, result:0.0.0.0:58670` |
| 09:43:44 | `netstat`：`TCP 0.0.0.0:58670 ... LISTENING 22152` |
| 09:52:19（收尾） | PID `22152` 仍在、58670 仍 LISTENING，运行约 9 分钟未退出；两个 `XtItClient` 仍在 |

**与 08:2x 基线的差异（重要）**：基线「XtModel 启动后退出 / 58670 未监听」⇒ 属**环境缺失**；
本轮 XtModel 起了且 58670 稳定监听 ⇒ 环境前置**已解除**，但行情仍拿不到，
所以本轮的阻塞类别是 **`quote_channel_no_data`（通道已应答但无行情数据）**，
不是环境缺失、也不是「反复重启能解决」。按授权「同一轮最多重启 2 次」，本轮**只拉起 1 次、零重启**。

日志真实原因（`xtquantservice_20261110.log`，全程只读）：
今日累计 **33 处** `account is expired`，其中 09:43:23–09:43:25 的 31 处 `reason: "account is expired"`
逐台命中行情服务器（203.135.101.34 / 119.147.202.44 / 36.99.48.21 / 42.228.16.211 /
43.242.96.162 等，port 55300），伴随 `accessible:false`、`StockList::init waiting`、
`WholeMarketQuoter::waitForInitFinish wait before send subscribe, timeout`。
⇒ **RPC 服务可监听、可应答，但上游行情服务器登录全部被拒**，这是 58670 无 tick 的直接成因。

### 17.R3 行情三态：`/market/quote` 三源、`/market/ticks`、`/market/kline`

正式证据 `...\qmt_market_probe_20261110_094547.json`（09:45:47）。

| 端点 / 源 | HTTP / code | `source` | 实测 | 三态 |
| --- | --- | --- | --- | --- |
| `/market/quote?source=auto`（600036.SH） | 200 / 0 | **`tencent`** | 有真实价格字段：`last=42.79`、`pre_close=43.01`、`change_pct=-0.51`、`volume=11528300` | **ok（公开源快照）** |
| `/market/quote?source=auto`（513090.SH） | 200 / 0 | `tencent` | `last=1.618`、`change_pct=0.5`、`volume=273481100` | ok（公开源快照） |
| `/market/quote?source=broker` | **503 / 503** | — | `行情获取失败：券商连接异常或未连接` | unavailable |
| `/market/quote?source=tdx` | **503 / 503** | — | `TDX 行情源不可用` | unavailable |
| `/market/ticks?source=auto` | **503 / 503** | — | `本地 TDX 行情源未就绪（未安装 eltdx 或行情服务器不可达）` | unavailable |
| `/market/kline?period=1d&source=auto` | **503 / 503** | — | `get_market_data 失败: 无法连接xtquant服务` | unavailable |
| `/market/kline?period=1m&source=auto` | **503 / 503** | — | 同上 | unavailable |

★ **证据分级（不可合并成一句「行情可用」）**：

* `source=tencent` 是**公开 HTTP 行情源快照**，不是券商 xtquant 通道、不是 58670 推送 tick、
  响应里**没有任何服务端时间戳** ⇒ `quote_snapshot_proven=true`、
  `broker_quote_proven=false`、`realtime_tick_proven=false`、`freshness_proven=false`。
  09:44:40 与 09:45:47 两次采样 `last` 从 42.78→42.79、`volume` 11420100→11528300 有变化，
  只能说明公开源盘中在更新，**不能**据此宣称大 QMT 实时行情已通。
* 基线里 `/market/kline` 曾回 `source=cache_stale`（缓存止于 2025-04）；**本轮连陈旧缓存都没返回**，
  直接 503。两种「有数据」都不存在 ⇒ 本轮没有任何日线 / 陈旧缓存被误当成实时 tick 的机会。
* 因此：**大 QMT 通道实时行情「不可用 / 未证实」**；REST 层只有公开源快照可用，
  两者是不同轴，不得互相顶替。

### 17.R4 45 秒探针：`axes` 与逐形状证据

主证据 `...\qmt_tradehours_validate_20261110_094711.json`（采样起点 09:46:19，`--seconds 45`），
日志 `...\qmt_tradehours_validate_0940.log`，**EXIT=2（需人工动作或未证实）**。

```
ports: {"58600": ["20752","22344"], "58610": [], "58670": ["22152"]}
axes : {"push_proven": false, "pull_proven": false, "realtime_tick_proven": false,
        "code_list_proven": true, "channel_58600_listening": true,
        "channel_58670_listening": true, "channel_58670_missing": false}
verdict: 实时行情未证实：58670 在监听但订阅抓帧未出现行情字段；拉取形状亦未命中；
         证券代码表已取到（不算行情数据）。需人工动作或继续参数形状扫描
```

* `probe_58670_tag`：`getTag` → `{"tag":"xtquant","version":"1.0"}`、`ErrorID=None` ⇒ **RPC 服务活着**（`ok`）。
* `probe_58670_fulltick`：`getFullTick({"stockMarkets":["600036.SH"]})` →
  `{"status":0,"params":{"result":{}}}`、`quote_proven=false`、`payload_kind=empty` ⇒
  **服务端应答了，但结果体为空**（`status=ok` 只代表应答，不代表有数据）。
* `capture_58670`（45s）：`total=11 / heartbeat=10 / ack=1 / quote=0 / data_other=0`，
  ACK 内容 `{status:0, params:{seq:1, error:{ErrorID:0,ErrorMsg:""}}}`；
  verdict「只有订阅应答与心跳（订阅未生效或非交易时段）」⇒ **ACK 没有被误计成推送**。
* `capture_58600`（6s）：`ack=1`（`ErrorID=200005 未找到处理函数`）+ `heartbeat=1` + `closed=1`，
  `quote=0` ⇒ 58600 订阅通道明确**不可用**。
* `probe_58600` 12 组形状：仅 `getStockListInSector{"sectorName":"沪深A股"}` 非空
  （**5225 条代码字符串**，`payload_kind=code_list`），其余全空；
  `getFullTick` 两组均 `200005`。`quote_shapes=0`、`tick_shapes=0`、`code_list_proven=true`
  ⇒ **代码表不是行情**。
* `file_orders`：`dbf_armed=0`、`batch_order_enabled=0`；`installs`：`declared_conflicts=33`、
  `plan_applied=false`（本轮零配置写入）。
* 修复后复跑 `--seconds 1`（`..._095125.json`）新增并打印
  **`blocking_class: quote_channel_no_data`**，EXIT 仍为 2。

### 17.R5 paper 下单 / 撤单与 live 前置缺口

| 项 | 实测 |
| --- | --- |
| 命令 | `qmt_order_smoke.py --code 513090 --volume 100 --price 1.500`（默认 **paper**） |
| 结果 | **PAPER-39**，precheck `allowed=true` → place OK → `order_visible` `filled=100, status=filled` |
| 撤单 | **SKIP（不适用）**：paper 按限价立即全额成交，模拟盘无柜台挂单；`cancel_applicable=false` |
| EXIT | **0**，verdict「paper 下单成功且已成交；撤单在模拟盘不适用（**未验证真实撤单**）」 |
| 真实撤单 | **未验证**。paper 下 503「模拟盘…券商柜台不存在这笔委托」是**刻意契约**，不是「撤单失败留挂单」 |
| 笔数 | 本轮**仅 1 笔 paper 单**，零真实委托 |

`--live`（只做前置检查，预期被拒）EXIT=**1**，证据 `...\qmt_order_smoke_live_20261110_0948.json`，
`steps` 仅 `health` + `live_preconditions` ⇒ **未向 `/trade/precheck` 与 `/trade/order` 发任何请求**。
四道前置逐项：

| # | 前置 | 实测 | 判定 |
| --- | --- | --- | --- |
| 1 | agent `injected` 含 `passorder` | `injected=[]`，`runtime_mode=standalone_process`（需 QMT GUI 切公式模式，注册树加密不可自动化） | **挡住** |
| 2 | `trading_enabled=true` | `false` | **挡住** |
| 3 | `/signal/mode=live` | `paper` | **挡住** |
| 4 | 目标 broker `connected=true` | `90c700f265d6` **connected=true、active=true** | **已绿** |

⇒ live 被 **1/2/3 三项**挡住（第 4 项已满足）；未手工绕过、未强切 live、未发单。
另：`agent alive=true` 只说明进程在跑，不等于可下单；health 的 `last_error` 亦明写
「未捕获到终端注入的 passorder ⇒ 本连接只能查行情/K线，无法下单」，三态未被粉饰。

### 17.R6 逐项三态总表（严禁把「没查」写成「查了没事」）

| 检查项 | 三态 | 依据 |
| --- | --- | --- |
| 后端 health / 版本 | ok | HTTP 200 / v0.5.4 |
| 端口 58600 / 进程 | ok | 双 PID LISTENING，进程只读核对 |
| 端口 58610 | unavailable | 无监听、SYN_SENT 堆积 |
| 端口 58670 | ok（本轮已 LISTENING） | PID 22152，09:43:36 绑定，09:52:19 仍在 |
| XtModel 拉起 | ok（拉起 1 次、零重启） | 进程存活、端口绑定 |
| XtModel 上游行情登录 | **unavailable** | 33 处 `account is expired`，`accessible:false` |
| `/market/quote` auto | ok（**公开源快照**，非券商/非 tick） | `source=tencent` + 价格字段 |
| `/market/quote` broker / tdx | unavailable | 503 |
| `/market/ticks` | unavailable | 503 |
| `/market/kline` 1d / 1m | unavailable（本轮连 cache_stale 都没有） | 503 |
| 58670 `getTag` | ok（服务应答） | `xtquant 1.0` |
| 58670 `getFullTick` | unavailable（应答但空结果） | `result={}`、`quote_proven=false` |
| 58670 订阅推送 | unavailable（ACK+心跳，0 quote） | 45s 抓帧 |
| 58600 订阅 / `getFullTick` | unavailable（明确证伪） | `ErrorID=200005` |
| 证券代码表 | ok（**仅 code_list**） | 5225 条字符串 |
| 实时 tick（push / pull） | **unavailable（未证实）** | `push_proven=pull_proven=realtime_tick_proven=false` |
| paper 下单 | **ok** | PAPER-39 filled=100 |
| paper 撤单 | **skipped（不适用，刻意契约）** | 已成交，无柜台挂单 |
| 真实撤单 | **skipped（未验证）** | live 未放行，本轮零真实委托 |
| live 发单 | **skipped（前置拒绝）** | EXIT=1，零请求 |
| 配置 / 进程结束 / push | skipped（按硬约束不执行） | — |

### 17.R7 自动检查问题清单

1. **自写行情探针首跑崩溃**：`output/qmt_market_probe_20261110.py` 打印格式串 8 个占位符只给
   7 个参数 ⇒ `TypeError`，证据未生成。**判为探针自身缺陷，不是接口结论**，修复后复跑。
2. **自写行情探针证据分级错**：初版按 `count` 判「空」，但 `/market/quote` 的 data **没有 `count` 键**，
   会把 `source=tencent` 的真实价格快照误报成 `state=empty`。已改为按 `last/price` 字段判据，
   并显式给出 `broker_quote_proven` / `realtime_tick_proven` / `freshness_proven` 三个独立轴。
   ⇒ 这正对应任务点名要查的「结果键 / 证据分级」；证据脚本在 `output/`（gitignored），非生产代码。
3. **探针参数形状补扫**：对 58670 另扫 10 组（4×`getFullTick`、2×`getMarketData`、
   2×`getStockListInSector`、`getInstrumentDetail`、`getTag`），
   `...\qmt_shape_audit_20261110_094817.json` 显示 `getFullTick` 四形状全 `result={}`、
   `getMarketData` 两组 `200005`、代码表两组全空 ⇒ **不是形状漏扫**，是上游无数据。
4. **ACK 误计**：已核 `summarize_capture` 把 `ack` 排除在 `data/quote` 之外，
   45s 只有 1 ACK + 10 心跳仍判「只有订阅应答与心跳」，无误报。
5. **证据分级**：5225 条代码表只计 `code_list_proven`；`getFullTick={}` 不计行情；
   本轮无日线 / 陈旧缓存冒充实时 tick。
6. **退出码与判词**：`tradehours` EXIT=2 / `order_smoke paper` EXIT=0 / `--live` EXIT=1，
   与各自 verdict、`exit_code_meaning` 逐条一致；`_finish` 的 `exit_code=0` 未被 `or 1` 吞掉。
7. **paper/live 未混用**：verdict 明写「未验证真实撤单」，未用 paper 成功冒充 live 成功。
8. **agent 三态未粉饰**：`alive=true` 与 `injected=[] / trading_enabled=false /
   standalone_process` 同时记录，health `last_error` 同源佐证。
9. **发现两处白名单内真实缺陷并修复**（见 17.R8）：① 默认 paper 冒烟在后端已切 live 时会打到
   真实柜台；② `qmt_tradehours_validate` 的 `pending_human_actions` 在 58670 已监听却无数据时
   仍劝人「保持 XtModel 可用」，误导反复重启（本系列明确禁止）。
10. **文档编号冲突**：既有 §17（21:00 收盘后）与本系列 §17 同名。已在节首加编号说明，
    旧节逐字未动；是否重排编号属待人工处理。

### 17.R8 自动修复清单（文件 / commit / 测试 / 是否回滚）

| 文件 | 修复 | 备份 |
| --- | --- | --- |
| `scripts/qmt_order_smoke.py` | ① 默认 paper 反查 `/signal/mode`，后端为 `live` 时 EXIT=1 且零发单（`mode_guard=blocked_live`）；`--live` 分支不重复打标签（`not_applicable_live`），避免「live 运行却标 confirmed_paper」的自相矛盾；② docstring 改写为真实四道前置口径，删除未实现的「账户快照为 live」声明 | `output/_autofix_backup_20261110_0940/qmt_order_smoke.py.bak` |
| `scripts/qmt_tradehours_validate.py` | `_conclusion` 新增 `blocking_class` 四态（`environment_absent` / `channel_58670_absent` / `quote_channel_no_data` / `none`）；`pending_human_actions` 按阻塞类别分流；证据与 stdout 均打印 `blocking_class` | `.../qmt_tradehours_validate.py.bak` |

* **本地提交**：`863f7bb [auto-fix] paper 冒烟反查后端模式防误打 live；58670 监听但无数据单独归类`
  （仅 `git add` 上述两个文件；**绝不 push**，本地 HEAD 由 `6b53e70` → `863f7bb`）。
* **门禁**：未增 / 未改任何测试文件 ⇒ 按任务约定走 `audit_doc_links.py` + 相关 pytest，
  **未跑 `ci_reconcile.py`**；`EXPECTED_TESTS=2952`、README 两处 `220 / 2952` 全部未动。
* **验证链**：`py_compile` OK →
  `pytest tests/test_qmt_tradehours_validate.py tests/test_xtq_rpc.py tests/test_qmt_file_order.py tests/test_qmt_client_routes.py tests/test_install_registry.py tests/test_qmt_order_smoke.py -q -p no:cacheprovider`
  = **156 passed**（6.32s / 复跑 5.78s，0 failed）→
  可证伪探针 `output/qmt_autofix_falsify_20261110.py` **9/9 PASS**（含「后端 live ⇒ 拒绝且零发单」与
  「paper ⇒ 不得过度拦截」正反两面，及 `blocking_class` 四态）→
  `scripts/audit_doc_links.py` **EXIT=0** →
  真实环境复跑 `tradehours --seconds 1` EXIT=2 / `blocking_class=quote_channel_no_data`、
  `order_smoke --live` EXIT=1 且未发单。
* **是否回滚**：**0 次回滚**，全程一次通过；备份 sha256 与修复前一致。
* **越界问题**：一律只记「待人工处理」（17.R9），未改任何生产业务代码 / 前端 / 配置 /
  下单撤单业务逻辑 / VERSION / 打包。

### 17.R9 仍未解决项（三态分开报）

**A. 环境缺失（本轮 58670 已不缺，其余仍在）**
① 58610 无监听（`SYN_SENT` 堆积）；② 58600 双 PID 同绑导致探针实例不确定；
③ 33 项声明端口冲突、`plan_applied=false`（落盘 `broker.ini` 需用户批准）。

**B. 需人工动作（代码与授权都解不开）**
① **行情账号 `account is expired`**（33 处）⇒ 券商侧续期/授权；反复重启 XtModel 无效；
② QMT GUI 内把策略从「独立进程」切到公式/内置 Python 形态以注入 `passorder`（注册树加密，不可自动化）；
③ `trading_enabled=false` 需在客户端开启；
④ `/signal/mode=paper` 本轮**未改、不得强切**；
⑤ 国信 `dd1822009f64` 未开通程序化交易权限（基线）、gd_qmt 账户 22453951 资产全 0（基线）；
⑥ `dbf_armed=0`、`batch_order_enabled=0`、导出下单模板 DBF 与 `is_automonitor` 需界面操作；
⑦ 文档 §17 编号重复是否重排；
⑧ **真实撤单验证**：须等四道前置全绿且脚本自检通过后再跑 `--live`，本轮**未做**。

**C. 接口不通 / 已应答但无数据**
① 58670 `getFullTick` 恒 `result={}`、45s 抓帧 0 quote（`blocking_class=quote_channel_no_data`）；
② 58600 `subscribeQuote` / `getFullTick` `ErrorID=200005 未找到处理函数`（明确证伪该通道）；
③ `/market/quote` broker / tdx、`/market/ticks`、`/market/kline` 均 503。
⇒ 以上均**不是**「没查」，也不是「环境缺失」；是通道已应答却无行情数据 / 源不可用。

### 17.R10 判读纪律与约束如实性

* **ACK 不是推送**：58670 的 `ErrorID=0` 订阅 ACK 与 10 个心跳一律不计 `quote`。
* **代码表不是行情**：5225 条只记 `code_list_proven`。
* **日线 / 陈旧缓存不是实时 tick**：本轮 kline 直接 503，连 `cache_stale` 都未出现；
  公开源 `tencent` 快照有价格字段，但**不是券商通道、不是 tick 推送、无服务端时间戳**。
* **服务端应答 ≠ 有数据**：`getTag` ok、`getFullTick` `status=0` 但 `result={}`。
* **paper 成交不是 live 成交**：PAPER-39 成功仅证明平台侧下单链路；live 发单为零。
* **paper 撤单 503 是刻意契约（不适用）**，不是「撤单失败留挂单」；报告一律写
  「真实撤单未验证」，绝不写「撤单已验证」。
* 约束全项保持：**未向真实柜台发单**、未改任何配置、未结束客户端进程、
  未伪造凭证 / 绕 PID 白名单、**未 push**、paper 仅 1 笔、live 零请求；
  XtModel 按授权拉起 **1 次 / 上限 2 次**，零重启。
* 退出码如实：`tradehours` 2（需人工动作 / 未证实）、`order_smoke paper` 0（下单成功且撤单不适用）、
  `order_smoke --live` 1（前置不满足、未发单）；三者含义互不顶替。
* §15.7 / §15.8 / §16 / 旧 §17 均**逐字未改**；本文档保持 **LF**（追加后 CRLF=0 复核）。

## 18. 2026-11-10 交易时段自动分析第 2 次（10:05–10:12，共 3 次）

> ★ **承接关系**：本节承接 §17（本系列第 1 次，09:42–09:52）与更早的 §15.7 / §15.8 / §16 /
> 2026-11-09 21:00 的旧 §17；上述章节**逐字未动**。子标题统一用 `18.R*`。
> ★ **时点**：全部采样落在 09:30–11:30 交易时段内（10:05:25 预检 / 10:12:16 收尾），
> 与 21:00 收盘后轮不同，证据强度不打折。
> ★ **三态口径**：`ok` = 真跑通且有内容判据；`unavailable` = 环境/前置缺失或接口不通
> （两者再分开写）；`skipped` = 本轮按要求未执行。绝不把「没查」写成「查了没事」。

### 18.R0 采样时间、命令与证据文件（绝对路径）

| 时刻（+08） | 动作 | 命令 / 端点 |
| --- | --- | --- |
| 10:05:25 | 只读预检 | `Get-NetTCPConnection` / `netstat -ano -p tcp`、`Get-CimInstance Win32_Process`（XtItClient / XtModel / python）、读 `agent_status.json` |
| 10:05:2x | 基线复核 | `curl --noproxy '*' -H 'X-API-Key: qmt-dev-key' .../api/v1/health`、`.../signal/mode`、`.../brokers` |
| 10:06:42 | 授权拉起 | `Start-Process P:\stock\guoxin_iquant_11\xtmodel\XtModel.exe -WorkingDirectory <其所在目录> -PassThru` |
| 10:07:24 | 行情三源 | `backend/runtimes/cp311/python.exe -u output/qmt_market_probe_20261110.py`；另 `curl` 补查 `kline?source=broker/tdx`、`ticks?source=broker` |
| 10:07:56–10:08:48 | 45 秒探针 | `backend/runtimes/cp311/python.exe -u scripts/qmt_tradehours_validate.py --seconds 45` |
| 10:09:22 | 下单/撤单 | `... -u scripts/qmt_order_smoke.py --code 513090 --volume 100 --price 1.500`；再 `--live` |
| 10:10:2x | 前序修复复核 | `git log --all --grep='[auto-fix]'`、`git show 863f7bb`、6 文件 `pytest`、`output/qmt_autofix_falsify_20261110.py`、`scripts/audit_doc_links.py` |
| 10:11 | 自动检查 | `backend/runtimes/cp311/python.exe -u output/qmt_autocheck_20261110_1000.py`（15 项，纯内存、零发单） |
| 10:12:16 | 收尾复核 | 再查端口 / 进程 / 日志 / 配置 mtime / agent / mode |

证据文件（全部绝对路径，`output/` 已被 `.gitignore` 忽略）：

* `P:\github_public\qmt_work\output\qmt_xtmodel_launch_20261110_1000.json`（拉起三态 + 20s 后端口/进程/日志计数）
* `P:\github_public\qmt_work\output\qmt_market_probe_20261110_100724.json`（**行情三态正式证据**）、`...\qmt_market_probe_1000_run.log`
* `P:\github_public\qmt_work\output\qmt_tradehours_validate_20261110_100848.json`（**本轮主证据**）、`...\qmt_tradehours_validate_1000.log`
* `P:\github_public\qmt_work\output\qmt_order_smoke_paper_20261110_1000.json`、`...\qmt_order_smoke_live_20261110_1000.json`
* `P:\github_public\qmt_work\output\qmt_autofix_falsify_20261110.json`（本轮复跑 9/9；§17 原版另存 `..._0940.json`）
* `P:\github_public\qmt_work\output\qmt_autocheck_20261110_1000.json`、`...\qmt_autocheck_20261110_1000.py`
* 外部只读来源：`P:\stock\guoxin_iquant_11\xtmodel\data\log\xtquantservice_20261110.log`、
  `C:\Users\Administrator\qmt_work\bigqmt_bridge\agent_status.json`

### 18.R1 端口 / 进程 / 后端基线（只读预检，10:05:25）

| 项 | 实测 | 三态 |
| --- | --- | --- |
| 后端 | `127.0.0.1:21118` health **HTTP 200 / code 0 / v0.5.4**，uptime 6144s，`lifecycle.ready=true` | ok |
| 58600 | **两个 PID 同时 LISTENING**：`20752`（`P:\stock\gd_qmt\bin.x64\XtItClient.exe`）、`22344`（`P:\stock\guoxin_iquant_11\bin.x64\XtItClient.exe`）——与 §17 的 PID **完全相同** | ok（但探针连到哪个实例不确定，可重复性变量未消除） |
| 58610 | **无监听**；`SYN_SENT` 堆积（PID 2796 / 19336 反复重试） | unavailable（通道缺失） |
| 58670 | **预检时无监听、无 `XtModel.exe` 进程**；日志最后写入停在 **09:55:16** | unavailable（**环境缺失**，与 §17 收尾「09:52:19 仍 LISTENING」不同，见 18.R2） |
| XtModel 进程 | 预检时不存在；`xtquantservice_20261110.log` 09:54–09:56 区间**无 shutdown / exit / fatal 行** ⇒ 消失真因日志不可判定 | unavailable（原因不可证，不臆测） |
| broker | `90c700f265d6` 文件桥 `connected=true, active=true`；`23019cce2bda`（gd_qmt/22453951）、`dd1822009f64`（guoxin/410001005814）均 `connected=false` | 与 §17 基线一致 |
| `/signal/mode` | `{"mode":"paper"}` | ok（**本轮未改、未强切**） |
| agent_status | `alive=true`、`runtime_mode=standalone_process`、`injected=[]`、`trading_enabled=false`，`ctx_methods` 无 `passorder` | ok（状态如实记录，未粉饰） |
| git | 干净；HEAD `b90cede`；文档 `i/lf w/lf`（CRLF=0，LF=1686，sha256 前缀 `9ab186b61ed52fc0`） | ok |

两个客户端**全程只读**：未结束进程、未写 `broker.ini` / `Config.xml` / `agent_config.json` /
`is_automonitor` / 任何下单配置。收尾复核（10:12:16）
`P:\stock\gd_qmt\python\agent_config.json` mtime 仍为 **2026-10-02 17:13:22**（未变）；
`P:\stock\gd_qmt\config\broker.ini`、`P:\stock\guoxin_iquant_11\config\broker.ini` 均只读核对、零写入。

### 18.R2 XtModel 拉起结果（授权范围内 1 次，零重启）

| 时刻 | 观测 |
| --- | --- |
| 10:05:25 | 58670 无监听、无 `XtModel.exe` ⇒ **按 §17 的「先查再拉起」约定，本轮必须重新拉起** |
| 10:06:42 | `Start-Process` 成功，PID **11496**，工作目录 = `P:\stock\guoxin_iquant_11\xtmodel` |
| 10:06:44 | 日志出现本轮新增 `login response ... resp:{ result: 4, reason: "account is expired" }`、`login failed, error:[4, 用户已过期], username:`（空） |
| 10:06:58 | 日志 `[init_quote] [xtdc]finished` → `[try_listen] ... portStart:58670` → `IPythonApiServer::createServer listen, finished:true ... port:58670` → `[try_listen] finished, result:0.0.0.0:58670` |
| 10:07:02（+20s） | 进程存活、`exit_code` 空；`TCP 0.0.0.0:58670 ... LISTENING 11496` |
| 10:12:16（收尾） | PID 11496 仍在、58670 仍 LISTENING，两个 `XtItClient` 仍在；日志仍在写（`last=10:12:10`） |

**日志真实原因（全程只读）**：今日 `account is expired` 累计由 §17 的 **33 处 → 49 处**
（+16 处集中在本轮 10:06:42–10:06:44），逐台命中行情服务器
`115.231.218.73:55310` / `42.228.16.211:55300` / `203.135.101.35:55300` /
`36.99.48.21:55300` / `115.231.218.79:55310` / `115.231.218.13:55300` /
`115.231.218.12:55300` 等，均为 `login failed` + `username:` 空；伴随
`WholeMarketQuoter::waitForInitFinish wait before send subscribe, timeout`、
`HisContract download done ... dataSize:0`、§17 尾部 `QuoteClient::subscribeQuote client is not connect`、
`[model][perform] send cache data ... count:0`。另有 XtModel 自身的
`IPythonApiServer::loadConfig read file failed, path:...\config\xtquantconfig.json` 与
`saveConfig save global config failed` —— 属 XtModel 进程行为，本轮**未替它写任何配置**。

⇒ **RPC 服务可监听、可应答，但上游行情服务器登录全部被拒**（账号过期），这是 58670 无 tick 的直接成因。
按授权「同一轮最多重启 2 次」，本轮**只拉起 1 次、零重启**（一次即绑定成功，无需重启）。

★ **与 §17 的关键轴变化**：§17 中 58670 从 09:43:36 稳定监听到 09:52:19；本轮预检时已消失，
日志止于 09:55:16 且无退出记录 ⇒ **XtModel 进程不跨自动化轮次存活**（推测与宿主进程树回收或客户端侧
自行退出有关，日志无法证实，故不作断言）。重新拉起后 58670 恢复监听，但行情结论**没有变化**：
`blocking_class=quote_channel_no_data`。⇒ 「反复拉起 XtModel」只能恢复**监听**，不能恢复**数据**；
§17 的 `pending_human_actions` 分流把处置指向上游登录态而非重启，本轮实证其正确。

### 18.R3 行情三态：`/market/quote` 三源、`/market/ticks`、`/market/kline`

正式证据 `...\qmt_market_probe_20261110_100724.json`（10:07:24，EXIT=0）。

| 端点 / 源 | HTTP / code | `source` | 实测 | 三态 |
| --- | --- | --- | --- | --- |
| `/market/quote?source=auto`（600036.SH） | 200 / 0 | **`tencent`** | `last=42.91`、`price=42.91`、`change_pct=-0.23`、`volume=15288000` | **ok（公开源快照）** |
| `/market/quote?source=auto`（513090.SH） | 200 / 0 | `tencent` | `last=1.608`、`change_pct=-0.12`、`volume=486335400` | ok（公开源快照） |
| `/market/quote?source=broker` | **503 / 503** | — | `行情获取失败：券商连接异常或未连接` | unavailable |
| `/market/quote?source=tdx` | **503 / 503** | — | `TDX 行情源不可用` | unavailable |
| `/market/ticks?source=auto` | **503 / 503** | — | `本地 TDX 行情源未就绪（未安装 eltdx 或行情服务器不可达）` | unavailable |
| `/market/ticks?source=broker` | **503 / 503** | — | 同上（补查） | unavailable |
| `/market/kline?period=1d&source=auto` | **503 / 503** | **`source=None`** | `缺少券商 SDK：…（get_market_data 失败: 无法连接xtquant服务…）` | unavailable |
| `/market/kline?period=1m&source=auto` | **503 / 503** | `source=None` | 同上 | unavailable |
| `/market/kline?period=1d&source=broker` | **503 / 503** | `source=None` | 同上（补查） | unavailable |
| `/market/kline?period=1d&source=tdx` | **503 / 503** | `source=None` | `券商不可用、TDX 行情源无数据且本地数据仓为空` | unavailable |

★ **source / stale 显式交代**：本轮 kline 全部 503、`data:null`，`source=None`、
`stale_suspect=false`、**没有 `cache_stale`、没有任何日线/分钟线返回**，
`as_of` / `cached_at` / `trading_date` / `note` 全为 `None`。
基线（08:2x）曾回 `source=cache_stale`（缓存止于 2025-04），§17 与本轮**都没有**；
⇒ 本轮不存在把陈旧缓存 / 日线 / 代码表误当成实时 tick 的机会。
另核：§17.R3 表中 kline 的消息是**缩写**，两轮原始证据的完整消息**逐字相同**
（都带「缺少券商 SDK…」前缀），不是新增故障。

★ **证据分级（不可合并成一句「行情可用」）**：`source=tencent` 是公开 HTTP 行情源快照，
不是券商 xtquant 通道、不是 58670 推送 tick，响应里**没有任何服务端时间戳**
⇒ `quote_snapshot_proven=true`、`broker_quote_proven=false`、`realtime_tick_proven=false`、
`freshness_proven=false`。09:45:47→10:07:24 两次采样 `last` 42.79→42.91、
513090 1.618→1.608 有变化，只能说明公开源盘中在更新，**不能**据此宣称大 QMT 实时行情已通。

⇒ **实时行情（大 QMT 通道）「不可用 / 未证实」**；REST 层只有公开源快照可用，两者是不同轴，不得互相顶替。

### 18.R4 45 秒探针：`axes` 与逐形状证据

主证据 `...\qmt_tradehours_validate_20261110_100848.json`（采样起点 10:07:56，打印 10:08:48，`--seconds 45`），
日志 `...\qmt_tradehours_validate_1000.log`，**EXIT=2（需人工动作或未证实）**。

```
ports: {"58600": ["20752","22344"], "58610": [], "58670": ["11496"]}
axes : {"push_proven": false, "pull_proven": false, "realtime_tick_proven": false,
        "code_list_proven": true, "channel_58600_listening": true,
        "channel_58670_listening": true, "channel_58670_missing": false}
blocking_class: quote_channel_no_data
verdict: 实时行情未证实：58670 在监听但订阅抓帧未出现行情字段；拉取形状亦未命中；
         证券代码表已取到（不算行情数据）。需人工动作或继续参数形状扫描
```

* `probe_58670_tag`：`getTag` → `status_code=0`、`ErrorID=None`、`payload_kind=dict`、`quote_proven=false`
  ⇒ **RPC 服务活着**（`ok` 只代表应答，不代表有数据）。
* `probe_58670_fulltick`：`getFullTick({"stockMarkets":["600036.SH"]})` → `payload_kind=empty`、
  `quote_proven=false` ⇒ **服务端应答了，结果体为空**。
* `capture_58670`（45s）：`total=10 / heartbeat=9 / ack=1 / quote=0 / data_other=0`，
  verdict「只有订阅应答与心跳（订阅未生效或非交易时段）」⇒ **ACK 没有被误计成推送**。
* `capture_58600`（6s）：`total=3 / ack=1 / heartbeat=1 / quote=0`，`proven=false` ⇒ 58600 订阅通道同样无行情。
* `probe_58600` 12 组形状：仅 `getStockListInSector{"sectorName":"沪深A股"}` 非空
  （**5225 条代码字符串**，`payload_kind=code_list`），`quote_shapes=0`、`tick_shapes=0`、
  `code_list_proven=true` ⇒ **代码表不是行情**。
* `installs`：`count=4`、`declared_conflicts=33`、`plan_applied=false`（零配置写入）；
  `file_orders`：`dbf_armed=0`、`batch_order_enabled=0`。
* 修复后的 `pending_human_actions[0]` 明确写「核查上游行情服务器登录态（account is expired /
  程序化交易权限），**不是反复重启 XtModel 能解决**」，与 18.R2 的实测一致 ⇒ §17 修复在本轮真实环境下仍生效。

### 18.R5 paper 下单 / 撤单与 live 前置缺口

| 项 | 实测 |
| --- | --- |
| 命令 | `qmt_order_smoke.py --code 513090 --volume 100 --price 1.500`（默认 **paper**） |
| 结果 | **PAPER-40**：`mode_guard=confirmed_paper` → precheck `allowed=true` → place OK → `order_visible` `filled=100, status=filled` |
| 撤单 | **SKIP（不适用）**：paper 按限价立即全额成交，模拟盘无柜台挂单；`cancel_applicable=false`、cancel 步骤 `ok=None` |
| EXIT | **0**，verdict「paper 下单成功且已成交；撤单在模拟盘不适用（**未验证真实撤单**）」 |
| 真实撤单 | **未验证**。paper 下 503「模拟盘…券商柜台不存在这笔委托」是**刻意契约**，不是「撤单失败留挂单」 |
| 笔数 | 本轮**仅 1 笔 paper 单**，零真实委托 |

`--live`（只做前置检查，预期被拒）EXIT=**1**，证据 `...\qmt_order_smoke_live_20261110_1000.json`，
`steps` 仅 `health` + `live_preconditions` ⇒ **未向 `/trade/precheck` 与 `/trade/order` 发任何请求**。

| # | 前置 | 实测 | 判定 |
| --- | --- | --- | --- |
| 1 | agent `injected` 含 `passorder` | `injected=[]`，`runtime_mode=standalone_process`（需 QMT GUI 切公式模式，注册树加密不可自动化） | **挡住** |
| 2 | `trading_enabled=true` | `false` | **挡住** |
| 3 | `/signal/mode=live` | `paper` | **挡住** |
| 4 | 目标 broker `connected=true` | `90c700f265d6` **connected=true、active=true** | **已绿** |

⇒ live 被 **1/2/3 三项**挡住（第 4 项已满足）；未手工绕过、未强切 live、未发单。
`agent alive=true` 只说明进程在跑，不等于可下单；health 的 `last_error` 同源写明
「未捕获到终端注入的 passorder ⇒ 本连接只能查行情/K线，无法下单」，三态未被粉饰。
**不得用 PAPER-40 的成功冒充 live 成功。**

### 18.R6 与 §17 逐轴对比（§17 → §18）

| 轴 | §17（09:42–09:52） | §18（10:05–10:12） | 变化 | 归因 |
| --- | --- | --- | --- | --- |
| 58670 监听 | PID 22152，09:43:36 绑定、09:52:19 仍在 | 预检**不在**（日志止 09:55:16）；10:06:58 由新 PID 11496 重新绑定，收尾仍在 | **变** | XtModel 不跨轮存活；日志无 exit/shutdown 行 ⇒ 真因不可证。重新拉起只恢复监听，不恢复数据 |
| XtModel 拉起 | 1 次 / 0 重启 | 1 次 / 0 重启 | 不变 | 授权上限 2 次均未触顶 |
| `account is expired` | 33 处 | 49 处（+16 全在新一轮启动时） | **变（量）** | 同一根因，非新问题；每次拉起都会再撞一遍登录拒绝 |
| 58600 | 双 PID 20752 / 22344 | 双 PID 20752 / 22344 | 不变 | 端口冲突与实例不确定性未消除 |
| 58610 | 无监听 + SYN_SENT | 无监听 + SYN_SENT | 不变 | 通道缺失 |
| 后端 / mode / brokers / agent | v0.5.4 / paper / 文件桥连通 / standalone | 逐项相同 | 不变 | 未改任何配置 |
| `axes` 七项 | push/pull/tick=false、code_list=true、两端口 listening、58670_missing=false | **逐位相同** | 不变 | 上游无行情数据，与监听是否刚恢复无关 |
| `blocking_class` | 主证据 `None`（修复前生成）；修复后复跑 `quote_channel_no_data` | 主证据即 `quote_channel_no_data` | **变（修复生效）** | `863f7bb` 已进主证据链，不再只在复跑里出现 |
| `capture_58670` | 11 帧 = 1 ACK + 10 心跳，0 quote | 10 帧 = 1 ACK + 9 心跳，0 quote | 微变 | 抓帧时长固定、心跳节奏波动；**quote 恒 0** |
| `capture_58600` | 1 ACK + 1 心跳 + 1 closed | 3 帧 = 1 ACK + 1 心跳，0 quote | 微变 | 同上；`proven` 恒 false |
| 58670 `getTag` / `getFullTick` | ok / `result={}`、quote_proven=false | ok（`payload_kind=dict`） / `empty`、quote_proven=false | 不变 | 应答≠有数据 |
| 58600 代码表 | 5225 条 `code_list` | 5225 条 `code_list` | 不变 | 代码表≠行情 |
| `/market/quote?source=auto` | 200 `tencent`，600036 last 42.79 / 513090 1.618 | 200 `tencent`，600036 last 42.91 / 513090 1.608 | 变（值） | 公开源盘中更新；**仍是快照，不是券商 tick** |
| `/market/quote` broker / tdx、ticks、kline 1d/1m | 全 503、无 `cache_stale` | 全 503、无 `cache_stale`（另补查 kline broker/tdx 亦 503） | 不变 | 源不可用 |
| paper 下单 | PAPER-39 filled=100、撤单 SKIP、EXIT 0 | PAPER-40 filled=100、撤单 SKIP、EXIT 0 | 不变（单号递增） | 平台侧链路稳定；**真实撤单仍未验证** |
| `--live` | EXIT 1，3 项缺口，零请求 | EXIT 1，3 项缺口，零请求 | 不变 | 四道前置未松动 |
| 前序 `[auto-fix]` 有效性 | 刚提交 `863f7bb`，156 passed / 9/9 | 仍有效、无回归（见 18.R7） | 不变 | — |

### 18.R7 前序 `[auto-fix]` 复核（要求：失效或引入回归则优先修回）

* `git log --oneline --all --grep='[auto-fix]'` ⇒ **全仓只有 1 条**：
  `863f7bb [auto-fix] paper 冒烟反查后端模式防误打 live；58670 监听但无数据单独归类`；
  `git show --stat` ⇒ 2 文件 / +52 −8（`scripts/qmt_order_smoke.py`、`scripts/qmt_tradehours_validate.py`）。
  HEAD 为 `b90cede`（文档），修复提交**未被后续覆盖或 revert**。
* **静态锚点仍在**：`mode_guard` / `blocked_live` / `confirmed_paper` / `not_applicable_live`、
  `blocking_class` 四态（`environment_absent` / `channel_58670_absent` / `quote_channel_no_data` / `none`）、
  `pending_human_actions` 按类别分流、stdout 打印 `blocking_class`。
* **真实环境功能复核**：10:09:22 paper 打出 `mode_guard=confirmed_paper` 且正常发单（未误拦截）；
  10:08:48 主证据带 `blocking_class=quote_channel_no_data` 且人工动作指向上游登录态而非重启；
  `--live` 仍零请求。⇒ 两条修复都**仍有效**。
* **回归测试**：`cd backend && ./runtimes/cp311/python.exe -m pytest tests/test_qmt_tradehours_validate.py
  tests/test_xtq_rpc.py tests/test_qmt_file_order.py tests/test_qmt_client_routes.py
  tests/test_install_registry.py tests/test_qmt_order_smoke.py -q -p no:cacheprovider`
  ⇒ **156 passed / 0 failed（5.81s）**，与 §17 的 156 一致；
  可证伪探针 `output/qmt_autofix_falsify_20261110.py` 本轮复跑 **9/9 PASS**
  （含「后端 live ⇒ 默认 paper 拒绝且零发单」「paper ⇒ 不得过度拦截」正反两面与 `blocking_class` 四态）；
  `scripts/audit_doc_links.py` **EXIT=0**。
* **门禁**：本轮**未增 / 未改任何测试文件** ⇒ 按任务约定不跑 `ci_reconcile.py`；
  `EXPECTED_TESTS=2952`、README 两处 `220 / 2952` 已 grep 复核、**全部未动**。
* ⇒ **结论：无失效、无回归，无需修回**；本轮没有产生新的 `[auto-fix]` 提交。

### 18.R8 自动检查问题清单

`output/qmt_autocheck_20261110_1000.py`（纯内存、零发单、零配置写）**15/15 PASS**，证据 `..._1000.json`。

1. **探针形状 / 结果键**：`/market/quote` 的 `data` 无 `count` 键（§17 修过的判据仍生效），
   按 `last` / `price` 字段判；kline 503 `data:null`，无 `bars` 可误读。
2. **证据分级**：`classify_payload` 对 5225 条代码表只给 `code_list/quote_proven=false`、
   对 `{}` 给 `empty`、对真实 tick 形状 `{代码:{lastPrice,…}}` 给 `quote_proven=true`；
   日线带 `open/high/low` 也会 `quote_proven=true`，但 `qmt_tradehours_validate` 另用
   `realtime_eligible`（`getFullTick` 或 `period=tick`）把「有行情字段」与「实时 tick」分开 ⇒ 无误并。
3. **ACK 误计**：真实抓帧 1 ACK 全被排除在 `quote/data` 之外、`proven=false`；
   合成帧（ACK 里塞 `lastPrice`）经 `summarize_capture` 仍 `proven=false`（分类在 `capture()` 完成，
   且 `_quote_signal` 先于 `status` 判定）⇒ 无误报。
4. **退出码与判词如实性**：`tradehours` 2 / 判词「未证实」、`paper` 0 / 判词「真实撤单未验证」、
   `--live` 1 / 判词「前置未满足、已拒绝发单」，三者与 `exit_code_meaning` 逐条一致；
   `_finish` 的合法 `0` 未被 `or 1` 吞掉（既有单测 + 本轮真值双证）。
5. **paper / live 结论混用**：paper verdict 明写「未验证真实撤单」，`cancel_applicable=false`、
   cancel 步骤 `ok=None`（SKIP 而非 FAIL）；live `steps` 只有 2 步且全文无 `/trade/precheck` `/trade/order` ⇒ 未混用。
6. **agent 三态粉饰**：`alive=true` 与 `injected=[] / trading_enabled=false / standalone_process`
   同时记录，health `last_error` 同源佐证；未把「进程活着」写成「可下单」。
7. **前序修复回归**：见 18.R7，无。
8. **新识别但本轮不修（如实记录，不凑数改代码）**：
   * (a) `output/qmt_market_probe_20261110.py::grade()` 对**合成**的
     `source=cache_stale` + 有 `bars` 的 kline 会给出 `state=ok`（虽 `stale_suspect=true`、
     `realtime_tick_proven=false`、`freshness_proven=false`）。该文件在 `output/`（gitignored、
     **不在白名单**），且本轮 kline 全 503、该分支**未触发** ⇒ 只记录局限；§18 结论一律用原始
     `http/code` + 显式 `source/stale`，**不引用该 `state` 字段**。
   * (b) `qmt_order_smoke.py` 在 `/signal/mode` 读不出时标 `mode_guard=unknown` 后**仍继续** paper 发单，
     属「读不出模式」的残余 fail-open 取舍；§17 已把它写成刻意口径，且修改它会触碰
     **下单业务逻辑（任务明令严禁）** ⇒ 记为残余风险、不修。本轮 mode 可读且为 paper，未触发。
   * (c) XtModel 进程不跨轮存活（18.R2）是**环境事实**，不是诊断脚本缺陷，不改代码。
   * (d) §17.R3 的 kline 消息是缩写、两轮原始消息逐字相同 ⇒ 仅作文档保真说明，**不回改 §17**。
9. ⇒ **本轮无需修复**：白名单内未发现新的「诊断说谎 / 假绿假红 / 结论混用」缺陷，
   未改任何生产代码、测试、配置、计数；无备份 / 回滚 / 新 `[auto-fix]` 提交。

### 18.R9 自动修复清单

| 项 | 本轮 |
| --- | --- |
| 白名单内修复 | **0 处**（如实写「本轮无需修复」，不为凑数改代码） |
| 备份 / 回滚 | 无（未进入修复流程，故无「已回滚」） |
| 新增 `[auto-fix]` 提交 | **无**；本地 HEAD 仍 `b90cede`（仅追加本节文档，另作 `[auto-doc]` 本地提交，**绝不 push**） |
| 验证链 | 未改测试 ⇒ 按约定走 `audit_doc_links.py`（EXIT=0）+ 6 文件 `pytest`（156 passed）+ 可证伪 9/9 + 自动检查 15/15；**未跑 `ci_reconcile.py`** |
| 门禁计数 | `EXPECTED_TESTS=2952`、README 两处 `220 / 2952` 未动；文档 LF 保持 |
| 越界问题 | (a)(b)(c)(d) 一律记「待人工处理 / 已知取舍」，未改任何生产业务代码、前端、配置、下单/撤单逻辑、VERSION / 打包 |

### 18.R10 逐项三态总表（严禁把「没查」写成「查了没事」）

| 检查项 | 三态 | 依据 |
| --- | --- | --- |
| 后端 health / 版本 | ok | HTTP 200 / v0.5.4 / ready=true |
| 端口 58600 / 进程 | ok | 双 PID LISTENING，进程只读核对 |
| 端口 58610 | unavailable | 无监听、SYN_SENT 堆积 |
| 端口 58670（预检） | **unavailable（环境缺失）** | 无监听、无 XtModel 进程 |
| XtModel 拉起 | ok（1 次 / 0 重启） | PID 11496，10:06:58 绑定 58670，收尾仍在 |
| 端口 58670（拉起后） | ok | 10:12:16 仍 LISTENING |
| XtModel 跨轮存活 | **unavailable** | 日志止 09:55:16、无 exit/shutdown 记录 ⇒ 真因不可证 |
| XtModel 上游行情登录 | **unavailable** | 49 处 `account is expired`，`username:` 空 |
| `/market/quote` auto | ok（**公开源快照**，非券商/非 tick） | `source=tencent` + 价格字段、无服务端时间戳 |
| `/market/quote` broker / tdx | unavailable | 503 |
| `/market/ticks`（auto / broker） | unavailable | 503 |
| `/market/kline` 1d / 1m（auto / broker / tdx） | unavailable（`source=None`、无 `cache_stale`） | 503 |
| 58670 `getTag` | ok（服务应答） | `status_code=0` |
| 58670 `getFullTick` | unavailable（应答但空结果） | `payload_kind=empty`、`quote_proven=false` |
| 58670 订阅推送 | unavailable（ACK+心跳，0 quote） | 45s 抓帧 10 帧 |
| 58600 订阅 / 12 组拉取 | unavailable（明确证伪） | `ErrorID=200005` 族、0 quote 形状 |
| 证券代码表 | ok（**仅 code_list**） | 5225 条字符串 |
| 实时 tick（push / pull） | **unavailable（未证实）** | `push_proven=pull_proven=realtime_tick_proven=false` |
| paper 下单 | **ok** | PAPER-40 filled=100 |
| paper 撤单 | **skipped（不适用，刻意契约）** | 已成交，`cancel_applicable=false` |
| 真实撤单 | **skipped（未验证）** | live 未放行，本轮零真实委托 |
| live 发单 | **skipped（前置拒绝）** | EXIT=1，零请求 |
| 配置写入 / 结束进程 / push | skipped（按硬约束不执行） | 配置 mtime 未变、`git status` 干净 |

### 18.R11 仍未解决项（三态分开报）

**A. 环境缺失**
① 58610 无监听（`SYN_SENT` 堆积）；② 58600 双 PID 同绑导致探针实例不确定；
③ 33 项声明端口冲突、`plan_applied=false`（落盘 `broker.ini` 需用户批准）；
④ **新增**：XtModel 不跨轮存活，11:00 第 3 次仍须先 `netstat 58670` 再决定是否拉起。

**B. 需人工动作（代码与授权都解不开）**
① **行情账号 `account is expired`**（今日 49 处）⇒ 券商侧续期/授权；反复重启 XtModel 无效；
② QMT GUI 内把策略从「独立进程」切到公式/内置 Python 形态以注入 `passorder`（注册树加密，不可自动化）；
③ `trading_enabled=false` 需在客户端开启；④ `/signal/mode=paper` 本轮**未改、不得强切**；
⑤ 国信 `dd1822009f64` 未开通程序化交易权限（基线）、gd_qmt 账户 22453951 资产全 0（基线）；
⑥ `dbf_armed=0`、`batch_order_enabled=0`、导出下单模板 DBF 与 `is_automonitor` 需界面操作；
⑦ 文档 §17 编号重复是否重排；⑧ `output/` 探针 `state` 对陈旧缓存的局限（18.R8a）与
`mode_guard=unknown` 残余 fail-open（18.R8b）需人工评估；
⑨ **真实撤单验证**：须等四道前置全绿且脚本自检通过后再跑 `--live`，本轮**未做**。

**C. 接口不通 / 已应答但无数据**
① 58670 `getFullTick` 恒空、45s 抓帧 0 quote（`blocking_class=quote_channel_no_data`）；
② 58600 `subscribeQuote` / `getFullTick` `ErrorID=200005 未找到处理函数`（明确证伪该通道）；
③ `/market/quote` broker / tdx、`/market/ticks`、`/market/kline` 均 503。
⇒ 以上均**不是**「没查」，也不是「环境缺失」；是通道已应答却无行情数据 / 源不可用。

### 18.R12 判读纪律、约束如实性与本轮四问

* **ACK 不是推送**：58670 的 `ErrorID=0` 订阅 ACK 与 9 个心跳一律不计 `quote`。
* **代码表不是行情**：5225 条只记 `code_list_proven`。
* **日线 / 陈旧缓存不是实时 tick**：本轮 kline 直接 503，`source=None`、无 `cache_stale`；
  公开源 `tencent` 快照有价格字段，但**不是券商通道、不是 tick 推送、无服务端时间戳**。
* **服务端应答 ≠ 有数据**：`getTag` ok、`getFullTick` `status=0` 但结果体为空。
* **恢复监听 ≠ 恢复数据**：本轮重新拉起 XtModel 让 58670 再次 LISTENING，`axes` 仍逐位不变。
* **paper 成交不是 live 成交**：PAPER-40 成功仅证明平台侧下单链路；live 发单为零。
* **paper 撤单 503 是刻意契约（不适用）**，不是「撤单失败留挂单」；一律写「真实撤单未验证」。
* 约束全项保持：**未向真实柜台发单**、未改任何配置、未结束客户端进程、未伪造凭证 /
  绕白名单、**未 push**、paper 仅 1 笔、live 零请求；XtModel 按授权拉起 **1 次 / 上限 2 次**，零重启。
* 退出码如实：`tradehours` 2（需人工动作 / 未证实）、`order_smoke paper` 0（下单成功且撤单不适用）、
  `order_smoke --live` 1（前置不满足、未发单），三者含义互不顶替。
* §15.7 / §15.8 / §16 / 两个 §17 均**逐字未改**；本文档保持 **LF**（追加后 CRLF=0 复核）。

**本轮四问（明确作答）**

| 问 | 答 |
| --- | --- |
| 实时行情是否可用 | **否**。大 QMT 通道 `push/pull/tick` 全 false、`blocking_class=quote_channel_no_data`；REST 仅 `source=tencent` 公开源快照可用，不是券商 tick |
| paper 下单是否成功 | **是**。PAPER-40 `filled=100`、EXIT=0；仅 1 笔、零真实资金 |
| 真实撤单是否验证 | **否**。paper 撤单不适用（刻意契约），live 未放行，零真实委托 |
| live 被哪几项挡住 | **3 项**：`/signal/mode=paper`、`trading_enabled=false`、`injected` 缺 `passorder`（`standalone_process`）；第 4 项 broker `connected=true` 已绿 |

## 20 REA 静态逆向：主进程 / miniquote / TradeClient / OrderService 的可对接性

> 编号说明：§19  reserved 给「11:00 第 3 次定时任务与三次对照表」，该轮无产物，故本节跳到 **§20**。
> §15.7 / §15.8 / §16 / 两个 §17 / §18 均**逐字未改**。
> 取证方式：REA `inspect_native_exports` / `inspect_pe_imports` / `inspect_analysis_view` + `Get-Process.Modules` + `netstat`。
> **纯静态 + 只读枚举**：未下单、未撤单、未改任何配置、未拉起或结束任何进程、未注入任何代码。
> 结构化证据：`output/qmt_rea_module_analysis_20261110.json`。

### 20.R0 结论先行（四问）

| 问 | 答 |
| --- | --- |
| 主进程 + miniquote 能否「直接对接」 | **能，但只能走进程间协议，不能走函数级调用**。二者是 EXE、不是库；合法缝隙是本地监听端口与官方 SDK，跨进程调内部函数需注入（本方案明确不做） |
| `TradeClient.dll` / `OrderService.dll` 能否直接调用 | **技术上能反解签名，工程上不可取**。仅 MSVC C++ mangled 成员导出（1085 / 446 个），无 `extern "C"` 平坦 ABI，且对象必须由宿主进程登录态初始化 |
| `qmt_work` 应接哪个模块 | **官方 `xtquant` Python SDK**（`xtdata` 行情 + `XtQuantTrader` 交易）—— 而且**项目已经接好了**：`XTPQuantAdapter` + `BridgeAdapter` 子进程桥 + `runtime.py` ABI 选择 + `backend/runtimes/cp311` |
| 能否「直接调用」原生模块 | **不能/不recommend**。`xtpythonclient` 与 `IPythonApiClient` 的 `.pyd` 只有一个真入口 `PyInit_*`，必须 `import` 进 CPython 3.11，**不能** `ctypes.CDLL`；`XtQuantServer*` 的 `handleOrderReq` 是 private 成员，由消息队列循环驱动 |

### 20.R1 运行时模块拓扑（决定性证据）

`Get-Process.Modules` 对活体进程只读枚举，把「静态导出表」和「谁真的加载了它」对齐：

* **PID 23112 `XtItClient.exe`**（20:02:22）：`XtQuantServer_qmt.dll`、`ipcmessagequeue.dll`、`TradeClient.dll`、`OrderService.dll`、`OrderServiceMemory.dll`、`OrderServiceUi.dll`、`QuoteClient.dll`、`BrokerService.dll`、`LinkageTrade.dll`、`GuosenQmtProvider.dll` / `GuosenMsgTemplate.dll` / `GuosenOXData.dll` / `GuosenOXDict.dll`、`DataApi.dll`、`quoter.dll`，以及 **`python36.dll` + `boost_python-vc140-mt-1_58.dll`**。
  ⇒ 全量客户端内嵌 **CPython 3.6 + boost.python(vc140)**；这也解释了策略注入进程为何必须是 py3.6 语法（与本机内置 Python 实测 py3.6.8 一致）。
* **PID 23916 `miniquote.exe`**（父 23112，`linkMiniQuoteHide`）：`XtQuantServer.dll`、`ipcmessagequeue.dll`、`TradeClient_mini.dll`、`OrderService_mini.dll`、`LinkageTrade_mini.dll`、`quoter.dll`。
  ⇒ miniquote 是 **mini 版 `XtQuantServer.dll` 的宿主**，与主进程的 `_qmt` 版是**两套独立栈**，不存在「主进程把交易 DLL 共享给 miniquote」。

同一时刻端口（`netstat -ano -p tcp`）：`0.0.0.0:58600 LISTENING(23112)`、`127.0.0.1:58341 LISTENING(23916)`；**`58610` / `58670` / `58620` / `64000` 全部未监听**。远端 `119.147.202.41:55300`×2（两进程各一条，迅投行情云）、`36.99.48.21:55300`（miniquote HK_L1 选站）、`120.83.10.160:56000`（国信 GTS 交易网关）。

> ★ 关键推论：**miniquote 已经把 `XtQuantServer.dll` 载进内存，但 58610 仍未绑定**（配置 `isAutoBind=0`）。这正是 `xtdata.get_client()` 抛「无法连接行情服务！」、以及 `/market` 系列 `SYN_SENT`/503 的成因——不是「模块不存在」，是「服务未绑定」。

### 20.R2 原生 DLL 导出面：没有 C ABI

| 模块 | sha256（前 16） | bytes | exports | ABI 判读 |
| --- | --- | --- | --- | --- |
| `TradeClient.dll` | `7be24d25e82d083c` | 4,080,640 | **1085** | 全是 MSVC C++ mangled 成员方法 |
| `OrderService.dll` | `2b3a24ca52486ce` | 4,444,160 | **446** | 同上 |
| `QuoteClient.dll` | `1a0951178d05bdc` | 879,104 | **183** | 同上 |
| `XtQuantServer.dll` | `0785cff18fbd7729` | 294,400 | **100** | `xtquantsdk::XtQuantServer` / `XtQuantServerTradeAdaptor` |
| `XtQuantServer_qmt.dll` | `7fa395d50888f998` | 174,080 | **72** | 同上（full 版） |

`XtQuantServer.dll` 的关键导出（**`AEAA` 前缀 = private 非静态成员**）：

```
?handleOrderReq@XtQuantServer@xtquantsdk@@AEAAXPEBU_OrderStockReq@2@_KPEBX@Z
?handleCancelReq@XtQuantServer@xtquantsdk@@AEAAXPEBU_CancelOrderStockReq@2@_KPEBX@Z
?handleConnectReq@XtQuantServer@xtquantsdk@@AEAAXPEBU_ConnectReq@2@_KPEBX@Z
?handleQueryStockAssetReq@XtQuantServer@xtquantsdk@@AEAAXPEBU_QueryStockAssetReq@2@_KPEBX@Z
?handleStartQuantServer@XtQuantServer@xtquantsdk@@AEAAXPEBU_StartQuantServer@2@_KPEBX@Z
?start@XtQuantServer@xtquantsdk@@QEAAXXZ
?sendData@XtQuantServerTradeAdaptor@...@@AEAA_NIPEBX_K1W4MessageAction@ipmq@base@@1_N@Z
```

`XtQuantServer_qmt.dll` 从 `ipcmessagequeue.dll` 导入 `MessageQueueWriter` / `MessageQueueReader` / `resumeOrCreateIfNotExist` / `setHandler` / `start`。
⇒ **内部真实通道是共享内存消息队列**；`handleOrderReq` 是消息循环分派的私有处理器，不是对外可调用的下单 API。`setFuncTable` 的签名里带 `std::map<std::string, boost::function<BSONObj(...)>>`、`init()` 带 `std::string` / `std::unique_ptr`，全是**编译器版本强绑定**的 C++ ABI。

据此判「直接调用」的三重代价：① 必须 vc140/MSVC 同工具链且对象布局逐字一致；② 需要宿主进程内已登录的账户/会话上下文（另起一个进程 `LoadLibrary` 只会得到一套未认证的空栈）；③ 绕过消息循环等于绕过回调、风控与状态机。**REA 能反解签名，不能替代运行态上下文** ⇒ 不新增原生 ABI 适配器。

### 20.R3 官方 SDK 的 `.pyd`：只能 import，不能 ctypes

| 模块 | sha256（前 16） | exports | 唯一真入口 | 其余 |
| --- | --- | --- | --- | --- |
| `xtquant/xtpythonclient.cp311-win_amd64.pyd` | `bfefebaa08f2566` | 126 | `PyInit_xtpythonclient` | 125 个 pybind11 异常类/辅助符号 |
| `xtquant/IPythonApiClient.cp311-win_amd64.pyd` | `eac51f9dedb8a44` | 129 | `PyInit_IPythonApiClient` | 122 个 pybind11 符号 + 6 个 `libiconv` |

导入表实证：

* `xtpythonclient.pyd` → **`python311.dll`**（约 120 个 `Py*` API）、`KERNEL32.dll::CreateFileMappingA` / `MapViewOfFileEx` / `PostQueuedCompletionStatus`（共享内存 + IOCP 的客户端侧）、`WS2_32.dll`、`MSVCP140.dll`。
* `IPythonApiClient.pyd` → `python311.dll`、`WS2_32.dll::WSARecv/WSASend/WSASocketW`、`LIBEAY32.dll`、`SSLEAY32.dll`、`log4cxx.dll`（自带 TCP/SSL 栈，对应 58610 RPC）。

⇒ 二者是 **CPython 3.11 扩展模块**：`PyInit_*` 返回 module 对象，用 `ctypes.CDLL(...).func()` 调用既取不到类也拿不到 GIL 语义。正确用法只有 `import xtquant.xtdata` / `from xtquant.xttrader import XtQuantTrader`。
两套安装都提供 `cp36/37/38/39/310/311` 的 `.pyd`（gd_qmt 另有 `cp312/cp313` 的 `datacenter`）；项目 `backend/runtimes/cp311` + `runtime.py::detect_xtquant_abis()` 正好命中，**ABI 不是当前阻塞点**。

### 20.R4 SDK 两端各自落在哪个模块

**行情侧**

```
xtquant.xtdata (Python)
  -> xtdata.ini  [client_xtdata] address=127.0.0.1:58610
  -> IPythonApiClient.cp311-win_amd64.pyd (TCP/BSON RPC client)
  -> 58610 server_xtquant / server_ipythonapi (isAutoBind=0)
  -> miniquote.exe 内 XtQuantServer.dll (PID 23916)
```
`xtdata.py::get_client()` 失败即抛「无法连接行情服务！」；当前 **58610 未监听**，`get_full_tick` / `subscribe_quote` / `get_market_data` 全部无从谈起。

**交易侧**

```
xtquant.xttrader.XtQuantTrader(path, session, callback)
  -> XTQC.XtQuantAsyncClient(path.encode('gb18030'), 'xtquant', session)
  -> xtpythonclient.cp311-win_amd64.pyd
  -> ipcmessagequeue.dll 共享内存（path = <root>/userdata 或 userdata_mini）
  -> XtQuantServer_qmt.dll (full, PID 23112) / XtQuantServer.dll (mini, PID 23916)
  -> TradeClient.dll / OrderService.dll -> GuosenQmtProvider -> GTS :56000
```
回调面 `XtQuantTraderCallback`：`on_stock_order` / `on_stock_trade` / `on_stock_asset` / `on_stock_position` / `on_order_error` / `on_cancel_error`。
注意 `path` 是**目录**不是网络地址——`XtQuantTrader` 靠本地 userdata 里的共享内存队列与客户端握手，这也说明为什么 `client_path` 的 full/mini 分派（`adapter.py` 已实现）不能写死。

### 20.R5 `qmt_work` 现状盘点：官方链路已存在

| 层 | 文件 | 事实 |
| --- | --- | --- |
| 适配器 | `backend/xtquant_client/xtp/adapter.py::XTPQuantAdapter.start()` | `import xtquant.xtdata` + `_load_trader_api()` → `XtQuantTrader`，含新旧 `xt_trader`/`xttrader` 兼容 |
| 子进程桥 | `bridge_client.py::BridgeAdapter` + `bridge_server.py` | 把原生 SDK 隔离到独立进程，NDJSON RPC 回传；正是为了躲开「pyd ABI + 全局 writer 池」 |
| ABI 选择 | `runtime.py::detect_xtquant_abis()` / `select_runtime()` | 扫 `.cpXXX-win_amd64.pyd`，与 `backend/runtimes/cp311` 匹配 |
| 一次性诊断 | `scripts/probe_xtquant_once.py` | 区分 `WaitingFreeWriter` 是重试自伤还是券商侧限制 |
| 只读旁路 | `connectors/transports/xtq_rpc.py::XtqRpcTransport` | 58600/58670 原始 NetPackage/BSON；`_fail` 明确写「**58600/58670 实测无任何下单方法**」 |

⇒ **不需要新增任何原生模块对接层**。当前该做的是把「为什么连不上」拆成可证伪的三态，而不是换通道。

### 20.R6 建议改动（分析结论，未实施、未改代码）

1. **保持** `XTPQuantAdapter` + `BridgeAdapter` 为唯一生产链路；明确否决 `LoadLibrary`/`GetProcAddress` 直调 `TradeClient.dll`、`OrderService.dll`、`XtQuantServer*.dll`，以及手写共享内存 IPC。
2. 交易时段验证器（`scripts/qmt_tradehours_validate.py`）增加 **`channel_58610_listening`** 轴：`wanted` 已含 58610，但 `axes` 未单列，导致 `xtdata` 失败被笼统归到「接口不通」。应精确归因为「服务未绑定」。
3. 诊断三态分列、互不顶替：`sdk_import_ok`（cp311 `.pyd` 可 import）/ `xtdata_service_bound`（58610 LISTENING）/ `trader_session_ok`（`connect() == 0` 且 `path` 指向正确 userdata）。「没查」不得写成「查过且干净」。
4. `XtQuantTrader` 的 `path` 继续按 full=`userdata` / mini=`userdata_mini` 分派；两套安装均已存在 `userdata_mini` 目录，可作为下一轮只读探针的目标。
5. 58600 旁路**永久标注只读诊断**：`getFullTick` / `subscribeQuote` 已实测 `ErrorID=200005`，不得承载下单、不得当行情主干。
6. **不得**为了绑 58610 去改 `isAutoBind` / `broker.ini` / `Config.xml`（约束：配置改动需用户批准）；是否开启 mini 行情服务属人工动作。

### 20.R7 约束如实性

* 未向真实柜台发单、未撤单、未伪造凭证、未绕 PID 白名单、未拉起/结束客户端进程。
* 20:02 主进程 `LOGIN account 410001005814 status=0` 已证；§17/§18 的 `account is expired` **仅属 XtModel/58670 支线**，本分析不外推、也不回改既往章节。
* live 四道前置仍缺 3 项（`/signal/mode=paper`、`trading_enabled=false`、`standalone_process` 缺 `passorder`）；全量客户端 `passorder` 注入需在 QMT GUI 切换策略形态（加密注册树，不可自动化）。
* 本节只回答「能否对接 / 接哪个模块」，**没有改动任何代码、测试、门禁计数或配置文件**；文档保持 LF。

## 21 官方 xtquant 分层取证（2026-11-10 20:55–21:05）：不是「不能用了」，是外部进程被 PID 鉴权拒

> 用户问：「官方 xtquant 现在不能用了？是否有办法下单？继续深度分析下」。
> 本轮**纯只读**：0 次下单 / 0 次撤单、未改任何配置或生产代码、未拉起或结束任何进程、未绕 PID 白名单、未伪造凭证。
> 结构化证据：`output/qmt_xtquant_deep_verdict_20261110.json`（schema `qmt_work.xtquant_deep_verdict.v1`）。

### 21.R0 结论先行（三问三答）

1. **「官方 xtquant 不能用了」是过宽的判定。** `xttrader` 可导入、下单 API 面齐全、cp311 `.pyd` ABI 匹配、客户端 `XtQuantServer` 已启动且**确实收到了我们的连接**。坏的只有三处：guoxin 当前 `xtdata.py` 与 `.pyd` 版本错配、58610 服务未绑定、外部进程被 PID 鉴权拒绝。
2. **本轮没有任何一条真实下单路径被证实可用。** 外部直连（路径 A）被券商/客户端授权面挡住，且按硬约束**不得绕过**；结构上唯一不受该限制的是客户端内置策略（路径 B/B′），但需 GUI 人工部署 + 四道 live 前置全绿，本轮未验证、未发单。
3. **平台自己的诊断看不见这个根因。** `xtp/diagnostics.py` 匹配不到真实日志措辞 `illegal pid 21664`，会返回**空诊断**，再退回 `adapter.py` 的四步泛化文案 —— 其中「换 session」已被实证证伪。

### 21.R1 分层实测矩阵（同一句「连不上」下藏五层）

| 层 | guoxin_iquant_11 | gd_qmt | 判读 |
|---|---|---|---|
| `xtquant.xtdata` import | **FAIL** `cannot import name 'get_local_data_field_list'` | **OK** 167 exports | guoxin 是 `.py`/`.pyd` 版本错配，非 SDK 整体失效 |
| `xtquant.xttrader` import | **OK** 8 exports | **OK** 9 exports | 交易包装层两边都好 |
| `xtquant.datacenter` | MISSING | **OK** 25 exports（`cp311` pyd） | 只 gd_qmt 有 |
| 下单 API 面 | `order_stock` / `order_stock_async` / `cancel_order_stock` / `cancel_order_stock_async` / `query_stock_orders` / `query_stock_asset` / `connect` / `start` / `stop` 全在 | 同左 | 方法没被删 |
| 58610 `xtdata` 服务 | **NOT LISTENING** | **NOT LISTENING** | `isAutoBind=0`，实时 xtdata RPC 不可用 |
| `connect()` | `rc=-1` | `rc=-1` | 同一个返回码，**三种不同现场**（见 21.R3） |

* guoxin `xtdata.py` 29,807 B（2020 版）需要 `get_local_data_field_list` / `get_local_tick_data_batch` / `get_np_tool`；当前 `IPythonApiClient.cp311-win_amd64.pyd` 只暴露 `IPythonApiClient` / `register_create_nparray` / `rpc_init` ⇒ 缺 3 个名字。
* `xtquant.swapped_20261108_1630/xtdata.py`（52,838 B）在包上下文里 exec **成功**（89 exports、`has_get_full_tick=true`、`data_dir=../userdata_mini/datadir`）；**未**替换客户端安装目录。
* `gd_qmt` 本地历史数据探针报 `ImportError: Missing required dependencies ['numpy','pytz']` ⇒ 属探针 `sys.path` 问题（cp311 运行时实有 numpy/pandas/pytz，`_common.py` 亦预暖），**未复跑**，故本地历史数据能力记为「未证实」，不是「可用」。

### 21.R2 决定性证据：服务端收到并拒绝了我们的连接

`P:\stock\guoxin_iquant_11\userdata\log\XtClient_20261110.log`：

```
L21600 20:53:57,910  Allow the XtQuantServer to start.
L21601 20:53:57,910  [orderservice] start quant server thread [P:\stock\guoxin_iquant_11\userdata]
L21602 20:53:57,916  [orderservice] start quant server v2 thread [P:\stock\guoxin_iquant_11\userdata]
L21804 20:55:57,449  [TC::COrderServiceQuantAdaptor::onConnected] quant session 900001, illegal pid 21664
```

* `900001` 正是本轮 `guoxin/userdata` 探针的 session；`down_queue_900001` 创建于 `20:55:57,416`，与日志相差 **33 ms**；PID `21664` 在探针退出后已消失。⇒ **共享内存 IPC 通、服务端在跑、请求被应用层拒绝**，不是传输层/ABI/依赖问题。
* 当日 `illegal pid` **133 行**（11-08 另有 6 行），而 `pid N not allowed` 格式 **0 行**。08:27–08:50 的 `quant session 0..5, illegal pid 2796` 证明**换 6 个 session 全被同一个 PID 规则拒绝** ⇒ `adapter.py` 四步里的「③ 换 session」在本机无效。
* **纠正一处易错归因**：`CIPCManager::init, not auth:mdl_auth_gt_ipc_pair` 今天确实出现（08:04 / 08:53 / 20:02 / 20:53），但每次都紧跟 `Allow the XtQuantServer to start.`；全日志 `The XtQuantServer is not allowed to start.` **0 命中**。⇒ 不能把前者当成「量化服务没被授权启动」，那会误导到「申请模块授权」方向。

### 21.R3 同一个 `rc=-1` 的三种现场（必须分开报）

| 目标 | 真因 | 证据 |
|---|---|---|
| `guoxin/userdata` | **应用层 PID 鉴权拒绝** | 见 21.R2：服务端在跑 + `onConnected illegal pid` + 会话队列被创建 |
| `guoxin/userdata_mini` | **该数据目录没有交易侧 quant server** | 目录内**无** `up_queue_xtquant` / `up_queue_win_xtquant`；miniquote 日志只有行情连接；`down_queue_900002` 由探针自身创建 |
| `gd_qmt/userdata_mini` | **探针时点根本没有客户端在跑** | `P:\stock` 下只有 guoxin 的 4 个进程；gd_qmt mini 日志最后写入 2026-09-29；`down_queue_win_910001` 由探针自身创建 |

### 21.R4 授权串实证（登录时服务端下发，L97，49,529 字符）

```
mdl_auth_xtquant = 1                              (交易模块有授权)
mdl_auth_xttrader_strict_connection_check = 0
mdl_auth_xtdata_strict_connection_check   = 0
mdl_auth_xtquant_strict_connection_check = 0
mdl_auth_xtquant_no_pid_check            = 0     (0 = PID 校验开启)
mdl_auth_gt_ipc_pair                     = 0
mdl_auth_ipython = 1   mdl_auth_use_cpp_sdk = 1  mdl_auth_python_anti_plugin = 1
mdl_auth_login_lite_mode                 = 0     (极简模式未开)
mdl_auth_allow_system_strategy_trade     = 0     (券商语义未核实，见 21.R8)
```

⇒ 模块授权在、三个 `strict_*` 全 0，但 `no_pid_check=0` 使 PID 校验生效，与 `illegal pid` 现场自洽。**这些是实证值，不是对券商授权策略的解释**；`allow_system_strategy_trade=0` / `trader_use_api=0` 的含义尚未核实，故路径 B 不写「已绿」。另 L22055 `AccountModelAuthority:true`（但 `m_bStartupAutorun:false`、`automatic run fail`）是一个有利但**不充分**的信号。

### 21.R5 我们自己的诊断会返回空（缺陷，本轮未修）

`backend/xtquant_client/xtp/diagnostics.py`：

```python
# L389
found = _scan_marker_lines(p, (_SERVER_BLOCKED_MARKER, "connect ret error", "not allowed"))
# L114
_PID_DENY_RE = re.compile(r"\bpid\b[^\n]{0,40}not allowed", re.IGNORECASE)
```

真实行是 `illegal pid 21664`：**三个 marker 一个都不含**（连 `not allowed` 都没有），所以根本进不了 `denies`；`_PID_DENY_RE` 又硬性要求字面量 `not allowed`。四个分支逐一落空：

| 分支 | 判据 | 本现场 |
|---|---|---|
| `xtquant_server_blocked` | `The XtQuantServer is not allowed to start.` | 0 命中 |
| `pid_not_allowed` | `pid ... not allowed` | 真实措辞是 `illegal pid` |
| `strict_check` | `strict_xttrader == 1` | 实际 `=0` |
| `xtquant_module_unauthorized` | `xtquant_auth == 0` | 实际 `=1` |

⇒ `res["reason"]` 保持空 → L429-430 提前 return → **无 title / 无 evidence / 无 steps**；上层退回 `adapter.py` 的泛化四步（极简模式 / 路径 / session / 权限），用户会被推去做已证伪的无效重试，还会白烧 45 s 重试预算。这是本项目反复出现的「**诊断自己说谎**」家族，但**本轮为只读分析，未改生产代码**。

### 21.R6 下单路径逐条裁决

| 路径 | 状态 | 依据 |
|---|---|---|
| **A** 官方 `XtQuantTrader` 外部直连 | **BLOCKED（已证）** | PID 鉴权拒绝；需券商侧加白名单或下发 `no_pid_check=1`，平台不得改写、不得绕过 |
| **B / B′** 内置策略 `passorder`（`agent_bigqmt`） | **设计上不受 PID 限制，但本轮未证实** | 客户端自启 `pythonw.exe`（pid=24232, ppid=17040）属合法宿主进程；但需 GUI 公式部署（加密注册树，不可自动化）+ 四道 live 前置全绿；后端三个端点本轮 **502**，`/signal/mode`、`trading_enabled`、`passorder injected` **均未复核** |
| **C** DBF 文件单 | **BLOCKED（配置）** | 四个安装 `is_automonitor=0`；改配置需用户批准；且 2026-10-23 起文件 IO/外联被关，B′ 才是合规形态 |
| **D** 58600 / 58670 原始 RPC | **死路** | `xtq_rpc.py` 明确记录实测无任何下单方法，且约定只读诊断 |
| **E** 58620 `server_tradecom` | **未监听** | `isAutoBind=0`，无 qmt_work 可用 API，未验证任何下单语义 |
| **F** 直调 `TradeClient/OrderService/XtQuantServer*.dll` | **不采纳** | §20 已证全是 MSVC C++ mangled 私有成员方法，无平坦 C ABI |

### 21.R7 建议改动（分析结论，**未实施**）

1. **P0**：扩 `_PID_DENY_RE` 与 marker 集，识别 `illegal pid <N>` 并归入 `pid_not_allowed`；补 3 条纯函数测试锁住新旧两种日志措辞。改用例需同步 `ci_reconcile.EXPECTED_TESTS` + 根 README **两处**硬编码计数。
2. **P0**：`adapter.py` 四步文案在「已实证为 PID 鉴权拒绝」时跳过「③ 换 session」，避免无效重试。
3. **P1**：诊断三态分列 `sdk_import_ok` / `xtdata_service_bound` / `trader_session_ok`（互不顶替），另增 `sdk_variant_compat` 与 `channel_58610_listening` 轴。
4. **P1**：按安装目录 ABI 兼容性选 `xtdata` 变体；**不修改客户端安装目录**，除非用户明确批准。
5. **P2**：路径 B/B′ 部署与 live 四前置复核，需 GUI 人工动作与明确授权。
6. **禁止项**：为绑 58610 改 `isAutoBind` / `broker.ini` / `Config.xml`；伪造凭证；绕 PID 白名单；把纸面成功说成实盘成功。

### 21.R8 约束如实性与「未查」清单

* 未向真实柜台发单/撤单、未绕 PID 白名单、未改配置或生产代码、未拉起或结束任何进程；`connect()` 每条只调一次且 `finally stop()`。
* 现场复核（21:03:16）：58600@17040、58341@23832、8086@17040 LISTEN；58610/58620/58670/64000 全 NOT LISTENING；`P:\stock` 下仅 guoxin 的 4 个进程，**gd_qmt 未运行**。
* 后端 `127.0.0.1:21118` 的 `/api/v1/health`、`/api/v1/qmt-agent/status`、`/api/v1/signal/mode` 全部 **502 Bad Gateway** ⇒ 运行模式与 live 前置**本轮未复核**，不写「已检查且干净」，也不沿用 §18 的旧值冒充本轮实测。
* 其余未查：`allow_system_strategy_trade=0` / `trader_use_api=0` 的券商语义；58610 绑定后实时行情能否取到；路径 B 的 `passorder` 注入与真实撤单；修好依赖注入后本地历史数据能否真取（探针未复跑）。
* §19 仍留给缺失的 11:00 三次对照表，未占用；文档保持 **LF**。

## 22 §21.R7 的 P0 实施与门禁验证（2026-11-10 21:35–21:50）

> 与 §20 / §21 的**纯只读分析不同**，本节记录用户「继续改进」后真正改动了生产代码的一轮。
> 范围严格限于 §21.R7 的两条 **P0** 与第 3 条中的 `channel_58610_listening` 轴；
> 三态诊断 / `sdk_variant_compat` / 路径 B 部署 / live 四前置复核 **仍未实施**。

### 22.R0 约束（与 21.R8 逐条对齐）

* **0 单、0 撤、未连真券商**；未改 `broker.ini` / `Config.xml` / `isAutoBind` / `xtdata.ini` / 任何客户端安装目录；未拉起或结束任何 QMT 进程；未绕 PID 白名单、未伪造凭证。
* 改动仅限 `qmt_work` 仓库内的 Python / 测试 / README / 门禁常量 / 文档。
* 后端 21118 的 `/health`、`/qmt-agent/status`、`/signal/mode` 上一轮为 **502**，本轮**未复核** ⇒ 运行模式、`trading_enabled`、`passorder injected` 一律不写「已检查且干净」。
* **修复对象是我们自己的诊断与文案，不是券商授权**：PID 鉴权拒绝依旧存在，本轮没有、也不允许绕过它。

### 22.R1 P0① `xtp/diagnostics.py`：让诊断看得见 `illegal pid`

| 项 | 改前 | 改后 |
|---|---|---|
| marker 集 | `(_SERVER_BLOCKED_MARKER, "connect ret error", "not allowed")` | 增 `ci_markers=("illegal pid",)` —— 新增**大小写不敏感**扫描通道（`_scan_marker_lines` / `_collect_marker_line` 的 pairs 改三元组，`lower()` 惰性、每行至多一次） |
| PID 判据 | `\bpid\b[^\n]{0,40}not allowed` | 并列 `\billegal\s+pid\b\s*[:=]?\s*\d+`（**PID 允许 0**：cp36/Jupyter 实测上报 `illegal pid 0`） |
| 证据去重 | 无 | `deny_lines = list(dict.fromkeys(denies + illegal_pid))`，一行同时命中两个 marker 只计一次 |
| 输出 | `reason=""` → 无 title / evidence / steps | `reason="pid_not_allowed"` + `session_retry_useless=True` + `pid_deny_wording` |

真实现场回放（`mdl_auth_xtquant=1`、三个 `strict_connection_check=0`、`no_pid_check=0`、日志含 `quant session 900001, illegal pid 21664`）已从「四分支全落空」变为 **`pid_not_allowed` 且证据/处置齐全**。

**护栏（反退化）**：`The XtQuantServer is not allowed to start.` 仍**不匹配**新正则 ⇒ `xtquant_server_blocked` 不会被抢走；测试 `test_illegal_pid_is_case_insensitive_and_accepts_pid_zero` 锁住这一条。

**顺带纠正一处「诊断自己说谎」**：`steps` 首句原硬编码 `strict_connection_check=1、xtquant_no_pid_check=0`，与本现场实证值（三个 strict 全 0）直接矛盾。改为「以日志实证值为准」的开关清单，并把 `pid_not_allowed` 的处置首句单列：换 session 无效、处置只有「券商加 PID 白名单」或「客户端内置 Python 策略通道」；同时明确**不**断言量化服务是否在跑（需另看日志证据）。

### 22.R2 P0② `xtp/adapter.py`：报错按证据改写，不再推荐已证伪的动作

| 位置 | 诊断为空（默认） | `reason == "pid_not_allowed"` |
|---|---|---|
| 四步 ③ | 「换另一个 session_id 再试（间隔 >3 秒）」 | 「**本项已由客户端日志证伪，请勿执行**；换 session 不改变被拒绝的 PID」 |
| 四步前 | 无 | 加注「①/③/④ 均非该根因的处置方向，请以上方『客户端侧根因』为准」 |
| 末尾建议 | 「以极简模式重新登录完整版大客户端」/「运行极速版」 | 改走「### 已证伪的方向」+「券商加 PID 白名单」或「大 QMT 桥接·文件」 |

判据**只认** `_diag["reason"] == "pid_not_allowed"`；诊断抛错 / 返回空时原四步与模式建议**一字不改**（反向锚 `test_trade_failure_keeps_generic_faq_when_diagnosis_is_empty`，同时保住既有 `test_trade_failure_suggestion_follows_primary_mode` 的断言）。

### 22.R3 P1 部分：58610 独立轴

`scripts/qmt_tradehours_validate.py`：`_conclusion().axes` 与 `report["axes"]` 增 `channel_58610_listening`；`report["xtdata_channel"]` 单列归因 —— 未监听写「**服务未绑定**（客户端未启动/未绑 58610），不是行情接口不通」；在监听写「服务已绑定，但**本轮未验证其返回内容，不等于行情已证实**」。该轴**不参与** `env_ok` / `exit_code`（本脚本 0/1/2 只裁决 58600/58670 的行情证据），并有反向测试锁住「58610 单独在监听绝不把未证实的一轮判绿」。

### 22.R4 测试与门禁

* 新增 **7 条**纯函数/替身用例：诊断 3（真实国信措辞 / `illegal pid 0` 与大小写变体 / 新旧措辞并存不重复计数）、adapter 2（证据驱动跳过建议 / 诊断为空反向保留）、验证器 2（58610 独立轴 / 不得假绿）。文件数仍 **220**。
* 定向复跑：`test_qmt_client_log_diagnosis` + `test_qmt_client_mode` + `test_qmt_tradehours_validate` → **73 passed**（cp311）。
* **三处同源计数**：`ci_reconcile.EXPECTED_TESTS` 2952→**2959**（含注释溯源）+ 根 README 第 463 行（带反引号）与第 544 行（不带）同步改 2959；两处都含裸数字 `2952`，`replace_all` 命中 2 处，改完 `grep` 复核。
* 全量门禁 `ci_reconcile.py`：**2959 / 76 / 46 RECONCILE OK**，六项检查全绿（后端用例 / 前端组件 / 注册页 / README 计数 / 前端 API 契约 / 行尾），退出码 0，耗时 385 s。
* Gate 4（backend 非测试 `*.py` ≤ 50KB）：`diagnostics.py` 29,357 B、`adapter.py` 36,054 B，均留足余量。
* 全量 `pytest backend/tests/`：**2949 passed / 10 skipped / 7 warnings，406.19 s，EXIT=0**（2959 总数与门禁常量一致），日志 `output/pytest_full_20261110_gate2.log`。
* **真实日志端到端复验**（比合成用例更硬的证据）：直接对 `P:\stock\guoxin_iquant_11\userdata` 的最新 `XtClient_20261110.log` 调 `diagnose_trade_connect()`，返回 `reason=pid_not_allowed`、`pid_deny_wording="illegal pid"`、`session_retry_useless=True`、`server_blocked/module_unauthorized/strict_check` 全为 `None`，`title` 231 字符 / `steps` 1018 字符 / `evidence` 2 条真实日志行（`quant session 4/5, illegal pid 24620`）。**修复前同一入口对该现场返回空**（无 title/evidence/steps），adapter 只能回落通用四步 FAQ —— 这条复验是本轮「诊断不再说谎」的可证伪判据。
* 生产代码改动后**重跑**全量门禁 `ci_reconcile.py`：仍 **2959 / 76 / 46 RECONCILE OK**，六项全绿，退出码 0，日志 `output/ci_reconcile_20261110_gate2.log`。

### 22.R4b 首轮全量 pytest 的 2 处既有基线违规（非本轮引入）

首轮全量 pytest 报 `2 failed, 2947 passed`，两条都落在 `HEAD` 已存在的代码上，与本轮 P0 改动无关。按「先查是不是自己造的」逐一核实后处理：

1. **`test_audit_doc_links` 断链 1 处**：本文档 §「QMT 策略落盘」段落里裸写了一个**硬约束索引文件名**（带 `.md` 后缀）。该文件只存在于隐藏的记忆目录 `.workbuddy-ai/memory/` 下，而 `scripts/audit_doc_links.py` **跳过隐藏目录**，所以对产品文档而言它是断链（文件真实存在 ≠ 链接可解析）。改为「与本机内置 Python 实测 py3.6.8 一致」，不再构成路径引用。
2. **`test_clock_unity` 3 处内联时刻生产者**（`HEAD` 即违规，非本轮新增）：

   | 位置 | 原写法 | 改后 |
   |---|---|---|
   | `app/routes/qmt_client.py:494` | `_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.000")` | `local_now().strftime(...)` |
   | `app/routes/qmt_client.py:551` | `_dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")` | `local_now().strftime(...)` |
   | `connectors/qmt_file_order.py:386` | `datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.000")` | `local_now().strftime(...)` |

   格式串**逐字不变**，`core.clock.local_now()` 本体就是 naive `datetime.now()`，属行为等价重构（`INSERTTIME` 与 DBF 文件名语义零变化，相关断言未改）。改动后定向 **185 passed**、全量 **2949 passed / EXIT=0**。

**刻意不动**：`connectors/qmt_file_order.py` 的 `datetime.date.today()`（DBF 头部日期）未改 —— 该测试的正则是 `(?<![\w.])date\.today\(`，负向后顾排除 `.` 前缀，`datetime.date.today()` 本就不命中；没有证据表明它是缺陷，按「判断 ≠ 缺陷」不 churn。

### 22.R5 仍未解决（不得与「已修」混写）

1. **重试控制流未改**：PID 鉴权已证时仍会跑完 6 session × 2 目录、`_CONNECT_RETRY_BUDGET=45s`；本轮只改文案，提前收敛记为 P1（改动循环需配合 `diagnose_trade_connect` 的 64MB 日志扫描预算，避免把连接路径拖慢）。
2. 诊断三态 `sdk_import_ok` / `xtdata_service_bound` / `trader_session_ok` 与 `sdk_variant_compat` 轴未做；国信 `xtdata.py`(29,807B/2020) 与 cp311 `.pyd` 的名字错配**未修**（不修改客户端安装目录，需用户明确批准）。
3. 11:00 定时任务静默失败未定位，§19 继续留空。
4. 路径 B/B′ 的 GUI 公式部署、live 四前置复核、真实撤单均未证实；58610 仍未监听，未改任何客户端配置。
5. **当前被证实的真实下单路径仍是 0 条**。本轮修复的是「诊断说谎」，不得把纸面/诊断成功说成实盘可用。

### 22.R6 产物

结构化证据 `output/qmt_pid_diagnosis_fix_20261110.json`（schema `qmt_work.pid_diagnosis_fix.v1`，13 键 / 5 项改动 / 6 条未修，含 `verification.post_gate_baseline_repairs`）；门禁日志 `output/ci_reconcile_20261110_gate.log`、`output/ci_reconcile_20261110_gate2.log`（改后重跑）、`output/pytest_full_20261110_gate.log`（首轮 2 failed）、`output/pytest_full_20261110_gate2.log`（终态 2949 passed）。文档保持 **LF**。

## 23 §22.R5 的 P1 落地：重试早停 + 三轴证据（2026-11-10）

### 23.R0 约束与边界

本轮只改**诊断路径的代码**，不做任何交易侧动作：

* 0 单 0 撤；未启动/结束任何客户端进程；
* 未改 `broker.ini` / `Config.xml` / `isAutoBind` / `xtdata.ini` 或任何客户端配置；
* 未绕过券商 PID 鉴权、未伪造凭证、未改白名单；
* **本轮改的是「诊断的速度与诚实度」，不是下单能力**。

### 23.R1 P1-① 重试早停：为什么必须先解决「归属」

现象：PID 鉴权已实证拒绝时，`start()` 仍会跑完 6 session × 2 目录（最多 12 次 `connect()`、最长 `_CONNECT_RETRY_BUDGET=45s`）。换 session 不改变被拒绝的 PID，所以这 45 秒是**纯空烧**，还额外占用 SDK 会话。

直觉做法「连失败就看日志里有没有 `illegal pid`，有就停」是**错的**，会误杀：

1. 客户端日志按天累积（当日实测 17MB+），历史里躺着上一天的 `illegal pid`；
2. 同机可能有别的进程/别的连接也在被拒，拒绝行不一定是**本次**产生的。

因此实现成三段式（`xtp/diagnostics.py`）：

| 环节 | 做法 | 防的是什么 |
|---|---|---|
| 取游标 | `log_cursor(trade_dir)` = (最新日志路径, **当前文件长度**) | 把历史行排除在增量之外 |
| 只扫增量 | `pid_denial_since(cur)` 从游标读到文件尾（上限 256KB），游标随之前进 | 不重复扫描、不漏新行 |
| 归属本次 | 增量里的 `quant session <N>` 必须命中刚试过的 `sid` 才算 | 不把别人的拒绝算到自己头上 |

两条补充规则：

* 增量里**解析不出** `quant session` 的措辞（如 `pid 24620 not allowed`）**仍算拒绝** —— 无法归属 ≠ 不是拒绝；
* 日志被轮转/截断（`size < offset`）时游标失效，从 0 重扫 —— 不能因此永久失明。

`adapter.py` 的接线：`connect` 返回非 0 后立刻 `pid_denial_since(cur, sessions=[sid])`，命中即 `pid_denied_early_stop` 并 break 出双层循环。实测效果：**12 次 → 1 次**。

★ 顺带修掉一处「说大话」：旧文案无论跑没跑完都写 `session_id 0~5 均 rc=-1`。早停时我们**没跑完**，那句就是把「没试」说成「试过」。现改为「已尝试 N 次后**提前终止**（原因…）」并附「提前终止的依据」日志行；未早停时才保留原句。

### 23.R2 P1-② 三轴证据：互不顶替

现场最容易犯的两类误读：

* 把「交易 `connect` 返回 rc=-1」读成「行情接口不通」；
* 把「58610 没监听」读成「xtquant 整套不能用」。

三者是**独立通道**，故 `diagnose_tri_state()` 并列输出三轴：

| 轴 | 取值 | 取证方式 |
|---|---|---|
| `sdk_import_ok` | `ok` / `failed` / `unchecked` | 调用方上报（本路径能走到 `connect` 说明导入已成功，故保持 `unchecked` 而非伪填 `ok`） |
| `xtdata_service_bound` | `bound` / `unusable` / `unknown`（附 `cause`） | 对 `127.0.0.1:58610` 做一次 TCP connect（0.3s 超时，不发协议帧） |
| `trader_session_ok` | `ok` / `failed` / `unchecked` | `connect()` 返回值 |

★ **`refused` 与 `timeout` 必须分开**（`probe_xtdata_service` 的 `cause`）：

* `ConnectionRefusedError` ⇒ 可断言「服务未监听」；
* `TimeoutError` ⇒ 只能断言「端口不可用」，**不能**断定客户端未启动 —— 防火墙丢包、backlog 打满都表现成超时。本机 58610 实测就是 **timeout**，若按 refused 写文案就是新的「假告状」。

★ 未检查恒为 `unchecked`：任一轴为 `ok`/`failed` 都不得推断其余两轴。

### 23.R3 删掉一句无条件假话

`_diagnose_one` 的 `steps` 末句原文：

> 注意：本问题**只影响交易通道**，行情（xtdata）不受影响，行情功能可继续使用。

而实测 58610 **不可达** ⇒ 这句是假话。现改为：

> 注意：本条只裁决**交易通道**；行情是另一条独立通道（官方 xtdata 走 127.0.0.1:58610），本诊断**未**验证行情侧，不能据此断言行情可用或不可用。

再由 `_attach_tri_state` 在实测 `unusable` 时补一句**有证据**的结论（`refused` 与 `timeout` 分开措辞）；`bound` 时**不补**、也不改写成「行情可用」。

### 23.R4 测试与门禁

* 新增 **12 条**用例（全部追加在既有文件内，`test_*.py` 仍 **220** 个）：
  * `test_qmt_client_log_diagnosis` **+9**：游标=尝试前文件长度故历史行不进增量 / 别的 session 的拒绝行不得早停（且不限 session 时反向成立）/ 无 session 标记仍算拒绝 / 日志截断时重扫 / 无日志不抛异常 / refused 与 timeout 的措辞分叉 / 三轴互不填平 / 行情侧结论只在 `unusable` 时补写 / 旧假话必须消失但「只裁决交易通道」边界保留；
  * `test_qmt_client_mode` **+3**：证到 PID 拒绝只试 1 次且不写「均失败」/ **无证据时必须照旧跑满 12 次组合**（反向锚，不得凭猜测早停）/ 三轴并列渲染。
* 三处同源计数：`ci_reconcile.EXPECTED_TESTS` 2959→**2971**（含溯源注释）+ 根 README 第 463 行与第 544 行同步改 2971。
* Gate 4（backend 非测试 `*.py` ≤ 50KB）：`diagnostics.py` 39,863 B、`adapter.py` 40,291 B、`env.py` 47,940 B，均有余量。
* 真实日志端到端复验（比合成用例更硬）：对 `XtClient_20261110.log` 调 `diagnose_trade_connect()` ⇒ `reason=pid_not_allowed`、三轴 `unchecked / unusable(timeout) / failed(-1)`、`行情（xtdata）不受影响` 已不存在、末行补出真实的「行情侧实测」结论。

### 23.R5 仍未解决（不得与「已修」混写）

1. 券商侧 PID 鉴权拒绝**本身未变**（未改、未绕过）；交易仍连不上。
2. 58610 仍未监听；`isAutoBind` 等客户端配置一个没动。
3. 国信 `xtdata.py`（29,807B / 2020 版）与 cp311 `.pyd` 的名字错配**未修**（不动客户端安装目录，需用户批准）。
4. `sdk_variant_compat` 轴未做：要证明「客户端安装里 `.py` 与 `.pyd` 是否配套」，需在不污染当前进程的前提下验证 `.pyd` 的名字面，方案未定。
5. 11:00 定时任务静默失败未定位，§19 继续留空。
6. 路径 B/B′ 的 GUI 公式部署、live 四前置复核、真实撤单均未证实。
7. **当前被证实的真实下单路径仍是 0 条。**

### 23.R6 产物

结构化证据 `output/qmt_early_stop_tristate_20261110.json`（schema `qmt_work.early_stop_tristate.v1`，13 键 / 5 项改动 / 7 条未修）；门禁日志 `output/pytest_full_20261110_gate3.log`、`output/ci_reconcile_20261110_gate3.log`。文档保持 **LF**。

## 24 交易通道 REA：客户端能下单，但程序化入口在哪（2026-11-11 08:00–08:10）

### 24.R0 本轮问题与约束

用户主张：`P:/stock/guoxin_iquant_11/bin.x64/XtItClient.exe` **现在可以下单**，要求用 REA 分析交易通道、判断能否实现程序化交易。

先把两件事分开，否则一定混淆：

* **GUI 手工下单** —— 人点鼠标，走客户端内部 C++（`OrderService` → `TradeClient` → 国信 GTS）。用户看到的就是这条；
* **程序化下单** —— 外部进程发起，必须有对外入口。

本轮只证第二条，且全程只读：0 单 0 撤、未改任何客户端配置、未启停进程、未绕 PID 鉴权。

### 24.R1 现场：交易侧确实在线（这是用户主张的证据）

| 观测 | 值 |
|---|---|
| 客户端 | `XtItClient.exe` PID **19340**，07:57:36 启动 |
| 柜台连接 | `59.36.9.32:56000`（国信 GTS）**ESTABLISHED** |
| 行情连接 | `119.147.202.41:55300`（迅投行情云 QuoterServer）ESTABLISHED |
| 登录 | 07:58:13 `onTradeServerLoginSuccess`：**trade server login success** |
| 就绪 | 07:58:25 `合约和交易用户数据准备完成，通知登录成功` |
| 账号 | 07:58:27 `refreshAccounts ... account_num = 5` |

⇒ **「现在可以下单」成立**，且是柜台级在线，不是缓存态。

### 24.R2 下单的真实形态：一条 BSON-RPC 命令，不是独立接口

`xtquant/qmttools/functions.py` 里 `passorder` 的实现只有一句：

```python
client = xtdata.get_client()
client.callFormula(requestid, 'passorder', bson.BSON.encode(data))
```

调用链：`ContextInfo.passorder` → `_passorder_impl(..., this.request_id)` → `IPythonApiClient.callFormula`。

**这条结论很重要**：既然下单和查询共用一条命令总线，那就可以用**只读的 `gettradedetail`** 去判定「总线通不通」，完全不必投单。本轮正是这么做的。

### 24.R3 端口矩阵：唯一开着的门不承载交易

| 端口 | 角色 | 监听 | 能否下单 |
|---|---|---|---|
| 58600 | `server_iPythonNotebook` / tag=`server_formula` | **是** | **否（已枚举证明）** |
| 58610 | `server_xtquant`（官方 xtdata 目标） | 否 | 未知 |
| 58620 | `server_tradecom`（`isAutoBind=0`） | 否 | 未证实 |
| 58670 | XtModel 分支 / `config/xtdata.ini` 目标 | 否 | 未证实 |
| 64000 | `server_miniqmtdata` | 否 | — |
| 63100 | `brokerserverport`（`brokerserverrun=1`） | 否 | — |

58600 是**唯一**活着的端口，所以必须把它证死。方法：先校准「不存在」的判据 —— 用一个显然不存在的名字探，得到 `ErrorID=200005 未找到处理函数`；再用同一判据扫全部候选。

结果（`output/probe_58600_surface_result.json`）：

* **存在 5 个**：`getDividFactors`、`getInstrumentDetail`、`getMarketData`、`getStockListInSector`、`getTradingDates`；
* **不存在 19 个**：含 `callFormula`、`gettradedetail`、`get_trade_detail`、`subscribeQuote`、`getFullTick` 等。

对照锚：同端口 `getInstrumentDetail` 返回 `status=0`（无 `ErrorID`），证明连接与帧格式没问题 —— 所以 200005 是**方法不存在**，不是我们探针写错。

⇒ **58600 上没有任何下单入口**，这是「枚举证明」，不是「没找到」。

### 24.R4 REA 静态：订单核心在哪、卡点在哪

| 二进制 | 关键结构 | 含义 |
|---|---|---|
| `XtQuantServer_qmt.dll`（72 导出） | `handleOrderReq(_OrderStockReq)`、`handleCancelReq`、`handleQuery*`、`handleConnectReq`、`setFuncTable` | 下单处理器**确实存在**，但全是 `AEAA` 私有成员函数，无 C ABI，外部无法直接调 |
| `OrderService.dll`（439 导出） | `COrderFuncService::orderStock`、`COrderService::loadFromConfig`、`COrderServiceQuantAdaptor::onConnected`、`COrderServiceDbfHelper`、`TradeCommandRcvd` | **订单核心**；PID 鉴权与 DBF 通道都在这里 |
| `TradeHub.dll`（50 导出，已加载） | `RZRKTrade::TradeUtils::placeOrder/passOrder/placeCommand/cancleCommand/queryCommand`，BSON-RPC over asio | 交易网关插件，但方向是**出站** |
| `ipcmessagequeue.dll`（97 导出） | `MessageQueueReader`、`MessageHandler::onStart` | xttrader 的共享内存通道 |

`OrderService.dll` 里那句日志把「内部函数」暴露得很清楚：

```text
passorder by percent,accid={0},opType={1},orderType={2},code={3},price={4},percent={5},calcNum={6}
TC::COrderFuncService::orderStock
```

即：所有门最终都汇进 `COrderFuncService::orderStock`。**门可以有很多，闸只有一个。**

### 24.R5 PID 闸门：不只是硬拒绝，还有一套白名单

```text
TC::COrderServiceQuantAdaptor::onConnected(unsigned __int64, int)
  e:/v2.0/src/business/orderservice/corderservicequantadaptor.cpp
  允许：quant session {0}, pid {1} connected
  拒绝：quant session {0}, illegal pid {1}
```

★ 本轮新增发现：`OrderService.dll` 里存在**设计好的授权通道**，不是只有硬拒绝：

* 配置键 `m_bUseWhiteList` / `m_bUsePrevWhitelist` / `importwhitelist`；
* `COrderService::queryFrameworkWhitelist(bool)` + `onQueryFrameworkWhiteList(bool, TTError, vector<...>)`；
* `sigFrameworkWhitelistChanged` / `slotFrameworkWhitelistChanged`；
* `se::DMAWhitelistInfo` / `TradeCommandRcvd::getDMAWhitelistInfo`；
* `CIPWhitelistCollector::slot_readBrokerIniTimer` → `IP white list changed from broker ini!`。

⇒ 合法路径是**把本平台进程纳入白名单**（券商/客户端侧动作），不是改进程名、注入、伪造 PID。后者属禁止行为，本轮未做，也不该做。

### 24.R6 五条门的判定

| 门 | 入口 | 状态 | 卡点 |
|---|---|---|---|
| A | `XtQuantTrader.order_stock`（共享内存） | 关 | `onConnected` 的 PID 鉴权（`illegal pid`） |
| B | `callFormula('passorder')`（58610 总线） | 关 | 58610/58670 均未绑定；58600 已枚举证明无此方法 |
| C | DBF 文件单（`export_data` + `dbf_order_thread`） | 关但**可开** | `is_automonitor=0`（两处配置）；线程未启动（日志 0 条） |
| D | `TradeHub` / `orderplugin.conf`（`doorder=1`） | **不是入站门** | 方向是出站（`send order to tradeModule`），且未连 `127.0.0.1:5000` |
| E | GUI 手工 | 开 | 人点鼠标，不可自动化审计 |

`orderplugin.conf` 实际内容（`doorder=1`）：`serverip=127.0.0.1` / `serverport=5000` / `user=123` / `pass=123`。它读的是 `[Global]`，配 `[order] socket`，用 BSON-RPC 连出去 —— **是「把单发出去」的插件，不是「把单收进来」的入口**。这一点必须说清，否则很容易被误当成下单通道。

### 24.R7 结论与仍未证实

* **客户端现在能下单** —— 成立，GUI 路径，柜台在线、5 账号就绪。
* **程序化下单当前不能** —— 三条候选门全关，卡点已精确到函数与配置项。
* **最现实的一条** —— DBF 文件单：唯一入站、且不依赖 PID 白名单，但要开 `is_automonitor`，**须用户批准**。
* **被证实的真实下单路径数量：0 条。**

未证实（不得写成已解决）：58610 绑定后是否同样被 PID 闸拦；58620 tradecom 的协议与鉴权；框架白名单的写入流程；DBF 开启后券商端是否接受。

证据：`output/qmt_trade_channel_rea_20261111.json`（schema `qmt_work.trade_channel_rea.v1`，14 键 / 5 门 / 6 结论），探针与静态分析产物见 `output/probe_trade_bus_58600_result.json`、`output/probe_58600_surface_result.json`、`output/rea_trade_channel_static.json`、`output/rea_orderservice_strings.json`、`output/rea_tradehub.json`。

## 25 文件单通道落地：把「猜的列」换成「有出处的列」（2026-11-11 08:1x）

### 25.R0 触发与约束

用户要求「继续落地实现」。上一节（§24）的结论是：五条门里只有 DBF 文件单是
**入站且不依赖 PID 白名单**的一条，但它卡在 `is_automonitor=0`。

本轮**不动客户端配置**，只把 qmt_work 侧这条通道从「库代码」变成「有出处、
有前置链、有安全不变量」的可交付能力：

* 0 单 0 撤；未改 `Config.xml` / `broker.ini` / `is_automonitor` 或任何客户端配置；
* 未启停进程；未往任何监控目录写入文件；
* 保留 `stage` 端点「**故意不提供** `allow_monitor_dir`」的既有设计（§刻意不动）。

### 25.R1 二进制实证：15 个列名与列序

此前 `DBF_COLUMNS` 的依据只有一句「实测顺序」。本轮在 `OrderService.dll` 里
找到**连续**的 15 个列名字符串，区间 `0x3100F8`–`0x3101C4`：

```text
ORDER_TYPE PRICE_TYPE MODE_PRICE STOCK_CODE VOLUME ACCOUNT_ID ACT_TYPE
BROKERTYPE STRATEGY NOTE TRADEPARAM INSERTTIME BASKETPATH EXTRAPARAM NOTE1
```

紧随其后就是 `TC::CDbfStrategyRecord::setColumnParam` 与
`[dbfStrategy [{0}] init [{1}] column Failure!` —— 说明这段数组就是列参数源。

落地方式：新增 `CLIENT_DBF_COLUMNS_BINARY`（**另抄一份**，不直接复用
`DBF_COLUMNS`）+ `CLIENT_DBF_COLUMNS_EVIDENCE`（记录二进制、偏移、相邻符号），
并用测试断言两者相等。**另抄一份的理由是「同源即同错」** —— 若断言写成
`DBF_COLUMNS == DBF_COLUMNS`，有人重排列表时测试永远绿。

### 25.R2 顺带查出一处真实分歧：INSERTTIME 的格式

`OrderService.dll` 的 `0x310418` 处格式串原始字节是：

```text
yyyy-MM-ddHH:mm:ss.zzz          ← dd 与 HH 之间没有空格（22 字符）
```

而本模块写的是带空格的 23 字符（`%Y-%m-%d %H:%M:%S.000`）。

这不是小事：`COrderServiceDbfHelper::checkInsertTimeInvalid` 存在，说明客户端
**确实会校验 INSERTTIME**。若它按上面那串解析，我们的时间戳会解析失败。

但该串也可能只用于日志 —— 它紧邻 `read {0} rows from {1} lastModify {2}`。

**处置：不改默认值，只把口径收成单点常量 `INSERT_TIME_FORMAT`，并把分歧
写成 `INSERT_TIME_FORMAT_EVIDENCE`（`status="unresolved"`）+ 测试。**
理由：没有 armed 状态实测就改，等于把「一种可能错」换成「另一种可能错」，
还顺手丢掉当前行为的可追溯性。等真机验证后再定，届时改一个常量即可。

### 25.R3 真机事实（只读读出，不是推断）

| 安装 | 账号 | `monitor_path` | `is_automonitor` |
|---|---|---|---|
| 国信 | **15624979679** | `P:/stock/guoxin_iquant_11/export_data` | `0` |
| gd_qmt | 22453951 | `P:/stock/gd_qmt/export_data` | `0` |

两个监控目录**都是空的**（无模板 DBF），`batchOrder\orderFile.csv` 也不存在，
`key_enable=False`。⇒ 没有模板可抄列宽，只能走 `DEFAULT_WIDTHS` 保守列宽；
但**列名与列序现在有二进制出处**，这一点不再是猜的。

### 25.R4 落地：readiness 前置链

`armed` 只是总开关，不是全部前置。新增 `file_order_readiness(userdata, *,
client_running=None)` 与只读路由 `GET /qmt/file-order/readiness`，把七步摊开：

| 步骤 | 判据 | 是否阻断 |
|---|---|---|
| `config` | 能解析出 `<DbfMonitorSetting>` | 是 |
| `armed` | `is_automonitor=1` 且 `monitor_path` 非空 | 是 |
| `monitor_dir` | 目录存在 | 是 |
| `writable` | `os.access(W_OK)` | 是 |
| `template` | 监控目录里有无 DBF 可作列宽模板 | 否（只告警） |
| `client` | 调用方上报的运行态 | 否 |
| `insert_time_format` | 口径是否已裁决 | 否（只告警） |

输出 `ready` / `blocking_step` / `blocking_detail`，并在 `honesty` 里写清
「`ready=True` 只表示客户端侧前置齐全，不代表该通道已被证实能成交」。

★ **最关键的一条安全设计：readiness 不做写探测。**
验证「目录可写」最直觉的做法是往监控目录写个探针文件 —— 但 **armed 时写文件
就等于下单**。所以只用 `os.access` 判只读权限，并在返回值里给
`probe_write_skipped=True` 明示「没试过」，不把「没试过」说成「试过且没问题」。
配套两条测试（函数级 + 路由级）断言调用后监控目录**仍然是空的**。

### 25.R5 测试与门禁

* 新增 **12 条**用例（都追加在既有文件内，`test_*.py` 仍 **220** 个）：
  * `test_qmt_file_order` **+7**：列名/列序锁二进制出处；INSERTTIME 单点且分歧被记录；
    `armed=0` 时目录齐备也不许 ready；`armed=1` 且目录存在才 ready（honesty 边界仍在）；
    readiness 绝不往监控目录写探针；目录不存在卡在 `monitor_dir`；未上报客户端运行态只能写「未检查」。
  * `test_qmt_client_routes` **+5**：readiness 路由的 `blocking_step` / `ready` /
    client 未检查 / 路由层不写监控目录 / 空 roots 提示。
* 三处同源计数：`ci_reconcile.EXPECTED_TESTS` 2971→**2983**（含溯源注释）+
  根 README 第 463 行与第 544 行同步改 2983。
* Gate 4（backend 非测试 `*.py` ≤ 50KB）：`qmt_file_order.py` 36,011 B、
  `qmt_client.py` 31,660 B，均有余量。

### 25.R6 仍未解决（不得与「已做」混写）

1. `is_automonitor` **仍是 0** —— 文件单当前**依然不能产生委托**；开它需用户批准。
2. INSERTTIME 格式分歧**未裁决**（需 armed 实测）。
3. `DEFAULT_WIDTHS` 仍是保守列宽，无真机模板校准（监控目录为空）。
4. 批量单 CSV 通道字段集仍未解，`implemented=False`。
5. 券商侧 PID 鉴权拒绝未变；`callFormula('passorder')` 仍不可达。
6. **被证实的真实下单路径仍为 0 条。**

### 25.R7 产物

代码：`backend/connectors/qmt_file_order.py`（列名出处 + INSERTTIME 单点 + readiness）、
`backend/app/routes/qmt_client.py`（readiness 路由）、两个测试文件。
证据：`output/qmt_file_order_landing_20261111.json`（schema `qmt_work.file_order_landing.v1`），
门禁日志 `output/pytest_full_20261111_gate1.log`、`output/ci_reconcile_20261111_gate1.log`。

## 26 `COrderFuncService::orderStock` 能不能直接调？（2026-11-11 08:2x）

### 26.R0 结论

**不能。** 不是「难」，是**符号层面就不成立**：它是私有成员函数、需要 `this`、
参数类没有导出构造函数、返回 `void`、单例是进程内的。逐条给证据。

### 26.R1 符号本身说明了什么

`OrderService.dll` 里的精确导出名（未删减）：

```text
?orderStock@COrderFuncService@TC@@AEAAXAEBVCOrderFuncParam@2@AEBVCOrderStrategyParam@2@@Z
```

按 MSVC 修饰规则拆开：

| 片段 | 含义 |
|---|---|
| `?orderStock@COrderFuncService@TC@@` | `TC::COrderFuncService::orderStock` |
| `AEAA` | `A`=__ptr64，`E`=__thiscall，**`A`=private**，`A`=非静态 |
| `X` | **返回 `void`** |
| `AEBVCOrderFuncParam@2@` | `const COrderFuncParam &` |
| `AEBVCOrderStrategyParam@2@@Z` | `const COrderStrategyParam &` |

也就是说：**它连 public 都不是**（对比同库的 `?cancel@COrderFuncService@TC@@QEAAX...`
是 `QEAA` = public）。「直接调用」= 直接调私有成员。

### 26.R2 五条独立的阻断点

1. **访问级**：`AEAA` = private。没有合法调用面。
2. **需要 `this`**：`__thiscall`，必须先拿到 `COrderFuncService*`。
3. **参数造不出来**：`COrderFuncParam` / `COrderStrategyParam` 的导出构造函数数量是 **0**
   （该 DLL 里 68 个相关导出**全部是"接收者"**，没有 `??0COrderFuncParam@TC@@`）。
   这两个类内含 Qt `QString`/`QMap` 与 `boost::shared_ptr`，布局随编译器/STL/boost
   版本变化 ⇒ 手工按偏移拼一个假对象，几乎必然在第一次析构时崩。
4. **单例是进程内的**：`utils::CManualSingletonT<COrderFuncService>::instance()`
   确实是 **public static**（`SA`）且可导出：

   ```text
   ?instance@?$CManualSingletonT@VCOrderFuncService@TC@@@utils@@SAPEAVCOrderFuncService@TC@@XZ
   ```

   但它返回的是**当前进程**里已构造的那一份（`s_pInstance` 是 `boost::shared_ptr`）。
   在**外部进程**里调它，只会拿到自己那份未初始化的空单例。
5. **返回 `void`**：没有返回码。即使注入成功，也**无法从返回值判断下单成功与否** ——
   成败只通过回调/信号送出，而那些回调需要先在宿主里注册。

### 26.R3 顺带确认：官方 API 也是「发出去就不管」

`functions.py::_passorder_impl` 同样是 `return`（None）：

```python
client.callFormula(requestid, 'passorder', bson.BSON.encode(data))
return
```

所以「下单成功」在任何路径上**都不是返回值给的**，必须靠后续
`gettradedetail` / 委托回报来确认。这一点对上层封装是硬要求：
**不能把"函数没抛异常"当成"单已下成"。**

### 26.R4 还存在的问题（分层列清，不混写）

**A. 通道层（能不能发出去）**

1. 58610 / 58670 未绑定 ⇒ `callFormula` 总线不可达；58600 已枚举证明无此方法。
2. 券商侧 PID 鉴权拒绝（`illegal pid`）未变；合法路径是纳入框架白名单。
3. `is_automonitor=0` ⇒ DBF 文件单不产生委托；开启需用户批准。
4. `TradeHub` / `orderplugin.conf` 是**出站**插件，不是入站入口。

**B. 正确性层（发出去会不会错）**

5. `INSERTTIME` 格式分歧未裁决（二进制 `0x310418` 无空格 vs 我们带空格）。
6. `DEFAULT_WIDTHS` 是保守列宽，无真机模板校准（两个监控目录都是空的）。
7. 批量单 CSV 通道字段集未解，`implemented=False`。

**C. 语义层（怎么知道成了）**

8. 下单路径全部 `void` / `None` 返回 ⇒ 必须靠回报确认，不能靠"没报错"。
9. `stage` → 交付仍是人工两步（刻意设计，未改）。

**D. 结论**

10. **被证实的真实下单路径：0 条。**
11. 唯一有「合法授权通道」的是 PID 白名单（`FrameworkWhiteList` / `DMAWhitelistInfo`），
    唯一「入站且不依赖白名单」的是 DBF（需 `is_automonitor=1`）。

---

## 27. PID 闸的授权位找到了：`mdl_auth_xtquant_no_pid_check = 0`

### 27.R0 这一轮要回答的问题

§24 的结论是「合法路径是把本平台进程**纳入框架白名单**」，但留下一个更基础的问题没答：

> `FrameworkWhiteList` 这份白名单，**是谁写进去的**？本机文件，还是柜台下发？

这个问题决定了后面所有动作的性质：

* 如果是**本机文件** ⇒ 存在技术路径（写文件）；
* 如果是**柜台下发** ⇒ 所谓"合法路径"是**业务申请**，任何本地改法都是伪造。

本轮答案是后者，而且找到了那个开关的名字。

### 27.R1 先分清三个 whitelist（混为一谈必然误判）

三者名字都含 `whitelist`，含义完全不同。把它们当成一回事，会得出
「本机已经有白名单了，为什么还被拒」这种自相矛盾的结论。

| 名称 | 管什么 | 归谁管 | 本机证据 |
| --- | --- | --- | --- |
| `FrameworkWhiteList` | **进程/会话**授权：谁可以连上来下单 | 柜台 `ttservice` 下发 | 缓存文件 `whitelist.json`，当前**不存在** |
| `DMAWhitelistInfo` | **账号**可交易的 DMA 标的（能不能买这只票） | 柜台 | `get_DMA_whitelist()` 返回 `canBuy` |
| `blackwhitelist` / `stkwhitelist` | 交易界面里的**股票**黑白名单标记 | 用户本地 | `Config.xml` 第 57 行、第 212 行 |

第二条的公开文档在 `config/fun.xml` 第 5606–5618 行：

```text
get_DMA_whitelist(accountID, strAccountType)
  返回：list，元素为 PythonObj，dir() 可看属性，例如 obj.canBuy
```

注意它的语义是"这个账号能买什么"，**不是**"哪个进程能连上来"。
拿它当授权依据是错的方向。

### 27.R2 关键证据一：授权串里本来就有这个开关

登录成功后，客户端收到柜台下发的授权串（`[auth log] [CProxyClient] receive module auth string`），
共 **1332** 个键。其中与本议题直接相关的：

| 键 | 值 | 含义 |
| --- | --- | --- |
| `mdl_auth_xtquant` | **1** | 量化（xtquant）通道本身是**授权开的** |
| `mdl_auth_xtquant_no_pid_check` | **0** | 「免 PID 检查」这个授权位是**关的** ⇒ 保留 PID 校验 |
| `mdl_auth_xtquant_strict_connection_check` | 0 | 严格连接检查关闭 |
| `mdl_auth_broker_whitelist` | **0** | 白名单模块整体**未启用** ⇒ `whitelist.json` 从来不会生成 |
| `mdl_auth_show_dma_stock_white_list` | 0 | DMA 白名单界面不显示 |
| `mdl_auth_ipython` | 1 | iPython 可用 |
| `mdl_auth_forbidden_python` | 0 | 未禁用 Python |

这解释了两件此前只能"推测"的事：

1. **为什么 `whitelist.json` 不存在** —— `mdl_auth_broker_whitelist=0`，白名单模块没启用，
   根本不会去查询、也不会去落盘。
2. **为什么 PID 校验一定会生效** —— `no_pid_check=0`，校验位是关的。

而且这两个键**只出现在日志里，不出现在任何配置文件里**：

```text
grep -rl mdl_auth_xtquant_no_pid_check userdata/ config/
  → 只命中 XtClient_2026*.log，无任何 ini / xml / json
```

**只由柜台在登录时下发，本地不持久化** —— 这一条直接排除了"改本地配置"这条路：
本地压根没有这个开关可以改，能改的只有日志本身，那是伪造审计。

### 27.R3 关键证据二：回调签名说明数据来自交易服务

`FrameworkWhiteList` 在二进制里的形态是一个**请求-应答**，不是文件读取：

```text
OrderService.dll  @0x2FEEF0  TC::COrderService::onQueryFrameworkWhiteList
OrderService.dll  @0x2FEF20  void __cdecl TC::COrderService::onQueryFrameworkWhiteList(
                                 bool, const utils::TTError &,
                                 const std::vector<boost::shared_ptr<ttservice::IData>, ...>)
TradeClient.dll   @0x2B36B8  TC::CWhiteListManager::onQueryWhiteList
TradeClient.dll   @0x2B36E0  void __cdecl TC::CWhiteListManager::onQueryWhiteList(
                                 const utils::TTError &,
                                 const std::vector<boost::shared_ptr<ttservice::IData>, ...>)
```

三个判据同时指向"柜台给"：

* 参数里有 `utils::TTError` —— 这是**网络应答的错误码**，不是文件 IO 的错误码；
* 数据容器是 `std::vector<boost::shared_ptr<ttservice::IData>>` —— `ttservice` 就是交易服务；
* `OrderService.dll @0x415B89` 还有一个 `?AVStockQueryDMAWhitelistDateResp@ttservice@@`，
  命名空间里明写 `Resp`（响应）。

对应的本地文件只是**缓存**，写入者与读取者都在 `CWhiteListManager` 里：

```text
TradeClient.dll  @0x2B3438  e:/v2.0/src/business/tradeclient/cwhitelistmanager.cpp
TradeClient.dll  @0x2B34C0  TC::CWhiteListManager::save(const std::string &)
TradeClient.dll  @0x2B35B0  TC::CWhiteListManager::load(std::string &)
TradeClient.dll  @0x2B3668  whitelist.json.lock
TradeClient.dll  @0x2B3680  whitelist.json
TradeClient.dll  @0x34D052  ?getRzrkDir@CWhiteListManager@TC@@IEAA?AVQString@@XZ
TradeClient.dll  @0x34B69E  ?getLockPath@CWhiteListManager@TC@@IEAA?AVQString@@XZ
```

### 27.R4 关键证据三：本机根本没有这个缓存文件

```text
find P:/stock/guoxin_iquant_11 -iname "*whitelist*"
  → 只有 pandas / tensorflow 自带的测试文件
find P:/stock/gd_qmt        -iname "*whitelist*"
  → 同上
```

两个安装都没有 `whitelist.json`。配合今日日志里 **0 条**
`CWhiteListManager` / `QueryFrameworkWhiteList` 记录，可以确认：
**框架白名单查询在本会话里根本没发生过。**

### 27.R5 现场日志：拒绝是纯 PID 维度，不是 IP

今日日志共 **9 次**拒绝，全部同形：

```text
5649: 2026-11-11 08:11:09,777 [TC::COrderServiceQuantAdaptor::onConnected]
      quant session 0, illegal pid 4012
```

而 `illegal IP` 命中 **0 次**。这一条很重要：说明问题不在来源地址，
**往 IP 白名单方向改是错的方向**。

容易被误读的两条 IP 日志，其实与 PID 闸无关：

```text
2000: TC::CIPWhitelistCollector::slot_readBrokerIniTimer  IP white list changed from broker ini!
2532: TC::CIPWhitelistCollector::updateIPWhitelist        IP white list changed from setting!
```

它们是客户端自己的**服务器地址收集器**（行情/交易服务器 IP 列表），
而且 `config/broker.ini` 与 `config/broker.xml` 里 `ip` / `white` 关键字 **0 命中**。

### 27.R6 PID 判定函数本身是 public，但仍然不是门

`Formula.dll` 里有一个 `mdl_auth_xtquant_no_pid_check` 的消费点，
紧邻 `TC::CStrategyTradeManager` 的非法检查族：

```text
Formula.dll  @0x1F0160  mdl_auth_xtquant_no_pid_check
Formula.dll  @0x1EFF50  illegal IP: {0}
Formula.dll  @0x1EFF60  TC::CStrategyTradeManager::onIllegalIPFind
Formula.dll  @0x1F0040  TC::CStrategyTradeManager::onIllegalFileFind
Formula.dll  @0x1F4E8B  sigFindIllegalIP
Formula.dll  @0x1F4EA8  sigFindIllegalFile
Formula.dll  @0x26D7C4  ?checkProcessID@CStrategyTradeManager@TC@@QEAA_N_J@Z
```

最后一个符号解码：

```text
QEAA  = public + __thiscall + __ptr64
_N    = 返回 bool
_J    = 参数 __int64（pid）
```

即 `bool CStrategyTradeManager::checkProcessID(__int64 pid)`，**是 public 导出的**。
但它不是门：

* 它只**回答**"这个 pid 是否被允许"，不**授予**权限；
* 宿主是 `utils::CSingletonT<CStrategyTradeManager>` 单例，`s_pInstance` 是进程内的
  `boost::shared_ptr` ⇒ 外部进程拿到的是自己那份未初始化的空实例，
  与 §26 里 `COrderFuncService::instance()` 完全同一个坑。

同理，查询入口也是私有：

```text
OrderService.dll @0x3D410A  ?queryFrameworkWhitelist@COrderService@TC@@AEAAX_N@Z
AEAA = private + __thiscall + __ptr64；X = 返回 void
OrderService.dll @0x3D5344  ?sigFrameworkWhitelistChanged@COrderService@TC@@QEAAXXZ   (public，但只是 Qt 信号)
OrderService.dll @0x3D548D  ?slotFrameworkWhitelistChanged@COrderService@TC@@IEAAXXZ  (IEAA = protected)
```

### 27.R7 五条被关掉的路（逐条给理由，不含糊）

| 做法 | 结论 | 理由 |
| --- | --- | --- |
| 本地写 `whitelist.json` 伪造 | **禁止且无效** | 伪造授权凭证；且它只是缓存，下次 `onQueryWhiteList` 回来就覆盖。`m_bUsePrevWhitelist` 只在**查询失败**时才回退旧值 |
| 把 `no_pid_check` 改成 1 | **禁止** | 该值只在日志里、不在任何配置文件；本地无处可改，能改的只有日志 = 伪造审计 |
| 外部调 `checkProcessID` | **不可行** | public 但宿主是进程内单例；且它只回答不授权 |
| 外部调 `queryFrameworkWhitelist` | **不可行** | `AEAA` = private + `__thiscall`，需要 `this` |
| 改 IP 白名单 | **方向错误** | 今日 `illegal IP` 命中 0 次，9 次拒绝全是 `illegal pid` |

`OrderService.dll` 里还有一块容易被误当成"白名单文件格式"的键名表
（`0x2FD408`–`0x2FD538`）：

```text
m_bAutoBalance / m_nBalanceTime / m_bStartupAutorun / m_bSwitchAutorun
m_bUsePrevWhitelist / m_bAutoStoploss / m_bAutoStopBalance / m_bUseWhiteList
m_nMaxLoss / m_bForbidUseWhiltelist / m_nAccountType
m_nT0CreditOrderType / m_nT0CreditSellOrderType / m_strMd5
whitelist / importwhitelist
```

这是**持久化设置对象的属性名表**（注意 `m_bForbidUseWhiltelist` 里 `Whiltelist` 是源码拼写错误），
不是文件结构。按它去造文件同样属于伪造。

### 27.R8 第四条通道：XtModel 配对 IPC —— 本地开关已开，照样被授权位拦住

这是本轮最值得记的一条，因为它把「本地改配置有没有用」这个问题**直接实证掉了**，
不需要我们去动任何配置。

`Formula.dll` 里除了下单相关的东西，还藏着一个**独立的 IPC 服务器**：

```text
Formula.dll  @0x1E3258  CIPCManager::init, not auth:mdl_auth_gt_ipc_pair
Formula.dll  @0x1E3290  void __cdecl TC::CIPCManager::init(void)
Formula.dll  @0x1E32C0  e:\v2.0\src\indexmodel\formula\cipcmanager.cpp
Formula.dll  @0x1E3330  ..\userdata_ipc
Formula.dll  @0x1E3340  CIPCManager::startServer listen, path:{}, succ:{}
Formula.dll  @0x1E33B0  CIPCManager::startClient connect, path:{}, sessionID:{}, connected:{}
Formula.dll  @0x1E34C0  CIPCManager::handleRequest func:{}, boParam:{}
Formula.dll  @0x1E34F0  bool __cdecl TC::CIPCManager::handleRequest(
                            const std::string &func, const bson::BSONObj &boParam, ...)
```

`handleRequest(func, boParam)` 是 **BSON-RPC 式函数分发** —— 与 `callFormula` 总线同族形态。
而且客户端实现**就在同一个 DLL 里**（XtModel SDK）：

```text
Formula.dll  @0x1F7370  xtipc::IPCClient::onMessage(const struct base::ipmq::PackageHeader &, void *)
Formula.dll  @0x1F7898  xtipc::IPCClient::handleStartServer
Formula.dll  @0x1F7A68  xtipc::IPCClient::handleConnect
Formula.dll  @0x1F8098  xtipc::IPCClient::sendBsonRequest
Formula.dll  @0x1F7C18  xtipc::IPCClient::sendHeartBeatReq
Formula.dll  @0x1F74A0  e:\v2.0\src\miniqmt\minquote\xtmodelsdk\xtipcclient\ipcclient.cpp
```

也就是说：**通道两端都在本机**，服务端 `TC::CIPCManager`、客户端 `xtipc::IPCClient`，
带心跳（`sendHeartBeatReq` / `handleHeartBeatResp`）与 BSON 请求（`sendBsonRequest`）。
这是设计上给**外部进程配对**用的口子。

**但它没起来，而且客户端自己说了原因：**

```text
2979: 2026-11-11 07:58:25,761  CIPCManager::init, not auth:mdl_auth_gt_ipc_pair
```

而本地开关**本来就是开的**：

```text
userdata/users/15624979679/Config.xml:47  <TradeSetting ... ipc_pair_flag="1" ...>
userdata/users/15624979679/Config_backup.xml:47  同为 1
```

现场状态也对得上：`P:\stock\guoxin_iquant_11\userdata_ipc` **目录不存在** ——
`init` 在授权检查处就 return 了，`startServer` 从未执行，所以没有任何端点可连。

这条证据的价值在于它回答了「那我改本地配置行不行」：

> **本地开关已经是 1 了，通道依然没开。** 决定权在柜台下发的
> `mdl_auth_gt_ipc_pair=0` 手里，不在 `Config.xml` 手里。

需要强调边界：这**不是**"绕过"的机会。它是官方设计的外部进程配对通道，只是当前账号未开通；
在授权位为 0 时强行起 server 或伪造配对，属于绕过授权，禁止。

### 27.R9 合法路径，以及开通后怎么验

**合法路径**：由券商（国信）在柜台侧开通
`mdl_auth_xtquant_no_pid_check`；或整体启用 `mdl_auth_broker_whitelist`
并把本平台进程纳入框架白名单；或开通 `mdl_auth_gt_ipc_pair` 启用 XtModel 配对 IPC。
**三条都是业务开通，不是本地配置。**

开通后必须用**两处独立证据**验证，缺一不可：

1. **授权位变了**：重登后看日志 auth string 里
   `"mdl_auth_xtquant_no_pid_check" : 1`；
2. **闸真的开了**：再连一次会话，日志出现
   `quant session N, pid M connected`，而**不是** `illegal pid`。

只看到第 1 条就说"能下单了"是错的 —— 授权位变了不等于这条链路上每个 gate 都放行。

### 27.R10 本轮没证明的（不写成已证明）

1. 没有实测"`no_pid_check=1` 后 `illegal pid` 必然消失"——需券商开通，当前不可达。
2. 没有确认 `CWhiteListManager::getRzrkDir()` 具体落在哪个磁盘路径（未加载 DLL，静态不可判）。
3. 没有确认 `m_bUsePrevWhitelist` / `m_bForbidUseWhiltelist` 的确切语义（仅从键名与相邻关系推断）。
4. 没有确认 `mdl_auth_broker_whitelist=0` 是"未申请"还是"券商未部署该模块"。
5. 没有确认 `mdl_auth_gt_ipc_pair=1` 后 `CIPCManager` 是否会正常 `startServer`（需券商开通）。
6. 没有解出 `xtipc` 的 `PackageHeader` 结构与 `func` 名字集合——授权位为 0、端点不存在，无法观测。

**被证实的真实下单路径仍然是 0 条。** 本轮的价值是把 §24 里"有白名单机制"
这句含糊的话，收紧成了"机制存在、授权位为 0、数据由柜台下发、本地无合法写入点"。

---

## 28. 行情与交易分开推进：当前实测状态与改进方案

本节记录 2026-11-11 08:32–08:40 的**只读**实测（未改配置、未投单、未启停客户端），
并据此给出改进方案。结论先说：**行情和交易不能一起推**，它们的卡点性质完全不同。

### 28.R0 现场状态

| 进程 | 状态 |
| --- | --- |
| `XtItClient.exe` | 运行中，pid 19340 |
| `miniquote.exe` | 运行中，pid 20076（对应 58341） |
| `XtMiniQuote.exe` | 未运行 |
| `XtModel.exe` | **未运行**（`xtmodel/data/log` 下无当日日志） |

| 端口 | 结果 |
| --- | --- |
| 58600 | LISTEN/ACCEPT |
| 58341 | LISTEN/ACCEPT（miniquote） |
| 58610 / 58620 / 58670 / 64000 / 63100 | **TIMEOUT**（端口不可用，不能断定未启动） |

### 28.R1 行情：不可用，而且根因不在我们这侧

`config/xtdata.ini` 的 `[client_xtdata] address=127.0.0.1:58670` —— 官方行情入口是 **58670（XtModel）**，
而 58670 现在不监听，因为 **XtModel.exe 没在跑**。

有人会退而用 58600（`server_formula`）凑行情。**这条路不能当主通道**，两个实测证据：

**证据一：方法面会漂移。** 同一组参数 `['600000.SH','1d',0,1]`：

```text
08:02  getMarketData -> status=0, ErrorID=None, result_keys=['result']   ⇒ 判为「存在」
08:33  getMarketData -> status=1, ErrorID=200005 未找到处理函数          ⇒ 判为「不存在」
```

参数完全一致，所以**不是参数差异**，是 58600 的方法面本身在变
（疑似 formula worker 生命周期相关，未证实）。

> **⚠ 本结论已于 §29.R2 撤回。** 四次连续采样（10:04 / 10:24 / 10:26 / 11:04）
> 中 `getMarketData_1d` 全部 `exists=True`；08:33 那次 `200005` 落在客户端重启窗口内，
> 属**瞬时缺失**（`transient_missing`），不是漂移。正确表述是
> 「58600 稳定存在 5 个只读行情方法，但**恒返回空数据**」。
> 它不能当行情主通道的理由是「稳定但空」，不是「漂移」。

**证据二：方法在，数据是空的。**

```text
getInstrumentDetail  -> {"result": {}}
getTradingDates      -> {"result": [], "last": true}
getDividFactors      -> {"result": []}
getStockListInSector -> {"result": [], "last": true}
```

「方法存在」≠「有数据」，更 ≠「实时」。58600 上 `getFullTick` 直接返回
`200005 未找到处理函数`，也没有订阅推送；而日线/分钟线同样带 `open/high/low`，
所以**不能靠字段名推实时**。

**XtModel 昨天确实起来过，但要看清它当时的状态**（`xtquantservice_20261110.log`）：

```text
IPythonApiServer::createServer listen, finished:true, ip:0.0.0.0, port:58670, autobind:1
saveConfig cp, config:{"autobind":1,...,"port":58670,...}
QuoteSubscriber::initQSDataImp key: 5_SH_L1, server: 115.231.218.73:55310
setLoginStatus true > true
```

绑定成功、行情服务器连上、登录状态 true —— 但同一份日志里还有：

```text
MarketInst::_loadFromLocal ... tdl:[0, 0, 0] / sl:[0]
MetaInfoManager::loadMetaInfo home dir not exists, path:.\data\datadir\metainfo
WARN WholeMarketQuoter::waitForMarketSub wholequote not ready, login:false, stocklistCompleted:true
```

**本地基础数据是空的**（交易日 0、股票列表 0、metainfo 目录不存在），
wholequote 也没 ready。所以：**启动 XtModel ≠ 行情可用**，中间还差一层基础数据。

### 28.R2 交易：程序化不可用，GUI 手工可下单

卡点已在 §24–§27 逐条证实，这里只做汇总：

1. PID 闸授权位 `mdl_auth_xtquant_no_pid_check=0`，柜台下发，**本地无开关可改**；
2. `mdl_auth_broker_whitelist=0`，`whitelist.json` 从未生成；
3. `callFormula` 总线依赖 58610/58670 绑定，二者当前都不通；58600 无此方法（200005）；
4. DBF 文件单需 `is_automonitor=1`，当前为 0。

**被证实的真实下单路径：0 条。**

### 28.R3 改进方案（分轨，每步都可验证）

原则：**行情先自救，交易等授权**；不把猜测写成方案。

**行情轨（不需要额外批准，均在已授权范围内）**

| 步 | 动作 | 预期判据 | 注意 |
| --- | --- | --- | --- |
| M1 | 启动 `xtmodel/XtModel.exe` | 58670 监听 + `IPythonApiServer listen finished:true` | 启动≠可用，见 28.R1 |
| M2 | 用 xtdata SDK 验证 `getFullTick` / `getMarketData` 是否**非空** | 必须有 tick 级证据，日线字段不算 | 空数据要如实报空 |
| M3 | 若 M2 仍空：补基础数据（股票列表 / 交易日 / 历史K线） | `tdl`/`sl` 非空、metainfo 目录生成 | 可能耗时，先小样本 |
| M4 | 行情源定稿：58670 为主，58600 仅兜底且标「不稳定」 | 适配层按源分级，空数据不当成功 | 不把 58600 当主通道 |

**交易轨（每一步都需要用户或券商）**

| 步 | 动作 | 预期判据 | 前置 |
| --- | --- | --- | --- |
| T1 | 开启 `is_automonitor=1`，走 DBF 文件单 | `file_order_readiness` 全绿 → 生成委托文件 | **需用户批准**；armed 后写文件即等于下单 |
| T2 | 向国信申请柜台授权（`no_pid_check` / `broker_whitelist`） | 授权位变 1 **且** 日志出现 `pid connected` | 业务开通，需对接客户经理 |
| T3 | 官方 xtquant 直连下单（`callFormula passorder`） | 58610/58670 绑定 + 授权位放行 | 前置全不满足，不可先行 |

**明确不可行的（不要再试）**

* 直接调 `COrderFuncService::orderStock`（private + 参数类 0 导出构造函数，见 §26）；
* 调 `TradeHub::placeOrder`（出站转发插件，不是入站门）；
* 伪造 `whitelist.json` / 改授权串 / 改日志（伪造凭证）；
* 授权位为 0 时强行起 `CIPCManager`（绕过授权，见 §27.R8）。

### 28.R4 定时巡检

已建三条工作日巡检（09:30 / 10:00 / 11:00），每次跑同一份检查清单并**强制写证据**：
行情端口与方法面、58600 是否仍漂移、交易授权位现值、DBF readiness、当日 `illegal pid` 计数。
即使前置全红也必须落盘，不得静默退出。

### 28.R5 本轮没证明的

1. 启动 XtModel 后 58670 是否**稳定**绑定（今日未启动，只有昨日日志佐证）；
2. 本地基础数据为空时 `getFullTick` 到底返回空还是报错；
3. 58600 方法面漂移的确切触发条件；
4. `is_automonitor=1` 后客户端是否真的读取并产生委托。

## 29. 盘中第 3 次采样（2026-11-11 09:56–10:04）：授权位随重登不变、58600 方法面再漂移

> 编号顺延自 §28（文中两个同名 §17 属历史遗留，本节不回改）。本轮是**纯只读**巡检：
> 未改配置、未投单（含模拟/纸面）、未启停客户端、未伪造凭证或授权、未绕 PID 白名单。
> 交易日判定：周三、11 月无中国法定节假日；当日两次交易服务登录成功、
> `refreshAccounts account_num = 5`、GTS `59.36.9.32:56000` ESTABLISHED ⇒ 在场，
> 09:56–10:04 处于 09:30–11:30 连续竞价，故做完整动态检查而非仅静态检查。

### 29.R0 现场状态与「重登」这个新变量

| 进程 | 08:32（§28） | 10:04（本节） | 说明 |
| --- | --- | --- | --- |
| `XtItClient.exe` | 运行中，pid 19340 | 运行中，pid **24240** | **08:53:21 重启过**（非本轮所为，本轮未启停任何进程） |
| `miniquote.exe` | 运行中，pid 20076 | 运行中，pid **21764** | 58341 仍 LISTEN |
| `XtMiniQuote.exe` | 未运行 | 未运行 | 未检查通过 ≠ 已检查通过，记为 unavailable |
| `XtModel.exe` | 未运行 | 未运行 | `xtmodel/data/log` 无当日日志 |

端口矩阵（`timeout` 与 `refused` 必须分清，本轮 0 个 refused）：

| 端口 | 判定 | 措辞 |
| --- | --- | --- |
| 58600 | **ok / LISTEN** | TCP connect 成功，`netstat` 显示 pid 24240 |
| 58610 / 58620 / 58670 / 63100 / 64000 | **unavailable / TimeoutError** | 「端口不可用」；**不能**写成「服务未监听/拒绝」——只有 `ConnectionRefusedError` 才支持该结论 |

登录态：当日日志有 **8 行** `onTradeServerLoginSuccess`（07:58:13 / 08:53:40 两轮），
GTS 一条 ESTABLISHED（本地 61974 → 59.36.9.32:56000，属 pid 24240），
`refreshAccounts` 12 行且 `account_num` 恒为 5 ⇒ 柜台级在线，**GUI 手工下单不受程序化结论约束**。

### 29.R1 行情：仍不可用，且拿到两个新事实

**新事实 ①：58600 方法面第三次漂移，且这次「方法全在、数据全空」。**

| 采样点 | `getMarketData` 同一组参数 `['600000.SH','1d',0,1]` | 判读 |
| --- | --- | --- |
| 08:02 | `status=0, ErrorID=None` | 存在 |
| 08:33 | `status=1, ErrorID=200005 未找到处理函数` | 不存在 |
| **10:04** | `status=0, ErrorID=None`（五个只读方法**全部**存在） | 又存在 |

同一组参数、同一端口、同一客户端 ⇒ 漂移是方法面自身在变，不是参数差异。
本轮把「存在性 / 数据性 / 时效性」三轴拆开记：五个方法 `method_available=ok`，
但 `getInstrumentDetail` / `getMarketData(1d)` / `getMarketData(1m)` /
`getTradingDates` / `getDividFactors` / `getStockListInSector` 的
`data_available` **全部 unavailable**（结果体 `classify_payload.kind=empty, size=0`）。
⇒ 「方法在」既推不出「有数据」，更推不出「实时」。

**新事实 ②：昨日的上游拒绝今日不存在，但阻塞换成了「进程没起」。**
`XtClient_20261111.log` 中 `account is expired` **0 次**（2026-11-10 曾 33 次）；
然而 `XtModel.exe` 仍未运行、58670 仍 TIMEOUT。这两种阻塞**互不顶替**：
不能因为「上游没拒了」就说行情可用，也不能把昨日的上游拒绝当成今日成因。

证据分级（本轮实测，非推断）：

* `realtime_eligible = false` —— 58670（官方 xtdata 入口，见 `config/xtdata.ini` 的
  `[client_xtdata] address=127.0.0.1:58670`）与 58610 均不可用；58600 无 `getFullTick`
  （`ErrorID=200005`），也没有订阅推送。
* `tick_proven = false` —— 本轮用**不存在的方法名**独立复验了 `200005 = 未找到处理函数`，
  再用同一判据确认 `getFullTick` 不存在；没有任何结果体可判为 tick。
* `period` 显式参与分级：日线 `1d` / 分钟线 `1m` 同样可能带 `open/high/low`，
  「有行情字段」≠「真实时 tick」。`getFullTick` 的真 tick 形状是
  `{代码: {lastPrice, …}}`，**外层没有 `stockCode` 键**，只看 `_CODE_KEYS` 会漏判真 tick；
  分级走 `classify_payload` 的 `code_shaped_keys` 分支兜住该形状（判据可复核）。
* 补充：昨日 `xtquantservice_20261110.log` 即使 `listen finished:true` 也有
  `tdl:[0,0,0] / sl:[0]`、`metainfo home dir not exists`、`wholequote login:false`
  ⇒ **启动 XtModel ≠ 行情可用**，中间还差一层本地基础数据。

### 29.R2 交易：程序化仍不可用；「重启客户端能不能刷出授权」被证伪

**只读总线探测**：只对 58600 发 `callFormula(requestid,'gettradedetail',BSON)`，
**全程未发 `passorder` 或任何下单/撤单命令**（其余五个端口不可连接 ⇒ `skipped`，未发 RPC）。
应答 `ErrorID=200005 未找到处理函数`，与同轮校准锚一致 ⇒ 58600 上**没有** `callFormula`
方法，交易总线不可达；这与 §24 的枚举结论一致（58600 只有 5 个只读行情方法）。

**授权位随重登不变（本节最硬的新证据）**：当日有**两次** `receive module auth string`
（L2025 @07:58:13、L8672 @08:53:33），四个键**逐字相同**：

| 键 | 07:58 会话 | 08:53 会话 | 含义 |
| --- | --- | --- | --- |
| `mdl_auth_xtquant` | 1 | 1 | 模块开了 |
| `mdl_auth_xtquant_no_pid_check` | **0** | **0** | PID 闸仍生效 |
| `mdl_auth_broker_whitelist` | **0** | **0** | 框架白名单未启用 |
| `mdl_auth_gt_ipc_pair` | **0** | **0** | 配对 IPC 未授权 |

⇒ 授权串是**柜台每次登录重新下发**的，客户端重启不会把它刷成 1；
「重启试试」不是可行路径，本地也没有该开关可改（键只在日志里，不在任何 ini/xml/json）。

**PID 闸与其它通道**：当日 `quant session 0, illegal pid 4012` **9 次**、
`quant session N, pid M connected` **0 次**、`illegal IP` **0 次** ⇒ 拒绝是纯 PID 维度。
★ 归属声明：这 9 次发生在 08:11–08:16，是早前探针留下的**当日累计**，
本轮 09:56–10:04 未发起任何 PID 闸连接，不能把它们算成本轮结果，更不能把「0 次 connected」
读成「本轮被放行过 0 次」。`CIPCManager::init, not auth:mdl_auth_gt_ipc_pair`
在**两次登录各出现一次**（L2979、L9638），`userdata_ipc` 目录仍不存在；
而本地 `ipc_pair_flag="1"` 本来就是开的 ⇒ 改本地配置无效再次被实证。

**`is_automonitor` 与 DBF**：四份配置（`Config.xml` L43、`Config_backup.xml` L43、
`xtuserconfig.xml` L2073、`xtMiniConfig.xml` L182）**全部 `is_automonitor="0"`**，
CSV 监控同样为 0。readiness 七步逐项结论：

| 步 | 国信 15624979679 | gd_qmt 22453951 | blocking |
| --- | --- | --- | --- |
| `config` | ok | ok | 是 |
| `armed` | **false** | **false** | 是 ⇒ **`blocking_step=armed`** |
| `monitor_dir` | ok | ok | 是 |
| `writable` | ok（仅 `os.access`） | ok | 是 |
| `template` | ok（0 个 DBF ⇒ 用保守列宽） | ok | 否 |
| `client` | ok（国信 pid 24240 在跑） | **unavailable**（该安装无 `XtItClient.exe`） | 否 |
| `insert_time_format` | ok（口径未裁决） | ok | 否 |

★ 客户端运行态按**安装**分别上报（`Get-CimInstance Win32_Process` 证实只有
`P:/stock/guoxin_iquant_11/bin.x64/` 下有进程）；不能因为国信在跑就把 gd_qmt 的
`client` 也写成「客户端在运行」。该步非 blocking，两个安装的 `blocking_step` 仍是 `armed`。

`columns_verified=True`（15 列与 `OrderService.dll` 0x3100F8–0x3101C4 逐字同序）；
INSERTTIME 分歧仍 `unresolved`（二进制 0x310418 为 `yyyy-MM-ddHH:mm:ss.zzz`，
无空格 22 字符），**未 armed 实测前不改默认值**。两个监控目录均空、
`template_dbf_count=0`，`probe_write_skipped=True` ⇒ 本轮**未往监控目录写任何文件**
（armed 时写文件即等于下单）。
**结论：`is_automonitor=0` ⇒ 无法产生委托**，不得表述成「通道已通」或「已就绪」。

### 29.R3 三问的直接回答

| 问题 | 回答 | 卡在哪一步 |
| --- | --- | --- |
| 行情现在能不能用？ | **不能**（unavailable） | 官方入口 58670 = `XtModel.exe` 未运行；58600 只有漂移的空壳方法，无 tick |
| 交易现在能不能用？ | **不能**（unavailable，程序化） | PID 闸授权位 0 + 交易总线端口全不可用 + `is_automonitor=0`；GUI 手工通道另计 |
| 能不能直接下单？ | **不能** | 被证实的真实下单路径仍 **0 条**；前置 11 项未全绿，且用户未当次批准 |

本轮 `real_order_attempt = skipped`：未投生产单、也未做纸面/模拟单，
因此不存在任何可误述为「真实成交」的结果。

### 29.R4 改进方案（只写有证据支撑的下一步）

| 步 | 状态 | 动作 | 判据与证据 | 前置 |
| --- | --- | --- | --- | --- |
| M1 | skipped | 由用户批准并**手动**启动 `xtmodel/XtModel.exe` | 58670 LISTEN + `IPythonApiServer listen finished:true`；依据是 `config/xtdata.ini` 指向它、本轮 `account is expired=0` | 本轮按约束未启停任何进程；**启动≠可用**，见 29.R1 |
| M2 | skipped | 58670 起来后只读验 `getFullTick` 是否非空 | `quote_proven=True` 且 `period=tick`；日线字段不算 | 依据本轮 `tick_proven=false` |
| T1 | skipped | 改 `is_automonitor=1` 走 DBF 文件单 | `file_order_readiness` 全绿；armed 后写文件即等于下单，须小样本 + 撤单预案 | **需用户当次明确批准**；依据本轮 `armed=false` |
| T2 | skipped | 向国信柜台申请开通 `no_pid_check` / `broker_whitelist` | **两处独立证据**：授权位变 1 **且**日志出现 `pid M connected`；只看前者不算 | 依据两次登录授权位恒 0、9 次 `illegal pid` |

**明确不可行（不要再试）**：58600 作为下单入口（`200005` 已校准）；
`TradeHub::placeOrder`（出站转发插件，非入站门）；改授权串 / 伪造 `whitelist.json` /
改日志（伪造凭证）；授权位为 0 时强起 `CIPCManager` 或伪造 IPC 配对（绕过授权）；
「重启客户端刷授权」（29.R2 两次登录证伪）。

### 29.R5 本轮没证明的

1. 启动 XtModel 后 58670 是否稳定绑定（本轮未启动，仅昨日日志佐证）；
2. 本地基础数据为空时 `getFullTick` 返回空还是报错；
3. 58600 方法面漂移的确切触发条件（现已三个采样点：存在 → 不存在 → 存在）；
4. `is_automonitor=1` 后客户端是否真的读取 DBF 并产生委托；
5. 58610 绑定后是否同样被 PID 闸拦；券商柜台侧开通授权位的流程与时效；
6. INSERTTIME 二进制格式串究竟用于日志还是校验（未 armed 实测，保持 `unresolved`）。

证据文件：`output/qmt_iface_check_20261111_1004.json`（schema `qmt_work.iface_check.v1`，
含 `failed_preconditions` 11 项）、`output/probe_iface_check_20261111.py`、
客户端 `userdata/log/XtClient_20261111.log`。

## 30. 10:20–10:26 盘中复检：58341 证伪、同轮 A/B 漂移轴、接入方案收敛

本节是 10:04 之后的又一份 `qmt_work.iface_check.v1` 证据（采样 10:26:53）。
交易日盘中**只读**：未改任何配置、未投单（含纸面/模拟）、未启停任何客户端进程、
未伪造凭证或授权、未绕 PID 白名单。本节不回改 §28 / §29。

### 30.R0 现场与探针自身的诚实性修正

| 轴 | 本轮实测 | 状态 |
| --- | --- | --- |
| `XtItClient.exe` | pid 24240（国信安装，08:53:21 启动） | ok |
| `miniquote.exe` | pid 21764（国信安装，08:53:33 启动） | ok |
| `XtMiniQuote.exe` / `XtModel.exe` | 未运行，本轮按约束未拉起 | unavailable |
| GTS | `59.36.9.32:56000` ESTABLISHED（pid 24240） | ok |
| 交易登录 | `onTradeServerLoginSuccess` 8 条（L2456 / L9613） | ok |
| 账户 | `refreshAccounts account_num = 5`（L9750） | ok |
| 监听端口 | 58600(24240) / 58341(21764) / 8086(24240) | ok |
| 行情云 | 3 条 ESTABLISHED 到 `:55300`（其中 2 条属 pid 21764） | 只证连接，不证数据 |

★ 安装归属由 `Get-CimInstance Win32_Process` 当次采集：只有国信安装有客户端，
gd_qmt 没有 `XtItClient.exe` ⇒ DBF readiness 的 `client` 步对它记 `unavailable`，
不把「未运行」写成「已检查通过」。

**v1 探针（10:04）里写死的现场值，v2 全部改成实算**：pid 与安装归属、日志路径与行号、
授权位、PID 闸次数、配置现值、端口 Timeout/Refused 措辞、`failed_preconditions` 文案、
证据分级 basis、三问结论。v2 另加四根轴：58341 方法面、58600 同轮 A/B、
日志增量归属游标、与 10:04 基线逐项差分；异常路径也强制写证据文件
（记 `failed_preconditions`，不静默退出）。

**基线差分（18 根轴）**：与 `output/qmt_iface_check_20261111_1004.json` 相比，
17 根不变，唯一变化是客户端日志行数 12497 → 13349（心跳类增长）。
⇒ 本轮的新知识来自**新增检查轴**，不是现场状态变好或变坏；
不能把「没变化」写成「重新验证通过」。

### 30.R1 新事实①：miniquote 58341 是第三个 BSON-RPC 端点，但不是行情旁路

58341 之前只被记过「LISTEN」，从未做方法面扫描。本轮用**与 58600 完全相同的**
只读方法名 + 不存在方法名校准锚，在同一采样窗口直接对照：

| 方法族 | 58600 / A 轮 | 58341 |
| --- | --- | --- |
| 6 个行情只读方法（`getStockListInSector` / `getInstrumentDetail` / `getMarketData(1d,1m)` / `getTradingDates` / `getDividFactors`） | 6/6 `method_exists=true`，结果体**全空** | **全部 `ErrorID=200005`** |
| 5 个 xtquant 基座方法（`getTag` / `checkVersion` / `getDataDir` / `getAppBinDir` / `getSectorList`） | 全部 `200005` | 全部 `200005` |
| `getFullTick` | `200005` | `200005` |
| 校准锚 `__qmtwork_calibration_unknown__` | `200005 未找到处理函数` | `200005 未找到处理函数` |
| `callFormula('gettradedetail')` | `200005` | `200005` |

58341 能正确回 `200005` ⇒ 它**讲同一套帧协议**（不是连不通、也不是协议错），
但本轮枚举的 13 个只读名字/校准锚全部「方法不存在」⇒ **不能当行情旁路**。
★ 边界：这只证明「枚举过的这些名字不存在」，**不能**推断「该端口没有任何方法」；
也没有用官方 xtdata 客户端的握手方式试过 ⇒ 计入 30.R7 的未证实项。

另一个额外轴：8086 发 BSON-RPC 帧后 `EOFError: 连接被对端关闭` ⇒
它是 CEF/HTTP 本地端口，不是 xtquant RPC 入口；`EOF` 与「方法不存在」是两回事，
不得混谈。客户端与行情云有 3 条 ESTABLISHED 也**不能**推出「本地 RPC 能取到 tick」。

### 30.R2 新事实②：58600 同轮 A/B 一致，且它连 xtquant 基座方法都没有

* 同一轮内间隔 2 秒做 A/B 两次采样，13 个名字的存在性 `changed_count=0`
  ⇒ **本轮内没有漂移**；跨轮仍是四个采样点：08:02 存在 → 08:33 `200005`
  → 10:04 存在 → 10:26 存在。漂移的确切触发条件仍未证实。
* 新增证据：58600 上 `getTag` / `checkVersion` / `getDataDir` /
  `getAppBinDir` / `getSectorList` 也全是 `200005` ⇒ 它不是 xtquant 官方服务，
  只是 `server_formula` 暴露的 6 个行情方法壳。
* 6 个存在的方法 `data_available` 全部 `unavailable`
  （`classify_payload.kind=empty, size=0`）⇒ **方法存在 ≠ 有数据 ≠ 实时**。
* `period` 显式参与分级：`1d` / `1m` 同样可能带 `open/high/low`，
  「有行情字段」绝不是「真实时 tick」；`getFullTick` 的真 tick 形状
  `{代码: {lastPrice, …}}` 外层没有 `stockCode` 键，分级靠 `code_shaped_keys` 兜住。

### 30.R3 行情：三根轴分开，且修掉 v1 的一处分类错误

端口矩阵 6 个：58600 `ok`；58610 / 58620 / 58670 / 63100 / 64000 全 **TimeoutError**、
`ConnectionRefusedError` 0 个 ⇒ 措辞只能是「端口不可用」，
绝不能写成「服务未监听 / 拒绝」。

★ **判据修正**：v1 把 `realtime_eligible` 建立在「58670/58610 上 `gettradedetail`
有应答」上——那是**交易总线**，不能顶替行情侧证据。v2 改成
「官方实时端口在听 **且** 其上行情侧只读方法（`getTag` / `getInstrumentDetail` /
`getMarketData` / `getFullTick`）有应答」。两个端口都 TIMEOUT ⇒ 该轴是
`skipped`（未发 RPC），不是「已检查通过」。`realtime_eligible=false`、
`tick_proven=false`（没有任何 `period=tick` 且 `quote_proven=true` 的结果体）。

上游侧：当日 `account is expired = 0`（11-10 曾 33 次），行情云有 3 条连接；
但 `XtModel.exe` 未运行、当日 xtmodel 日志不存在、58670 仍 TIMEOUT。
**「上游没拒」与「本地没起」是两种阻塞，互不顶替**；2026-11-10 日志即使
`listen finished:true` 也有 `tdl:[0,0,0] / sl:[0]`、`wholequote login:false`
⇒ 启动 XtModel ≠ 行情可用。

### 30.R4 交易：程序化仍不可用，PID 拒绝行的归属被单独裁定

* **只读总线**：58600 与 58341 的 `callFormula('gettradedetail')` 及不存在命令名
  均 `200005`；8086 `EOF`；58610 / 58620 / 64000 / 63100 端口不可用 ⇒ `skipped`，
  未发 RPC。全程**未发 `passorder`**、未投任何单。
* **授权位**：auth string 两次（L2025 @07:58:13、L8672 @08:53:33）五键逐字相同：
  `mdl_auth_xtquant=1`、`no_pid_check=0`、`broker_whitelist=0`、
  `gt_ipc_pair=0`、`strict_connection_check=0` ⇒ 「重启刷授权」再次证伪，
  且该键只在柜台下发的日志里，本地没有可改的开关。
* **PID 闸**：当日 `illegal pid 4012` **9 次**（08:11:09–08:16:38）、
  `pid M connected` **0 次**、`illegal IP` **0 次** ⇒ 拒绝是纯 PID 维度。
  ★ 归属：本轮日志字节游标 2,035,604 → 2,048,151（增量 62 行）里
  `illegal_pid_in_delta=0`、`pid_connected_in_delta=0` ⇒ 本轮只读 RPC
  **没有触发新的 PID 鉴权**；那 9 次是早前探针的当日累计，不能算成本轮结果。
* **配对 IPC**：`CIPCManager::init, not auth:mdl_auth_gt_ipc_pair` 两次登录各一次
  （L2979 / L9638），`userdata_ipc` 目录不存在；本地 `ipc_pair_flag="1"` 开着也无效。
* **DBF 文件单**：4 份配置共 8 处 `is_automonitor="0"`（每份 Dbf + Csv 各一处），
  `ipc_pair_flag="1"` 两处，`Config.xml:L43` 现值 0。readiness 七步：

  | 步 | 国信 15624979679 | gd_qmt 22453951 | blocking |
  | --- | --- | --- | --- |
  | `config` | ok | ok | 是 |
  | `armed` | **unavailable** | **unavailable** | 是 ⇒ `blocking_step=armed` |
  | `monitor_dir` | ok | ok | 是 |
  | `writable` | ok（仅 `os.access`） | ok | 是 |
  | `template` | ok（0 个 DBF ⇒ 保守列宽） | ok | 否 |
  | `client` | ok（pid 24240） | **unavailable**（该安装无客户端） | 否 |
  | `insert_time_format` | ok（口径 `unresolved`） | ok | 否 |

  两个监控目录均空、0 个 DBF 模板、`probe_write_skipped=True` ⇒
  **本轮未往监控目录写任何文件**（armed 时写文件即等于下单）。
  `is_automonitor=0` ⇒ **无法产生委托**，不得表述成「通道已通」。

### 30.R5 三问的直接回答

| 问题 | 回答 | 卡在哪一步 |
| --- | --- | --- |
| 行情现在能不能用？ | **不能**（unavailable） | 官方 58670（XtModel）未运行/未监听；58600 与 58341 均无 `getFullTick`，58600 六个方法结果体全空 |
| 交易现在能不能用？ | **不能**（unavailable，程序化） | 授权位 `no_pid_check=0` + 交易总线端口全不可用 + `callFormula` 不存在 + `is_automonitor=0` |
| 能不能直接下单？ | **不能** | 被证实的真实下单路径仍 **0 条**；12 项 `failed_preconditions` 未全绿，且用户未当次批准 |

`gui_manual_order=ok` 只说明柜台级在线（GTS + 登录 + `account_num=5`），
是人工通道，不构成程序化接口可用，也不等于已验证成交。
`real_order_attempt=skipped`：本轮连纸面/模拟单都没做，
不存在任何可误述为真实成交的结果。

### 30.R6 改进后的接入方案（只保留有证据的下一步）

**收敛原则**：候选通道从「很多个」砍成两条；所有动作本轮一律 `skipped`，
凡涉及改配置/启进程/下单的，必须用户当次明确批准。

| 处置 | 通道 | 本轮证据 |
| --- | --- | --- |
| 删除 | 58600 作行情/下单主通道 | 无 `getFullTick` / `callFormula`，方法面跨轮漂移，结果体全空 |
| **删除（新）** | miniquote 58341 作行情旁路 | 13 个只读名 + 校准锚全 `200005`（30.R1） |
| **删除（新）** | 8086 作 RPC 入口 | 发 BSON 帧直接 `EOF`，CEF/HTTP 端口 |
| 删除 | 改本地配置 / 重启刷授权 | 授权位柜台下发、两次登录逐字相同；`ipc_pair_flag=1` 已开仍被拦 |
| 删除 | `TradeHub::placeOrder` / 伪造授权 / 强起 CIPC | 出站插件 / 伪造凭证 / 绕过授权，硬约束禁止 |
| 保留（blocked） | **DBF 文件单**：唯一入站且不依赖 PID 白名单 | 卡在 `armed`：`is_automonitor=0` |
| 保留（blocked） | **官方 58670/XtModel 行情 + 官方 `passorder`** | 卡在 XtModel 未起、授权位 0 |

下一步（状态全为 `skipped`，依据是当次实测而非猜测）：

| 步 | 动作 | 判据 | 前置 |
| --- | --- | --- | --- |
| M0（新） | 把 58341 / 8086 从候选清单移除 | 已由本轮证据证伪，无需再试 | 无 |
| M1 | 用户批准并**手动**启动 `XtModel.exe` | 58670 LISTEN + `listen finished:true` | 本轮未启停任何进程；启动 ≠ 可用 |
| M2 | 58670 起来后只读验 `getFullTick` 是否非空 | `period=tick` 且 `quote_proven=true`；日线字段不算 | 依据本轮 `tick_proven=false` |
| T1 | 批准后改 `is_automonitor=1` 走 DBF | `file_order_readiness` 全绿；armed 后写文件即等于下单，须小样本 + 撤单预案 | **需用户当次明确批准** |
| T2 | 向国信柜台申请开通 `no_pid_check` / `broker_whitelist` | **两处独立证据**：授权位变 1 **且**日志出现 `pid M connected` | 依据两次登录恒 0、9 次 `illegal pid` |

同时把两条判据写进方案本身：① 行情资格只看行情侧证据，
不许用交易总线 `gettradedetail` 顶替；② 三态恒为
`ok` / `unavailable` / `skipped`，未检查不得写成通过。
对证据 JSON 全量校验 `state` 字段：ok 75 / unavailable 20 / skipped 13，
**无第四态**（v2 初稿里两处计划态标签 `blocked` 已归一为 `unavailable`）。

### 30.R7 本轮没证明的 / 明确不可行

未证实：58341 是否有本轮未枚举的方法、或需要官方 xtdata 握手才能列出方法面；
启动 XtModel 后 58670 是否稳定绑定；本地基础数据为空时 `getFullTick` 返回空还是报错；
58600 方法面漂移的触发条件；`is_automonitor=1` 后是否真会读取 DBF 并产生委托；
58610 绑定后是否同样被 PID 闸拦；柜台侧开通授权位的流程与时效；
INSERTTIME 二进制格式串究竟用于日志还是校验。

明确不可行：58600 / 58341 / 8086 作为下单或实时行情入口；
`TradeHub::placeOrder`；改授权串、伪造 `whitelist.json`、改日志；
授权位为 0 时强起 `CIPCManager` 或伪造 IPC 配对。

证据文件：`output/qmt_iface_check_20261111_1026.json`（schema `qmt_work.iface_check.v1`，
含 `sampled_at` / `preconditions` / `market` / `trade` / `dbf` / `verdicts` /
`not_proven`，`failed_preconditions` 12 项、`delta_vs_baseline` 18 轴）、
`output/probe_iface_check_v2_20261111.py`、客户端 `XtClient_20261111.log`。

## 31. 11:00 盘中复检（采样 11:04:48）：端口面完备 + 内部实时链活着 ⇒ 阻塞收敛到「对外导出层」

### 31.R0 本轮边界

- 交易日按 `weekday=Wednesday` + 11:04 落在 09:30–11:30 判定为盘中；★ 节假日日历未接入（`holiday_calendar_checked=false`），该判定本身记为未完全证实。
- 纯只读：0 单 0 撤、0 次配置改动、0 次进程启停、0 次 DBF 写探测；补充探针 **0 帧**发往客户端端口。
- 三态恒为 `ok` / `unavailable` / `skipped`；`real_order_attempt=skipped`（连纸面/模拟单都没做，不存在可误述为真实成交的结果）。
- 证据：`output/qmt_iface_check_20261111_1104.json`（schema `qmt_work.iface_check.v1`，20 键）。

### 31.R1 A 环境与登录态

`XtItClient.exe` pid 24240、`miniquote.exe` pid 21764 在跑；`XtMiniQuote.exe` / `XtModel.exe` 未运行（按约束不启停，如实报告 `unavailable`）。GTS `59.36.9.32:56000` ESTABLISHED 1 条；行情云 `:55300` ESTABLISHED 3 条 —— **只证明有 TCP 连接，不等于有行情数据**。

当日日志 14,723 行 / 2,198,081 B：`onTradeServerLoginSuccess` 8 次（首 L2456 07:58:13）、`refreshAccounts account_num=5` 14 次（末 L9750）。auth string **2 次登录**（07:58 与 08:53 重启）四键逐字相同：`mdl_auth_xtquant=1`、`no_pid_check=0`、`broker_whitelist=0`、`gt_ipc_pair=0`（`auth_bits_stable_across_sessions=true`）⇒ 第三次复证「重启刷授权」无效。

### 31.R2 B 行情接口

端口矩阵 6 个：`58600` = `ok`（LISTENING）；`58610/58620/58670/63100/64000` 共 5 个全为 **TimeoutError**，0 个 `ConnectionRefusedError` ⇒ 措辞只能是「端口不可用」，不能写成「服务未监听/拒绝」。额外轴 `58341`/`8086` 在听。

`58600` 同轮 A/B 两次采样 `changed_count=0`。A 轮方法面：`getStockListInSector` / `getInstrumentDetail` / `getMarketData(1d,1m)` / `getTradingDates` / `getDividFactors` 6 个存在但**结果体全空**；`getFullTick` 与 xtquant 基座 5 方法（`getTag` / `checkVersion` / `getDataDir` / `getAppBinDir` / `getSectorList`）全 `200005`。校准锚（不存在的同名族方法）在 `58600` 与 `58341` 双端口均返回 `200005 未找到处理函数` ⇒ 判据本轮独立复验成立。`58341` 的 13 个只读名 + 校准锚全 `200005`（30.R1 结论复现）。

证据分级：`realtime_eligible=false`（官方 58610/58670 在听的 = []）、`tick_proven=false`（`period=tick` 且 `quote_proven=true` 的候选 = []）、`period_participates=true`。★ 日线/分钟线同样带 `open/high/low`，`getFullTick` 的 `{代码:{lastPrice,…}}` 外层无 `stockCode` 键 ⇒ 「有行情字段」绝不等于「真实时 tick」。

本轮复核 `config/xtdata.ini` 仍为 `[client_xtdata] address=127.0.0.1:58670` ⇒ 58670 是官方行情入口的配置判据；`bin.x64/xtdata.ini` 本轮**不存在**（§28 曾记为「空文件」，历史状态未复查，记为口径不一致待核）。

### 31.R3 C 交易接口

只读总线探测（**只发 `callFormula(...,'gettradedetail',...)`，绝不 `passorder`**）：`58600` / `58341` 均 `ErrorID=200005`，`8086` 发 BSON 帧被对端关闭（`ErrorID=None`）⇒ 交易总线在任何可达端口上都不存在。

当日 PID 闸：`illegal pid` **9 次**（L5649…L6082，08:11:09–08:16:38，全为早前探针 pid 4012）、`pid connected` **0 次**、`illegal IP` **0 次** ⇒ 拒绝是纯 PID 维度。日志字节游标 2,197,365 → 2,209,912（增量 62 行）里 **0 条**新拒绝 / 0 条放行 ⇒ 当日 9 次是历史累计，**不能算本轮新产生**。

`CIPCManager::init, not auth:mdl_auth_gt_ipc_pair` 2 次（L2979 / L9638），`userdata_ipc` 目录不存在、`init_auth_granted=false`；本地 `ipc_pair_flag=1` 本来就是开的（§27.R8）⇒ 本地开关无效。

### 31.R4 D DBF 文件单（只读 readiness，唯一入站且不依赖白名单）

`is_automonitor=0` 共 8 处（4 个配置文件各 2 处：`Config.xml` L43/L44、`Config_backup.xml` L43/L44、`xtuserconfig.xml` L2073/L2074、`xtMiniConfig.xml` L182/L183）；`Config.xml:L43` 的 `DbfMonitorSetting` 现值 `"0"`。两个安装 `blocking_step=armed`、`ready=false` ⇒ **无法产生委托**，不得表述成「通道已通」。监控目录空、0 个 DBF 模板、`probe_write_skipped=true`；`columns_verified=true`（`DBF_COLUMNS` 与客户端二进制 15 列一致），INSERTTIME 格式串口径仍 `unresolved`。gd_qmt 的 `client` 步记 `unavailable`（该安装无客户端在跑），不粉饰为通过。

事后完整性复核（11:10，纯只读）：`Config.xml` 的 mtime 为 11:09:42，属**客户端自身 GUI 状态写入**（探针全程只读，未打开过写句柄）；L43/L44 `is_automonitor="0"` 与本轮采样一致。两个监控目录仍为空，未生成 `whitelist.json` / `userdata_ipc`；目录下只有一个与文件单无关的 `userdata/data/xt_client_status.dbf`（不在监控目录，不计入 `template_dbf_count`）。

### 31.R5 ★ 新事实 F1：全量 LISTENING 归属 ⇒ 端口矩阵对 TCP 入站面完备

此前每次只逐个探固定 6+2 个端口，从未证明「没有别的隐藏入站端点」。本轮 `netstat -ano -p tcp` 全量 **48** 个 LISTENING 端点 + `Get-CimInstance Win32_Process` 逐 PID 归属：

| 端口 | PID | 进程 | 归属 | 已在矩阵 |
| --- | --- | --- | --- | --- |
| 58600 | 24240 | `XtItClient.exe` | 国信安装 | 是 |
| 8086 | 24240 | `XtItClient.exe` | 国信安装 | 是 |
| 58341 | 21764 | `miniquote.exe` | 国信安装 | 是 |
| 8888 | 23528 | `pythonw.exe -m notebook` | 国信安装 | 否 |
| 5766 | 8380 | `GuosenPassGuardXInput.exe` | 券商密码保护组件 | 否 |

矩阵外两个都**已有文档结论**：8888 = 客户端内嵌 Jupyter（明文 token，见 L40/L73/L307），历史实测其 kernel 交易被 PID 闸拒（`illegal pid 0`）；5766 是密码输入保护组件。⇒ **不存在尚未枚举的行情/交易 RPC 端点**，「还有没试过的端口」从待办删除。
边界：只覆盖 netstat 可见的 TCP LISTENING；UDP / 共享内存 / 命名管道 / 进程内调用 / 出站连接**未检查**，不能推断没有其它形态的通道。

### 31.R6 ★ 新事实 F2：盘中客户端内部实时行情链是活的

`XtClient_Formula_20261111.log`（GBK）实算：`REALTIME*/… data complete, start calc` **380 条** + `send model result` **380 条**，09:31:00（L85）→ 11:06:06（L1224），按小时 09:116 / 10:236 / 11:28，4 个固定 reqid（`0REALTIME_0000012` / `0REALTIME_0000095` / `0REALTIMEVOL_0000011` / `0REALTIMEVOL_0000096`），分钟计数 `before 1, curr 2` → `before 96, curr 97`。

⇒ 交易时段内客户端内部持续收到数据、完成计算并回送结果。**行情不可用的根因不是「没开盘」「上游断流」「行情云没连」**，而是「没有可用的对外接口把数据导出来」；结合 F1，导出层唯一官方候选仍是 58670/XtModel。交叉验证：09:56 / 10:04 / 10:26 / 11:04 四个**盘中**采样点，`58600` 结果体一致为空、`getFullTick=200005` ⇒ 「等开盘就有数据」被证伪。
边界：这是**进程内公式模型日志**，不等于外部进程能经 RPC 取到 tick，也不证明 tick 级完备（可能只是分钟级）；日志没有周期字段 ⇒ 本轴**不得**写进 `tick_proven`，`realtime_eligible` 仍为 `false`。

### 31.R7 ★ 差分探针自身的诚实性修正（判据教训）

补充探针首版把 10:26 的 `market.surface_58600` 当平铺字典读，实际它是 `{"round_A":…, "round_B":…}`，于是 7 个方法轴读成 `None` → 误报「7 项漂移」。改法：取 `round_A`，并把「上一轮缺该轴」单列 `unreadable_before`（不可比），绝不并入 `changed`。重算后 `axes_checked=24`、`changed_count=0`、`unchanged=24`、`unreadable=0` ⇒ 与 10:26 相比三问结论与 12 项 `failed_preconditions` 均**未变**，无新增放行。
★ 与「探针断言不可证伪 = 探针错」同族：**差分出现大面积变化时先怀疑差分器，不要先宣布现场变了**。

### 31.R8 三问的直接回答与改进后的接入方案

| 问题 | 回答 | 卡在哪一步 |
| --- | --- | --- |
| 行情现在能不能用？ | **不能**（unavailable） | 不是没数据：F2 证明内部实时链活着；卡在**对外导出层** —— 58670/XtModel 未运行且 5 个候选端口全 Timeout，`58600`/`58341` 无 `getFullTick`、结果体全空 |
| 交易现在能不能用？ | **不能**（unavailable，程序化） | 授权位 `no_pid_check=0` / `broker_whitelist=0`（只能柜台侧开通）+ 总线 `callFormula`/`gettradedetail` 在任何可达端口都不存在 + `is_automonitor=0` |
| 能不能直接下单？ | **不能** | 被证实的真实下单路径仍 **0 条**；12 项 `failed_preconditions` 未全绿，用户未当次批准；`real_order_attempt=skipped` |

`gui_manual_order=ok` 仅指柜台级在线（GTS + 登录 8 次 + `account_num=5`）的人工通道，不构成程序化接口可用，也不等于已验证成交。

| 处置 | 项 | 当次证据 |
| --- | --- | --- |
| 删除 | 换交易时点 / 排查上游行情源 | F2：盘中 380 条实时计算 + 四采样点一致 |
| 删除 | 继续扩大端口矩阵找隐藏端点 | F1：全量 48 端点归属，矩阵外仅 8888/5766 且已有结论 |
| 新增 `skipped` | M0b：不再消耗轮次，直接推进 M1 | 同上 |
| 新增 `skipped` | M1b：**需用户当次批准**才可在内嵌 Jupyter kernel 里只跑 xtdata 只读行情探测 | F1 实证 8888 由 QMT `pythonw.exe` 监听；会创建 kernel ⇒ 属客户端状态变更，本轮未执行；★ 绝不碰 `passorder`/xttrader，交易侧 kernel 历史被 PID 闸拒 |
| 保留 blocked | M1 启动 XtModel → 58670；M2 只读验 `getFullTick`（`period=tick` 且非空） | `tick_proven=false` |
| 保留 blocked | T1 `is_automonitor=1`（需批准）；T2 柜台开通授权位（授权位变 1 **且**日志出现 `pid M connected`） | `armed=false`、两次登录恒 0 |

新增未证实：节假日日历、UDP/共享内存/命名管道形态通道、内嵌 Jupyter 行情侧 xtdata 是否可用、Formula REALTIME 数据的时间粒度；原有未证实项见 30.R7。明确不可行不变：58600/58341/8086 作行情或下单入口、`TradeHub::placeOrder`、改授权串/伪造 whitelist/改日志、授权位为 0 时强起 CIPC 或伪造 IPC 配对。

证据文件：`output/qmt_iface_check_20261111_1104.json`（含 `sampled_at` / `preconditions`（`failed_preconditions` 12 项）/ `market` / `trade` / `dbf` / `verdicts` / `not_proven` + `supplement_1100`）、`output/probe_iface_1100_supplement.py`、`output/probe_iface_check_v2_20261111.py`、`output/probe_iface_check_v2_20261111_1100.log`、`output/probe_iface_1100_supplement.log`。

---

## 29. 三次定时巡检的实际结果与方案改进（含对 §28 的纠错）

本节回答一个问题：09:30 / 10:00 / 11:00 三次巡检到底买到了什么，方案该改哪里。
结论先说：**三次运行没有带来任何新结论，但暴露了三个方案层面的缺陷**，
其中一个是上一轮自己写错的判断。

### 29.R0 三次运行的实际产出

| 计划时点 | 证据文件 | 实际采样 | 当场生成的探针 | 键数 | failed_preconditions |
| --- | --- | --- | --- | --- | --- |
| 09:30 | `qmt_iface_check_20261111_1004.json` | 10:04:53（草稿 09:56） | v1，36,237 B，6 步 | 16 | 11 |
| 10:00 | `qmt_iface_check_20261111_1024.json` / `_1026.json` | 10:24:32 / 10:26:53 | v2，58,665 B，8 步 | 19 | 12 |
| 11:00 | `qmt_iface_check_20261111_1104.json` | 11:04:48（补充轴 11:06:38） | v2 + 另一个 supplement 脚本 | 20 | 12 |

三次的 `verdicts` **完全一致**：

```text
market_can_use_now      unavailable
trade_can_use_now       unavailable
can_place_order_directly unavailable
gui_manual_order        ok（柜台级在线，人工通道）
real_order_attempt      skipped（未投单）
```

11:00 那轮还做了与上一份的逐项差分：**24 个轴 checked，changed=0**。
也就是说，在同一交易时段内再跑一遍同样的探针，信息增量为零。

### 29.R1 三次运行暴露的三个方案缺陷

| 编号 | 缺陷 | 证据 |
| --- | --- | --- |
| D1 | **每次巡检都重新生成探针** | v1 36KB → v2 58KB → 又写一个 supplement；三次跑的是三个不同程序，跨轮对比要靠事后补差分代码 |
| D2 | **判据不稳，单次缺失被写成长期结论** | 08:33 一次 `200005` 被写成「方法面会漂移」，而 10:04/10:24/10:26/11:04 四次采样全部 `exists=True` |
| D3 | **三次同质重复，且跨 mission 缺轴被伪装成可比** | 24 轴 0 变化；没采的轴被记成 `unreadable`，看起来像「读了但读不到」，实际是「没读」 |

### 29.R2 纠错：撤回 §28 的「方法面会漂移」

§28.R1 里写过：

> 参数完全一致，所以**不是参数差异**，是 58600 的方法面本身在变（疑似 formula worker 生命周期相关，未证实）。

这句话**不成立**，必须撤回。四次连续采样的实测序列：

```text
getInstrumentDetail    [True, True, True, True, True]   stable_present
getMarketData_1d       [True, True, True, True, True]   stable_present
getMarketData_1m       [True, True, True, True, True]   stable_present
getTradingDates        [True, True, True, True, True]   stable_present
getDividFactors        [True, True, True, True, True]   stable_present
getStockListInSector   [True, True, True, True, True]   stable_present
getFullTick            [False, False, False, False, False] stable_absent
```

`getMarketData_1d` 五次全 `True`。08:33 那次 `200005` 落在客户端重启窗口内
（当日 pid 从 19340 → 24240 → 3624 变了三次），属于**瞬时缺失**，不是漂移。

**正确的表述是**：58600 稳定存在 5 个只读行情方法，但**恒返回空数据**
（`result_class.kind=empty, size=0`），且没有 `getFullTick`（稳定不存在）。
它不能当行情主通道的理由是「**稳定但空**」，不是「漂移」。

这两件事必须分开写：**「方法在」≠「有数据」≠「实时」**。
把「空」说成「漂移」，会导致下一步去查生命周期这种错方向。

### 29.R3 改进一：把巡检收敛成常驻引擎（治 D1）

新增 `scripts/qmt_iface_check.py`，一个引擎 + 三个 mission：

| mission | 时段 | 覆盖的轴 |
| --- | --- | --- |
| `preopen` | 09:30 | `env` / `auth` / `ports` / `dbf` —— **故意不扫方法面** |
| `market` | 10:00 | `env` / `ports` / `surface` / `official_ports` / `formula_realtime` / `upstream` / `dbf` |
| `trade` | 11:00 | `env` / `auth` / `ports` / `bus` / `pid_gate` / `listening_inventory` / `dbf` |

`preopen` 不扫方法面是刻意的：开盘前 formula worker 可能未就绪，
扫了只会得到假缺失——这正是 D2 的成因。

三条定时任务已改为调用同一引擎的不同 mission，不再各自生成探针。
产物名带 mission：`qmt_iface_check_<YYYYMMDD>_<HHMM>_<mission>.json`。

### 29.R4 改进二：R1「单次缺失不得称为漂移」写进代码（治 D2）

```python
def classify_presence(series):
    """全部 True ⇒ stable_present；全部 False ⇒ stable_absent；
       有 True 有 False ⇒ flapping；最近一次 False 但历史全 True ⇒ transient_missing"""
```

配套 `drift_claim_allowed(label)`：**只有 `flapping` 才返回 True**。

还有一条容易被忽略的细节：稳定性序列只收**真正采过该方法的轮次**。
没采该轴的 mission 不贡献 `None`，否则 `None` 会冒充「读了但读不到」，
把「没查」写成「查了没结果」——这属于同一族错误。

### 29.R5 改进三：R2 客户端重启 = 新授权会话（治授权时效）

授权位是**按登录下发**的，不落盘。当日客户端重启了三次
（19340 → 24240 → 3624），所以每一轮的授权位都必须重新读，不能沿用上一轮。

引擎用 `session_boundary()` 比对上一份证据里的 `client_pid`，变化即标记
`client_restarted=true`。本轮实测到一次真实触发：

```json
{"current_pid": 3624, "previous_pid": 24240,
 "previous_file": "qmt_iface_check_20261111_1104.json",
 "client_restarted": true}
```

重启后重新读取的授权位**没有变化**（`no_pid_check=0` / `broker_whitelist=0` /
`gt_ipc_pair=0`），说明柜台侧当日未做任何变更——这是一个干净的否证，
比「大概没变」有用。

### 29.R6 改进四：R3 未查的轴记 `skipped`，不记 `unavailable`（治 D3）

每份证据都带 `axes_covered` 与 `axes_skipped_by_mission`。
跨 mission 比较时，没覆盖的轴明确是 `skipped`，
不再出现「`unreadable` 看起来像读了但读不到」的假象。

### 29.R7 改进五：调度从「三次相同」改为「三次分工」（治 D3）

原设计是三次跑同一份检查清单，实测 24 轴 0 变化。现在：

* 09:30 只判**开盘前能不能开始**（环境/登录态/授权位/DBF），不碰方法面；
* 10:00 只判**行情能不能导出来**（方法面 A/B 双采样 + 官方端口判活 + Formula 实时日志）；
* 11:00 只判**交易通道通不通**（总线只读探测 + PID 闸增量 + 全量 LISTENING 归属）。

并且每条任务的 prompt 都写明：**若 `failed_preconditions` 与上一轮完全相同，
明确写「本轮无变化」，不要重复造方案**。这是对「同质重复」的直接约束。

### 29.R8 方案重排：行情先证导出，交易转向内置策略

三次运行里唯一真正的新事实来自 11:00 的补充轴：

```text
XtClient_Formula_20261111.log
  realtime_data_complete_count = 380
  realtime_send_result_count   = 380
  09:31–11:06 连续，reqid 形如 0REALTIME_0000095 / 0REALTIMEVOL_0000011
```

即：**交易时段内客户端内部公式模型持续收到实时数据并完成计算**。
所以行情不可用不是「没开盘 / 上游断流」，而是「**没有可用的对外接口把数据导出来**」。
这条直接改变了优先级：

| 原计划 | 新排序 | 理由 |
| --- | --- | --- |
| M3 补基础数据 | **降为 deferred** | 数据链本来就是活的；在 M1b 未证伪前先补数据属于猜因 |
| M1 启动 XtModel | 保留但降级 | 昨日日志显示能绑 58670，但本地 `tdl:[0,0,0]`、`wholequote login:false` ⇒ 启动 ≠ 可用 |
| — | **M1b 新增且前置** | 客户端内嵌 Jupyter（`127.0.0.1:8888`，QMT 目录 `pythonw.exe`）与数据引擎同宿主，是唯一「用活数据」的路径；需用户当次批准 |

交易侧同理重排。外部 RPC 已被**结构性**排除（58600 无 `callFormula`/`gettradedetail`、
58610/58670 未绑定、PID 闸授权位为 0），DBF 需要改配置，
于是**内置策略（路径 B/B′）+ 本地桥升为唯一主线**——它不是「绕过」，
而是客户端自启进程内的官方通道；仓库已有 `agent_bigqmt` 栈可复用。

### 29.R9 本轮没证明的

1. 09:30 那次为何 10:04 才落盘（草稿 09:56）——调度延迟原因未查清，不写成已查清。
2. 节假日日历未接入 ⇒ 交易日判定只按 weekday。
3. UDP / 共享内存 / 命名管道 / 进程内调用形态的入站通道（只枚举 TCP LISTENING）。
4. 客户端内嵌 Jupyter kernel 的行情侧 `xtdata` API 是否可用（未执行，需批准）。
5. Formula 引擎 REALTIME 数据的时间粒度（分钟线还是 tick），日志未给周期字段。
6. 路径 B/B′ 的 GUI 公式部署与 live 四前置复核。
7. `is_automonitor=1` 后客户端是否真的读取并产生委托。

**被证实的真实下单路径仍然是 0 条。** 本节的价值是把「三次跑完没有新结论」
这件事本身变成可执行结论：**问题不在采样次数，在采样方式**。
