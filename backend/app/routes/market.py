from core.config import export_dir
from core.context import AppContext, get_ctx
from core.paths import PathError, validate_dir
from core.quote_fields import pick_last_price
# --- stdlib imports injected by fix_route_imports ---
import asyncio
import logging
import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.routes._common import BrokerError, _call, _need, envelope_ok, err, no_broker, ok
from app.services.market import (
    QUOTES_FILL_SEM,
    ServiceError,
    build_analysis,
    enrich_search_row,
    kline_io,
    normalize_code,
    perf_from_bars,
    perf_stale,
    quote_error,
)
from app.services.market import aggregates as msvc
# V11 §5.3 F：同步状态落库（「上次同步跑成什么样」跨重启可见）
from app.sync.state import STREAM_MARKET_SYNC, STREAM_SYNC_BARS, last_run

# 这两个常量定义在 common.py；此前经 aggregates 隐式 re-export 使用，
# 2026-09-08 改为从源头直接导入，消除「删掉 aggregates 的未使用导入就断」的脆弱耦合。
from app.services.market.common import (
    ETF_LIST_TTL,
    ETF_QUOTE_CAP,
    METRIC_KEYS,
    metric_sources,
)
from datasource.board import classify_board
from datasource.instrument import with_exchange_suffix
from datasource.periods import (
    UnknownPeriodError,
    UnsupportedPeriodError,
    all_periods,
    normalize_period,
    spec,
    to_eltdx_period,
)
from datasource.providers import provider_catalog
from datasource.registry import (
    DataSourceUnavailable,
    MarketDataUnavailable,
    UnsupportedDataSource,
    get_hub,
)
from core.clock import now_iso

log = logging.getLogger("qmt_work.market")

router = APIRouter()


@router.get("/market/providers")
async def market_providers(ctx: AppContext = Depends(get_ctx)):
    """Provider 能力目录：只把真实注册的实现标记为 active。"""
    return ok(provider_catalog.describe())


@router.get("/market/session")
async def market_session(ctx: AppContext = Depends(get_ctx)):
    """当前交易会话快照（交易日 / 盘中阶段 / 数据参照日）。

    为什么单开一个端点而不是复用 ``/health.trading_session``：后者只有
    ``{mode, active, trading_day}`` 三个字段，**说不出「该看哪一天的数据」**。
    非交易日打开行情页时后端照常返回上一交易日的数据（这是对的），但界面
    没有任何地方标注这一点，用户会把周六看到的数字当成「今天的行情」。

    返回：
    ``{today, trading_day, active, phase, last_trading_day, as_of,
       next_trading_day, calendar:{mode,exact}, now}``

    - ``phase``：holiday / pre_open / open / lunch_break / closed，
      比 ``active`` 的布尔值多一层区分（``active=False`` 同时覆盖
      「休市/盘前/午休/已收盘」四种完全不同的用户预期）。
    - ``as_of``：数据参照日，非交易日 = 上一交易日。
    - ``calendar.mode``：exchange（券商真实日历，最准）/ builtin（内置节假日表）。
    """
    from app.sync.calendar import session_snapshot

    try:
        return ok(session_snapshot())
    except Exception as exc:  # noqa: BLE001
        log.warning("session_snapshot 失败：%s", exc)
        return err(500, f"交易会话查询失败：{exc}")

# 兼容别名：既有单测/调用方仍以 routes.market 引用（重构 P1-1 保持行为与符号兼容）
_normalize_code = normalize_code
_enrich_search_row = enrich_search_row
_perf_from_bars = perf_from_bars
_perf_stale = perf_stale


