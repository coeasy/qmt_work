"""eltdx 适配器：通达信(TDX)公共行情源，直连行情服务器，无需任何券商客户端。

作为「无券商连接」时的行情 / 基础数据补充（DataSource 实现）。覆盖：
- 实时盘口快照（含五档 buy_levels / sell_levels）
- 历史 K 线（日/周/月/分钟，支持前/后复权）
- 合约基础信息（名称来自 A 股列表、昨收来自快照、涨跌停按板块推算）
- 全市场股票列表（含中文名，解决 stock-info 中文名缺失）
- 行业 / 概念（通达信行业 N012 + 题材概念，来自 F10 网关）

⚠️ 许可证：eltdx 仅允许个人学习 / 协议研究 / 非商业研究使用，禁止商业使用。
本适配器仅将其作为非商业场景下的行情补充源，不伪造任何数据。

本地缓存：股票名称表 / 行业概念表持久化到 <data_dir> 下的 JSON，
重启后优先读本地缓存（带 TTL），避免首拉 / 每次重启都走网络。
"""
import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from eltdx import TdxClient

from app.datasource.base import DataSource
from app.datasource.board import classify_board, limit_ratio

log = logging.getLogger("qmt_work.datasource.eltdx")

_EXCH_PFX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}

# 本地缓存文件名（与 app.db / 单实例锁同目录）
_NAME_CACHE = "stock_names.json"
_INDUSTRY_CACHE = "stock_industry.json"
# 名称表缓存有效期（天）；行业概念表不随行情变动，缓存不过期（仅增量补充新代码）
_NAME_TTL_DAYS = 1

# ---------------- 公共行情连接治理（防止连接风暴 / 被限速封 IP） ----------------
# 复用单个 TdxClient（带 TTL 回收）；全局最小请求间隔限流；并发连接数上限。
_CLIENT_TTL = 180.0          # 复用连接最长存活（秒），到期重建
_MIN_CALL_INTERVAL = 0.12    # 两次 TDX 调用间最小间隔（秒），全局限流
_MAX_CONCURRENT = 6          # 同时打到 TDX 的连接数上限（信号量）
_PRECLOSE_TTL = 86400.0      # 昨收缓存有效期（秒）：盘中不变，按日缓存即可


def _cache_dir() -> Path:
    """返回运行时数据目录（与 app.db 同目录）。"""
    try:
        from app.config import settings
        d = Path(str(settings.db_path)).parent
    except Exception:  # noqa: BLE001
        d = Path("data")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _name_cache_path() -> Path:
    return _cache_dir() / _NAME_CACHE


def _industry_cache_path() -> Path:
    return _cache_dir() / _INDUSTRY_CACHE


def _num(code: str) -> str:
    """qmt 代码 600519.SH -> 6 位数字 600519（F10/题材接口用纯数字）。"""
    return (code or "").split(".")[0].strip()


def _to_eltdx(code: str) -> str:
    """qmt 代码 600519.SH -> eltdx 代码 sh600519。"""
    c = (code or "").upper().strip()
    if "." in c:
        num, exch = c.split(".", 1)
    else:
        num, exch = c, "SH"
    pfx = _EXCH_PFX.get(exch, "sh")
    return f"{pfx}{num}"


def _to_qmt(eltdx_code: str) -> str:
    """eltdx 代码 sh600519 -> qmt 代码 600519.SH。"""
    pfx = eltdx_code[:2].lower()
    num = eltdx_code[2:]
    exch = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(pfx, "SH")
    return f"{num}.{exch}"


def _map_period(p: str) -> str:
    """qmt 周期 -> eltdx 周期（day/week/month/year/1m/5m/15m/30m/60m）。"""
    p = (p or "1d").lower()
    m = {
        "1d": "day", "day": "day", "d": "day",
        "1w": "week", "week": "week", "w": "week",
        "1m": "1m", "min": "1m", "minute": "1m", "1min": "1m",
        "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m",
        "1mon": "month", "month": "month", "mo": "month",
        "1y": "year", "year": "year", "y": "year",
    }
    return m.get(p, "day")


def _map_adjust(a: Optional[str]) -> Optional[str]:
    """qmt 复权 -> eltdx 复权（qfq/hfq，其余视为不复权）。"""
    if not a:
        return None
    a = a.lower()
    if a in ("qfq", "hfq"):
        return a
    return None


def _load_json_cache(path: Path, max_age_days: Optional[float] = None) -> Optional[dict]:
    """读取 JSON 缓存；max_age_days 不为 None 时超期返回 None。"""
    try:
        if not path.exists():
            return None
        if max_age_days is not None:
            age = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).days
            if age > max_age_days:
                return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("读取本地缓存失败 %s: %s", path.name, exc)
        return None


def _save_json_cache(path: Path, data: dict) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("写入本地缓存失败 %s: %s", path.name, exc)


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
        """加载名称表到类级 _name_map（所有实例共享，原地更新避免实例级 shadow）。"""
        # 检查类级 dict 是否已加载
        if self.__class__._name_map:
            return
        with self._lock:
            # 双重检查
            if self.__class__._name_map:
                return
            # 1) 优先读本地缓存（带 TTL）
            cached = _load_json_cache(_name_cache_path(), max_age_days=_NAME_TTL_DAYS)
            if cached:
                self.__class__._name_map.update(cached)
                log.info("eltdx 名称表已从本地缓存加载：%d 只", len(cached))
                return
            # 2) 缓存缺失/过期 → 走网络拉全市场列表
            def _run():
                with TdxClient(timeout=12) as cl:
                    lst = cl.helpers.latest_stock_list()
                    rows = getattr(lst, "rows", lst) or []
                    m = {}
                    for r in rows:
                        ex = (getattr(r, "exchange", "sh") or "sh").upper()
                        cd = getattr(r, "code", "")
                        nm = getattr(r, "name", "")
                        if cd:
                            m[f"{cd}.{ex}"] = nm
                    return m
            try:
                fetched = await asyncio.to_thread(_run)
                self.__class__._name_map.update(fetched)
                _save_json_cache(_name_cache_path(), fetched)
                self.__class__._rebuild_search_index()
                log.info("eltdx 名称表已拉取并缓存：%d 只", len(fetched))
            except Exception as exc:  # noqa: BLE001
                log.warning("eltdx 名称表拉取失败，尝试回退本地过期缓存：%s", exc)
                stale = _load_json_cache(_name_cache_path())
                if stale:
                    self.__class__._name_map.update(stale)
                    self.__class__._rebuild_search_index()

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
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }
            return self._use_client(_inner)
        return await asyncio.to_thread(_run)

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
                    out.append({
                        "time": ts,
                        "open": b.open, "high": b.high, "low": b.low,
                        "close": b.close, "volume": b.volume_lots, "amount": b.amount,
                    })
                return out
            return self._use_client(_inner)
        return await asyncio.to_thread(_run)

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

    async def search(self, q: str, limit: int = 20) -> list:
        """按代码 / 中文名模糊搜索（基于本地小写索引，完全离线，不依赖券商连接）。

        精确匹配（code 或全名）优先，其次包含匹配；上限 limit 条。
        """
        q = (q or "").strip()
        if not q:
            return []
        await self._ensure_name_map()
        idx = self.__class__._search_index
        ql = q.lower()
        exact = idx.get(ql) or []
        contain = []
        if len(exact) < limit:
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


__all__ = ["EltdxSource", "_to_eltdx", "_to_qmt"]
