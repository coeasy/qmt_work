# 券商接入指南（Broker Onboarding）

> qmt_work 量化交易网关 · 多券商 / 多账户 / 多客户端版本接入手册
> 最后更新：2026-08-22

本指南说明如何在 qmt_work 中新增一家券商（或一种新的客户端类型），并解释连接、适配、运行时兼容与零 mock 的约束。所有行情 / 交易 / 账户接口都走券商**真实 SDK**，平台**不返回任何模拟数据**。

---

## 1. 架构总览

券商接入由五层组成，**上层只依赖抽象，不感知具体券商**：

```
前端「券商连接」页
        │  REST /api/v1/brokers/*
        ▼
app/routes/broker.py        ── 连接 CRUD / 连接 / 切换 / 健康
        │
        ▼
xtquant_client/manager.py    ── BrokerManager（多连接、活跃连接、持久化）
        │  create_adapter(broker_id, ...)
        ▼
xtquant_client/registry.py   ── BrokerProfile 目录 + Registry（能力协商 / 热插拔）
        │
        ▼
xtquant_client/{xtp,ths,ptrade,juejin}.py ── 具体 BrokerAdapter 实现
        │
        ▼
券商客户端（迅投 MiniQMT / 同花顺 / PTrade / 掘金 …）真实 SDK
```

关键文件：

| 文件 | 职责 |
|---|---|
| `xtquant_client/registry.py` | `BrokerProfile` 数据类、`BROKER_PROFILES` 内置目录、`Registry`（V2 能力协商 + 热插拔）、`get_profile` / `create_adapter` / `list_profiles` |
| `xtquant_client/base.py` | `BrokerAdapter` 抽象基类、统一异常（`BrokerError` / `BrokerNotConnectedError` / `BrokerSDKError`）、统一响应约定 |
| `xtquant_client/manager.py` | `BrokerManager`、`ConnectionConfig`、`Connection`、连接生命周期（load/add/connect/disconnect/set_active） |
| `xtquant_client/xtp.py` | `XTPQuantAdapter`（迅投系共用，覆盖国金/华鑫/银河/中信建投/兴业/广发/恒生UF），含多版本 xtquant 兼容与桥接 |
| `xtquant_client/adapters/ths.py` `ptrade.py` `juejin.py` | 同花顺 / PTrade / 掘金 适配器（SDK 就绪后启用） |
| `app/routes/broker.py` | 券商连接 REST 接口（`/brokers`、`/brokers/profiles`、`/brokers/{conn_id}/connect` …） |
| `app/config.py` | 全局配置（含引导连接环境变量） |

---

## 2. 新增一家「内置券商」（代码层）

绝大多数券商（迅投系）**无需写新适配器**，只需在 `registry.py` 的 `BROKER_PROFILES` 追加一条 `BrokerProfile`，并复用 `XTPQuantAdapter`。

### 2.1 追加 BrokerProfile

```python
# xtquant_client/registry.py
BrokerProfile(
    id="mybroker",                       # 唯一 id，前端/API 用此标识
    name="某券商 QMT",                    # 展示名（前端「券商连接」页列出）
    adapter="xtp",                       # 适配器实现：xtp / ths / ptrade / juejin
    default_client_path=r"C:\某券商\userdata_mini",  # 默认客户端路径（提示用）
    supported_account_types=["STOCK", "CREDIT"],     # STOCK/CREDIT/OPTION/FUTURES
    supported_periods=["1m","5m","15m","30m","60m","1d","1w","1mon"],
    sdk_required="xtquant",              # 运行所需 SDK 的 import/pip 名
    min_version="迅投 xtquant",
    multi_version=True,                  # 是否参与多版本 ABI 兼容（见 §4）
    capabilities=["quote","kline","trade","account","positions"],
    note="某券商 MiniQMT",
)
```

字段说明：

