# atst 四方对标与全量覆盖方案

> 对标对象：`1nchaos/adata`、`zhangxiangliang/stock-api`、`simonlin1212/a-stock-data`、`ArvinLovegood/go-stock`
> 对标基准：`coeasy/atst`（`p:/github_public/tstdx`，v1.0.1 @ `21df21b`）
> 编写日期：2026-10-01
> 前序文档：`atst最终最优化方案.md`（V2/V3/V4 整合版）——**本文档是它的第 5 层补丁，不取代它，而是修正其口径并补上它漏掉的缺口。**

---

## 0. 执行摘要

### 0.1 一句话结论

**行情核心面（stock-api 全部、adata 大部分）atst 已全覆盖且更深；真正的缺口在"资讯 / 信号 / 事件 / 宏观 / 指数"这 5 个非行情层——因为 atst 的 Provider 全集里根本没有巨潮、同花顺、交易所官方、财联社/见闻、统计局/中债这几家源。**

### 0.2 关键数字（实测，非文档自述）

| 指标 | atst 实测 | a-stock-data | adata | stock-api |
|---|---|---|---|---|
| 数据源 | **11 Provider** | 34 源 | ~6 源 | 3 源 |
| 能力/端点 | **172 capability** | 87 端点 | ~45 API | 5 MCP 工具 |
| 行情协议 | TDX 5 协议族 + Web | 纯 HTTP | HTTP | HTTP |
| 语言/形态 | Python 库/CLI/HTTP/WS/MCP | Skill(Markdown) | Python SDK | TS SDK |
| 桌面 UI / AI | ❌（明确不做） | ❌ | ❌ | ❌ |

### 0.3 覆盖判定

| 对标项目 | 判定 | 说明 |
|---|---|---|
| **stock-api** | ✅ **已全覆盖** | 能力面 100% 覆盖，仅剩 2 处策略差异（见 §4.1），无需投入 |
| **adata** | 🟡 **90% 覆盖** | 6 个小缺口：概念板块反查、申万行业、ETF 份额、交易日历源、概念实时行情、指数成分（见 §4.2） |
| **a-stock-data** | 🟠 **~55% 覆盖** | 行情/资金/财务/公告/龙虎榜已覆盖；**7 大类非行情层缺失**（见 §4.3） |
| **go-stock** | ⚪ **定位差异，不做** | 应用层（桌面 UI / AI 分析 / 报警），atst 是基础设施，L6 明确排除（见 §4.4） |

### 0.4 如果只做一件事

> **放开 `Symbol` 层对 HK/US 的 fail-closed 拦截。**
> 腾讯 `qt.gtimg.cn/q=hk|us` 与新浪 `hq.sinajs.cn/list=hk` 的适配器**已经写完**（`atst/web/sources.py` 已声明、`atst/web/tencent/adapters.py` 有实现），但 `atst/domain/symbol.py:123` 的 `tdx_market` 对 HK/US 直接 `raise SymbolError`，导致统一入口 `Client.call("quotes", market="hk")` 完全走不通，只能绕道 Direct Provider API。
> 这是**投入最小、落差最大**的一处：代码已就绪，缺的只是策略放行。

---

## 1. 实测口径校准（V4 方案文档三处过时数字）

V4 方案（2026-10-01 整合）成文时，东财/腾讯的接入面比它写的宽得多。**以代码为准**，以下为实测更正：

| V4 文档说法 | 实测现状 | 影响 |
|---|---|---|
| "6 个 Web Provider 仅 18 个适配器 / 约 30 端点" | Provider 实为 **11 个**（`tdx/tencent/sina/eastmoney/baidu/jsl/boc/iwencai` + `builtin/derived/local_vipdoc`）；东财单家已接 13 个 host（`push2` / `push2his` / `push2delay` / `92.push2` / `92.push2his` / `push2ex` / `datacenter-web` / `datacenter` / `reportapi` / `np-anotice-stock` / `newsapi` / `emappdata` / `fundmobapi` / `fundf10` / `fundts`） | V4 的"东财 22 端点"低估了实际规模；**D19 `push2delay` 已接入**（8 处引用），V4 把它列为"必做"——实际已完成 |
| "腾讯公开端点 ≥14 个仅接入约 6 个" | 已接 6 族 host：`qt.gtimg.cn`（A股+**hk**+**us**+**hf_**+**s_**）、`ifzq.gtimg.cn`（kline+fqkline）、`web.ifzq.gtimg.cn`（minute）、`stock.gtimg.cn`（逐笔）、`proxy.finance.qq.com`（getBoardRankList + mktHs/rank） | **D1 多市场前缀的适配器已存在**，卡在 Symbol 层（见 §0.4）；V4 把 D1 列为"最高杠杆"仍成立，但工作量从"1 周"降到"2~3 天" |
| "复权事件源（东财单点）" | 仍是单点。`AdjustEngine` 自算因子（`atst/domain/adjust.py`），事件源只有东财 `RPT_SHAREBONUS`；新浪 `qianfuquan`/`houfuquan` **0 引用**，巨潮复权因子 **0 引用** | V4 判断准确，D11/N1 仍是 P0 |

