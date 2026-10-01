# atst 全面升级优化方案（V6 综合版）

> **本文件取代 V4（atst最终最优化方案.md）与 V5（atst四方对标与全量覆盖方案.md），是唯一执行依据。**
> V4 = 协议生存与可用性透明 ｜ V5 = 四方对标口径校准 + 缺口清单
> V6 = 两者合并 + 双线扩张（新源扩展 × 现有源端点深挖）+ 全量覆盖路线
> 整合日期：2026-10-01
> 基准代码：`coeasy/atst` v1.0.1 @ `21df21b`

---

## 0. 执行摘要

### 0.1 三句话结论

1. **行情核心面 atst 已与四方持平或更深（172 capability vs 87/45/5）；真正的差距在"资讯/信号/事件/宏观/指数"5 个非行情层——atst 的 Provider 全集里没有巨潮、同花顺、交易所官方、中债/统计局/见闻这几家源。**
2. **最高杠杆的一件事不是加新源，而是放开 `Symbol` 层对 HK/US 的 fail-closed 拦截——腾讯 `hk`/`us` 与新浪 `hk` 适配器已写完，2~3 天激活 4 个已有适配器。**
3. **双线扩张策略**：现有 6 源深挖端点（18 适配器 → 75 端点）× 新增 5 源（巨潮/交易所/同花顺/中债/见闻），合计目标 Web Provider 11→**16**、端点 ≈50→**≈140**、capability 172→**≈340**。

### 0.2 关键数字（实测，非文档自述）

| 指标 | V4 文档说法 | V5 实测 | V6 目标 |
|---|---|---|---|
| Web Provider | 7 | **11** | **16** |
| Web 端点 | ≈30 | ≈50 | **≈140** |
| 注册 capability | 172 | 172 | **≈340** |
| 每能力平均源数 | ≈1.2 | ≈1.2 | **≥2（跨风控域）** |
| 死名字 | 8 | 8 | **0** |
| 新增 Provider | 11（V4 列） | 已有 11 | +5 = 16 |

### 0.3 四方对标判定

| 项目 | 判定 | 主缺口 |
|---|---|---|
| **stock-api** | ✅ 全覆盖 | 仅策略差异（多源兜底 vs 显式禁止）+ 代码格式 |
| **adata** | 🟡 90% | 概念反查/申万行业/ETF份额/交易日历源 |
| **a-stock-data** | 🟠 ~55% | 7 大类非行情层 |
| **go-stock** | ⚪ 定位差异 | L6 排除（应用层） |

### 0.4 如果资源有限，只做这五件事（≈3 周）

| 序 | 事项 | 工作量 | 收益 |
|---|---|---|---|
| ① | **Symbol 层解冻 HK/US/BJ + 4 前缀适配器激活** | 3 天 | 救活港美股/北交所，消除最大"宣称 vs 现实"落差 |
| ② | **capabilities 带状态 + 死名字下线四面出口** | 1 周 | 消除"172 名字里 8 个死的"误导 |
| ③ | **复权三源对拍**（东财 + 新浪因子 + 巨潮因子） | 2 周 | 消除复权单点，隐性 bug 高发区 |
| ④ | **东财估值字段进快照**（显式 enrich） | 2 天 | PE/PB/市值/换手率，快照体验拉平 |
| ⑤ | **字段 spec 单一事实源 + golden 断言** | 2 周 | 9 类字段陷阱钉死，端点 4 倍扩张的基础设施 |

---

## 1. 现状诊断（V5 实测口径，V4 三处过时数字已更正）

### 1.1 已经具备、不要再重复投入的

| 域 | 能力 | 实测状态 | 证据 |
|---|---|---|---|
| 协议 | TDX 5 协议族、85 命令 / 61 解析器、vipdoc 离线 | ✅ | 四家中唯一 |
| K 线 | 日/周/月 + 分钟；五面贯通 | ✅ | |
| 复权 | `adjusted_bars` + `AdjustEngine` | ✅ | **但事件源单点（见 P3）** |
| 接入 | CLI 31 子命令 / HTTP 43 端点 / WS 13-19 方法 / MCP 9-13 工具 | ✅ | |
| 基本面 | 三表/财报/分红/股东/高管/评级/概况/公告 | ✅ | |
| 基金 | 净值/估值/列表/排行/经理/公司/持仓/行业 | ✅ | |
| 期货债券 | efinance_deriv + bond | ✅ | |
| 东财 datacenter | 22 个 RPT 报表已接 | ✅ | `RPT_LIFT_STAGE`/`RPTA_WEB_RZRQ`/`RPT_SHAREBONUS`/`RPT_DATA_BLOCKTRADE`/`RPT_HOLDERNUM`/`RPT_PUBLIC_OP_NEWPREDICT`/`RPT_ECONOMY_CPI`/`RPT_ECONOMY_GDP`/`RPT_ECONOMY_PPI`/`RPT_BOND_CB_LIST`/`RPT_F10_BASIC_ORGINFO`/`RPT_F10_EH_HOLDERS`/`RPT_HOLDERNUMLATEST`/`RPT_INDUSTRY_INDEX`/`RPT_CONCEPT_INDEX`/`RPT_LICO_FN_CPD`/`RPT_MUTUAL_HOLD`/`RPT_EXECUTIVE_HOLD_DETAILS`/`RPT_SHARE_HOLDER_INCREASE`/`RPT_WEB_RESPREDICT`/`RPTA_APP_IPOAPPLY`/`RPT_LIFT_STOCK` |
| push2delay | 东财容灾出口 | ✅ **已完成**（V4 列为"必做"可划掉） | 8 处引用 |
| 百度 MA5/10/20 | K 线均价 | ✅ | `baidu/adapters.py:217-219` |
| 期货持仓排名 | TDX `0x0207` | 🟡 有协议路径，非交易所官方 | |

### 1.2 四个真实问题（V5 校准后）

