# 真实下单 513090 阻断链取证 · 与「客户端不自动连大 QMT」修复

> 任务（用户指令）：在客户端调用下单，**真实下单 513090 先打 100 股**，测试是否在 QMT
> 能看到真实订单；客户端**没有自动连接大 QMT**；检查还存在哪些潜在问题，全部优化修改。
>
> 日期：2026-10-14（首轮）· 2026-10-15（二轮：自动连接时序 + 归因修复 + 真机复验）
> 机器：本机（大 QMT `P:\stock\gd_qmt`，账号 22453951，客户端 **v0.5.3 已交付**：
> 2026-10-15 构建，`qmt_work-Setup-0.5.3.exe` + zip + latest.yml，构建后自检
> 29/29、MCP 端到端 133 工具通过、包内无运行期状态）

---

## 0. 一句话结论

**真实订单没有、也无法通过代码「改成能下」——它被卡在一个只有你能做的人工动作上。**

策略 `QMT_WORK_AGENT` 被 QMT 登记成了 **「独立进程 / 原生 Python」** 形态：QMT 用
`pythonw.exe -u <策略.py> <userdata> <ts>` 把它当**外部脚本**拉起，而 QMT **只把
`passorder` / `get_trade_detail_data` 注入「在终端进程内被公式引擎挂载」的那个文件命名空间**。
所以这个进程从出生起就**一个交易函数都拿不到** —— 心跳正常、PROBE 正常、桥显示「已连接」，
但下单与账户查询永远失败。

这不是代码 bug，是**注册形态选错了**。把该策略在 QMT 里改成 **「内置Python」** 形态并重新编译，
交易通道即刻可用（步骤见 §3）。

---

## 1. 真实下单实测：原始响应（未做任何粉饰）

用客户端自己的 API 下单（`output/order_e2e/live_order_513090.py`）：

| 步骤 | 请求 | 原始响应 |
|---|---|---|
| 切实盘 | `POST /signal/mode {"mode":"live"}` | `code=0`（成功切到 live） |
| **下单** | `POST /trade/order {code:513090, direction:buy, volume:100, price:1.73, price_type:limit, conn_id:7a5bc9bd02aa}` | **`code=400`** `风控/执行拒绝：账户快照未就绪：拒绝买入开仓（总资产仍为演示值 1000000，无法核算仓位与资金；请等待账户同步完成或连接券商）` |
| 查委托 | `GET /trade/orders` | **`code=503`** `缺少券商 SDK：…未在该解析路径捕获到函数 get_trade_detail_data…` |
| 查成交 | `GET /trade/deals` | 同上 503 |
| 查持仓 | `GET /trade/positions` | 同上 503 |

**订单根本没到券商**：它在**本地风控闸门**就被拦下了。而这个风控闸门是**故意的正确行为**
（`backend/gateway/risk.py` P0-5：实盘买入时若账户快照还是演示值 100 万，占比闸门形同虚设，
一律拒绝，绝不用假分母放行真单）。

真问题是**上一环**：账户快照**永远不可能就绪**，因为券商通道查不到账户 —— 而这又指向 §2 的根因。

---

## 2. 完整阻断链（四环，每环都有可复算的原始证据）

### 环 1 · 路径 A（xtquant 直连）被**券商授权**封死 —— 与我们的代码无关

`P:\stock\gd_qmt\userdata\log\XtClient_20261014.log`：

```
2026-10-14 19:10:30,169 CIPCManager::init, not auth:mdl_auth_gt_ipc_pair
2026-10-14 19:10:30,335 The XtQuantServer is not allowed to start.
```

同一份服务端下发的授权串里 `mdl_auth_xtquant: 1`、`mdl_auth_xtquant_no_pid_check: 1`，
唯独 **`mdl_auth_gt_ipc_pair: 0`** ⇒ XtQuantServer 起不来 ⇒ 58610 不监听 ⇒
**任何外部 `xtquant.xttrader.connect()` 都不可能成功**。

这条授权串是**服务端下发**的（日志原话 `[auth log] [CProxyClient] receive module auth string`），
本地 `config/modules.lua` 里**没有** `gt_ipc_pair` 这个键（实测 grep 只命中
`mdl_auth_local_python` / `mdl_auth_close_python_md5`）⇒ **本机改不了**，平台侧也改不了。

> 这也解释了客户端为什么「没有自动连接大 QMT」：自动连接走的就是路径 A，
> 而路径 A 在这台机器上**结构性不可能成功**。

### 环 2 · 路径 B（大 QMT 文件桥）本身是通的 —— 但 agent 拿不到交易函数

桥连接 `7a5bc9bd02aa` 是**活的**：

```json
{"conn_id":"7a5bc9bd02aa","status":"connected","connected":true,"active":true,
 "bigqmt":{"connector_key":"qmt.big.bridge.file","transport":"file",
   "agent":{"agent_ver":"1.2.0","py":"3.6.8","funcs":[],"forwarded_funcs":[76 个 xtdata 接口],
            "trading_enabled":false,"runtime_mode":"standalone_process"}}}
```

