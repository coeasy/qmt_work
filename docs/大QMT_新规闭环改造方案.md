# 大 QMT 新规下的闭环改造方案（v2）

> 适用版本：qmt_work v0.5.4 · 初版 2026-10-31 · v2 同日修订（补边界实测、远程下单、外部方案清单）
> 前置文档：`docs/QMT_无文件桥下单方案.md`（**该文 TCP 方案已失效，仅保留能力定档与断线语义章节**）
> 回答四件事：**边界到底在哪**、**怎么落地**、**还能不能远程下单**、**别人是怎么改的**。

---

## 0. 一页结论（v2 有三处修正）

1. **通道全灭**。2026-10-23 盘后起，策略进程内文件读写、网络访问（含 `127.0.0.1`）、miniQMT 直连同时关闭。
   `qmt_work` 现有 6 条链路（file / tcp / redis / zmq / http / xtquant）没有一条能活。
2. **网络不是「报错」而是「停模」**（策略被终端强制停止 + 防外挂弹窗）。这比文件严格一个量级：
   文件被拦只是这一步失败，网络被拦是**整个策略消失**。因此**禁止在策略里做任何连通性试探**。
3. ⚠️ **修正一：持仓主真相源是柜台，不是账本**。社区已跑通的做法是
   `get_trade_detail_data(ACCOUNT, TYPE, 'POSITION')` 启动即重建内存持仓——这个接口新规明确允许。
   本地账本从「主存储」降级为「可丢的辅助信息」。
4. ⚠️ **修正二：一个资金账号只跑一个策略**。重启后策略会把账户里**所有**持仓认领为自己的，
   多策略共用账号会互相踩踏。原方案的「仓位隔离」在新规下**无法靠账本实现**，只能靠账号隔离。
5. ⚠️ **修正三：远程实时下单在合规路径内不可行**。远程桌面人工下单可以，程序化外接不行（见 §7）。
6. 两条合法越界通道不变：**源码即配置**（盘前烘焙）+ **日志/原生导出即回传**（盘后读取）。

---

## 1. 事实基线（按证据强度分级）

| # | 事实 | 强度 | 来源 |
|---|---|---|---|
| F1 | 2026-08-28 中证协 + 中基协《证券公司交易信息系统接入管理规范（试行）》 | 强 | 券商公告转述 |
| F2 | 2026-10-23 盘后起：① 关闭外部文件读写 ② 关闭外部网络连接 ③ 关停 miniQMT | 强（多源一致） | 知乎/CSDN/微博/叩富 |
| F3 | 策略内 `open()` 报 `PermissionError: Forbidden FileIO` | 强 | 国金模拟端实测（2026-10-23） |
| F4 | 文件边界实测矩阵（见 §2.2）：读/写/追加/二进制/CSV/JSON/NumPy/mkdir/rename/remove/mmap/runpy **全拒** | 强（逐项表格） | 国金 QMT 10.23 后实测（2026-10-26） |
| F4b | **本机复核（2026-10-31）**：读、写**全部失败**，不存在「只关写不关读」或「只拦绝对路径」的宽松形态 | 强（用户实测） | 本机实测确认 |
| F4c | **数据库整类不可用**：`sqlite3`（含 `:memory:`）、MySQL、Redis 一律不可用 ⇒ 探针不再逐项探测 | 强（同上） | 本机实测确认 |
| F5 | 网络边界：TCP（**含本机回环**）/HTTP/ZeroMQ **触发停模**；WebSocket 收 2 帧后停止 | 强（逐项表格） | 同上 |
| F6 | 内置 Python 由 3.6.8 升 **3.12**（2.1.32 以后客户端）；老客户端仍 3.6.8 | 强（社区公告 + 双源） | 迅投社区 + 微博 |
| F7 | 第三方数据白名单当前只有 **Tushare**，券商侧配置，非所有券商同步 | 中 | 微博/知乎 |
| F8 | `save_py_key`/`get_py_key` 可用，value 约 **5000 字节**，`errid==0` 成功；官方文档未收录 | 中（单源实测） | 知乎 quantkoala |
| F9 | 板块函数可用，可承载结构化状态 | 中（单源实测） | 同上 |
| F10 | 标准行情、账户查询、`passorder` 开放；**客户端原生导出不受影响** | 强 | 多源 + 微博明确列出 |
| F11 | 程序化交易须首次前报备；高频 = 每秒申报/撤单 ≥300 笔 **或** 单日 ≥2 万笔 | 强 | 上交所规则 |
| F12 | 「文件搬到 `bin.x64` 即内部文件可读写」 | **弱（已被告伪）** | 单源提出，另一源明确否定 |
| F13 | 存在「规避原生监控」的外接实现（作者自称跑通，往返 49–53 ms，但不公开） | 中（自述） | 知乎 2026-10-26 |

**读法**：F4/F5 是本次最重要的新增证据，直接决定「能不能再挣扎一下」——答案是**不能**。
F12/F13 只能当「别人这么说过」，**架构不得依赖**。

---

## 2. 边界到底在哪（问题一）

### 2.1 先定性：拦在哪一层

判断方法只有一行：在策略里打印

```python
import builtins, socket
print(open.__module__, socket.socket.__module__)
```

- 正常应为 `_io` / `socket`；
- 若出现**券商自己的模块名**，说明做了替换 ⇒ 拦截发生在 **Python 策略层**（hook 抛异常）；
- 若仍是 `_io` 却抛 `PermissionError` ⇒ 拦截在**更底层**（进程令牌/驱动/文件系统过滤）。