| # | 问题 | 证据 |
|---|---|---|
| **P1** | **HK/US 从统一入口不可达** | `symbol.py:123` `tdx_market` 对 HK/US `raise SymbolError`；但 `tencent/adapters.py` 已有 `TencentHkSource`/`TencentUsSource`、`sina/adapters.py` 已有 `SinaHkSource` |
| **P2** | **8 个死名字仍以"能力"身份存在** | CHANGELOG 自曝：`auction`/`volume_price`/`block_quotes`/`minute_history`/`security_list` 等；其中 6 个无 Web 出路 |
| **P3** | **复权事件源单点** | `AdjustEngine` 只吃东财 `RPT_SHAREBONUS`；新浪 `qianfuquan`/`houfuquan` 0 引用；巨潮因子 0 引用 |
| **P4** | **5 个非行情层无 Provider** | cninfo 巨潮 0、10jqka 同花顺 0（hexin 3 处全是 iwencai cookie）、sse/szse/bse 交易所官方 0、中债/货币网 0、华尔街见闻 0 |

### 1.3 现有 6 源端点深挖现状（V5 实测）

| Provider | 已接端点族 | 已接端点数 | 可深挖端点数 | 深挖后总数 |
|---|---|---|---|---|
| **腾讯** | `qt.gtimg.cn`(A股+hk+us+hf_+s_) + `ifzq`(kline+fqkline) + `web.ifzq`(minute) + `stock.gtimg`(逐笔) + `proxy.finance`(getBoardRankList+mktHs) | ~10 | `ff_` 资金流 / `s_pk` 盘口 / `web.sqt` 五档市值 / `proxy.finance getRank`(hy2+gn2) / `data.gtimg flashdata` 归档 / `stock.finance dadan` 大单 / `web.ifzq day/query` 五日分时 | **+6 = 16** |
| **新浪** | `hq.sinajs`(A股+hk) + `quotes.sina.cn`(K线) + `suggest3` + `vip.stock`(boards+news+fundflow) | ~8 | `bj` 北交所 / `gb_` 美股盘前盘后 / `qianfuquan`/`houfuquan` 复权因子 / `market.finance pricehis` 分价表 / `market.finance downxls` 历史明细 / `vip.stock getAllListPage` 成交明细 / `vip.stock getPriceCounts` 分价表V2 / `finance.sina realstock` 研报列表 / `global.finance EsgService` 已接 | **+7 = 15** |
| **东财** | 15 个 host（push2/push2his/push2delay/92.push2/92.push2his/push2ex/datacenter-web/datacenter/reportapi/np-anotice/newsapi/emappdata/fundmobapi/fundf10/fundts） | ~30 | `stock/get` 估值字段 / `getTopicZTPool` 涨停池+原因 / `RPT_BILLBOARD_DAILYDETAILS` 席位 / `pdf.dfcfw` 研报PDF / `qType=1` 行业研报 | **+4 = 34** |
| **百度** | `finance.pae.baidu.com/selfselect/getstockquotation` | 1 | `getrelatedblock` 概念归属(申万+概念+地域) / 分时 / 逐笔 / 五档 | **+4 = 5** |
| **集思录** | `jisilu.cn/data/cbnew/cb_list/` | 1 | `premium_rt`/`ytm_rt`/`convert_value`/`cb_balance`/`rating_cd`/`force_redeem`/`year_left` / ETF/QDII/封基/分级/债券/指数估值 | **+2 = 3** |
| **中行** | 外汇牌价 | 1 | 历史汇率 / 远期挂牌利率 | **+2 = 3** |
| **合计** | — | **≈51** | **+25** | **≈76** |

### 1.4 新增 5 源端点清单

| 新 Provider | 核心价值 | 端点数 | capability 增量 |
|---|---|---|---|
| **巨潮 cninfo** | 全量公告 + 两融 + 大宗 + 停复牌 + **复权因子** + 互动易 | 8 | +12 |
| **交易所官方 exchange** | 上交所两融/龙虎榜/大宗 + 深交所 ShowReport 全套 + 交易日历 + ETF 份额 | 7 | +10 |
| **同花顺 ths** | `getharden` 题材归因(独家) + 北向资金 + 一致预期EPS + 概念成分 + 热榜 | 6 | +9 |
| **中债/货币网 chinabond** | 国债/信用债收益率曲线 + LPR + Shibor + 回购定盘 | 4 | +6 |
| **华尔街见闻 wallstreet** | 7×24 快讯 + 全球宏观日历 + 央视网文字稿 | 3 | +4 |
| **合计** | — | **28** | **+41** |

---

## 2. 目标态

```
┌─ L5 接入层 ── CLI / HTTP / WS / MCP / TS-SDK / doctor 全绿，死名字不出现
├─ L4 架构层 ── 能力路由表 + 字段 spec + 单位归一化 + 健康度 + 跨源对拍
├─ L3 新增层 ── 5 个新源（巨潮/交易所/同花顺/中债/见闻）= 28 端点 / +41 capability
├─ L2 深挖层 ── 现有 6 源深挖 25 端点（腾讯6+新浪7+东财4+百度4+集思录2+中行2）
├─ L1 透明层 ── capabilities 带状态；死名字下线四面出口
└─ L0 生存层 ── Symbol 层解冻 HK/US/BJ；8 死名字四选一处置；复权三源
```

**双线扩张原则**：
- **纵线（深挖）**：现有 6 源 51 端点 → +25 = 76 端点（低风险、成本低）
- **横线（新源）**：+5 Provider 28 端点 = 104 端点（高风险、需评估风控）
- 合计：**16 Provider × ≈140 端点 × ≈340 capability**

---

## 3. 分层任务清单

### L0 · 生存层（W1~W2，不做则后面全无意义）

