"""多源行情数据路由与注册中心（DataSourceManager）。

目标：把原先散落在 market.py / tools / sync / main 中、写死的
`source: auto/broker/eltdx` 逻辑收敛到单一抽象，使「新增数据源」只需要在
registry 注册一个 DataSource 实现，路由 / 回退 / 健康检查全部自动生效。

设计要点：
- 第三方补充源（eltdx / 未来 akshare / tushare / eastmoney…）以 DataSource
  子类形式注册进 `plugins`（name -> 实例），按 name 路由。
- 券商直连作为内置伪源 `broker`，经 broker_manager 取当前/指定连接，不实现
  交易、只暴露行情/基础数据接口（与 DataSource 对齐）。
- `auto` 模式按 `auto_chain` 顺序回退（默认 broker -> eltdx）；任意已注册
  源的 name 也可被 `source=` 显式指定。
- 所有方法统一返回「带 source 字段的归一化 dict」或 None（不可用），调用方
  据此决定 503 文案，绝不让某源异常击穿整体。
- 治理：每个源调用都套 `asyncio.wait_for` 超时 + 简单熔断（连续失败 N 次后
  冷却一段时间，避免慢/坏源拖垮 auto 链或打爆远端）。
"""
import asyncio
import time
from typing import Optional

from datasource.base import DataSource
from datasource.board import classify_board, limit_ratio
from datasource.instrument import with_exchange_suffix
from datasource.periods import (
    UnknownPeriodError,
    adjust_allowed_periods,
    normalize_period,
)
from xtquant_client.base import BrokerError

log = __import__("logging").getLogger("qmt_work.datasource.registry")

# 单源调用超时与熔断参数
_PER_SOURCE_TIMEOUT = 8.0
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN = 30.0


def _contentless_detail(det: Optional[dict], code: str) -> bool:
    """判定一份合约详情是否「成功但无内容」（空壳）。

    部分券商 SDK（xtquant 的合约/F10 详情）在部分客户端版本上只回空壳：
    name 回退成代码本身、涨跌停/昨收/行业/概念全为空，**且不抛异常**。
    auto 链若把它当成功直接返回，eltdx 里明明有完整画像（中文名/行业/
    概念/涨跌停价）却永远轮不到——界面表现就是「个股资料只有代码、无名称、
    无涨跌停」，而用户无从判断是没数据还是坏了。

    判定口径（任一成立即视为有内容）：
      · name 存在且不等于代码本身；
      · 昨收/涨停/跌停 任一有值；
      · 行业或概念非空。
    """
    if not det:
        return True
    name = (det.get("name") or "").strip()
    bare = (code or "").split(".")[0].upper()
    # name 为空或回退成代码本身（空壳）→ 无有效名称，强制视为无内容，
    # 让 auto 链回退 eltdx 补全中文名（否则界面只剩代码、无名称）。
    if not name or name.upper() in (bare, (code or "").upper()):
        return True
    for key in ("pre_close", "high_limit", "low_limit",
                "up_limit_price", "down_limit_price"):
        if det.get(key) is not None:
            return False
    if det.get("industry") or det.get("concepts"):
        return False
    return True


class DataSourceUnavailable(Exception):
    """请求的数据源均不可用（auto 全链失败 / 显式源缺失）。"""


class UnsupportedDataSource(DataSourceUnavailable):
    """调用方显式指定了未注册的数据源。

    这是配置/能力错误，不是允许回退到 auto 的运行时缺数；单独类型便于 REST/MCP
    入口返回明确的 400，而不会把用户要求的 source 静默改成另一来源。
    """

    def __init__(self, source: str, available: list[str]):
        self.source = source
        self.available = available
        super().__init__(
            f"未注册的数据源: {source}；可用数据源: {', '.join(available) or '无'}"
        )