两者后果不同：前者「换个 API 可能绕过」（但属规避，不做），后者「换 API 也没用」。
**结论对方案没有影响**：两条路我们都不走，但这个判据决定了「策略被拦时该给用户什么解释」。

### 2.2 文件：已实测的禁区（国金 10.23 后）

| 类别 | 具体操作 | 结果 |
|---|---|---|
| 读 | `open` / `io.open` / `Path.read_bytes` | ❌ 权限拒绝 |
| 写 | `w` / `a` / `x` / 二进制模式 | ❌ 权限拒绝 |
| 数据文件 | CSV、JSON 读写；NumPy `save`/`load` | ❌ 拒绝 |
| 目录 | `os.mkdir` | ❌ 拒绝 |
| 元操作 | `rename` / `replace` / `remove` | ❌ 拒绝 |
| 共享内存 | 匿名 / 命名 `mmap` | ❌ 打开映射被拒 |
| 执行 | `runpy.run_path` | ❌ 文件访问被拒 |
| **数据库** | `sqlite3`（含 `:memory:`）/ MySQL / Redis | ❌ **整类不可用**（本机已确认，不再逐项探测） |

> **本机结论（2026-10-31 复核）**：读写**全部失败**，没有「只关写不关读」「只拦绝对路径」
> 这类宽松形态。因此下面「未证实」清单里凡是依赖文件读的降级方案（读外部持仓表、
> 读同目录已有数据），**在本机一律不成立**，不要写进架构。

**未证实（必须探针实测，不能推断）**：`tempfile`、`sqlite3`、`pickle`、`logging.FileHandler`、
`os.path.exists`/`stat`（元数据）、同目录自定义模块 `import`、目录内**已存在**的 txt/csv/json 读取。

后两项很关键，因为存在两种常见宽松形态：

- **只关写不关读**（F15~F17 PASS、F2/F3 BLOCK）⇒ 还能读外部给的持仓表，只是不能落盘；
- **只拦绝对路径**（F2 BLOCK、F3 PASS）⇒ 相对路径仍可用。

这两种形态都**不能**作为架构依赖（券商随时收紧），但决定了「降级方案要不要做」。

### 2.3 网络：不是异常，是停模

| 方式 | 结果 |
|---|---|
| TCP（含**本机回环** `127.0.0.1`） | **原生监控 → 触发停模** |
| HTTP（标准库 / `requests`） | 停模 |
| ZeroMQ | 防外挂弹窗 + 停模 |
| WebSocket | 收到 2 帧后停止，持续通信不通过 |
| UDP | 未测（**默认按封禁处理**） |

★ **这是全篇最贵的一条经验**：文件被拦只损失一次调用，网络被拦是**策略整个被停掉**，
还可能留下「防外挂」告警记录。所以：

> **策略代码里不允许出现任何「试探性」网络调用**，包括 `try: socket.create_connection(...) except: pass`。
> 探针只能在**模拟账号**里跑，且**只跑一次**。

### 2.4 六个最容易误判的边界

1. **「不能读写文件」≠「文件系统不可用」**。策略源码本身必须被加载，否则任何策略都起不来 ⇒
   限制在**策略进程的 API 层**，不是磁盘层。外部程序读写完全不受限。
2. **「能 import」≠「能用」**。`import pandas` 成功只说明库在硬盘上；真正的判据是实际调用。
3. **`WARN` ≠ 被封**。目标不可达、路径不存在、库没装（`SKIP`）都会显示异常，
   **只有明确的 `BLOCK`（权限拒绝/被拦异常）才算封禁**。
4. **`PASS` ≠ 长期可用**。今天能跑，升级一次就没了。凡「非官方文档承诺」的能力，都要有降级路径。
5. **删代码要删干净**。残留的 `import requests` 即使从不调用，也可能触发拦截（社区经验，未证实但成本极低）。
6. **报错文本会骗人**。`Forbidden FileIO` 的拼写错误（原文 `Foribdden`）说明这是券商自定义消息，
   不是标准 `OSError` 文案 ⇒ 不要用异常文本做判定，用**异常类型 + 操作类别**做判定。

### 2.5 三分钟自查（对现有策略/bundle）

```
open(            # 文件读写
os.makedirs / os.path / os.remove / os.rename
json.dump / json.load          # 落盘
to_csv / read_csv / to_excel / np.save / np.load
requests / urllib / socket / websocket / zmq
subprocess / os.system / runpy
sqlite3 / pymysql / redis
```

命中任何一项 ⇒ 该策略在新规下**启动即失败或运行中停模**。

---

## 3. 账户侧：现在还能怎么「开启量化交易」

收紧的是**通道**，不是**权限**。

| 路径 | 门槛（2026 口径） | 形态 | 与本项目关系 |
|---|---|---|---|
| **大 QMT + 内置策略** | 多数 10 万；头部 50 万（20 日日均） | 策略跑在客户端沙箱内，自产自销 | **主路径** |
| 大 QMT + 外部程序 | 同上 | 新规下**不能**接入实时回路 | 降级为盘前/盘后 |
| PTrade | 部分券商 10 万起 | 券商托管，策略上传运行 | **并行备选**（`ptrade_v1` dialect 已存在） |
| miniQMT / xtquant | — | **已关停** | 代码保留，标 deprecated |
| 自建柜台 | 机构级 | 需专门审批 | 个人不可行 |