- `id`：全局唯一；会被 `get_profile(id)` 查找。请勿与内置 id 冲突。
- `adapter`：对应 `create_adapter` 中的分支。迅投系一律用 `"xtp"`。
- `supported_account_types`：决定 `Registry.effective_capabilities` 派生出的 `option`/`credit`/`futures` 能力。
- `default_client_path`：仅为前端自动填充提示；真实路径由用户在「券商连接」页填写（应指向客户端根下的 `userdata_mini`）。
- `multi_version`：标记该券商是否走多版本 xtquant 兼容（桥接）逻辑。

追加后，**前端「券商连接」页会自动列出该券商**，无需改前端代码。

### 2.2 需要新适配器时（非迅投系）

当券商使用完全不同的 SDK（如已规划的 同花顺 / PTrade / 掘金）时，需实现 `BrokerAdapter` 子类，并在 `create_adapter` 增加分支：

```python
# xtquant_client/adapters/mybroker.py
from xtquant_client.base import BrokerAdapter, BrokerSDKError

class MyBrokerAdapter(BrokerAdapter):
    @property
    def broker_name(self) -> str: return "某券商"
    @property
    def adapter_id(self) -> str:   return "mybroker"
    @property
    def client_version(self) -> str: return "v1.0"

    def start(self) -> None:
        # 建立与券商客户端的连接（行情 + 交易）
        ...

    def close(self) -> None: ...
    def is_connected(self) -> bool: ...

    def get_quote(self, code: str) -> dict: ...
    def get_full_tick(self, codes: list[str]) -> dict: ...
    def get_kline(self, code, period, count, start="", end="") -> list[dict]: ...
    def get_tick(self, code: str) -> dict: ...
    def get_stock_list(self, sector="沪深A股") -> list[dict]: ...
    def subscribe_quote(self, codes, on_tick) -> None: ...

    def get_account(self) -> dict: ...
    def get_positions(self, symbol=None) -> list[dict]: ...
    def get_cash(self) -> dict: ...
    def get_orders(self) -> list[dict]: ...
    def get_deals(self) -> list[dict]: ...

    def place_order(self, code, direction, price_type, price, volume,
                    strategy_name="", remark="") -> dict: ...
    def cancel_order(self, order_id: str) -> dict: ...
```

然后在 `registry.py` 顶部 `from .adapters.mybroker import MyBrokerAdapter`，并在 `create_adapter` 中补：

```python
if profile.adapter == "mybroker":
    return MyBrokerAdapter(...)
```

**统一契约（务必遵守）**：
- 行情/交易/账户全部走券商真实 SDK，**禁止任何假数据**。
- 券商未连接 / SDK 缺失时，方法抛 `BrokerNotConnectedError` 或 `BrokerSDKError`，由上层转换为 503/400，前端展示「未连接券商客户端」。
- 抽象方法（标注 `@abstractmethod`）必须实现；可选方法（`search_stocks`/`cancel_order_price`/`get_sector_list` 等）有默认实现，子类按需覆盖。
- `test_connection()` 默认用 `is_connected()` + 轻量查询验证；行情模式（未配 `account_id`）只用行情查询，避免把「未填交易账户」误报成故障。

---

## 3. 连接配置（ConnectionConfig）

每个「券商连接」是一条 `ConnectionConfig`，由用户在「券商连接」页填写或经 API 提交：

| 字段 | 说明 |
|---|---|
| `conn_id` | 连接唯一 id（为空自动生成） |
| `name` | 连接展示名 |
| `broker_id` | 券商档案 id（对应 `BrokerProfile.id`） |
| `client_path` | 券商客户端 `userdata_mini` 目录（**关键**，填错会导致连不上已运行的客户端） |
| `account_id` | 资金账号（行情模式可留空） |
| `account_type` | STOCK / CREDIT / OPTION / FUTURES |
| `session_id` | 迅投会话 id（整数，多账户/多会话时用） |
| `min_version` | 版本提示（可选） |
| `active` | 是否活跃连接（活跃连接是行情/交易/账户的默认来源） |