class _BoundBrokerSource:
    """把单个券商连接包装成 DataSource 形态（仅行情/基础数据，无交易）。

    内置到 manager，不对外注册；为 broker 连接动态创建，conn_id 已绑定。
    """

    name = "broker"
    capabilities = frozenset({"quote", "kline", "instrument_detail"})

    def __init__(self, bridge):
        self._b = bridge

    async def get_quote(self, code: str) -> Optional[dict]:
        try:
            q = await self._b.call(self._b.gateway.get_quote, code)
        except BrokerError:
            return None
        if isinstance(q, dict) and isinstance(q.get("code"), int):
            return None  # _call 返回了 err(503) dict
        return q

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: Optional[str] = None) -> Optional[list]:
        try:
            bars = await self._b.call(self._b.gateway.get_kline, code, period, count)
        except BrokerError:
            return None
        if isinstance(bars, dict) and bars.get("code"):
            return None
        return bars

    async def get_instrument_detail(self, code: str) -> Optional[dict]:
        try:
            det = await self._b.call(self._b.gateway.get_instrument_detail, code)
        except (BrokerError, AttributeError):
            # AttributeError：适配器未实现该方法（如 BridgeAdapter 历史缺失
            # get_instrument_detail）。降级为不可用，避免击穿 auto 链 / 误触发熔断。
            return None
        if isinstance(det, dict) and isinstance(det.get("code"), int):
            return None
        return det

    async def get_stock_list(self) -> Optional[list]:
        # 券商侧无统一全市场股票列表接口；auto 链中跳过。
        return None


