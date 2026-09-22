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
import inspect
import time
from typing import Optional

from core.clock import bar_date  # K 线交易日格式唯一入口（V11 R13）
from datasource.base import DataSource, EXT_DETAIL_KEYS
from datasource.board import classify_board, limit_ratio
from datasource.instrument import with_exchange_suffix
from datasource.periods import (
    UnknownPeriodError,
    adjust_allowed_periods,
    normalize_period,
)
from xtquant_client.base import BrokerError

log = __import__("logging").getLogger("qmt_work.datasource.registry")


def _local_name(code: str) -> str:
    """本地名称兜底（O(1)、无网络）：**唯一实现**在 `datasource/eltdx_utils.py`。

    刻意用函数内 import：`registry` 处于导入链上游（`eltdx_source` 依赖
    `eltdx_utils`），模块级 import 容易踩循环导入。Python 会缓存模块，
    每次调用的额外开销只是一次 dict 查找。
    """
    try:
        from datasource.eltdx_utils import lookup_name
        return lookup_name(code)
    except Exception:  # noqa: BLE001  名称查不到不该让行情链路整体失败
        return ""


def bars_last_date(bars) -> str:
    """取一批 K 线里**最后一根**的交易日（``"YYYYMMDD"``；无法解析返回 ``""``）。

    ★ 不假设 bars 已按时间升序：实测券商与在线源都升序，但补洞/合并路径不保证，
    直接取 ``bars[-1]`` 会拿错。这里对所有行归一化后取最大，代价 O(n)，
    n 通常 <= 320，可接受。
    """
    if not bars:
        return ""
    latest = ""
    for b in bars:
        if isinstance(b, dict):
            raw = b.get("time")
        else:
            raw = getattr(b, "time", None)
        d = bar_date(raw)
        if d and d > latest:
            latest = d
    return latest

# 单源调用超时与熔断参数
_PER_SOURCE_TIMEOUT = 8.0
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN = 30.0

# 合约详情富化的时间预算（仅在券商详情为空壳时才会用到，见 DataSourceManager._enrich_detail）。
# ★ 2026-09-14 实测：券商对 ETF / 指数常回空壳详情（无名称/无涨跌停），
#   此时原实现会顺序遍历全部插件源且**不设总时限**。一旦某源不可达
#   （实测 sina 经本机代理 403，单次约 5.5s），整次 get_quote 就被拖到 5.6s ——
#   实测同一接口 股票 0.02s / ETF·指数 5.6s 的巨大差异即由此而来。
#   详情只是「增强项」：拿不到也必须让行情本身照常返回。
_DETAIL_ENRICH_BUDGET = 2.0     # 遍历全部插件源的总预算
_DETAIL_ENRICH_ATTEMPT = 1.0    # 单源切片（不可达源快速出局，给后续源留机会）
# 详情富化结果缓存：合约画像日内基本不变，同一代码在 TTL 内只付一次网络代价。
_DETAIL_CACHE_TTL = 300.0
_DETAIL_CACHE_MAX = 4096


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


def _accepts_kline_range(src) -> bool:
    """这个源能不能**按日期区间**取 K 线（声明 + 签名双重确认）。

    判据有两层，缺一不可：

    1. ``supports_kline_range`` 显式声明（**声明式能力**，不是 try/except 试探）；
    2. ``get_kline`` 的签名**真的收** ``start``/``end``。

    第 2 层不是多余的：签名不收区间时，``src.get_kline(..., start=...)`` 会在
    **协程创建处**抛 ``TypeError``（在 ``_call_source`` 的 try 之外），于是整条
    链在这一步就断了 —— 表面现象是「全量回补永远不生效」，而日志里只有一条
    debug 级异常，极难定位。这里提前把「声明了却不认」的源判为不支持，
    让它老老实实降级，而不是把整条链拖死。
    """
    if not getattr(src, "supports_kline_range", False):
        return False
    fn = getattr(src, "get_kline", None)
    if fn is None:
        return False
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):     # 内建/C 扩展无签名 ⇒ 无法确认，按不支持处理
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True                      # **kwargs 能收下区间
    return "start" in params and "end" in params


