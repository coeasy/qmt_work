# QMT 无文件桥下单方案（socket 通道）

> ⛔ **本文的方案 A（TCP/回环 socket 通道）自 2026-10-23 起已失效**：券商收紧范围不止文件 IO，
> 外网与 `127.0.0.1`/`localhost` 的 socket 一并受限（详见 `docs/大QMT_新规闭环改造方案.md` §1 F5）。
> 本文仍有效的部分：**§1 能力定档表**（判定口径 A/B/C/D 的方法论）、**§3 原语实测数据**、
> **§4.5 断线语义**（在闭环架构下演化为「重启即全量对账」）。**不要按 §7 落地步骤施工。**
>
> 背景：大 QMT 后续可能收紧策略进程的文件权限。本文回答「不能读写文件之后怎么下单」，
> 并给出已在本机 `bin.x64/pythonw.exe`（QMT 内置 Python 3.6.8）上实测过的方案。
>
> 实测环境：2026-10-22，`P:\stock\gd_qmt\bin.x64\pythonw.exe` = Python 3.6.8；
> 服务端 `backend/runtimes/cp311/python.exe` = Python 3.11。原始探针脚本见
> `output/_tcp_poc_agent.py` 与 `output/_tcp_poc_server.py`（可复跑）。

## 0. 结论（先看这段）

1. **「QMT 不能读写文件」不可能指「完全不能访问文件系统」** —— 策略进程至少要读自己的
   源码才能被启动，否则任何策略都跑不起来。所以限制必然是下面三种之一，而三种里
   **socket 全部可用**。
2. 推荐做法：**把文件桥换成一个 TCP 长连接**，agent 侧作**客户端**主动连本机后端，
   复用现有 `Executor.execute()`（下单逻辑一行不改），只替换 I/O 层。
3. 已在真解释器上验证：握手、200 单连续往返、200 KB 大帧、对端强杀后自动重连、
   重复 `signal_id` 幂等去重、QMT 先启动后端后启动 —— **6 项全过，0 个坏帧**。
4. 延迟：tick 驱动 **p50 56 ms**；独立线程驱动 **p50 10 ms**。
   （现状文件桥是 500 ms 轮询，两者都比它快。）
5. 真正需要重新设计的不是通道，而是**「断线时订单到哪了」这个语义** ——
   文件桥是天然持久化日志，socket 不是。第 4.5 节给出补偿方案，这是本方案唯一的
   硬风险点，必须一起做。

## 1. 先定性：到底是哪一层不能写

不同层的封锁，可用的通道完全不同。**动手前先花 30 秒判定**，否则会选错方案。

| 口径 | 典型形态 | 文件桥 | socket | 判定方法 |
|---|---|---|---|---|
| A. 只能读自己源码，不能写 | 策略目录只读、沙箱只读挂载 | 死（写不了 `req/`） | 可用 | 策略里 `open('<bridge>/x','w')` 抛 `PermissionError` |
| B. 只能读写自己目录 | 沙箱把进程 cwd 之外全挡 | 死（后端读不到那个目录） | 可用 | 写自己目录成功、写 bridge 目录失败 |
| C. Python 层 `open()` 被拦 | 终端 patch 了 `builtins.open` | 死 | 可用 | `open` 抛 `RuntimeError`/自定义异常而非 `OSError` |
| D. 网络也一起禁 | 出站连接被防火墙/沙箱拦 | 死 | 死 | `socket.connect(('127.0.0.1', <port>))` 抛 `WSAEACCES`/超时 |

只有落到 D 才需要退到第 6 节的兜底路线。A/B/C 三种，socket 都是干净的解法。

**判定脚本（粘进策略里跑一次即可）**：

```python
import socket, tempfile, os
def _probe():
    out = []
    for label, fn in [
        ("write_bridge", lambda: open(os.path.join(BRIDGE, "_t"), "w").write("x")),
        ("write_own",    lambda: open(os.path.join(HERE, "_t"), "w").write("x")),
        ("loopback",     lambda: socket.create_connection(("127.0.0.1", 21119), 1.0)),
    ]:
        try:
            fn(); out.append("%s=OK" % label)
        except Exception as exc:
            out.append("%s=%s:%s" % (label, type(exc).__name__, exc))
    return out
```

## 2. 现状链路：要替换的只有「最后一段」

