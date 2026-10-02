# 大小 QMT 使用说明（qmt_work 量化交易网关）

> 版本：v1.1（2026-10-02）——新增 §10（定时任务的数据新鲜度语义 & 界面默认深色外观）
> 姊妹文档：
> - `docs/BIG_QMT_COMPAT_PLAN.md`（大 QMT 兼容支持方案，可行性论证）
> - `docs/UNIFIED_TRADING_ABSTRACTION.md`（统一交易接口抽象，选型论证）
> - `docs/BROKER_ONBOARDING.md`（券商接入指南，代码层）
> - `docs/API接口文档.md`（REST/WS 接口契约）
>
> 本文定位：**面向使用者 / 运维**的「大小 QMT 怎么区分、各自怎么配、接口与能力差在哪、出问题怎么查」一站式说明。不涉及抽象层设计之争，只讲「你现在该怎么做」。

---

## 0. 先读这一节：你到底用的是哪种 QMT？

绝大多数困惑来自一个事实：**「QMT」不是一个东西，而是两条形态差异极大的产品线**。先对号入座，再跳到对应章节。

| 你手上的客户端 | 俗称 | 进程 / 目录 | qmt_work 里的叫法 | 本文章节 |
|---|---|---|---|---|
| 券商给的「极速版 / MiniQMT」 | 小 QMT | `XtMiniQmt.exe` + `userdata_mini/` | **直连（direct）** | §3 |
| 券商给的「完整版 QMT / 大 QMT」 | 大 QMT（完整版） | QMT 主程序 + `userdata/` | **直连 full（direct）** 或 **策略桥（路径 B）** | §4 / §5 |
| 同一安装目录里两套目录都在 | 大小合一安装 | 同根下 `userdata/` + `userdata_mini/` | 见 §2.4 特别说明 | §2.4 |

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

---

## 1. 概念与本质差异

| 维度 | 小 QMT（极速版 / MiniQMT） | 大 QMT（完整版） |
|---|---|---|
| 交易进程 | `XtMiniQmt.exe` 独立进程，生成 `userdata_mini/` | QMT 主程序，生成 `userdata/` |
| 外部 xtquant 直连 | 支持（正被 PID 白名单收紧） | 部分券商同样收紧（同一授权串体系 `mdl_auth_xttrader/xtdata_strict_connection_check`） |
| 内置 Python | **无**（外部 Python 直接 `import xtquant` 调 SDK） | **有**（内置 Python 3.6.x，策略脚本被挂载执行） |
| 内部交易 API | 经 xtquant SDK：`XtQuantTrader.order_stock` / `cancel` / `query_*` | 注入策略命名空间的全局函数：`passorder` / `cancel` / `get_trade_detail_data`，以及 `ContextInfo` 方法族 |
| 行情 | xtdata 经 miniquote 端口 **58610** | `ContextInfo.get_full_tick` / `get_market_data` / `download_history_data`；**大窗口 58600 仅交易，行情 RPC 仍须 miniquote 58610** |
| 回调 | `XtQuantTraderCallback` 推送（on_order / on_trade） | 部分有回调（如两融），普通股票账户常需**轮询 diff 合成** |
| 版本碎片 | xtquant pyd 按 ABI 编译（cp36~cp312） | 内置 Python 老旧（3.6），无 `shared_memory`，部分函数依赖客户端版本 |
| 「策略」概念 | **无**（不存在策略注册树） | **有**（客户端持久化注册树，策略须写入注册树才会出现在列表） |

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

| 形态 | Dialect（方言） | Transport（传输） | 对应前端接入模式 |
|---|---|---|---|
| 小 QMT 直连 / 大 QMT 直连 full | `xtquant.v1` | InProcess（进程内 SDK）/ SubprocessBridge（ABI 不匹配时子进程桥） | `direct`（空 connector_key） |
| 大 QMT 策略桥（路径 B） | `bigqmt.v1` | `file`（文件）/ `redis`（队列）/ `zmq`（同机极速） | `bridgeFile` / `bridgeRedis` / `bridgeZmq` |

