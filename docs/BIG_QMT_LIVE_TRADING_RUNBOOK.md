# 大 QMT 实盘下单操作手册（Live Trading Runbook）

> **定位**：从「当前状态」到「实盘可下单」的完整操作闭环 —— 怎么做、怎么验、怎么停。
> 架构 / 概念 / 大小 QMT 差异见 [`QMT_大小版本使用说明.md`](QMT_大小版本使用说明.md)，本文不重复。
> **基线**：2026-10-12 实测（bundle 1.2.0 · agent_ver 1.2.0 · QMT 内置 py3.6.8 · 桥目录文件传输）。

## 0. 一句话结论

实盘链路（路由 → 风控 → 执行器 → 桥 → agent → `passorder` → 柜台）**已全线打通并经 paper 模式实测验证**；
当前唯一缺失的是**客户端侧动作**：策略以公式模式 in-process 挂载 + 打开 `trading_enabled`。
三层阻塞及证据见 §1，操作步骤见 §4。

---

## 1. 当前状态基线与阻塞链

一条根因（策略未以公式模式挂载）→ **三层独立阻塞**。Operator 若逐层试错，会连撞三次错误；
用预检工具（§4 Phase 0）可一次列全。

| # | 阻塞点 | 证据（可复跑） | 直接后果 | 归属 |
|---|--------|----------------|----------|------|
| 1 | agent 以 `standalone_process` 运行，未拿到终端注入函数 | `bigqmt_bridge/probe_result.json`：`runtime_mode=standalone_process`、`injected: []`；`agent_status.json`：`injected: []` | 无 `passorder`/`cancel`/`get_trade_detail_data` —— 下单、撤单、委托/账户查询全不可用 | **客户端**（QMT GUI 挂载动作） |
| 2 | `trading_enabled=false` | `P:\stock\gd_qmt\python\agent_config.json` | agent 侧总闸：即使有注入函数也拒单 | **配置**（一条命令可改） |
| 3 | 账户快照从未喂入风控 | 主库 `account_snapshot` 表无行 → `risk.data_source` 停留 `"demo"` | P0-5 闸门拒一切实盘买入（「账户快照未就绪」）；根因同 #1（拉不到券商账户数据） | **平台侧后果**，客户端侧根因 |

> 阻塞 #3 是 Operator **最先撞到的**错误（切 live 后下单即见），但它是最后一层 —— 这就是
> 「必须先跑预检、不要逐层试错」的原因。

**已就绪项**（同样有证据）：QMT 客户端在跑、agent 心跳新鲜、部署件与源码一致（新鲜度校验通过）、
策略已登记进注册树、paper 模式下单→查询全链路闭环（PAPER-9/10 实测）。

---

## 2. 实盘链路全景

```
Trade 页 / REST / MCP / 策略引擎
        │
        ▼
 SignalRouter.submit（唯一交易入口：幂等 key 含 mode+conn_id、单飞、WAL、审计）
        │
        ▼
 RiskManager（风控，mandatory）
   · P0-5 账户快照闸：data_source != live ⇒ 拒实盘买入
   · 占比 / 在途原子化 / 单标的日频次 / 日累计金额 / 日亏损熔断
        │
        ▼
 Executor ──► BrokerAdapter（bigqmt 桥：信封式文件 IPC）
        │
        ▼
 BIGQMT_AGENT（QMT 进程内公式策略）── passorder/cancel ──► 券商柜台
        ▲
        └── SyncEngine 周期拉取 get_positions / get_cash ──►
            feed_account_snapshot() ──► risk.data_source = "live"（P0-5 放行前提）
```

模式分流（`SignalRouter.mode`）：`paper` 本地模拟账本 / `dry_run` 预演 / `live` 上述真实链路。
切模式本身**无护栏**（护栏全部在下单时，fail-closed）—— 所以 §6 的急停流程以「切回 paper」为最快手段。

---

## 3. 前置条件清单

开跑前逐项确认（全部满足再进 §4）：