```
qmt_work 后端 (py3.11)                 文件桥                   大 QMT 策略 (py3.6.8)
BigQmtBridge                            bridge_dir/req/*.json    Executor.poll_once()
  └ FileSignalTransport.invoke()  ──►  写 req/<sid>.json   ──►   读 → execute() → passorder()
                                  ◄──  读 resp/<sid>.json  ◄──   写 resp/<sid>.json
  └ _drain_events()               ◄──  events.ndjson      ◄──   _emit_event()
```

关键结论：**下单能力与文件无关**。`passorder` 是终端注入的函数，`Executor.do_place()`
里没有任何文件操作。文件只承担「把请求送进去、把结果送出来」。

因此改造面被严格限定为四个 I/O 点：

| 点 | 现在 | 无文件后 |
|---|---|---|
| 取请求 | `Executor.poll_once()` 读 `req/*.json` | 从 socket 收 `REQ` 帧 |
| 回结果 | `Executor._write_response()` 写 `resp/*.json` | 发 `RESP` 帧 |
| 发事件 | `BIGQMT_AGENT._emit_event()` 追加 `events.ndjson` | 发 `EVENT` 帧 |
| 读配置 | `load_config()` 读 `agent_config.json` | 用 `EMBEDDED_CONFIG`（已有钩子，`BIGQMT_AGENT.py:117`） |

`Executor.execute()`、`do_place()`、`do_cancel()`、`do_query()`、passorder 三级降级签名
**全部不动**。这也是本方案风险低的原因：业务语义零改动。

## 3. 通道候选矩阵（真机实测能力）

在 `bin.x64/pythonw.exe`（3.6.8）上逐项实跑的结果：

| 原语 | 结果 | 结论 |
|---|---|---|
| `socket` 回环 bind/connect/send/recv | OK（echo 正确） | 主通道可行 |
| 主动 `connect()` 到外部监听 | OK | agent 可作客户端 |
| 非阻塞 + `settimeout` | OK（`socket.timeout` 正常抛出） | 不会卡死 handlebar |
| `select` | OK | 单线程轮询可行 |
| `threading` | OK（线程真的跑起来了） | 可解耦 bar 频率 |
| `ctypes.windll.user32` | OK | 兜底手段存在 |
| `urllib.request` | OK | HTTP 长轮询可行（但仍依赖 socket） |
| `mmap` | OK | 共享内存可行（但仍需文件/命名对象） |

注意：`mmap` 与命名管道本质仍是文件对象，若走口径 C（`open` 被拦）不一定能绕过；
**socket 是唯一不依赖文件系统抽象的双向通道**。

## 4. 推荐方案 A：TCP NDJSON 长连接

### 4.1 为什么 agent 必须是客户端

| 方向 | 问题 |
|---|---|
| agent 监听端口（服务端） | 端口占用/被防火墙弹窗；QMT 重启后端口释放不及时；多账户多策略要抢端口 |
| **agent 主动连出（客户端）** | 无监听端口、无防火墙弹窗、重启后自动重连；后端可以只开一个端口复用多策略 |

所以：**后端 `asyncio.start_server` 监听 `127.0.0.1:21119`，agent 主动连上来**。
绑定地址必须是 `127.0.0.1`，绝不允许 `0.0.0.0`（否则同网段任何人都能下单）。

### 4.2 帧协议（NDJSON，一行一帧）

选 NDJSON 而不是长度前缀，理由：`json.dumps` 已经转义换行，帧边界天然安全；
出问题时 `nc 127.0.0.1 21119` 就能人肉看流量。

```
→ HELLO   {"t":"hello","v":1,"agent_id":"qmt-main","token":"...","py":"3.6.8","ver":"1.3.0","caps":[...]}
← HELLO_ACK {"t":"hello_ack","v":1,"session":"s-7f3a","server_time_ms":...}
← REQ     {"t":"req","signal_id":"...","op":"PLACE","params":{...},"ts":...,"auth":"..."}
→ RESP    {"t":"resp","signal_id":"...","ok":true,"result":{...},"agent":{...},"ts":...}
→ EVENT   {"t":"event","seq":41,"type":"ORDER","data":{...},"ts":...}
← PING / → PONG   （15 s，双向判活）
← STOP    （优雅退出，替代 STOP 文件）
```