开通动作（与代码无关，但会卡上线）：

1. 风险测评 C4 以上 → 签《程序化交易风险揭示书》《合规承诺书》；
2. 券商 APP「业务办理 → 权限开通 → QMT 量化权限」，审核 1–3 工作日；
3. **程序化交易报告**：首次交易前提交策略类型/频率/资金来源；策略重大变更、资金规模明显变化需变更报告；
4. 确认券商是否下发 **Tushare 白名单**；
5. 确认客户端内置 Python 是 3.6.8 还是 3.12。

> ⚠️ 闭环后外部无法实时熔断，**风控必须内建**：单票上限、单日委托笔数、撤单率、回撤停机。

---

## 4. 外部方案判真伪（含公众号清单）

### 4.1 十种方案

| # | 方案 | 判决 | 理由 |
|---|---|---|---|
| 1 | **内置策略闭环** | ✅ 唯一长期合规 | 监管明确导向；F10 保证交易能力 |
| 2 | **源码烘焙注入** | ✅ 可用，人工一次 | 不涉及运行时 I/O；策略注册本就无法自动化 |
| 3 | **柜台重建持仓** | ✅ **首选** | 官方接口，重启即对齐，天然免疫「文件损坏/丢失」 |
| 4 | **`save_py_key`** | ⚠️ 辅助 | 5000 字节、未公开接口、随时移除 |
| 5 | **板块当数据库** | ⚠️ 辅助 | 容量大但脏、无事务、污染行情界面 |
| 6 | **安装目录内部文件** | ❌ 不可依赖 | F12 单源且被否定 |
| 7 | **HTTP / TCP / 回环桥接** | ❌ **失效且危险** | F5：**停模**，不是报错 |
| 8 | **miniQMT / xtquant 直连** | ❌ 失效 | 关停 + 授权串 |
| 9 | **Tushare 白名单** | ✅ 部分券商 | 只解决「数据进来」，不解决「信号出去」 |
| 10 | **规避监控的外接**（未公开实现） | ❌ **不采用** | 规避终端安全控制；触发防外挂；版本升级即碎；审计上说不清 |

### 4.2 公众号 / 自媒体参考清单

| 来源 | 核心内容 | 我们采纳什么 | 不采纳什么 |
|---|---|---|---|
| 公众号 **WTSolutions**《10月23日QMT会封了哪些功能？写个内置Python 探针，跑一遍就知》(10-22) | **73 项 / 8 组**能力探针；`PASS/BLOCK/WARN/SKIP` 四状态；「结果组合」判定表 | ✅ 吸收分组与四状态判定（**D 组按本机实测移除**，落地为 68 项 / 7 组，见附录 A） | 网盘脚本不直接用（来路不明，需自建可审计版本） |
| 公众号 **WTSolutions** 前篇《传言成真：券商推送 QMT 重要更新》 | 影响面拆解 | ✅ 背景口径 | — |
| 公众号《QMT 权限收紧：外网、本地读写限制，Tushare 等数据源安排更》 | Tushare 白名单安排 | ✅ 白名单事实 | 原文抓取失败（微信反爬），仅作背景 |
| 知乎 **量化君也**《QMT量化策略生存化改造》(10-23) | `save_py_key` / 板块 / 安装目录 三条持久化路子 | ✅ 前两条 | ❌ 安装目录（F12） |
| 知乎《QMT监管新规落地！量化选手的「策略改造生存指南」》(10-11) | **柜台重建持仓**、日志上屏、删网络代码、**一账号一策略** | ✅ **核心采纳**（本方案 §6.1/§6.2） | — |
| 知乎《10.23之后：QMT 改了什么，限制了什么，还能外接吗》(10-26) | 逐项实测矩阵、停模证据、「还能外接」的灰色实现 | ✅ 矩阵与停模（§2.2/§2.3） | ❌ 灰色外接 |
| 微博 **段星星投资气象报告** | 禁库清单；**客户端原生导出不受影响** | ✅ 导出通道（§6.4） | — |
| CSDN **wc_li**《大QMT 内置策略闭环架构：落地改造清单与券商接入要点》(10-30) | 四步改造清单、券商接入要点 | ✅ 结构参考 | — |
| 雪球《mini 即将停用！三套应对方案》 | 文件监控 / HTTP 桥接 | ❌ 均已失效 | — |

> 说明：以上为公开可检索内容，观点归属原作者；方案取舍以本项目实测为准。

---

## 5. 对 qmt_work 的冲击面

### 5.1 通道全部降级

| 通道 | 代码位置 | 状态 | 处置 |
|---|---|---|---|
| `file`（FileSignal） | `connectors/transports/file_signal.py`、`qmt_api.py::poll_once` | 死 | 标 `legacy`，连接即报错 |
| `tcp`（未实现） | 旧方案文档 | 死（**且会停模**） | 停止开发 |
| `redis` / `zmq` | `connectors/registry.py` | 死 | 保留 profile，连接即报 `TransportUnavailable` |
| `http`（gateway） | `transports/http_gateway.py` | 死 | 同上 |
| `inprocess`（xtquant） | `qmt.mini` / `qmt.big.direct` | 死 | 同上 |
| **`embedded`（新）** | 本方案 | ✅ | 见 §6 |

### 5.2 语义层连锁