@router.get("/market/search")
async def market_search(q: str, limit: int = 20, include_boards: bool = True, ctx: AppContext = Depends(get_ctx)):
    """标的搜索：代码 / 中文名 / 拼音首字母 / 板块名 模糊匹配（零网络，基于本地缓存）。

    返回 {code:0, data:[{code, name, type, exchange, board, label, match, pinyin?}, ...]}：
    - type: stock/etf/index/board/bond/unknown（画像层分类，前端据此显示徽章）
    - match: exact/code/name/pinyin（命中方式，前端可高亮）
    - include_boards=true（默认）联合板块名称检索，板块条目 type=board
    q 为空时返回空列表。完全离线，不依赖券商连接。
    """
    q = (q or "").strip()
    if not q:
        return ok([])
    limit = max(1, min(int(limit or 20), 50))
    try:
        rows = await get_hub().search_stocks(q, limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("股票搜索失败：%s", exc)
        # ★ 绝不吞成空列表：那会让「检索索引故障」与「确实没有这只票」在界面上
        #   长得一模一样（都是空结果 + code=0），用户会以为自己把代码记错了，
        #   反复重试而不是去查服务状态。两者必须可区分。
        return err(503, f"检索服务不可用：{exc}")
    out = [enrich_search_row(r, q) for r in (rows or []) if r.get("code")]
    # 板块联合检索（仅当无股票结果或结果不足时补充；板块排在股票之后）
    if include_boards and len(out) < limit:
        try:
            matches, _ = await get_hub().search_boards(q, max(1, limit - len(out)))
        except Exception:  # noqa: BLE001
            matches = []
        for m in matches or []:
            if len(out) >= limit:
                break
            code = str(m.get("code", "") or "")
            if not code or any(o["code"] == code for o in out):
                continue
            kind = str(m.get("kind", "") or "")
            out.append({"code": code, "name": m.get("name", ""),
                        "type": "board", "exchange": "板块",
                        "board": "行业板块" if kind == "industry" else "概念板块",
                        "label": "板块", "match": "name"})
    return ok(out)


@router.get("/market/resolve")
async def market_resolve(q: str, limit: int = 8, ctx: AppContext = Depends(get_ctx)):
    """标的解析归一：任意输入（代码/带后缀/前缀式/名称/拼音）→ 标准 QMT 代码 + 候选。

    唯一命中 → resolved=true；多候选 → resolved=false + candidates（前端让用户选择）。
    中文/拼音输入退化为搜索语义。返回
    {q, resolved, code, name, type, exchange, board, label, match, candidates}。
    """
    q = (q or "").strip()
    if not q:
        return err(400, "缺少 q")
    candidates: list = []

    # 1) 代码形式：归一后缀，逐一验证名称表
    for code in normalize_code(q):
        try:
            det = await get_hub().get_instrument_detail(code)
        except Exception:  # noqa: BLE001
            det = None
        name = (det or {}).get("name") or ""
        from datasource.instrument import classify_instrument
        cls = classify_instrument(code, name)
        candidates.append({"code": code, "name": name, "type": cls["type"],
                           "exchange": cls["exchange"], "board": cls["board"],
                           "label": cls["label"], "match": "code"})
    # 代码直接命中唯一候选 → 解析成功
    if len(candidates) == 1 and candidates[0]["name"]:
        c = candidates[0]
        return ok({"q": q, "resolved": True, **c, "candidates": candidates[:limit]})
    # 歧义双候选：取有名称者优先（000001.SH 上证指数 / 000001.SZ 平安银行均合法 → 双候选）

    # 2) 名称/拼音：搜索语义补候选（与 search 同源）
    try:
        rows = await get_hub().search_stocks(q, limit)
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows or []:
        if len(candidates) >= limit:
            break
        code = str(r.get("code", "") or "")
        if not code or any(c["code"] == code for c in candidates):
            continue
        candidates.append(enrich_search_row(r, q))

    resolved = None
    if candidates:
        # 名称精确等于 q 的候选唯一 → 直接解析成功
        exact = [c for c in candidates if c.get("name") and c["name"] == q]
        if len(exact) == 1:
            resolved = exact[0]
        elif len(candidates) == 1 and candidates[0].get("name"):
            resolved = candidates[0]
    if resolved:
        return ok({"q": q, "resolved": True, **resolved,
                   "candidates": candidates[:limit]})
    return ok({"q": q, "resolved": False, "code": "", "name": "",
               "type": "unknown", "exchange": "—", "board": "—", "label": "标的",
               "candidates": candidates[:limit]})


@router.get("/market/analysis")
async def market_analysis(code: str, conn_id: str = "", source: str = "auto", ctx: AppContext = Depends(get_ctx)):
    """标的深度画像：单请求并发聚合 6 维（快照/画像/股本/表现/资金流/估值）。

    编排逻辑见 services/market/analysis.py；此处只做参数校验与信封包装。
    """
    if not (code or "").strip():
        return err(400, "缺少 code")
    try:
        return ok(await build_analysis(get_hub(), code, conn_id=conn_id,
                                       source=source, broker_of=_need))
    except ValueError:
        return err(400, "缺少 code")


@router.get("/market/quote")
async def market_quote(code: str, conn_id: str = "", source: str = "auto", ctx: AppContext = Depends(get_ctx)):
    """实时行情快照（最新价 / 涨跌幅 / 成交量 / 买卖五档 + 合约名称/涨跌停）。

    source:
      - auto（默认）：券商优先，券商未连接或失败时回退 eltdx(TDX 公共行情)
      - broker：仅券商
      - eltdx：仅 TDX 公共行情（无需券商客户端）
    """
    try:
        q = await get_hub().get_quote(code, source=source, conn_id=conn_id or None)
    except UnsupportedDataSource as exc:
        return err(400, str(exc))
    except MarketDataUnavailable:
        return err(*quote_error(code, source))
    if not q or not isinstance(q, dict) or q.get("last") is None:
        # 彻底无可用源（auto 链全失败 / 显式源缺数据）：绝不静默返回 null，
        # 让前端能据此显示明确的「行情不可用」而非误判为“连接正常但无数据”。
        return err(*quote_error(code, source))
    # 必须用 ok() 包裹：quote dict 自带 "code" 字段（股票代码），若裸返回，
    # 前端 _req 会误判 `j.code !== 0` 直接抛错 → 个股/行情分析全部空数据。
    return ok(q)


@router.post("/market/quotes")
async def market_quotes(body: dict, ctx: AppContext = Depends(get_ctx)):
    """批量行情快照（报价牌 / 综合排名命脉）。

    优先返回 SyncEngine.latest_quotes 缓存中已订阅的实时快照（零新增网络调用）；
    缓存缺失的代码经 DataSourceManager.get_quote 尽力补齐（单只 5s 超时、整体 gather，
    失败静默跳过），保证报价牌/排名首屏即有数据，不伪造。

    参数（body JSON）：codes(必填) / source / conn_id。
    返回 {items:[归一化 quote...], served, requested}。
    """
    codes = [str(c).strip() for c in (body.get("codes") or []) if str(c).strip()]
    if not codes:
        return ok({"items": []})
    source = str(body.get("source") or "auto")
    conn_id = body.get("conn_id") or None
    m = get_hub()
    se = getattr(ctx, "sync_engine", None)
    cache = getattr(se, "latest_quotes", None) or {}
    items: list = []
    missing: list = []
    for c in codes:
        q = cache.get(c.upper()) or cache.get(c)
        if q and isinstance(q, dict):
            items.append({**q, "source": q.get("source") or "cache"})
        else:
            missing.append(c)
    if missing:
        # ★ V17 补丁：补齐打源优先用 **批量** `get_quotes`（绕过 N 只串行节流）。
        #   tencent 源已重写为真正批量（1 次 HTTP 拉全部），其它源走基类默认（gather 单只）。
        #   这样报价牌 4 只 = 1 次 HTTP，不再被 SyncEngine 同步任务的全局节流锁串成 N 次
        #   请求 —— 实测冷启动时整批 5s 超时返空的根因。
        async def _fill_one(c):
            try:
                async with QUOTES_FILL_SEM:
                    return await asyncio.wait_for(
                        m.get_quote(c, source=source, conn_id=conn_id), timeout=8)
            except Exception:  # noqa: BLE001
                return None

        async def _fill_batch(cs):
            """批量补齐 —— 优先走 m.get_quotes（基类默认 gather 单只；tencent/sina 重写为单 HTTP）。"""
            try:
                async with QUOTES_FILL_SEM:
                    res_map = await asyncio.wait_for(
                        m.get_quotes(cs, source=source, conn_id=conn_id), timeout=10)
            except Exception:  # noqa: BLE001
                return [None] * len(cs)
            # m.get_quotes 在 plugins 链可能不支持（callables 不同），逐个回退单只
            if not isinstance(res_map, dict):
                return await asyncio.gather(*[_fill_one(c) for c in cs])
            return [res_map.get(c) for c in cs]

        res = await _fill_batch(missing)
        for _c, q in zip(missing, res):
            if q and isinstance(q, dict):
                items.append(q)
    _normalize_quotes(items)
    return ok({"items": items, "served": len(items), "requested": len(codes)})


def _normalize_quotes(items: list) -> list:
    """行情列表的**统一出口**：补名称 + 归一化最新价。

    ★ 2026-09-20 实测：本端点**优先读 SyncEngine 的实时快照缓存**
    （`latest_quotes`，券商 WS 推来的原始快照），这条路径**不经过**
    `DataSourceManager._merge_quote`，所以那里的名称兜底对它无效。
    结果是：个股有名字（快照里带），**指数一律没有** —— 自选股 / 报价牌里
    沪深300、创业板指显示成一串 `000300.SH`，与顶部指数条（走 overview，
    已修好）自相矛盾。

    在这里统一补一次，比让每条前端链路各兜一遍更可靠（新增消费方自动受益）。
    查不到就置 `""`：前端 `format.ts::namePair` 会渲染成代码占位 ——
    显示效果与「拿代码当名称」一致，但语义诚实（不再污染 `_is_st` 之类判据）。

    ★★ 价格归一化（2026-09-20 补）：本端点是**唯一**对外返回行情列表的出口，
    但两条来源的键名**不一致** ——
      - 券商 WS 快照缓存：给 `price`
      - eltdx / 打源补齐：给 `last`（见 `marketApi.quote` 注释里那条老警告）
    前端 `Quote` 类型只有 `price`，直接读 `last` 会得到一堆 `undefined`
    （「订阅到了但没数字」的老坑）。这里用 `core.quote_fields.pick_last_price`
    （键序唯一入口）统一成 `price`，前端契约就此唯一。

    ⚠️ 取不到价就**删掉** `price` 键，绝不写 0 —— 前端 `fmtPrice(undefined)`
    才是诚实的 `--`，`0.00` 会被读成「这只票跌到 0 了」。
    """
    from datasource.eltdx_utils import lookup_name

    for it in items or []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip().upper()

        # ① 名称：查不到 / 「名 == 代码」都置空（前端回退到代码显示）
        nm = str(it.get("name") or "").strip()
        if nm.upper() == code:      # 「名称就是代码」= 没有名称
            nm = ""
        if not nm and code:
            nm = lookup_name(code)
        it["name"] = nm

        # ② 最新价：统一为契约名 price（键序唯一入口 core.quote_fields）
        px = pick_last_price(it)
        if px is None:
            it.pop("price", None)
        else:
            it["price"] = px
    return items


#: 「行情快照派生字段」—— 只有**公开行情源**（腾讯/新浪这类）能从一条快照里给出，
#: 本地 TDX 与券商详情接口都不提供。
#:
#: ★ 2026-09-21：清单已上移到 `app/services/market/common.py::METRIC_KEYS`
#:   （单一真源）。原因是 `/market/analysis` 的估值维度也要按同一份清单去找源
#:   —— 它此前只认券商财务接口，于是同一份 PE/PB 在 stock-info 有、在 analysis
#:   却说「无数据」。两处各留一份清单必然再次分叉，故此处只保留别名。
_METRIC_KEYS = METRIC_KEYS
_metric_sources = metric_sources


@router.get("/market/stock-info")
async def market_stock_info(code: str, conn_id: str = "", source: str = "auto", ctx: AppContext = Depends(get_ctx)):
    """股票基本信息：名称 / 板块 / 交易所 / 涨跌停 / 昨收 + 行情派生字段（供右侧面板）。

    source: auto（券商优先，失败回退 eltdx）/ broker / eltdx
    """
    # 名称表与券商接口均以带后缀代码为键，裸代码会查不到中文名（界面只剩数字）
    code = with_exchange_suffix((code or "").strip().upper())
    board = classify_board(code)
    # ★ 本地名称兜底：券商/TDX 未回名称时**去查名称表**，绝不拿代码冒充
    #   （2026-09-20 实测：指数名称表里没有 ⇒ 此前恒显示 `000300.SH`）。
    #   查不到返回 ""，前端 `format.ts::namePair` 会渲染成代码占位。
    from datasource.eltdx_utils import lookup_name as _lookup_name
    info = {
        "code": code,
        "name": _lookup_name(code),
        "exchange": board.get("exchange"),
        "board": board.get("board"),
        "high_limit": None,
        "low_limit": None,
        "pre_close": None,
        "industry": "",
        "concepts": [],
        # ---- 扩展字段（2026-09-21）----
        # 腾讯快照里**本来就有**市值 / PE / PB / 换手 / 振幅 / 均价 / 量比，
        # 解析出来透出即可，**零额外请求**。其它源（eltdx / broker）拿不到就保持
        # None —— 前端一律渲染 `--`，绝不用 0 或占位数字冒充。
        "open": None,
        "high": None,
        "low": None,
        "avg_price": None,
        "amplitude": None,
        "turnover_rate": None,
        "volume_ratio": None,
        "pe_ttm": None,
        "pb": None,
        "circ_mv": None,
        "total_mv": None,
        "amount": None,
        # 行情派生字段实际来自哪个源（详情源提供不了时由公开源补齐；没补上则不出现）
        "metrics_source": None,
    }

    try:
        det = await get_hub().get_instrument_detail(code, source=source, conn_id=conn_id or None)
    except UnsupportedDataSource as exc:
        return err(400, str(exc))
    except MarketDataUnavailable:
        # 全部源不可用：板块按代码前缀推断，绝不伪造数值。
        info["note"] = "未连接券商且 TDX 行情源不可用，板块按代码前缀推断"
        return ok(info)

    # ★ 「源都不可用」与「源都在、但都返回空壳」是两回事：前者抛 MarketDataUnavailable
    #   （上面那条分支），后者会走到这里拿到 **None**（`get_instrument_detail` 全链无内容时
    #   `return last`，而 last 仍是 None）。典型场景就是**全新安装的客户端**：没连券商、
    #   本地也没下载过 TDX 数据。
    #   实测（2026-09-21 打包态）：此前直接 `det.get(...)` ⇒ AttributeError ⇒ **HTTP 500**，
    #   而右侧「基本信息」恰恰是要「快速查看」的面板 —— 一点开就报错。
    #   这里降级为「画像字段留空 + 诚实说明」，并**继续走下面的行情补齐**：
    #   公开行情源与本地 TDX 数据无关，仍可能拿到实时数值，不该一并放弃。
    if not isinstance(det, dict):
        info["note"] = "未取到本地画像（未连券商、本地也无该标的资料），仅展示行情字段"
        det = {}

    info["name"] = det.get("name") or _lookup_name(code)
    info["exchange"] = det.get("exchange") or info["exchange"]
    info["high_limit"] = det.get("high_limit")
    info["low_limit"] = det.get("low_limit")
    info["pre_close"] = det.get("pre_close")
    info["industry"] = det.get("industry") or ""
    info["concepts"] = det.get("concepts") or []
    info["source"] = det.get("source")
    # 扩展字段：只在**真的拿到**时覆盖（det 里缺键或值为 None 就保持 None → 前端 `--`）
    for key in _METRIC_KEYS:
        val = det.get(key)
        if val is not None:
            info[key] = val

    # ---- 缺口补齐：换一个**声明了这些字段**的源再取一次行情 ----
    #
    # ⚠️⚠️ 为什么必须有这一步（2026-09-21 打包态实测）：
    #   详情源优先链是 broker → eltdx，而**本地 TDX 与券商都不提供**市值 / PE / PB /
    #   换手 / 振幅 / 均价 / 量比（它们是「行情快照派生字段」，只有公开行情源有）。
    #   打包版自带 eltdx ⇒ 实测 `source=eltdx`，于是这 12 个字段**全是 null**，
    #   界面上就是一整列 `--` —— 功能等于没做。
    #
    # 只对**确有缺口**的字段补，且按源依次尝试；补齐即停。任何异常都吞掉：
    # 补不上就保持 None（前端 `--`），绝不因此让整个接口失败。
    missing = [k for k in _METRIC_KEYS if info.get(k) is None]
    if missing:
        for name in _metric_sources():
            if not missing:
                break
            try:
                q = await get_hub().get_quote(code, source=name, conn_id=conn_id or None)
            except Exception:  # noqa: BLE001 — 补齐是尽力而为，失败不影响主数据
                q = None
            if not isinstance(q, dict):
                continue
            for key in list(missing):
                if q.get(key) is not None:
                    info[key] = q[key]
                    missing.remove(key)
            if info.get("metrics_source") is None:
                info["metrics_source"] = name
    return ok(info)

@router.get("/market/sources")
async def market_sources(ctx: AppContext = Depends(get_ctx)):
    """列出已注册行情数据源及其可用性（供前端「数据源」选择 / 健康展示）。

    返回 {sources:[name,...], auto_chain:[...], health:{name:{available,note}}, active:当前auto首源}。
    """
    m = get_hub()
    h = await m.health()
    active = next((n for n in m._auto_chain if h.get(n, {}).get("available")), None)
    return ok({
        "sources": m.list_sources(),
        "source_capabilities": m.describe_sources(),
        "auto_chain": m._auto_chain,
        "health": h,
        "active": active,
    })


@router.get("/market/periods")
async def market_periods(ctx: AppContext = Depends(get_ctx)):
    """可用周期清单（契约驱动 UI 的数据源）。

    前端周期条据此渲染，并对 supported=false 的周期置灰 + tooltip 显示 reason。
    这样后端新增/下线周期时前端自动跟随，杜绝「点了出别的周期」的静默错误（P0-1）。
    """
    return ok({"periods": all_periods()})


def _kline_empty_note(code: str, period: str, src: str) -> str:
    """「所有源都正常应答、但都没有该标的的 K 线」时的**准确**说明。

    ⚠️ 与「源不可用」（503，另一条分支）必须分开说：这两种空的**成因与处置完全不同**。
    这里只陈述本请求自己确知的事实（哪个源应答了、本地仓也没有），不去读
    `last_failure_trace()` —— 那是全局共享的「最近一次」记录，并发下可能来自别的请求。
    """
    try:
        label = spec(period).label
    except Exception:  # noqa: BLE001 文案拼接失败不该影响响应
        label = period
    return (f"行情源（{src}）已正常应答，但没有 {code} 的{label} K 线，"
            f"本地数据仓也没有该标的。可能原因：新上市 / 长期停牌 / 该周期无成交，"
            f"或当前数据源不覆盖该标的。")


@router.get("/market/kline")
async def market_kline(code: str, period: str = "1d", count: int = 250,
                       conn_id: str = "", force: bool = False, source: str = "auto",
                       adj: str = "", ctx: AppContext = Depends(get_ctx)):
    """历史 K 线（C1 本地缓存优先；source: auto=券商优先回退eltdx / broker / eltdx）。
    adj: ''=不复权 / qfq=前复权 / hfq=后复权。显式复权时**优先**走 eltdx（原生支持复权、
    少一次券商 RPC）；eltdx 不可用或返回空则继续走券商 —— 券商侧经 `dividend_type`
    参数化同样支持复权（V11 R14 起 adjust 已真正透传，此前漏传导致降级时口径静默
    变成不复权却仍标「前复权」）。本响应的 `adjust` 是**实际取数口径**，前端角标应以它为准。

    周期在入口即校验：未知周期返回明确错误，不支持的周期（如季线）返回原因。
    ⚠️ 绝不能静默降级为日线 —— 那会让用户看到错误周期的数据却不自知（P0-1）。
    """
    try:
        period = normalize_period(period)
    except UnknownPeriodError as exc:
        return err(400, str(exc))
    # 分时不是 K 线周期：走独立端点 /market/minutes。这里必须显式拒绝并给出
    # 去向，否则会「通过校验 → 券商返回空 → 静默 0 根」，前端图表空白却无报错。
    if spec(period).kind == "tick":
        return err(400, "分时数据请使用 /market/minutes 端点（K 线端点不支持 tick 周期）")
    try:
        if not spec(period).supported:
            return err(400, f"周期 {spec(period).label} 暂不可用：{spec(period).reason}")
    except UnsupportedPeriodError as exc:  # pragma: no cover - spec() 已归一化
        return err(400, str(exc))

    from tools import fetch_kline_cached
    try:
        res = await fetch_kline_cached(code, period, count,
                                       broker_id=conn_id or None, force=force,
                                       source=source,
                                       adjust=adj or None)
    except UnsupportedDataSource as exc:
        return err(400, str(exc))
    except (BrokerError, DataSourceUnavailable) as exc:
        return err(503, str(exc))
    bars = res.get("bars") or []
    # ★★ 空 bars 一律先试本地数据仓兜底（2026-09-21 修 A8，用户报「从行情工作台
    #    打开时 K 线图无法正常展示」的第二个根因）。
    #
    # 原条件是 `not bars and not res.get("source")` —— 只有「一个源都没应答」时才兜底。
    # 而实测最常见的形态恰恰是**源都在、都正常应答、但都返回空壳**：
    #     GET /market/kline?code=000001.SH  →  {"source":"broker", "count":0, "bars":[]}
    # 此时 `res["source"]` 是 "broker"（真值）⇒ 兜底被整段跳过 ⇒ 直接 200 + 0 根 +
    # `note: null` ⇒ 前端 K 线图留一块白板，**没有任何文字说明为什么**。
    #
    # 「源都不可用」（抛异常 / 超时）与「源都在但都返回空壳」（正常返回 0 根）是
    # 两种**不同的空**，只接住前者就会在后者上静默。这里让两者都接住：
    # 先兜底本地，本地也没有再按成因分叉文案。
    if not bars:
        from datasource.degrade import envelope, local_bars
        # ⚠️ 必须透传 count：local_bars 默认 limit=500，不透传会让「请求 count=30」
        # 在远程源不可用时返回最多 500 根（实测 320 根 = 本地全量），
        # 前端图表与指标计算随之失真。降级路径与主路径必须给出同一根数契约。
        dres = local_bars(code, period=period, adjust=adj or "", limit=count)
        if dres is not None:
            return ok(envelope(
                {"code": code, "period": period, "count": len(dres.results),
                 "cached_at": None, "note": None, "adjust": adj or "",
                 "bars": dres.results}, dres))
        src = res.get("source")
        if src:
            # 有源应答过 ⇒ 是「空壳」而不是「不可用」。刻意返回 **200 + count:0 + note**
            # 而不是 503，理由在**用户可见性**上：
            #   前端 KLineChart 的 `.catch()` 分支（网络/错误码）只能显示一句通用猜测
            #   「暂无 K 线数据（可能停牌或该周期无成交）」；而 200 + note 会走
            #   `meta.note` 分支（KLineChart.tsx:377）把**真实原因**写在画布上。
            #   错误归因必须落到用户看得见的地方，不能退化成一句猜测。
            #
            # ⚠️ 这里**不用** `msvc._unavailable()`，两个原因：
            #   ① 它的框架是「获取失败」—— 而此刻所有源都**成功应答**了，只是没有数据。
            #      把「都成功了但没数据」说成「获取失败」会把排查方向带偏（本项目
            #      已经因为这类措辞吃过亏：把「不支持」说成「网络坏了」）。
            #   ② 它读的是 `get_hub().last_failure_trace()`，那是一份**全局共享的
            #      「最近一次」**记录；而本端点在 fetch_kline_cached 内部会打多次源调用，
            #      并发下这份记录完全可能来自**另一个请求**的链路 ⇒ 文案会张冠李戴。
            #      所以这里只陈述本请求**自己确知**的事实。
            return ok({"code": code, "period": period, "count": 0,
                       "source": src, "cached_at": None, "as_of": None,
                       "note": _kline_empty_note(code, period, src), "adjust": adj or "",
                       "stale": False, "bars": []})
        return err(503, f"K 线获取失败：{code} 券商不可用、TDX 行情源无数据且本地数据仓为空，请连接券商或检查网络。")
    # ⚠️ stale 一致性（曾经出现 source=cache_stale 却 stale:false 误导用户）：
    # 当缓存层回退供给「过期缓存」时（kline_cache.get_or_fetch 返回 source=cache_stale），
    # 必须同步把 stale 置 True，并透传 as_of（=cached_at 数据截至时间），绝不静默冒充最新。
    src = res.get("source")
    stale = src == "cache_stale"
    return ok({"code": code, "period": period, "count": len(bars),
               "source": src, "cached_at": res.get("cached_at"),
               "as_of": res.get("cached_at") if stale else None,
               "note": res.get("note"), "adjust": adj or "",
               "stale": stale,
               "bars": bars})

@router.get("/market/minutes")
async def market_minutes(code: str, date: str = "", source: str = "auto", ctx: AppContext = Depends(get_ctx)):
    """当日分时（1 分钟）曲线：价格线 + 均价线 + 分钟量，含昨收基准。

    date 为空取最新交易日分时；格式 YYYY-MM-DD 取历史分时。
    数据源：TDX 公共行情（eltdx）；券商 SDK 无分时接口。全源无数据返回 503（零 mock）。
    返回 {code, trading_date, pre_close, open_price, points:[{t,price,avg,volume}]}。
    """
    try:
        res = await get_hub().get_minutes(code, trading_date=date or None, source=source)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"分时获取失败：{code}（{exc}）")
    if not res or not res.get("points"):
        return err(503, f"分时获取失败：{code} 暂无 TDX 分时数据（非交易时段或网络不可用）。")
    return ok(res)