约束：
- 单帧上限 1 MiB，超限直接断开（防内存打爆）；
- 读到坏 JSON 只丢该帧并计数，**不断连接**；
- `signal_id` 与 `client_order_id` 同源，agent 用它做幂等键（见 4.5）。

### 4.3 线程模型：两种，按 `passorder` 线程亲和性选

| 模型 | 做法 | 实测 p50 | 适用 |
|---|---|---|---|
| **T1 tick 驱动** | `handlebar` 每 tick 调一次 `pump()` | **56 ms** | 保守默认；下单与终端同线程，最安全 |
| **T2 线程驱动** | 守护线程独占 socket，`handlebar` 只做心跳 | **10 ms** | 延迟敏感；需确认 `passorder` 可跨线程调用 |

T2 的收益不只是快 5 倍，而是**与 bar 频率解耦** —— 公式模式下 `handlebar` 可能只在每根
K 线触发一次（1 分钟），T1 的订单延迟就变成分钟级，T2 仍是 10 ms。当前部署实测是
`runtime_mode=standalone_process`（主循环自转），T1 已经够用，所以：

> **P0 先做 T1，把 `pump()` 放在现有 `handlebar` 里；T2 作为 P2 可选升级。**

无论哪种模型，`pump()` 的铁律：**单次预算 = 一个 tick，按时间退出，不按 syscall 次数**。
（PoC 第一版每个 tick 只 `recv()` 一次，200 KB 帧花了 220 ms；改成「循环 recv 直到
would-block 或超预算」后降到 50 ms —— 这个坑很隐蔽。）

### 4.4 零文件自举

`transport=tcp` 时必须**一次文件都不碰**，清单如下（都在 `BIGQMT_AGENT.py`）：

| 现在会碰文件的地方 | tcp 模式下的替代 |
|---|---|
| `load_config()` 读 `agent_config.json` | 用 `EMBEDDED_CONFIG`（bundle 生成时烘焙进去） |
| `_ensure_dirs()` 建 `req/` `resp/` | 跳过 |
| `self_probe()` 写 `probe_result.json` | PROBE 结果走 `RESP` 帧回传 |
| `write_status()` 写 `agent_status.json` | 心跳 `PONG` 里带 status 摘要 |
| `_emit_event()` 追加 `events.ndjson` | 发 `EVENT` 帧（带 `seq`，断线后按 seq 重放） |
| `run_standalone()` 看 `STOP` 文件 | 收 `STOP` 帧；或后端断开即退出 |

`EMBEDDED_CONFIG` 已存在（`BIGQMT_AGENT.py:117/264`），只需给
`gen_qmt_agent_bundle.py` / `qmt_agent_deploy.py` 加 `--embed-config '<json>'`。
副作用：**token 会以明文出现在策略源码里**。本机部署可接受，但要在 `DEPLOY.md` 里写明。

### 4.5 唯一的硬风险：断线时「这单到底发没发」

文件桥顺带提供了一份**持久化日志**：`req/<sid>.json` 在响应写回前一直存在，进程崩了
重启后还能看到「有一单没回」。socket 没有这个性质 —— 发出 `REQ` 后连接断了，
**客户端无法区分「对端没收到」和「对端已下单但回执丢了」**。

补偿方案（三条一起做，缺一不可）：

1. **幂等键前移**：`signal_id` 直接作为 `client_order_id` 传给 `passorder` 的备注位
   （`do_place()` 已支持，`qmt_api.py:621`）。柜台侧 `remark` 里能查到 `cid=`，
   天然可去重。
2. **重连先对账，绝不盲重试**：连接恢复后，后端对「未收到 `RESP` 的 `signal_id`」
   一律先发 `QUERY_ORDER`/`QUERY_TRADE` 按备注位查，查到就认账，查不到才允许重发。
   **任何情况下不允许「超时就重发 PLACE」** —— 这是重复委托的唯一来源。
3. **agent 侧内存幂等缓存 + 重启声明**：agent 保留最近 N 个 `signal_id → RESP`
   （有界，PoC 里 2000 条），重放命中直接回缓存并标记 `duplicate=true`；
   进程重启后缓存为空，故 `HELLO` 里带 `boot_id`，后端见到新 `boot_id`
   立刻进入「全量对账」模式。

PoC 已验证第 3 条：强杀连接后重放同一 `signal_id`，agent 回 `duplicate=true`，
`executed` 计数不增（201 → 201）。