class _BoundBrokerSource:
    """把单个券商连接包装成 DataSource 形态（仅行情/基础数据，无交易）。

    内置到 manager，不对外注册；为 broker 连接动态创建，conn_id 已绑定。
    """

    name = "broker"
    # V11 R6：补 kline_qfq/kline_hfq —— get_kline 明确透传 adjust（QMT 经 dividend_type
    # 参数化），契约链也把 broker 列在复权链首位，此前声明漏了两个变体。
    # 不含 stock_list：get_stock_list 恒返回 None（券商侧无全市场列表接口）。
    capabilities = frozenset({"quote", "kline", "kline_qfq", "kline_hfq",
                              "instrument_detail"})
    #: ★ 唯一支持**按日期区间**取 K 线的源（V11 §5.3 P0-3 III）。
    #: 迅投 ``get_market_data(start_time=, end_time=)`` + ``download_history_data``
    #: 都能按日期区间工作，因此「全量回补」可以**逐年向前翻页**；
    #: 而 eltdx / 免费在线源只接受 ``count``（最近 N 根），无法指定区间 ——
    #: 声明这个标志，让回补逻辑只走真正做得到的源，而不是「假装翻页、其实一直
    #: 拿最近 120 根」。声明式能力优于 try/except TypeError。
    supports_kline_range = True

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
                        adjust: Optional[str] = None,
                        start: str = "", end: str = "") -> Optional[list]:
        try:
            # 透传 adjust：QMT 复权经 dividend_type 参数化（quotes.get_kline），
            # 使 broker 能参与 qfq/hfq 链（D9 v1.3）。
            # ``start``/``end``（YYYYMMDD 或 YYYY-MM-DD，空 = 不限）供全量回补
            # 按区间翻页；普通调用不传，行为与改造前完全一致。
            bars = await self._b.call(self._b.gateway.get_kline, code, period, count,
                                      adjust=adjust, start=start or "", end=end or "")
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

    async def get_sector_stocks(self, sector: str = "沪深A股") -> Optional[list[str]]:
        """板块成分股代码列表（实测「沪深A股」返回 5224 只）。

        ★ 为什么需要它：``_sup_chain`` 对 ``sector`` 能力**刻意排除 broker**
        （历史上券商未实现 ``get_board_constituents``），但券商其实有
        ``gateway.get_sector_stocks`` —— 同一个能力，K 线同步那条路走得通
        （``app/routes/market.py::_get_sector_stocks``），选股池那条路却走不通，
        于是**纯券商环境下选股永远报「股票池为空」**。本方法把这条能力接进
        ``DataSource`` 抽象，供 universe 作券商兜底。
        """
        try:
            res = await self._b.call(self._b.gateway.get_sector_stocks, sector)
        except (BrokerError, AttributeError):
            return None
        if isinstance(res, dict) and res.get("code"):
            return None
        if isinstance(res, list) and res:
            return [str(c) for c in res]
        return None