**另外三处 V4 未列但实测已完成的**（可从待办里划掉）：
- 东财 datacenter 六报表已接：`RPT_LIFT_STAGE`（解禁）、`RPTA_WEB_RZRQ`（两融）、`RPT_SHAREBONUS`（分红）、`RPT_DATA_BLOCKTRADE`（大宗）、`RPT_HOLDERNUM`（股东户数）、`RPT_PUBLIC_OP_NEWPREDICT`（业绩预告）
- 百度 K 线 MA5/MA10/MA20 已实现（`atst/web/baidu/adapters.py:217-219`）
- 期货持仓排名有 TDX 协议路径（`0x0207 GOODS_HOLDING`），非全缺

---

## 2. 四方能力全景矩阵

图例：✅ 已覆盖 ｜ 🟡 部分覆盖 ｜ ❌ 缺失 ｜ ⚪ 定位排除

### 2.1 行情层

| 能力 | atst | adata | a-stock-data | stock-api |
|---|---|---|---|---|
| A 股实时价 + 五档 | ✅ TDX/腾讯/新浪/百度 | ✅ | ✅ | ✅ |
| 港股实时 | ✅ 腾讯 `hk` + 新浪 `hk`（**入口被 Symbol 拦**） | ⚪ | ✅ | ✅ |
| 美股实时 | ✅ 腾讯 `us`（入口被拦）；❌ 新浪 `gb_` 盘前盘后 | ⚪ | ✅ | ✅ |
| 北交所实时 | ❌ 腾讯 `bj` / 新浪 `bj` 均未接 | ⚪ | 🟡 备胎 | ⚪ |
| 外盘商品 / 外汇 | ✅ 腾讯 `hf_` + 中行牌价 | ⚪ | ✅ | ⚪ |
| 日/周/月 K + 分钟 K | ✅ 腾讯/东财/新浪/百度/TDX | ✅ | ✅ | ✅ |
| 前后复权 K | ✅ 腾讯 `fqkline` + 东财 + `AdjustEngine` | ⚪ | ✅ | ⚪ |
| K 线带 MA5/10/20 | ✅ 百度 | ⚪ | ✅ | ⚪ |
| 分时 / 逐笔 | ✅ 腾讯 `minute/query` + `stock.gtimg.cn` | ✅ | ✅ | ⚪ |
| 大盘统计 | ✅ 腾讯 `s_` | ⚪ | ⚪ | ⚪ |
| 复权因子序列（外部源） | ❌ 仅东财事件源 | ⚪ | ✅ 新浪因子 | ⚪ |
| 通达信盘后包（全市场日线） | ❌ vipdoc 是本地不是官网下载 | ⚪ | ✅ | ⚪ |

### 2.2 板块 / 概念 / 指数

| 能力 | atst | adata | a-stock-data |
|---|---|---|---|
| 行业板块列表 + 排行 | ✅ 新浪 `newSinaHy/newFLJK` + 腾讯 `mktHs/rank` + 东财 `clist` | ✅ | ✅ |
| 板块成分股 | ✅ 东财 `clist` | ✅ | ✅ |
| **个股 → 所属概念反查** | ❌ | ✅ 同花顺 + 东财 | ✅ 东财板块归属 |
| **申万一级/二级行业** | ❌ | ✅ 百度 | ✅ 申万变迁史 |
| 概念排行 `hy2`/`gn2`（124 行业 / 803 概念） | ❌ | ⚪ | ✅ |
| 概念 / 指数行情 K 线 | 🟡 东财 `clist` 可推 | ✅ | ✅ |
| 概念实时行情 | ❌ | ✅ | ✅ |
| 概念资金流（今日/5日/10日） | 🟡 有板块资金流骨架 | ✅ | ✅ |
| 中证/国证成分与权重 | ❌ | 🟡 部分 | ✅ |
| 中证 PE 与股息率分位 | ❌ | ⚪ | ✅ |
| 官方交易日历 | 🟡 **仅内置静态表**，无 web 适配器（`diagnostics.py:364` 已自曝） | ✅ 深交所 | ✅ 深交所 |

### 2.3 资金 / 信号 / 龙虎榜