- [ ] 平台后端运行中：`GET /api/v1/health` 返 200
- [ ] 大 QMT 客户端已登录、行情正常（`GET /api/v1/market/quote?code=513090` 有返回即证明）
- [ ] bundle 已部署且**新鲜**：`GET /api/v1/qmt-agent/status` → `deployed.exists=true`
- [ ] 策略已登记进注册树：`GET /api/v1/qmt-agent/status` 或 `python scripts/qmt_agent_deploy.py check`
- [ ] 已知边界：若日志出现 `connect rc=-1` / `pid X not allowed`，是**券商授权串**
      （`mdl_auth_xttrader=1` + PID 白名单）在拒连 —— 平台无法改写，需联系券商；
      行情（xtdata）不受影响

---

## 4. 六阶段操作步骤

每个阶段都有**验证命令与期望输出**；任何阶段验证不过，停在该阶段排查（§7），不要带着阻塞往下走。

### Phase 0 · 预检（每次操作前后都跑）

```bash
python scripts/qmt_live_preflight.py --db <主库路径>
```

期望（当前基线，2026-10-12）：6 项阻塞 —— `运行时形态`、`passorder`、`cancel`、
`get_trade_detail_data`、`trading_enabled`、`账户快照`；1 项待动作 —— `平台信号模式=paper`。
`--json` 输出可供自动化消费；退出码 0=无阻塞，1=有阻塞。

### Phase 1 · 部署 / 刷新 bundle

```bash
# 文件名必须与注册树条目一致：本机注册名是大写 QMT_WORK_AGENT
python scripts/qmt_agent_deploy.py deploy --filename QMT_WORK_AGENT.py \
    --strategy QMT_WORK_AGENT [--force]
```

- 编码必须 utf-8（默认）：QMT 内置 py3.6 tokenizer 读 gbk 源文件会
  `SyntaxError: encoding problem`，表现为「启动即停止」
- deploy 默认带「用 QMT 内置解释器真跑一遍」前置门禁；部署前自动备份旧件
  （`python/QMT_WORK_AGENT.bak.<ts>.py`）

**验证**：`python scripts/qmt_agent_deploy.py check`；或预检「部署件新鲜度=就绪」
（与当前源码现生成 bundle 逐行比对，防「源码已修、部署件滞后」的漂移假绿灯）。

### Phase 2 · GUI 挂载公式策略（客户端动作，约 30 秒，**无法自动化**）

> 为什么不能自动化：策略列表 = 客户端持久化注册树（`config/user/root/configFormula`
> 加密容器），**不是** `python/` 目录扫描 —— 目录里放两个 md5 完全相同的 .py，重启后
> 一个在列表一个不在。三重证据（运行期整文件字节锁 / GBK 字节搜零命中 / .rzrk 加密）
> 表明逆向写注册树有写坏既有策略的风险，平台不提供该能力。

**路径 A（导入本地策略，推荐）**：

1. QMT 左侧进「模型研究」（找不到走「模型交易」）
2. 策略区空白处 **右键** →「导入本地策略」/「本地.rzrk导入」→ 文件类型选 `*.py`
3. 选中 `P:\stock\gd_qmt\python\QMT_WORK_AGENT.py`
   （可先跑 `python scripts/qmt_agent_deploy.py register --reveal` 让资源管理器自动选中该文件）

**路径 B（新建 + 粘贴）**：deploy 时加 `--txt` 产 `.txt` 副本；「我的」页 → 新建策略 →
Python 策略 → 全选删除模板 → 粘贴全部内容 → **点「编译」保存**（编译/保存才会登记进注册树）。

**注册后必做**：「模型交易」里找到它 → 右键勾选「**自动运行**」
（`startupAutorun=true` 记进 UiSettingConfig，之后 QMT 每次启动自动拉起，零人工）。

**验证**：预检「运行时形态=`qmt_formula`」「注册树登记=已登记」；
`python scripts/qmt_agent_verify.py` 通过（心跳新鲜 + 注入函数捕获 + 注册在位）。

### Phase 3 · 打开 trading_enabled

```bash
python scripts/qmt_agent_deploy.py config --trading
```

然后回 QMT「模型交易」**停止再运行**策略（配置要重启才生效；只改文件不重启 = 运行态仍 false）。

**验证**：`bigqmt_bridge/agent_status.json` → `trading_enabled=true`；预检该项就绪。