class DataSourceManager:
    """多源行情路由中心（进程级单例，见 get_manager）。"""

    def __init__(self):
        self._plugins: dict[str, DataSource] = {}
        self._broker_factory = None  # conn_id -> _BoundBrokerSource | None
        # V11 R6：auto 链**默认按能力契约链解析**（provider_catalog.resolve_chain）。
        # `_auto_chain_override` 只在显式 set_auto_chain() 后生效 —— 一条与能力无关的
        # 粗粒度覆盖，优先级高于契约链，供运维/测试临时改序（默认 None）。
        #
        # 此前 `_auto_chain` 是「注册顺序」的副产物，与契约链**两套规则并存**：
        # 5 条读路径（quote/detail/minutes/stock_list/search）用注册序，
        # 只有 K 线用契约链；且 `provider_catalog.set_override()` 只对后者生效 ——
        # 实测 `set_override('quote',['tencent'])` 后 API 回显 ['tencent']，
        # 而 get_quote 仍取 sina（改链 API 对 5 条路径静默无效）。
        self._auto_chain_override: list[str] | None = None
        # 商用模式：True 时链路求值阶段跳过 Research-Only 等禁止商用的源（如 eltdx）。
        self._commercial_mode: bool = False
        self._breakers: dict[str, dict] = {}
        # 详情富化结果缓存：code -> (写入时间, det)；见 _enrich_detail
        self._detail_cache: dict[str, tuple[float, dict]] = {}
        # 最近一次 _first_supported 的失败溯源（见 last_failure_trace）
        self._last_chain: list[str] = []
        self._last_trace: list[str] = []

    # ---------- 注册 ----------
    def register(self, source: DataSource) -> "DataSourceManager":
        self._plugins[source.name] = source
        return self

    def register_broker(self, factory) -> "DataSourceManager":
        """factory(conn_id) -> _BoundBrokerSource | None（无连接返回 None）。"""
        self._broker_factory = factory
        return self

    def set_auto_chain(self, chain: list[str] | None) -> "DataSourceManager":
        """设定**与能力无关**的粗粒度回退序覆盖；传 None 清除、恢复契约链。

        未注册的源名直接丢弃（不静默保留无效项）。这是运维/测试用的逃生口，
        生产默认路径是 ``provider_catalog.resolve_chain(capability)``。

        与契约链的差别：覆盖链**不做依赖可用性过滤**（它是一条显式指令，调用方自担），
        但仍做注册 / 许可证 / 能力校验三重过滤。
        """
        if chain is None:
            self._auto_chain_override = None
            return self
        self._auto_chain_override = [c for c in chain if c == "broker" or c in self._plugins]
        return self

    @property
    def _auto_chain(self) -> list[str]:
        """当前 auto 候选的**只读视图**：有覆盖用覆盖，否则取 kline 契约链的解析结果。

        保留此名字供 ``routes/market.py`` 的 ``/market/sources`` 展示 —— 此前它直接暴露
        内部注册序（与实际生效链可能不同），现已改为暴露真实生效的链。
        """
        return self._resolve_sources("auto", "kline")

    # ---------- 多源能力链（Phase 3：CapabilityChain，替代写死的 auto_chain）----------
    def set_commercial_mode(self, value: bool) -> "DataSourceManager":
        """设置商用模式；True 时链路求值阶段跳过 Research-Only 等禁止商用的源。"""
        self._commercial_mode = bool(value)
        return self

    def _registered_set(self) -> set[str]:
        return set(self.list_sources())

    def _declared_map(self) -> dict[str, frozenset[str]]:
        """provider_id -> 实现类自述的能力集。

        **这是能力的唯一真源**（``DataSource.capabilities``），供 ``resolve_chain`` 做
        能力校验。broker 不经 ``register`` 注册（它由 ``_BoundBrokerSource`` 按连接动态
        构造），故在此显式补上其类声明。
        """
        out: dict[str, frozenset[str]] = {}
        if self._broker_factory is not None:
            out["broker"] = frozenset(_BoundBrokerSource.capabilities)
        for name, src in self._plugins.items():
            caps = getattr(type(src), "capabilities", None)
            if caps:
                out[name] = frozenset(caps)
        return out

    def _resolve_sources(self, source: str, capability: str) -> list[str]:
        """**唯一**的 auto 候选解析入口（D-J §J.2；V11 R6 统一）。

        - auto/prefer_qmt/空：有 ``set_auto_chain`` 覆盖则用覆盖（注册 + 许可 + 能力三重
          过滤，不做依赖过滤）；否则取 ``provider_catalog.resolve_chain(capability)``
          （注册 + 依赖 + 许可 + 能力四重过滤）；
        - explicit:<id>：仅该源，不降级（不支持该能力由调用方负责报错）；
        - qmt_only：仅 broker；local_only：空（由 canonical 层处理）；
        - broker / 具体源名：单源。

        V11 R6 之前，5 条读路径走的是「注册序 + 许可证」的 ``_auto_candidates()``，
        与 K 线的契约链**两套规则并存**，且 ``provider_catalog.set_override()`` 只对
        后者生效 —— 现已全部收敛到本方法。
        """
        from datasource.providers import provider_catalog
        src = (source or "auto").strip()
        if src.startswith("explicit:"):
            return [src[len("explicit:"):]]
        if src in ("", "auto", "prefer_qmt"):
            if self._auto_chain_override is not None:
                declared = self._declared_map()
                return [n for n in self._auto_chain_override
                        if (n == "broker" or self._license_ok(n))
                        and (n not in declared or capability in declared[n])]
            return provider_catalog.resolve_chain(
                capability, commercial_mode=self._commercial_mode,
                registered=self._registered_set(), declared=self._declared_map())
        if src == "qmt_only":
            return ["broker"] if "broker" in self._registered_set() else []
        if src == "local_only":
            return []
        if src == "broker":
            return ["broker"]
        return [src]

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

    def _license_ok(self, name: str) -> bool:
        """该源在当前模式下是否允许使用（商用模式跳过 Research-Only 源）。

        决策权统一在 ``provider_catalog.is_commercial_ok``，此处不另写一套判断，
        避免两处 commercial_ok 口径漂移。

        ★ 修复（2026-09-13，许可证合规）：此前只有 ``get_kline`` 经
        ``resolve_chain`` 应用了商用过滤，而 ``get_quote`` /
        ``get_instrument_detail`` / ``get_minutes`` / ``get_stock_list`` /
        ``search_stocks`` 都直接遍历 ``_auto_chain``，**绕过了许可证过滤**。
        后果：商用模式下 K 线已正确跳过 eltdx（ELTDX Research-Only，禁止商用），
        行情却仍在用 eltdx —— 等于把禁止商用的数据源用在了商业部署里。
        现统一走 ``_resolve_sources()``（V11 R6 起按能力契约链），五条路径与 K 线同规则。
        """
        if not self._commercial_mode:
            return True
        from datasource.providers import provider_catalog
        return provider_catalog.is_commercial_ok(name)

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

    async def _call_source(self, name: str, coro, timeout: Optional[float] = None):
        """对单个源调用施加超时 + 熔断；成功返回结果，失败/熔断/超时返回 None。

        熔断打开时直接丢弃未启动的协程（close），避免「coroutine never awaited」警告。

        ``timeout`` 可选覆盖单源超时：详情富化等「增强项」用更短的切片，
        让不可达源快速出局而不是拖死整次调用（见 ``_enrich_detail``）。
        """
        if not self._source_allowed(name):
            if coro is not None and hasattr(coro, "close"):
                coro.close()
            return None
        try:
            res = await asyncio.wait_for(coro, timeout=timeout or _PER_SOURCE_TIMEOUT)
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
        # ★ 名称兜底必须**真的去查名称**，不能拿代码冒充（2026-09-20 实测修复）。
        #
        # 为什么之前指数名恒为代码：详情层（instrument_detail）对**指数/板块**这类
        # 没有真实合约资料的品种，会把 name **回落成代码本身**（实测 broker 与 eltdx
        # 都如此）。`_from_plugin` 早就发现并打了补丁，但 `_from_broker` **没有** ——
        # 于是「连着券商」这条最常用的路径上，详情名（=代码）把源层已解析好的
        # 「沪深300」覆盖掉，界面只剩 `000300.SH`。
        #
        # 修法：把「详情名 == 代码」一律视为**没有名称**，逐级回退到
        # 源层名称 → 本地名称表/内置指数表 → 空串（前端 `format.ts::namePair`
        # 会把空串渲染成代码占位，显示效果一致但语义诚实，不再污染 `_is_st` 之类判据）。
        det_name = str(detail.get("name") or "").strip()
        if det_name.upper() == code.upper():
            det_name = ""
        raw["name"] = (det_name or str(raw.get("name") or "").strip()
                       or _local_name(code) or "")
        raw["exchange"] = detail.get("exchange") or board.get("exchange")
        raw["high_limit"] = detail.get("high_limit") or detail.get("up_limit_price")
        raw["low_limit"] = detail.get("low_limit") or detail.get("down_limit_price")
        # 昨收三级回退：详情层 → 源层 pre_close → 源层 lastClose（券商口径名）。
        # ★ 中间那级是批量路径必需的：公开源的批量画像只给 name/pre_close/last/...
        # 六个键，若某级缺失，少了 ``raw["pre_close"]`` 这一级会把源层已解析好的昨收
        # **直接抹成 None**，进而让下面 change/change_pct 的推导整体失效（涨跌全变 None）。
        raw["pre_close"] = (detail.get("pre_close") or raw.get("pre_close")
                            or raw.get("lastClose"))
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

    async def _enrich_detail(self, code: str, det: Optional[dict]) -> dict:
        """券商详情为空壳时补齐画像：**结果缓存 → 本地名称兜底 → 限预算网络遍历**。

        ★ 为什么需要（2026-09-14 实测）：券商对 ETF / 指数常回空壳详情，原实现会
        顺序遍历全部插件源且**不设总时限**。一旦某源不可达（实测 sina 经本机代理
        403，单次约 5.5s），整次 get_quote 被拖到 5.6s —— 实测同一接口
        股票 0.02s / ETF·指数 5.6s 的差异即由此而来。而实测这 5.6s **只换来一个
        名称**（`_merge_quote` 的 broker 分支只用 det 的 name/涨跌停/昨收，
        行业与概念并不透出）。代价与收益严重不匹配，故：

        ① **结果缓存**（``_DETAIL_CACHE_TTL``）：合约画像日内基本不变，
           同一代码在 TTL 内只付一次网络代价；只有「非空壳」结果才入缓存
           （空壳入缓存会让富化永久失效）。
        ② **本地名称兜底**（``lookup_name``，零网络 O(1)）：先补上最关键的 name，
           即使后续网络全失败，界面也不会退化成「只有代码」。
        ③ **限预算网络遍历**：单源切片 + 总预算，超预算即放弃剩余源。
           注意**不做**「有本地名称就跳过网络」的短路——正常部署下插件源能给出
           完整画像（涨跌停/行业/概念），短路会让这些部署白丢数据。
        """
        det = dict(det or {})
        # ① 结果缓存
        hit = self._detail_cache.get(code)
        if hit is not None and time.time() - hit[0] < _DETAIL_CACHE_TTL:
            return dict(hit[1])
        # ② 本地名称兜底：先补 name，网络全失败时至少不丢名称
        nm = det.get("name")
        if not nm or str(nm) == code:
            local = self.lookup_name(code)
            if local:
                det["name"] = local
        # ③ 限预算网络遍历
        deadline = time.time() + _DETAIL_ENRICH_BUDGET
        best = det
        for pname in self._plugins:
            remain = deadline - time.time()
            if remain <= 0:
                log.debug("详情富化预算用尽，放弃剩余源：%s", code)
                break
            pdet = await self._call_source(
                pname, self._plugins[pname].get_instrument_detail(code),
                timeout=min(remain, _DETAIL_ENRICH_ATTEMPT))
            if not _contentless_detail(pdet, code):
                best = pdet
                break
        if not _contentless_detail(best, code):
            # 容量防护：条目数与代码数同量级，超限整体重置即可（不做 LRU）
            if len(self._detail_cache) >= _DETAIL_CACHE_MAX:
                self._detail_cache.clear()
            self._detail_cache[code] = (time.time(), dict(best))
        return best

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
                det = await self._enrich_detail(code, det)
            return self._merge_quote(raw, code, board, det, "broker")

        async def _from_plugin(name: str):
            src = self._plugins.get(name)
            if src is None:
                return None
            raw = await self._call_source(name, src.get_quote(code))
            if raw is None:
                return None
            det = await self._detail_for(name, src, code)
            # 指数 / 板块 等品种没有真实 instrument_detail：详情层会把 name 回落成
            # 代码本身（实测 000001.SH 的 det = {"name": "000001.SH", industry: 全国性银行}），
            # 此时 _merge_quote 会用它覆盖源层已解析好的「上证指数」，
            # 界面就退化成「上证指数显示为 000001.SH」。
            # 因此当详情名缺失或等于代码时，回退到源层 get_quote 的名称。
            det_name = (det.get("name") or "").strip()
            if not det_name or det_name == code:
                if raw.get("name"):
                    det = {**det, "name": raw["name"]}
            return self._merge_quote(raw, code, board, det, name,
                                     industry=det.get("industry") or "",
                                     concepts=det.get("concepts") or [])

        if source == "broker":
            return await _from_broker()
        if source in self._plugins:
            return await _from_plugin(source)
        # auto：按能力契约链依次尝试（V11 R6：与 K 线共用同一解析入口）
        for name in self._resolve_sources(source, "quote"):
            if name == "broker":
                q = await _from_broker()
            else:
                q = await _from_plugin(name)
            if q is not None:
                return q
        return None

    # ---------- 行情快照（批量） ----------
    async def get_quotes(self, codes, source: str = "auto",
                         conn_id: Optional[str] = None) -> dict:
        """批量盘口快照 —— ``{code: dict | None}``。

        ★ 为什么必须有这个方法（2026-09-20 实测）：``/market/quotes`` 的自选股 /
        报价牌冷启动要一次拉 4~N 只。此前它逐只调本类的 ``get_quote``，而公开源
        有 **全局 0.3s 节流锁**（``_PublicSource._MIN_INTERVAL``，跨请求共享），
        N 只 = N 次 HTTP + N 次锁等待；同步任务在前面占着锁时，冷启动 4 只
        **一只都拿不到**（items 恒空）。批量接口让 N 只收敛成 1 次 HTTP。

        与 ``get_quote`` 的语义差异（重要，勿混淆）：
        - ``get_quote`` 的 auto 是「逐个源尝试，第一个成功的整体返回」；
          批量版是「**同一源内**批量拉取，未拿到的 code **带着缺口继续走下一个源**」，
          即降级粒度是 **code 级** 而不是整批级 —— 一批里 3 只成功 1 只失败时，
          失败的 1 只仍有机会被下一个源补上，成功的 3 只不会被重来一遍。
        - 券商没有批量接口（QMT 盘口是逐只订阅的），故 broker 分支仍是并发单只，
          与 ``get_quote(broker)`` 语义完全一致。

        ⚠️ 返回值**总包含全部入参 code**（拿不到的显式置 None），调用方据此决定
        是否走兜底；绝不静默丢键。
        """
        source = self._validate_source(source)
        # 规范化 + 去重（保序）：券商只认 600519.SH，名称表也以带后缀代码为键
        ordered: list = []
        seen: set = set()
        for raw_code in codes or []:
            full = with_exchange_suffix(raw_code)
            if full and full not in seen:
                seen.add(full)
                ordered.append(full)
        if not ordered:
            return {}
        boards = {c: classify_board(c) for c in ordered}
        out: dict = {c: None for c in ordered}

        async def _plugin_batch(name: str, todo: list) -> dict:
            src = self._plugins.get(name)
            if src is None:
                return {c: None for c in todo}
            raws = await self._call_source(name, src.get_quotes(todo))
            if not isinstance(raws, dict):
                return {c: None for c in todo}
            hit = [c for c in todo if raws.get(c)]
            dets: dict = {}
            # ① 画像**先从已取到的快照派生**（公开源零额外 HTTP —— 见
            #    ``_PublicSource.derive_detail``）；不能派生的才回源批量取。
            #    这样一次批量 = 1 次 HTTP；否则画像会再打一次，收益砍半。
            todo_det: list = []
            for c in hit:
                try:
                    d = src.derive_detail(raws[c])
                except Exception:  # noqa: BLE001  派生是增强项，失败不该拖垮行情
                    d = None
                if d:
                    dets[c] = d
                else:
                    todo_det.append(c)
            if todo_det:
                got = await self._call_source(name, src.get_details(todo_det))
                if isinstance(got, dict):
                    dets.update({c: v for c, v in got.items() if v})
            res: dict = {}
            for c in todo:
                raw = raws.get(c)
                if not raw:
                    res[c] = None
                    continue
                det = dets.get(c) or {}
                # 与 _from_plugin 同一条规则：详情名缺失或等于代码时回退源层名称，
                # 否则指数会被详情层的「名称 == 代码」覆盖成 000001.SH。
                det_name = str(det.get("name") or "").strip()
                if not det_name or det_name.upper() == c.upper():
                    if raw.get("name"):
                        det = {**det, "name": raw["name"]}
                res[c] = self._merge_quote(
                    raw, c, boards[c], det, name,
                    industry=det.get("industry") or "",
                    concepts=det.get("concepts") or [])
            return res

        async def _broker_batch(todo: list) -> dict:
            # ⚠️ 这里是「一批标的」的**假并发**：`asyncio.gather` 只并发了等待，
            #    每次 `self.get_quote(...)` 都会各发一条 `get_quote` RPC
            #    （`bridge_client.py::BridgeAdapter.get_quote` → `_rpc("get_quote", [code])`）
            #    ⇒ **N 只标的 = N 次跨进程 RPC**。取 8 个指数就是 8 次。
            #
            # ★ 桥接侧其实**已有批量接口**：`BridgeAdapter.get_full_tick(codes)`
            #   （`_rpc("get_full_tick", [list(codes)])`）一次就能拿全。
            #
            # ⚠️ 但**不能**把这里直接换成批量 RPC：`self.get_quote()` 除取价之外还做了
            #    名称兜底 / 板块分类 / 详情合并（见上面 `_merge_quote` 那段），
            #    直接批量取原始 tick 会**静默丢掉这些字段**（指数名会退化成代码）。
            #    正确做法是「批量只用于取原始 tick，逐只归一化照旧」。
            #
            # ⚠️ 归属**尚未定论**，别当结论用：隔离实例实测 `source=broker` 冷 8.140s、
            #    `source=tencent` 冷 0.688s（同一后端进程，8 只指数）。8 次 RPC 是不是
            #    那 8 秒的**主因**，需要真券商环境再测一次才能确认。
            results = await asyncio.gather(
                *[self.get_quote(c, "broker", conn_id) for c in todo],
                return_exceptions=True)
            return {c: (None if isinstance(r, Exception) else r)
                    for c, r in zip(todo, results)}

        if source == "broker":
            return await _broker_batch(ordered)
        if source in self._plugins:
            return await _plugin_batch(source, ordered)
        # auto：code 级降级 —— 缺口交给链上的下一个源
        for name in self._resolve_sources(source, "quote"):
            todo = [c for c in ordered if out.get(c) is None]
            if not todo:
                break
            part = await (_broker_batch(todo) if name == "broker"
                          else _plugin_batch(name, todo))
            for c, q in (part or {}).items():
                if q is not None:
                    out[c] = q
        return out

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
            # ★ 快照派生字段必须**原样透出**：早先这里只挑固定的 6 个键，
            #   插件层新解析出来的市值 / PE / PB / 换手… 全在这一步被丢掉，
            #   端点恒返回 null —— 而直接调插件的单测是全绿的（绕过了 manager）。
            #   常量与插件层共用 `EXT_DETAIL_KEYS`，杜绝两边漂移。
            ext = {k: det.get(k) for k in EXT_DETAIL_KEYS}
            return {
                **base,
                **ext,
                "name": det.get("name") or code,
                "exchange": det.get("exchange") or base["exchange"],
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
        for name in self._resolve_sources(source, "instrument_detail"):
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
                        adjust: Optional[str] = None,
                        min_date: Optional[str] = None) -> tuple[Optional[list], Optional[str]]:
        """返回 (bars, source_name)；bars 为 None 表示无可用源。

        复权（qfq/hfq）经 dividend_type 参数化后，QMT 同样参与复权链（不再强制跳过
        broker）；其余按能力链 QMT→eltdx→baostock→akshare 依次降级（D9 v1.3 锁定）。

        ``min_date``（V11 R13，**新鲜度门槛**）：要求最后一根交易日 >= 该值才算
        「够新」，不满足则**继续降级**到链上下一个源。

        ★ 为什么需要它：源链是「第一个**非空**即返回」，而「非空」≠「够新」。
        实测（2026-09-18）全市场同步 5153 只：
        ``broker`` 本地历史只下载到 ``20250418``（一年多前）却**非空**，于是
        **永不降级**，163 万根陈旧数据被写成成功，界面显示「已完成」。
        这是继「空转报成功」「对账假空态」之后**第三种假成功**形态 ——
        有数据，但是陈的。

        全链都不满足时返回**其中最接近**的一份（有数据总优于无数据），
        是否算陈旧由调用方（同步器）按 :func:`bars_last_date` 自行判定并标注，
        本方法**不谎报**新鲜度。``min_date`` 为 None 时行为与改造前完全一致。
        """
        source = self._validate_source(source)
        try:
            _canon = normalize_period(period)
        except UnknownPeriodError:
            _canon = None
        cap = ("kline_qfq" if (adjust in ("qfq", "hfq")
                               and _canon in adjust_allowed_periods()) else "kline")
        chain = self._resolve_sources(source, cap)
        fallback: Optional[tuple] = None  # (bars, name, last_dt)
        for name in chain:
            if name == "broker":
                b = self._broker(conn_id)
                if b is None:
                    continue
                bars = await self._call_source("broker", b.get_kline(code, period, count, adjust))
            else:
                src = self._plugins.get(name)
                if src is None or not hasattr(src, "get_kline"):
                    continue
                bars = await self._call_source(name, src.get_kline(code, period, count, adjust))
            if not bars:
                continue
            if not min_date:
                return bars, name
            last = bars_last_date(bars)
            if last and last >= min_date:
                return bars, name
            if fallback is None or last > fallback[2]:
                fallback = (bars, name, last)
        if fallback is not None:
            log.debug("get_kline %s：全链数据均未达 min_date=%s，返回最接近的 %s(%s)",
                      code, min_date, fallback[1], fallback[2] or "无日期")
            return fallback[0], fallback[1]
        return None, None

    async def get_kline_range(self, code: str, period: str = "1d", *,
                              adjust: Optional[str] = None,
                              start: str = "", end: str = "",
                              count: int = 5000,
                              source: str = "auto",
                              conn_id: Optional[str] = None
                              ) -> tuple[Optional[list], Optional[str]]:
        """按**日期区间**取 K 线（全量回补专用；V11 §5.3 P0-3 III）。

        返回 ``(bars, source_name)``；``bars is None`` 表示**链上没有源支持区间**，
        调用方据此如实报「退化」而不是假装拿到了历史。

        ★ 与 :meth:`get_kline` 只差一点，但这一点是关键：这里**只走声明了
        ``supports_kline_range`` 的源**。免费在线源（eltdx / 腾讯 / 新浪）只接受
        ``count``（最近 N 根），把 ``start``/``end`` 传过去它们会**静默忽略** ——
        调用方拿到「最近 N 根」却以为拿到了某一年的历史，逐年翻页于是变成
        「同一批最近数据重复 12 遍」。判据必须来自**声明式能力**，
        而不是 try/except TypeError 那种「试了才知道」的写法。

        当前只有券商（``_BoundBrokerSource``）声明该能力：迅投
        ``get_market_data(start_time=, end_time=)`` + ``download_history_data``
        都能按区间工作。纯在线源环境下本方法恒返 ``(None, None)``。
        """
        source = self._validate_source(source)
        try:
            _canon = normalize_period(period)
        except UnknownPeriodError:
            _canon = None
        cap = ("kline_qfq" if (adjust in ("qfq", "hfq")
                               and _canon in adjust_allowed_periods()) else "kline")
        for name in self._resolve_sources(source, cap):
            if name == "broker":
                b = self._broker(conn_id)
                if b is None or not _accepts_kline_range(b):
                    continue
                bars = await self._call_source(
                    "broker", b.get_kline(code, period, count, adjust,
                                          start=start or "", end=end or ""))
            else:
                src = self._plugins.get(name)
                if src is None or not hasattr(src, "get_kline"):
                    continue
                if not _accepts_kline_range(src):
                    continue
                bars = await self._call_source(
                    name, src.get_kline(code, period, count, adjust,
                                        start=start or "", end=end or ""))
            if bars:
                return bars, name
        return None, None

    # ---------- 当日分时（仅补充源提供；券商 SDK 无分时接口） ----------
    async def get_minutes(self, code: str, trading_date: Optional[str] = None,
                          source: str = "auto") -> Optional[dict]:
        """当日分时曲线（价格+均价+分钟量）。按 ``minutes`` 能力链遍历补充源（跳过券商），
        全部无数据返回 None。

        V11 R6：此前借道「注册序」候选链，会去试 sina/tencent 等**没有 get_minutes**
        的源（靠 hasattr 事后跳过）；现按 ``minutes`` 能力解析，候选集即「真正实现该
        能力的源」。
        """
        source = self._validate_source(source)
        for name in self._resolve_sources(source, "minutes"):
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
    def _sup_chain(self, source: str = "auto", capability: str = "kline") -> list:
        """按 source + **真实能力**解析补充源候选链（V11 R6：不再固定借道 kline 链）。

        - "broker"：返回空链 → 方法返回 None，绝不悄悄回退补充源行情，
          否则「仅券商」的降级语义失效，用户会误以为看的是券商数据。
        - 具体源名 / explicit:<id>：只用该源（交由 _resolve_sources 处理）。
        - "auto"/空：按 ``capability`` 的能力链依次降级，并排除 broker
          （本方法服务于「券商 SDK 无此接口」的能力，如分时/板块/资金流）。

        此前无论问什么能力都传 "kline" —— 实测 `get_moneyflow` 拿到的是 kline 链
        ``[tencent, sina]``，而 moneyflow 的真实链是 ``(broker, eltdx, akshare)``，
        **两者源集合不相交**（恰好本机无源实现该方法，掩盖了错配）。
        """
        want = self._validate_source(source)
        if want == "broker":
            return []
        return [n for n in self._resolve_sources(source, capability) if n != "broker"]

    async def _first_supported(self, method: str, *args, source: str = "auto",
                               capability: str = "kline", **kwargs):
        """按 source + capability 解析的链，找到第一个实现该方法的补充源并返回 (结果, 源名)。

        失败原因记录在 :attr:`_last_chain` / :attr:`_last_trace`，供
        :meth:`last_failure_trace` 读取 —— 返回值本身只有 ``(None, None)``，
        信息量不足以让用户判断该怎么办。
        """
        chain = self._sup_chain(source, capability)
        self._last_chain = list(chain)
        self._last_trace = []
        for name in chain:
            src = self._plugins.get(name)
            if src is None:
                self._last_trace.append(f"{name}: 未注册")
                continue
            if not hasattr(src, method):
                self._last_trace.append(f"{name}: 不支持 {method}")
                continue
            if not self._source_allowed(name):
                self._last_trace.append(f"{name}: 熔断冷却中")
                continue
            res = await self._call_source(name, getattr(src, method)(*args, **kwargs))
            if res:
                return res, name
            self._last_trace.append(f"{name}: 调用失败或返回空")
        return None, None

    async def get_sector_stocks(self, sector: str = "沪深A股",
                                conn_id: Optional[str] = None) -> tuple[Optional[list], Optional[str]]:
        """券商板块成分股（代码列表, 源名）；未连接券商或不支持时返回 (None, None)。

        与 :meth:`get_board_constituents` 的区别：后者走「在线补充源」能力链
        （**刻意排除 broker**，见 :meth:`_sup_chain`），本方法**只用券商**。
        两者是互补的两条路，不是重复实现 —— 在线源给的是带名称/权重的成分明细，
        券商给的是纯代码列表，够选股池用。
        """
        b = self._broker(conn_id)
        if b is None or not hasattr(b, "get_sector_stocks"):
            return None, None
        codes = await self._call_source("broker", b.get_sector_stocks(sector))
        if not codes:
            return None, None
        return list(codes), "broker"

    def last_failure_trace(self) -> dict:
        """最近一次 :meth:`_first_supported` 的失败溯源。

        为什么需要它：``_first_supported`` 失败时只返回 ``(None, None)``，
        **丢弃了全部失败原因**，上层于是只能给一句泛化兜底文案。

        实测（2026-09-19）：用户 ``source=broker`` 查板块榜得到
        「TDX 行情源暂不可用，请检查网络或连接券商」—— 而他**明明连着券商**。
        真实原因是 ``_sup_chain("broker")`` **刻意返回空链**（券商不提供 sector
        能力），与 TDX、与网络都无关。把「不支持」说成「网络坏了」会直接带偏排查方向。
        """
        return {"chain": list(self._last_chain), "tried": list(self._last_trace)}

    async def get_boards(self, kind: str = "industry", sort_by: str = "pct",
                         limit: int = 50, source: str = "auto") -> tuple[Optional[list], Optional[str]]:
        """板块指数榜单（真实板块指数快照）。返回 (rows, source_name)。"""
        return await self._first_supported("get_boards", kind, sort_by, limit, source=source,
                                          capability="sector")

    async def get_board_constituents(self, code: str, limit: int = 50, page: int = 0,
                                     source: str = "auto") -> tuple[Optional[dict], Optional[str]]:
        """板块成分股。返回 ({code,total,items,page,has_more}, source_name)。"""
        return await self._first_supported("get_board_constituents", code, limit, page, source=source,
                                          capability="sector")

    async def get_board_kline(self, code: str, period: str = "1d", count: int = 60,
                              source: str = "auto") -> tuple[Optional[list], Optional[str]]:
        """板块 / 指数 K 线（kind='index'）。"""
        return await self._first_supported("get_board_kline", code, period, count, source=source,
                                          capability="index_constituent")

    async def search_boards(self, name: str, limit: int = 8,
                            source: str = "auto") -> tuple[Optional[list], Optional[str]]:
        """板块名称→代码匹配（深链稳化）。返回 (rows, source_name)。"""
        return await self._first_supported("search_boards", name, limit, source=source,
                                          capability="sector")

    async def get_etf_list(self, limit: int = 0,
                           source: str = "auto") -> tuple[Optional[list], Optional[str]]:
        """ETF 清单（代码 + 名称）。limit<=0 返回全量，避免整段截断。"""
        return await self._first_supported("get_etf_list", limit, source=source,
                                          capability="etf_list")

    async def get_moneyflow(self, code: str,
                            source: str = "auto") -> tuple[Optional[dict], Optional[str]]:
        """个股资金流（真实内外盘口径）。"""
        return await self._first_supported("get_moneyflow", code, source=source,
                                          capability="moneyflow")

    async def get_share_capital(self, codes: list,
                                source: str = "auto") -> tuple[dict, Optional[str]]:
        """流通股本（换手率分母）。无数据返回空 dict（调用方显式降级）。"""
        for name in self._sup_chain(source, "capital"):
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
        for name in self._sup_chain(source, "price_limit"):
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
        for name in self._resolve_sources(source, "stock_list"):
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
        for name in self._resolve_sources("auto", "search"):
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
    # 2026-09-13：tstdx（pytdx）已按方案 §6.1 移除——其注册名 "tstdx" 与
    # 能力链里的 "pytdx" 标识错配，实际从未被选中过，属无效冗余。
    from datasource.optional_sources import BaoStockSource
    for source_cls in (BaoStockSource,):
        if source_cls.available():
            source = source_cls()
            m.register(source)
            provider_catalog.register(source)
    from datasource.akshare_source import AkshareSource
    if AkshareSource.available():
        _ak = AkshareSource()
        m.register(_ak)
        provider_catalog.register(_ak)
    # 商用模式默认关闭（研究/个人使用，eltdx 全功能可用）。构建为商用时由环境变量
    # QMT_COMMERCIAL=1 打开，届时链路自动跳过 eltdx 等禁止商用的源（D-J §J.5）。
    import os
    m.set_commercial_mode(os.environ.get("QMT_COMMERCIAL") == "1")
    _manager = m
    return _manager


