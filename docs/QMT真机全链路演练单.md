# QMT 真机全链路演练单

> 对应计划：`二轮复查与优化改进计划（2026-08-27）.md` → **N6 验证缺口 / V1 真机演练**
> 目的：验证 `xtquant_client/xtp.py` / `discovery.py` / `manager.py` 的**深度诊断路径（进程探测 / 端口探测 / 配置一致性）**与「启动 → 订阅 → 下单 → 撤单 → 对账」全链路在实际 QMT 机器上符合预期。
> 环境：装有 QMT 客户端（任意券商，如国信 iQuant / 广发 / 银河）的 Windows 机器。
> **安全：全程使用仿真/测试账号 + 最小手数（1 手），绝不实盘。所有命令在 PowerShell 执行。**

---

## 环节 0 · 预检（约 3 分钟）

```powershell
# 1) 确认客户端已登录，且该账号已开启「程序化交易/策略交易权限」
#    （极速版 MiniQMT / 已开通策略交易权限账号；否则交易连接会 rc=-1 'illegal pid'）
#    ⚠️ 修改权限后需重启客户端，否则客户端缓存旧的权限配置。
# 2) 确认后端端口未被占用
Test-NetConnection -ComputerName 127.0.0.1 -Port 8218 -InformationLevel Quiet
# 3) 确认某只活跃标的（如 601288.SH）
```

---

## 环节 1 · 启动后端（约 2 分钟）

```powershell
# 源码运行（若用 exe 则直接双击 qmt_work.exe）
cd p:\github_public\qmt_work\backend
& "P:\python\miniforge3\envs\py312\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8218
```

**预期**：日志出现 `Uvicorn running on http://127.0.0.1:8218`，无 ABI 报错。

---

## 环节 2 · 接入券商并触发诊断（约 5 分钟）

```powershell
# 0) ABI 运行时矩阵（确认主端→桥接方案能匹配窗口客户端）
Invoke-RestMethod -Uri "http://127.0.0.1:8218/api/v1/brokers/runtimes" | ConvertTo-Json -Depth 5

# 1) 添加连接（account_id 可留空由客户端配置自动补全；broker_id 留空由 Config.xml 券商名自动识别）
$body = @{ broker_id=""; client_path="P:\stock\guoxin_iquantV3\userdata"; account_id=""; account_type="STOCK"; autoconnect=$true } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8218/api/v1/brokers" -Body $body -ContentType "application/json" | ConvertTo-Json -Depth 5
```

**预期（关键诊断点 ① 连接）**：返回 `connected: true`；记录返回的 `conn_id`，随后

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8218/api/v1/brokers/{conn_id}/health" | ConvertTo-Json
```

应 `status:"ok"`。

```powershell
# 2) 深度诊断：进程/端口/配置一致性全量探测
Invoke-RestMethod -Uri "http://127.0.0.1:8218/api/v1/brokers/diagnostics?deep=1" | ConvertTo-Json -Depth 6
```

**预期（关键诊断点 ② 进程探测）**：`connections[].client_type` 为 `full` / `mini` / `both` / `none` 之一；能识别已运行的 `XtItClient.exe` / `XtMiniQmt.exe` / `miniquote.exe`。

**预期（关键诊断点 ③ 端口探测）**：行情端口 `58610`、完整版交易端口 `58600` 被探测；`client_mode=full` 走 `userdata`，仅 mini 独跑走 `userdata_mini`。

**预期（关键诊断点 ④ 配置一致性）**：无「路径猜测 / 账号冲突」告警；`config` 探针输出与登录的券商 & 账号一致。

---

## 环节 3 · 订阅验证（约 5 分钟）

```powershell
# 1) 实时行情（tick 快照，非交易时段可能为空——属已知行为）
$q = Invoke-RestMethod -Uri "http://127.0.0.1:8218/api/v1/market/quote?code=601288.SH&conn_id=CONN_ID"
#   用返回的 latest/last/close 作为下单价（限价单 price 必须 >0，默认关闭价格偏离校验）
$PRICE = $q.data.price ?? $q.data.last ?? $q.data.close
"现价参考: $PRICE"
$q | ConvertTo-Json

# 2) 日 K 线（历史数据，不依赖实时登录）
Invoke-RestMethod -Uri "http://127.0.0.1:8218/api/v1/market/kline?code=601288.SH&period=1d&count=5" | ConvertTo-Json -Depth 4

