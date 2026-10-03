# qmt_work 部署与安全审计报告 + 改进优化方案

> 审计基线：v0.4.3（2026-10-02 主线）· 审计日期 2026-10-03
> 审计范围：鉴权 / 加密 / 风控 / 信号路由 / 审计链 / CORS / 限流 / 构建打包 / 密钥管理 / 运行期治理
> 审计方法：源码逐文件实查（auth / crypto / masking / risk / signal_router / webhook_out / totp / idempotency / apikey / rate_limit / build_exe / release.yml / .env.example）+ TECH_DEBT.md 36 条交叉核对
> 证伪原则：每条问题必须可被一条 `grep` 或一条测试证伪（与 TECH_DEBT 收录规则一致）

---

## 一、安全基线现状（已做对的）

项目在安全上**已沉淀相当扎实的基础**，下面这些是已闭环的硬约束，不是缺口：

| # | 控制项 | 实现 | 证伪 |
|---|---|---|---|
| S1 | **零 mock 铁律** | 未连券商返回 503 + 引导；失败绝不包 `code=0`；拒单 ≠ 服务不可用（`broker_unavailable` 分流 503 vs 400） | `grep -rn "code.*0.*ok" backend/app/routes/` 应只在成功路径 |
| S2 | **主密钥不再打进安装包** | `crypto._key_file()` 跟随主库目录（`settings.db_path.parent`），不再落 `exe_dir()/data`；TD-26 已闭环 | `grep -n "master.key" backend/core/crypto.py` 路径口径唯一 |
| S3 | **密钥文件权限加固** | `_harden_key_file`：POSIX 0600；Windows `icacls /inheritance:r` 仅留当前用户 + SYSTEM | 启动日志无「权限加固失败」告警 |
| S4 | **字段级静态加密** | `crypto.encrypt_fields`：通知渠道 params 的 secret/password/url/token/auth 落库前 AES-256-GCM 加密，带 `enc:v1:` 前缀；历史明文兼容读取 | `grep -rn "enc:v1:" backend/` 命中加密写路径 |
| S5 | **日志脱敏过滤器** | `masking.SensitiveFilter` 挂 root logger；`api_key=` / `Bearer xxx` / `X-API-Key: xxx` 三类模式自动掩码；`mask_dict` 递归掩码敏感键 | `test_unit.py` 1018-1019 行断言 |
| S6 | **多密钥 + scope 分级** | `api_keys` 表（key_hash/scopes/rate_limit/status/ip_allow/expires_at/grace_until）；scope：market/trade/account/backtest/admin/*；**未匹配路径 default-deny（admin）** | `apikey.py::UNMATCHED_SCOPE = "admin"` |
| S7 | **常量时间比较防时序侧信道** | 主密钥校验 `hmac.compare_digest`；webhook 签名校验同款 | `auth.py:73` / `signal.py:117` |
| S8 | **loopback 免鉴权防绕过** | 携带 `X-Forwarded-For` / `Forwarded` 头一律不再信任 `client.host`，必须走完整鉴权（防反代伪造） | `auth.py::is_loopback` |
| S9 | **启动自检：默认密钥 + 远程监听 = 拒绝启动** | `run.py::_self_check`：`host` 非 loopback 且 `api_key == "qmt-dev-key"` ⇒ `sys.exit(1)` | `run.py:108` |
| S10 | **TOTP 二次确认（RFC 6238）** | 零依赖实现（hmac/hashlib/struct）；大额下单挂起令牌 + TOTP 校验；确认前**重跑风控**（防挂起期间参数变化） | `totp.py` + `signal_router.confirm` |
| S11 | **单飞幂等防并发重复下单** | `idempotency.single_flight`：同 key 并发只执行一次真实逻辑，其余复用 future；窗口缓存有硬上限 `_MAX_CACHE=4096` + 裁剪 | `idempotency.py` |
| S12 | **审计链式哈希** | `audit_chain_hash = sha256(prev_hash ‖ 字段值)`；任何历史记录被篡改/删除，其后所有 hash 无法自洽 | `db.py:138` + `/audit/verify` |
| S13 | **webhook 入站签名校验** | `QMT_WEBHOOK_SECRET` 非空时强制 HMAC-SHA256；未配置默认拒绝（`webhook_allow_insecure=false`），须显式开启才放行 | `signal.py:106-123` |
| S14 | **webhook 出站 HMAC 签名 + 指数退避重试** | `WebhookOut._sign`：`t=<ts>,v1=<hmac>`；fire-and-forget 不阻塞调用方 | `webhook_out.py` |
| S15 | **风控原子化占比判定** | 校验+计数+在途登记在同一把锁内完成；在途买入 TTL=120s，防并发多单各自按旧仓位判定双双通过 → 超仓 | `risk.py::check_order` |
| S16 | **市价单必须有保护价** | 市价单取不到最新行情 → 挂起人工确认（手动）/ 拒绝（自动化引擎）；绝不盲目放行 | `signal_router._route_inner` |
| S17 | **限价单必须 price > 0** | 防限价单以 0 元送出的灾难 | `risk._validate_order` |
| S18 | **CORS 默认不启用** | `QMT_CORS_ORIGINS` 空 = 仅同源；启用时置于最外层，OPTIONS 预检不被鉴权拦截 | `main.py:290-295` |
| S19 | **限流有 LRU 上限** | `RateLimiter` OrderedDict + `max_keys=10000`，防大量不同 key 内存无限增长 | `rate_limit.py` |
| S20 | **信号模式持久化 + 安全默认** | 启动从 `runtime_config` 读取；失败/无记录默认 `paper`（绝不静默回退 live） | `signal_router._load_persisted_mode` |

> **结论**：上述 20 项是项目的安全资产。改进方案**不在动这些已闭环的纪律**，而是补齐尚未覆盖的盲区与未闭环项。

---

## 二、潜在问题（按优先级）

### P0 · 安全红线（必须修）

#### P0-1 CORS `allow_credentials=True` 与 `allow_origins` 用户可配 = 凭证泄露面

**现状**（`main.py:293-295`）：
```python
app.add_middleware(CORSMiddleware, allow_origins=origins,
                   allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
```

**风险**：
- `allow_credentials=True` + 用户配 `QMT_CORS_ORIGINS=*`（或配错）= **任何站点可携带受害者浏览器凭证（Cookie/Authorization）发起跨域请求**；
- 浏览器默认不带 Cookie（本项目用 `X-API-Key` 头），但若用户在反向代理层把 token 落到 Cookie，凭证就会被跨站携带；
- `allow_methods=["*"]` 包含 `DELETE`/`PUT`，扩大了 CSRF 面。

**证伪**：`grep -n "allow_credentials" backend/app/main.py` 命中且为 `True`。

**建议**：
1. `allow_credentials=True` 时**禁止** `allow_origins` 含 `*`（FastAPI/Starlette 已会抛错，但应显式校验用户配置不含 `*`）；
2. 收紧 `allow_methods` 到实际使用的集合（`GET/POST/PUT/DELETE/PATCH`）；
3. 文档明确：`allow_credentials=True` 与 `QMT_CORS_ORIGINS` 的组合风险，建议生产关闭凭证。

---

#### P0-2 默认 API Key `qmt-dev-key` 仍可本机绕过启动自检

**现状**（`run.py:103-113`）：
```python
elif settings.api_key == "qmt-dev-key":
    log.warning("自检：API Key 仍为默认值 qmt-dev-key —— 生产环境必须修改！")
    if _is_remote_listen(settings.host):
        sys.exit(1)   # 远程 + 默认 = 拒绝
    log.warning("自检：当前仅本机访问，默认密钥风险可控——但生产环境仍务必修改")
```

**风险**：
- **本机 + 默认密钥 = 放行**。但桌面壳打包态的 `QMT_HOST` 默认 `0.0.0.0`（见 `.env.example`），用户若不改 host 也不改 key，**会直接被自检挡住**——这是对的；
- **但**：用户把 host 改回 `127.0.0.1` + 不改 key ⇒ 自检放行 ⇒ 本机任何进程（含恶意脚本）可用 `qmt-dev-key` 调用全部接口，包括下单；
- 桌面壳场景下，本机恶意进程是真实威胁面（不是理论攻击）。

**证伪**：`grep -n "qmt-dev-key" backend/run.py` 命中并触发 warning 而非 exit。

**建议**：
1. 启动自检增加：**检测到默认密钥时，写状态标记 `weak_api_key=true` 到 `runtime_config`**，前端「系统状态」页显式黄条告警「当前使用默认 API Key，存在安全风险」；
2. 默认密钥 + 任何非 loopback 绑定（含 `0.0.0.0`）维持 `sys.exit(1)` 不变；
3. 文档与 `.env.example` 强化「生产必须改」的提示，并在「连接管理」页首次启动时弹一次性引导。

---

#### P0-3 webhook 入站签名未做时间戳防重放

**现状**（`signal.py:114-118`）：
```python
provided = request.headers.get("x-signature", "")
expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
if not hmac.compare_digest(provided, expected):
    return err(401, "签名校验失败")
```

**风险**：
- 只校验 HMAC，**不校验时间戳** ⇒ 攻击者截获一条合法 webhook 请求后可**无限重放**；
- 虽然下游有 `idempotency_key` 幂等，但外部系统未必传 idempotency_key；
- 信号路由的 `_auto_idem_window` 只对引擎来源生效，`webhook` 来源**不自动生成幂等键**（`_ENGINE_SOURCES` 不含 webhook）。

**证伪**：`grep -n "timestamp\|replay\|nonce" backend/app/routes/signal.py` 应命中但当前为 0。

**建议**：
1. webhook 协议增加 `X-QmtWork-Timestamp` 头，签名内容改为 `f"{ts}.{body}"`（与出站 `WebhookOut._sign` 同款）；
2. 服务端校验 `ts` 与服务器时间偏差 ≤ 5 分钟，超期拒绝；
3. 可选：增加 `X-QmtWork-Nonce`，服务端缓存 5 分钟内已见 nonce，防同时间窗内重放。

---

#### P0-4 主密钥固定 nonce 前缀 `_NONCE` 削弱 GCM 安全性

**现状**（`crypto.py:51`）：
```python
_NONCE = b"qmt-agent-v1!!"  # 固定 12 字节前缀（每个密文再拼随机 nonce）
```

**实际语义**（读代码后澄清）：`encrypt_plain` 里 `nonce = os.urandom(12)` 是**每条密文随机生成**的，`_NONCE` 是作为 AES-GCM 的 **AAD（associated data）**传入，不是固定 nonce。**这是安全的设计**——AAD 固定、nonce 随机，GCM 安全性不受影响。

**但**：
- 注释写「固定 12 字节前缀（每个密文再拼随机 nonce）」**容易让审计者误读为 nonce 固定**；
- 若未来有人把 `_NONCE` 误用为 nonce（而非 AAD），会变成**同一密钥 + 固定 nonce** 的灾难性失败（GCM 在同一 key+nonce 下可恢复明文）。

**证伪**：`grep -n "_NONCE" backend/core/crypto.py` 看到它作为 `AESGCM.encrypt(nonce, ..., _NONCE)` 的第三参（AAD）。

**建议**：
1. 重命名 `_NONCE` → `_AAD` 或 `_ASSOCIATED_DATA`，消除「它是 nonce」的歧义；
2. 注释改为「AAD（associated data），固定值；nonce 每条密文随机生成，见 `os.urandom(12)`」。

---

### P1 · 部署与运行期风险

#### P1-1 数据库备份在主库较大时仍会放大启动成本

**现状**：TD-25 已修「备份不阻塞启动」，但**主库 585MB + keep=10 + 每次启动复制整库 ⇒ 11 分钟写 4.4GB** 的事实仍在；全量回归须 `QMT_DB_BACKUP_ENABLED=0`。

**风险**：
- 用户首次启动（冷库）不触发，但**长期运行后主库膨胀**会让每次启动都产生一次整库复制；
- SSD 寿命消耗 + 磁盘空间占用（keep=10 × 585MB = 5.85GB）；
- 备份目录无**单独的清理门禁**：`QMT_DB_BACKUP_MAX_TOTAL_MB=4096` 兜底，但 `QMT_DB_BACKUP_MIN_KEEP=2` 意味着 4096MB 超限时仍保留 2 份。

**建议**：
1. 备份改用**增量**方案：首次全量，后续只备份 WAL 增量段（SQLite 在线备份 API 的 `step` 已支持分页，但当前实现是整库 copy）；
2. 备份目录体积告警：超 `MAX_TOTAL_MB` 的 80% 时前端「系统状态」页显示黄条；
3. 提供「备份立即清理」的 UI 入口（当前只能手动删文件）。

---

#### P1-2 大 QMT 策略桥的"客户端侧不可控"无产品级降级

**现状**：大 QMT 能否跑只看**注册树**（QMT 客户端持久化，需 GUI 动作）；自动注册三重证据不可行（整文件锁/GBK 字节搜零命中/.rzrk 加密容器）。

**风险**：
- 用户装完包 → 面板显示 `available=false` → 不知道下一步做什么；
- `probe_stale` 诚实标注了"陈旧"，但**没有可操作的引导**（"打开 QMT → 新建策略 → 指向 bundle 路径"这三步只在文档里，界面无）；
- `qmt_diag_report.py` 脚本存在但**前端未接入**，用户不知道有这个工具。

**建议**：
1. 「连接管理」页在 `bigqmt.alive=false` 时展示 3 步折叠引导（附「打开策略目录」按钮）；
2. 「系统状态」页新增「导出诊断包」按钮 → 调 `qmt_diag_report.py` 逻辑，前端下载 zip（含 `qmt_work.log` + 客户端日志 + probe + capabilities 快照）。

---

#### P1-3 前端 SPA 旧 chunk 缓存无自动重试守卫

**现状**（README FAQ 明确登记）：SPA 重新构建部署后，旧 entry chunk 引用被清理 → `Failed to fetch dynamically imported module`，需手动 `Ctrl+Shift+R`。

**风险**：用户更新后第一次打开白屏，且无任何提示，直接误判为"坏了"。

**建议**：`main.tsx` 加顶层 `ErrorBoundary`，捕获动态导入失败 → 提示用户 → 自动 `location.reload()`（带 60s 冷却防死循环）。

---

#### P1-4 `eltdx` Research-Only 许可在界面上无告警

**现状**：`eltdx` 在 `requirements-optional.txt`，`commercial_mode=true` 时数据源链自动跳过；但**用户界面没有明确告警**"当前数据源含商用受限"。

**风险**：合规上属静默风险——用户在商用环境用了 eltdx 数据却不知情。

**建议**：
1. 系统设置 → 数据源链显示当前激活源及许可标注；
2. `commercial_mode=true` 时前端弹黄条「当前数据源链含 Research-Only 许可组件，禁商用」；
3. 启动日志打印当前激活源与许可状态。

---

#### P1-5 限流对 loopback 完全豁免

**现状**（`rate_limit.py:57-58`）：
```python
if host in {"127.0.0.1", "::1", "localhost"}:
    return await call_next(request)
```

**风险**：
- 桌面壳场景下本机进程不限流是合理的（同机信任）；
- **但**：若用户把后端暴露到局域网（`QMT_HOST=0.0.0.0`）并在前面加反代，反代把 `X-Forwarded-For` 透传过来，`auth.is_loopback` 会因转发头存在而**不再信任** `client.host`——这是对的；
- **但限流中间件没有同款防绕过逻辑**：`rate_limit_middleware` 只看 `request.client.host`，**不检查转发头**。若用户在反代层把 `X-Forwarded-For: 127.0.0.1` 透传，限流会误判为 loopback 而豁免。

**证伪**：`grep -n "x-forwarded-for\|forwarded" backend/gateway/rate_limit.py` 应命中但当前为 0（对比 `auth.py:36` 有同款检查）。

**建议**：`rate_limit_middleware` 复用 `auth.is_loopback` 的转发头检查逻辑，保持两处口径一致。

---

### P2 · 战略与体验项

#### P2-1 契约门禁盲区：响应载荷字段级漂移

**现状**：`check_api_contract_drift.py` 只核**路径 + 查询参数**（TD-31 已诚实登记）；响应体是运行期构造 dict，静态推导成本大。

**风险**：后端上浮的诊断字段前端零消费（TD-31 实例：4 个字段前端 grep 命中 0）。

**建议**：为 3~5 个高风险端点（`connector_probe` / `account/aggregate` / `market/quotes`）硬编码期望字段集合；缺失即红灯。

---

#### P2-2 `_broker_batch` 仍是 N 次 RPC

**现状**（TD-01）：`datasource/manager_quotes.py:101` 明写"N 只标的 = N 次跨进程 RPC"。

**建议**：见上一轮方案 A7——在无真券商约束下先用 `get_full_tick` 语义封装为 `BrokerAdapter.batch_get_quote`（默认回退逐只），契约测试用计数断言。

---

#### P2-3 插件内核声明式，执行委托未落地

**现状**（TD-P2-04）：`plugins/kernel.py` 仅 61 行，只有 `install/activate/deactivate/remove`；`activate` 只改 `state` 字符串，**无任何执行边界**。

**风险**：插件系统承诺了一个不存在的排障能力。

**建议**：最小可用——subprocess + JSON-RPC 边界；`PluginManifest.permissions` 补「插件能做什么」的白名单。

---

## 三、详细改进优化方案

### 阶段 A：安全止血（1 周，最高优先级）

| # | 动作 | 交付物 | 判据 |
|---|---|---|---|
| A1 | **CORS 凭证收紧** | `main.py` 校验 `allow_credentials=True` 时 `allow_origins` 不含 `*`；`allow_methods` 收紧到 `GET/POST/PUT/DELETE/PATCH` | 配 `*` 启动报错；配合法 origin 通过 |
| A2 | **默认密钥告警上浮到 UI** | 启动写 `weak_api_key=true` 到 `runtime_config`；前端「系统状态」页黄条；「连接管理」页首次启动弹一次性引导 | 前端可见黄条；改密钥后黄条消失 |
| A3 | **webhook 入站防重放** | 协议增加 `X-QmtWork-Timestamp` 头，签名内容改 `f"{ts}.{body}"`；服务端校验时间偏差 ≤ 5 分钟 | 重放 5 分钟前的请求返回 401 |
| A4 | **`_NONCE` 重命名消除歧义** | `crypto.py` 重命名 `_NONCE` → `_AAD`；注释改「AAD，固定值；nonce 每条随机生成」 | `grep "_NONCE" backend/core/crypto.py` 命中为 0 |
| A5 | **限流转发头防绕过** | `rate_limit_middleware` 复用 `auth.is_loopback` 的转发头检查逻辑 | 透传 `X-Forwarded-For: 127.0.0.1` 不再豁免限流 |
| A6 | **可证伪守卫** | 新增 `verify_cors_safety.py`（构造 `*` + credentials 必须报红）+ `verify_webhook_replay.py`（重放必须报红） | 守卫 2/2 + 终态对照 |

### 阶段 B：部署体验（2 周）

| # | 动作 | 交付物 | 判据 |
|---|---|---|---|
| B1 | **一键诊断包导出（UI）** | 「系统状态」页新增按钮 → 调 `qmt_diag_report.py` 逻辑 → 前端下载 zip（含 5+ 诊断文件） | 前端可下载 zip |
| B2 | **大 QMT 注册树操作可视化引导** | 「连接管理」页在 `bigqmt.alive=false` 时展示 3 步折叠引导 + 「打开策略目录」按钮 | 面板可用点击完成注册三步 |
| B3 | **SPA 旧 chunk 自动重载守卫** | `main.tsx` 顶层 `ErrorBoundary`，捕获动态导入失败 → 提示 → 自动 reload（60s 冷却） | 手动强删一个 chunk 分片后打开 SPA 应自动恢复 |
| B4 | **eltdx 商用模式提示** | 系统设置 → 数据源链显示激活源及许可标注；`commercial_mode=true` 时前端弹黄条 | 切换商用模式后界面出现告警条 |
| B5 | **备份目录体积告警** | 超 `MAX_TOTAL_MB` 的 80% 时前端「系统状态」页显示黄条；提供「备份立即清理」UI 入口 | 体积达 80% 阈值时黄条可见 |
| B6 | **响应载荷字段级对账 MVP** | `check_api_contract_drift.py` 增加 3~5 个高风险端点期望字段集合；缺失即红灯 | 人为删一个前端字段消费，门禁报红 |

### 阶段 C：能力补齐（4 周）

| # | 动作 | 交付物 |
|---|---|---|
| C1 | **`_broker_batch` 批量化**（TD-01） | `BrokerAdapter.batch_get_quote` 默认回退逐只；桥/mini/大 QMT 三家实现批量；计数断言 |
| C2 | **同花顺量化真 SDK 接入**（TD-P2-02） | 新 dialect + profile；复用 `Dialect × Transport` 正交抽象 |
| C3 | **MCP 写能力两阶段**（TD-P2-03） | `submit_order` 类工具走「拟稿 → confirm → 落地」两阶段 |
| C4 | **插件内核执行委托**（TD-P2-04） | subprocess + JSON-RPC 边界；权限白名单 |
| C5 | **文档站点化**（TD-P2-05） | `docs/` → VitePress；TECH_DEBT 独立成"已知限制"章节 |

---

## 四、执行优先级建议

**本轮最高 ROI**：A1（CORS 收紧）+ A3（webhook 防重放）+ A5（限流转发头防绕过）——这三条是**安全红线的直接补齐**，改动量小、判据清晰、可证伪守卫易写。

**次优先**：A2（默认密钥告警上浮 UI）+ B1（诊断包导出）+ B3（SPA 重载守卫）——这三条是**部署体验的痛点**，用户可直接感知。

**战略级**：C2（同花顺真接入）——把"10 个档案但只有 7 家真调"这个产品宣传上的硬伤消掉。

---

## 五、一句话总结

**qmt_work 的安全基线远超一般量化平台**：20 条已闭环的硬约束（零 mock / 密钥不进包 / 字段级加密 / 日志脱敏 / 审计链哈希 / 单飞幂等 / 风控原子化 / TOTP / webhook 签名 / scope 分级 / 启动自检）已构成扎实的纵深防御。**真正的缺口不在"已做的"，在"尚未覆盖的盲区"**：CORS 凭证组合、webhook 防重放、限流转发头防绕过、默认密钥 UI 告警——这四条是本轮止血点，改动量小但风险消除显著。

---

## 六、Phase 1 实施记录（2026-10-03）

### 已闭环项

| 修复 | 对应问题 | 实现文件 | 测试覆盖 |
|------|----------|----------|----------|
| **三档远程访问模型** | 用户无法产品化启用远程访问 | `core/config.py`（Settings + helper）+ `run.py`（_self_check） | `test_remote_access_tiers.py` 16 用例 |
| **远程访问管理 API** | 无 UI/API 管理密钥/档位/TOTP | `app/routes/remote_access.py`（6 端点） | `test_remote_access_routes.py` 16 用例 |
| **限流 x-forwarded-for 防绕过** | P0-5 限流中间件仅看 client.host | `gateway/rate_limit.py`（改用 `auth.is_loopback`） | `test_rate_limit.py` 3 新增用例 |
| **CORS 通配符降级** | P0-1 allow_credentials + * 组合 | `app/main.py`（* 自动降级为仅同源 + 警告） | 手动验证 |
| **wan 档强制 TOTP** | 远程实盘无二次确认 | `run.py::_self_check` + API 前置校验 | `test_remote_access_tiers.py` + `test_remote_access_routes.py` |
| **wan 档 signal.mode 默认 paper** | 远程实盘风险过高 | `remote_access.py`（切档时自动 set_mode paper） | `test_remote_access_routes.py` |
| **启动自检分档差异化** | 单一自检逻辑无法覆盖三档 | `run.py::_self_check`（off/lan/wan 三层分支） | `test_remote_access_tiers.py` 6 自检用例 |

### 测试统计

```
test_remote_access_tiers.py  : 16 passed（配置归一化 + 辅助函数 + 自检分档）
test_remote_access_routes.py : 16 passed（6 端点 + 前置校验 + 配置写入）
test_rate_limit.py           :  7 passed（4 原有 + 3 新增 x-forwarded-for 防绕过）
                              ─────────
                              39 passed, 0 failed
```

### 遗留项（Phase 2+）

- **P0-3 webhook 防重放**：需新增 X-QmtWork-Timestamp 头 + 时间戳校验
- **P0-4 主密钥 nonce 前缀**：需评估是否改为全随机 nonce（兼容性风险）
- **P0-2 默认密钥 UI 告警**：前端需在系统状态页展示 weak_api_key 标记
- **P1-3 SPA chunk 404 白屏**：前端路由守卫
- **P1-4 eltdx 许可证合规**：商用需评估替换方案

---

> 本文档与 `docs/TECH_DEBT.md` 互补：TECH_DEBT 记录"已入板可证伪"的技术债，本文档记录"安全与部署维度的审计结论与改进方案"。每条改进项落地后，应同步在 TECH_DEBT 新增条目（可证伪、可追溯）。