class DataSourceManager:
    """多源行情路由中心（进程级单例，见 get_manager）。"""

    def __init__(self):
        self._plugins: dict[str, DataSource] = {}
        self._broker_factory = None  # conn_id -> _BoundBrokerSource | None
        # auto 回退顺序：先券商，再各补充源。可在运行时经 set_auto_chain 调整。
        self._auto_chain: list[str] = ["broker"]
        self._breakers: dict[str, dict] = {}

    # ---------- 注册 ----------
    def register(self, source: DataSource) -> "DataSourceManager":
        self._plugins[source.name] = source
        if source.name not in self._auto_chain:
            self._auto_chain.append(source.name)
        return self

    def register_broker(self, factory) -> "DataSourceManager":
        """factory(conn_id) -> _BoundBrokerSource | None（无连接返回 None）。"""
        self._broker_factory = factory
        return self

    def set_auto_chain(self, chain: list[str]) -> "DataSourceManager":
        """显式设定 auto 回退顺序；未在 chain 中的已注册源不参与 auto。"""
        self._auto_chain = [c for c in chain if c == "broker" or c in self._plugins]
        return self

    def list_sources(self) -> list[str]:
        out = []
        if self._broker_factory is not None:
            out.append("broker")
        out.extend(self._plugins.keys())
        return out

    def describe_sources(self) -> dict[str, dict]:
        """返回 active provider 的能力画像，供前端/MCP capability router 使用。"""
        out: dict[str, dict] = {}
        if self._broker_factory is not None:
            out["broker"] = {
                "provider": "broker", "active": True,
                "capabilities": sorted(_BoundBrokerSource.capabilities),
            }
        for name, src in self._plugins.items():
            manifest = (src.capability_manifest()
                        if hasattr(src, "capability_manifest") else {
                            "provider": name, "capabilities": []})
            out[name] = {**manifest, "active": True}
        return out

    # ---------- 熔断 ----------
    def _breaker(self, name: str) -> dict:
        b = self._breakers.get(name)
        if b is None:
            b = {"failures": 0, "open_until": 0.0}
            self._breakers[name] = b
        return b

    def _source_allowed(self, name: str) -> bool:
        b = self._breaker(name)
        if b["open_until"] and time.time() < b["open_until"]:
            return False
        return True

    def _record_failure(self, name: str) -> None:
        b = self._breaker(name)
        b["failures"] += 1
        if b["failures"] >= _BREAKER_THRESHOLD:
            b["open_until"] = time.time() + _BREAKER_COOLDOWN
            log.warning("数据源 %s 熔断（连续 %d 次失败），冷却 %.0fs",
                        name, b["failures"], _BREAKER_COOLDOWN)

    def _record_success(self, name: str) -> None:
        b = self._breaker(name)
        if b["failures"] or b["open_until"]:
            b["failures"] = 0
            b["open_until"] = 0.0

    async def _call_source(self, name: str, coro):
        """对单个源调用施加超时 + 熔断；成功返回结果，失败/熔断/超时返回 None。

        熔断打开时直接丢弃未启动的协程（close），避免「coroutine never awaited」警告。
        """
        if not self._source_allowed(name):
            if coro is not None and hasattr(coro, "close"):
                coro.close()
            return None
        try:
            res = await asyncio.wait_for(coro, timeout=_PER_SOURCE_TIMEOUT)
            self._record_success(name)
            return res
        except Exception as exc:  # noqa: BLE001
            self._record_failure(name)
            log.warning("数据源 %s 调用失败（已计入熔断）%s: %s",
                        name, type(exc).__name__, exc)
            return None

    # ---------- 内部工具 ----------
    def _broker(self, conn_id: Optional[str]):
        if self._broker_factory is None:
            return None
        return self._broker_factory(conn_id)

    def _validate_source(self, source: str) -> str:
        """校验 source；只有 auto 才允许回退链，显式未知源必须立即报错。"""
        want = (source or "auto").strip().lower()
        if want in ("auto", "broker") or want in self._plugins:
            return want
        raise UnsupportedDataSource(want, self.list_sources())

    def validate_source(self, source: str = "auto") -> str:
        """公开的 source 能力校验入口，供缓存/REST 编排层复用。"""
        return self._validate_source(source)

    @staticmethod
    def _merge_quote(raw: dict, code: str, board: dict, detail: dict,
                     src: str, industry: str = "", concepts=None) -> dict:
        raw = dict(raw)
        raw["name"] = detail.get("name") or raw.get("name") or code
        raw["exchange"] = detail.get("exchange") or board.get("exchange")
        raw["high_limit"] = detail.get("high_limit") or detail.get("up_limit_price")
        raw["low_limit"] = detail.get("low_limit") or detail.get("down_limit_price")
        raw["pre_close"] = detail.get("pre_close") or raw.get("lastClose")
        raw["board"] = board.get("board")
        if industry:
            raw["industry"] = industry
        if concepts:
            raw["concepts"] = concepts
        # 涨跌额/幅：broker 原始快照不带 change/change_pct（eltdx 已在源内计算），
        # 统一从 last/昨收 真实推导；任一缺失或分母为 0 时置 None（不伪造）。
        if raw.get("change") is None or raw.get("change_pct") is None:
            last, pre = raw.get("last"), raw.get("pre_close")
            if last is not None and pre:
                raw["change"] = round(float(last) - float(pre), 4)
                raw["change_pct"] = round((float(last) - float(pre)) / float(pre) * 100, 2)
            else:
                raw["change"] = None
                raw["change_pct"] = None
        raw["source"] = src
        return raw

    async def _detail_for(self, name: str, src, code: str) -> dict:
        det = await self._call_source(name, src.get_instrument_detail(code))
        return det or {}

    # ---------- 行情快照 ----------
    async def get_quote(self, code: str, source: str = "auto",
                        conn_id: Optional[str] = None) -> Optional[dict]:
        source = self._validate_source(source)
        # 代码规范化（唯一入口）：券商只认 600519.SH，eltdx 名称表也以带后缀代码为键。
        # 传裸代码会让券商静默返空、eltdx 查不到中文名——两处都表现为「无数据」。
        code = with_exchange_suffix(code)
        board = classify_board(code)

        async def _from_broker():
            b = self._broker(conn_id)
            if b is None:
                return None
            raw = await self._call_source("broker", b.get_quote(code))
            if raw is None:
                return None
            det = await self._detail_for("broker", b, code)
            # 券商实时价最准，但其合约详情可能是空壳（无中文名/涨跌停）。
            # 名称缺失会一路透到界面（个股名显示为一串代码），因此这里用补充源
            # 富化画像；价格仍以券商为准，富化失败也不影响行情本身。
            if _contentless_detail(det, code):
                for pname in self._plugins:
                    pdet = await self._call_source(
                        pname, self._plugins[pname].get_instrument_detail(code))
                    if not _contentless_detail(pdet, code):
                        det = pdet
                        break
            return self._merge_quote(raw, code, board, det, "broker")

        async def _from_plugin(name: str):
            src = self._plugins.get(name)
            if src is None:
                return None
            raw = await self._call_source(name, src.get_quote(code))
            if raw is None:
                return None
            det = await self._detail_for(name, src, code)
            return self._merge_quote(raw, code, board, det, name,
                                     industry=det.get("industry") or "",
                                     concepts=det.get("concepts") or [])

        if source == "broker":
            return await _from_broker()
        if source in self._plugins:
            return await _from_plugin(source)
        # auto：按 auto_chain 依次尝试
        for name in self._auto_chain:
            if name == "broker":
                q = await _from_broker()
            else:
                q = await _from_plugin(name)
            if q is not None:
                return q
        return None

    # ---------- 合约基础信息 ----------
    async def get_instrument_detail(self, code: str, source: str = "auto",
                                    conn_id: Optional[str] = None) -> Optional[dict]:
        source = self._validate_source(source)
        # 同上：统一补交易所后缀，否则 eltdx 名称表（键为 600519.SH）查不到中文名，
        # 券商侧也只回空壳 —— 界面就会把个股名显示成一串代码。
        code = with_exchange_suffix(code)
        board = classify_board(code)
        base = {
            "code": code,
            "name": code,
            "exchange": board.get("exchange"),
            "board": board.get("board"),
            "high_limit": None,
            "low_limit": None,
            "pre_close": None,
            "industry": "",
            "concepts": [],
        }

        async def _broker_detail():
            b = self._broker(conn_id)
            if b is None:
                return None
            det = await self._detail_for("broker", b, code)
            if not det:
                return None
            return {
                **base,
                "name": det.get("name") or code,
                "exchange": det.get("exchange") or base["exchange"],
                "high_limit": det.get("up_limit_price"),
                "low_limit": det.get("down_limit_price"),
                "pre_close": det.get("pre_close"),
                "source": "broker",
            }

        async def _plugin_detail(name: str):
            src = self._plugins.get(name)
            if src is None:
                return None
            det = await self._call_source(name, src.get_instrument_detail(code))
            if not det:
                return None
            return {
                **base,
                "name": det.get("name") or code,
                "exchange": det.get("exchange") or base["exchange"],
                "high_limit": det.get("high_limit"),
                "low_limit": det.get("low_limit"),
                "pre_close": det.get("pre_close"),
                "industry": det.get("industry") or "",
                "concepts": det.get("concepts") or [],
                "source": name,
            }

        if source == "broker":
            return await _broker_detail()
        if source in self._plugins:
            return await _plugin_detail(source)
        last: Optional[dict] = None
        for name in self._auto_chain:
            det = await (_broker_detail() if name == "broker" else _plugin_detail(name))
            if det is None:
                continue
            # 「成功但无内容」不等于成功：券商空壳详情会让 eltdx 的完整画像
            # （中文名/行业/概念/涨跌停）永远轮不到，界面只剩代码。
            if not _contentless_detail(det, code):
                return det
            last = det
        # 全链都无内容：返回最后一个空壳（至少带上交易所/板块），保持旧行为不退化
        return last

    # ---------- 历史 K 线 ----------
    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        source: str = "auto", conn_id: Optional[str] = None,
                        adjust: Optional[str] = None) -> tuple[Optional[list], Optional[str]]:
        """返回 (bars, source_name)；bars 为 None 表示无可用源。

        复权（qfq/hfq）券商不支持，自动改走支持复权的补充源（eltdx）。
        """
        source = self._validate_source(source)
        # 周期判定引用契约常量（第三份硬编码已消除）。
        # 注意：旧常量不含 canonical "1mo"，导致月线+qfq 时不会改走支持复权的补充源。
        try:
            _canon = normalize_period(period)
        except UnknownPeriodError:
            _canon = None
        if source == "auto" and adjust in ("qfq", "hfq") and _canon in adjust_allowed_periods():
            for name in self._auto_chain:
                if name == "broker":
                    continue
                src = self._plugins.get(name)
                if src is None:
                    continue
                bars = await self._call_source(name, src.get_kline(code, period, count, adjust))
                if bars:
                    return bars, name
            return None, None

        async def _broker_kline():
            b = self._broker(conn_id)
            if b is None:
                return None
            return await self._call_source("broker", b.get_kline(code, period, count, adjust))

        if source == "broker":
            bars = await _broker_kline()
            return (bars, "broker") if bars is not None else (None, None)
        if source in self._plugins:
            src = self._plugins.get(source)
            bars = await self._call_source(source, src.get_kline(code, period, count, adjust))
            return (bars, source) if bars is not None else (None, None)
        for name in self._auto_chain:
            if name == "broker":
                bars = await _broker_kline()
            else:
                src = self._plugins.get(name)
                if src is None:
                    continue
                bars = await self._call_source(name, src.get_kline(code, period, count, adjust))
            if bars is not None:
                return bars, name
        return None, None

    # ---------- 当日分时（仅补充源提供；券商 SDK 无分时接口） ----------
    async def get_minutes(self, code: str, trading_date: Optional[str] = None,
                          source: str = "auto") -> Optional[dict]:
        """当日分时曲线（价格+均价+分钟量）。按 auto 链遍历补充源（跳过券商），
        全部无数据返回 None。"""
        source = self._validate_source(source)
        for name in self._auto_chain:
            if name == "broker":
                continue
            if source not in ("auto", name):
                continue
            src = self._plugins.get(name)
            if src is None or not hasattr(src, "get_minutes"):
                continue
            res = await self._call_source(name, src.get_minutes(code, trading_date))
            if res and res.get("points"):
                return res
        return None

    # ---------- 指数 / 板块 / ETF / 资金流（东财对标能力，仅补充源提供） ----------
    def _sup_chain(self, source: str = "auto") -> list:
        """按 source 解析补充源候选链（P1-7：source 参数必须真正生效）。

        - "broker"：返回空链 → 方法返回 None，绝不悄悄回退 TDX 公共行情，
          否则「仅券商」的降级语义失效，用户会误以为看的是券商数据。
        - 具体源名（如 eltdx）：只用该源。
        - "auto"/空：按 _auto_chain 顺序回退（跳过券商）。
        """
        want = self._validate_source(source)
        if want == "broker":
            return []
        if want in ("", "auto"):
            return [n for n in self._auto_chain if n != "broker"]
        return [want]

    async def _first_supported(self, method: str, *args, source: str = "auto",
                               **kwargs):
        """按 source 解析的链找到第一个实现该方法的补充源并返回 (结果, 源名)。"""
        for name in self._sup_chain(source):
            src = self._plugins.get(name)
            if src is None or not hasattr(src, method):
                continue
            res = await self._call_source(name, getattr(src, method)(*args, **kwargs))
            if res:
                return res, name
        return None, None

    async def get_boards(self, kind: str = "industry", sort_by: str = "pct",
                         limit: int = 50, source: str = "auto") -> tuple[Optional[list], Optional[str]]:
        """板块指数榜单（真实板块指数快照）。返回 (rows, source_name)。"""
        return await self._first_supported("get_boards", kind, sort_by, limit, source=source)

    async def get_board_constituents(self, code: str, limit: int = 50, page: int = 0,
                                     source: str = "auto") -> tuple[Optional[dict], Optional[str]]:
        """板块成分股。返回 ({code,total,items,page,has_more}, source_name)。"""
        return await self._first_supported("get_board_constituents", code, limit, page, source=source)

    async def get_board_kline(self, code: str, period: str = "1d", count: int = 60,
                              source: str = "auto") -> tuple[Optional[list], Optional[str]]:
        """板块 / 指数 K 线（kind='index'）。"""
        return await self._first_supported("get_board_kline", code, period, count, source=source)

    async def search_boards(self, name: str, limit: int = 8,
                            source: str = "auto") -> tuple[Optional[list], Optional[str]]:
        """板块名称→代码匹配（深链稳化）。返回 (rows, source_name)。"""
        return await self._first_supported("search_boards", name, limit, source=source)

    async def get_etf_list(self, limit: int = 0,
                           source: str = "auto") -> tuple[Optional[list], Optional[str]]:
        """ETF 清单（代码 + 名称）。limit<=0 返回全量，避免整段截断。"""
        return await self._first_supported("get_etf_list", limit, source=source)

    async def get_moneyflow(self, code: str,
                            source: str = "auto") -> tuple[Optional[dict], Optional[str]]:
        """个股资金流（真实内外盘口径）。"""
        return await self._first_supported("get_moneyflow", code, source=source)

    async def get_share_capital(self, codes: list,
                                source: str = "auto") -> tuple[dict, Optional[str]]:
        """流通股本（换手率分母）。无数据返回空 dict（调用方显式降级）。"""
        for name in self._sup_chain(source):
            src = self._plugins.get(name)
            if src is None or not hasattr(src, "get_share_capital"):
                continue
            res = await self._call_source(name, src.get_share_capital(codes))
            if res:
                return res, name
        return {}, None

    async def get_price_limits(self, codes: list,
                               source: str = "auto") -> tuple[dict, Optional[str]]:
        """涨跌停价。无数据返回空 dict。"""
        for name in self._sup_chain(source):
            src = self._plugins.get(name)
            if src is None or not hasattr(src, "get_price_limits"):
                continue
            res = await self._call_source(name, src.get_price_limits(codes))
            if res:
                return res, name
        return {}, None

    # ---------- 全市场股票列表（名称来源） ----------
    async def get_stock_list(self, source: str = "auto",
                             conn_id: Optional[str] = None) -> Optional[list]:
        source = self._validate_source(source)
        if source in self._plugins:
            return await self._call_source(source, self._plugins[source].get_stock_list())
        if source == "broker":
            b = self._broker(conn_id)
            if b is None:
                return None
            lst = await self._call_source("broker", b.get_stock_list())
            return lst
        for name in self._auto_chain:
            if name == "broker":
                b = self._broker(conn_id)
                if b:
                    lst = await self._call_source("broker", b.get_stock_list())
                    if lst:
                        return lst
                continue
            src = self._plugins.get(name)
            if src is None:
                continue
            lst = await self._call_source(name, src.get_stock_list())
            if lst:
                return lst
        return None

    def lookup_name(self, code: str) -> Optional[str]:
        """O(1) 名称兜底：遍历已注册补充源的名称缓存（无网络）。"""
        for src in self._plugins.values():
            fn = getattr(src, "lookup_name", None)
            if fn is None:
                continue
            nm = fn(code)
            if nm:
                return nm
        return None

    # ---------- 搜索（优先索引化，退化全量过滤） ----------
    async def search_stocks(self, q: str, limit: int = 20) -> list:
        """按代码 / 中文名模糊搜索；优先用补充源自带的索引化 search，退化全量过滤。"""
        q = (q or "").strip()
        if not q:
            return []
        for name in self._auto_chain:
            if name == "broker":
                continue
            src = self._plugins.get(name)
            if src is None or not self._source_allowed(name):
                continue
            fn = getattr(src, "search", None)
            if fn is None:
                continue
            res = await self._call_source(name, fn(q, limit))
            if res:
                return res
        # 兜底：全量列表线性过滤
        lst = await self.get_stock_list()
        if not lst:
            return []
        ql = q.lower()
        exact = [x for x in lst if x.get("code") == q or (x.get("name") or "") == q]
        contain = [x for x in lst if x not in exact
                   and (ql in (x.get("code") or "").lower()
                        or ql in (x.get("name") or "").lower())]
        return (exact + contain)[:limit]

    # ---------- 预热 / 健康检查 ----------
    async def warmup_all(self) -> None:
        for src in self._plugins.values():
            fn = getattr(src, "warmup", None)
            if fn is None:
                continue
            try:
                await fn()
            except Exception as exc:  # noqa: BLE001
                log.warning("数据源 %s 预热失败: %s", src.name, exc)

    async def health(self) -> dict:
        out = {}
        b = self._broker(None) if self._broker_factory else None
        br = self._breaker("broker")
        open_until = br.get("open_until") or 0.0
        tripped = bool(open_until) and time.time() < open_until
        note = "券商已连接" if b else "未连接券商客户端"
        if tripped:
            note = f"熔断冷却中（剩余 {max(0, int(open_until - time.time()))}s）"
        out["broker"] = {"available": (b is not None) and not tripped,
                         "note": note, "breaker_failures": br.get("failures", 0)}
        for name, src in self._plugins.items():
            fn = getattr(src, "is_ready", None)
            ready = fn() if fn else True
            br = self._breaker(name)
            open_until = br.get("open_until") or 0.0
            tripped = bool(open_until) and time.time() < open_until
            note = "就绪" if ready else "未就绪（首请求惰性加载）"
            if tripped:
                note = f"熔断冷却中（剩余 {max(0, int(open_until - time.time()))}s）"
            out[name] = {"available": ready and not tripped,
                         "note": note, "breaker_failures": br.get("failures", 0)}
        return out


