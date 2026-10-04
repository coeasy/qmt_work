"""eltdx 适配器：通达信(TDX)公共行情源，直连行情服务器，无需任何券商客户端。

作为「无券商连接」时的行情 / 基础数据补充（DataSource 实现）。覆盖：
- 实时盘口快照（含五档 buy_levels / sell_levels）
- 历史 K 线（日/周/月/分钟，支持前/后复权）
- 合约基础信息（名称来自 A 股列表、昨收来自快照、涨跌停按板块推算）
- 全市场股票列表（含中文名，解决 stock-info 中文名缺失）
- 行业 / 概念（通达信行业 N012 + 题材概念，来自 F10 网关）

★ 传输层（2026-10-04）：**easy_tdx 优先**（MIT 许可，商用安全），eltdx 降级为
easy_tdx 缺失时的可选回退（Research-Only，禁止商用）。两者共享本适配层的
七族门面接口：easy_tdx 经 ``datasource/tdx_transport.py`` 门面适配，行为口径
（单位 / 分页 / 降级语义）见该模块 docstring 的实测记录。当前后端见
``_TDX_BACKEND``（easy_tdx / eltdx / none），源 ID 定为 ``tdx``。

- 两个库均为**可选**依赖：缺失时模块仍可正常 import（``_HAS_TDX=False``），
  所有网络方法提前返回 / 抛明确异常，系统自动降级为券商数据源
- 降级 ≠ 造假：宁可无数据，也不返回任何伪造行情（项目零 mock 铁律）

详见 docs/THIRD_PARTY_LICENSES.md（easy_tdx: MIT；eltdx: Research-Only）。

本地缓存：股票名称表 / 行业概念表持久化到 <data_dir> 下的 JSON，
重启后优先读本地缓存（带 TTL），避免首拉 / 每次重启都走网络。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional
from core.clock import now_iso

try:  # 首选传输：easy_tdx 门面（MIT 许可，商用安全）
    from datasource.tdx_transport import EasyTdxClient as TdxClient
    _HAS_TDX = True
    _TDX_BACKEND = "easy_tdx"
except ImportError:
    try:  # 回退传输：eltdx 原生门面（Research-Only，禁止商用）
        from eltdx import TdxClient
        _HAS_TDX = True
        _TDX_BACKEND = "eltdx"
    except ImportError:  # pragma: no cover - 取决于部署环境是否安装
        TdxClient = None  # type: ignore[assignment,misc]
        _HAS_TDX = False
        _TDX_BACKEND = "none"

from datasource.base import DataSource, EXT_DETAIL_KEYS
from datasource.board import classify_board, limit_ratio
from datasource.eltdx_utils import (  # noqa: F401
    _EXCH_PFX,
    _FW2HW,
    _INDEX_FALLBACK_NAMES,
    _INDUSTRY_CACHE,
    _NAME_CACHE,
    _cache_dir,
    _f,
    _load_json_cache,
    _map_adjust,
    _map_period,
    _name_cache_path,
    _normalize_name,
    _num,
    _save_json_cache,
    _to_eltdx,
    _to_qmt,
    is_index_code,
)
from datasource.eltdx_boards import EltdxBoardMixin
from datasource.eltdx_industry import EltdxIndustryMixin

log = logging.getLogger("qmt_work.datasource.eltdx")

# ---------------- 公共行情连接治理（防止连接风暴 / 被限速封 IP） ----------------
# 复用单个 TdxClient（带 TTL 回收）；全局最小请求间隔限流；并发连接数上限。
_CLIENT_TTL = 180.0          # 复用连接最长存活（秒），到期重建
_MIN_CALL_INTERVAL = 0.12    # 两次 TDX 调用间最小间隔（秒），全局限流
_MAX_CONCURRENT = 6          # 同时打到 TDX 的连接数上限（信号量）
_PRECLOSE_TTL = 86400.0      # 昨收缓存有效期（秒）：盘中不变，按日缓存即可
_NAME_BATCH = 2000           # 每批经 stock_profile_table 取简称的证券数

class EltdxSource(EltdxBoardMixin, EltdxIndustryMixin, DataSource):
    #: 源 ID（2026-10-04 由 "eltdx" 改为 "tdx"：传输层已切换为 easy_tdx(MIT)，
    #: eltdx 库仅为回退；能力链 / provider 目录 / 测试断言同步更新）
    name = "tdx"

    #: V11 R6：能力声明（**能力的唯一真源**）。此前未声明 → 继承基类默认值
    #: ``{quote, kline, instrument_detail, stock_list}``，与本类实际实现严重不符：
    #: 本类还实现了 sector / index_constituent / capital / price_limit / moneyflow /
    #: minutes / etf_list / search —— 补上后 ``resolve_chain`` 的能力校验才不会
    #: 把 eltdx 从这些能力的链里剔除（它其实是这些能力的**唯一**提供方）。
    #: 不含 fundamental / calendar / suspend / corporate_action：本类未实现。
    capabilities = frozenset({
        "quote", "kline", "kline_qfq", "kline_hfq", "instrument_detail", "stock_list",
        "sector", "index_constituent", "capital", "price_limit", "moneyflow",
        "minutes", "etf_list", "search", "ticks",
    })

    #: 代码->名称 缓存（首次拉全市场列表后常驻进程内存，并持久化到本地 JSON）
    _name_map: dict = {}
    #: 小写检索索引：lower(name/code) -> [(code, name), ...]（名称表加载时一次性建）
    _search_index: dict = {}
    #: 代码->{"industry": str, "concepts": [str], "ts": iso} 缓存（持久化到本地 JSON）
    _industry_map: dict = {}
    _industry_loaded: bool = False
    #: 代码->最近一次行业拉取时间戳（空结果按 _INDUSTRY_RETRY_TTL 重试；
    #: 磁盘缓存里的陈旧空值首次命中时 _industry_ts 为 0 → 必然重拉）
    _industry_ts: dict = {}
    _lock = threading.Lock()
    _industry_lock = threading.Lock()
    # 异步路径专用锁。threading.Lock 绝不能跨 await 持有：它会在线程池 worker 仍在
    # 做 90s 网络枚举时，让事件循环线程同步卡在 `with self._lock` 上，整个 uvicorn
    # 事件循环冻结——所有 REST/WS/静态文件请求永久挂起（前端表现为「tab 打不开」），
    # 且外层 asyncio.wait_for 的超时也因事件循环被占死而永不触发。
    _aio_lock: "asyncio.Lock | None" = None

    @classmethod
    def _get_aio_lock(cls) -> "asyncio.Lock":
        """惰性创建类级 asyncio.Lock。仅创建路径用 _lock 保护，内部无 await，纳秒级。"""
        if cls._aio_lock is None:
            with cls._lock:
                if cls._aio_lock is None:
                    cls._aio_lock = asyncio.Lock()
        return cls._aio_lock
    # 连接治理（类级，跨实例共享）
    _client = None
    _client_ts = 0.0
    _client_lock = threading.Lock()
    _rate_lock = threading.Lock()
    _last_call = 0.0
    _conn_sem = threading.BoundedSemaphore(_MAX_CONCURRENT)
    # 昨收缓存：避免 stock-info / quote 富化反复打快照
    _preclose_map: dict = {}
    _preclose_ts: dict = {}
    # 行情派生指标缓存（估值/市值/换手/量比/振幅/均价，同一次快照直供）
    _metric_map: dict = {}
    _metric_ts: dict = {}
    # 声明本源可提供的详情字段（与 EXT_DETAIL_KEYS 共用一份常量，杜绝漂移）。
    # 2026-10-04 R1：MAC 快照已原生提供这些字段，故 metric_sources() 能把 tdx
    # 纳入「缺口补齐候选源」，优先于公开行情源（少一次网络 + 口径一致）。
    _DETAIL_KEYS = EXT_DETAIL_KEYS

    # EXT_DETAIL_KEYS 契约名 → easy_tdx MAC 快照门面属性名。
    # 2026-10-04 R1：get_quote 与 get_instrument_detail **必须共用这一份映射**，
    # 否则「界面显示正常（get_quote 手写正确）、但 stock-info 面板全空
    # （instrument_detail 按契约名 getattr 拿到 None）」——两处各写一份必漂移。
    _SNAP_METRIC_MAP = {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "avg_price": "avg_price",
        "amplitude": "amplitude",
        "turnover_rate": "turnover",
        "volume_ratio": "vol_ratio",
        "pe_ttm": "pe_ttm",
        "pb": "pb_ratio",
        "circ_mv": "circulating_market_cap",
        "total_mv": "total_market_cap",
        "amount": "amount",
    }

    @classmethod
    def lookup_name(cls, code: str) -> Optional[str]:
        """O(1) 名称兜底（WS 推送 / 搜索用），直接读进程内名称表，无网络。"""
        return cls._name_map.get(code)

    @classmethod
    def is_ready(cls) -> bool:
        """健康检查：名称表已加载即视为就绪（行业表缺失不阻塞）。"""
        return bool(cls._name_map)

    @classmethod
    def _rebuild_search_index(cls) -> None:
        """基于名称表构建小写检索索引（code 与 name 都可被搜），O(1) 定位候选。"""
        idx: dict = {}
        for code, name in cls._name_map.items():
            for key in (code, (name or "").lower()):
                if not key:
                    continue
                idx.setdefault(key, []).append((code, name))
        cls._search_index = idx

    # ---------- 连接治理 ----------
    def _acquire_client(self, timeout: int = 8) -> TdxClient:
        """返回可复用的 TdxClient（类级单例，TTL 到期重建）。"""
        if not _HAS_TDX:
            raise RuntimeError(
                "easy_tdx/eltdx 均未安装，TDX 行情源不可用（可选补充源）。"
                "请连接券商数据源，或 pip install -r requirements-optional.txt"
            )
        now = time.time()
        with self.__class__._client_lock:
            c = self.__class__._client
            if c is not None and (now - self.__class__._client_ts) < _CLIENT_TTL:
                return c
            c = TdxClient(timeout=timeout)
            self.__class__._client = c
            self.__class__._client_ts = now
            return c

    def _invalidate_client(self) -> None:
        with self.__class__._client_lock:
            self.__class__._client = None

    @classmethod
    def _throttle(cls) -> None:
        """全局最小请求间隔限流（在 worker 线程内 sleep，不阻塞事件循环）。"""
        with cls._rate_lock:
            now = time.time()
            wait = _MIN_CALL_INTERVAL - (now - cls._last_call)
            if wait > 0:
                time.sleep(wait)
            cls._last_call = time.time()

    def _use_client(self, fn):
        """在限流 + 并发上限下，用复用连接执行 fn(cl)；失败重建并以 `with` 兜底重试一次。

        优先复用已建立的连接（消除每次建连的连接风暴）；若复用失败（连接失效 /
        该 SDK 需 `with` 重新进入才连得上），则新建连接并以上下文管理器方式重试，
        保证至少不低于原 `with TdxClient()` 行为的可用性。
        """
        self._throttle()
        with self.__class__._conn_sem:
            cl = self._acquire_client()
            try:
                return fn(cl)
            except Exception:  # noqa: BLE001
                self._invalidate_client()
                cl = self._acquire_client()
                with cl:
                    return fn(cl)

    # ---------- 名称表（本地缓存 + 预热） ----------
    async def _ensure_name_map(self):
        """加载名称表到类级 _name_map（所有实例共享，原地更新避免实例级 shadow）。

        名称是稳定的（证券代码/证券简称基本不变号），因此只要本地缓存存在就直接使用，
        不再按 TTL 到期就重拉——重拉走的是已失效的网络接口，反而会让本可用的名称表
        在进程内保持为空，导致搜索/名称/就绪检测全部失效。仅当缓存文件完全缺失时才
        尝试网络枚举（此时取不到中文名，仅填充代码，保证 is_ready 至少为真）。
        """
        # 检查类级 dict 是否已加载
        if self.__class__._name_map:
            return
        async with self.__class__._get_aio_lock():
            # 双重检查
            if self.__class__._name_map:
                return
            # 1) 优先读本地缓存：只要缓存里含中文简称即用（名称不随 TTL 变化，避免无效重拉）。
            #    兼容旧 bug：曾出现「仅代码、中文名为空」的坏缓存 → 视为过期，走下方网络重建。
            #    文件 I/O 放线程池：同步读文件会直接占死事件循环。
            cached = await asyncio.to_thread(_load_json_cache, _name_cache_path())
            # ★ 2026-10-04 新增质量校验：实测出现过 2199 条**纯基金残表**（缺全部 A 股
            # 条目），旧逻辑「有值就用」会让网络重建被永久跳过 —— 搜索/名称/详情的
            # 中文名全部失效且无自愈路径。缓存必须覆盖 A 股（6xx.SH / 00x·30x.SZ）
            # 才视为有效。
            def _has_stock_coverage(d: dict) -> bool:
                for k in d:
                    if k.endswith(".SH") and k[:2] in ("60", "68"):
                        return True
                    if k.endswith(".SZ") and k[:3] in ("000", "001", "002", "003",
                                                       "300", "301"):
                        return True
                return False

            if cached and any(cached.values()) and _has_stock_coverage(cached):
                # 历史缓存可能是「规整逻辑加入之前」落盘的原始值（实测 000858.SZ
                # 存成 "五 粮 液"），直接 update 会把脏名字带进内存与接口返回。
                # 加载时统一再规整一次，并仅在确有变化时回写，避免每次启动都写盘。
                clean = {k: _normalize_name(v) for k, v in cached.items()}
                self.__class__._name_map.update(clean)
                self.__class__._rebuild_search_index()
                if clean != cached:
                    await asyncio.to_thread(_save_json_cache, _name_cache_path(), clean)
                    log.info("eltdx 名称表已从本地缓存加载并规整：%d 只", len(clean))
                else:
                    log.info("eltdx 名称表已从本地缓存加载：%d 只", len(clean))
                return
            # 2) 缓存缺失 → 网络枚举全 A 股代码 + 批量取中文名（TDX 公共行情可搜索/可就绪）
            def _run():
                if not _HAS_TDX:
                    log.warning("TDX 传输不可用，跳过名称表网络枚举（降级：仅券商源可用）")
                    return
                # ★ 模块级 TdxClient（easy_tdx 门面 / eltdx 回退）——禁止函数内局部
                # import：那会绕过测试对模块属性的替换点（R21 同族教训）。
                with TdxClient(timeout=90) as cl:
                    # 2.1) 分页枚举全 A 股证券代码。沪深A股 覆盖沪深两市，另兼容 "A股" 分类；
                    #      单个分类失败（如分类名随 SDK 变更失效）不中断整体，逐类 try。
                    ent = []          # (code, ex, full_code)，e.g. ("600519","SH","sh600519")
                    seen = set()
                    for cat in ("沪深A股", "A股"):
                        try:
                            start = 0
                            while True:
                                page = cl.quotes.list_by_category(category=cat, start=start, count=80)
                                recs = getattr(page, "records", []) or []
                                if not recs:
                                    break
                                for r in recs:
                                    cd = getattr(r, "code", "")
                                    ex = (getattr(r, "exchange", "sh") or "sh").lower()
                                    if not cd or f"{cd}.{ex.upper()}" in seen:
                                        continue
                                    seen.add(f"{cd}.{ex.upper()}")
                                    ent.append((cd, ex.upper(), f"{ex}{cd}"))
                                if len(recs) < 80:
                                    break
                                start += len(recs)
                                if start > 20000:
                                    break
                        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                            # 单个分类枚举失败不影响整体：记录调试级日志，避免全量失败时无从排查
                            log.debug("eltdx 枚举分类失败（已跳过）：%s", exc)
                            continue
                    # 2.2) 用 stock_profile_table 批量取证券简称。一次几百上千条，全市场约数
                    #      秒；分批 + 逐批容错，避免单条脏数据拖垮整张名称表。
                    m = {}
                    for i in range(0, len(ent), _NAME_BATCH):
                        batch = [e[2] for e in ent[i:i + _NAME_BATCH]]
                        try:
                            tbl = cl.helpers.stock_profile_table(
                                batch, include_security=True, include_finance=False)
                            for row in (tbl.rows or []):
                                fc = (getattr(row, "full_code", "") or "")
                                if len(fc) < 3 or fc[:2].lower() not in ("sh", "sz", "bj"):
                                    continue
                                nm = (getattr(row, "name", "") or "").strip()
                                m[f"{fc[2:]}.{fc[:2].upper()}"] = _normalize_name(nm)
                        except (AttributeError, TypeError, ValueError) as exc:
                            # 单批 stock_profile_table 失败：本批中文名缺失，2.3 步会兜底填空串
                            log.debug("eltdx stock_profile_table 批次失败（已跳过）：%s", exc)
                            continue
                    # 2.3) 兜底：未取得中文名的已枚举代码也入库（保证可搜索 / is_ready 为真）
                    for cd, ex, _ in ent:
                        m.setdefault(f"{cd}.{ex}", "")
                    return m
            try:
                fetched = await asyncio.to_thread(_run)
                if fetched:
                    self.__class__._name_map.update(fetched)
                    self.__class__._rebuild_search_index()
                    _save_json_cache(_name_cache_path(), fetched)
                    named = sum(1 for v in fetched.values() if v)
                    log.info("eltdx 名称表已枚举并缓存：%d 只（含 %d 个中文简称）",
                             len(fetched), named)
            except Exception as exc:  # noqa: BLE001
                log.warning("eltdx 名称表网络枚举失败（名称表将为空，仅影响中文名搜索）: %s", exc)


    # ---------- 预热（应用启动钩子，best-effort 不阻塞） ----------
    async def warmup(self):
        """应用启动时预热：名称表（网络拉取并缓存）+ 行业概念表（仅读本地缓存）。"""
        try:
            await self._ensure_name_map()
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 预热名称表失败（首请求时惰性加载）：%s", exc)
        try:
            await self._ensure_industry_loaded()
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 预热行业表失败：%s", exc)

    # ---------- DataSource 接口 ----------
    async def get_stock_list(self) -> list:
        await self._ensure_name_map()
        return [{"code": k, "name": v} for k, v in self._name_map.items()]

    async def get_quote(self, code: str) -> dict:
        ec = _to_eltdx(code)
        def _run():
            def _inner(cl):
                snaps = cl.quotes.get_snapshots([ec])
                s = snaps[0] if isinstance(snaps, list) else snaps.get(ec)
                if s is None:
                    raise RuntimeError(f"eltdx 未返回 {code} 的行情快照")
                bids = [{"price": lv.price, "volume": lv.volume}
                        for lv in (s.buy_levels or [])]
                asks = [{"price": lv.price, "volume": lv.volume}
                        for lv in (s.sell_levels or [])]
                # 通达信风格盘口扩展字段：内外盘 / 现量 / 委买委卖总量（五档合计）。
                # 委比/委差由前端按五档派生。
                # 行情派生指标（2026-10-04 R1）：MAC 快照原生即含换手率/量比/振幅/
                # 估值/市值/主力净流入，此前因认为「快照无」而未取（注释已作废），
                # 导致 stock-info / analysis 端点的 EXT_DETAIL_KEYS 恒为 null、
                # 只能绕道公开行情源补。现按 EXT_DETAIL_KEYS 同名直供（取不到为 None）。
                return {
                    "code": code,
                    "last": s.last_price,
                    "lastClose": s.pre_close_price,
                    "volume": s.total_hand,
                    "bid": bids[0]["price"] if bids else None,
                    "ask": asks[0]["price"] if asks else None,
                    "bid_vol": bids[0]["volume"] if bids else None,
                    "ask_vol": asks[0]["volume"] if asks else None,
                    "bids": bids,
                    "asks": asks,
                    "inside": getattr(s, "inside_dish", None),    # 内盘（手）
                    "outside": getattr(s, "outer_disc", None),    # 外盘（手）
                    "current_hand": getattr(s, "current_hand", None),  # 现量（手）
                    "sum_buy_vol": getattr(s, "sum_buy_vol", None),    # 委买五档总量
                    "sum_sell_vol": getattr(s, "sum_sell_vol", None),  # 委卖五档总量
                    # —— EXT_DETAIL_KEYS 同名直供（供 manager 层白名单透出）——
                    # ⚠️ 与 get_instrument_detail 共用 _SNAP_METRIC_MAP，一处定义杜绝漂移。
                    **{k: getattr(s, a, None)
                       for k, a in self._SNAP_METRIC_MAP.items()},
                    "ts": now_iso(),
                }
            out = self._use_client(_inner)
            # 涨跌额/幅：由 last 与 lastClose 真实计算（非估算），供指数条/板块/ETF
            # 与个股统一使用同一字段名，前端不必各算一遍。分母为 0 时置 None。
            last = out.get("last")
            pre = out.get("lastClose")
            if last is not None and pre:
                out["change"] = round(float(last) - float(pre), 4)
                out["change_pct"] = round((float(last) - float(pre)) / float(pre) * 100, 2)
            else:
                out["change"] = None
                out["change_pct"] = None
            return out
        res = await asyncio.to_thread(_run)
        # 指数/板块名称补齐：股票名称表不覆盖指数代码，走指数名称表。
        if res and res.get("name") in (None, "", code):
            nm = await self._get_index_name(code)
            if nm:
                res["name"] = nm
        return res

    async def get_ticks(self, code: str, count: int = 60) -> dict:
        """当日**逐笔成交**（真实市场成交，非本账户成交回报）—— 无需券商（0x0FC5）。

        ★ 为什么需要它（2026-10-03）：看盘界面的「成交流」此前只有两条路，且都
        依赖券商桥：
          ① WS `deal` 事件 —— 那是**本账户成交回报**（`adapter.get_deals` /
             `on_trade`），看别人的票 / 未交易时永远为空，语义也不是「市场成交」；
          ② `GET /market/l2` —— 券商 L2 逐笔，未连接返 503。
        结果是「行情工作台的成交流」在没连券商时恒空，而**真实市场逐笔本来就
        可以从本地 TDX 直接取到**（本方法）。零 mock 铁律下，宁可接上真源，
        也不该把账户回报冒充成市场成交流。

        口径（与前端 `MarketTicksPanel` 的契约，改一侧必须同步另一侧）：
        - `time`   ``HH:MM:SS``（源只给到分，秒位补 ``00``）
        - `price`  成交价（元）
        - `volume` 成交量（**手**，与 eltdx `TradeTick.volume` 同口径，勿再乘 100）
        - `amount` 成交额（元）＝ ``price * volume * 100``
        - `side`   ``buy`` / ``sell`` / ``neutral``（主动买/主动卖/中性）
        - 排序      **最新在前**（源在 start=0 页内按时间升序，此处整体反转）

        ⚠️ 休市 / 非交易日调用返回的是**最近一个交易日**的逐笔（TDX 侧行为），
        因此必须把 ``trading_date`` 一并回给调用方 —— 否则用户会把上一交易日的
        成交当成今日行情（与 `TradingDateBadge` 同一类诚实性要求）。

        返回 ``{code, items: [...], trading_date: str|None, count, source}``；
        源不可用 / 无数据时 ``items`` 为空列表（**不造假填充**）。
        """
        ec = _to_eltdx(code)
        n = max(1, min(int(count or 60), 500))

        def _run():
            def _inner(cl):
                page = cl.trades.today(ec, start=0, count=n)
                # ★ 为什么优先 `actual_trades` 而不是 `ticks`（eltdx 文档明示）：
                #   `ticks` **原样保留 `status=8` 的集合竞价快照** —— 那是竞价过程的
                #   撮合快照，不是真实成交。把它们当成逐笔显示，用户会在开盘前看到
                #   一串「09:25 的成交」，而那一刻根本没有成交发生。
                #   `actual_trades` 排除非成交快照，同时保留 09:25 / 15:00 与
                #   `status=5` 盘后固定价格的**真实成交**（这些确实是成交，要显示）。
                #   拿不到 `actual_trades`（老版本/字段缺失）时退回 `ticks`，
                #   并按 `event_kind` 过滤掉竞价快照 —— 不能因为取不到精确视图
                #   就把快照当成交。
                ticks = list(getattr(page, "actual_trades", None) or ())
                if not ticks:
                    ticks = [
                        t for t in list(getattr(page, "ticks", ()) or ())
                        if str(getattr(t, "event_kind", "") or "trade").strip().lower() == "trade"
                    ]
                out = []
                for t in ticks:
                    price = _f(getattr(t, "price", None))
                    vol = getattr(t, "volume", 0) or 0
                    label = str(getattr(t, "time_label", "") or "")
                    # 源给 HH:MM（分钟精度）；补齐秒位使前端按统一时间格式渲染
                    if len(label) == 5:
                        label = f"{label}:00"
                    side = str(getattr(t, "side", "") or "").strip().lower()
                    if side not in ("buy", "sell", "neutral"):
                        side = "neutral"
                    out.append({
                        "time": label,
                        "price": price,
                        "volume": int(vol),
                        "amount": round(float(price) * int(vol) * 100.0, 2)
                        if price is not None else None,
                        "side": side,
                        "order_count": int(getattr(t, "order_count", 0) or 0),
                    })
                # 源在「最新一页」内按时间升序（早 → 晚）；成交流要**最新在前**
                out.reverse()
                td = getattr(page, "trading_date", None)
                return {
                    "code": code,
                    "items": out,
                    "count": len(out),
                    "trading_date": str(td) if td else None,
                }
            return self._use_client(_inner)

        res = await asyncio.to_thread(_run)
        res["source"] = self.name
        return res

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: Optional[str] = None) -> list:
        ec = _to_eltdx(code)
        p = _map_period(period)
        adj = _map_adjust(adjust)
        # ★★ `kind` 决定「取到谁的数据」，猜错**不是返回空而是抛异常**（见下）。
        kind = "index" if is_index_code(code) else "stock"

        def _run():
            def _inner(cl):
                bars = self._bars_any_kind(cl, ec, p, count, adj, kind)
                bl = getattr(bars, "bars", bars) or []
                out = []
                for b in bl:
                    t = getattr(b, "time", None)
                    ts = t.strftime("%Y%m%d") if hasattr(t, "strftime") else str(t)
                    raw_vol = getattr(b, "volume_lots", None)
                    out.append({
                        "time": ts,
                        "open": b.open, "high": b.high, "low": b.low,
                        "close": b.close,
                        # T6 单位归一：eltdx K 线 volume 为「手」，统一 ×100 为「股」
                        # （对齐 volume 契约单一真源；券商直连天然为股，无需处理）
                        "volume": (raw_vol * 100) if raw_vol is not None else None,
                        "volume_unit": "shares",
                        "amount": b.amount,
                    })
                return out
            return self._use_client(_inner)
        return await asyncio.to_thread(_run)

    @staticmethod
    def _bars_any_kind(cl, ec: str, period: str, count: int,
                       adjust: Optional[str], kind: str):
        """按 ``kind`` 取 K 线；kind 猜错时自动换另一边重试一次。

        ★★ 为什么必须有这一层（2026-09-21 实测，用户报「从行情工作台打开时 K 线图
        无法正常展示」的**直接根因**）：

        1. 指数与个股在 eltdx 侧走的是**两条不同的数据通道**（``kind="index"`` /
           ``kind="stock"``），本模块只在 ``get_board_kline``（/market/indices 用）
           里传过 ``kind="index"``，**通用 get_kline 从来没传** ⇒ 指数一律按个股取。
        2. 猜错的表现**不是「返回空」而是抛**
           ``ProtocolError: invalid kline date: 30869201``（实测），
           因为个股通道拿到的字节流按个股布局解析，日期字段是垃圾。
        3. 更糟的是这个异常会一路冒到 ``registry._first_supported`` 被计成
           **数据源故障**，连续 3 次即**熔断 30s** ⇒ **一个指数的请求会把整个 eltdx
           源打成不可用**，连个股 K 线、分时、资金流一起遭殃
           （实测日志 2026-09-21 22:11:18 「数据源 eltdx 熔断（连续 3 次失败），冷却 30s」）。
        4. 工作台默认标的正是 ``000001.SH``（上证指数）⇒ 一打开就是空图。

        在**这里**就地重试另一个 kind，既拿到正确数据，又不会把「kind 猜错」这种
        自身可修复的错误上报成数据源故障（异常不逃出 ``_inner`` ⇒ 不计入熔断）。

        ``is_index_code`` 的前缀判定已用 eltdx 权威清单（``codes.all_indices()``）
        核对过：1678 个指数**漏判 0 条**，5574 只个股**误判 0 条**（2026-09-21 实测）。
        重试只是给「前缀表未覆盖的冷门指数段」兜底，正常路径不会触发。
        """
        try:
            return cl.bars.get(ec, period=period, count=count, adjust=adjust, kind=kind)
        except Exception as exc:  # noqa: BLE001
            other = "stock" if kind == "index" else "index"
            log.debug("eltdx K 线 kind=%s 失败（%s），改用 kind=%s 重试：%s",
                      kind, ec, other, exc)
            return cl.bars.get(ec, period=period, count=count, adjust=adjust, kind=other)

    async def get_minutes(self, code: str, trading_date: Optional[str] = None) -> Optional[dict]:
        """当日分时（1 分钟曲线）：价格线 + 均价线 + 分钟量，含昨收基准。

        trading_date 为空取今日（today）；传 "YYYY-MM-DD" 取历史分时（history）。
        返回 {code, trading_date, pre_close, points:[{t, price, avg, volume}]}；
        无数据返回 None（绝不伪造）。
        """
        ec = _to_eltdx(code)
        def _run():
            def _inner(cl):
                if trading_date:
                    from datetime import date as _date
                    y, m, d = [int(x) for x in trading_date.split("-")]
                    ser = cl.minutes.history(ec, _date(y, m, d))
                else:
                    ser = cl.minutes.today(ec)
                if ser is None:
                    return None
                pts = []
                for p in (getattr(ser, "points", None) or ()):
                    t = getattr(p, "time_label", "") or (
                        p.time.strftime("%H:%M") if getattr(p, "time", None) else "")
                    pts.append({
                        "t": t,
                        "price": getattr(p, "price", None),
                        "avg": getattr(p, "avg_price", None),
                        "volume": getattr(p, "volume", 0),
                    })
                if not pts:
                    return None
                td = getattr(ser, "trading_date", None)
                return {
                    "code": code,
                    "trading_date": td.isoformat() if td else None,
                    "pre_close": getattr(ser, "prev_close", None),
                    "open_price": getattr(ser, "open_price", None),
                    "points": pts,
                }
            return self._use_client(_inner)
        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 分时获取失败 %s: %s", code, exc)
            return None

    # ==================== 指数 / 板块 / ETF / 资金流（东财对标能力） ====================
    # 以下全部经 2026-08-29 实测确认可用，参数与字段口径均来自真实返回，无推测。
    #
    # 实测要点（改动前必读，否则极易踩坑）：
    # - 指数 / 板块 K 线必须传 kind="index"，用默认 kind="stock" 会报
    #   ProtocolError: invalid kline date（workday.py 内部同样如此调用）。
    # - 板块 = 通达信板块指数代码段 881xxx(行业) / 880xxx(概念+统计)，共 1119 只，
    #   全量快照实测 0.42s；名称取自 codes.all(market) 的 security profile。
    # - 板块成分股走 f10.theme_market(req_id="200744")，列 N001=市场 N002=代码
    #   N003=名称 N004=涨跌幅 N005=价格，总数在 table[0] 的 total_num。
    # - 资金流用快照真实内外盘 inside_dish/outer_disc，非估算。

    # 板块分类：881=行业；880=概念（含少量统计类需过滤）
    _BOARD_SEG_INDUSTRY = "881"
    _BOARD_SEG_CONCEPT = "880"
    # 统计类板块（非真实板块，是市场温度计指标）：涨跌家数/停板家数/均价/涨跌幅统计
    _STAT_BOARD_PAT = ("涨跌家数", "停板家数", "北证涨跌", "北证停板", "北证均价",
                       "主板涨跌", "主板停板", "创业涨跌", "创业停板", "科创涨跌",
                       "科创停板", "通用回购", "深证涨跌", "深证停板")
    # R6（B3）强化：名称含这些通用模式的一律归 stat（优先于 concept 判定），
    # 覆盖「全Ａ等权/全Ａ中位/当日盈亏」等此前漏进 concept 榜的统计类条目。
    _STAT_BOARD_GENERIC_PAT = ("中位", "等权", "涨跌", "停板", "盈亏", "均价")

    _BOARD_TTL = 3600.0   # 板块代码表 / 名称表缓存（秒）：板块清单不随行情变动
    _ETF_TTL = 3600.0
    _BOARDS_CACHE: dict = {}
    _ETF_CACHE: dict = {}

    async def get_moneyflow(self, code: str) -> Optional[dict]:
        """个股资金流（真实口径）：内外盘累计 + 买卖力道分钟序列 + 量比。

        内外盘来自快照 inside_dish/outer_disc（真实累计手数，非估算）；
        strength 为分钟级主买/主卖委托序列。任一缺失返回 None 由调用方显式标注。
        """
        ec = _to_eltdx(code)

        def _run():
            def _inner(cl):
                snaps = cl.quotes.get_snapshots([ec])
                s = snaps[0] if isinstance(snaps, list) else None
                if s is None:
                    return None
                inside = getattr(s, "inside_dish", None)
                outside = getattr(s, "outer_disc", None)
                points = []
                # 真实量比：快照自带 vol_ratio（easy_tdx 路径），缺失时退回
                # eltdx 分钟对比序列（前一日同时段量 ÷ 当日量）。
                vol_cmp = getattr(s, "vol_ratio", None)
                try:
                    if vol_cmp is None:
                        vc = cl.minutes.aux(ec, kind="volume_comparison")
                        pts = getattr(vc, "points", None) or []
                        if pts:
                            lastp = pts[-1]
                            a = getattr(lastp, "series_a", None)   # 前一日同时段量
                            b = getattr(lastp, "series_b", None)   # 当日量
                            vol_cmp = round(b / a, 2) if a else None
                except Exception as exc:  # noqa: BLE001
                    log.debug("eltdx 量比获取失败 %s: %s", code, exc)
                try:
                    aux = cl.minutes.aux(ec, kind="buy_sell_strength")
                    for p in (getattr(aux, "points", None) or []):
                        points.append({
                            "t": getattr(p, "time_label", None),
                            "buy": getattr(p, "buy_commission", None),
                            "sell": getattr(p, "sell_commission", None),
                        })
                except Exception as exc:  # noqa: BLE001
                    log.debug("eltdx 买卖力道获取失败 %s: %s", code, exc)
                return {
                    "code": code,
                    "inside": inside,       # 内盘累计（手）
                    "outside": outside,     # 外盘累计（手）
                    # 主力净流入（元，MAC 原生口径）。2026-10-04 R1：easy_tdx MAC
                    # 快照自带该字段，此前完全未取 → 资金流面板的主力净流入恒空。
                    # ⚠️ 与 net（外盘-内盘，单位手）语义不同，故独立字段不覆盖 net。
                    "main_net_amount": getattr(s, "main_net_amount", None),
                    "net": (outside - inside) if (inside is not None and outside is not None) else None,
                    "strength": points,     # 分钟级主买/主卖
                    "volume_ratio": vol_cmp,  # 量比
                    "est": False,           # 真实口径，非估算
                    "ts": now_iso(),
                }
            return self._use_client(_inner)
        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 资金流获取失败 %s: %s", code, exc)
            return None

    async def get_share_capital(self, codes: list) -> dict:
        """流通股本（用于换手率）。返回 {qmt_code: {total, circulating, free_float}}。"""
        if not codes:
            return {}
        ec_list = [_to_eltdx(c) for c in codes]

        def _run():
            def _inner(cl):
                tbl = cl.helpers.daily_shares(ec_list)
                out = {}
                for row in (getattr(tbl, "rows", None) or ()):
                    out[_to_qmt(row.full_code)] = {
                        "total_shares": getattr(row, "total_shares", None),
                        "circulating_shares": getattr(row, "circulating_shares", None),
                        "free_float_shares": getattr(row, "free_float_shares", None),
                        "trade_date": str(getattr(row, "trade_date", "") or ""),
                        "source": getattr(row, "share_source", None),
                    }
                return out
            return self._use_client(_inner)
        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 流通股本获取失败：%s", exc)
            return {}

    async def get_price_limits(self, codes: list) -> dict:
        """涨跌停价（真实口径，含规则与比例）。返回 {qmt_code: {...}}。"""
        if not codes:
            return {}
        ec_list = [_to_eltdx(c) for c in codes]

        def _run():
            def _inner(cl):
                tbl = cl.helpers.daily_price_limits(ec_list)
                out = {}
                for row in (getattr(tbl, "rows", None) or ()):
                    out[_to_qmt(row.full_code)] = {
                        "name": getattr(row, "name", None),
                        "pre_close": getattr(row, "pre_close", None),
                        "limit_up": getattr(row, "limit_up_price", None),
                        "limit_down": getattr(row, "limit_down_price", None),
                        "limit_ratio_pct": getattr(row, "limit_ratio_pct", None),
                        "limit_rule": getattr(row, "limit_rule", None),
                        "limit_status": getattr(row, "limit_status", None),
                        "trade_date": str(getattr(row, "trade_date", "") or ""),
                    }
                return out
            return self._use_client(_inner)
        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 涨跌停价获取失败：%s", exc)
            return {}

    async def get_instrument_detail(self, code: str) -> dict:
        await self._ensure_name_map()
        name = self._name_map.get(code, "")
        ind = await self._get_industry(code)
        # 昨收与行情派生指标共用 TTL 缓存：避免每次 stock-info / 估值富化都打快照
        now = time.time()
        pre_close = self._preclose_map.get(code)
        metrics = dict(self.__class__._metric_map.get(code) or {})
        need_snap = (
            pre_close is None
            or (now - self.__class__._preclose_ts.get(code, 0)) > _PRECLOSE_TTL
            or (not metrics
                and (now - self.__class__._metric_ts.get(code, 0)) > _PRECLOSE_TTL)
        )
        if need_snap:
            # 一次快照同时取昨收 + 全部行情派生指标（估值/市值/换手/量比/振幅/均价）。
            # 2026-10-04 R1 数据完善：easy_tdx MAC 快照原生就是富数据（33 字段），
            # 此前本方法只取 pre_close，导致前端 StockInfoPanel 的「市盈(TTM)/市净率/
            # 总市值/流通市值/换手率/振幅/均价」在 TDX 环境下全空，只能绕道公开行情源
            # 补齐（多一次网络 + 口径不一致 + 离线场景直接空壳）。现在同一次快照直供，
            # market.py 的 _METRIC_KEYS 缺口清单为空即不再外呼公开源。
            ec = _to_eltdx(code)

            def _run():
                def _inner(cl):
                    snaps = cl.quotes.get_snapshots([ec])
                    s = snaps[0] if isinstance(snaps, list) else snaps.get(ec)
                    if not s:
                        return None, {}
                    m = {k: getattr(s, a, None)
                         for k, a in self.__class__._SNAP_METRIC_MAP.items()}
                    return s.pre_close_price, m
                return self._use_client(_inner)
            try:
                pc, m = await asyncio.to_thread(_run)
            except Exception as exc:  # noqa: BLE001
                log.warning("eltdx 昨收/指标获取失败 %s: %s", code, exc)
                pc, m = None, {}
            pre_close = pc if pc is not None else self._preclose_map.get(code)
            if pre_close is not None:
                self.__class__._preclose_map[code] = pre_close
                self.__class__._preclose_ts[code] = now
            if m:
                self.__class__._metric_map[code] = m
                self.__class__._metric_ts[code] = now
                metrics = m        # ⚠️ 必须回写局部变量：下方 return 展开的是它，
                                  #    只写缓存不回填会让 stock-info 面板恒 null
                                  #    （pre_close 有值但估值全空正是此因）。
        # 涨跌停按板块推算（A股：主板±10%，科创/创业/北交±20%；可转债无涨跌幅）
        board = classify_board(code)
        limit = limit_ratio(code, name)
        if limit is None:
            high_limit = low_limit = None
        else:
            high_limit = round(pre_close * (1 + limit), 2) if pre_close else None
            low_limit = round(pre_close * (1 - limit), 2) if pre_close else None
        return {
            "name": name or code,
            "exchange": board.get("exchange"),
            "board": board.get("board"),
            "industry": ind.get("industry") or "",
            "concepts": ind.get("concepts") or [],
            "high_limit": high_limit,
            "low_limit": low_limit,
            "pre_close": pre_close,
            # 行情派生指标：与 market.py 的 _METRIC_KEYS 同名直供（缺项为 None →
            # 前端 `--`，由 _metric_map 缺项时再按缺口去公开行情源补）。
            **{k: v for k, v in metrics.items() if v is not None},
        }

    def _index_match(self, q: str, limit: int) -> list:
        """在已加载的名称索引上匹配（同步、纯本地）：精确 → 拼音首字母 → 包含。"""
        idx = self.__class__._search_index
        ql = q.lower()
        exact = idx.get(ql) or []
        contain = []
        # 拼音首字母匹配（q 为纯字母且非 6 位代码形态时启用；名称含字母的除外）
        if ql.isalpha() and not q.isdigit():
            from datasource.pinyin import matches_initials
            for code, name in self.__class__._name_map.items():
                if len(exact) + len(contain) >= limit:
                    break
                it = (code, name)
                if it in exact or it in contain:
                    continue
                if name and matches_initials(name, ql):
                    contain.append(it)
        if len(exact) + len(contain) < limit:
            for key, items in idx.items():
                if ql in key:
                    for it in items:
                        if it not in exact and it not in contain:
                            contain.append(it)
                            if len(exact) + len(contain) >= limit:
                                break
                if len(exact) + len(contain) >= limit:
                    break
        out = []
        for code, name in exact + contain:
            if len(out) >= limit:
                break
            out.append({"code": code, "name": name})
        return out

    def _merge_into_name_map(self, entries: list) -> None:
        """把 [{code,name}] 清单并入类级名称表并持久化（ETF 可搜索的关键路径）。

        仅补充缺失项（已有中文名不覆盖），增量写回 stock_names.json —— 并入一次后
        重启即离线可搜，后续不再触发网络。
        """
        added = {}
        for e in entries or []:
            code = (e.get("code") or "").strip()
            # 与名称表构建路径保持同一口径：并入前先规整（去 TDX 填充空格 / 全角转半角），
            # 否则 ETF 名称会以原始脏值进入内存与 stock_names.json。
            nm = _normalize_name((e.get("name") or "").strip())
            if code and nm and not self.__class__._name_map.get(code):
                self.__class__._name_map[code] = nm
                added[code] = nm
        if added:
            self.__class__._rebuild_search_index()
            try:
                _save_json_cache(_name_cache_path(), dict(self.__class__._name_map))
                log.info("eltdx 名称表已并入 ETF 简称：%d 只（总 %d 只）",
                         len(added), len(self.__class__._name_map))
            except OSError as exc:
                log.warning("eltdx 名称表持久化失败：%s", exc)

    async def search(self, q: str, limit: int = 20) -> list:
        """按代码 / 中文名 / 拼音首字母模糊搜索（本地索引优先，ETF 惰性并入）。

        匹配优先级：精确（code/全名）→ 拼音首字母（支持多音字，gzmt→贵州茅台、
        payh→平安银行）→ 包含；上限 limit 条。
        A 股名称表不含 ETF（51/56/58/15/16 段）：当 ETF 代码段查询无命中时，惰性拉取
        ETF 清单并入名称表后重试一次（此后持久化，重启离线可搜；非 ETF 段不触发网络）。
        """
        q = (q or "").strip()
        if not q:
            return []
        await self._ensure_name_map()
        out = self._index_match(q, limit)
        digits = "".join(ch for ch in q if ch.isdigit())
        if not out and digits[:2] in ("51", "56", "58", "15", "16"):
            try:
                await self.get_etf_list()
                out = self._index_match(q, limit)
            except Exception as exc:  # noqa: BLE001
                log.debug("eltdx ETF 清单并入重试失败：%s", exc)
        return out


__all__ = ["EltdxSource", "_to_eltdx", "_to_qmt"]