| 能力 | atst | adata | a-stock-data |
|---|---|---|---|
| 个股资金流（分钟/120 日） | ✅ 东财 `push2` / `push2his` fflow | ✅ | ✅ |
| 腾讯 `ff_` 资金流（跨域备胎） | ❌ | ⚪ | ⚪ |
| 板块资金流 | 🟡 | ✅ | ✅ |
| 龙虎榜每日上榜 | ✅ 东财 `RPT_DMSK_TS_STOCKNEW` | ✅ | ✅ |
| **龙虎榜买卖席位 TOP5** | ❌（`longhu.py:38` 注释"经探测已失效"） | ✅ | ✅ |
| 涨停池 / 炸板池 / 跌停池 | 🟡 有 `ZBPool` / `DTPool` | ⚪ | ✅ |
| **`getTopicZTPool` 涨停池 + 涨停原因题材** | ❌ | ⚪ | ✅ |
| **同花顺题材归因 `getharden`** | ❌ | ⚪ | ✅ **独家** |
| **北向资金（分钟/历史）** | ❌ | ⚪ | ✅ |
| 限售解禁 | ✅ `RPT_LIFT_STAGE` | ⚪ | ✅ |
| 两融明细 / 汇总 | 🟡 `RPTA_WEB_RZRQ`（东财单源） | ⚪ | ✅ 东财+交易所官方 |
| 大宗交易 | ✅ `RPT_DATA_BLOCKTRADE` | ⚪ | ✅ |
| 股东户数 | ✅ `RPT_HOLDERNUM` | ⚪ | ✅ |
| 人气榜 | ✅ 东财 `emappdata` | ⚪ | ✅ |
| 重点监控池 / 日内异动池 | ❌ | ⚪ | ✅ |
| 筹码分布 CYQ | ❌ | ⚪ | ✅ |

### 2.4 财务 / 基本面 / 事件

| 能力 | atst | adata | a-stock-data |
|---|---|---|---|
| 三表 / 财报 | ✅ 东财 `datacenter` + 新浪三表 + TDX F10 | 🟡 核心财务 | ✅ |
| 季报快照（37 字段） | ✅ TDX finance | ⚪ | ✅ |
| 公司概况 / 股本 / 股东变化 / 高管 | ✅ | ⚪ | ✅ |
| 分红送转 | ✅ `RPT_SHAREBONUS` | ✅ | ✅ |
| 评级 / 研报列表 | ✅ 东财 `reportapi` | ⚪ | ✅ |
| **研报 PDF 下载** | ❌ | ⚪ | ✅ |
| **行业研报**（qType=1） | 🟡 同端点，未单独封装 | ⚪ | ✅ |
| **一致预期 EPS** | ❌ | ⚪ | ✅ 同花顺 |
| 自然语言搜索 | ✅ iwencai（需 cookie） | ⚪ | ✅ iwencai |
| 业绩预告 / 快报 | ✅ `RPT_PUBLIC_OP_NEWPREDICT` | ⚪ | ✅ |
| 机构调研 | 🟡 best-effort（`news.py:39` 自曝"需重新抓包校准"） | ⚪ | ✅ |
| 增减持 / 回购 / 股权质押 | ❌ | ⚪ | ✅ |
| 新股申购日历 | 🟡 web 侧仅 `ipo_calendar` | ⚪ | ✅ |
| **估值历史（PE/PB/PS+换手+ST）** | ❌ | ⚪ | ✅ baostock |
| 上市退市日 | ❌ | ⚪ | ✅ |
| 申万行业变迁史 / ST 名单 | ❌ | ⚪ | ✅ |

### 2.5 公告 / 新闻 / 舆情

| 能力 | atst | adata | a-stock-data |
|---|---|---|---|
| 个股公告 | ✅ 东财 `np-anotice-stock` | ⚪ | ⚪ |
| **巨潮全量公告（沪深北）** | ❌ **无 cninfo Provider** | ⚪ | ✅ |
| 个股新闻 | ✅ 新浪 `vCB_AllNewsStock` + 东财 `newsapi` | ⚪ | ✅ |
| 7×24 快讯 | 🟡 东财快讯 | ⚪ | ✅ 华尔街见闻/央视网 |
| 财联社电报 | ❌（上游已下线） | ⚪ | ⚪ |
| 互动易 / 上证 e互动 | ❌ | ⚪ | ✅ |
| 同花顺热榜 / 概念命中 | ❌ | ⚪ | ✅ |

### 2.6 宏观 / 期货 / ETF / 转债