| 编号 | 任务 | 做法 | 验收 | 投入 |
|---|---|---|---|---|
| **S1** ⭐⭐⭐ | **Symbol 层解冻 HK/US/BJ** | `domain/symbol.py:123` 的 `tdx_market` 放开 HK/US；新增 BJ 市场映射；**前置：字段 spec + golden 样本**（腾讯 hk/us 与 A 股字段布局不同，解析器不能按 A 股 88 字段切） | `Client.call("quotes", symbols=["hk00700","usaapl","bj832000"])` 走统一入口返回结构化数据 | 3 天 |
| **S2** | **8 死名字四选一处置** | 每个死名字选：①修参数复活 ②换命令替代 ③转 Web 出路 ④**下线** | 8 个 100% 归位，残留 0 | 1 周 |
| **S3** | **新浪复权因子第二源** | `finance.sina.com.cn/realstock/company/{code}/qianfuquan.js?d=` + `houfuquan.js`；0→1 | `AdjustEngine` 能吃新浪因子 | 2 天 |
| **S4** | **市场词汇收口** | `Market` ↔ `cn_a/cn_bse/hk/us/future/commodity/option/bond/fx` 单一事实源映射 | HK/US 返回结构化数据而非裸 `SymbolError` | 2 天 |
| **S5** | **stock-api 代码格式别名** | Symbol 归一化层加 `SH510500`/`SZ000651`/`HK00700`/`USAAPL` 大小写不敏感输入 | `SH510500` 输入可用 | 0.5 天 |
| **S6** | **2026-09 协议握手验证矩阵** | 5 协议族 × 主站池逐台跑「版本标识握手 + tdxlevel 认证 + zlib 流式解码」三格；入 CI 常驻 | 5 族各 ≥1 台主站握手成功 | 1 周 |

### L1 · 透明层（W2~W3，投入最小收益最大）

| 编号 | 任务 | 做法 | 验收 |
|---|---|---|---|
| **T1** ⭐ | **capabilities 带状态** | 三处出口（`Client.capabilities()` / `GET /v13/capabilities` / `WS runtime.capabilities`）同增 `status`(available/offline/degraded/unverified) + `last_verified` + `fallback_to` | 172 项全部有状态，无 `unknown`；S1 完成后 hk/us/bj 标 `available` |
| **T2** | **死名字下线四面出口** | 门禁：offline capability 不得注册为 CLI 子命令 / HTTP 路由 / MCP 工具 / WS 方法 | 新增架构测试，出现即红 |
| **T3** | **health 明细化** | `runtime.health` 暴露 `core_unavailable` 明细 + 每族主站存活数 + 最近握手时间 | 一眼看出哪一面挂了 |
| **T4** | **错误语义统一** | `CommandOffline E3035` / `AllHostsUnreachable E2040` → 统一 `SourceUnavailable` + 建议替代路径字段 | 调用方无需区分底层异常 |

### L2 · 深挖层（现有 6 源：51 端点 → +25 = 76 端点）

#### D组 · 腾讯财经（深挖 6 端点）

| 编号 | 端点 | 要点 | 现状 | 投入 |
|---|---|---|---|---|
| **D1** ⭐ | ~~`qt.gtimg.cn/q=hk\|us\|hf_\|fx_`~~ | **适配器已就绪**，只需 S1 放开 Symbol 层 | ✅ 适配器已写，卡在 Symbol 层 | 含在 S1 |
| **D2** | `qt.gtimg.cn/q=ff_` | 资金流（主力/散户流入流出净额）— 东财跨域备胎 | ❌ | 1 天 |
| **D3** | `qt.gtimg.cn/q=s_pk` | 盘口分析 | ❌ | 1 天 |
| **D4** | `web.sqt.gtimg.cn/q=` | 含总成交量/外盘内盘/五档/市值 | ❌ | 1 天 |
| **D5** ⭐ | `proxy.finance.qq.com/.../rank/pt/getRank` | **板块排行：`hy2` 124 行业 / `gn` 803 概念** | ❌ 高价值 | 2 天 |
| **D6** | `data.gtimg.cn/flashdata/hushen/{minute,daily,weekly,monthly,4day}` | 历史归档（golden 校验用） | ❌ | 1 天 |

#### D组 · 新浪财经（深挖 7 端点）

| 编号 | 端点 | 要点 | 现状 | 投入 |
|---|---|---|---|---|
| **D7** ⭐ | `hq.sinajs.cn/list=bj` | **北交所** | ❌ | 1 天 |
| **D8** ⭐ | `hq.sinajs.cn/list=gb_` | **美股盘前盘后（36 字段）** | ❌ | 1 天 |
| **D9** | ~~`qianfuquan.js`/`houfuquan.js`~~ | **复权因子第二源** | ❌（含在 S3） | 含在 S3 |
| **D10** | `market.finance.sina.com.cn/pricehis.php` | **分价表** → 替代已死 0x051A 量价分布 | ❌ | 2 天 |
| **D11** | `market.finance.sina.com.cn/downxls.php` | 历史成交明细 XLS → 补历史分时+逐笔 | ❌ | 2 天 |
| **D12** | `vip.stock.../CN_Transaction.getAllListPage` | 最新成交明细 | ❌ | 1 天 |
| **D13** | `finance.sina.com.cn/realstock/company/{code}/qianfuquan.js` | ~~复权因子~~（含在 D9） | — | — |
| **D14** | `vip.stock.../research/list` | 研报列表（第二来源，无评级与目标价） | ❌ | 1 天 |

#### D组 · 东方财富（深挖 4 端点）

| 编号 | 端点 | 要点 | 现状 | 投入 |
|---|---|---|---|---|
| **D15** ⭐ | `push2.../api/qt/stock/get` 补字段 | `f162` PE / `f167` PB / `f116` 总市值 / `f117` 流通市值 / `f168` 换手 / `f51` 涨停价 / `f52` 跌停价；显式 `enrich=["valuation"]` 隔离，逐字段 `_provenance` | ❌ | 2 天 |
| **D16** ⭐ | `push2ex.../getTopicZTPool` / `ZBPool` / `DTPool` | **涨停池+涨停原因题材**（ZBPool/DTPool 已有，缺 getTopicZTPool 原因字段） | 🟡 部分 | 2 天 |
| **D17** | `datacenter-web.../RPT_BILLBOARD_DAILYDETAILSBUY/SELL` | **龙虎榜买卖席位 TOP5**（`longhu.py:38` 注释"已失效"，需重抓包） | ❌ | 2 天 |
| **D18** | `pdf.dfcfw.com/pdf/H3_{infoCode}_1.pdf` | **研报 PDF 下载**（需 Referer `reportapi.eastmoney.com`） | ❌ | 1 天 |

