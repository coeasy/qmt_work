"""eltdx 适配器：通达信(TDX)公共行情源，直连行情服务器，无需任何券商客户端。

作为「无券商连接」时的行情 / 基础数据补充（DataSource 实现）。覆盖：
- 实时盘口快照（含五档 buy_levels / sell_levels）
- 历史 K 线（日/周/月/分钟，支持前/后复权）
- 合约基础信息（名称来自 A 股列表、昨收来自快照、涨跌停按板块推算）
- 全市场股票列表（含中文名，解决 stock-info 中文名缺失）
- 行业 / 概念（通达信行业 N012 + 题材概念，来自 F10 网关）

⚠️ 许可证：eltdx 采用「ELTDX Research-Only License」，仅允许个人学习 / 协议研究 /
非商业研究使用，禁止一切商业使用和滥用。本适配器将其作为**可选**行情补充源：

- 默认不安装（见 backend/requirements-optional.txt），商用部署请确保环境无此包
- 本模块采用软依赖导入：eltdx 缺失时模块仍可正常 import，`_HAS_ELTDX=False`，
  所有网络方法提前返回 / 抛明确异常，系统自动降级为券商数据源
- 降级 ≠ 造假：宁可无数据，也不返回任何伪造行情（项目零 mock 铁律）

详见 docs/THIRD_PARTY_LICENSES.md 第 3.2 节「阻断级风险」。

本地缓存：股票名称表 / 行业概念表持久化到 <data_dir> 下的 JSON，
重启后优先读本地缓存（带 TTL），避免首拉 / 每次重启都走网络。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime
from typing import Optional

try:  # 可选依赖：商用部署禁止安装 eltdx（Research-Only 许可）
    from eltdx import TdxClient
    _HAS_ELTDX = True
except ImportError:  # pragma: no cover - 取决于部署环境是否安装
    TdxClient = None  # type: ignore[assignment,misc]
    _HAS_ELTDX = False

from datasource.base import DataSource
from datasource.board import classify_board, limit_ratio
from datasource.periods import to_eltdx_period
from datasource.eltdx_utils import (  # noqa: F401
    _EXCH_PFX, _NAME_CACHE, _INDUSTRY_CACHE, _FW2HW, _INDEX_FALLBACK_NAMES,
    _f, _normalize_name, _cache_dir, _name_cache_path, _industry_cache_path,
    _num, _to_eltdx, _to_qmt, _map_period, _map_adjust, _load_json_cache, _save_json_cache,
)

log = logging.getLogger("qmt_work.datasource.eltdx")

# ---------------- 公共行情连接治理（防止连接风暴 / 被限速封 IP） ----------------
# 复用单个 TdxClient（带 TTL 回收）；全局最小请求间隔限流；并发连接数上限。
_CLIENT_TTL = 180.0          # 复用连接最长存活（秒），到期重建
_MIN_CALL_INTERVAL = 0.12    # 两次 TDX 调用间最小间隔（秒），全局限流
_MAX_CONCURRENT = 6          # 同时打到 TDX 的连接数上限（信号量）
_PRECLOSE_TTL = 86400.0      # 昨收缓存有效期（秒）：盘中不变，按日缓存即可
_NAME_BATCH = 2000           # 每批经 stock_profile_table 取简称的证券数

class EltdxSource(DataSource):
    name = "eltdx"
    #: 代码->名称 缓存（首次拉全市场列表后常驻进程内存，并持久化到本地 JSON）
    _name_map: dict = {}
    #: 小写检索索引：lower(name/code) -> [(code, name), ...]（名称表加载时一次性建）
    _search_index: dict = {}
    #: 代码->{"industry": str, "concepts": [str], "ts": iso} 缓存（持久化到本地 JSON）
    _industry_map: dict = {}
    _industry_loaded: bool = False
    _lock = threading.Lock()
    _industry_lock = threading.Lock()
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
        if not _HAS_ELTDX:
            raise RuntimeError(
                "eltdx 未安装，TDX 行情源不可用（非商业可选的补充源）。"
                "请连接券商数据源，或在非商业场景下 pip install -r requirements-optional.txt"
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
        with self._lock:
            # 双重检查
            if self.__class__._name_map:
                return
            # 1) 优先读本地缓存：只要缓存里含中文简称即用（名称不随 TTL 变化，避免无效重拉）。
            #    兼容旧 bug：曾出现「仅代码、中文名为空」的坏缓存 → 视为过期，走下方网络重建。
            cached = _load_json_cache(_name_cache_path())
            if cached and any(cached.values()):
                self.__class__._name_map.update(cached)
                self.__class__._rebuild_search_index()
                log.info("eltdx 名称表已从本地缓存加载：%d 只", len(cached))
                return
            # 2) 缓存缺失 → 网络枚举全 A 股代码 + 批量取中文名（TDX 公共行情可搜索/可就绪）
            def _run():
                if not _HAS_ELTDX:
                    log.warning("eltdx 未安装，跳过名称表网络枚举（降级：仅券商源可用）")
                    return
                from eltdx import TdxClient as _TCL
                with _TCL(timeout=90) as cl:
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

    async def _ensure_industry_loaded(self):
        """加载行业概念表到类级 _industry_map（所有实例共享）。"""
        if self.__class__._industry_loaded:
            return
        with self._lock:
            if self.__class__._industry_loaded:
                return
            cached = _load_json_cache(_industry_cache_path())
            if cached:
                self.__class__._industry_map.update(cached)
            self.__class__._industry_loaded = True
            if cached:
                log.info("eltdx 行业概念表已从本地缓存加载：%d 只", len(cached))

    async def _get_industry(self, code: str) -> dict:
        """返回 {industry, concepts}；优先本地缓存，缺失则经 F10 网关拉取并落盘。

        并发安全：网络拉取与本地落盘均经 _industry_lock 串行；且仅在确有
        新记录时才重写整份缓存文件，避免高频请求下的写放大与丢更新。
        """
        await self._ensure_industry_loaded()
        cls_imap = self.__class__._industry_map
        with self.__class__._industry_lock:
            if code in cls_imap:
                return cls_imap[code]
        num = _num(code)
        industry, concepts = "", []
        try:
            def _run():
                def _inner(cl):
                    ind = ""
                    try:
                        sc = cl.f10.stock_score(num, section="pf")
                        rows = getattr(sc, "rows", None) or []
                        if rows:
                            ind = (rows[0].get("N012") or "").strip()
                    except Exception as e:  # noqa: BLE001
                        log.warning("eltdx 行业(F10)拉取失败 %s: %s", code, e)
                    cons = []
                    try:
                        tp = cl.helpers.stock_topics(num)
                        for t in (getattr(tp, "topics", []) or []):
                            nm = getattr(t, "topic_name", None)
                            if nm is None and isinstance(t, dict):
                                nm = t.get("topic_name")
                            if nm:
                                cons.append(nm)
                    except Exception as e:  # noqa: BLE001
                        log.warning("eltdx 题材拉取失败 %s: %s", code, e)
                    return ind, cons[:8]
                return self._use_client(_inner)
            industry, concepts = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 行业/题材获取失败 %s: %s", code, exc)

        rec = {"industry": industry, "concepts": concepts,
               "ts": datetime.now().isoformat(timespec="seconds")}
        with self.__class__._industry_lock:
            if code not in cls_imap:  # 二次检查，避免并发重复落盘
                cls_imap[code] = rec
                _save_json_cache(_industry_cache_path(), cls_imap)
        return rec

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
                # 委比/委差由前端按五档派生；换手率需流通股本（快照无），暂不伪造。
                return {
                    "code": code,
                    "last": s.last_price,
                    "open": s.open_price,
                    "high": s.high_price,
                    "low": s.low_price,
                    "lastClose": s.pre_close_price,
                    "volume": s.total_hand,
                    "amount": s.amount,
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
                    "ts": datetime.now().isoformat(timespec="seconds"),
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

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: Optional[str] = None) -> list:
        ec = _to_eltdx(code)
        p = _map_period(period)
        adj = _map_adjust(adjust)
        def _run():
            def _inner(cl):
                bars = cl.bars.get(ec, period=p, count=count, adjust=adj)
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

    def _is_stat_name(self, nm: str) -> bool:
        """统计类板块判定：精确模式 + 通用模式（B3 强化）任一命中即 stat。"""
        return (any(k in nm for k in self._STAT_BOARD_PAT)
                or any(k in nm for k in self._STAT_BOARD_GENERIC_PAT))
    _BOARD_TTL = 3600.0   # 板块代码表 / 名称表缓存（秒）：板块清单不随行情变动
    _ETF_TTL = 3600.0
    _BOARDS_CACHE: dict = {}
    _ETF_CACHE: dict = {}

    @classmethod
    def _board_cache_get(cls, key: str, ttl: float):
        item = cls._BOARDS_CACHE.get(key)
        if item and (time.time() - item[0]) < ttl:
            return item[1]
        return None

    @classmethod
    def _board_cache_set(cls, key: str, val) -> None:
        cls._BOARDS_CACHE[key] = (time.time(), val)

    def _load_index_names(self, cl) -> dict:
        """交易所指数/板块代码 -> 名称（来自 security profile，本地解析无额外网络往返风险）。"""
        names: dict = {}
        for mk in ("sh", "sz", "bj"):
            try:
                for p in cl.codes.all(mk):
                    if getattr(p, "category", None) == "index":
                        names[p.full_code] = p.name
            except Exception as exc:  # noqa: BLE001
                log.debug("eltdx 指数名称表 %s 加载失败：%s", mk, exc)
        return names

    async def _get_index_name(self, code: str) -> Optional[str]:
        """指数 / 板块名称（股票名称表不覆盖指数代码，单独维护）。

        结果按 _BOARD_TTL 缓存；仅在确为指数/板块代码段时才触发网络加载，
        普通个股直接返回 None，避免给每笔 quote 增加无谓往返。
        """
        ec = _to_eltdx(code)
        c6 = ec[2:]
        is_index = c6.startswith(("000", "999")) or ec[:2] == "bj" \
            or c6.startswith(("399", "880", "881", "899"))
        if not is_index:
            return None
        cache_key = "index_names"
        names = self._board_cache_get(cache_key, self._BOARD_TTL)
        if names is None:
            def _run():
                def _inner(cl):
                    return self._load_index_names(cl)
                return self._use_client(_inner)
            try:
                names = await asyncio.to_thread(_run)
            except Exception as exc:  # noqa: BLE001
                log.debug("eltdx 指数名称表加载失败：%s", exc)
                names = {}
            if names:
                self._board_cache_set(cache_key, names)
        if names and names.get(ec):
            return names[ec]
        # 主要指数兜底：部分交易所清单不含常用宽基指数名
        return _INDEX_FALLBACK_NAMES.get(code.upper())

    def _load_board_codes(self, cl) -> tuple[list, dict]:
        """返回 (板块代码列表, 代码->名称)。缓存 1 小时。"""
        cached = self._board_cache_get("boards", self._BOARD_TTL)
        if cached:
            return cached
        codes = []
        for c in cl.codes.all_indices():
            seg = c[2:5]
            if seg in (self._BOARD_SEG_INDUSTRY, self._BOARD_SEG_CONCEPT):
                codes.append(c)
        names = self._load_index_names(cl)
        out = (codes, names)
        self._board_cache_set("boards", out)
        return out

    async def get_boards(self, kind: str = "industry", sort_by: str = "pct",
                         limit: int = 50) -> list:
        """板块指数榜单（真实板块指数快照，非自聚合）。

        kind: industry(881xxx) / concept(880xxx 剔除统计类) / stat(仅统计类)
        sort_by: pct(涨跌幅) / amount(成交额)
        返回元素含 code/name/last/change_pct/amount/kind/source，按 sort_by 降序。
        """
        def _run():
            def _inner(cl):
                codes, names = self._load_board_codes(cl)
                seg = (self._BOARD_SEG_INDUSTRY if kind == "industry"
                       else self._BOARD_SEG_CONCEPT)
                target = [c for c in codes if c[2:5] == seg]
                rows = []
                for i in range(0, len(target), 80):
                    batch = target[i:i + 80]
                    try:
                        snaps = cl.quotes.get_snapshots(batch)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("eltdx 板块快照批次失败：%s", exc)
                        continue
                    for s in (snaps if isinstance(snaps, list) else []):
                        last = getattr(s, "last_price", None) or 0
                        if not last:
                            continue
                        nm = names.get(s.full_code, "")
                        is_stat = self._is_stat_name(nm)
                        if kind == "concept" and is_stat:
                            continue
                        if kind == "stat" and not is_stat:
                            continue
                        rows.append({
                            "code": _to_qmt(s.full_code),
                            "name": nm,
                            "last": last,
                            "lastClose": getattr(s, "pre_close_price", None),
                            "change_pct": (round(s.change_pct, 2)
                                           if getattr(s, "change_pct", None) is not None else None),
                            "amount": getattr(s, "amount", None),
                            "volume": getattr(s, "total_hand", None),
                            "kind": "stat" if is_stat else kind,
                            # 口径标注（P1-5）：统计类板块的 last 是「家数」而非指数点位，
                            # 前端据此显示「家」而不是按涨跌幅染色误读为「北证涨了 27%」。
                            "metric": "count" if is_stat else "point",
                            "unit": "家" if is_stat else "",
                        })
                rows.sort(key=lambda r: (r.get("amount") if sort_by == "amount"
                                         else (r.get("change_pct") if r.get("change_pct") is not None
                                               else -999)) or 0, reverse=True)
                return rows[:limit]
            return self._use_client(_inner)
        return await asyncio.to_thread(_run)

    async def search_boards(self, name: str, limit: int = 8) -> list:
        """板块名称 → 代码精确匹配（P1-8 深链稳化）。

        个股页只拿到行业/概念「名称」，若前端靠 name.includes 模糊匹配板块榜，
        同名/近名板块（如「半导体」vs「半导体概念」）会命中错项，榜单未加载时
        还会静默失败（点击无反应）。此处在服务端做精确 + 前缀 + 包含三级匹配，
        返回 [{code,name,kind,metric}]，前端据 code 精确选中。
        """
        name = (name or "").strip()
        if not name:
            return []

        def _run():
            def _inner(cl):
                codes, names = self._load_board_codes(cl)
                seg2kind = {self._BOARD_SEG_INDUSTRY: "industry",
                            self._BOARD_SEG_CONCEPT: "concept"}
                scored = []
                for c in codes:
                    nm = names.get(c, "")
                    if not nm:
                        continue
                    if nm == name:
                        score = 0                      # 完全一致
                    elif nm.startswith(name) or name.startswith(nm):
                        score = 1                      # 前缀/包含
                    elif name in nm:
                        score = 2                      # 名称包含
                    else:
                        continue
                    is_stat = self._is_stat_name(nm)
                    scored.append((score, {
                        "code": _to_qmt(c),
                        "name": nm,
                        "kind": "stat" if is_stat else seg2kind.get(c[2:5], "concept"),
                        "metric": "count" if is_stat else "point",
                        "unit": "家" if is_stat else "",
                    }))
                scored.sort(key=lambda x: (x[0], -len(x[1]["name"])))
                return [r for _, r in scored[:limit]]
            return self._use_client(_inner)
        res = await asyncio.to_thread(_run)
        return res or []

    async def get_board_constituents(self, code: str, limit: int = 50, page: int = 0) -> dict:
        """板块成分股（f10.theme_market req_id=200744）。

        实测列：N001=市场(0深/1沪/2北) N002=代码 N003=名称 N004=涨跌幅 N005=价格；
        成分总数在 table[0] 的 total_num 字段。
        page/limit 直接透传 f10 分页（page_size=limit），大板块（电子 548 只）
        可翻页取全量，不再只显示前 50 只（P0-3）。
        """
        ec = _to_eltdx(code)
        page = max(0, int(page or 0))

        def _run():
            def _inner(cl):
                resp = cl.f10.theme_market(ec, req_id="200744", page=page, page_size=limit)
                tables = list(getattr(resp, "tables", ()) or ())
                total = None
                for cell in (tables[0].rows if tables else ()):
                    if isinstance(cell, dict) and "total_num" in cell:
                        try:
                            total = int(cell["total_num"])
                        except (TypeError, ValueError):
                            total = None
                mkt = {"0": "SZ", "1": "SH", "2": "BJ"}
                out = []
                if len(tables) > 1:
                    for row in tables[1].rows:
                        if not isinstance(row, dict):
                            continue
                        c6 = str(row.get("N002") or "")
                        if not c6:
                            continue
                        out.append({
                            "code": f"{c6}.{mkt.get(str(row.get('N001') or ''), 'SH')}",
                            "name": row.get("N003") or "",
                            "change_pct": _f(row.get("N004")),
                            "last": _f(row.get("N005")),
                        })
                has_more = bool(total is not None and (page + 1) * limit < total)
                return {"code": code, "total": total, "items": out[:limit],
                        "page": page, "page_size": limit, "has_more": has_more,
                        "page_count": (int((total + limit - 1) / limit)
                                       if total else (1 if out else 0))}
            return self._use_client(_inner)
        return await asyncio.to_thread(_run)

    async def get_board_kline(self, code: str, period: str = "1d", count: int = 60) -> list:
        """板块 / 指数 K 线（必须 kind="index"，否则 ProtocolError）。"""
        ec = _to_eltdx(code)
        p = to_eltdx_period(period)

        def _run():
            def _inner(cl):
                bars = cl.bars.get(ec, period=p, count=count, kind="index")
                bl = getattr(bars, "bars", bars) or []
                out = []
                for b in bl:
                    t = getattr(b, "time", None)
                    ts = t.strftime("%Y%m%d") if hasattr(t, "strftime") else str(t)
                    raw_vol = getattr(b, "volume_lots", None)
                    out.append({
                        "time": ts,
                        "open": getattr(b, "open", None),
                        "high": getattr(b, "high", None),
                        "low": getattr(b, "low", None),
                        "close": getattr(b, "close", None),
                        # T6 单位归一：手 → 股（契约单一真源，见 Bar.volume_unit）
                        "volume": (raw_vol * 100) if raw_vol is not None else None,
                        "volume_unit": "shares",
                    })
                return out
            return self._use_client(_inner)
        return await asyncio.to_thread(_run)

    async def get_etf_list(self, limit: int = 0) -> list:
        """ETF 清单（代码段 51/56/58/15/16 过滤 + 名称）。

        ⚠️ P0-2 截断缺陷：清单按 code 升序排列，若直接取前 N 条（旧行为
        `cached[:limit]`），会把 56 段（沪行业/主题）、58 段（沪跨境/商品）整段
        切掉 —— 前端这两个分组 tab 点开永远是空表。因此默认返回全量，
        仅当调用方显式给出小于全量的 limit 时才截断。
        """
        cached = self._board_cache_get("etfs", self._ETF_TTL)
        if cached:
            # 并入名称表（幂等）：旧缓存升级后首次访问即让 ETF 全局可搜
            self._merge_into_name_map(cached)
            return self._cap_etfs(cached, limit)

        def _run():
            def _inner(cl):
                out = []
                # codes.all_etfs() 只返回代码字符串；codes.etfs(mk) 才带 .name，
                # 否则 ETF 清单会全行空名称（界面上不可辨识）。
                for mk in ("sh", "sz", "bj"):
                    try:
                        items = cl.codes.etfs(mk)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("eltdx ETF 清单 %s 加载失败：%s", mk, exc)
                        continue
                    for it in items:
                        fc = getattr(it, "full_code", "") or ""
                        c6 = fc[2:]
                        if c6[:2] not in ("51", "56", "58", "15", "16"):
                            continue
                        out.append({"code": _to_qmt(fc), "name": getattr(it, "name", "") or ""})
                return out
            return self._use_client(_inner)
        etfs = await asyncio.to_thread(_run)
        # 名称兜底：个别市场清单缺失时，用已加载的通用名称表补全
        if any(not e["name"] for e in etfs):
            await self._ensure_name_map()
            nm = self.__class__._name_map
            for it in etfs:
                if not it["name"]:
                    it["name"] = nm.get(it["code"], "")
        etfs.sort(key=lambda e: e["code"])
        self._board_cache_set("etfs", etfs)
        # 并入名称表（幂等）并持久化：此后 ETF 搜索/详情/解析全链路离线可用
        self._merge_into_name_map(etfs)
        return self._cap_etfs(etfs, limit)

    @staticmethod
    def _cap_etfs(items: list, limit: int) -> list:
        """limit<=0 或 ≥ 全量时返回全量，否则截断（避免整段丢失，见 get_etf_list 说明）。"""
        try:
            n = int(limit or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0 or n >= len(items):
            return items
        return items[:n]

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
                vol_cmp = None
                try:
                    vc = cl.minutes.aux(ec, kind="volume_comparison")
                    pts = getattr(vc, "points", None) or []
                    if pts:
                        lastp = pts[-1]
                        a = getattr(lastp, "series_a", None)   # 前一日同时段量
                        b = getattr(lastp, "series_b", None)   # 当日量
                        vol_cmp = round(b / a, 2) if a else None
                except Exception as exc:  # noqa: BLE001
                    log.debug("eltdx 量比获取失败 %s: %s", code, exc)
                return {
                    "code": code,
                    "inside": inside,       # 内盘累计（手）
                    "outside": outside,     # 外盘累计（手）
                    "net": (outside - inside) if (inside is not None and outside is not None) else None,
                    "strength": points,     # 分钟级主买/主卖
                    "volume_ratio": vol_cmp,  # 量比
                    "est": False,           # 真实口径，非估算
                    "ts": datetime.now().isoformat(timespec="seconds"),
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
        # 昨收按日缓存：避免每次 stock-info / quote 富化都打快照
        pre_close = self._preclose_map.get(code)
        if pre_close is None or (time.time() - self.__class__._preclose_ts.get(code, 0)) > _PRECLOSE_TTL:
            ec = _to_eltdx(code)
            def _run():
                def _inner(cl):
                    snaps = cl.quotes.get_snapshots([ec])
                    s = snaps[0] if isinstance(snaps, list) else snaps.get(ec)
                    return s.pre_close_price if s else None
                return self._use_client(_inner)
            try:
                pre_close = await asyncio.to_thread(_run)
            except Exception as exc:  # noqa: BLE001
                log.warning("eltdx 昨收获取失败 %s: %s", code, exc)
                pre_close = self._preclose_map.get(code)
            if pre_close is not None:
                self.__class__._preclose_map[code] = pre_close
                self.__class__._preclose_ts[code] = time.time()
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
            nm = (e.get("name") or "").strip()
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