连接配置持久化到 `broker_connections` 表，应用启动时可自动重连活跃连接。

---

## 4. 多版本 xtquant 兼容（桥接）

迅投官方 `xtquant` 仅发布 `cp36 ~ cp312` 变体；本项目主后端打包运行时为 **Python 3.13**，与券商 `xtquant` ABI 不兼容。`create_adapter` 据此自动选择连接方案：

1. **进程内直连**：当券商 `xtquant` 的 ABI 包含主后端 Python 版本（或 `xtquant` 未定位、无法判断）时，直接 `XTPQuantAdapter.start()`。
2. **子进程桥接（Bridge）**：当 ABI 不兼容且无兼容运行时时，`require_runtime_or_raise` 给出清晰可操作提示；若检测到兼容运行时（bundled 或系统），则通过 `BridgeAdapter` 拉起一个 **ABI 兼容的独立 Python 子进程** 加载 `xtquant`，经 IPC 与主后端通信。
3. **SDK 缺失即报错**：`xtquant` 完全不可用（无兼容运行时）时抛 `BrokerSDKError`，提示用户确认 `client_path` 与客户端登录状态——**绝不静默退回进程内并在 `start()` 时才崩**。

> 版本兼容路线图与打包说明不随仓库发布；调试「EXE 起不来」多为测试方法假象（端口锁/残留实例），并非代码 bug——以 `scripts/` 下的正确验证流程为准。

---

## 5. 运行时热插拔（无需改代码 / 无需重启）

> ★ 2026-10-09 更正：本节原写「热插拔专用端点 `profiles/hotplug`（在 brokers 域下）」——
> **该端点并不存在**（契约 254 个 REST 端点里查无此项）。热插拔实际是**内建在新建连接
> 里**的自动行为，文档此前把一个内部函数当成了对外端点。本次同时新增了
> `scripts/check_doc_endpoint_links.py` 门禁：文档里声明的每个 REST 端点都必须能在
> 契约里查到，防止这类「文档写了不存在的接口」再次溜过。

### 5.1 实际触发方式：新建连接时自动登记

`POST /api/v1/brokers`（新建连接）里，若传入的 `broker_id` **不在内置档案中**，
路由会自动调用 `xtquant_client.registry.hotplug_profile()` 把它登记为
「通用迅投 XTP 适配器」档案，然后照常建立连接：

```jsonc
// POST /api/v1/brokers
{
  "broker_id": "custom1",          // ← 任意未知 id，会被自动登记
  "client_path": "C:\\custom\\userdata_mini",
  "account_id": "12345678",
  "account_type": "STOCK"
}
```

登记时写入的档案形如（见 `app/routes/broker.py`）：

```json
{
  "id": "custom1", "name": "<由 agent_broker_name 推导>",
  "adapter": "xtp",
  "supported_account_types": ["STOCK", "CREDIT", "OPTION", "FUTURES"],
  "note": "自动登记：QMT 全券商通用迅投适配器"
}
```

> 注意：`connector_key` 非空（大 QMT 桥接 / 文件桥等）时**不走**这条自动登记路径。

### 5.2 需要在代码里显式登记时

只有**后端/脚本内**才直接调函数（它不是 HTTP 端点）：

```python
from xtquant_client.registry import hotplug_profile
hotplug_profile({
    "id": "custom1", "name": "自定义券商", "adapter": "xtp",
    "default_client_path": r"C:\custom\userdata_mini",
    "supported_account_types": ["STOCK"],
    "capabilities": ["quote", "kline", "trade", "account", "positions"],
})
```

内置档案不可删除；自定义档案可经 `Registry.unregister_profile` 删除。

---

## 6. 引导连接（环境变量）

不想在 UI 里手动添加时，可用环境变量在启动时自动建立默认连接（**不自动连接**，待应用启动后由 lifespan 拉起）：