| 能力 | atst | adata | a-stock-data |
|---|---|---|---|
| CPI | ✅ `RPT_ECONOMY_CPI` | ⚪ | ✅ |
| 社融 / PMI | ❌ | ⚪ | ✅ |
| 国债/信用债收益率曲线、LPR、Shibor、回购定盘 | ❌ | ⚪ | ✅ |
| 全球宏观日历 | ❌ | ⚪ | ✅ |
| 期货实时 / K 线 / 逐笔 | ✅ 东财 `efinance_deriv` + TDX | ⚪ | ✅ |
| 期货会员持仓排名 | 🟡 TDX `0x0207`（非交易所官方） | ⚪ | ✅ |
| 期货日 K 主力连续 + 具体合约 | ❌ | ⚪ | ✅ |
| A50 期指 / 上海金现货 | ❌ | ⚪ | ✅ |
| ETF 净值 / 估值 / 列表 / 排行 / 经理 / 公司 / 持仓 | ✅ 东财 `fundmobapi` 全套 | ✅ ETF 行情 | ✅ |
| ETF 场内基金折溢价 | ✅ 集思录 | ⚪ | ✅ |
| **ETF 份额** | ❌ | ⚪ | ✅ |
| ETF 期权 T 型 / 希腊字母 / IV | 🟡 东财 `efinance_options` | ⚪ | ✅ 新浪 |
| 可转债基础列表 | ✅ 集思录 `cb_list` | ⚪ | ✅ |
| **可转债核心字段（溢价率/到期收益率/转股价值/评级/强赎/剩余年限）** | ❌ | ⚪ | ✅ |

### 2.7 工程与接入形态

| 能力 | atst | adata | a-stock-data | stock-api | go-stock |
|---|---|---|---|---|---|
| Python 库 | ✅ | ✅ | ✅(Skill) | ⚪ | ⚪ |
| TypeScript 库 / 浏览器 SDK | ❌（L5-I2 未做） | ⚪ | ⚪ | ✅ | ⚪ |
| CLI | ✅ 31 子命令 | ⚪ | ⚪ | ✅ | ⚪ |
| HTTP / WebSocket / MCP | ✅ 43 端点 / 13-19 方法 / 9-13 工具 | ⚪ | ⚪ | ✅ MCP | ⚪ |
| 桌面 UI | ⚪ | ⚪ | ⚪ | ⚪ | ✅ Wails |
| AI 分析 / 报警 / 回测 | ⚪（L6 排除） | ⚪ | ⚪ | ⚪ | ✅ |
| 代理池支持 | ❌ | ✅ | ⚪ | ⚪ | ⚪ |
| 多源自动兜底 | ⚪ **显式禁止隐式跨 Provider fallback**（架构契约） | ✅ | ✅ | ✅ | ⚪ |

---

## 3. 缺口清单（58 项，按优先级分级）

### P0 · 必须做（消除"宣称 vs 现实"落差，1 周内）

| # | 缺口 | 现状证据 | 投入 | 收益 |
|---|---|---|---|---|
| **G01** | **HK/US 从统一入口不可达** | `atst/domain/symbol.py:123` `tdx_market` 对 HK/US `raise SymbolError`；而 `atst/web/sources.py` 已声明腾讯 `hk`/`us`、新浪 `hk`，`tencent/adapters.py` 已实现 | 2~3 天 | 一个改动激活 4 个已写好的适配器 |
| **G02** | **新浪 `qianfuquan.js` / `houfuquan.js` 复权因子** | 0 引用；`AdjustEngine` 只能吃东财事件 | 2~3 天 | 复权第二源，V4 的 D11 |
| **G03** | **百度 `getrelatedblock` 概念板块归属**（申万一级/二级 + 概念 + 地域） | `baidu/adapters.py` 仅 `getstockquotation` 1 端点 | 1~2 天 | 补齐 adata 的核心信息能力，成本最低 |
| **G04** | **北交所 `bj` 前缀**（腾讯 `bj` + 新浪 `bj`） | 两处均未接 | 2 天 | 补齐国内第四市场 |

### P1 · 建议做（2~4 周）

| # | 缺口 | 数据源 | 投入 |
|---|---|---|---|
| **G05** | **巨潮 cninfo Provider**：公告全量 + 两融 + 大宗 + 停复牌 + 复权因子 | `cninfo.com.cn` | 1 周 |
| **G06** | **巨潮证券复权因子**（复权第三源，与 G02 组成三源对拍） | 同上 | 含在 G05 |
| **G07** | 东财估值字段进快照（`f162` PE / `f167` PB / `f116`/`f117` 市值 / `f168` 换手 / `f51`/`f52` 涨跌停价） | `push2/.../stock/get` | 2 天 |
| **G08** | 东财 `getTopicZTPool` 涨停池 + 涨停原因题材 | `push2ex` | 2 天 |
| **G09** | 龙虎榜买卖席位 TOP5 重抓包（`RPT_BILLBOARD_DAILYDETAILSBUY/SELL`） | `datacenter-web` | 2 天 |
| **G10** | 研报 PDF 下载（`pdf.dfcfw.com/pdf/H3_{infoCode}_1.pdf`，需 Referer） | `reportapi` | 1 天 |
| **G11** | 交易所官方 Provider：上交所两融明细/汇总 + 公司概况页；深交所 `ShowReport` 两融/标的证券/龙虎榜 | `sse.com.cn` / `szse.cn` | 1 周 |
| **G12** | **官方交易日历适配器**（深交所/上交所）替换内置静态表 | 同上 | 2 天 |
| **G13** | 腾讯 `ff_` 资金流（东财被封时的跨风控域备胎） | `qt.gtimg.cn` | 1 天 |
| **G14** | 新浪 `gb_` 美股（36 字段含盘前盘后） | `hq.sinajs.cn` | 1 天 |
| **G15** | 集思录可转债全字段（`premium_rt` / `ytm_rt` / `convert_value` / `cb_balance` / `rating_cd` / `force_redeem` / `year_left`） | `jisilu.cn` | 3 天 |
| **G16** | ETF 份额（上交所按日归档 + 深交所快照） | 交易所官方 | 2 天 |
| **G17** | 新浪 `pricehis.php` 分价表（替代已死的 TDX 0x051A 量价分布） | `market.finance.sina.com.cn` | 2 天 |