### Phase 4 · 平台切 live + 等账户快照

```bash
curl -X POST http://127.0.0.1:21118/api/v1/signal/mode \
    -H 'Content-Type: application/json' -d '{"mode":"live"}'
```

- 切 live 无护栏（设计如此：护栏在下单时 fail-closed），切完**先等快照再下单**
- 等 SyncEngine 喂入账户快照：预检「账户快照=就绪」（最新行 ≤10 分钟）。
  快照来源是 agent 注入的 `get_trade_detail_data` 等函数 —— 这也是 Phase 2 没做会在此撞
  P0-5「账户快照未就绪」的原因
- 模式持久化在 `runtime_config`（`signal.mode`），重启不静默回退

**验证**：预检除「平台信号模式」外全部就绪（该项此时应显示 `live`）。

### Phase 5 · 首单验证（小额，如 513090 × 100 股 ≈ ¥174）

```bash
# 1. 取真实价（保护价，市价单也必须传真实估价）
curl "http://127.0.0.1:21118/api/v1/market/quote?code=513090"

# 2. 预检（allowed=true 才继续；false 时按 reason 处置，不要强闯）
curl -X POST http://127.0.0.1:21118/api/v1/trade/precheck \
    -H 'Content-Type: application/json' \
    -d '{"code":"513090","direction":"buy","volume":100,"price":1.739,"price_type":"limit"}'

# 3. 提交
curl -X POST http://127.0.0.1:21118/api/v1/trade/order \
    -H 'Content-Type: application/json' \
    -d '{"code":"513090","direction":"buy","volume":100,"price":1.739,"price_type":"limit","remark":"live-first-order"}'

# 4. 回读对账（委托/成交/持仓三端点都应在 live 下返券商数据）
curl "http://127.0.0.1:21118/api/v1/trade/orders"
curl "http://127.0.0.1:21118/api/v1/trade/deals"
curl "http://127.0.0.1:21118/api/v1/trade/positions"
```

**验收**：平台委托号 / 成交明细与 **QMT 界面「当日委托」**逐字段一致（方向、数量、价格、状态）；
持仓现金与券商账户对得上。不一致先查 §7，不要继续加单。

### Phase 6 · 常态化运营

- **监控**：`bigqmt_bridge/agent_status.json` 心跳（10s 一拍；新鲜度 = 在跑判据，
  残留文件 ≠ 存活）；预检工具可入定时任务
- **变更纪律**：改 agent 源码 → `gen bundle` → `deploy --force` → QMT 里重启策略 →
  **预检全绿**才恢复下单。源码改完不重部署 = 部署漂移（假绿灯经典形态）
- **撤单**：`POST /api/v1/trade/cancel`，body `{"order_id":"..."}`；
  `conn_id` 必须传对（多账户下传错会撤错单，工具不会静默回退到活跃连接）

---

## 5. 验证速查表

| 阶段 | 命令 | 期望 |
|------|------|------|
| 0 | `python scripts/qmt_live_preflight.py --db <db>` | 阻塞数逐阶段下降 |
| 1 | `python scripts/qmt_agent_deploy.py check` | bundle 就位 / 语法 / 编码全过 |
| 2 | `python scripts/qmt_agent_verify.py` | 心跳新鲜 + `passorder/cancel` 已捕获 + 已注册 |
| 3 | 读 `agent_status.json` | `trading_enabled=true` |
| 4 | 预检 + `GET /api/v1/signal/mode` | `mode=live`，快照 ≤10min |
| 5 | §4 Phase 5 四连 | precheck `allowed=true` → 成交 → 三端点可见 → 与 QMT 界面对账一致 |

---

## 6. 风控与急停（Kill Switch）

分级响应，**从最快/影响最小开始**：

| 级别 | 动作 | 生效范围 | 耗时 |
|------|------|----------|------|
| L1 | `POST /api/v1/signal/mode` `{"mode":"paper"}` | 路由层立即不再发实盘委托（进程/数据不动） | 秒级 |
| L2 | QMT「模型交易」停止 `QMT_WORK_AGENT` | agent 心跳停，桥无应答 | 秒级 |
| L3 | `python scripts/qmt_agent_deploy.py config --no-trading` + 重启策略 | agent 总闸，防误操作重启后自动接单 | 分钟级 |
| L4 | 桥目录放 `STOP` 文件 | agent 主循环消费后改名 `STOP.consumed.<ts>` 并退出 | 一个循环周期 |