> 技术细节（可跳过）：`xtquant.v1` 把 canonical op 映射到 `XtQuantAdapter` 方法名（`place_order` 等）；`bigqmt.v1` 把 canonical op 映射到大 QMT agent 的 wire action（`PLACE` / `CANCEL_ORDER` / `QUERY_*` 等）。两者最终都汇入同一套 `SignalRouter` / `ExecutionService` / 账户页 / 事件泵，上层代码零改动。

### 2.3 前端「接入模式」开关

在「券商连接」页，接入模式 `accessMode` 的取值与后端 `connector_key` 映射如下：

| 前端接入模式 | 提交到后端的 connector_key | 含义 |
|---|---|---|
| `direct`（默认，留空） | ``（空） | xtquant 直连：小 QMT 填 `userdata_mini`，大 QMT 填 `userdata` |
| `bridgeFile` | `qmt.big.bridge.file` | 大 QMT 策略桥，文件传输（零部署，秒级） |
| `bridgeRedis` | `qmt.big.bridge.redis` | 大 QMT 策略桥，Redis 传输（<50ms，需装 Redis） |
| `bridgeZmq` | `qmt.big.bridge.zmq` | 大 QMT 策略桥，ZMQ 传输（同机极速） |

### 2.4 大小合一安装的特别说明

有些券商给的「大 QMT 完整版」安装目录下**同时**有 `userdata/`（大）和 `userdata_mini/`（小）。这是「大小合一」安装，但：

- **注册树只有一个，且属于大 QMT**。`userdata_mini/` 下没有 `config/user/root`、也没有 `python/` 策略目录。
- 因此即便目录里能看到 `userdata_mini`，小 QMT 在此安装里**仍没有「策略」概念**；你想用「策略桥（路径 B）」必须走大 QMT 的 `userdata/` + 注册树，不能套用小 QMT 的 `userdata_mini`。
- 直连时，小 QMT 永远指向 `userdata_mini`，大 QMT 直连（full）指向 `userdata`——目录不同，不要指错。

---

## 3. 小 QMT（直连）使用说明

### 3.1 适用场景

券商的「极速版 / MiniQMT」客户端，且**外部 xtquant 直连未被 PID 白名单封死**（即 `import xtquant; XtQuantTrader().connect()` 能成功，交易连接不返 `rc=-1`）。

### 3.2 部署步骤

1. 安装券商极速版客户端，记下 `userdata_mini` 所在根目录（例如 `C:/QMT/userdata_mini`）。
2. 打开 qmt_work 前端 →「券商连接」页。
3. 接入模式选 **`direct`（直连）**。
4. 填写：
   - **券商**：在档案下拉里选对应券商（迅投系一般复用 `xtp` 适配器，无需新建）。
   - **客户端路径**：`C:/QMT/userdata_mini`（极速版）。
   - **资金账号** / **账户类型**：`STOCK`（普通）/ `CREDIT`（信用）/ `OPTION`（期权）/ `FUTURES`（期货）。
5. 点「添加连接」——后端默认建连即自动连接（autoconnect），连接会持久化，之后每次启动自动重连。

### 3.3 能力与限制

- **支持**：实时行情、历史 K 线、交易（下单/撤单）、资金、持仓、当日委托、当日成交；经 `XtQuantTraderCallback` **回调推送**委托/成交事件到前端。
- **限制**：受券商授权串 `mdl_auth_xttrader/xtdata_strict_connection_check` + `no_pid_check=0` 的 PID 白名单约束。一旦券商收紧，外部直连 `connect` 返 `rc=-1`（日志 `quant session N, pid X not allowed`），此时小 QMT **在 qmt_work 里无法接入**（小 QMT 没有策略桥兜底），只能引导找券商放开，或改用大 QMT 完整版走路径 B。

---

## 4. 大 QMT 直连（client_mode=full）使用说明

### 4.1 适用场景

你拿到的是「完整版 QMT」，且希望像小 QMT 一样从外部 Python 直连它。qmt_work 已把 `client_mode=full` 做成一等公民：`ConnectionConfig` → `registry.create_adapter` → `XTPQuantAdapter.start()` → `_effective_trade_dir()` 会在 `userdata` 与 `userdata_mini` 候选目录间互备降级。

### 4.2 部署步骤