### 4.6 时序

```
QMT 启动                    后端
  │ connect 127.0.0.1:21119  │
  ├──── HELLO(token,boot_id) ─────►  校验 token，登记会话
  │◄──── HELLO_ACK(session) ───────
  │                                │  用户点「买入」
  │◄──── REQ(PLACE,sid,params) ────  生成 sid
  ├─ execute() → passorder()      │
  ├──── RESP(sid,ok,result) ──────►  resolve 等待中的 future
  ├──── EVENT(ORDER,seq) ─────────►  推给事件总线
  │                                │
  ├──── PING ─────────────────────►  （15 s 一次）
```

## 5. 方案 B：小 QMT + xtquant 直连（彻底绕开策略）

如果不想再维护策略端的任何通道，还有一条**代码里已经存在**的路：
`backend/connectors/registry.py` 里的 `qmt.mini` / `qmt.big.direct`，
走 `xtquant_client`（`XtQuantTrader` 直连），**策略完全不参与下单**。

| 维度 | A. 大 QMT + socket 桥 | B. 小 QMT + xtquant 直连 |
|---|---|---|
| 策略端是否要跑 | 要（且要能连 socket） | **不要** |
| 文件依赖 | 无 | 无 |
| 后端依赖 | 零（纯 stdlib） | 需 `xtquant` 包 + 小 QMT 客户端在跑 |
| 下单延迟 | 10–56 ms | 更低（同进程/本地 RPC） |
| 迁移成本 | 改 4 个 I/O 点 + 后端加一个 transport | **改连接器 key + 确认客户端形态** |
| 风险 | 断线语义（4.5） | 账户权限/客户端形态（极简模式 vs 完整版） |

**建议**：如果这台机器本来就能跑小 QMT，B 其实更省事 —— 它把「策略端」这个最不稳定
的环节整个删掉了。A 的价值在于**保留大 QMT 的策略能力**（回测、图表、终端内调试）。
两者不冲突：`Transport × Dialect` 本来就是正交的，可以同时挂两套、按账户切换。

## 6. 方案 C：兜底（仅当网络也被禁）

| 手段 | 可行性 | 评价 |
|---|---|---|
| `ctypes` + `WM_COPYDATA` 发给后端隐藏窗口 | 实测 `user32` 可用 | 双向、无文件、无端口；但要写消息循环，调试极痛苦 |
| `CreateFileMapping(INVALID_HANDLE_VALUE)` 共享内存 + 命名事件 | 可行 | 纯内核对象不落盘；但**若封锁发生在 OS 层则同样失败** |
| 剪贴板 | 可行 | 会被用户复制操作覆盖，不能用于生产 |
| 读终端自己的日志文件 | 单向 | 只能「收」，无法「发」，不构成通道 |

**不建议**用 `ctypes` 直接调 Win32 文件 API 去绕过策略沙箱 —— 那是在规避终端的安全
控制，既容易被版本升级打断，也可能违反券商终端的使用约定。真落到这一步，正确做法是
走方案 B，或跟券商确认可用的官方接口。

## 7. 落地步骤

### P0（1–2 天，最小可用）
1. `backend/connectors/transports/` 下新增 `tcp_signal.py`：`TcpSignalTransport(BaseTransport)`，
   实现 `invoke()`（发 `REQ`、按 `signal_id` 等 `RESP`、超时抛 `TransportError`）
   与 `_drain_events()`（消费 `EVENT`）。
2. `backend/connectors/registry.py`：加 `qmt.big.bridge.tcp` 一行 +
   `_TRANSPORT_REQUIRED["tcp"] = ("tcp_addr",)`。
3. 后端 lifespan 里起 `asyncio.start_server`（**只绑 127.0.0.1**），端口默认 21119，
   冲突时可配；注意 Windows 保留端口段（`netsh int ipv4 show excludedportrange tcp`）。
4. `backend/agent_bigqmt/qmt_api.py`：把 `poll_once()` 的「读目录」抽成
   `_next_requests()`，文件版与 socket 版各自实现；`_write_response()` 同理抽成
   `_deliver()`。**`execute()` 不动。**
5. `backend/agent_bigqmt/BIGQMT_AGENT.py`：新增 `SocketChannel`（直接照搬 PoC 的
   `output/_tcp_poc_agent.py`，它已按 py3.6 写）；`handlebar` 里按 `cfg["transport"]`
   分派 `pump()`。