def commercial_mode() -> bool:
    """当前是否处于**商用模式**（``QMT_COMMERCIAL=1``）。

    **唯一实现**（V11 R7 收敛）：此前 ``app/platform.py``、``app/data/bars_provider.py``、
    ``app/screener/fundamentals.py`` 各写了一份**逐字相同**的实现（其中一份还带
    ``noqa: BLE901`` 笔误），三份都靠「函数内 ``from datasource.registry import get_manager``」
    让测试的 monkeypatch 生效 —— 重复本身就是漂移风险，故收敛到本函数。

    读的是进程级单例 ``DataSourceManager._commercial_mode``（由 ``get_manager()`` 在
    构建时按环境变量初始化，可经 ``set_commercial_mode`` 改写）；单例不可用时回退环境变量。

    注意：本函数在**调用时**才解析 ``get_manager``（模块全局），因此
    ``monkeypatch.setattr(datasource.registry, "get_manager", ...)`` 仍然生效。
    """
    try:
        return get_manager()._commercial_mode
    except Exception:  # noqa: BLE001 — 单例构建失败（如依赖缺失）时回退环境变量
        import os
        return os.environ.get("QMT_COMMERCIAL") == "1"


# 统一入口：get_hub() 返回全局 DataSourceManager 单例（旧 app.datasource.manager
# 兼容层已删除，全部引用收敛到本模块）。MarketDataHub 为历史别名，勿在新代码使用。
get_hub = get_manager
MarketDataHub = DataSourceManager
MarketDataUnavailable = DataSourceUnavailable


__all__ = ["DataSourceManager", "DataSourceUnavailable", "UnsupportedDataSource", "get_manager",
           "get_hub", "MarketDataHub", "MarketDataUnavailable", "commercial_mode",
           "classify_board", "limit_ratio"]