`bridge_dir/agent_status.json` 心跳新鲜（uptime 8717s），但：

* `runtime_mode: "standalone_process"` —— **agent 自己承认是独立进程**
* `injected: []` —— **注入函数面为空**
* `bridge_dir/probe_result.json` 里 `trade_surface`：
  `{"present": [], "missing": ["cancel","get_trade_detail_data","passorder"], "can_submit": false}`

### 环 3 · 为什么它是独立进程 —— QMT 日志里的**启动命令行**

```
2026-10-14 19:10:48,203 [TC::CTradeStrategyData::doRun]
    execude cmd:  -u "P:\stock\gd_qmt\python\QMT_WORK_AGENT.py" "P:\stock\gd_qmt\userdata" 1791976248203
```

对照 QMT 自带、**确实能交易**的策略，它们走的是**完全不同的另一条日志形态**：

```
2026-10-14 19:11:39,940 [TC::CTradeStrategyData::doRun] doRun: ... m_luaFile:P:\stock\gd_qmt\python\ZT996.py,
    ..., m_scriptType:1, ..., m_runMode:1
```

进程树也印证：`pythonw.exe` **PID 17724 的父进程 = 7072 = `XtItClient.exe`**（QMT 自己 spawn 的）。

**全量日志交叉验证**（`XtClient_2026*.log` 全部 14 次 `execude cmd` 记录）：
出现外部进程启动行的策略**只有 `QMT_WORK_AGENT` 一个**；其余全部走进程内 `m_scriptType:1`。

### 环 4 · 形态还**写在文件上**（可离线自证）

| 策略文件 | 首 16 字节 | 形态 | 能下单 |
|---|---|---|---|
| `QMT_WORK_AGENT.py` | `# -*- coding: utf-8 -*-` （**明文源码**） | 独立进程 | ❌ |
| `国债逆回购.py` | `MmJNMuofqSiOo1t_…`（**单行不透明容器**） | 内置Python | ✅ |
| `新股申购.py` | `MmJNMuofqSiOo1t_…`（同上） | 内置Python | ✅ |
| `ZT996.py` | `MiFBOecYoHXTplIeaO0x…`（同上） | 内置Python | ✅ |

而这三个「容器形态」的策略，在 `XtClient_FormulaOutput_20261014.log` 里**真的拿到了账户**：

```
2026-10-14 19:10:48,468 reqid = 0P:\stock\gd_qmt\python\国债逆回购.py_0003008,
    output = GZNHG:['131810.SZ', '204001.SH'] 22453951 STOCK 单笔买入数量1000000
```

⇒ **同一个账号、同一台机器、同一个 QMT，内置Python 形态可以交易，独立进程形态不能。**
这就是全部答案。

---

## 3. 让真实订单能下的**唯一人工步骤**（约 1 分钟）

> 为什么必须人工：运行形态存在**客户端持久化注册树**里
> （`config/user/root/configFormula`，运行期整文件字节锁，`open()` 一律
> `PermissionError`），写它的唯一入口是客户端自己的 UI 动作。
> QMT 的 CEF 调试口 8086 实测 **0 个 target**（`GET /json/0/json/list` → `[ ]`），
> 模型交易面板是原生 Qt ⇒ **无法脚本化**。详见 `backend/agent_bigqmt/DEPLOY.md` §2.1.1。

1. 打开大 QMT → 「我的」→ **新建策略** → **Python 策略**；
2. 在编辑器的**类型 / 运行方式**里选 **「内置Python」**
   （**不要**选「原生Python / 独立进程」）；
3. 全选删掉模板代码，粘贴 `P:\stock\gd_qmt\python\QMT_WORK_AGENT.py` 的全部内容；
4. 点 **「编译」**；
5. 在「模型交易」里找到它 → 右键 → **设置自动运行** → 停止再运行（**改形态后必须重启策略**）；
6. 最后把 `P:\stock\gd_qmt\python\agent_config.json` 的 `trading_enabled` 置 `true`
   并**再次重启策略**（安全阀只在 `init()` 读一次）。

### 自证（每一步都有判据，不用猜）

```bash
# ① 形态对不对（本机实测：现在是「独立进程 ⇒ 永远下不了单」，改完必须变成「内置Python」）
python scripts/qmt_agent_deploy.py check --strategy QMT_WORK_AGENT --filename QMT_WORK_AGENT.py

# ② 文件是否已编译成公式体（改完后首 200 字节应无空格、非 # 开头）
python -c "print(open(r'P:\stock\gd_qmt\python\QMT_WORK_AGENT.py','rb').read(200)[:80])"

# ③ QMT 日志里应**只**出现 m_scriptType:1，**不再**出现 execude cmd -u <本文件>
# ④ agent 自证：bridge_dir/agent_status.json 里 runtime_mode 变成 qmt_formula、
#    injected 含 passorder、trading_enabled=true
```

改完后重跑本次实测脚本即可看到真实委托：