| 变量 | 说明 |
|---|---|
| `QMT_BROKER_ID` | 券商档案 id（默认 `guojin`） |
| `QMT_CLIENT_PATH` | 客户端 `userdata_mini` 目录 |
| `QMT_ACCOUNT_ID` | 资金账号 |
| `QMT_ACCOUNT_TYPE` | STOCK / CREDIT / OPTION / FUTURES |
| `QMT_SESSION_ID` | 迅投会话 id（整数） |

配置优先级：环境变量(`QMT_*`) > exe 同目录 `qmt_work_config.json` > `.env` > 默认值。

---

## 7. REST 接口速查（券商连接）

> 统一响应包：`{"code":0,"message":"ok","data":{}}`；业务异常返回 **HTTP 200 + `code != 0`**（`code=503` 表示尚未连接券商客户端）。本机回环请求免鉴权。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/brokers` | 当前连接列表（未连接时 `data:[]`，**零 mock**） |
| GET | `/api/v1/brokers/profiles` | 可接入券商档案列表（内置 + 自定义） |
| GET | `/api/v1/brokers/auto-detect` | 自动发现本机 QMT/MiniQMT 客户端（进程 + 目录扫描） |
| GET | `/api/v1/brokers/runtimes` | ABI 运行时矩阵、主后端 Python、bundled 运行时 |
| POST | `/api/v1/brokers` | 新增连接（body: `broker_id`/`client_path`/`account_id`/`account_type`/`session_id`/`active`/`autoconnect`） |
| POST | `/api/v1/brokers/test` | 探测连接可用性（不落地，附环境诊断） |
| POST | `/api/v1/brokers/{conn_id}/connect` | 连接（含客户端路径/SDK 预检，失败返回 503 + 可操作诊断） |
| POST | `/api/v1/brokers/{conn_id}/disconnect` | 断开（清活跃 + 取消重连任务） |
| POST | `/api/v1/brokers/{conn_id}/active` | 设为活跃连接（行情/交易/账户默认来源） |
| DELETE | `/api/v1/brokers/{conn_id}` | 删除连接 |
| GET | `/api/v1/brokers/{conn_id}/health` | 单连接健康状态 |

---

## 8. 排障要点

### 8.1 `connect` 失败：先读诊断，再动手

`POST /api/v1/brokers/{conn_id}/connect` 失败时返回的是**结构化多行诊断**，不是一句「连不上」。
处理顺序：先看 `### ★ 客户端侧根因（优先按此处理）` 一节（它带**客户端日志原始证据**），
再看 `### 已实测的排查事实`，最后才是 `### 官方四步排查`。

| 诊断里的 `reason` | 含义 | 处理 |
|---|---|---|
| `pid_not_allowed` / `strict_check` | 客户端启用了「量化连接严格校验」，对本平台进程做 **PID 白名单校验**（客户端日志：`quant session N, pid X not allowed, return`） | 该开关由**券商服务端下发的模块授权**控制（`mdl_auth_xttrader_strict_connection_check=1`、`mdl_auth_xtquant_no_pid_check=0`），**平台侧无法改写**。联系券商为该资金账号申请「程序化交易 / 外部策略接入」授权，并把调用进程加入白名单 |
| 路径类 | `client_path` 指向不存在目录 / 未找到 xtquant SDK | 按诊断给出的路径提示修正（极速版 `userdata_mini` / 完整版 `userdata`） |
| session 类 | `connect rc=...` 且日志显示 session 冲突 | 换 `session_id` 重试；同一 session 两次 `connect` 间隔需 >3 秒 |