#### D组 · 百度 PAE（深挖 4 端点）

| 编号 | 端点 | 要点 | 现状 | 投入 |
|---|---|---|---|---|
| **D19** ⭐ | `finance.pae.baidu.com/selfselect/getrelatedblock` | **概念板块归属**（申万一级/二级 + 概念 + 地域三维） | ❌ 成本最低 | 1 天 |
| **D20** | 分时补齐 | 已有 quote/kline/minute/ticks | ⚠️ | 1 天 |
| **D21** | 逐笔补齐 | | ⚠️ | 1 天 |
| **D22** | 五档补齐 | | ⚠️ | 1 天 |

#### D组 · 集思录（深挖 2 端点）

| 编号 | 端点 | 要点 | 现状 | 投入 |
|---|---|---|---|---|
| **D23** ⭐ | `jisilu.cn/data/cbnew/cb_list/` 补字段 | `premium_rt` 溢价率 / `ytm_rt` 到期收益率 / `convert_value` 转股价值 / `cb_balance` 余额 / `rating_cd` 评级 / `force_redeem` 强赎 / `year_left` 剩余年限 | ⚠️ 浅 | 2 天 |
| **D24** | `jisilu.cn/data/...` ETF/QDII/封基/分级/债券/**指数估值分位** | | ❌ | 2 天 |

#### D组 · 中行（深挖 2 端点）

| 编号 | 端点 | 要点 | 现状 | 投入 |
|---|---|---|---|---|
| **D25** | 历史汇率 | 历史外汇牌价 | ❌ | 1 天 |
| **D26** | 远期挂牌利率 | 远期外汇 | ❌ | 1 天 |

### L3 · 新增层（5 个新源 = 28 端点 / +41 capability）

| 编号 | 新 Provider | 端点 | 核心价值 | 投入 | 顺序 |
|---|---|---|---|---|---|
| **N1** ⭐⭐⭐⭐⭐ | **巨潮 cninfo** `webapi.cninfo.com.cn` | 8 端点：全量公告 `p_info3015` + 两融 `p_rzrq3104` + 大宗 + 停复牌 `p_stock2202` + **复权因子**(第三源) + 互动易 + 北交所公告 `p_info3014` + 港交所公告 `p_info3024` | **复权第三源** + 跨风控域备胎 | 1 周 | **1** |
| **N2** ⭐⭐⭐⭐⭐ | **交易所官方 exchange** | 7 端点：上交所两融明细/汇总 + 大宗 + 龙虎榜 + 深交所 `ShowReport/data`(CATALOGID=1837 两融/1834 标的证券) + 交易日历 + ETF 份额 | 跨域备胎 + 官方权威 | 1 周 | **2** |
| **N3** ⭐⭐⭐⭐ | **同花顺 ths** `10jqka.com.cn` | 6 端点：`getharden` 题材归因(独家) + `hsgtApi` 北向资金(分钟/历史) + `basic.10jqka` 一致预期EPS + 概念成分 + 涨停池 + 热榜 | **题材归因独家** + 北向 | 1.5 周 | **3** |
| **N4** ⭐⭐⭐ | **中债/货币网 chinabond** | 4 端点：国债收益率曲线 + 信用债收益率 + LPR + Shibor/回购定盘 | 宏观利率层 0→1 | 1 周 | **4** |
| **N5** ⭐⭐ | **华尔街见闻 wallstreet** | 3 端点：7×24 快讯 + 全球宏观日历 + 央视网新闻联播文字稿 | 新闻补强 | 0.5 周 | **5** |

> **N3 同花顺风险**：Cookie + 限流 + 字段级 null。`zhangfu/huanshou/close` 等字段 **null 保留 null，绝不填 0**。接入前先跑 30 天稳定性观察。

### L4 · 架构层（贯穿全程，支撑 3 倍端点扩张）

| 编号 | 任务 | 说明 |
|---|---|---|
| **A1** ⭐ | **字段 spec 单一事实源** | 每端点一份 YAML：url / encoding / required_headers / periods / max_bars / fields(idx,name,unit) / derived。**解析器由 spec 生成**，新增端点 = 加 spec，不改代码 |
| **A2** ⭐ | **golden 断言覆盖 9 类陷阱** | 每类陷阱 ≥1 条断言（见附录） |
| **A3** ⭐ | **能力路由表** | `capability → [(provider, endpoint, priority, risk_domain)]`；**每能力必须含 ≥2 个不同 risk_domain** |
| **A4** | **风控域标签** | `eastmoney`(22+端点同域，被封全灭) / `tencent` / `sina` / `exchange` / `ths` / `cninfo` / `jsl` / `official` / `media` / `chinabond`；FallbackPolicy **优先跨域降级** |
| **A5** | **单位与编码归一化** | 量(手/股/份)、额(元/万元/亿)、市值统一；GBK/GB18030/UTF-8 在 transport 层统一，不泄漏到解析器 |
| **A6** | **分层限流 + 健康度** | per-endpoint 令牌桶 + per-domain 并发闸门 + 全局闸门；腾讯 ≥100ms 间隔；健康度看板 |
| **A7** | **跨源对拍（只告警不覆盖）** | 实时价(腾讯 vs 新浪 vs 东财, >0.5% 告警) / 日线 OHLC(>0.1%) / **复权因子三源(>0.5%)** / 成交额(>1%) |
| **A8** | 死能力治理门禁 | offline capability 禁止注册到四面出口（与 T2 同源） |

### L5 · 接入层（W6~W9）

| 编号 | 任务 |
|---|---|
| **I1** | MCP 工具集扩至高频 15 个；**死名字不为工具** |
| **I2** | TS / 浏览器 SDK：不重写协议，走现有 HTTP 契约 + OpenAPI 生成 |
| **I3** | `atst doctor` 一键体检：主站握手 + 五族可用性 + 四面出口 + 死名字残留 + 全端点健康度 |
| **I4** | 破坏性变更治理：F-47 收紧后发 minor 版本 + 迁移说明；`/v13` 路由版本策略明确 |

### L6 · 明确不做（scope 边界，保持项目纪律）

AI 分析、智能选股、回测引擎、涨跌报警推送、桌面 UI、K 线绘图与指标、本地全量数据库。
> 这些是 go-stock 的应用层能力。atst 是基础设施——CHANGELOG 显示项目已因 F-49 主动清掉过一个"同一件事的第二实现"，此纪律必须保持。
> **真正该押的护城河**：vipdoc 离线 + `LocalDaySink` 增量落盘（对标四家全无）、TDX 协议级能力、集思录可转债/场内基金折溢价。

---

## 4. ROI 排序与资源约束下的取舍

| 档 | 任务 | 工作量 | ROI | 资源紧张时 |
|---|---|---|---|---|
| **T0** | S1(Symbol 解冻) + S2(死名字) + T1/T2(状态透明) + S5(代码别名) | 3 周 | ★★★★★ | **必做** |
| **T1** | D15(估值 enrich) + S3/N1(复权三源) + A1/A2(spec + golden) + D19(百度概念) | 3 周 | ★★★★★ | **必做** |
| **T2** | D16/D17/D18(涨停池+席位+研报PDF) + N2(交易所官方) + A3/A4(路由+风控域) | 4 周 | ★★★★ | 建议做 |
| **T3** | D23/D24(集思录) + D10(分价表) + N3(同花顺) + D5/D6/D8(腾讯深挖) | 4 周 | ★★★ | 可延后 |
| **T4** | N4/N5(中债+见闻) + D2~D4/D11~D14(腾讯新浪深挖) + D25~D26(中行) | 4 周 | ★★ | 可砍 |
| **T5** | I1/I2(MCP 扩容 + TS SDK) | 2 周 | ★★ | 视需求 |

> **最坏情况只做 T0**：3 周，消除 80% 现存风险（Symbol 不确定性、死名字误导、港美股/北交所不可用的最大落差）。

---

## 5. 里程碑与排期

| 阶段 | 周期 | 内容 | 出口标准 |
|---|---|---|---|
| **M0 生存** | W1~W2 | S1~S6（Symbol 解冻 + 死名字 + 复权第二源 + 代码别名 + 握手矩阵） | HK/US/BJ 统一入口可用；8 死名字 100% 归位 |
| **M1 透明+估值** *(与 M0 并行)* | W1~W3 | T1~T4 + D15 + D19（百度概念） | 172 capability 带状态；快照带估值；百度概念归属可用 |
| **M2 复权三源+巨潮** | W2~W4 | S3 + **N1** + D10/D11（新浪分价表/历史明细） | 复权三源对拍；巨潮公告沪深北全量 |
| **M3 东财+交易所硬化** | W4~W6 | D16/D17/D18 + **N2** + D5/D8（腾讯板块排行+新浪美股） + A3/A4 | 涨停池+原因；席位TOP5；研报PDF；两融跨域备胎 |
| **M4 集思录+同花顺** | W6~W10 | D23/D24 + **N3** + D2~D4（腾讯深挖） | 转债字段齐全；题材归因与北向可用 |
| **M5 宏观+期货扩展** | W10~W14 | **N4** + **N5** + 期货日K主力连续 + 筹码CYQ | 宏观利率、快讯可用 |
| **M6 架构硬化** | 贯穿 | A1~A8 + I1~I4 | 新增端点 = 加 spec；`doctor` 全绿 |

---

## 6. 双线扩张详细端点表

### 6.1 纵线：现有 6 源深挖 25 端点

| # | Provider | 端点 | 字段/要点 | V6 编号 | 优先级 |
|---|---|---|---|---|---|
| 1 | 腾讯 | `qt.gtimg.cn/q=ff_` | 资金流(主力/散户净额) | D2 | P1 |
| 2 | 腾讯 | `qt.gtimg.cn/q=s_pk` | 盘口分析 | D3 | P3 |
| 3 | 腾讯 | `web.sqt.gtimg.cn/q=` | 总成交量/外盘内盘/五档/市值 | D4 | P3 |
| 4 | 腾讯 | `proxy.finance.qq.com/.../rank/pt/getRank` | 板块排行(hy2 124行业/gn2 803概念) | D5 | P2 |
| 5 | 腾讯 | `data.gtimg.cn/flashdata/hushen/...` | 历史归档(golden校验) | D6 | P3 |
| 6 | 腾讯 | `stock.finance.qq.com/.../dadan.php` | 大单数据 | — | P4 |
| 7 | 新浪 | `hq.sinajs.cn/list=bj` | 北交所 | D7 | P0 |
| 8 | 新浪 | `hq.sinajs.cn/list=gb_` | 美股盘前盘后(36字段) | D8 | P1 |
| 9 | 新浪 | `market.finance.sina.com.cn/pricehis.php` | 分价表 | D10 | P2 |
| 10 | 新浪 | `market.finance.sina.com.cn/downxls.php` | 历史成交明细 XLS | D11 | P3 |
| 11 | 新浪 | `vip.stock.../CN_Transaction.getAllListPage` | 最新成交明细 | D12 | P3 |
| 12 | 新浪 | `vip.stock.../research/list` | 研报列表(第二来源) | D14 | P3 |
| 13 | 东财 | `push2.../api/qt/stock/get` 补字段 | f162 PE/f167 PB/f116 总市值/f117 流通市值/f168 换手/f51 涨停/f52 跌停 | D15 | P1 |
| 14 | 东财 | `push2ex.../getTopicZTPool` | 涨停池+涨停原因题材 | D16 | P2 |
| 15 | 东财 | `datacenter.../RPT_BILLBOARD_DAILYDETAILSBUY/SELL` | 龙虎榜买卖席位 TOP5 | D17 | P2 |
| 16 | 东财 | `pdf.dfcfw.com/pdf/H3_{infoCode}_1.pdf` | 研报 PDF 下载 | D18 | P2 |
| 17 | 百度 | `finance.pae.baidu.com/selfselect/getrelatedblock` | 概念归属(申万+概念+地域) | D19 | P0 |
| 18 | 百度 | 分时补齐 | | D20 | P3 |
| 19 | 百度 | 逐笔补齐 | | D21 | P3 |
| 20 | 百度 | 五档补齐 | | D22 | P3 |
| 21 | 集思录 | `cb_list/` 补字段 | premium_rt/ytm_rt/convert_value/cb_balance/rating_cd/force_redeem/year_left | D23 | P2 |
| 22 | 集思录 | ETF/QDII/封基/分级/指数估值 | | D24 | P3 |
| 23 | 中行 | 历史汇率 | | D25 | P4 |
| 24 | 中行 | 远期挂牌利率 | | D26 | P4 |
| 25 | 新浪 | `qianfuquan.js`/`houfuquan.js` | 复权因子第二源 | S3 | P0 |

### 6.2 横线：新增 5 Provider 28 端点

#### N1 · 巨潮 cninfo（8 端点 / +12 capability）

| # | 端点 | 能力 | risk_domain |
|---|---|---|---|
| 1 | `webapi.cninfo.com.cn/api/.../p_info3015` | 全量公告 | cninfo |
| 2 | `webapi.cninfo.com.cn/api/.../p_info3014` | 北交所公告 | cninfo |
| 3 | `webapi.cninfo.com.cn/api/.../p_info3024` | 港交所公告 | cninfo |
| 4 | `webapi.cninfo.com.cn/api/.../p_rzrq3104` | 两融明细 | cninfo |
| 5 | `webapi.cninfo.com.cn/api/.../p_stock2202` | 停复牌 | cninfo |
| 6 | `webapi.cninfo.com.cn/api/.../p_blocktrade` | 大宗交易 | cninfo |
| 7 | **复权因子接口** | **复权因子第三源** | cninfo |
| 8 | 互动易 | 舆情问答 | cninfo |

> **重点**：复权因子端点（N1-7）与 S3（新浪因子）+ 东财 `RPT_SHAREBONUS` 组成三源对拍（V4 的 A7）。

#### N2 · 交易所官方 exchange（7 端点 / +10 capability）

| # | 端点 | 能力 | risk_domain |
|---|---|---|---|
| 1 | `query.sse.com.cn/.../margin/detail` | 上交所两融明细 | official |
| 2 | `query.sse.com.cn/.../margin/sum` | 上交所两融汇总 | official |
| 3 | `query.sse.com.cn/.../infodisplay/...` | 上交所大宗交易 | official |
| 4 | `query.sse.com.cn/.../infodisplay/...lhb` | 上交所龙虎榜 | official |
| 5 | `szse.cn/.../ShowReport/...CATALOGID=1837` | 深交所两融 | official |
| 6 | `szse.cn/.../ShowReport/...CATALOGID=1834` | 深交所标的证券 | official |
| 7 | `szse.cn/.../ShowReport/...交易日历` + `sse.com.cn/.../calendar` | 交易日历(替换内置静态表) | official |
| 8 | 上交所按日归档 + 深交所快照 | ETF 份额 | official |

#### N3 · 同花顺 ths（6 端点 / +9 capability）

| # | 端点 | 能力 | risk_domain |
|---|---|---|---|
| 1 | `zx.10jqka.com.cn/event/api/getharden` | **题材归因(独家)** | ths |
| 2 | `data.10jqka.com.cn/hgt/...` 或 `data.hexin.cn/...hsgtApi` | 北向资金(分钟/历史) | ths |
| 3 | `basic.10jqka.com.cn/.../concept` | 一致预期 EPS | ths |
| 4 | `basic.10jqka.com.cn/.../conceptlist` | 概念成分 | ths |
| 5 | `basic.10jqka.com.cn/.../limit_up_pool` | 涨停池 | ths |
| 6 | `basic.10jqka.com.cn/.../hot` | 热榜 | ths |

#### N4 · 中债/货币网 chinabond（4 端点 / +6 capability）

| # | 端点 | 能力 | risk_domain |
|---|---|---|---|
| 1 | `chinabond.com.cn/.../yieldcurve` | 国债收益率曲线 | chinabond |
| 2 | `chinabond.com.cn/.../credit` | 信用债收益率 | chinabond |
| 3 | `chinamoney.com.cn/.../lpr` | LPR | chinabond |
| 4 | `chinamoney.com.cn/.../shibor` + `回购定盘` | Shibor + 回购定盘利率 | chinabond |

#### N5 · 华尔街见闻 wallstreet（3 端点 / +4 capability）

| # | 端点 | 能力 | risk_domain |
|---|---|---|---|
| 1 | `wallstreetcn.com/.../live` | 7×24 快讯 | media |
| 2 | `wallstreetcn.com/.../calendar` | 全球宏观日历 | media |
| 3 | `cn.cctv.com/.../xinwenlianbo` | 新闻联播文字稿 | media |

---

## 7. 验收 Checklist

### M0 生存
- [ ] `Client.call("quotes", symbols=["hk00700","usaapl","bj832000"])` 统一入口可用
- [ ] `SH510500` 大小写不敏感输入别名可用
- [ ] 8 个死名字 100% 归位，offline 名字在四面出口归零
- [ ] 5 协议族握手矩阵全绿，且为**常驻 CI**（非一次性）
- [ ] 新浪复权因子第二源可用

### M1 透明+估值
- [ ] 172 capability 带状态，hk/us/bj 标 `available`
- [ ] 快照带估值字段（显式 `enrich=["valuation"]`，逐字段 `_provenance`）
- [ ] 百度概念归属返回申万一级/二级 + 概念 + 地域三维
- [ ] `runtime.health` 中 hk/us/bj capability 状态为 `available`

### M2 复权三源+巨潮
- [ ] 复权因子三源（东财 / 新浪 / 巨潮）三向对拍，偏差 >0.5% 告警
- [ ] 巨潮公告沪深北全量可检索
- [ ] 官方交易日历替换内置静态表，`diagnostics.py:364` 自曝注释可删除
- [ ] 新浪分价表可用（替代已死 0x051A）

### M3 东财+交易所硬化
- [ ] 涨停池 + 涨停原因题材可用
- [ ] 龙虎榜买卖席位 TOP5 可用（或明确标注上游失效并下线）
- [ ] 研报 PDF 可下载（Referer 装配正确）
- [ ] 两融 / 大宗有 ≥2 个不同 risk_domain 的源
- [ ] ETF 份额可用
- [ ] 腾讯 `ff_` 资金流跨域备胎可用
- [ ] 新浪 `gb_` 美股盘前盘后可用

### M4 集思录+同花顺
- [ ] 集思录可转债 7 个核心字段齐全
- [ ] 题材归因 `getharden` 可用（若失效则显式下线，不留死名字）
- [ ] 北向资金分钟 + 历史可用
- [ ] 腾讯板块排行（124 行业 + 803 概念）可用
- [ ] `docs/providers/ths.md` 注明维护成本与 30 天稳定性观察结果

### M5 宏观+期货
- [ ] 国债/信用债收益率曲线可用
- [ ] LPR / Shibor / 回购定盘可用
- [ ] 7×24 快讯可用
- [ ] 期货日 K 主力连续可用
- [ ] 筹码分布 CYQ 可用（本地自算）

### 全局
- [ ] Web Provider 11 → **16**（+cninfo / exchange / ths / chinabond / wallstreet）
- [ ] Web 端点 ≈51 → **≈140**（深挖 +25 + 新源 +28 = +53）
- [ ] 注册 capability 172 → **≈340**，且**每个 ≥2 个不同 risk_domain 的源**
- [ ] 港美股 / 北交所 / 商品 / 外汇 全部可查
- [ ] **复权因子三源对拍**
- [ ] 估值字段进快照
- [ ] 集合竞价等无替代的死能力**显式下线**
- [ ] 9 类字段陷阱各 ≥1 条 golden 断言
- [ ] `atst doctor` 输出全端点健康度
- [ ] 每个新源有 `docs/providers/<source>.md`，含**实测日期与状态**
- [ ] README 能力矩阵由 capabilities 状态**自动生成**，禁止手抄

---

## 8. 风险与合规

| # | 风险 | 应对 |
|---|---|---|
| **N1** | **北交所行情授权**：`bse.cn` Level-1 行情需授权 | G04/S1 只走腾讯/新浪 `bj` 前缀，不直连 bse.cn；README 与 SECURITY.md 明示"个人研究用途" |
| **N2** | **同花顺维护成本高**：Cookie + 限流 + 字段级 null | N3 放 M4 之后；`zhangfu/huanshou/close` 等 **null 保留 null，绝不填 0**；接入前跑 30 天稳定性观察 |
| **N3** ⭐ | **S1 放开 HK/US 可能引入静默错误**：腾讯 hk/us 与 A 股字段布局不同，解析器按 A 股 88 字段切会**静默产出错数据** | **必须先写字段 spec（A1）+ golden 样本，再放 Symbol 层**。顺序反了就是 P0 事故 |
| 4 | 巨潮/交易所官方接口的请求频次合规 | N1/N2 统一走 per-domain 并发闸门（A6），不与东财系共用配额 |
| 5 | 东财系单点（22+端点同域，被封全灭） | D2 `ff_` + N2 交易所官方为必做备胎；push2delay 已就位 |
| 6 | 协议变更是持续战 | S6 握手矩阵做成常驻 CI，修完不拆 |
| 7 | 不要盲目追源数量 | 源价值 = 可用性 × 独立性 × 1/维护成本 |
| 8 | 复权是隐性 bug 高发区 | 三源完成前，对复权结果加日级对拍告警 |
| 9 | 文档与现实落差比功能缺失更危险 | README 每个市场/能力标注「实测日期 + 状态」，与 capabilities 状态同源生成 |

---

## 附录 · 9 类字段陷阱速查表（每条需 ≥1 条 golden 断言）

| # | 陷阱 | 详情 | 后果 | 防护 |
|---|---|---|---|---|
| 1 ⚠️ | **腾讯 mkline 第 8 字段** | `[时间,开,收,高,低,量(手),{},换手率基点]`——**是换手率不是成交额**；各根累加÷100=当日换手率%；成交额 = 量(手)×100×均价 | 日成交额**小三个数量级且不报错** | 交叉对拍 |
| 2 | 编码 | 腾讯/新浪 **GBK/GB18030**；东财/百度 UTF-8 | 中文名乱码 | transport 层统一解码 |
| 3 | Referer | 新浪缺 Referer→**403**；腾讯 mkline 需 `gu.qq.com`；集思录需 Cookie+Referer | 静默失败 | spec 声明 `required_headers` |
| 4 | 周期上限 | 腾讯 mkline **≤320**；新浪 K线 **≤1023**；东财 `lmt` | 数据截断 | spec 声明 `max_bars`，超限分页并告警 |
| 5 | 周期缺失 | **新浪无周/月线**（需自行聚合） | 口径不一致 | spec 标注 `derived=true` |
| 6 | 单位差 | 量：腾讯=手 / 新浪=**股** / 东财=手；额：腾讯=**万元** / 新浪=**元**；市值：腾讯=亿 / 东财=元 | 数值差 100~10000 倍 | spec 声明 `unit`，自动换算 |
| 7 | 五档位置 | 腾讯 [9-28]；新浪 [10-29]；东财 f19-f40 | 买卖颠倒/错位 | spec 声明 bid/ask index |
| 8 | 复权口径 | 东财/腾讯=服务端算；新浪=给因子自算；atst=AdjustEngine 自算 | 除权日跳空处理不一致 | 三源日级对拍告警 |
| 9 | **同花顺字段 null** | `zhangfu/huanshou/close` 曾整体返回 null | 填 0 会造出假数据 | **null 保留 null，绝不填 0** |

---

## 9. 待核实项（落地前以仓库实测为准）

| # | 项 | 建议核实方式 |
|---|---|---|
| 1 | atst 是否已支持 0c03 tdxlevel 认证与新版版本标识 `18 7b 00 01` | 真机握手 |
| 2 | 腾讯 7 适配器具体覆盖哪些端点（是否含 `ff_` / `s_pk` / `getRank`） | 已实测：6 族 host，缺 ff_/s_pk/getRank |
| 3 | `qt.gtimg.cn` 的 hk/us/hf_/fx_ 是否被 Symbol 层提前拦截 | **已核实：是的，`symbol.py:123` 拦截** |
| 4 | atst 是否已踩腾讯 mkline 第 8 字段陷阱 | 建议直接跑一次对拍 |
| 5 | 8 个死名字中"2 个 inferred-block"具体是哪两个 | 查 CHANGELOG |
| 6 | `export_security_list`（0x044D 分页）与 `security_list` 的 offline 判定是否冲突 | 查 catalog |
| 7 | 东财 `push2ex` 涨停池 2026-09 后是否仍在服役 | 真机请求 |
| 8 | 北交所 `bse.cn` 匿名 Cookie 会话可用性 + **授权要求确认** | 法务确认 |
| 9 | 集思录 `cb_list_new` 是否需登录 Cookie | 真机请求 |
| 10 | 巨潮"证券复权因子"接口的字段口径与调用方式 | 抓包 |
| 11 | 深交所 `ShowReport/data` 各 `CATALOGID` 完整清单 | 抓包 |
| 12 | 新浪 `qianfuquan.js` 返回结构与因子精度 | 真机采样 |
| 13 | 同花顺 `getharden` 当前是否仍返回有效数据 | 真机请求 |
| 14 | 新浪 `gb_` 美股 36 字段含盘前盘后的字段索引 | 真机采样 |
| 15 | 百度 `getrelatedblock` 返回的申万分类层级深度 | 真机采样 |
| 16 | 东财 `reportapi` 是否已含行业研报 qType=1 | 发一次 qType=1 请求 |
| 17 | 龙虎榜 `RPT_BILLBOARD_DAILYDETAILSBUY/SELL` 是"已失效"还是"报表名变更" | 抓包 datacenter-web 最新报表名 |
| 18 | 巨潮互动易接口是否需登录 | 真机请求 |
| 19 | 中债收益率曲线接口的返回格式（JSON/CSV） | 抓包 |
| 20 | 华尔街见闻快讯接口是否需登录 | 真机请求 |
| 21 | 同花顺 `hsgtApi` 北向资金接口的字段口径 | 抓包 |

---

## 10. 与 V4/V5 方案的合并关系

| V4 编号 | V5 编号 | V6 编号 | 状态修正 |
|---|---|---|---|
| D1 腾讯 hk/us/hf_/fx_ | G01 | S1 | **适配器已就绪，工作量 1 周 → 3 天** |
| D9 新浪 bj/hk/gb_ | G04+G14 | D7+D8+S1 | hk 已接；bj 与 gb_ 未接 |
| D11 新浪复权因子 | G02 | S3 | 未变 |
| D6 东财估值 enrich | G07 | D15 | 未变 |
| D19 push2delay | — | — | **已完成，可划掉** |
| D18 涨停池 | G08 | D16 | 部分完成，缺 getTopicZTPool 原因 |
| N1 巨潮 | G05+G06 | N1 | 未变 |
| N2 交易所官方 | G11+G12+G16 | N2 | 未变 |
| N3 北交所官方 | — | — | **降级**：S1 用腾讯/新浪前缀即可，不接 bse.cn |
| N4 同花顺 | G18 | N3 | 未变；新增 N2 风险 |
| N5 期货交易所 | G23 | — | 部分已有 TDX 0x0207，降级 |
| N6/N7 宏观 | G21+G22 | N4 | 未变 |
| N8 HKEX | — | — | 归入 N3 的北向 |
| N9/N10/N11 新闻舆情 | G28~G30 | N5 | 未变 |
| — | G03 百度概念 | D19 | V5 新增，V6 保留 |
| — | G10 研报PDF | D18 | V5 新增，V6 保留 |
| — | G15 转债全字段 | D23 | V5 新增，V6 保留 |
| — | G16 ETF份额 | N2-8 | V5 新增，V6 保留 |
| — | G25 筹码CYQ | — | V5 新增，V6 归入 M5 |
| — | G32 TS SDK | I2 | V5 新增，V6 保留 |
| — | G33 代理池 | — | 不做（atst 的 host 池比单 IP 代理更稳） |
| A1~A8 | A1~A8 | A1~A8 | 并行贯穿，仍是硬依赖 |
| T1/T2 透明层 | T1/T2 | T1/T2 | 未变 |
| I2 TS SDK | G32 | I2 | 未变 |

**V6 新增、V4/V5 未列的关键项**：
- **S1 Symbol 层解冻**（V5 的 G01 提级为 V6 的 S1，作为整个方案的第一优先级）
- **S5 stock-api 代码格式别名**（V4/V5 均未列）
- **D19 百度概念反查**（V5 的 G03）
- **D18 研报 PDF 下载**（V5 的 G10）
- **D23 集思录可转债全字段**（V5 的 G15）
- **N2-8 ETF 份额**（V5 的 G16）
- **N3 同花顺风险条目 N2**（字段级 null 防护）

---

## 11. 最坏情况：只做 M0

**投入 3 周，消除 80% 的"宣称 vs 现实"落差**：

1. 港美股/北交所从统一入口可查（4 个已有适配器被激活）
2. 8 个死名字 100% 归位
3. 复权第二源就位
4. `runtime.health` 开始说真话
5. stock-api 代码格式兼容
6. 5 协议族握手矩阵常驻 CI