```bash
python output/order_e2e/live_order_513090.py --conn-id 7a5bc9bd02aa
```

---

## 4. 本次实际修复的问题（代码 / 文档 / 测试）

用户要求「检查还存在哪些潜在问题，全部优化修改」。以下是**已发现并已修**的项，
每一项都有可复算的根因与回归锁。

### 4.1 部署文档与工具把「下不了单的形态」当成推荐路径（★ 直接造成本次故障）

`DEPLOY.md` §3.1 原文把「导入本地策略」与「新建+粘贴」写成**二选一、等价** —— 这是错的：
前者得到独立进程（永远下不了单）。已在三处修正：

* `DEPLOY.md` §3 新增**两种运行形态对照表**（含 `m_scriptType:1` vs `execude cmd` 判据）
  与「形态写在文件上」的判据；
* `DEPLOY.md` §3.1 **只推荐「新建策略 → 内置Python → 编译」**，「导入本地策略」明确标注
  **仅行情/回测可用**；§8 故障表新增「心跳正常、显示已连接，但下单/查询全失败」一行；
  §11 首次验证清单新增「运行形态 = 内置Python」一项；
* `scripts/qmt_agent_deploy.py` 新增 `run_mode_of_strategy()`：从**主日志**与**文件形态**
  两条独立证据判定形态，`check` 会醒目报出 `独立进程（原生 Python）—— 永远下不了单`
  并给出修法与自证判据；`register --wait` 在登记成功后会**继续校验形态**，形态不对返回码 2。

### 4.2 客户端**永远不自动连大 QMT 文件桥**（用户明确反馈）

`_auto_connect_active()` 只探测**路径 A**；且它只要拿到 conn_id 就收工 —— 而路径 A 在这台
机器上会**建出一条连不上的连接**（`last_error=交易连接异常：WaitingFreeWriter instances exceed
maximum limit`）后就再也不往下走，桥因此永远不会被自动接上。

* `xtquant_client/autoconnect.py` 新增 `detect_bridge_agents()` /
  `build_bridge_connection()`：从 `<QMT根>/python/agent_config.json`（兼容旧布局）
  读部署配置，校验 `bridge_dir` 存在 + `agent_status.json` 心跳未过期，**不猜账号、不生成 token**；
* `app/bootstrap/phase_broker.py`：`_auto_connect_active` 改为
  **路径 A 真连上（`adapter.is_connected()`）才收工，否则兜底走路径 B**；新增
  `_auto_connect_bridge()` 与 `_find_bridge_conn()`（按归一化 `bridge_dir` 幂等复用）；
* `xtquant_client/manager.py` 新增 `repair_bridge_config()`：桥的 token/账号/目录漂移时
  重建适配器（只改 cfg 不生效）。

### 4.3 「已连接但下不了单」的**假绿灯**

`gateway/health._check` 只看 `adapter.is_connected()` ⇒ agent 活着就挂绿灯，而它拿不到
`passorder`（本次实测状态）。存活 ≠ 可用，这是本项目反复出现的假绿灯家族。

* `connectors/bigqmt_bridge.py` 新增 `capability_gap()`：**O(1) 零 IO**（复用
  `connector_probe` 的本地缓存），严格区分「未知 / 有能力 / 确知无能力」三种语义；
* `gateway/health._check`：适配器若报出能力缺口 ⇒ `health_status` 如实降级为
  `needs_action` 并把可执行处置写进 `last_error`；缺口消失自动恢复。
  **未实现该方法的适配器行为完全不变**（只增不减）。

### 4.4 回归锁（新增 39 个用例）

| 文件 | 新增 | 锁什么 |
|---|---|---|
| `tests/test_bigqmt_deploy_scripts.py` | +8 | 两种形态都能从日志判出；中文名 cp1252 mojibake 必须还原；无日志证据只能标「推断」；旁路日志不得被当主日志；`check` 必须给出修法；**`check` 在独立进程形态下退出码必须非 0（2）、健康形态必须 0**（见 4.6） |
| `tests/test_autoconnect.py` | +14 | 桥发现的全部准入/拒绝口径（心跳过期、alive=false、目录不存在、跨根去重、坏 JSON）；**路径 A 未连通必须兜底走桥**；路径 A 真连通时不得多建桥；**守护不得用「路径 A 有没有候选」当闸门**；守护必须尊重关闭开关与手动断开 |
| `tests/test_broker_capability_health.py` | +10（新文件） | 能力缺口必须降级为 needs_action；缺口消失恢复；**无该方法适配器行为不变**；诊断抛异常不得炸巡检；`capability_gap` 零 IO |
| `tests/test_sync_state.py` | +4（并修复 5 条存量红） | 冷热分区流的 `zone → 流名` 映射；`zone=cold/hot` 各写各的流、互不覆盖；`zone` 缺省推断（`mode=full` ⇒ both，否则 hot）；旧库回退读遗留 `sync.bars` |
| `tests/test_sync_bars_full.py` | +1（并修复 1 条存量假失败） | `sync_many` 不传 `plans` 时的缺省计划语义（`zone=auto` ⇒ `cold=True`；`zone=hot` ⇒ 不碰冷回补） |