### P2 · 可选做（4~8 周）

| # | 缺口 | 数据源 |
|---|---|---|
| **G18** | **同花顺 Provider**：`getharden` 题材归因（独家）+ `data.hexin.cn hsgtApi` 北向 + `basic.10jqka` 一致预期 EPS + 概念成分 | `10jqka.com.cn` |
| **G19** | 增减持 / 股票回购 / 股权质押 / 机构调研重抓包 | 东财 `datacenter` |
| **G20** | 估值历史（PE/PB/PS + 换手 + 停牌 + ST）、上市退市日、申万行业变迁史、ST 名单 | baostock 路径 / 申万 |
| **G21** | 宏观利率层：国债/信用债收益率曲线、LPR、Shibor、回购定盘利率 | 中债 / 中国货币网 |
| **G22** | 社融 / PMI / 全球宏观日历 | 人民银行 / 统计局 |
| **G23** | 期货日 K 主力连续 + 具体合约、A50 期指、上海金现货 | 新浪 / 上金所 |
| **G24** | 中证/国证成分与权重、中证 PE 与股息率分位 | 中证指数 / 国证 |
| **G25** | 筹码分布 CYQ（本地计算，无外部依赖） | 自算 |
| **G26** | 腾讯 `s_pk` 盘口分析 / `web.sqt.gtimg.cn` 五档市值 / `proxy.finance getRank`（124 行业 + 803 概念排行） | `qt.gtimg.cn` 系 |
| **G27** | 重点监控池 / 日内异动池 | 东财 `push2ex` |

### P3 · 视需求做

| # | 缺口 | 说明 |
|---|---|---|
| **G28** | 互动易（巨潮）/ 上证 e互动 | 舆情问答，合规敏感 |
| **G29** | 华尔街见闻 / 央视网新闻联播文字稿 | 新闻补强；财联社已下线不用做 |
| **G30** | 同花顺热榜 / 概念命中 | 与 G18 同源 |
| **G31** | 新浪 ETF 期权 T 型报价 / 希腊字母 / IV | 东财已有基础期权能力 |
| **G32** | TypeScript / 浏览器 SDK（V4 的 I2） | 走 OpenAPI 生成，不重写协议 |
| **G33** | 代理池支持（adata 有） | 仅当部署在受限网络时 |

### P4 · 明确不做

| 缺口 | 理由 |
|---|---|
| go-stock 的桌面 UI / AI 分析 / 钉钉报警 / 弹幕 / 指标选股 / 多轮对话 | atst 是基础设施，V4 的 L6 已明确排除；这些是消费端应用 |
| 通达信官网盘后包 zip 下载 | atst 有 `vipdoc` 本地离线解析 + `LocalDaySink` 增量落盘，**这是护城河而非缺口**——比下载 zip 更强 |
| akshare 兼容层 | V4 明确移除第三方数据封装依赖，纪律不变 |

---

## 4. 逐家差异分析

### 4.1 stock-api — ✅ 已全覆盖，仅 2 处策略差异

能力面（`getStock` / `getStocks` / `getKlines` / `searchStocks` / `inspectStock` 五个 MCP 工具）atst 全部有对应实现。

| 差异 | stock-api | atst | 判定 |
|---|---|---|---|
| 多源兜底 | `stocks.auto` 自动 `tencent → sina → eastmoney` | **显式禁止隐式跨 Provider fallback**；跨源只走显式 `FallbackPolicy` + `ProviderOrchestrator` | **设计差异，不改**（架构契约，改了就破坏 provenance 可审计性） |
| 代码格式 | `SH510500` / `SZ000651` / `HK00700` / `USAAPL` 前缀 | `sh510500` / `sz000651` / `hk00700` / `usaapl` | 建议在 Symbol 归一化层加一个大小写不敏感的 `SH510500` 输入别名，成本极低 |

**投入：0~0.5 天**（仅加代码格式别名）。

### 4.2 adata — 🟡 90% 覆盖，6 个小缺口