| 现状 | 新规下 | 处理 |
|---|---|---|
| 后端算信号 → 送进 QMT | 送不进去 | 算法下沉；后端只产「计划」 |
| `events.ndjson` 回传 | 写不了 | 日志行 + 原生导出 |
| 断线重连 + 幂等重放 | 不存在「连接」 | 重启即全量对账 |
| 本地账本做仓位隔离 | 不可靠 | **一策略一账号** + 柜台重建 |
| 策略端 py3.6 门禁 | 可能是 3.12 | 双目标 |
| 前端「实时买入」 | 无法即时生效 | 改「生成计划 / 部署 / 查看执行」 |

---

## 6. 目标架构（v2）

```
┌─ A 盘前备料（外部 · py3.11 后端） ─────────────────────────────┐
│  选股/因子/参数 → plan.json → 烘焙进策略源码（人工覆盖一次）      │
└───────────────────────────┬────────────────────────────────────┘
┌─ B 注入（策略启动瞬间） ───▼────────────────────────────────────┐
│  EMBEDDED_PLAN 校验（版本/日期）→ 柜台重建持仓 → 决定是否只读     │
└───────────────────────────┬────────────────────────────────────┘
┌─ C 盘中闭环（QMT 沙箱 · 全自治） ─▼────────────────────────────┐
│  行情/定时驱动 → 信号 → 内建风控 → passorder                     │
│  持仓：柜台为准 + 内存增量 │ 事件：print("@@QMTW|…")             │
└───────────────────────────┬────────────────────────────────────┘
┌─ D 盘后复盘（外部） ───────▼────────────────────────────────────┐
│  日志 tail + 客户端原生导出 → 三方对账 → 绩效 → 次日 plan.json   │
└────────────────────────────────────────────────────────────────┘
```

### 6.1 持仓真相源 = 柜台（v2 核心修正）

```python
def rebuild_from_counter():
    rows = get_trade_detail_data(ACCOUNT_ID, ACCOUNT_TYPE, 'POSITION')
    pos = {r.m_strInstrumentID + '.' + r.m_strExchangeID: r.m_nVolume for r in rows}
    return {k: v for k, v in pos.items() if v > 0}
```

- **启动**：柜台重建，内存持仓 = 柜台持仓；
- **运行中**：`deal_callback` 增量更新内存，不落盘；
- **账本降级**：只存「柜台没有的信息」（策略标签、当日已发单计数），**丢了不影响正确性**；
- **不再有「只读模式」作为常态**：柜台可查 ⇒ 永远能恢复。只有「柜台查询失败」才降级
  （此时 `print` 明确原因并停止下单，不能拿空仓当满仓）。

### 6.2 一个资金账号只跑一个策略（硬纪律）

重启后策略认领账户**全部**持仓 ⇒ 多策略共用账号时，A 会把 B 的仓位纳入调仓计算。
新规下「仓位隔离」无法靠文件实现，只有三条路：

1. **一策略一账号**（推荐，也是社区共识）；
2. 单账号多策略时，每个策略只在自己「标的池」内计算（弱隔离，有交叉风险）；
3. 用 `save_py_key` 存归属标签（强隔离但依赖未公开接口，不可靠）。

产品上必须显式提示：**同一账号绑定第二个策略时给出警告**，而不是默默允许。

### 6.3 计划的语义（替代「信号」）

盘前产出的是**条件计划**，不是即时指令：

```json
{"id":"p-20261102-01","strat":"etf_rot","window":["09:35","14:50"],
 "rule":{"type":"rebalance","target":[{"code":"513100.SH","w":0.5},{"code":"511990.SH","w":0.5}]},
 "guard":{"max_turnover":0.3,"max_orders":8,"stop_dd":0.06}}
```

盘中由策略自己判定「窗口内是否触发、触发几次、是否被 guard 拦下」。

### 6.4 回传：日志 + 原生导出双通道

| 通道 | 实时性 | 完整性 | 用途 |
|---|---|---|---|
| 日志 `print("@@QMTW|…")` → tail `userdata\log\XtClient_<date>.log` | 秒级 | 中（受轮转影响） | 实时监控、告警 |
| **客户端原生导出**（成交/持仓/委托） | 盘后 | **高**（官方明说不受影响） | 对账基准、绩效 |

日志行格式：`@@QMTW|{"k":"ORDER","s":41,"b":"<boot_id>","t":...,"d":{...}}`，
`BOOT_ID` 解决「重启后 seq 归零」，单行 ≤8 KB。

### 6.5 对账：重启即全量，永不「超时重发」

1. `BOOT_ID` 变化 ⇒ 「有 intent 无 result」全部标 `UNKNOWN`；
2. `UNKNOWN` 一律**先查后动**：查柜台委托（按 `cid=` 备注位）→ 查到认账，查不到才允许重发；
3. **任何情况下不允许「超时重发 PLACE」**；
4. `passorder` 的 `remark` 必须写 `cid=<signal_id>`，这是唯一跨进程锚点。

### 6.6 双 Python 目标

- 语法取交集（不用 `:=`、`match`；f-string 在 3.6 可用）；
- `get_market_data` 返回值：3.6 + 老 pandas 可能是 `Panel`，3.12 + pandas 2.x 是 `DataFrame`
  ⇒ bundle 内加 `_as_df()` 适配层，业务代码不散落 `isinstance`；
- 编码：3.6 时代 `gbk + CRLF` 是硬约束（`gbk+LF` 必 `SyntaxError`，报错位置指向 docstring 中文）；
  3.12 大概率可放宽为 `utf-8`，但**必须实测**。

---

## 7. 还能不能远程下单（问题三）