@router.get("/market/limitup")
async def market_limitup(sector: str = "沪深A股", min_pct: float = 9.5,
                         only_limit: bool = True, limit: int = 200,
                         sort: str = "change", ctx: AppContext = Depends(get_ctx)):
    """涨停板：扫描板块内最新行情，列出涨停（或接近涨停）个股及最新数据，便于快速选股交易。"""
    b = _need()
    if b is None:
        return no_broker()
    try:
        from engines.limitup import scan_limit_up
        rows = await scan_limit_up(b, sector, min_pct, only_limit, limit, sort)
    except BrokerError as exc:
        return err(503, str(exc))
    return ok({"sector": sector, "count": len(rows), "rows": rows})

@router.get("/market/breadth")
async def market_breadth(ctx: AppContext = Depends(get_ctx)):
    """市场广度统计：全市场/板块/主要指数涨跌停家数。"""
    b = _need()
    if b is None:
        return no_broker()
    try:
        from engines.limitup import market_breadth as _mb
        return ok(await _mb(b))
    except BrokerError as exc:
        return err(503, str(exc))


# ---------- 多维行情：指数 / 板块 / ETF / 资金流（编排见 services/market/aggregates.py） ----------

@router.get("/market/indices")
async def market_indices(codes: str = "", source: str = "auto", ttl: int = 3,
                         spark: bool = False, spark_days: int = 20, ctx: AppContext = Depends(get_ctx)):
    """主要指数聚合快照（顶部指数条数据源）。

    并发拉取，单只失败返回 null 并计入 errors，不因一只失败拖垮整条。
    codes: 逗号分隔，默认 DEFAULT_INDICES。ttl: 秒级缓存（0=不缓存）。
    spark=true 时附带近 spark_days 日收盘价序列（真实 K 线，kind=index）。
    """
    try:
        return ok(await msvc.indices_snapshot(codes, source=source, ttl=ttl,
                                              spark=spark, spark_days=spark_days))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/boards")