| 缺口 | 对应项 | 工作量 |
|---|---|---|
| 个股 → 概念反查（同花顺 + 东财） | G03 + G18 | 3 天 |
| 申万一级/二级行业 | G03（百度 `getrelatedblock` 含申万分类） | 含在 G03 |
| 概念实时行情 | G26 的 `getRank` | 2 天 |
| ETF 份额 | G16 | 2 天 |
| 交易日历官方源 | G12 | 2 天 |
| 指数成分（同花顺口径） | G18 | 含在 G18 |

> adata 还带 **代理池**（`adata.proxy(ip=...)` 每次请求换 IP）。atst 有分层限流 + 主站池，但无代理池抽象。若目标场景包含受限出口网络，可考虑 G33；否则不需要——atst 的东财 host 池已有 5 台 failover，比单 IP 代理更稳。

**补齐 adata 总投入：约 10 天。**

### 4.3 a-stock-data — 🟠 ~55% 覆盖，主缺口源（7 大类）

atst 已覆盖它的 1~7 层（行情/研报主体/资金面/公告/基础财务/龙虎榜基础/解禁两融大宗股东户数）。**缺失集中在它 3.9/3.10 版本新加的非行情层：**

| 缺失大类 | 具体缺口 | 优先级 |
|---|---|---|
| **① 题材与北向** | 同花顺 `getharden` 题材归因、`hsgtApi` 北向分钟/历史 | P1（G18） |
| **② 事件驱动** | 增减持 / 回购 / 股权质押 / 机构调研重校准 | P2（G19） |
| **③ 宏观与利率** | 收益率曲线 / LPR / Shibor / 社融 / PMI / 全球日历 | P2（G21、G22） |
| **④ 指数体系** | 中证/国证成分权重 / 中证 PE 与股息率 / 申万变迁 | P2（G20、G24） |
| **⑤ 期货扩展** | 日 K 主力连续、A50、上海金 | P2（G23） |
| **⑥ 舆情互动** | 互动易 / 上证 e互动 / 同花顺热榜 | P3（G28、G30） |
| **⑦ 基础补强** | 估值历史 / 上市退市日 / ST 名单 / 筹码 CYQ | P2（G20、G25） |

**另有一处反向优势**：a-stock-data 的 mootdx 在 2026-09 起 K 线命令失效（其 CHANGELOG #52 自曝），atst 走的是 TDX 协议直连 + 2026-09 版本标识/zlib 适配，且 vipdoc 离线兜底，**在通达信这条线上 atst 比 a-stock-data 更稳**。

**补齐 a-stock-data 全部 15 层总投入：约 6~8 周（P0+P1+P2）。**

### 4.4 go-stock — ⚪ 定位差异，不做

go-stock 是 **Go + Wails + NaiveUI 的桌面客户端**（AI 大模型分析 + K 线绘图 + 钉钉报警 + 多轮对话 + 指标选股 + 财务日历 + 热门话题 + 弹幕），数据来源用 Tushare。

- 它是 **atst 的下游消费者形态**，不是竞品数据源。
- atst 的 V4 方案 L6 已明确排除：AI 分析、智能选股、回测引擎、报警推送、桌面 UI、K 线绘图。
- **建议动作**：0 投入。但可以在 `docs/` 加一段"典型集成案例：atst 作为 go-stock 类应用的数据底座"，把 atst 定位讲清楚——这对项目叙事有帮助。

---

## 5. 全量覆盖路线图

### Phase 0 · 入口解冻（W1，2~5 天，收益最高）

```
G01  Symbol 层放开 HK/US ──→ Market 词汇收口（V4 的 S4）
G04  腾讯 bj + 新浪 bj 北交所
stock-api 代码格式别名（SH510500）
G03  百度 getrelatedblock（申万 + 概念 + 地域）
```

**出口标准**：`Client.call("quotes", symbols=["hk00700","usaapl","bj832000"])` 走统一入口返回结构化数据而非裸 `SymbolError`；百度概念归属可用。

### Phase 1 · 复权三源 + 巨潮（W2~W4）

```
G02  新浪 qianfuquan / houfuquan
G05+G06  巨潮 cninfo Provider（公告 + 两融 + 大宗 + 停复牌 + 复权因子）
        └─ 与东财 dividend_history 组成三源对拍（V4 的 A7）
G12  交易所官方交易日历替换内置静态表
```

**出口标准**：复权因子三源对拍 >0.5% 告警上线；`provenance` 能区分因子来源。

### Phase 2 · 东财 + 交易所硬化（W4~W6）

```
G07  估值字段 enrich=["valuation"]（V4 的 D6）
G08  getTopicZTPool 涨停池 + 涨停原因
G09  龙虎榜席位 TOP5 重抓包
G10  研报 PDF 下载
G11  交易所官方 Provider（上交所两融 + 公司概况；深交所 ShowReport）
G16  ETF 份额
G13  腾讯 ff_ 资金流（跨域备胎）
G14  新浪 gb_ 美股
```