### 7.1 先给结论

**「程序化实时远程下单」在新规下不可行；「人工远程下单」可以，而且只有这一条路。**

原因是定性问题，不是技术问题：监管要的是「策略在沙箱内闭环、不被外部信号驱动」。
任何「外部发指令 → 策略执行」的实现，都要绕过原生监控——而监控的处置是**停模 + 防外挂弹窗**，
不是拒绝一次调用。拿账户去赌这个，不划算。

### 7.2 五种形态对比

| 形态 | 实时性 | 合规 | 依赖 | 结论 |
|---|---|---|---|---|
| 策略内接收远程指令（socket/文件/HTTP） | 秒级 | ❌ 需规避监控，触发停模 | — | **不采用** |
| **远程改计划**（plan.json → 人工/远控部署） | 分钟~小时 | ✅ | 有人或远控在线 | **主推**（准实时） |
| **远程桌面人工下单**（RDP/ToDesk/向日葵） | 秒~分钟 | ✅ 人工操作，非程序化外接 | 机器开机 + QMT 已登录 + 远控在线 | **唯一「真远程下单」** |
| 券商 App 手工下单 | 秒 | ✅ | 手机 | 最简单 |
| 智能插座断电 | — | ✅ | 智能插座 | **只能急停**，不能下单 |
| 过渡期旧通道（券商未生效） | 秒 | ⚠️ 随时消失 | 券商节奏 | 不做产品承诺 |

### 7.3 产品上怎么表达（重要）

把「远程」拆成三个**可交付**能力，不要做一个假的「远程买入」按钮：

1. **远程监控**：日志回传 → 手机看持仓/成交/异常（✅ 可做）；
2. **远程干预**：一键急停（策略内 `guard.stop` 开关 + 智能插座双保险）（✅ 可做）；
3. **远程决策**：盘前生成/修改计划，落地由本地或远控完成（✅ 可做，延迟诚实标注为「分钟级」）；
4. **远程实时下单**：❌ 不做。前端若保留按钮，必须明确写「需本地确认」，不能给即时反馈的假象。

> 若客户强需求真实时性，只有两条路：换 **PTrade**（券商托管形态，管理面在券商侧），
> 或申请**机构级柜台直连**。个人账户没有第三条。

---

## 8. 怎么落地（问题二）

### P0 · 探测定档（0.5 天，不做完不动代码）

自建探针（不直接用网盘脚本），对齐 **7 组 68 项**（已实现：`scripts/qmt_sandbox_probe.py`）：

| 组 | 项数 | 测什么 | 关键项 |
|---|---|---|---|
| H 运行痕迹 | 4 | `open`/`socket.socket` 来自哪个模块、cwd、策略目录、`sys.path` | **H1/H2 决定拦在 Python 层还是 OS 层** |
| F 文件 | 17 | 绝对/相对路径读写、列目录、建目录、重命名删除、pathlib、json、csv×2、**同目录模块 import**、同目录已有数据读取 | F14 才是「能否自己加库」的真判据 |
| N 网络 | 13 | DNS、TCP 公网/回环、UDP、HTTP、WebSocket、ZeroMQ、局域网、ssl | **N 组会停模 ⇒ 模拟账号 + 只跑一次** |
| K 服务端 KV | 4 | `save_py_key`/`get_py_key`、不存在的键、超长 value | 决定辅助通道是否存在 |
| P 子进程 | 2 | `subprocess`、`os.system` | 预期 BLOCK |
| I 第三方库 | 26 | requests/pandas/talib/zmq/openpyxl… 能否 import | `SKIP` 是没装，不是被封 |
| B 原生 API | 2 | `get_market_data_ex`、`ContextInfo` 属性 | 保底能力确认 |
| ~~D 数据库~~ | ~~5~~ | **已移除**：sqlite/MySQL/Redis 实测全不可用 | 需要复测时 `--with-db` 显式打开 |

> 移除 D 组的两个理由：① 结论已确定，留着只是制造「可能有救」的错觉；
> ② 代价真实存在 —— redis-py 默认重试会让单次 `ping()` 卡 **30 秒**，
> 探针从 0.3 秒变成半分钟，跑一次就不想跑第二次的工具等于没有。

**四状态判定**：`PASS` 可用 / `BLOCK` 被拦 / `WARN` 目标不可达 / `SKIP` 未安装。
**五条铁律**：只有 BLOCK 算封禁；先看 H1/H2；PASS≠能用；F14 是自定义库判据；F15~F17 WARN 是文件没放对。

产出 `output/qmt_probe_<date>.json` + 日志截图存档（客户端升级后重跑对比）。

### P1 · 无状态化（3–5 天）

| 文件 | 改动 |
|---|---|
| `agent_bigqmt/BIGQMT_AGENT.py` | 删 `_ensure_dirs`/`write_status`/`self_probe` 落盘/`events.ndjson`/`STOP` 文件；`load_config` 只认 `EMBEDDED_CONFIG`；`init()` 走「校验计划 → 柜台重建 → 决定是否可交易」 |
| `agent_bigqmt/qmt_api.py` | `execute()` **一行不改**；`poll_once` → `tick(now)`；`_write_response` → `emit()` |
| 新增 `position_source.py` | `rebuild_from_counter()` + `deal_callback` 增量；查询失败 ⇒ 停止下单并打印原因 |
| `connectors/registry.py` | 新增 `qmt.big.closed`；旧 profile 抛 `TransportUnavailable("新规下该通道已关闭")` |
| `scripts/gen_qmt_agent_bundle.py` | `--plan --py 36\|312 --bundle-id`；体积门禁 ≤512 KB |
| `scripts/check_bigqmt_agent_py36.py` | 升级为双目标 `--target 36,312`；禁 `socket`/`urllib`/`requests`/`subprocess`/`open(` |