### 4.5 复检本轮改动时**又抓到**的三处

用户的要求是「**检查还存在哪些潜在问题，全部优化修改**」，所以修完之后又对同一条链路做了
一次复检。以下三处是复检的产出：一处仍在「不自动连大 QMT」家族内（4.5.1），两处是
**同源的测试腐化** —— 契约变更没配套证伪判据，于是套件长期红着/假绿（4.5.2 / 4.5.3）。

#### 4.5.1 自动连接守护有**第二条**复现路径（`phase_watchdogs._broker_auto_connect_guard`）

§4.2 修的是**启动那一刻**的自动连接。而启动后还有一条每 30s 复探的守护，它自带一个
前置闸门：先 `detect_candidates()` + `pick_active()`，**拿不到运行中的 xtquant 客户端就
`continue`** —— 于是路径 B 永远走不到，桥永远不会被复探接入。

这与 §4.2 是同一个错误的两种写法（都用「路径 A 有没有」当唯一闸门）。已改为**直接调用
`_auto_connect_active()`**（它内部先 A 后 B，且在「A 无候选且 B 无 agent」时是**纯 no-op**、
不建任何连接）。顺带消掉一次**重复的**全盘 `detect_candidates` 扫描（旧写法每轮扫两遍）。
循环体抽成 `_auto_connect_once()`，以便单测直接驱动而不用去推 30s 死循环。

#### 4.5.2 冷热分区流的**测试覆盖为零**，导致 5 条用例长期静默变红

v0.5.2 把日线同步的**写入**从 `sync.bars` 拆成 `sync.bars.hot` / `sync.bars.cold`，
但**没有任何用例断言这件事**，而 `tests/test_sync_state.py` 里 5 条由 `_run_bars` 驱动的
用例仍在读遗留流名 `STREAM_SYNC_BARS` —— `last_run()` 恒返回 `None`，套件一直红着。

这不是「测试写错了」这么简单：**契约变更没有配套的证伪判据**，于是变更本身有没有接对
（zone 推断对不对、两条流会不会互相覆盖）**无人可证**。已补 4 条把映射 / 接线 / 缺省推断
/ 旧库回退全部锁死，并修好那 5 条。

#### 4.5.3 一条「单跑绿、全量红」的假失败：命题写得不完整

`tests/test_sync_bars_full.py::test_incremental_does_not_page_or_filter` 断言
「增量模式压根不碰区间接口」（`hub.calls == []`），但它**没有钉 `zone`**。而
`sync_many()` 在不传 `plans` 时的缺省计划是 `SyncPlan(hot=True, cold=(zone != "hot"))`
—— `zone` 缺省 `auto` ⇒ `cold=True` ⇒ 会走冷回补，而冷回补**本来就要**打区间接口。

于是该断言是否成立，取决于 `BarsSyncer._cold`（进程级单例 `get_cold_store()`）有没有被
**别的用例**初始化过：

| 运行方式 | `_cold` | `_sync_one_cold` 行为 | 结果 |
|---|---|---|---|
| 单跑本文件 | `None` | 取数**前**就返回（「冷仓未初始化」） | 通过（1.5s） |
| 全量套件 | 已被前面的用例初始化 | 真的去取区间 | 假失败（`assert ['20260101'] == []`） |

**结论：探针命题不完整，不是产品缺陷。** 修法是钉住 `zone="hot"`（回到它本来要测的
「热增量不翻页」），并**新增一条用例把 `sync_many` 缺省计划的语义钉成可见事实** ——
缺省计划绕过 `_build_plans` 为 `auto` 写的保守判据（无条件 `cold=True`），
`app/sync/datasets.py` 正走这条路。**本轮不动产品行为**：缺省偏保守在语义上说得通，
且 `_sync_one_cold` 对「冷仓未初始化 / 源不支持区间」都有优雅降级；要改需先确认
`datasets.py` 的冷回补是否**有意为之**。留作独立议题。

### 4.6 体检工具自己「假绿灯」：打出 `[FAIL]` 却 `return 0`

`scripts/qmt_agent_deploy.py check` 是本报告 §3「自证」里让用户跑的那条命令。它在本机
**正确**判出了「独立进程 ⇒ 永远下不了单」并打了一大段 `[FAIL]`，但**退出码是 0**：

```console
$ python scripts/qmt_agent_deploy.py check --strategy QMT_WORK_AGENT --filename QMT_WORK_AGENT.py
运行形态     : **独立进程（原生 Python）—— 永远下不了单**
[FAIL] 该策略被 QMT 以**独立进程**方式运行 ⇒ 终端不会注入任何交易函数。
$ echo $?
0        # ← 改前
```

后果：`if qmt_agent_deploy.py check; then echo "部署就绪"; fi` 会得到「部署就绪」，
而这台机器**一单都下不出去** —— 又一处「工具说谎比没有工具更坏」。