# ---------------- 进程级单例 ----------------
_manager: Optional[DataSourceManager] = None


def _default_broker_factory(conn_id: Optional[str]):
    from core.state import state
    if state.broker_manager is None:
        return None
    b = state.broker_manager.bridge(conn_id)
    return _BoundBrokerSource(b) if b is not None else None


def get_manager() -> DataSourceManager:
    """惰性构建并注册所有已知数据源的单例。"""
    global _manager
    if _manager is not None:
        return _manager
    m = DataSourceManager()
    m.register_broker(_default_broker_factory)
    # 注册 eltdx（若可用）；缺失依赖时静默跳过，系统回退到纯券商模式。
    try:
        from datasource.eltdx_source import EltdxSource
        source = EltdxSource()
        m.register(source)
        from datasource.providers import provider_catalog
        provider_catalog.register(source)
    except Exception as exc:  # noqa: BLE001
        log.warning("eltdx 数据源注册跳过（依赖缺失）：%s", exc)
    from datasource.providers import provider_catalog
    from datasource.public_sources import SinaSource, TencentSource
    for source in (SinaSource(), TencentSource()):
        m.register(source)
        provider_catalog.register(source)
    from datasource.optional_sources import BaoStockSource, TstdxSource
    for source_cls in (TstdxSource, BaoStockSource):
        if source_cls.available():
            source = source_cls()
            m.register(source)
            provider_catalog.register(source)
    m.set_auto_chain(["broker", "eltdx"])
    _manager = m
    return _manager


# 统一入口：get_hub() 返回全局 DataSourceManager 单例（旧 app.datasource.manager
# 兼容层已删除，全部引用收敛到本模块）。MarketDataHub 为历史别名，勿在新代码使用。
get_hub = get_manager
MarketDataHub = DataSourceManager
MarketDataUnavailable = DataSourceUnavailable


__all__ = ["DataSourceManager", "DataSourceUnavailable", "UnsupportedDataSource", "get_manager",
           "get_hub", "MarketDataHub", "MarketDataUnavailable",
           "classify_board", "limit_ratio"]