### P2 · 回传与对账（3 天）

1. 日志跟随：`@@QMTW|` 前缀过滤、坏行跳过、`BOOT_ID`/`seq` 去重、跨日轮转（inode+大小双判据）；
2. 原生导出解析：成交/持仓/委托 → 落库；
3. `ReconcileService`：柜台导出 × 日志事件 × 本地 intent 三方比对，差异未清零前**禁止自动部署新计划**。

### P3 · 运维与产品语义（2–3 天）

1. 前端「实时买入/卖出」→「生成计划 → 部署策略 → 查看执行」三段式；
2. 同一账号绑定第二个策略 ⇒ 显式警告（§6.2）；
3. 远程运维手册：远控软件 + 智能插座急停 + 日志远程查看；
4. 盘前数据落源码：股票池 top-N（N≤200）、因子分位，超体积上限**显式告警**不静默截断。

### 测试与门禁

- 新增：`test_bigqmt_embedded_transport.py`、`test_bigqmt_log_tail.py`、`test_bigqmt_position_source.py`
- **计数三处同源**：`scripts/ci_reconcile.py::EXPECTED_TESTS` + 根 `README.md` 两处
- 前端用例数不在门禁内，需手核

---

## 9. 风险与红线

| 风险 | 后果 | 红线 |
|---|---|---|
| 策略内试探网络 | **策略停模 + 防外挂告警** | 任何 `socket`/`requests`/`zmq` 调用（含 try/except）一律禁止 |
| 依赖安装目录内部文件 | 一次升级全失效 | 禁止作为架构依赖 |
| 依赖 `save_py_key` 单副本 | 接口移除即丢 | 只存「可丢信息」 |
| 多策略共用账号 | 互相认领持仓、重复买卖 | 一策略一账号 |
| 超时重发 PLACE | 重复委托，真实资金损失 | 先查后发，`cid=` 锚点 |
| 风控留在后端 | 闭环后等于无风控 | 全部内建 |
| 探针「编译通过」当「能跑」 | 假绿 | 必须真拉起进程 |
| 用别人的券商结论 | 版本差异误判 | 每台机器跑一次 P0 |

---

## 10. 验收标准（可证伪）

| 项 | 判据 | 反例 |
|---|---|---|
| 零文件 | bundle 静态扫描 `open(`/`os.path`/`sqlite3`/`shutil` 零命中 | 任一命中 |
| 零网络 | `socket`/`urllib`/`requests`/`zmq`/`xtquant` 零命中 | 任一命中 |
| 柜台对齐 | 手工买入 100 股后重启策略，内存持仓含这 100 股 | 不含 |
| 柜台失败 | 断柜台后启动 ⇒ 停止下单 + 打印 `counter_unavailable` | 照常下单 |
| 计划过期 | 部署昨日 plan ⇒ 拒绝并打印 `plan_stale` | 照常执行 |
| 账本一致 | 盘后 `RESULT` 事件数 == 柜台委托数（按 `cid`） | 差 1 笔 |
| 后端离线 | 杀掉后端，策略照常按计划运行 | 策略停摆 |
| 双版本 | 同一 bundle 在 3.6.8 与 3.12 均可 `init` 通过 | 任一 SyntaxError |
| 不重复下单 | 发单后杀进程 → 重启 → 只 1 笔委托 | 2 笔 |

---

## 11. 迁移与回滚

- 旧通道代码**保留不删**，profile 标 `legacy` + 连接即报明确错误（不同券商生效时间不同）；
- 开关 `QMT_CHANNEL=embedded|legacy`，默认 `embedded`；`legacy` 启动时打印醒目警告；
- 文档同步：旧方案文档已加失效横幅；`DEPLOY.md` 路径表把「A/B」改为「C：闭环」。

---

## 12. 实现状态（2026-10-31，第二轮）

### 已落地（可跑、有测试）