**出口标准**：东财被封时可切 `push2delay`（已就位）+ `ff_` 资金流（新增）；两融/大宗有跨风控域备胎。

### Phase 3 · 同花顺 + 基础补强（W6~W10）

```
G18  同花顺 Provider（getharden + hsgtApi + 一致预期 + 概念成分）
G17  新浪 pricehis 分价表（替代死掉的 0x051A）
G15  集思录可转债全字段
G19  事件驱动补齐（增减持/回购/质押/调研重校准）
G20  估值历史 + 上市退市日 + 申万变迁 + ST 名单
G26  腾讯 getRank（124 行业 + 803 概念排行）
```

**出口标准**：题材归因与北向资金可用；`docs/providers/ths.md` 注明维护成本。

### Phase 4 · 宏观 + 期货 + 舆情（W10~W16，可砍）

```
G21  收益率曲线 / LPR / Shibor / 回购定盘
G22  社融 / PMI / 全球宏观日历
G24  中证/国证成分权重 + 中证 PE 股息率
G23  期货日 K 主力连续 / A50 / 上海金
G25  筹码 CYQ（本地自算）
G28  互动易 / e互动
G29  华尔街见闻 / 新闻联播
```

### 并行贯穿（不做则上述全部埋单）

| 项 | 说明 |
|---|---|
| **V4 的 A1/A2** | 字段 spec 单一事实源 + golden 断言覆盖 9 类陷阱。**端点从 30 涨到 120 时，手工写解析器必崩** |
| **V4 的 A3/A4** | 能力路由表 + 风控域标签。G05/G11/G18 引入 3 个新 Provider 后，"每能力 ≥2 个 risk_domain"必须机器可校验 |
| **V4 的 A7** | 跨源对拍只告警不覆盖——G02/G06 三源完成前，复权结果必须加日级对拍 |
| **V4 的 T1/T2** | capabilities 带状态 + 死名字下线。G01 放开 HK/US 后，`hk`/`us` capability 才应标 `available` |
| **G32 TS SDK** | 走 OpenAPI 生成，与 Phase 0~4 无依赖，可随时插入 |

---

## 6. 验收 Checklist

### Phase 0
- [ ] `Client.call("quotes", symbols=["hk00700","usaapl","bj832000"])` 统一入口可用
- [ ] `SH510500` 大小写不敏感输入别名可用
- [ ] 百度概念归属返回申万一级/二级 + 概念 + 地域三维
- [ ] `runtime.health` 中 hk/us/bj capability 状态为 `available`（不再是 `offline`）

### Phase 1
- [ ] 复权因子三源（东财 dividend_history / 新浪 qianfuquan / 巨潮因子）三向对拍，偏差 >0.5% 告警
- [ ] 巨潮公告沪深北全量可检索
- [ ] 官方交易日历替换内置静态表，`diagnostics.py:364` 的自曝注释可删除

### Phase 2
- [ ] 快照带估值字段（显式 `enrich=["valuation"]`，逐字段 `_provenance`）
- [ ] 涨停池 + 涨停原因题材可用；`ZBPool`/`DTPool` 与 `getTopicZTPool` 口径对齐
- [ ] 龙虎榜买卖席位 TOP5 可用（或明确标注上游失效并下线该 capability）
- [ ] 研报 PDF 可下载（Referer 装配正确）
- [ ] 两融 / 大宗有 ≥2 个不同 risk_domain 的源

### Phase 3
- [ ] 题材归因 `getharden` 可用（若上游失效则显式下线，不留死名字）
- [ ] 北向资金分钟 + 历史可用
- [ ] 集思录可转债 7 个核心字段齐全
- [ ] 新增 3 个 Provider（cninfo / exchange / ths）各有 `docs/providers/<source>.md`，含**实测日期与状态**

### 全局
- [ ] Web Provider 11 → **14**（+cninfo / exchange / ths）
- [ ] Web 端点 ≈当前实测 → **≈120**
- [ ] 每个 capability **≥2 个不同 risk_domain** 的源（机器可校验）
- [ ] 9 类字段陷阱各 ≥1 条 golden 断言
- [ ] `atst doctor` 输出全端点健康度
- [ ] README 能力矩阵由 capabilities 状态**自动生成**，禁止手抄
- [ ] a-stock-data 的 15 层中，**不做**的层（应用/AI）在 README 明确列出，不留模糊地带

---

## 7. 风险与合规（在 V4 基础上新增 3 条）