- 误下错单：先 L1 断流，再 `POST /api/v1/trade/cancel` 撤单，再按需 L2/L3
- 风控日亏损熔断自动触发（`risk.circuit` 事件 + 通知），无需手工
- 恢复实盘：反向走 §4（L3/L4 后需重开 trading / 重启策略 + 预检全绿）

---

## 7. 故障排查表

| 症状 | 根因 | 处置 |
|------|------|------|
| 预检「运行时形态=standalone_process」 | 未以公式策略挂载（或跑的是外部进程自举） | §4 Phase 2 |
| 已挂载但 `injected: []` | QMT 里跑的不是注册那一个文件 / 旧 bundle | `deploy --force` + 重启策略后重测 |
| 下单 400「账户快照未就绪」 | P0-5：`data_source=demo`，快照未喂入 | §4 Phase 4；确认注入函数就绪且 SyncEngine 在同步活跃连接 |
| 下单返「未捕获到 passorder」 | agent 无注入函数（同 #1） | §4 Phase 2 |
| `connect rc=-1` / `pid X not allowed` | 券商授权串 `mdl_auth_xttrader=1` + PID 白名单 | 联系券商；平台改不了。行情不受影响 |
| 日志里的 `parse error` | 无害噪声（5 个内置策略同样在打） | 忽略 |
| 策略「启动即停止」 | bundle 编码非 utf-8 / 语法错 | deploy 默认 utf-8；`check` 已拦 |
| probe 显示能力缺失但策略刚跑 | 陈旧 probe 快照 | 运行后重测；**陈旧快照只能说「未见」，不能断言缺失** |
| 读注册树 `PermissionError` | 运行期整文件字节锁 | 用主日志 `XtClient_<8位日期>.log` 解析（工具已处理；`os.stat` 判可读是假绿） |
| 「自动运行」不生效 | UiSettingConfig 未勾选 | §4 Phase 2 最后一步 |
| 切了 live 重启后又回 paper | —— 不会：`signal.mode` 持久化在 runtime_config | 若发生即为 bug，报障 |

---

## 8. FAQ

**Q：能不能全自动实盘？**
A：部署、配置、切模式、下单、撤单、对账都可自动化（脚本 + REST）；**唯一不能自动化的是
GUI 注册树写入**（三重证据见 Phase 2）。挂载一次 + 勾选自动运行后，日常重启零人工。

**Q：小 QMT（MiniQMT）能用本文吗？**
A：不能。小 QMT 无「策略」概念（`userdata_mini/` 无注册树、无 `python/`），
实盘能力来自 xtquant 直连（路径 A，受授权串限制），与本文的文件桥（路径 B）是两条路。

**Q：paper 和 live 会串单吗？**
A：不会。下单幂等 key 含 `mode` 与 `conn_id`（2026-10-12 修复：此前 live 单会命中 5s 内的
paper 缓存结果）；查询端点按模式分流（paper 读本地账本，live 走券商）。

**Q：资金校验什么时候生效？**
A：快照喂入后 P0-5 追加「可用资金充足性」校验（`get_cash` 的 cash）；未喂入时不做该校验
—— 把「没有资产上报能力」误判成「没有资金」会错杀正常下单。

---

## 9. 参考

- [`QMT_大小版本使用说明.md`](QMT_大小版本使用说明.md) —— 架构 / 概念 / 接口差异 / 速查表
- `scripts/qmt_live_preflight.py` —— 本文配套预检工具（阻塞链一次列全）
- `scripts/qmt_agent_deploy.py` / `scripts/qmt_agent_verify.py` / `scripts/qmt_diag_report.py`
- `backend/tests/test_paper_mode_queries.py` —— paper 查询闭环守卫（live 路径不变的反向验证）
- [`TECH_DEBT.md`](TECH_DEBT.md) —— TD 台账（本文涉及 TD-23~26 环境护栏、TD-31 载荷消费、TD-33 诊断诚实性）