6. `scripts/gen_qmt_agent_bundle.py` + `scripts/qmt_agent_deploy.py`：加
   `--transport tcp --tcp-addr 127.0.0.1:21119 --embed-config`，并把 `agent_config.json`
   的可选项写进 `EMBEDDED_CONFIG`。
7. `scripts/qmt_agent_verify.py`：`bundle_health` 增 `transport` 判据 ——
   `transport=tcp` 时校验「bundle 内无 `bridge_dir` 读路径」且 `EMBEDDED_CONFIG` 非空。

### P1（1 天，安全与对账）
8. `HELLO` 校验 token（`hmac.compare_digest`）+ `boot_id`；同 `agent_id` 已有活跃会话时
   拒绝新连接（防本机进程冒充）。
9. 断线对账：`BigQmtBridge` 对「有 `signal_id` 无 `RESP`」的请求先查后发（4.5 第 2 条）。
10. 诊断面板 `backend/connectors/bigqmt_bridge_diag.py` 增 `transport/session/rtt_ms/seq`。

### P2（可选）
11. T2 线程驱动模式（需先在测试账户确认 `passorder` 跨线程可调用）。
12. 事件 `seq` 断点重放（现在文件桥用「字节偏移 + inode」判轮转，socket 用 `seq` 更简单）。

### 测试与门禁
- `backend/tests/` 下新增 `test_bigqmt_tcp_transport.py`：假 agent（asyncio 客户端）↔ transport，
  覆盖 超时 / 断连 / 重复 `signal_id` / 错 token / 超大帧 / 半帧粘包。
- `backend/tests/` 下新增 `test_bigqmt_socket_channel.py`：agent 侧状态机（连接、退避、幂等、坏帧）。
- `scripts/check_bigqmt_agent_py36.py` 必须对新增的 `SocketChannel` 过闸（禁 f-string、
  禁 `asyncio`、禁 3.7+ 语法）。
- 加用例后**三处同源计数**要一起改：`scripts/ci_reconcile.py::EXPECTED_TESTS`
  + 根 `README.md` 两处（测试表 + 目录树）。

## 8. 验收标准

| 项 | 判据 |
|---|---|
| 通道可用 | `nc 127.0.0.1 21119` 能收到 `HELLO_ACK`；QMT 日志出现 `channel connected` |
| 真下单 | `trading_enabled=true` 后，PROBE → 下一笔 100 股限价单，柜台委托列表可见 |
| 断线 | 杀掉后端 10 s 内 agent 自动重连；恢复后 `rtt_ms` 正常 |
| 不重复下单 | 强杀连接后重放同 `signal_id`，柜台只出现 1 笔委托（靠 `cid=` 备注位核） |
| 零文件 | 把 `bridge_dir` 指向一个**不存在的盘符**，全流程仍正常（这是最硬的判据） |
| 延迟 | tick 模式 p95 < 100 ms；线程模式 p95 < 30 ms |

## 9. 实测数据（2026-10-22，`bin.x64/pythonw.exe` = 3.6.8）

```
server listening on 127.0.0.1:59019
hello   : {"t": "hello", ..., "py": "3.6.8", "ver": "poc-0.1"}
seq RTT : n=200 p50=56.0ms p95=65.0ms max=70.0ms     ← tick 驱动（50ms tick）
200KB   : ok=True rtt=52.7ms
re-hello: poc-agent after 0.11s                        ← 对端强杀后重连
replay  : duplicate=True ok=True                       ← 幂等命中，不重复执行
agent   : {"executed": 201, "dupes": 1, "bad_frames": 0}

# 线程驱动
seq RTT : n=200 p50=10.2ms p95=17.0ms max=20.4ms
200KB   : ok=True rtt=10.9ms
re-hello: poc-agent after 0.04s

# QMT 先启动、后端晚 2.5s 才 listen
bound 59255 but NOT listening for 2.5s (agent must retry)
→ 连接成功，全流程 PASS
```

复跑方式：

```
backend/runtimes/cp311/python.exe output/_tcp_poc_server.py tick
backend/runtimes/cp311/python.exe output/_tcp_poc_server.py thread
backend/runtimes/cp311/python.exe output/_tcp_poc_server.py thread late
```
