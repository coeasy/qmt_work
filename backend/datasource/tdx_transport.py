"""easy_tdx 传输门面：把 easy_tdx（MIT 许可）适配为 eltdx 风格的七族门面接口。

背景（2026-10-04）：
    eltdx 库为「ELTDX Research-Only License」（禁止商用，见
    docs/THIRD_PARTY_LICENSES.md 3.2）。改用 **MIT 许可**的 easy_tdx 作为 TDX
    传输层后，商用部署不再被许可阻断。本模块提供与 ``eltdx.TdxClient``
    **门面同形**的客户端类（quotes/bars/minutes/codes/trades/helpers/f10 七族），
    使 ``eltdx_source.py`` / ``eltdx_boards.py`` 适配层**零改动**切换传输；
    eltdx 库降级为「easy_tdx 缺失时的可选回退」。

easy_tdx 实测口径（2026-10-04，easy_tdx 1.1.0，改动前必读）：
    - **MAC 协议**（端口 7709，``MacClient``）承载全部行情：快照 / K 线（含复权）/
      分时 / 逐笔 / 板块 / 资金流 / 所属板块。指数与个股在同一命名空间自动路由
      （SH ``000001`` 返回上证指数，实测 ✓）。
    - **标准协议**（``TdxClient``）对当前服务端仅证券清单枚举可用
      （``get_security_count`` / ``get_security_list``）；其五档行情返回空、
      K 线命令抛 ``TdxDecodeError``（服务端协议演进 > 客户端解析器），
      ``get_security_list_all()`` 会**挂死**（实测 6 分钟无返回）——绝不可用。
    - MAC 快照**无五档盘口**：``buy_levels`` / ``sell_levels`` 恒为空列表
      （诚实降级，前端盘口显示「暂无」）；内外盘 / 换手 / 均价来自
      ``get_symbol_info``（仅单标的快照时补取，避免批量请求翻倍）。
    - 北交所（BJ）MAC 返回空 → 快照 / K 线抛明确异常（诚实降级，不伪造）。

单位契约（★ 与 eltdx 适配层对接的关键，改一侧必须同步另一侧）：
    - MAC **K 线** ``vol`` = **股**（amount/vol ≈ 价格，日线与分钟线实测一致）
      → 门面输出 ``volume_lots = vol / 100``（手），适配层维持「手 ×100 = 股」不变；
    - MAC **快照** ``vol`` / ``symbol_info.vol`` = **手**（amount/vol/100 ≈ 价格）；
    - MAC **逐笔** ``vol`` = 手（与 eltdx ``TradeTick.volume`` 同口径）；
    - MAC **分时** ``vol`` = 股（上证指数 14:59 分钟量 ~9480 万，若为手则 94.8 亿
      股/分钟不可能成立）。

超时与重试：适配层 ``_use_client`` 已有「复用失败 → 重建连接重试一次」语义，
本门面保持「连接惰性建立 + 自动重连」（MacClient auto_reconnect），不额外重试。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date as _date, datetime as _datetime
from types import SimpleNamespace as _NS
from typing import Optional

log = logging.getLogger("qmt_work.datasource.tdx_transport")

try:  # 主传输：easy_tdx（MIT，商用安全）
    from easy_tdx import MacClient, Market, Period, Adjust
    _HAS_EASY_TDX = True
except ImportError:  # pragma: no cover - 取决于部署环境是否安装
    MacClient = None  # type: ignore[assignment,misc]
    Market = Period = Adjust = None  # type: ignore[assignment,misc]
    _HAS_EASY_TDX = False

# ---------------- 常量 ----------------
_ENUM_TTL = 3600.0        # 证券清单枚举缓存（秒）：清单日内基本不变
_HOST_TTL = 1800.0        # 最优主机缓存（秒）：避免每次重连都全量 ping
_SNAP_BATCH = 80          # MAC 批量快照单次上限（官方约束）
_STD_PAGE = 1000          # 标准协议清单分页大小

#: eltdx 门面周期串 → easy_tdx Period 枚举（与 datasource/periods.py 契约一致）
_PERIOD_MAP = {}
if _HAS_EASY_TDX:
    _PERIOD_MAP = {
        "1m": Period.MIN_1, "5m": Period.MIN_5, "15m": Period.MIN_15,
        "30m": Period.MIN_30, "60m": Period.MIN_60,
        "day": Period.DAILY, "week": Period.WEEKLY,
        "month": Period.MONTHLY, "year": Period.YEARLY,
    }

_MARKET_BY_PFX = {"sh": Market.SH, "sz": Market.SZ, "bj": Market.BJ} if _HAS_EASY_TDX else {}

# 各交易所代码段分类（与 eltdx_utils.is_index_code 同一口径，见其 docstring）
_STOCK_PFX = {
    "sh": ("600", "601", "603", "605", "688", "689"),
    "sz": ("000", "001", "002", "003", "300", "301", "302"),
    "bj": ("43", "83", "87", "92"),
}
_INDEX_PFX = {
    "sh": ("000", "880", "881", "999"),
    "sz": ("399",),
    "bj": ("899",),
}
_ETF_PFX = {"sh": ("51", "56", "58"), "sz": ("15", "16"), "bj": ()}

#: 逐笔 bs_flag → 方向（0=主买 1=主卖 2=中性 5=盘后固定价成交，实测分布）
_BS_FLAG_SIDE = {0: "buy", 1: "sell", 2: "neutral", 5: "neutral"}


def is_available() -> bool:
    """easy_tdx 是否可导入。"""
    return _HAS_EASY_TDX


def active_backend() -> str:
    """当前 TDX 传输后端：easy_tdx（MIT）或 none。"""
    return "easy_tdx" if _HAS_EASY_TDX else "none"


def _pfx6(ec: str) -> tuple[str, str]:
    """'sh600519' -> ('sh', '600519')；非法输入抛 ValueError。"""
    ec = (ec or "").strip().lower()
    if len(ec) < 8 or ec[:2] not in _MARKET_BY_PFX:
        raise ValueError(f"非法 TDX 代码：{ec!r}")
    return ec[:2], ec[2:]


def _classify(mk: str, code: str) -> str:
    """按交易所分别判定证券类别（index/fund/stock/other）。"""
    if code.startswith(_INDEX_PFX.get(mk, ())):
        return "index"
    if code.startswith(_ETF_PFX.get(mk, ())):
        return "fund"
    if code.startswith(_STOCK_PFX.get(mk, ())):
        return "stock"
    return "other"


def _to_dt(s) -> Optional[_datetime]:
    """easy_tdx datetime 列（str/Timestamp）→ datetime；失败 None。"""
    if isinstance(s, _datetime):
        return s
    try:
        return _datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return _datetime.strptime(str(s)[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _py(v):
    """numpy 标量 → Python 原生数值。

    ★ easy_tdx 返回 pandas DataFrame，``iterrows`` 产物全是 ``numpy.int64`` /
    ``numpy.float32`` —— 直接放进 API 响应会让 FastAPI 的 JSON 编码器炸出
    ``numpy.int64 object is not iterable``（实测 /market/board/moneyflow 500）。
    门面层统一转原生数值，适配层与路由零感知。
    """
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:  # noqa: BLE001
            return v
    return v


def _chg_pct(close, pre_close) -> Optional[float]:
    """涨跌幅（%）：分母为 0 / 缺失时 None，不伪造 0。"""
    try:
        c, p = float(close), float(pre_close)
        return round((c - p) / p * 100, 2) if p else None
    except (TypeError, ValueError):
        return None


class _EnumCache:
    """证券清单枚举（类级缓存）：名称 / 类别的唯一来源。

    ★ 性能实测（2026-10-04，改用 MAC 前的教训）：标准协议
    ``get_security_list`` 分页**每页恒定 ~8.3s**（服务端限速），全市场 56 页
    ≈ 8 分钟 —— 绝不可用于同步路径。MAC ``get_stock_quotes_list(Category.A)``
    按代码排序分页枚举全 A 股（沪深+北交 5577 只）**仅 ~2s** 且自带简称。

    ★ 为什么必须类级：适配层 ``_acquire_client`` 按 TTL（180s）重建门面实例，
    实例级缓存会每次重建都重新枚举全市场。
    """

    def __init__(self):
        self._rows: dict[str, dict] = {}   # full_code -> {name, category, code, mk}
        self._ts = 0.0
        self._lock = threading.Lock()

    def _ensure(self, timeout: float) -> dict[str, dict]:
        with self._lock:
            if self._rows and (time.time() - self._ts) < _ENUM_TTL:
                return self._rows
            from easy_tdx import Category, SortType, SortOrder
            rows: dict[str, dict] = {}
            with _MacHolder(timeout) as holder:
                cli = holder.client()
                start, guard = 0, 0
                while guard < 40000:
                    df = cli.get_stock_quotes_list(
                        Category.A, start=start, count=_SNAP_BATCH,
                        sort_type=SortType.CODE, sort_order=SortOrder.ASC)
                    n = 0 if df is None else len(df)
                    if n == 0:
                        break
                    for _, r in df.iterrows():
                        code = str(r.get("code") or "")
                        if len(code) != 6 or not code.isdigit():
                            continue
                        mkint = int(r.get("market", 1))
                        mk = "sz" if mkint == 0 else ("bj" if mkint == 2 else "sh")
                        rows[f"{mk}{code}"] = {
                            "code": code, "mk": mk,
                            "name": str(r.get("name") or ""),
                            "category": _classify(mk, code) if mk != "bj" else "stock",
                        }
                    start += n
                    guard += n
            # 指数/板块：MAC 板块清单（881/880，带名称）+ 主要宽基兜底名
            from datasource.eltdx_utils import _INDEX_FALLBACK_NAMES
            for fc, name in _board_names().items():
                rows.setdefault(fc, {"code": fc[2:], "mk": "sh",
                                     "name": name, "category": "index"})
            for code, name in _INDEX_FALLBACK_NAMES.items():
                num, _, mk = code.partition(".")
                mk = mk.lower() or "sh"
                rows.setdefault(f"{mk.lower()}{num}",
                                {"code": num, "mk": mk.lower(), "name": name,
                                 "category": "index"})
            if rows:
                self._rows = rows
                self._ts = time.time()
                log.info("easy_tdx 证券清单枚举完成：%d 条（MAC 分类枚举，缓存 %ds）",
                         len(rows), _ENUM_TTL)
            return self._rows

    def get(self, timeout: float = 10.0) -> dict[str, dict]:
        try:
            return self._ensure(timeout)
        except Exception as exc:  # noqa: BLE001
            log.warning("easy_tdx 证券清单枚举失败：%s", exc)
            return dict(self._rows)

    def stocks(self) -> list[tuple[str, str, str]]:
        """A 股清单 [(code6, mk, full_code)]（按 market+code 稳定排序）。"""
        rows = self.get()
        out = [(v["code"], v["mk"], fc) for fc, v in rows.items()
               if v["category"] == "stock"]
        out.sort(key=lambda x: (x[1], x[0]))
        return out


_enum_cache = _EnumCache()

#: MAC 板块清单缓存（code->name + 代码列表），进程级
_board_cache: dict = {}
_board_lock = threading.Lock()


def _board_names() -> dict[str, str]:
    """MAC 板块指数 {sh881xxx/sh880xxx: 名称}（进程级缓存 1h）。"""
    now = time.time()
    with _board_lock:
        if _board_cache and (now - _board_cache.get("_ts", 0)) < _ENUM_TTL:
            return _board_cache["names"]
    names: dict[str, str] = {}
    try:
        from easy_tdx import BoardType
        with MacClient.from_best_host() as cli:
            for bt in (BoardType.HY, BoardType.GN, BoardType.FG, BoardType.DQ):
                try:
                    df = cli.get_board_list(bt)
                except Exception as exc:  # noqa: BLE001
                    log.debug("easy_tdx 板块清单 %s 失败：%s", bt, exc)
                    continue
                if df is None or len(df) == 0:
                    continue
                for _, r in df.iterrows():
                    code = str(r.get("code") or "")
                    if code[:3] in ("881", "880"):
                        names[f"sh{code}"] = str(r.get("name") or "")
    except Exception as exc:  # noqa: BLE001
        log.debug("easy_tdx 板块清单加载失败：%s", exc)
    with _board_lock:
        _board_cache["names"] = names
        _board_cache["_ts"] = now
    return names


def _board_codes() -> list:
    return sorted(_board_names().keys())


# ---------------- ETF 清单持久化 + 后台清扫 ----------------
_ETF_TTL = 7 * 86400.0
_etf_sweep_started = threading.Event()


def _etf_cache_path():
    from datasource.eltdx_utils import _cache_dir
    return _cache_dir() / "tdx_etf_list.json"


def _load_etf_cache():
    """读 ETF 清单缓存；缺失/过期返回 None。"""
    import json
    try:
        p = _etf_cache_path()
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > _ETF_TTL:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) and data else None
    except (OSError, ValueError):
        return None


def _save_etf_cache(items: list) -> None:
    import json
    try:
        _etf_cache_path().write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8")
        log.info("easy_tdx ETF 清单已缓存：%d 条（%s）", len(items), _etf_cache_path().name)
    except OSError as exc:
        log.warning("easy_tdx ETF 清单落盘失败：%s", exc)


def _kick_etf_sweep() -> None:
    """后台线程清扫 ETF 清单（每进程至多一次；完成后落盘供后续读取）。"""
    if _etf_sweep_started.is_set():
        return
    _etf_sweep_started.set()

    def _run():
        try:
            fam = EasyTdxClient(10.0).codes
            items = fam._collect_etfs_sync()
            if items:
                _save_etf_cache(items)
            else:
                log.warning("easy_tdx ETF 清扫未取得数据（本轮放弃，下个进程重试）")
        except Exception as exc:  # noqa: BLE001
            log.warning("easy_tdx ETF 后台清扫失败：%s", exc)

    threading.Thread(target=_run, name="easytdx-etf-sweep", daemon=True).start()


# ---------------- 个股所属板块缓存（行业 + 题材共用） ----------------
_belong_cache: dict = {}
_belong_ts: dict = {}
_belong_lock = threading.Lock()
_BELONG_TTL = 3600.0


def _belong_of(owner: "EasyTdxClient", ec: str):
    """MAC belong_board 结果（缓存 1h）：[NS(board_code, board_name)]。"""
    now = time.time()
    if ec in _belong_cache and (now - _belong_ts.get(ec, 0)) < _BELONG_TTL:
        return _belong_cache[ec]
    mkt, code6 = _pfx6(ec)

    def _fetch(cli):
        return cli.get_belong_board(_MARKET_BY_PFX[mkt], code6)

    df = owner._with_mac(_fetch)
    topics = []
    if df is not None and len(df):
        for _, r in df.iterrows():
            nm = str(r.get("board_name") or "")
            if nm:
                topics.append(_NS(board_code=str(r.get("board_code") or ""),
                                  topic_name=nm))
    with _belong_lock:
        _belong_cache[ec] = topics
        _belong_ts[ec] = now
    return topics


class _MacHolder:
    """MAC 连接持有者：类级缓存最优主机，实例内惰性建连 + 自动重连。"""

    _host = None
    _host_ts = 0.0
    _host_lock = threading.Lock()

    def __init__(self, timeout: float = 8.0):
        self._timeout = timeout
        self._cli = None

    def _best_host(self):
        cls = type(self)
        now = time.time()
        with cls._host_lock:
            if cls._host and (now - cls._host_ts) < _HOST_TTL:
                return cls._host
        with MacClient.from_best_host() as probe:
            host = getattr(probe, "host", None)
        if host:
            with cls._host_lock:
                cls._host = host
                cls._host_ts = time.time()
        return host

    def client(self):
        if self._cli is not None:
            return self._cli
        host = self._best_host()
        self._cli = (MacClient(host=host, timeout=self._timeout) if host
                     else MacClient(timeout=self._timeout))
        self._cli.connect()
        return self._cli

    def invalidate(self) -> None:
        try:
            if self._cli is not None:
                self._cli.close()
        except Exception as exc:  # noqa: BLE001
            from core.errors import swallow
            # 关闭失败只影响本次连接回收；下次惰性重建会用新连接，无需上抛
            swallow(exc, why="MAC 连接关闭失败（将被惰性重建覆盖）")
        self._cli = None

    def __enter__(self):
        self.client()
        return self

    def __exit__(self, *exc):
        self.invalidate()
        return False

    def close(self) -> None:
        self.invalidate()


class EasyTdxClient:
    """与 eltdx ``TdxClient`` 门面同形的 easy_tdx 客户端（七族接口）。

    支持上下文管理器（``with EasyTdxClient(timeout=90) as cl``）与惰性建连，
    语义对齐适配层 ``_use_client`` 的「复用 / 失败重建」用法。
    """

    def __init__(self, timeout: float = 8.0):
        if not _HAS_EASY_TDX:
            raise ImportError(
                "easy_tdx 未安装，TDX 行情源不可用（MIT 许可，商用安全）。"
                "请安装 backend/requirements-optional.txt，或连接券商数据源。")
        self._timeout = timeout
        self._mac = _MacHolder(timeout)
        # ---- 七族门面（与 eltdx.TdxClient 同形）----
        self.quotes = _QuotesFamily(self)
        self.bars = _BarsFamily(self)
        self.minutes = _MinutesFamily(self)
        self.trades = _TradesFamily(self)
        self.codes = _CodesFamily(self)
        self.helpers = _HelpersFamily(self)
        self.f10 = _F10Family(self)

    # ---- 上下文 / 生命周期 ----
    def connect(self):
        self._mac.client()
        return self

    def ensure_connected(self):
        return self.connect()

    def close(self) -> None:
        self._mac.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        # 保持连接对象可被适配层复用：退出上下文仅断开 MAC 连接（下次惰性重连）
        self.close()
        return False

    # ---- 内部工具 ----
    def _mkt(self, ec: str):
        pfx, code6 = _pfx6(ec)
        return _MARKET_BY_PFX[pfx], code6

    def _with_mac(self, fn):
        """在 MAC 连接上执行 fn(cli)；连接级失败重建一次（适配层还有外层重试）。"""
        try:
            return fn(self._mac.client())
        except Exception:
            self._mac.invalidate()
            return fn(self._mac.client())


# ============================ quotes 族 ============================

class _QuotesFamily:
    def __init__(self, owner: EasyTdxClient):
        self._o = owner

    def list_by_category(self, category: str = "沪深A股", start: int = 0, count: int = 80):
        """A 股分页枚举（category 仅兼容签名：easy_tdx 统一为全 A 股清单）。"""
        stocks = _enum_cache.stocks()
        page = stocks[max(0, int(start)): max(0, int(start)) + max(1, int(count))]
        recs = [_NS(code=c, exchange=mk) for c, mk, _ in page]
        return _NS(records=recs, total=len(stocks))

    def get_snapshots(self, ecs: list):
        """批量快照。单标的时补取 symbol_info（内外盘/换手/均价）。

        ★ MAC 无五档：buy_levels/sell_levels 恒为空（诚实降级）。
        返回 list[NS]；无数据的标的**不伪造**（直接缺席，调用方按 None 处理）。
        """
        if not ecs:
            return []
        out: dict[str, _NS] = {}

        def _fetch(cli):
            for i in range(0, len(ecs), _SNAP_BATCH):
                batch = ecs[i:i + _SNAP_BATCH]
                pairs, keys = [], []
                for ec in batch:
                    mkt, code6 = self._o._mkt(ec)
                    pairs.append((mkt, code6))
                    keys.append(ec.lower())
                df = cli.get_stock_quotes(pairs)
                if df is None or len(df) == 0:
                    continue
                for _, r in df.iterrows():
                    ec = f"{('sh' if int(r.get('market', 1)) == 1 else 'sz')}{str(r.get('code') or '')}"
                    if ec not in keys:
                        continue
                    out[ec] = self._snap_from_row(ec, r)
            return out

        self._o._with_mac(_fetch)
        snaps = [out.get(str(ec).lower()) for ec in ecs]
        # 单标的快照补内外盘/换手/均价（moneyflow / instrument_detail 路径依赖）
        if len(ecs) == 1 and snaps and snaps[0] is not None:
            self._enrich_single(snaps[0])
        return snaps

    @staticmethod
    def _snap_from_row(ec: str, r) -> _NS:
        pre = _py(r.get("pre_close"))
        close = _py(r.get("close"))
        return _NS(
            full_code=ec,
            last_price=close,
            open_price=_py(r.get("open")),
            high_price=_py(r.get("high")),
            low_price=_py(r.get("low")),
            pre_close_price=pre,
            total_hand=_py(r.get("vol")),            # 手
            amount=_py(r.get("amount")),
            change_pct=_chg_pct(close, pre),
            vol_ratio=_py(r.get("vol_ratio")),       # 真实量比（MAC 快照自带）
            buy_levels=[], sell_levels=[],           # MAC 无五档（诚实降级）
            inside_dish=None, outer_disc=None,       # 批量路径无内外盘
            current_hand=_py(r.get("last_volume")),
            sum_buy_vol=None, sum_sell_vol=None,
        )

    def _enrich_single(self, snap: _NS) -> _NS:
        """单标的快照补内外盘/换手/均价（get_symbol_info，一次额外调用）。"""
        try:
            mkt, code6 = _pfx6(snap.full_code)

            def _fetch(cli):
                return cli.get_symbol_info(_MARKET_BY_PFX[mkt], code6)

            si = self._o._with_mac(_fetch)
            if si is not None and len(si):
                row = si.iloc[0]
                snap.inside_dish = _py(row.get("inside_volume"))    # 手
                snap.outer_disc = _py(row.get("outside_volume"))    # 手
                snap.current_hand = _py(row.get("vol"))
                snap.turnover = _py(row.get("turnover"))
                snap.avg_price = _py(row.get("avg"))
                si_time = row.get("time")
                if si_time is not None:
                    snap.trade_time = str(si_time)
        except Exception as exc:  # noqa: BLE001
            log.debug("easy_tdx symbol_info 补取失败 %s：%s", snap.full_code, exc)
        return snap


# ============================ bars 族 ============================

class _BarsFamily:
    def __init__(self, owner: EasyTdxClient):
        self._o = owner

    def get(self, ec: str, period: str = "day", count: int = 100,
            adjust: Optional[str] = None, kind: str = "stock"):
        """K 线（MAC 自动路由指数/个股，kind 仅兼容签名）。

        返回 NS(bars=[NS(time, open, high, low, close, volume_lots, amount)])；
        空数据抛 RuntimeError（调用方按「无数据」处理，不伪造）。
        """
        mkt, code6 = self._o._mkt(ec)
        per = _PERIOD_MAP.get(period)
        if per is None:
            raise ValueError(f"easy_tdx 不支持周期 {period!r}（契约见 datasource/periods.py）")
        adj = (Adjust.NONE if not adjust else
               {"qfq": Adjust.QFQ, "hfq": Adjust.HFQ}.get(str(adjust).lower(), Adjust.NONE))
        n = max(1, int(count or 100))

        def _fetch(cli):
            return cli.get_stock_kline(mkt, code6, per, count=n, adjust=adj)

        df = self._o._with_mac(_fetch)
        bars = []
        if df is not None and len(df):
            for _, r in df.iterrows():
                t = _to_dt(r.get("datetime"))
                vol = _py(r.get("vol"))
                bars.append(_NS(
                    time=t,
                    open=_py(r.get("open")), high=_py(r.get("high")),
                    low=_py(r.get("low")), close=_py(r.get("close")),
                    # MAC K 线 vol=股 → volume_lots=手（适配层维持 ×100=股 契约）
                    volume_lots=(vol / 100.0) if vol is not None else None,
                    amount=_py(r.get("amount")),
                ))
        if not bars:
            raise RuntimeError(f"easy_tdx 未返回 {ec} {period} K 线（可能为该源不支持的市场）")
        return _NS(bars=bars)


# ============================ minutes 族 ============================

class _MinutesFamily:
    def __init__(self, owner: EasyTdxClient):
        self._o = owner

    def today(self, ec: str):
        return self._fetch(ec, None)

    def history(self, ec: str, d: "_date"):
        return self._fetch(ec, d)

    def _fetch(self, ec: str, d: Optional[_date]):
        """分时（1 分钟线）。返回 NS(points, trading_date, prev_close, open_price)；
        空数据返回 None（适配层按「无数据」处理）。"""
        mkt, code6 = self._o._mkt(ec)

        def _fetch(cli):
            if d is not None:
                return cli.get_tick_chart(mkt, code6, date=int(d.strftime("%Y%m%d")))
            return cli.get_tick_chart(mkt, code6)

        df = self._o._with_mac(_fetch)
        if df is None or len(df) == 0:
            return None
        pts = []
        for _, r in df.iterrows():
            t = r.get("time")
            label = t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)[:5]
            pts.append(_NS(
                time_label=label,
                price=_py(r.get("price")),
                avg_price=_py(r.get("avg")),
                volume=_py(r.get("vol")),   # 股（实测口径见模块 docstring）
            ))
        # 昨收/今开：从快照补（一次调用）；缺失时 None（前端按缺省渲染）
        pre_close = open_price = td = None
        try:
            snaps = self._o.quotes.get_snapshots([ec])
            s = snaps[0] if snaps else None
            if s is not None:
                pre_close = s.pre_close_price
                open_price = s.open_price
        except Exception as exc:  # noqa: BLE001
            log.debug("easy_tdx 分时昨收补取失败 %s：%s", ec, exc)
        # 交易日：以 symbol_info 时间戳为准（休市日取的是最近交易日数据，
        # 必须如实标注，否则用户把上一交易日分时当成今日行情）
        try:
            mkt2, code6b = _pfx6(ec)

            def _fetch_td(cli):
                return cli.get_symbol_info(_MARKET_BY_PFX[mkt2], code6b)

            si = self._o._with_mac(_fetch_td)
            if si is not None and len(si):
                t = si.iloc[0].get("time")
                td = t.date() if hasattr(t, "date") else None
        except Exception as exc:  # noqa: BLE001
            log.debug("easy_tdx 分时交易日补取失败 %s：%s", ec, exc)
        if td is None:
            td = d if d is not None else _date.today()
        return _NS(points=pts, trading_date=td, prev_close=pre_close,
                   open_price=open_price)

    def aux(self, ec: str, kind: str = "buy_sell_strength"):
        """★ easy_tdx 不提供分钟级买卖力道/量比对比序列 → 显式异常（诚实降级）。

        适配层捕获后 strength=[] / volume_ratio=None（不伪造）；快照量比
        （vol_ratio）经 get_quote 字段透出，前端仍可展示真实量比。
        """
        raise RuntimeError(
            f"easy_tdx 传输不提供 minutes.aux({kind!r})——诚实降级，不伪造序列")


# ============================ trades 族 ============================

class _TradesFamily:
    def __init__(self, owner: EasyTdxClient):
        self._o = owner

    def today(self, ec: str, start: int = 0, count: int = 60):
        """当日逐笔（start=0 为最新一页，页内时间升序——与 eltdx 语义一致）。"""
        mkt, code6 = self._o._mkt(ec)
        n = max(1, int(count or 60))
        fetch_n = n if int(start or 0) <= 0 else min(int(start) + n, 5000)

        def _fetch(cli):
            return cli.get_transactions(mkt, code6, count=fetch_n)

        df = self._o._with_mac(_fetch)
        rows = [] if df is None or len(df) == 0 else df.tail(n)
        ticks = []
        for _, r in rows.iterrows():
            t = r.get("time")
            label = t.strftime("%H:%M:%S") if hasattr(t, "strftime") else str(t)
            side = _BS_FLAG_SIDE.get(int(r.get("bs_flag") or 2), "neutral")
            ticks.append(_NS(
                price=_py(r.get("price")),
                volume=_py(r.get("vol")),       # 手（与 eltdx TradeTick 同口径）
                time_label=label,
                side=side,
                order_count=int(r.get("trade_count") or 0),
                event_kind="trade",
            ))
        # 交易日：以 symbol_info 的时间戳为准（休市日返回最近交易日，诚实标注）
        td = None
        try:
            with _MacHolder(self._o._timeout) as h:
                si = h.client().get_symbol_info(mkt, code6)
            if si is not None and len(si):
                t = si.iloc[0].get("time")
                td = t.date() if hasattr(t, "date") else None
        except Exception as exc:  # noqa: BLE001
            log.debug("easy_tdx 逐笔交易日补取失败 %s：%s", ec, exc)
        return _NS(actual_trades=ticks, ticks=ticks, trading_date=td)


# ============================ codes 族 ============================

class _CodesFamily:
    def __init__(self, owner: EasyTdxClient):
        self._o = owner

    def all(self, mk: str):
        """某市场清单 → [NS(category, full_code, name)]。

        股票来自 MAC 分类枚举；指数/板块来自 MAC 板块清单 + 宽基兜底名。
        ★ 注意：本实现**不覆盖**基金（ETF）与冷门指数 —— 前者见 ``etfs``，
        后者由适配层 ``_INDEX_FALLBACK_NAMES`` 与快照名兜底。
        """
        rows = _enum_cache.get()
        out = []
        for fc, v in rows.items():
            if v["mk"] == mk:
                out.append(_NS(category=v["category"], full_code=fc, name=v["name"]))
        out.sort(key=lambda x: x.full_code)
        return out

    def all_indices(self) -> list:
        """全部指数/板块代码（'sh000001' 形态）= MAC 板块指数 + 宽基兜底。"""
        rows = _enum_cache.get()
        out = [fc for fc, v in rows.items() if v["category"] == "index"]
        out.extend(_board_codes())
        return sorted(set(out))

    def etfs(self, mk: str):
        """ETF 清单（代码+名称）。

        ★ MAC 分类枚举**不含基金**；标准协议分页枚举实测每页恒定 ~8.3s
        （服务端限速），全市场清扫 ≈ 8 分钟 —— 因此：
        1. 结果持久化到 ``tdx_etf_list.json``（TTL 7 天），优先读缓存；
        2. 缓存缺失时**后台线程**清扫一次（每进程至多一次），完成后落盘；
        3. 就绪前本方法返回空列表（诚实降级：宁可空，不阻塞请求、不伪造）。
        """
        cached = _load_etf_cache()
        if cached is not None:
            return [_NS(full_code=e["full_code"], name=e["name"])
                    for e in cached if e.get("mk") == mk]
        _kick_etf_sweep()
        return []

    def _collect_etfs_sync(self, deadline_s: float = 600.0) -> list:
        """标准协议全市场清扫 ETF（后台线程专用；deadline 防挂死）。"""
        from easy_tdx import TdxClient, Market
        out = []
        deadline = time.time() + deadline_s
        with TdxClient(timeout=10) as std:
            for mk_name, mk in (("sh", Market.SH), ("sz", Market.SZ), ("bj", Market.BJ)):
                start = 0
                while start < 60000 and time.time() < deadline:
                    try:
                        df = std.get_security_list(mk, start)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("easy_tdx ETF 清扫 %s@%d 失败：%s", mk_name, start, exc)
                        break
                    if df is None or len(df) == 0:
                        break
                    for _, r in df.iterrows():
                        code = str(r.get("code") or "")
                        if len(code) == 6 and code.isdigit() and _classify(mk_name, code) == "fund":
                            out.append({"full_code": f"{mk_name}{code}",
                                        "mk": mk_name, "name": str(r.get("name") or "")})
                    start += len(df)
        return out


# ============================ helpers 族 ============================

class _HelpersFamily:
    def __init__(self, owner: EasyTdxClient):
        self._o = owner

    def stock_profile_table(self, ecs: list, include_security: bool = True,
                            include_finance: bool = False):
        """批量证券简称（来自清单枚举缓存，无额外网络）。"""
        rows = _enum_cache.get()
        out = []
        for ec in ecs:
            v = rows.get(str(ec).lower())
            if v is not None:
                out.append(_NS(full_code=str(ec).lower(), name=v["name"]))
        return _NS(rows=out)

    def stock_topics(self, num: str):
        """个股所属板块（题材/概念，MAC belong_board，缓存 1h）。"""
        ec = num if len(num) >= 8 else f"sh{num}"
        return _NS(topics=_belong_of(self._o, ec))

    def daily_shares(self, ecs: list):
        """股本（MAC 快照 total_shares/float_shares，单位**万股** → 股）。"""
        if not ecs:
            return _NS(rows=[])

        def _fetch(cli):
            pairs = []
            for ec in ecs:
                mkt, code6 = _pfx6(ec)
                pairs.append((_MARKET_BY_PFX[mkt], code6))
            out = []
            for i in range(0, len(pairs), _SNAP_BATCH):
                df = cli.get_stock_quotes(pairs[i:i + _SNAP_BATCH])
                if df is None or len(df) == 0:
                    continue
                for _, r in df.iterrows():
                    mk = "sh" if int(r.get("market", 1)) == 1 else "sz"
                    out.append((f"{mk}{r.get('code')}", r))
            return out

        raw = self._o._with_mac(_fetch)
        rows = []
        for ec, r in raw:
            try:
                tot = r.get("total_shares")
                flt = r.get("float_shares")
                rows.append(_NS(
                    full_code=ec,
                    total_shares=(float(tot) * 1e4) if tot is not None else None,
                    circulating_shares=(float(flt) * 1e4) if flt is not None else None,
                    free_float_shares=None,            # MAC 无自由流通股本（诚实置空）
                    trade_date="",
                    share_source="easy_tdx",
                ))
            except (TypeError, ValueError):
                continue
        return _NS(rows=rows)

    def daily_price_limits(self, ecs: list):
        """涨跌停价（MAC 快照 buy_price_limit/sell_price_limit，真实口径）。"""
        if not ecs:
            return _NS(rows=[])

        def _fetch(cli):
            pairs = []
            for ec in ecs:
                mkt, code6 = _pfx6(ec)
                pairs.append((_MARKET_BY_PFX[mkt], code6))
            out = []
            for i in range(0, len(pairs), _SNAP_BATCH):
                df = cli.get_stock_quotes(pairs[i:i + _SNAP_BATCH])
                if df is None or len(df) == 0:
                    continue
                for _, r in df.iterrows():
                    mk = "sh" if int(r.get("market", 1)) == 1 else "sz"
                    out.append((f"{mk}{r.get('code')}", r))
            return out
        raw = self._o._with_mac(_fetch)
        rows = []
        for ec, r in raw:
            up, dn, pre = _py(r.get("buy_price_limit")), _py(r.get("sell_price_limit")), _py(r.get("pre_close"))
            rows.append(_NS(
                full_code=ec,
                name=str(r.get("name") or ""),
                pre_close=pre,
                limit_up_price=up, limit_down_price=dn,
                limit_ratio_pct=_chg_pct(up, pre),
                limit_rule="easy_tdx price limit",
                limit_status=None, trade_date="",
            ))
        return _NS(rows=rows)


# ============================ f10 族 ============================

class _F10Family:
    def __init__(self, owner: EasyTdxClient):
        self._o = owner

    def stock_score(self, num: str, section: str = "pf"):
        """行业（N012，通达信行业）：由所属板块推导（board_code 881xxx = 行业）。

        eltdx 走 F10 网关取行业；easy_tdx 无该网关，但 belong_board 的
        行业板块成员关系给出**同一口径**的行业归属（881xxx 为通达信行业）。
        取不到（如北交所）时 rows=[]（诚实降级，适配层 industry=""）。
        """
        ec = num if len(num) >= 8 else f"sh{num}"
        try:
            topics = _belong_of(self._o, ec)
        except Exception as exc:  # noqa: BLE001
            log.debug("easy_tdx 行业推导失败 %s：%s", ec, exc)
            return _NS(rows=[])
        for t in topics:
            if str(t.board_code or "").startswith("881"):
                return _NS(rows=[{"N012": t.topic_name}])
        return _NS(rows=[])

    def theme_market(self, ec: str, req_id: str = "200744", page: int = 0,
                     page_size: int = 50):
        """板块成分股（MAC board_members，替代 eltdx F10 网关）。

        返回 eltdx theme_market 同形结构：tables[0].rows 含 total_num；
        tables[1].rows 每行 N001=市场 N002=代码 N003=名称 N004=涨跌幅 N005=价格。
        """
        mkt, code6 = _pfx6(ec)

        def _fetch(cli):
            return cli.get_board_members(code6)

        df = self._o._with_mac(_fetch)
        total = 0 if df is None else len(df)
        items = []
        if total:
            lo, hi = max(0, int(page)) * int(page_size), (max(0, int(page)) + 1) * int(page_size)
            for _, r in df.iloc[lo:hi].iterrows():
                mkint = int(r.get("market", 1))
                pre = r.get("pre_close")
                close = r.get("close")
                items.append({
                    "N001": "0" if mkint == 0 else ("2" if mkint == 2 else "1"),
                    "N002": str(r.get("code") or ""),
                    "N003": str(r.get("name") or ""),
                    "N004": _chg_pct(_py(close), _py(pre)),
                    "N005": _py(close),
                })
        return _NS(tables=(_NS(rows=[{"total_num": total}]), _NS(rows=items)))


__all__ = ["EasyTdxClient", "is_available", "active_backend"]