async def market_boards(kind: str = "industry", sort_by: str = "pct",
                        limit: int = 50, source: str = "auto", ttl: int = 10, ctx: AppContext = Depends(get_ctx)):
    """板块榜单（行业 881xxx / 概念 880xxx / 统计类 stat）。"""
    try:
        return ok(await msvc.boards(kind, sort_by=sort_by, limit=limit,
                                    source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/constituents")
async def market_board_constituents(code: str, limit: int = 100, page: int = 0,
                                    source: str = "auto", ttl: int = 15, ctx: AppContext = Depends(get_ctx)):
    """板块成分股（真实板块成分，非全市场过滤）。"""
    try:
        return ok(await msvc.board_constituents(code, limit=limit, page=page,
                                                source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/lookup")
async def market_board_lookup(name: str, limit: int = 8, source: str = "auto",
                              ttl: int = 600, ctx: AppContext = Depends(get_ctx)):
    """板块名称 → 代码匹配（P1-8 深链稳化）。"""
    try:
        return ok(await msvc.board_lookup(name, limit=limit, source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/kline")
async def market_board_kline(code: str, period: str = "1d", count: int = 60,
                             source: str = "auto", ttl: int = 60, ctx: AppContext = Depends(get_ctx)):
    """板块 / 指数 K 线（内部按 kind=index 取，避免 ProtocolError）。"""
    try:
        # 未知周期 UnknownPeriodError / 数据源不支持 UnsupportedPeriodError，
        # 二者均为 ValueError 子类；绝不静默降级为日线。
        to_eltdx_period(period)
    except ValueError as exc:
        return err(400, str(exc))
    try:
        return ok(await msvc.board_kline(code, period=period, count=count,
                                         source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/etfs")
async def market_etfs(limit: int = 0, with_quote: bool = False,
                      quote_limit: int = ETF_QUOTE_CAP, source: str = "auto",
                      ttl: int = ETF_LIST_TTL, ctx: AppContext = Depends(get_ctx)):
    """ETF 全市场清单（代码段 51/56/58/15/16）。"""
    try:
        return ok(await msvc.etfs(limit, with_quote=with_quote,
                                  quote_limit=quote_limit, source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/moneyflow")
async def market_moneyflow(code: str, source: str = "auto", ctx: AppContext = Depends(get_ctx)):
    """个股资金流（真实口径：快照内外盘 + 分钟级买卖力道 + 量比）。"""
    try:
        return ok(await msvc.moneyflow(code, source=source))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/capital")
async def market_capital(codes: str, source: str = "auto", ttl: int = 300, ctx: AppContext = Depends(get_ctx)):
    """批量流通股本 + 涨跌停价（换手率与涨跌停展示的真实口径来源）。"""
    try:
        return ok(await msvc.capital(codes, source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/board/moneyflow")
async def market_board_moneyflow(code: str, source: str = "auto",
                                 top_n: int = 30, ttl: int = 30, ctx: AppContext = Depends(get_ctx)):
    """板块资金流：聚合成分股当日主力净流入（外盘-内盘），真实口径。"""
    try:
        return ok(await msvc.board_moneyflow(code, source=source, top_n=top_n, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/rotation")
async def market_rotation(days: int = 5, kind: str = "industry",
                          top_n: int = 40, source: str = "auto", ctx: AppContext = Depends(get_ctx)):
    """板块轮动：取板块榜 topN（按 |涨跌幅|），各取日K 计算每日%chg，返回矩阵供热力图。"""
    try:
        return ok(await msvc.rotation(days, kind=kind, top_n=top_n, source=source))
    except ServiceError as exc:
        return err(exc.code, exc.message)


@router.get("/market/overview")
async def market_overview(source: str = "auto", ttl: int = 60, ctx: AppContext = Depends(get_ctx)):
    """市场概览（E3）：统计类板块真实家数 + 主要指数快照 + 宽度趋势 + 两市成交额。

    ttl 默认 60s（原 10s）：本端点三块取数合计冷启动可达数秒、稳态 2.4~4s（实测见
    `services/market/aggregates.py::overview`），而返回内容里唯一会「过期」的指数
    最新价由前端 `useLiveQuotes` 实时叠加，不依赖本快照 ⇒ 短 ttl 只是让用户反复白等。
    调用方仍可显式传 `ttl=0` 强制不走缓存。
    """
    try:
        return ok(await msvc.overview(source=source, ttl=ttl))
    except ServiceError as exc:
        return err(exc.code, exc.message)


# ===================== G3 资金流落库 / 回放 / 自动采集 =====================
class _MoneyflowSnapshotReq(BaseModel):
    codes: list = []
    board: str = ""


@router.post("/market/moneyflow/snapshot")
async def market_moneyflow_snapshot(body: _MoneyflowSnapshotReq, ctx: AppContext = Depends(get_ctx)):
    """采集个股/板块资金流快照落库（G3）。body: {codes:[...]} 或 {board:"881319.SH"}。
    返回 {inserted, codes, ts}。"""
    codes = [str(c).strip() for c in (body.codes or []) if c][:300]
    if body.board and not codes:
        try:
            res, _ = await get_hub().get_board_constituents(body.board, 200, 0, source="auto")
            if res and res.get("items"):
                codes = [c["code"] for c in res["items"] if c.get("code")][:300]
        except Exception as exc:  # noqa: BLE001
            return err(503, f"板块成分获取失败：{exc}")
    if not codes:
        return err(400, "缺少 codes 或 board")
    inserted = await kline_io.snapshot_codes(codes)
    return ok({"inserted": inserted, "codes": codes,
               "ts": now_iso()})


@router.get("/market/moneyflow/replay")
async def market_moneyflow_replay(code: str, date: str = "", limit: int = 500, ctx: AppContext = Depends(get_ctx)):
    """资金流回放：取 code 的历史快照序列（按 ts 升序）。date=YYYY-MM-DD 可选过滤某日。"""
    if not code:
        return err(400, "缺少 code")
    from core.db import get_db
    return ok(kline_io.moneyflow_replay(get_db(), code, date=date, limit=limit))


# start_moneyflow_collector 由 services/market/kline_io 提供（main.py lifespan 调用）。
start_moneyflow_collector = kline_io.start_moneyflow_collector


@router.get("/market/kline/sync-status")
async def kline_sync_status(ctx: AppContext = Depends(get_ctx)):
    """行情缓存定时更新状态（开关/触发时间/最近一次运行/冷热分层），供前端展示与配置。

    「每日 16:00 自动下载 K 线」的观测面：``enabled`` + ``sync_time`` 说明会不会跑、
    几点跑；``last_run`` 说明今天跑没跑、跑了几只；``hot`` 说明冷热怎么分的。
    前端据此能如实告诉用户「已同步 / 待同步 / 未启用」，而不是只显示一个开关。
    """
    ms = getattr(ctx, "market_sync", None)
    if ms is None:
        # ★ 同步器没装配，**不等于**「从来没有同步过」。两条流的落库记录是 DB 数据，
        #   与调度器是否启动无关；在此丢掉它们，恰好会让用户在「出故障」时失去
        #   唯一的线索（上次跑到哪、成没成）。
        return ok({"initialized": False,
                   "last_run_persisted": last_run(STREAM_MARKET_SYNC),
                   "last_bars_run": last_run(STREAM_SYNC_BARS)})
    rc = getattr(ctx, "runtime_config", None)
    kc = getattr(ctx, "kline_cache", None)
    hot = {}
    if kc is not None:
        try:
            st = kc.stats()
            hot = {"hot_days": st.get("hot_days"), "hot_cutoff": st.get("hot_cutoff"),
                   "hot_rows": st.get("hot_rows"), "cold_rows": st.get("archive_rows"),
                   "cold_enabled": st.get("cold_enabled"),
                   "cold_path": st.get("cold_path")}
        except Exception:  # noqa: BLE001 状态查询失败不该让整个端点 500
            hot = {}
    info = {
        "initialized": True,
        "enabled": ms.enabled,
        "sync_time": ms.sync_time,
        # runtime_config 可写标记（put /config/runtime 用同一 domain key）
        "keys": {
            "enabled": "market.sync.enabled",
            "sync_time": "market.sync.time",
            "hot_days": "market.hot_days",
        },
        "last_run": getattr(ctx, "_market_sync_last", None),
        # ★ 落库版的上次运行（V11 §5.3 F）。内存里的 last_run **重启即丢**，
        #   而用户判断「今天的数据到底同步了没有」正是在重启之后。
        #   两者并存：内存版更实时（刚跑完立刻可见），落库版跨重启可查。
        "last_run_persisted": last_run(STREAM_MARKET_SYNC),
        # ★★ 这是**另一条流**：``market.sync`` 是热窗口刷新（只把最近若干天的
        #   热数据回源一遍），而 ``sync.bars`` 才是「全市场日线落库」那条路。
        #   两者的 detail 结构完全不同：只有 sync.bars 才有
        #   ``sync_mode`` / ``paged`` / ``as_of_min`` / ``skipped_complete`` /
        #   ``stale`` / ``as_of_max``。
        #   ⚠️ 界面若把全量回补的字段从 ``last_run_persisted`` 读，会**永远读不到**
        #   （热刷新的 detail 里根本没有这些键），表现为「全量回补按钮点了没反应」。
        #   所以这里必须把两条流都如实给出，由界面各自取用。
        "last_bars_run": last_run(STREAM_SYNC_BARS),
        "hot": hot,
        "config": rc.all().get("market.sync.enabled") if rc else None,
    }
    return ok(info)


@router.get("/market/coverage")
async def market_coverage(period: str = "1d", adjust: str = "qfq",
                          lookback_days: int = 30, ctx: AppContext = Depends(get_ctx)):
    """本地日线的**覆盖度报表**：每个交易日在库多少只 + 各数据源占比。

    ★ 为什么要有这个端点：「上次同步 ok=5221」只说明**调用**成功了，不说明
    数据**新到哪天** —— 源链「第一个非空即返回」时，券商本地历史停在一年前
    也照样 ok（详见 docs 硬约束清单「非空≠够新」）。用户唯一能自查的办法就是
    看「最近 N 个交易日，每天在库多少只」。因此这里如实返回 ``per_day``，
    **不做任何填充**：某天缺失就是缺失，不能补成 0 也不能省略。
    """
    from datasource.quality import coverage_report
    try:
        rep = await asyncio.to_thread(
            coverage_report, ctx.db, period=period, adjust=adjust,
            lookback_days=max(1, min(int(lookback_days or 30), 250)))
    except Exception as exc:  # noqa: BLE001 — 报表查询失败要说清原因，不返空表冒充「无数据」
        return err(500, f"覆盖率报表查询失败：{exc}")
    per_day = list(rep.get("per_day") or [])
    # 回显查询口径：用户看到的「30 天 / qfq」必须是**实际用的**那一组，
    # 而不是他自己以为的那一组（参数被兜底过就说明不了问题）。
    rep["period"] = period
    rep["adjust"] = adjust
    rep["latest_day"] = per_day[0]["dt"] if per_day else ""
    rep["latest_codes"] = per_day[0]["codes"] if per_day else 0
    rep["days_with_data"] = len(per_day)
    # 同步状态（落库）一并返回：覆盖度与「上次同步跑成什么样」必须放在一起看 ——
    # 只给覆盖度，用户不知道是「没跑」还是「跑了但源没数据」。
    rep["sync"] = last_run(STREAM_SYNC_BARS)
    return ok(rep)


@router.get("/market/datasets/snapshots")
async def dataset_snapshots(dataset_id: str = "cn_equity_daily", limit: int = 20, ctx: AppContext = Depends(get_ctx)):
    """查询历史数据集快照；只返回已落库的版本/校验/质量元数据。"""
    from core.db import get_db
    rows = get_db().query(
        "SELECT id,dataset_id,version,provider_id,batch_id,as_of,coverage_start,"
        "coverage_end,row_count,checksum,quality_state,manifest_json,created_at "
        "FROM dataset_snapshots WHERE dataset_id=? ORDER BY created_at DESC LIMIT ?",
        (dataset_id, max(1, min(int(limit), 200))),
    )
    return ok({"dataset_id": dataset_id, "items": rows})


@router.get("/market/kline/cache")
async def kline_cache_stats(ctx: AppContext = Depends(get_ctx)):
    """K 线缓存统计（行数/热表·归档/序列数/命中率）。"""
    if ctx.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    return ok(await asyncio.to_thread(ctx.kline_cache.stats))

@router.delete("/market/kline/cache")
async def kline_cache_clear(code: str = "", period: str = "", ctx: AppContext = Depends(get_ctx)):
    """清理 K 线缓存（可按 code / code+period 精确清理）。"""
    if ctx.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    n = await asyncio.to_thread(ctx.kline_cache.clear, code=code, period=period)
    ctx.db.audit("admin", "kline_cache.clear", code or "*",
                   {"period": period}, f"deleted={n}")
    return ok({"deleted": n})


# ---------------- 历史 K 线导出到本地指定目录（CSV/JSON） ----------------

def _resolve_export_dir(ctx: AppContext, raw) -> str:
    """解析导出目录：未指定时用运行期配置 ``offline.export_dir``（默认 <运行目录>/export）。

    ★ 目录可能来自用户输入，必须经 ``core.paths.validate_dir`` 校验：
    否则填个 ``C:\\Windows\\System32`` 就把导出文件写进系统目录了
    （轻则权限报错，重则污染系统目录）。校验唯一入口，不在这里另写一套。
    """
    rc = ctx.runtime_config
    v = str(raw or "").strip()
    if not v:
        v = str(rc.get("offline.export_dir") or "") if rc else ""
    return str(validate_dir(str(export_dir(v)), create=True))


@router.post("/market/kline/export")
async def kline_export(body: dict, ctx: AppContext = Depends(get_ctx)):
    """批量导出历史 K 线到本地指定目录（CSV / JSON）。

    参数（body JSON）：
    - dest_dir: **可选**，导出目录（不存在自动创建）；省略时用运行期配置
      ``offline.export_dir``（默认 ``<运行目录>/export``，可在「设置 → 数据目录」改）
    - codes: 可选，代码列表；省略则导出本地缓存中全部 code×period 序列
    - period: 可选，仅导出该周期
    - count: 每序列导出最近 N 根；0/省略=导出该序列全部根数
    - format: csv | json，默认 csv
    - refresh: false（默认）快速直接用本地缓存导出；true 先回源券商刷新到缓存再导出（需连接券商）
    - conn_id: 指定 broker 连接（refresh 回源用）

    数据来自本地 K 线缓存（KlineCache），"快速"导出完全离线，无网络调用。
    """
    if ctx.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    body = dict(body or {})
    try:
        body["dest_dir"] = _resolve_export_dir(ctx, body.get("dest_dir"))
    except PathError as exc:
        return err(400, str(exc))
    try:
        out = await kline_io.kline_export(ctx.kline_cache, body)
    except ValueError as exc:
        return err(400, str(exc))
    ctx.db.audit("admin", "kline.export", body.get("dest_dir") or "",
                   {"format": body.get("format") or "csv",
                    "refresh": bool(body.get("refresh", False)),
                    "dest_dir": body.get("dest_dir") or ""},
                   f"files={out.get('exported', 0)} rows={out.get('rows', 0)}")
    return ok(out)


@router.get("/market/kline/export")
async def kline_export_read(code: str, dest_dir: str = "", period: str = "1d",
                            format: str = "csv", ctx: AppContext = Depends(get_ctx)):
    """读取本地导出目录中已导出的历史 K 线文件（离线/断线时也可用）。

    直接读磁盘文件，不依赖券商连接；文件不存在返回 404。
    ``dest_dir`` 省略时用运行期配置（与 POST 同口径）。
    format: csv | json（须与导出时一致）。
    """
    from gateway.kline_cache import KlineCache
    try:
        dest_dir = _resolve_export_dir(ctx, dest_dir)
    except PathError as exc:
        return err(400, str(exc))
    path = KlineCache.file_path(code, period, dest_dir, format)
    if not os.path.exists(path):
        return err(404, f"导出文件不存在：{os.path.basename(path)}"
                        "（请先 POST /market/kline/export 导出）")
    try:
        bars = await asyncio.to_thread(KlineCache.read_export, path, format)
    except Exception as exc:  # noqa: BLE001
        return err(500, f"读取导出文件失败：{exc}")
    return ok({"code": code, "period": period, "format": format,
               "file": path, "count": len(bars), "bars": bars})


# ---------------- 同步全部历史 K 线（日线+周线）到本地指定目录 ----------------

@router.post("/market/kline/sync")
async def kline_sync(body: dict, ctx: AppContext = Depends(get_ctx)):
    """把一批股票的最新历史 K 线（含日线 1d、周线 1w）同步到本地指定目录。

    流程：确定股票集合 → 逐只回源券商拉取最新 K 线写入本地缓存 → 导出到 dest_dir。
    参数（body JSON）：dest_dir(**可选**，省略时用运行期配置 ``offline.export_dir``)/
    codes/sector/periods/count/format/limit/conn_id。
    单只失败不中断整体（errors 列出）。真实行情，缺数据不伪造。
    """
    if ctx.kline_cache is None:
        return err(503, "K 线缓存未初始化")
    try:
        dest = _resolve_export_dir(ctx, (body or {}).get("dest_dir"))
    except PathError as exc:
        return err(400, str(exc))
    body = {**(body or {}), "dest_dir": dest}

    async def _get_sector_stocks(sector: str, conn_id):
        b = _need(conn_id)
        if b is None:
            raise BrokerError("未连接任何券商客户端：省略 codes 需用板块成分，请先连接券商。")
        return await _call(b, b.gateway.get_sector_stocks, sector) or []

    try:
        out = await kline_io.kline_sync(ctx.kline_cache, body, _get_sector_stocks)
    except ValueError as exc:
        return err(400, str(exc))
    except BrokerError as exc:
        return err(503, str(exc))
    except LookupError as exc:
        return err(404, str(exc))
    ctx.db.audit("admin", "kline.sync", body.get("dest_dir") or "",
                   {"format": body.get("format") or "csv",
                    "periods": out["periods"], "count": int(body.get("count") or 250),
                    "codes": out["codes_total"]},
                   f"files={out['files_count']} rows={out['rows']} errors={len(out['errors'])}")
    return ok(out)


# ---------------- 行情爬虫（真实 K 线落库） ----------------

@router.post("/market/crawl")
async def crawl_market(body: dict, ctx: AppContext = Depends(get_ctx)):
    """创建/提交market / crawl（POST /market/crawl）。"""
    try:
        return ok(await kline_io.crawl_market(body))
    except PermissionError as exc:
        return err(503, str(exc))


# ---------------- LLM 配置（加密存储） ----------------

@router.get("/market/l2")
async def market_l2(code: str, count: int = 100, ctx: AppContext = Depends(get_ctx)):
    """获取market / l2（GET /market/l2）。"""
    b = _need()
    if b is None:
        return no_broker()
    res = await _call(b, b.gateway.get_l2_transactions, code, count)
    return envelope_ok(res)


# ---------------- 策略模板库 ----------------