| # | 风险 | 应对 |
|---|---|---|
| **N1** | **北交所行情授权**：`bse.cn` Level-1 行情明确需授权（V4 已列为唯一需法务确认项） | G04 只走腾讯/新浪前缀兜底，不直连 bse.cn；README 与 SECURITY.md 明示"个人研究用途" |
| **N2** | **同花顺维护成本高**：Cookie + 限流 + 字段级 null（其 V3.0 已因反爬 401 把行业排名换成东财） | G18 放 Phase 3 之后；`zhangfu/huanshou/close` 等字段 **null 保留 null，绝不填 0**；接入前先跑 30 天稳定性观察 |
| **N3** | **G01 放开 HK/US 可能引入静默错误**：腾讯 `hk`/`us` 与 A 股的字段布局不同（`qt.gtimg.cn` 不同前缀是不同字段集），Symbol 层放开后若解析器仍按 A 股 88 字段切，会**静默产出错数据** | 必须先写字段 spec（V4 的 A1）+ golden 样本，再放 Symbol 层。顺序反了就是 P0 事故 |
| 4 | 巨潮/交易所官方接口的请求频次合规 | G05/G11 统一走 per-domain 并发闸门（V4 的 A6），不与东财系共用配额 |
| 5 | 东财系单点（V4 已列） | G13 `ff_` + G11 交易所官方为必做备胎 |

---

## 8. 与 V4 方案的合并关系

| V4 编号 | 本文对应 | 状态修正 |
|---|---|---|
| D1 腾讯 hk/us/hf_/fx_ | G01 | **适配器已就绪，工作量 1 周 → 2~3 天**；真正缺的是 Symbol 层放行 |
| D9 新浪 bj/hk/gb_ | G04 + G14 | hk 已接；bj 与 gb_ 未接 |
| D11 新浪复权因子 | G02 | 未变，P0 |
| D6 东财估值 enrich | G07 | 未变 |
| D19 push2delay | — | **已完成，可从待办划掉** |
| D18 涨停池 | G08 | 部分完成（ZBPool/DTPool 已有），缺 getTopicZTPool |
| N1 巨潮 | G05 + G06 | 未变，P1 |
| N2 交易所官方 | G11 + G12 + G16 | 未变，P1 |
| N3 北交所官方 | — | **降级**：G04 用腾讯/新浪前缀即可，不接 bse.cn（合规） |
| N4 同花顺 | G18 | 未变，P2；新增 N2 风险 |
| N5 期货交易所 | G23 + 已有 TDX `0x0207` | 部分已有，降级为 P2 |
| N6/N7 宏观 | G21 + G22 | 未变，P2 |
| N8 HKEX | — | 归入 G18 的北向，不单列 |
| N9/N10/N11 新闻舆情 | G28 + G29 + G30 | 未变，P3 |
| A1/A2/A3/A4/A6/A7/A8 | 并行贯穿 | 未变，仍是硬依赖 |
| T1/T2 透明层 | G01 的后置条件 | 未变 |
| I2 TS SDK | G32 | 未变 |

**本文新增、V4 未列的 9 项**：G01（Symbol 解冻）、G03（百度概念反查）、G10（研报 PDF）、G12（官方交易日历）、G15（转债全字段）、G16（ETF 份额）、G25（筹码 CYQ）、G32（TS SDK 提级）、G33（代理池）。

---

## 9. 最坏情况：只做 Phase 0

**投入 5 天，消除 80% 的"宣称 vs 现实"落差**：

1. 港美股/北交所从统一入口可查（4 个已有适配器被激活）
2. 概念板块反查可用（adata 的核心能力补齐）
3. 复权第二源就位（隐性 bug 高发区的单点消除）
4. `runtime.health` 不再对 HK/US 报 `offline`，能力矩阵开始说真话

---

## 10. 待核实项（在 V4 的 15 项基础上补充）

| # | 项 | 建议核实方式 |
|---|---|---|
| 16 | 腾讯 `hk`/`us` 字段布局与 A 股 88 字段的差异清单 | 真机采样 `qt.gtimg.cn/q=hk00700,usAAPL,sh600519` 三前缀对比，产出字段索引 diff 表 |
| 17 | `SH510500` 大小写别名的输入面范围（仅 Symbol 层，还是 QuerySpec 也要认） | 查 `atst/query.py` 的 symbol 入口链 |
| 18 | 东财 `reportapi` 是否已含行业研报 qType=1（同一端点） | 发一次 `qType=1` 请求验证 |
| 19 | 龙虎榜 `RPT_BILLBOARD_DAILYDETAILSBUY/SELL` 是"已失效"还是"报表名变更" | 抓包 datacenter-web 最新报表名列表 |
| 20 | stock-api `get_klines` 的 `period` 完整枚举（V4 第 15 项，仍未核） | 读其 `src/providers/*.ts` 或 `api-status/*.json` |
| 21 | 百度 `getrelatedblock` 返回的申万分类层级深度（一级 only 还是一级+二级） | 真机采样一次 |
| 22 | 巨潮"证券复权因子"接口的字段口径与调用方式（V4 第 10 项，仍未核） | 抓包 cninfo 复权因子页 |