# 3) 订阅推送（WebSocket）：另开终端连 ws，观察 order/trade 事件
$ws = New-Object System.Net.WebSockets.ClientWebSocket
$ws.ConnectAsync([Uri]"ws://127.0.0.1:8218/api/v1/ws", [Threading.CancellationToken]::None).GetAwaiter().GetResult()
"ws opened"
# 收到事件时 type 应为 broker.* / order.* / trade.* / account_snapshot
```

**通过标准**：K 线返回 ≥1 根且字段含 `open/high/low/close`；WS 能建立连接。

---

## 环节 4 · 交易链路：下单 → 查询 → 撤单 → 对账（关键，约 15 分钟）

```powershell
# 0) 取真实现价（限价单 price 必须 >0，且须为预检查/下单/幂等重发共用同一值）——
#    沿用环节 3 的 $PRICE；若换终端请先用 GET /market/quote?code=601288.SH 重新取价
if (-not $PRICE) { $PRICE = 5.00 }   # 仅演示占位，正式请从行情接口读取
"将使用下单价: $PRICE"

# 1) 预检查（只读，风控前哨）—— 期望 ok:true 且 allowed:true
$pc = @{ code="601288.SH"; direction="buy"; volume=100; price=$PRICE; price_type="limit" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8218/api/v1/trade/precheck" -Body $pc -ContentType "application/json" | ConvertTo-Json

# 2) 下单（限价，price>0 且 volume>0；农业银行示例用价格可直接成交，成交最小手数）
$o = @{ conn_id="CONN_ID"; broker_id=""; code="601288.SH"; direction="buy"; volume=100; price=$PRICE; price_type="limit"; source="manual" } | ConvertTo-Json
$r1 = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8218/api/v1/trade/order" -Body $o -ContentType "application/json"
$r1 | ConvertTo-Json -Depth 4
$ORDER_ID = $r1.data.order_id   # 记录供撤单/对账

# 3) 核实幂等——price 是幂等哈希的一部分（key 含 code:direction:volume:price:price_type），
#    必须用与 #2 完全相同的请求体原样重发，5s 窗口内应返回 duplicated:true 且不二次成交。
$r2 = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8218/api/v1/trade/order" -Body $o -ContentType "application/json"
$r2 | ConvertTo-Json -Depth 4
"幂等标记: $($r2.data.duplicated)"   # 期望 True

# 4) 撤单（用上一步 order_id）
$c = @{ conn_id="CONN_ID"; order_id=$ORDER_ID } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8218/api/v1/trade/cancel" -Body $c -ContentType "application/json" | ConvertTo-Json

# 5) 对账三连
Invoke-RestMethod -Uri "http://127.0.0.1:8218/api/v1/trade/orders"     | ConvertTo-Json -Depth 4
Invoke-RestMethod -Uri "http://127.0.0.1:8218/api/v1/trade/deals"      | ConvertTo-Json -Depth 4
Invoke-RestMethod -Uri "http://127.0.0.1:8218/api/v1/trade/positions"  | ConvertTo-Json -Depth 4
```

**通过标准（关键诊断点 ⑤ 对账）**：

- 下单返回 `code:0` 且 `order_id` 非空；重复提交触发 `duplicated:true` 且券商仅一次性成交；
- 撤单后 `trade/orders` 对应单状态为 `已撤`，成交数不再增加；
- `trade/deals`、`trade/positions` 与券商客户端界面逐条一致；
- WS 端在 `order` / `trade` 事件上收到对应推送（验证订阅回路）。

---

## 环节 5 · 数据导出与收尾（约 3 分钟）

```powershell
# Feather 导出（仅源码运行可用；EXE 因排除 pyarrow 降级 CSV/JSON）—— 断言目标目录产出文件
$sync = @{ dest_dir="P:\github_public\qmt_work\kline_data\drill"; codes=@("601288.SH","600000.SH"); periods=@("1d","1w"); count=250; format="feather" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8218/api/v1/market/kline/sync" -Body $sync -ContentType "application/json" | ConvertTo-Json -Depth 4
Get-ChildItem "P:\github_public\qmt_work\kline_data\drill"   # 应有 *1d.feather / *1w.feather

# 清洁：删除演练连接 + 清缓存
#   DELETE /brokers/{conn_id}
#   DELETE /market/kline/cache
```

**通过标准**：导出文件数 === 标的数 × 周期数；用 `KlineCache.read_export` 反向读取列齐全。

---

## 演练记录模板（验收输出）

| 环节 | 断言 | 结果 ✓/✗ |
|---|---|---|
| 启动 | 8218 正常监听，无 ABI 报错 | |
| 诊断 ①②③④ | client_type / 端口 / 配置一致性符合预期 | |
| 订阅 | K 线 ≥1、WS 连通 | |
| 交易 | 下单 → 幂等 → 撤单 → 对账一致 | |
| 数据 | Feather 导出文件齐全 | |

> 附：故障日志（`*_probe_diag` 输出 / `brokers/diagnostics` 快照）一并归档。