| 模块 | 作用 | 对应阶段 | 测试 |
|---|---|---|---|
| `backend/connectors/bigqmt_log_tail.py` | 结构化日志行解析：前缀过滤 / 坏行计数 / `(boot_id,seq)` 去重 / 轮转与半行游标 | P2 回传 | `test_bigqmt_log_tail.py` |
| `backend/connectors/transports/embedded.py` | 闭环传输：只受理计划 op，实时 op 明确拒绝（`ClosedLoopRealtimeUnsupported`） | P1 通道 | `test_bigqmt_embedded_transport.py` |
| `backend/connectors/bigqmt_closed_loop_channel.py` | 胶水：日志 → 意图终态 + canonical 事件 | P2 回传 | `test_bigqmt_closed_loop_channel.py` |
| `backend/connectors/bigqmt_reconcile.py` | 三方对账（意图 × 日志 × 柜台），阻断态与 `unknown_intents` | P2 对账 | `test_bigqmt_reconcile.py` |
| `backend/agent_bigqmt/position_source.py` | 柜台重建持仓 + 成交增量；`None≠[]`、未就绪禁止交易 | P1 无状态化 | `test_bigqmt_position_source.py` |
| `backend/agent_bigqmt/closed_loop.py` | 计划解析 / 时间窗 / 风控闸门 / 意图生成 / `emit()` 回传行 | P1 闭环 | `test_bigqmt_closed_loop.py` |
| `scripts/qmt_sandbox_probe.py` | P0 探针：68 项 7 组，生成 gbk+CRLF 策略 + 解析定档 | P0 探测 | `test_qmt_sandbox_probe.py` |
| `backend/connectors/registry.py` | 新增 `qmt.big.closed`（embedded）+ 失效清单 `RETIRED_PROFILES` | P1 通道 | 同上（registry 断言） |
| `backend/connectors/transports/semantics.py` | `embedded` 事件语义 = `POLL_DIFF` / 上界 5000ms | P1 通道 | 同上 |
| **`backend/agent_bigqmt/BIGQMT_AGENT.py`（控制流切换）** | 双模式：`EMBEDDED_PLAN` 非空即闭环（`init` 校验计划→柜台重建→只读/可交易；`handlebar`→`tick()`；`_emit_event`→`print("@@QMTW|…")`），为空仍是旧文件桥 | P1 闭环 | `test_bigqmt_closed_loop_agent.py` |
| **`scripts/gen_qmt_agent_bundle.py`（`--plan` / `--bundle-id`）** | 把 `position_source` / `closed_loop` 内联进单文件；计划以 **Python 字面量**烘焙进 `EMBEDDED_PLAN` | P1 打包 | 同上 + `test_bigqmt_agent_bundle.py` |
| **`scripts/qmt_closed_loop_verify.py`** | 闭环体检：判据落在 `@@QMTW` 前缀记录（BOOT/STATUS/READONLY/HALT/REJECT），退出码 0/2/1 如实 | P3 运维 | `test_qmt_closed_loop_verify.py` |
| `scripts/check_bigqmt_agent_py36.py` | 白名单加入 `position_source` / `closed_loop`（两者都被内联，运行期无额外文件） | P1 门禁 | 脚本自身 |
| **`frontend-next/src/domains/trading/ClosedLoopNotice.tsx`** | 四项远程能力逐条标注（监控✅/干预✅/决策✅分钟级/实时下单❌）+ 三段式说明；挂在交易页（紧凑）与调仓页 | P3 产品语义 | `tests/closedLoopNotice.test.tsx` |

**真机证据（本机 `bin.x64/pythonw.exe` = Python 3.6.8，2026-10-31）**：
gbk + CRLF 的闭环 bundle（148 KB / 3,546 行）以
`pythonw.exe -u <bundle> <userdata> <ts>` 拉起后正常常驻，输出
`READONLY` + `BOOT` + `STOP` 三条记录并以 0 退出 —— 独立进程模式拿不到柜台
接口，按纪律进只读，**这正是设计要的行为**，不是故障。

### 本轮新发现的三处「会静默出错」的地方（已修）

| 现象 | 真因 | 处理 |
|---|---|---|
| ETF 计划（`513100.SH`）下单被拒：`不符合 stock 标的格式` | `do_place` 读的是 `cfg["default_account_type"]`，而计划配置里写的是 `account_type` | 闭环配置做**单向别名**（`account_type` → `default_account_type`），并让 `REJECT` 回传里点名这条出路 |
| 柜台查询把 ETF 账户传成 `ETF` ⇒ 返回空 ⇒ 当成空仓 | 「柜台账户类型」与「passorder 标的类型」被混成一个键 | 拆出 `query_account_type`（默认 `STOCK`），与 `account_type` 正交 |
| `READONLY` 出现在 `BOOT` **之前**（实测 s=1 → s=2），按时间戳/seq 卡边界会漏掉 | 启动顺序就是先只读再报 BOOT | 体检按 **`boot_id`** 划会话边界，不按 seq/ts |

### 第三轮（运行态搬进 API / 界面）

前两轮把**策略端**打通了，但**外部仍然读不到运行态**：`POST /qmt-agent/diagnose`
只看 `agent_status.json` / `probe_result.json` / `events.ndjson` 三份桥文件，而闭环
一个都不写。于是出现了一个比假绿灯更坏的东西 —— **假红灯**：

```
QMT 客户端在跑但 agent 策略未运行（心跳 stale），请在 QMT 里点「运行」
```

一条正在正常下单的策略被这样报出来，用户照着做就会把它重启掉，当天的仓位
计划直接断在半路。

| 改动 | 作用 |
|---|---|
| `app/routes/qmt_agent.py::_closed_loop_runtime()` | 检测到闭环产物就**换判据**：心跳从客户端日志的 `@@QMTW|` 行推导；判定逻辑**不重写**，直接调 `qmt_closed_loop_verify.evaluate()`，保证界面与命令行同一句话 |
| `POST /qmt-agent/diagnose` | 新增 `closed_loop` 字段（`None` = 旧文件桥）；`heartbeat` 与旧形态**同形**（前端不必学第二套字段名）；`problems[]` 只取 `evaluate()` 的 `[FAIL]` 行 |
| `GET /qmt-agent/closed-loop` | 只读日志的运行态端点，比 `diagnose` 轻，可让前端定时轮询；支持 `log_path` 指定历史日志（排障要看的是**那一天**，不是最新那份） |
| 能力真相 | 闭环下没有 `probe_result.json`，改用 `BOOT.injected`（终端**实际注入**了哪些函数）。**刻意不用** `trading_enabled` —— 独立进程模式下它为 true 却没有 `passorder`，拿它当判据会把「不能下单」显示成「可交易」 |
| `frontend-next/src/services/api/qmtAgent.ts` | 新增 `QmtAgentClosedLoopState` / `closedLoop()`；`heartbeat.ts` 放宽为 `string \| number \| null`（闭环给 ISO 串，旧文件给数字） |
| `QmtAgentDeploy.tsx` 诊断面板 | 新增「运行形态 / 本轮进度 / 不交易原因」三块；心跳**注明来源是客户端日志**；独立进程红字给改法；闭环下「自检未上报」解释成形态决定而非故障 |
| `scripts/gen_contracts.py` | 基线刷新（`rest_endpoints.json` +1 键） |