已改为：**独立进程形态 ⇒ 退出码 2**（2 = 「已部署但需人工动作才能下单」，与
`register --wait` 超时的退出码、`qmt_agent_verify.py` 的 0/2 语义一致；1 仍留给
「环境缺失 / QMT 未找到」）。健康形态（内置Python）仍返回 0。实测：改前恒 0，改后返回 2。
两条用例分别锁正面与**证伪面**（健康形态不得非 0，否则自动化会把正常部署拦下）。

---

## 4A. 二轮修复（2026-10-15）：自动连接**时序** + 误导归因

首轮（§4）修的是「**走不到**路径 B」；二轮真机复验时发现机制通了但**太慢 / 归因错**。
用户原话「客户端 没有自动连接大qmt」在二轮拿到了完整的时序证据链。

### 4A.1 真机时序取证：桥不是不连，是**被路径 A 拖死 + 守护晚 30s**

验证窗口 08:42:06→08:42:49（fresh profile + dev Electron + 自动连接开启）：

| 时刻 | 事件 | 证据 |
|---|---|---|
| 08:42:06 | 启动预算 8s 内**探测都没跑完**（冷缓存，热缓存实测 2.84s/4 候选）⇒ 交后台守护 | 日志 `启动期 8s 内未完成本机客户端探测` |
| +30s | 守护 `while True: sleep(30)` **先睡后跑** ⇒ 首轮 08:42:39 才开始 | 守护循环旧顺序 |
| 08:42:39 | 守护首轮建路径 A 连接（xtquant auto-loaded） | 日志 `xtquant auto-loaded from client dir` |
| 08:42:44 | 路径 A 的交易目录解析中…… | 日志 `XtQuantTrader 交易目录按客户端模式解析` |
| 08:42:49 | 验证脚本关机 —— **路径 A 的慢失败（41.87s 起）还没结束，桥兜底根本没跑到** | 连接列表 `[]` |

⇒ 两个时序缺陷叠加：**① 守护先睡后跑（首轮 +30s）；② 路径 A 的连接尝试无 bound**
（`add_connection(cfg, True)` / `activate()` 内部 `_safe_start` → `adapter.start()`，
本机慢失败 41.87s 起，`bridge_client._READY_TIMEOUT=100s`），把整轮自动连接拖死。

### 4A.2 修复一：连接动作加**硬超时**（`phase_broker._bounded_connect_action`）

* 新增 `_bounded_connect_action(fn, *args)`：`asyncio.wait_for(to_thread(fn), CONNECT_HARD_TIMEOUT=32s)`，
  超时返回 `(None, False)`；
* `_auto_connect_qmt_client` 的 `activate` / `add_connection`、`_auto_connect_bridge` 的两个分支
  全部改走它 —— 超时即按「未连通」放行，调用方继续走路径 B；
* 超时**不取消底层线程**（`to_thread` 取消不掉，取消只会让状态停在半路）：此刻连接对象已在
  manager 里注册（`_build` 先注册再 `_safe_start`），由 `BrokerHealthMonitor` 后台重连接管结局；
* 回归锁：`test_path_a_add_connection_hang_does_not_starve_path_b` /
  `test_path_a_activate_hang_does_not_starve_path_b`（把 32s 缩成 250ms 桩，
  断言「探过 A + 必然探 B + 桥连上时返回桥 conn_id + 整轮 < 5s」）。

### 4A.3 修复二：守护**先跑再睡**（`phase_watchdogs._broker_auto_connect_guard`）

* 循环体从 `sleep → run` 改为 `run → sleep`：首轮在相位装配结束时**立即执行**，
  与 `phase_broker` 的启动自动连接无缝衔接（那次可能刚被 8s 预算掐断）；
* 连带修掉一个潜伏缺陷：**「无事可做」不得计入失败退避**。旧实现把
  `_auto_connect_once()` 返回空串一律当失败 ⇒ 连接稳定连上后每 30s 白涨一节拍，
  几分钟后退避到 300s 上限，真掉线时要等最多 5 分钟才复探。现在已连上 / 手动断开 /
  开关关闭都会清零退避，保持 30s 基础节拍；
* 回归锁：`test_guard_runs_first_round_before_sleeping`（order 断言 run 先于 sleep）、
  `test_guard_idle_rounds_do_not_inflate_backoff`（连续空闲轮 `slept == [30, 30]`）。

### 4A.4 修复三：能力门禁被**路由层前缀**伪装成「没连券商」（误导归因）

二轮首次真机下单时抓到：桥**已连接**，live 下单 503 的消息却是
「未连接任何券商客户端：请到「券商连接」页添加并连接券商。（当前通道不具备下单能力…）」——
前缀与事实相反，把用户引向「去连一个已经连上的券商」。

* `app/routes/trade.py`：`broker_unavailable` 分支**原样透传** router 的 reason
  （删掉无条件 `MSG_NO_BROKER` 前缀）；路由层只做 HTTP 语义映射，病因由各拒绝点自负；