与小 QMT 直连几乎一致，唯一区别是**客户端路径指到 `userdata`**（不是 `userdata_mini`）：

1. 接入模式选 **`direct`（直连）**。
2. **客户端路径**填 `C:/QMT/userdata`（完整版根下的 `userdata`）。
3. 其余（券商 / 资金账号 / 账户类型）同 §3.2。

### 4.3 限制

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
                                         │  python/qmt_work_agent.py（bundle，被 QMT 挂载执行）
                                         │  capture_qmt_injected_funcs(globals())  ← 捕获 passorder 等
                                         │  轮询 diff 合成委托/成交事件 → events.ndjson
                                         ▼
                                    QMT 柜台（passorder / cancel / get_trade_detail_data）
```

- **入口必须自己就是实现**：QMT 只把 `passorder` 等函数注入**被挂载的那一个文件**的命名空间。薄壳 `from X import *` 会让 `globals()` 指向被导入模块，导致注入函数一个都捕获不到。所以产物是**单文件 bundle** `python/qmt_work_agent.py`。
- **自动验证**：bundle 启动即写 `probe_result.json`（自检）+ `agent_status.json`（心跳，每 10s）。**心跳新鲜度才是「策略在跑」的判据**——文件残留 ≠ 活着（可能已崩，见 §7.2）。
- **自动拉起**：在 QMT 客户端开启 `tryAutoRunStrategy`，每次登录自动拉起策略。

### 5.2 部署步骤

#### 5.2.0 快速上手（Windows 双击，约 2 分钟）

Windows 用户**推荐走这条路径**：脚本自动找 QMT 目录、生成 bundle、生成 config 模板、打开资源管理器选中 bundle，你只需在 QMT 里点一下「导入本地策略」。

1. **双击 `deploy_qmt_work_agent.bat`**（仓库根目录）
   - 自动探测 QMT 目录（先试 `P:\stock\gd_qmt` / `D:\QMT` / `C:\QMT` / 「国投证券QMT交易端」等常见路径；找不到再手动输入）
   - 生成单文件 bundle `qmt_work_agent.py` + 一份 `.txt` 副本（备用路径 B 粘贴用）
   - 若 `agent_config.json` 不存在则从模板拷贝，存在则保留用户原配置
   - 打开资源管理器并**直接选中** bundle 文件，方便下一步拖选
2. **在 QMT 里导入策略**（约 30 秒，唯一不可自动化的步骤）
   - **路径 A（推荐）**：「模型研究」→ 策略区 → 右键 → 「导入本地策略」/「本地.rzrk导入」→ 选上一步打开的 `qmt_work_agent.py`
   - **路径 B（备用，QMT「导入本地策略」被券商禁时才用）**：「我的」→ 新建策略 → Python 策略 → 全选删除模板 → 记事本双击 `qmt_work_agent.txt` 全选复制粘贴 → 点「编译」保存（编译/保存才会登记进注册树）
3. **关闭并重启 QMT**（注册树落盘要重启才生效）
4. **双击 `diag_qmt_work_agent.bat`** 验证
   - 应显示：`已注册: 是`、`心跳新鲜`、`registered: true`、`alive: true`
   - 想让策略随 QMT 自动拉起：在「模型交易」里选中策略 → 勾选「自动运行」（记进 `UiSettingConfig`，QMT 每次启动会自动拉起）

> **为什么这 4 步里的第 2 步不能自动化？** —— 「导入本地策略」是 QMT 客户端 GUI 对话框（原生 Win32，不是 CEF webview，CDP 摸不到）；注册树是加密容器（XTF1），逆向写有写坏现有 35 条策略的风险。这三重证据见 §5.2.1 步骤 3。

---

#### 5.2.1 详细展开版（6 步）

> 前置：你已安装大 QMT 完整版，且能用它的客户端打开「策略」相关界面。
> Windows 用户走上面「快速上手 4 步」即可，本节是展开版供排障参考。

**步骤 1 · 生成 bundle**

```bash
# 生成器真源 backend/agent_bigqmt/，产物是单文件 bundle
python scripts/gen_qmt_agent_bundle.py
# 或直接用部署工具（推荐，一步到位含校验）
python scripts/qmt_agent_deploy.py deploy --target <你的大QMT根>
```

产物 `qmt_work_agent.py` 须放到大 QMT 的：

```
<大QMT根>/python/qmt_work_agent.py
```

**步骤 2 · 配置 agent**

配置查找顺序（找到第一个即用）：

1. `python/agent_config.json`
2. `python/agent_bigqmt/agent_config.json`
3. `python/../agent_config.json`
4. `EMBEDDED_CONFIG`（`--embed-config` 内嵌，用 `pprint` 渲染 Python 字面量；注意 JSON 的 `true/false` 在 py 源码里非法，必须用 `pprint` 渲染）

至少包含：桥接模式（file/redis/zmq）、对应参数（file=桥目录 / redis=连接串 / zmq=tcp 地址）、token（与前端填的一致）。

**步骤 3 · 在大 QMT 客户端写注册树（GUI 动作，无法自动）**

这是最容易被忽略的一步：**bundle 放进 `python/` 不会自动出现在策略列表**。策略列表来自客户端持久化注册树，不是目录扫描。你必须在大 QMT 客户端里做「导入本地策略 / 新建策略 + 编译」这类**写注册树的 UI 动作**，把 `qmt_work_agent.py` 登记进去。

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

| 传输 | 延迟 | 部署成本 | 稳定性 | 何时用 |
|---|---|---|---|---|
| `file`（默认） | 秒级（轮询间隔） | 零（只需共享目录） | 高（组件最少） | **首选兜底**，没装 Redis/ZMQ 时 |
| `redis` | <50ms | 需装 Redis 或 pyzmq | 高 | 多策略并发、低延迟需求 |
| `zmq` | 同机极速 | 需装 pyzmq | 高 | 单机同进程、极致低延迟 |

单一配置键切换，**协议不变**，三种传输共用同一份 `BrokerAdapter` 映射。

### 5.4 存活判据与诊断（前端可见）

大 QMT 桥的「连没连上」不能只看文件残留，必须看**一次真实往返**。后端 `is_connected()` 语义是「现在可用」：

```
is_connected() = self._connected and not self._agent_unresponsive
```

前端「券商连接」页对每条桥接连接展示存活标签（来自后端 `connector_probe()` 的字段）：

| 字段 | 含义 | 前端呈现 |
|---|---|---|
| `available` | 桥整体可用 | 基础连通 |
| `agent_unresponsive` | agent 心跳超时（策略可能已崩） | warn / 断开 |
| `liveness_failures` | 连续存活探测失败次数 | 重试计数 |
| `last_agent_ok_age_s` | 距上次成功往返的秒数 | 「X 秒前正常」 |
| `livenessLabel` / `hint` | 综合判定的可读标签与建议 | 连接卡片上的状态条 |

> 典型「假绿灯」陷阱：文件残留 `agent_status.json` 还在，但策略进程已崩、心跳不再刷新 → 旧文件让面板显示「已连接」，实际已死。以 `last_agent_ok_age_s` 与真实往返为准，不要只看文件存在。

### 5.5 CEF 远程调试（高级诊断）

大 QMT 的 CEF 远程调试端口 `127.0.0.1:8086`（属 `XtItClient.exe`，`Chrome/102`）可脚本化驱动**已打开**的网页型面板（`/webstrategyedit/*`、`/innerApi/*`、`webwidget/*`）。但 `/json/new` 与 `Target.createTarget` 被拒，只能驱动已开的页，不能自己开页。

工具：`scripts/qmt_cef_cdp.py {find|list|watch|inspect|eval|inject-file|read-editor}`。

### 5.6 工具链速查

| 工具 | 用途 |
|---|---|
| **`deploy_qmt_work_agent.bat`** | **Windows 双击**：一键部署（自动找 QMT → 生成 bundle + config → 打开资源管理器 → 打印下一步指引） |
| **`diag_qmt_work_agent.bat`** | **Windows 双击**：一键诊断（跑 probe + verify + inspect 三合一，输出结构化状态） |
| `scripts/qmt_agent_deploy.py {deploy\|register\|check\|config\|inspect} [--reveal] [--txt]` | 部署 / 登记 / 检查 / 配置 / 检视 bundle（`--txt` 额外产一份 .txt 副本供路径 B 粘贴） |
| `scripts/gen_qmt_agent_bundle.py [--out X.py] [--txt] [--embed-config CFG]` | 从 `backend/agent_bigqmt/` 生成单文件 bundle |
| `scripts/qmt_strategy_list_probe.py --target X [--qmt-dir ...]` | 探查策略注册树里是否已登记 |
| `scripts/qmt_agent_verify.py [--json]` | 注册态 + 心跳一起判（是否真在跑），JSON 输出可直接给程序消费 |
| `scripts/qmt_cef_cdp.py` | CEF 面板 CDP 诊断 |
| `scripts/check_bigqmt_agent_py36.py` | G3 校验：bundle 入口捕获的注入函数名字面量是否 ≤3（必须走 `capture_qmt_injected_funcs(globals())`） |

### 5.7 常见部署错误 → 解决方案

| 症状 | 根因 | 解决 |
|---|---|---|
| 「模型交易」里看不到 `qmt_work_agent` | 注册树未登记（bundle 已拷贝但未在 QMT GUI 里做导入动作） | 走 §5.2.0 步骤 2：QMT「模型研究」右键「导入本地策略」→ 选 `qmt_work_agent.py` → 重启 QMT |
| `qmt_agent_verify` 报「心跳已过期 Xs（阈值 45s）」 | 策略未在跑（未启动 / 已崩 / 未启动 autorun） | ①在 QMT「模型交易」手工点「运行」启动一次；②想自动拉起就勾「自动运行」（记进 `UiSettingConfig`）；③确认 `agent_config.json` 路径正确 |
| `qmt_agent_verify` 报 `registered: false` | 同上（注册树未登记） | 同「模型交易里看不到」 |
| `qmt_agent_verify` 报 `probe_stale: true` | 上面结论只是「陈旧快照里未见」，不代表当前缺失 | 先让策略真正启动一次（生成新的 `probe_result.json`），再跑 verify |
| 桥连不上但前端无错 | `agent_config.json` 里 `bridge_dir` 与前端桥接参数不一致（**日常排障第一名**） | 两处必须**完全一致**，包括绝对/相对路径与斜杠方向 |
| `trading_enabled: false` | 默认下单关闭（安全默认） | 编辑 `agent_config.json` 改为 `true`，重启策略 |
| `inspect` 报「配置文件被独占锁定」/ `PermissionError` | QMT 正在运行持有文件锁 | 关 QMT 再跑 inspect；`--force` 只跳过运行判定，**不做锁规避** |
| 「导入本地策略」菜单灰色 / 找不到 | 券商禁用了 .rzrk 导入 | 改走路径 B：新建策略 → 粘贴 `qmt_work_agent.txt` 全部内容 → 编译 |
| 策略启动但报 `NameError` / `ModuleNotFoundError` | bundle 生成失败或编码问题 | 跑 `python scripts/check_bigqmt_agent_py36.py`；确保用 `gen_qmt_agent_bundle.py` 生成的 bundle 而非手改 |
| 策略启动即退出、无日志 | `agent_config.json` JSON 语法错 | 用 `python -c "import json; json.load(open('agent_config.json'))"` 校验；模板见 `backend/agent_bigqmt/agent_config.example.json` |
| 端口 `8086` 拒绝连接（跑 CEF CDP 时） | QMT 未开「CEF 调试」或客户端版本不支持 | 关闭 CEF 诊断路径，改用 `qmt_agent_verify`；`qmt_cef_cdp.py` 需要 QMT 客户端以调试模式启动 |
| 心跳一直 stale 但「模型交易」里显示运行中 | handlebar 未触发（无行情推进，常见于收盘后或策略未订阅） | 这是**误判活死**的常见来源 —— 心跳新鲜度只是判据之一，不能单独作为唯一判据（TD 系列根因）。用 `is_connected()` 的「现在可用」语义判 |

---

## 6. 接口与能力差异对照表

qmt_work 对上层暴露的接口（行情 / 交易 / 账户 / 持仓 / 订阅 / 历史 K 线）在大小 QMT 下的支撑情况：

| 能力 | 小 QMT 直连 / 大 QMT 直连 full | 大 QMT 策略桥（路径 B） |
|---|---|---|
| 下单 / 撤单 | ✅ xtquant `order_stock` | ✅ `passorder` / `cancel` |
| 资金 / 持仓 / 委托 / 成交查询 | ✅ `query_*` | ✅ `get_trade_detail_data` 族 |
| 实时行情 | ✅ xtdata（58610） | ✅ `ContextInfo.get_full_tick`（58610） |
| 历史 K 线 | ✅ `download_history_data` | ✅ `ContextInfo.get_market_data` / `download_history_data` |
| 委托/成交事件推送 | ✅ 回调（`XtQuantTraderCallback`） | ⚠️ **文件桥不支持回调式订阅**：`subscribe_quote` 只记录意图，由事件泵每秒对账（`SUB_QUOTE` → agent 轮询 diff → `events.ndjson` → WS） |
| 订阅行情 | ✅ 原生回调 | ⚠️ 同上，经事件泵对账合成（秒级，非毫秒级） |
| 两融 / 期权 / 期货 | 取决于券商档案 `capabilities` | 取决于 agent 注入函数集（能力协商） |

**关于「大 QMT 文件桥的行情订阅」要特别注意**（曾是最典型的「假绿灯」）：文件桥没有回调式订阅通道，所以 `subscribe_quote` 只把意图写进内存、**真正的 `SUB_QUOTE` 下发由事件泵每秒对账完成**。若对账逻辑没发出 `SUB_QUOTE`，agent 的订阅集恒为空，`quote_events` 恒返回 0，事件泵每秒空转，界面价格永远停在种子值——而连接状态 / 健康检查 / 订阅日志**全绿**。判活务必以「真实行情是否在动」+ `last_agent_ok_age_s` 为准，不要只看状态灯。

---

## 7. 故障排查

### 7.1 交易连不上：`connect ret error-1` / `pid X not allowed`

- **根因**：券商下发授权串 `mdl_auth_xttrader/xtdata_strict_connection_check=1` 且 `no_pid_check=0`，QMT 对**外部 xtquant 进程做 PID 白名单校验**。日志特征：`quant session N, pid X not allowed, return` + `connect ret error-1`。
- **行情 vs 交易**：行情 `xtdata` 通常正常，**仅交易 connect 返 `rc=-1`**。
- **平台无法改写**：这是券商客户端内部行为。处理：
  - 小 QMT → 引导找券商放开授权（或换支持外部直连的客户端）。
  - 大 QMT → 切到 §5 策略桥（路径 B），绕开外部直连。
- **授权串位置**：只在客户端日志**头部**（不在 tail）；日志按 `_log_rank` 选（交易主 2 > 辅助 1 > 行情 0），不能只按 mtime 取最新。

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

1. **发版**：`gen_qmt_agent_bundle.py`（生成单文件 bundle）或 `qmt_agent_deploy.py deploy`（部署 + 校验一步到位），落到 `<QMT>/python/qmt_work_agent.py`。
2. **看文件**：`qmt_strategy_list_probe.py --target <QMT>/python/qmt_work_agent.py`
   —— `--target` 既可传**策略名**（`qmt_work_agent`）也可传**文件名 / 路径**（过去拼 `.py` 时会误报「文件就位:否」，现已修正）。
   正常应见「文件就位: **是**」；「已注册」通常为**否**，因为拷贝文件不会写注册树。
3. **注册并运行**：在 QMT 客户端做一次写注册树的 UI 动作（§5.3），运行策略。
   再跑 `qmt_agent_verify.py`，当它显示心跳新鲜且 `probe_stale: false` 时，才算**真正闭环**。

> 这三步是判定「大 QMT 到底能不能跑」的唯一可靠口径：**文件在 ≠ 已注册，注册了 ≠ 在运行，运行过 ≠ 现在还在跑**。

---

## 8. 安全与硬约束（务必知道）

- **零 mock**：所有行情 / 交易 / 账户接口都走券商**真实 SDK**，平台**不返回任何模拟数据**。未连券商 → HTTP 200 + 业务码 `503` + 引导；券商不可用 → `503`，可用但被拒 → `400` + 真因。失败**绝不包 `code=0`**（前端靠 `code!==0` 抛错）。
- **`SignalRouter.submit` 唯一交易入口**：任何下单都经它，风控 / 幂等 / TOTP / 审计不可绕过。大 QMT 的 `passorder` 也是经桥回到这条入口，不会绕过。
- **运行期状态不进安装包**：`master.key` / `app.db` / `qmt_work_config.json` / `data/` / `logs/` 不会打进发布包（构建脚本有 `purge + verify` 硬门禁）。干净安装首次启动自行生成密钥。

---

## 9. 一页速查（Cheat Sheet）

| 我要… | 选哪种 | 前端接入模式 | 客户端路径 / 桥参数 |
|---|---|---|---|
| 极速版能直连 | 小 QMT 直连 | `direct` | `userdata_mini` |
| 完整版能直连 | 大 QMT 直连 full | `direct` | `userdata` |
| 完整版被封直连 | 大 QMT 策略桥（兜底） | `bridgeFile`（零部署） | 桥目录 |
| 完整版要低延迟 | 大 QMT 策略桥 | `bridgeRedis` / `bridgeZmq` | Redis 串 / ZMQ 地址 |
| 只有极速版且被封 | **无法接入** | — | 引导找券商放开授权 |

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

| 当前时刻 | 期望的 K 线日期 |
|---|---|
| 交易日 且 已过 `ready_hour`（默认 18 点） | **今天**（当日收盘数据已发布） |
| 交易日 但 未过 `ready_hour` | **上一交易日**（当日数据源约 17:00–18:00 才稳定，不等） |
| 非交易日（周末/节假日） | **上一交易日** |

**为什么必须有 `ready_hour`**：收盘后数据源并不立即吐出当日数据。若口径写成「日历今日」，那么每天 16:15 的定时选股都会判「落后」→ 触发一次全市场补数 → 补完拿到的仍是昨天的数据 → 第二天重复。**这是每天一次的全市场无效 RPC**。`ready_hour` 让判断落在「数据真已经可得」的那一天上，补数从「每天一次」降为「只在真的落后时」。

`ready_hour` 可在触发参数里覆盖（`{"ready_hour": 16}` 更保守、`19` 更宽松）。

### 10.3 增量同步的两种「跳过」

`sync_bars` 任务（含「定时更新日线」）的返回体里有两个**含义不同**的跳过计数，分别呈现、不可相加：

| 字段 | 含义 | 何时非 0 |
|---|---|---|
| `skipped_complete` | 历史已补到起点、**本次压根不用问源** | `backfill_from` 有值时，`bars` 已存在该标的且起点不晚于回补起点 |
| `skipped_fresh` | 本次的增量目标日 **已经是最新**，不用再问源 | 任务参数带 `skip_fresh: true`（默认「定时更新日线」已开启） |

两者共同的效果：**全市场已经最新时，返回 `total=0` 且两个跳过数之和覆盖全部标的——这是成功，不是失败。** 判定任务是否空转必须同时看这三个字段；只判 `total == 0` 会把刚省下来的几万次 RPC 误报成「未获取到任何股票」。

`skip_fresh` 的实现是**只读一次 SQLite**（`LocalStore.latest_dt_map` 一次聚合查询，返回 `{code: 最新dt}`），与 `expected_bar_date()` 比对；查询失败时**不做跳过、全部重跑**——宁可多问源几次，不可漏同步。

### 10.4 任务级失败不再吞掉整批

行情补数与多策略选股都改成了「单个失败只影响自己」：

- **补数**：一只标的的任务级异常（含 `CancelledError`）只记为它自己的 `failed=1`，其余标的的 `ok`/`bars` 全部保留落库。
- **多策略选股**：N 个策略并行计算，失败的那个从结果里剔除、在 `strategy_failures` 里**点名**（形如 `ma_volume: 策略内部错误：除零`），其余策略结果照常落库。只有**全部**失败才向上抛错。

> 历史教训（R15）：这两处原来是「整批一损俱损」——一只标的抛 `CancelledError` 会让 `asyncio.gather` 把整批结果连同已完成的部分一起丢弃。