> ### ⚠️ QMT 客户端小版本升级后突然连不上，第一嫌疑就是这个严格校验开关
>
> 实测（广发 `gd_qmt`，2026-09-28）：客户端升级前授权串 `strict_connection_check=0`，
> 升级后变成 `1` 且 `no_pid_check=0` ⇒ 交易通道 `connect()` **恒返回 `-1`**，
> 而**行情（xtdata）完全不受影响**。
>
> 所以 **「行情正常能看、就是下不了单 / 连不上券商」是这个根因的典型症状**，
> 不要误判成网络、端口或资金账号未开通的问题。
>
> 定位方法（与平台内部所用同一套证据）：打开
> `<客户端数据目录>/log/XtMiniQmt_<YYYYMMDD>.log`
> （完整版是 `XtClient_<YYYYMMDD>.log`），搜 `not allowed`：
> ```
> quant session 5, pid 28844 not allowed, return
> [quant]XtQuantServer:: connect ret error-1
> ```
> 出现即命中。授权串本身写在日志**头部**的 `receive module auth string` 之后
> （键名 `mdl_auth_*`），平台侧 `read_client_auth_flags()` 可直接读出。

其余要点：

- **连不上已运行的 QMT 客户端**：除上表外，根因多为 `client_path` 填错或指向非 `userdata_mini`。
  `connect` 会先做一次 `probe_environment(light=True)` + `discover()` 预检，给出可操作提示。
- **大 / 小窗口客户端识别**：平台自动识别 58600（XtItClient 大窗口交易端口）与 58610（miniquote 小窗口行情端口），
  端口回退时连本机 miniquote 实际监听端口。**数据目录顺序按运行进程判定**
  （跑 `XtMiniQmt.exe` 就先试 `userdata_mini`），不会被静态布局结论覆盖。
- **多连接**：不同连接用不同数据库目录天然获得不同端口；`active` 连接是实时行情与交易的默认来源，
  切换即 `POST /api/v1/brokers/{conn_id}/active`。
- **零 mock**：未连接任何券商时 `active_bridge()` 返回 `None`，上层返回明确 503，**不会返回任何模拟数据**。

### 8.2 客户端调用时延预算（前后端必须同时给足，否则真实原因会被超时掩盖）

一次 `connect` 最坏要跑「桥接子进程冷启动 + 候选数据目录(≤2) × session(6) 逐个 `XtQuantTrader.connect()`」，
实测约 **45 s**（`XTPQuantAdapter._CONNECT_RETRY_BUDGET`）。两端预算必须配套：

| 环节 | 常量 | 值 | 说明 |
|---|---|---|---|
| 子进程内交易重试总预算 | `XTPQuantAdapter._CONNECT_RETRY_BUDGET` | **45 s** | 12 次 connect 的最坏耗时 |
| 父端等待子进程启动结果 | `bridge_client._READY_TIMEOUT` | **100 s** | 必须 > 上者 |
| 父端握手（**只管 IPC 就绪**） | `bridge_client._HANDSHAKE_TIMEOUT` | **30 s** | `_ping` 在启动期立即回 `starting:true`，启动结果由 `_wait_ready` 承载 |
| 前端 `connect` / `test` | `frontend-next/src/services/api/broker.ts` 显式 `timeout` | **120 s** | 不得落到默认 15 s |
| 前端 `auto-detect` | 同上（全盘扫描实测约 25 s，且 `retry: 0`） | **60 s** | 默认 15 s 会把「正在发现」误报成「发现失败」 |

> **绝不能让任一环使用 `http` 的默认超时（15 s）**：那样 `connect` 会在诊断信息产出**之前**
> 就被 abort，用户只看到「请求超时（15s）」，后端辛苦构造的根因永远到不了界面
> —— 这正是「前后端未贯通」最典型的形态。回归测试：`frontend-next/tests/brokerConnectBudget.test.ts`。

---

## 9. 测试与验证

- 契约测试：`backend/tests/test_contract.py`（XTPQuantAdapter 回调序列 / 状态词汇表 / 对账）、`backend/tests/test_contract_api.py`（响应包 / 注册表 / REST 契约）。
- 版本兼容：`backend/tests/test_version_compat.py`（多版本 xtquant 差异回归）。
- 端口选择：`backend/tests/test_port_pick.py`（端口锁陈旧复用加固回归）。
- 运行态验证：启动 EXE 后 `GET /api/v1/health` 应返回 `status: pass`、`GET /api/v1/brokers` 未连接时 `data: []`。