* `gateway/signal_router.py`：两个 reason 富化 —— conn 闸门 missing 带出
  「注册表里没有这条连接；请到「连接管理」核对」、`_live()` 无桥场景自带完整 `MSG_NO_BROKER` 引导；
* 回归锁：`test_order_broker_unavailable_reason_passes_through_without_false_prefix` +
  `test_live_order_capability_gap_is_actionable_and_not_misattributed`（断言真实归因在 msg 中、
  误导前缀**不在**、门禁先于执行 `orders == []`）。

### 4A.5 修复四：前端 banner 吞掉多行可执行指引

能力门禁的 `last_error` 是 `\n原因：…\n处置：…` 多行文案，而
`frontend-next/src/domains/trading/trade.module.css` 的 `.banner` 没有
`white-space: pre-line` ⇒ HTML 默认把换行折成空格，用户看到的是一行长句。
已加 `white-space: pre-line`。

### 4A.6 二轮门禁与真机复验

见 §7.2（真机复验脚本 `output/order_e2e/live_autoconnect_e2e.py` 的全绿记录）。

---

## 5. 顺带发现、但**刻意未改**的项（判断 ≠ 缺陷）

* **实盘下单被「账户快照未就绪」拦下**：这是 P0-5 的**正确**安全设计（不用演示值 100 万当
  分母放行真单）。它在本机表现为「误导性文案」只是因为上游查不到账户 —— 修好 §3 之后
  文案与事实就一致了。**不改**。
* **`mdl_auth_gt_ipc_pair=0`**：服务端下发，本地无对应键 ⇒ 平台侧不可修，如实上报。
* **注册树加密容器（`XTF1`）的写入算法**：DEPLOY.md §2.1.1 已论证「逆向写入的收益与风险
  严重不成比例（写错=用户 35 个策略条目一起损坏）」⇒ **明确不做**，正确做法是走 QMT 自己的
  「编译」动作（即 §3）。

## 6. 未做 / 遗留（逐条给出「为什么不现在做」）

* **`GET /brokers/auto-detect` 尚未把桥候选一并返回给界面**。自动连接已在「启动 + 30s 守护」
  两处生效（§4.2 / §4.5.1），但连接管理页的「重新探测」按钮仍只列 xtquant 客户端。
  **为什么不做**：这是对外契约面的新增字段，且字段必须有消费者 —— 只加后端字段而不做界面渲染，
  恰好会造出本项目明令禁止的**载荷孤儿**（`audit_payload_orphans.py` / TD-31 家族：
  「后端多算、前端零消费」）。要做就得「后端字段 + 界面渲染 + 契约基线同步」一起做，
  属于**功能增量**而非本次的**故障修复**，故留作独立任务。
* **`sync_many` 缺省计划偏保守（`zone=auto` ⇒ `cold=True`，绕过 `_build_plans` 的 auto
  判据）**，`app/sync/datasets.py` 正走这条路（§4.5.3）。已用一条用例把事实钉住，
  **未改产品行为** —— 要改需先确认该处冷回补是否**有意为之**（改错的后果是数据集同步
  静默不再补冷历史）。属独立议题。
* ~~**打包客户端仍是旧产物**~~（v0.5.3 已交付，2026-10-15 20:25）：本轮全部修复已进入
  安装包（`qmt_work-Setup-0.5.3.exe` 200 MB + `qmt_work-0.5.3.zip` 255 MB +
  `latest.yml` version=0.5.3），构建后自检 **29/29**（启动 / REST 冒烟 / RSI 契约 /
  SPA / WS / 窗口渲染 / 零残留）、MCP 端到端 **133 工具**通过、zip 根项仅
  `_internal` + `qmt_work.exe`（无 master.key / app.db / 配置 / 日志）。
  换构建后 `ORDER_FLOW_E2E_VERIFICATION.md` 与本文 §7.2 的实测记录需按新构建复测。

---

## 7. 门禁复核（本轮结束时的实测值，不是抄来的）

| 门禁 | 命令 | 实测 |
|---|---|---|
| 后端全量 | `cd backend && python -m pytest -q -p no:randomly`（cp311） | **2544 passed / 10 skipped / 0 failed**（收集 2554） |
| 计数对账 | `python scripts/ci_reconcile.py` | 后端用例 2554 ✓ · 前端组件 75 ✓ · 注册页 46 ✓ · README 计数 ✓ · 前端 API 全命中契约 ✓ · 行尾 ✓ |
| 前端单测 | `cd frontend-next && npm run test:serial` | **57 文件 / 556 用例全通过**（README 原写 555，已同步） |
| 文档断链 | `python scripts/audit_doc_links.py` | **断链 0 处**（已知有意缺失 7 项，逐条带理由） |
| 裸 except 水位线 | `pytest tests/test_no_bare_except_pass.py` | 3 passed（**恰好回到 151 上限**，本轮新增的吞异常一律走 `core.errors.swallow(exc, why=...)`） |