顺带修掉的假告警：闭环产物不再报「bridge_dir 不存在」—— 它不写文件桥，
那个目录对它是**无关配置**，报出来只会让人去修一个用不上的路径。

测试：`test_qmt_agent_closed_loop_diagnose.py`（19 例，日志行用真实 `emit()`
构造、bundle 用真实生成器、判定走真实 `evaluate()`）+ `tests/qmtAgentClosedLoop.test.ts`（15 例）。
其中「不覆盖心跳」的变异能让两条用例立刻变红，并复现上面那句假红灯原文 ——
说明这组用例是**可证伪**的。

### 尚未接线（明确留白，不是遗漏）

| 项 | 为什么还没做 | 前置条件 |
|---|---|---|
| 前端把「实时买入/卖出」按钮**本身**改成三段式（不是只加说明） | 本轮做的是「不给假象」（加 `ClosedLoopNotice`，逐条标出哪条路还在、延迟多少），**没有删改任何下单按钮** —— 因为按钮的即时反馈语义一改，`OrderForm` 相关的既有用例会成片变红，而交互稿还没定 | 计划格式已冻结（本轮完成），可以开工；建议先定「点了之后看到什么」 |
| 定时作业把 `ClosedLoopChannel.poll()` 挂到事件总线 | 运行态现在能**按需查**（`GET /qmt-agent/closed-loop`），但还没有「后台定时 poll → 意图终态自动推进 → 事件入总线」的作业；需要先定「哪个账户跟哪份日志」的映射与结果落库表，挂了没有归属信息就是脏数据 | 日志发现逻辑已在 `qmt_closed_loop_verify.newest_log()` 与路由层共用（`_closed_loop_runtime`），可直接复用 |
| `gen_qmt_agent_bundle.py --py 36\|312`、`check_bigqmt_agent_py36.py --target 36,312` | 双 Python 目标（3.12 客户端）只在文档里论证过，未实测 | 需要一台 3.12 客户端做真机对照 |
| 盘前计划落源码的**体积告警**（股票池 top-N） | 当前计划只有几十个目标，远未到体积上限 | 出现大股票池需求时再加 |

---

## 附录 A · 探针骨架（3.6 / 3.12 通用）

```python
# coding:gbk
import sys, os, json

def _t(group, name, fn):
    try:
        return {"g": group, "k": name, "st": "PASS", "v": str(fn())[:120]}
    except Exception as e:
        msg = type(e).__name__ + ": " + str(e)[:120]
        st = "BLOCK" if ("Permission" in msg or "Forbidden" in msg or "denied" in msg) else "WARN"
        return {"g": group, "k": name, "st": st, "e": msg}

def init(C):
    r = []
    r.append(_t("H", "open_src", lambda: open.__module__))
    r.append(_t("H", "socket_src", lambda: __import__("socket").socket.__module__))
    r.append(_t("H", "cwd", lambda: os.getcwd()))
    r.append(_t("H", "path", lambda: sys.path[:3]))
    r.append(_t("F", "read_abs", lambda: open(os.path.join(os.getcwd(), "probe_sample.txt")).read()))
    r.append(_t("F", "write_abs", lambda: open(os.path.join(os.getcwd(), "_p.txt"), "w").write("x")))
    r.append(_t("F", "mkdir", lambda: os.mkdir(os.path.join(os.getcwd(), "sub"))))
    r.append(_t("D", "sqlite_mem", lambda: __import__("sqlite3").connect(":memory:").execute("select 1").fetchone()))
    r.append(_t("K", "save_key", lambda: save_py_key("qmtw_probe", "1")))
    r.append(_t("K", "get_key", lambda: get_py_key("qmtw_probe")))
    r.append(_t("B", "quote", lambda: len(C.get_market_data_ex(["close"], ["000001.SZ"], period="1d", count=2))))
    print("@@QMTW_PROBE|" + json.dumps({"py": sys.version, "res": r}, ensure_ascii=False))
```

> **N 组（网络）默认不放进日常探针**：它会导致停模。只在模拟账号、单独一次运行、且明确知道代价时执行。

## 附录 B · 报错对照

| 现象 | 含义 |
|---|---|
| `PermissionError: Forbidden FileIO` | 策略进程文件 IO 被拦（F3） |
| 策略突然消失 + 「防外挂」弹窗 | 触发了网络监控停模（F5），**不是崩溃** |
| `ImportError: cannot import name 'etree' from 'lxml'` | 3.12 下 lxml 版本不匹配（装 Tushare 典型坑） |
| `AttributeError: module 'pandas' has no attribute 'Panel'` | 3.12 + pandas 2.x |
| `ImportError: Forbidden: Module X not in whitelist!` | 第三方库白名单（与文件限制是两套机制） |
| `NameError: name 'save_py_key' is not defined` | 该终端未注入 KV 接口 |