> **本轮之前的套件是 9 failed**，逐条定性（这一步是必须做的：不区分「我弄坏的」与
> 「本来就坏的」，就会把存量问题当成自己的锅去乱改，或反过来把回归当成存量放过）：
>
> | 类别 | 条数 | 判定依据 |
> |---|---|---|
> | **本轮新引入的回归** | 3 | 2 条 `test_audit_doc_links`（新增的取证文档未登记进索引 + 文档里出现裸 `ZT996.py` 引用）、1 条 `test_no_bare_except_pass`（水位线 151→152） |
> | **存量** | 6 | 5 条 `test_sync_state`（冷热分区流改名未同步用例）+ 1 条 `test_sync_bars_full` 假失败 |
>
> 定性手法：把改动 `git stash` 后重跑同一批文件 —— 存量项在**未改动的基线**上照样红。
> 修完后 **0 failed**。

### 7.1 收尾时又抓到的两处（自我纠错，记在这里以免下次重犯）

**① 行尾整文件翻转（TD-39 同型）。** 收尾复核 `git ls-files --eol` 时发现
`backend/tests/test_autoconnect.py` 变成 `i/lf w/mixed` —— HEAD 是纯 LF，工作区
被整文件写成了 CRLF，`git diff --stat` 因此显示 **893 插入 / 536 删除**，而
`git diff --ignore-cr-at-eol` 显示真实改动只有 **+357 / −0**。
⇒ 已按字节归一化回 LF；三个**新建**文件（`test_broker_capability_health.py`、
`scripts/cdp_order_flow.py`、`scripts/order_flow_test.py`）同样全 CRLF，一并归一化。
判据命令：`git ls-files --eol | awk '{i=substr($1,3);w=substr($2,3);if(i!=w)print}'`。

**② 用例数多算 1。** 先前把 `EXPECTED_TESTS` 与 README 写成 **2554**，实测逐文件收集
为 **2553**。实证法（不靠心算）：`git show HEAD:<file> | grep -c 'def test_'` 对比现状，
六处增量为 14 + **7** + 7 + 1 + 4 + 10 = **43**（此前把 `test_bigqmt_deploy_scripts`
误记为 +8，实为 +7）⇒ 2510 + 43 = 2553。已同步 `scripts/ci_reconcile.py`
（常量与注释 `+44`→`+43`、`+8`→`+7`）与根 `README.md` 的**两处**写死数字
（§测试与验证表 463 行、目录树 544 行）。
（随后又补了 1 条「单轮只探一次候选」的用例 ⇒ 收集数回到 **2554**，四处已再次同步。）

**③ 一次「并行负载假红」（非回归，已定性）。** 在同时跑 `ci_reconcile.py`、
逐文件收集、前端套件的情况下，`test_datasource.py::test_shell_detail_network_enrichment_is_time_bounded`
报了一次 FAIL。**单跑该文件 20 passed**，空闲机器重跑全量 **2543 passed / 0 failed**。
成因：该用例用墙钟断言 `dt < 2.5`（内部预算 `_DETAIL_ENRICH_BUDGET=2.0`，正常耗时 ≈1.0s），
三进程抢 CPU 时事件循环被饿过 2.5s。**判定为非回归、不churn 该护栏**
（改阈值会削弱它抓「原 5.6s 无时限」回归的能力），改为登记进 flake 清单。

### 7.2 二轮真机复验：`output/order_e2e/live_autoconnect_e2e.py`（33/33 全绿）

fresh profile + dev Electron + 自动连接开启，全流程可复算、零 mock。**2026-10-15 19:0x
两连跑：首跑 32/33（唯一失败是验证脚本自己的判据缺陷，见 7.2.2），修正判据后复跑 33/33，
耗时 59.8s。**

#### 7.2.1 自动连接提速的实测效果（用户反馈的核心故障）

| 时点 | 首轮验证（08:42，修复前） | 复验（19:05，修复后） |
|---|---|---|
| 客户端就绪 | 13.5s | 18.1s |
| 桥连上时刻 | **就绪后 +38.1s 仍未连**（脚本关机时连接列表 `[]`） | **就绪时已连上**（轮询等待 0.0s） |
| 路径 A 状态 | 慢失败 41.87s 起，把整轮拖死 | 启动预算 8s 内未连上即交后台，last_error 如实「已转入后台自动重连」 |
| 桥连接 | 无 | `841ae96f08cc` connected=true **active=true** |

⇒ 两个时序修复（§4A.2 硬超时 / §4A.3 先跑再睡）在真机上把「就绪后 38s+ 仍未连通」
变成「就绪即连通」。路径 A 依旧如实呈未连接（本机授权封死，不伪装），桥兜底顶上并成为
active 连接。

#### 7.2.2 复验中抓到的**验证工具自己假告状**（TD-33 家族新成员）

首跑唯一失败项：「订单未到达 agent（桥 resp 无 PLASE/passorder 新响应）」报
`新响应 6 个，其中 PLACE 类 6 个` —— 而 live 下单**全部被能力门禁拦在下单前**，
QMT 日志 513090 命中零增量，订单根本不可能到桥。

逐字节查那 6 个 resp 文件：全是查询类响应（`get_trade_detail_data` 缺失报错）与
agent meta。**"PLACE" 字样来自每个响应都内嵌的 agent meta `actions` 广告清单**
（`["PROBE","PLACE","CANCEL_ORDER",...]`）—— 那是**协议支持的动作名**，不是发生了下单。
裸 grep 字符串把「能力清单」读成了「行为记录」，与 TD-33「诊断工具假告状」同族。

修正（`_resp_is_order_evidence`，结构化判据，正反两面都验证过）：

* 下单证据仅两类：① `ok=true` 且 `result` 带订单标识（order_id/合同编号/委托编号/entrust_no）；
  ② 错误出自交易侧（passorder/拒单/风控/委托合同/下单失败）；
* 证伪面：拿真机观察到的 10 个非订单响应离线跑分类器，全部 `False`；
* 证真面：合成「下单成功」「交易侧拒单」两种响应，全部 `True`。

复跑该检查的输出本身就是审计素材：
`新响应 21 个（agent meta×1、live查询503×8、行情探测失败×11、其他×1），下单证据 0 个`。

#### 7.2.3 其余阶段的实测结论

* **能力门禁归因**（§4A.4）：live 下单 503 消息含「不具备下单能力 / 独立进程 / 公式策略」，
  不含「未连接任何券商客户端」、不含「账户快照未就绪」；显式带桥 conn_id 同样被门禁拦；
  不存在 conn_id 被拒且归因带出「请到连接管理核对」；
* **live 查询 503 不返假空列表**：委托/持仓为「缺少券商 SDK（get_trade_detail_data）」，
  成交一次为「券商响应超时（>12s）」—— 两种归因都诚实，故断言只钉 503 不钉文案；
* **paper 回归**：已注册未连接 conn_id ⇒ `PAPER-3 filled`；桥 conn_id ⇒ `PAPER-4`；
  不存在 conn_id 仍被拒；委托查询闭环（本地账本 4 笔可查回）；
* **健康诚实性**：桥 connected=true 且 `health_status=needs_action` + 可执行处置
  （「典型成因：该策略被 QMT 登记成了独立进程/原生Python…」）；
* **零残留**：关机后 electron 残留 0 个（脚本只杀自有 profile 的进程，不碰用户的 QMT）。

### 7.3 全量回归抓到的 **flaky**（单跑绿 / 全量红）——测试设计缺陷，非产品缺陷

二轮新增的 `test_guard_runs_first_round_before_sleeping` 在全量跑里挂过一次
（同代码重跑全量又全绿 ⇒ 竞态）。逐层定位：

**现象**：`assert order == ["run", "sleep"]` / `assert slept == [60.0]` 偶发失败；
同一次全量里还出现 `PytestUnhandledThreadExceptionWarning`。

**根因（测试桩打了全局）**：用例里
`monkeypatch.setattr(pw.asyncio, "sleep", _fake_sleep)` —— `pw.asyncio` 就是
**全局 asyncio 模块**，改它的 `sleep` 会击中**进程内所有**后台任务。而
`app_client`（session 作用域）的 lifespan 把一大批后台循环装在 TestClient 的
portal **线程**上：system broadcast(5s) / pump guard(2s) / health monitor(5s) /
条件单(2s) / 自动连接守护(30s)，它们全都 `await asyncio.sleep(...)`。用例的
patch 窗口（约 0.2s）里任一线程醒来调到测试桩：① 把断言桶 `slept`/`order`
污染成假失败；② 桩里抛的 `_StopGuard` **炸死生产任务**——正是那条
`PytestUnhandledThreadExceptionWarning` 的来源。

**修复一（测试隔离，根因）**：新增 `_ThreadScopedSleepShim` —— 只替换
`pw.asyncio` 这个**模块属性**（不再碰全局 `asyncio.sleep`），且对**其它线程**
透明委派真实 `asyncio.sleep`，只有发起用例的线程拿到测试桩；
`_fake_once` 同样只记录发起线程的调用。两个守护用例全部改走它。

**修复二（产品语义顺带收紧）**：`setup()` 抽出 `_install_auto_connect_guard(app)`，
`broker_auto_connect=False` 时**不装配**守护任务 —— 与 docstring「完全不介入」
的语义一致（旧实现照样 create_task，一个每 30s 空转一圈的任务），同时也从测试
会话里移除了一个与用例抢模块级全局的隐形方。该开关只从配置文件读取、无运行时
切换口，按启动时取值决定装配是完备的。回归锁
`test_guard_installed_only_when_switch_on` 正反两面（开⇒装配 / 关⇒不装配；
装配条件写反的后果是自动连接整体停摆，即用户报过的故障）。

**验证**：组合跑（appcontext + capabilities + 三个守护用例）**5/5 轮全绿**；
最终全量 **2570 passed / 0 failed**。教训归入「测试桩不得打进程级全局」家族：
patch 的对象必须是**模块自己的属性**，且要考虑会话级 fixture 留下的活任务。
