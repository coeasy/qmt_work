"""Real public Sina/Tencent quote and daily-bar providers."""
from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from datasource.base import DataSource, EXT_DETAIL_KEYS

log = __import__("logging").getLogger("qmt_work.datasource.public_sources")


def _vendor_code(code: str) -> str:
    bare, _, exchange = code.upper().partition(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange)
    if not prefix:
        raise ValueError(f"股票代码必须带交易所后缀: {code}")
    return prefix + bare


# --------------------------------------------------------------------------
# 腾讯快照字段下标
#
# 快照形如 ``v_sh600519="1~贵州茅台~600519~1257.12~1266.98~..."``，按 ``~`` 切分后
# **0-based** 取用。
#
# ⚠️⚠️ 这些下标是**实测**出来的（2026-09-21 拉 sh600519 / sh000001 / sz300750 三个
# 样本逐位核对：涨停价 1393.68 = 昨收 1266.98 × 1.1、跌停 1140.28 = × 0.9、
# 创业板 300750 涨停 365.16 = 304.30 × 1.2 —— 都自洽），**不是照抄文档猜的**。
# 猜错的后果不是报错，而是界面显示一个**看起来很合理的错数字**，比 `--` 危险得多。
#
# 只挑「快照里本来就有、且个股基本面面板用得上」的字段。腾讯这一条请求能带 ~50 个
# 字段，以前只解析了 7 个，市值 / PE / PB / 换手 / 振幅这些**已经在响应里躺着**，
# 却还要别处再取或干脆不显示 —— 现在顺手解析出来，**零额外请求**。
# --------------------------------------------------------------------------
_TX_IDX = {
    "name": 1,
    "last": 3,          # 最新价
    "pre_close": 4,     # 昨收
    "open": 5,          # 今开
    "volume": 6,        # 成交量（手）→ 转股
    "high": 33,         # 最高
    "low": 34,          # 最低
    "amount": 37,       # 成交额（万元）→ 转元
    "turnover_rate": 38,  # 换手率 %
    "pe_ttm": 39,       # 市盈率（TTM，亏损股为负）
    "amplitude": 43,    # 振幅 %
    "circ_mv": 44,      # 流通市值（亿元）→ 转元
    "total_mv": 45,     # 总市值（亿元）→ 转元
    "pb": 46,           # 市净率
    "high_limit": 47,   # 涨停价（-1 = 无，见下）
    "low_limit": 48,    # 跌停价（-1 = 无）
    "volume_ratio": 49,  # 量比
    "avg_price": 51,    # 均价
}

_TX_MIN_FIELDS = 7  # 少于这个长度视为「没返回」（与历史行为一致）


def _tx_num(fields: list, key: str) -> float | None:
    """按 `_TX_IDX` 取一个数；缺位 / 空串 / 非数字一律 None（不猜、不填 0）。"""
    idx = _TX_IDX[key]
    if idx >= len(fields):
        return None
    try:
        return float(fields[idx])
    except (TypeError, ValueError):
        return None


def _tx_snapshot(code: str, fields: list) -> dict:
    """把腾讯快照的 `~` 字段数组解析成行情字典 —— **单只与批量共用一份解析**。

    ★ 必须共用：此前 `get_quote` 与 `get_quotes` 各写一遍，字段要加就得改两处，
      漏改的那条路径会静默少字段（历史上批量就被漏过一次）。

    ⚠️ 两个必须特殊处理的值（实测，不是理论）：
      - **涨跌停价**：指数 / 无涨跌幅品种返回 **`-1`**（不是 0，也不是空）。
        直接透传会让「上证指数涨停价 -1.00」这种荒谬值出现在界面上 ⇒ 只认 `> 0`。
      - **市净率**：指数返回 `0.00`。显示 `0.00` 会被读成「净资产为零」 ⇒ 只认 `> 0`。
      其余字段**保留原值**（`pe_ttm` 亏损股本就是负数，`amplitude` 一字板本就是 0），
      一律不做「<=0 就当没有」的粗暴过滤 —— 那会把真实数据抹掉。
    """
    def _pos(key: str) -> float | None:
        v = _tx_num(fields, key)
        return v if (v is not None and v > 0) else None

    volume = _tx_num(fields, "volume")
    amount = _pos("amount")
    circ = _pos("circ_mv")
    total = _pos("total_mv")
    name = fields[_TX_IDX["name"]] if len(fields) > _TX_IDX["name"] else ""
    return {
        "code": code,
        "name": name,
        "last": _tx_num(fields, "last"),
        "pre_close": _tx_num(fields, "pre_close"),
        "open": _tx_num(fields, "open"),
        "high": _tx_num(fields, "high"),
        "low": _tx_num(fields, "low"),
        # 手 → 股；万元 → 元；亿元 → 元（与 broker / eltdx 口径一致，前端 fmtAmount 直出「亿」）
        "volume": (volume * 100) if volume is not None else None,
        "amount": (amount * 1e4) if amount is not None else None,
        "turnover_rate": _tx_num(fields, "turnover_rate"),
        "pe_ttm": _tx_num(fields, "pe_ttm"),
        "amplitude": _tx_num(fields, "amplitude"),
        "circ_mv": (circ * 1e8) if circ is not None else None,
        "total_mv": (total * 1e8) if total is not None else None,
        "pb": _pos("pb"),
        "high_limit": _pos("high_limit"),
        "low_limit": _pos("low_limit"),
        "volume_ratio": _tx_num(fields, "volume_ratio"),
        "avg_price": _tx_num(fields, "avg_price"),
        "source": "tencent",
    }


class _PublicSource(DataSource):
    capabilities = frozenset({"quote", "kline", "instrument_detail"})

    #: 单源**最小请求间隔**（秒）—— 免费公开接口有速率限制，全市场批量同步
    #: （5000+ 只）若不节流会被直接封禁（实测腾讯返回 ``501 Not Implemented``，
    #: 且**不限 count**：连 20 根的请求也一并 501）。届时降级链路整体失效，
    #: 5000 只又静默退回券商的陈旧数据 —— 正是 §8.8 要消灭的「假成功」。
    #: 0.3s ⇒ 约 3 req/s，全市场约 30 分钟，是被封与吞吐之间的折中。
    _MIN_INTERVAL = 0.3
    _last_call = 0.0
    _gate = None

    @classmethod
    async def _throttle(cls) -> None:
        """串行化本源的请求并保证最小间隔（同类共享，跨实例生效）。

        锁**懒创建**：模块导入时不一定有事件循环，直接建 asyncio.Lock 会在
        某些 Python 版本绑到错误的循环上。
        """
        if cls._gate is None:
            cls._gate = asyncio.Lock()
        async with cls._gate:
            now = time.monotonic()
            wait = cls._MIN_INTERVAL - (now - cls._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            cls._last_call = time.monotonic()

    async def _get(self, url: str) -> str:
        await self._throttle()
        # DEBUG 级留痕：诊断「批量到底生效了没」时，数这个日志里 URL 的出现次数
        # 是最直接的证据（1 次 = 批量生效；N 次 = 退化成逐只）。
        log.debug("公开源 HTTP: %s", url)
        async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "qmt_work/1.0"}) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def get_instrument_detail(self, code: str) -> dict:
        quote = await self.get_quote(code)
        return {key: quote.get(key) for key in self._DETAIL_KEYS}

    #: 画像可从盘口快照派生的字段（与 ``get_instrument_detail`` 的返回键一致）
    #:
    #: ★ 与快照解析共用同一套键：快照里解析出来的市值 / PE / PB / 换手 / 涨跌停，
    #:   画像层**原样透出**，「基本信息」面板因此不用再发第二次请求。
    _DETAIL_KEYS = ("name", "pre_close", "last") + EXT_DETAIL_KEYS

    def derive_detail(self, raw: dict) -> dict:
        """从已取到的行情快照派生画像 —— **零额外 HTTP**。

        ★ 为什么需要：``get_instrument_detail`` 本身就是 ``get_quote`` 的字段子集
        （见上）。批量场景若再走一次 ``get_quotes`` 去「批量取画像」，
        N 只标的会退化成 **2 次** HTTP（1 次行情 + 1 次画像），且都要排队过
        0.3s 全局节流锁 —— 批量的收益被砍掉一半。有了派生钩子，
        manager 侧拿到行情后直接本地派生，画像不再产生任何请求。

        ⚠️ 只在**批量路径**启用（``DataSourceManager.get_quotes``）。单只路径
        （``get_quote``）仍走 ``get_instrument_detail``：将来若某个源把详情接口
        升级成能返回涨跌停 / 行业 / 概念，单只路径会自动拿到，而本派生只承诺
        「快照里有的字段」，不会悄悄丢掉那些升级。
        """
        if not raw:
            return {}
        return {k: raw.get(k) for k in self._DETAIL_KEYS}

    async def get_details(self, codes):
        """批量画像 —— 只用于**手上没有行情快照**时的兜底路径。

        正常批量链路走 ``derive_detail``（零请求）；这里保留是为了让
        ``get_details`` 作为独立接口仍然可用且语义正确。
        """
        if not codes:
            return {}
        quotes = await self.get_quotes(codes)
        if not isinstance(quotes, dict):
            return {c: None for c in codes}
        return {c: (self.derive_detail(q) if q else None)
                for c, q in quotes.items()}

    async def get_stock_list(self) -> list:
        # 公共源不提供全市场列表 —— 这里 raise 是**有意的显式拒绝**，不是待实现桩。
        # 对应的能力声明（见 capabilities）也不含 "stock_list"，故 resolve_chain 的
        # 能力校验不会把公共源排进 stock_list 链（V11 R6：此前注册序候选链会真的
        # 调到这里并计入熔断失败）。
        raise RuntimeError(f"{self.name} 不提供全市场股票列表")


class SinaSource(_PublicSource):
    name = "sina"
    # 新浪 get_kline 忽略 adjust 参数（只回不复权日线），故**不声明**复权变体能力；
    # 契约链 kline_qfq/kline_hfq 也确实未列入 sina（两处一致）。

    async def get_quote(self, code: str) -> dict:
        vendor = _vendor_code(code)
        raw = await self._get(f"https://hq.sinajs.cn/list={vendor}")
        match = re.search(r'="([^"]*)"', raw)
        if not match or not match.group(1):
            raise RuntimeError(f"新浪未返回行情: {code}")
        fields = match.group(1).split(",")
        if len(fields) < 10:
            raise RuntimeError(f"新浪行情字段不完整: {code}")
        return {"code": code, "name": fields[0], "open": float(fields[1] or 0),
                "pre_close": float(fields[2] or 0), "last": float(fields[3] or 0),
                "high": float(fields[4] or 0), "low": float(fields[5] or 0),
                "volume": float(fields[8] or 0), "amount": float(fields[9] or 0),
                "source": self.name}

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: str | None = None) -> list:
        if period != "1d":
            raise ValueError("新浪公共源当前只提供日线")
        scale = max(1, min(int(count), 1000))
        raw = await self._get(
            f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={_vendor_code(code)}&scale=240&ma=no&datalen={scale}")
        rows = json.loads(raw)
        return [{"time": r["day"], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r.get("volume", 0)), "source": self.name} for r in rows]


class TencentSource(_PublicSource):
    name = "tencent"
    # V11 R6：腾讯 get_kline 真的处理 adjust（映射到 fqkline 的 adj 参数），
    # 契约链也把 tencent 列在 kline_qfq/kline_hfq 里 —— 此前声明漏了这两个变体，
    # 属「链里有、声明无」，会让能力校验误剔（或反向让链形同虚设）。
    capabilities = frozenset({"quote", "kline", "kline_qfq", "kline_hfq",
                              "instrument_detail"})

    async def get_quote(self, code: str) -> dict:
        vendor = _vendor_code(code)
        raw = await self._get(f"https://qt.gtimg.cn/q={vendor}")
        match = re.search(r'="([^"]*)"', raw)
        fields = match.group(1).split("~") if match else []
        if len(fields) < _TX_MIN_FIELDS:
            raise RuntimeError(f"腾讯未返回行情: {code}")
        return _tx_snapshot(code, fields)

    async def get_quotes(self, codes):
        """批量行情 —— 1 次 HTTP 拉多只（绕过 N 只串行节流的瓶颈）。

        腾讯接口原生支持批量（``qt.gtimg.cn/q=sh000001,sz000002,...``），返回形如
        ``v_sh000001="...~...";v_sz000002="...~...";``。每只解析同单只。
        """
        if not codes:
            return {}
        vendors = [_vendor_code(c) for c in codes]
        url = "https://qt.gtimg.cn/q=" + ",".join(vendors)
        raw = await self._get(url)
        out: dict = {}
        # ★ 不依赖换行 —— 腾讯批量返回可能换行分隔、也可能单行用 ``;`` 分隔，
        #   用正则一次性抓出所有 ``v_<vendor>="<fields>"``，避免漏第二只。
        pattern = re.compile(r'v_([a-z]+\d+)="([^"]*)"')
        for vendor, fields_str in pattern.findall(raw):
            fields = fields_str.split("~")
            if len(fields) < _TX_MIN_FIELDS:
                continue
            # 重建 code（带后缀）
            prefix = vendor[:2].upper()
            bare = vendor[2:]
            code = f"{bare}.{prefix}"
            # ★ 与单只共用同一份解析（否则两条路径会少字段/多字段不一致）
            out[code] = _tx_snapshot(code, fields)
        # 没拉到的 code 标 None（让上游走兜底，不要静默丢）
        for c in codes:
            out.setdefault(c, None)
        return out

    async def get_kline(self, code: str, period: str = "1d", count: int = 250,
                        adjust: str | None = None) -> list:
        if period != "1d":
            raise ValueError("腾讯公共源当前只提供日线")
        adj = adjust if adjust in {"qfq", "hfq"} else "qfq"
        raw = await self._get(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
            f"{_vendor_code(code)},day,,,{max(1, min(int(count), 1000))},{adj}")
        payload = json.loads(raw)
        data = payload.get("data", {}).get(_vendor_code(code), {})
        # ★ 复权键名是 ``"<adj>day"`` 而不是 ``"<adj>"``（V11 R13 实测修正）。
        # 接口实际返回的键：qfq → ``qfqday``、hfq → ``hfqday``、不复权 → ``day``。
        # 此前按 ``data.get(adj) or data.get("day")`` 取，对**有复权历史**的标的
        # 恒取不到（键根本不叫 ``qfq``），只有返回裸 ``day`` 的（无除权的次新股、
        # 科创板新股）才侥幸成功 —— 实测全市场 5153 只里**只有 49 只**能取到，
        # 其余 5093 只腾讯源恒返空。
        #
        # 后果很严重：券商本地历史陈旧（实测只到 20250418）时本该降级到在线源，
        # 而在线源此刻恒返空 ⇒ 「降级」永远拿不到数据，5093 只股票的日线
        # 一年多没更新，同步却照报成功。
        rows = data.get(f"{adj}day") or data.get(adj) or data.get("day") or []
        if not rows:
            # 键名形状将来再变时不静默返空：写日志，避免又是一次「查不到原因的空」
            log.warning("腾讯 K 线无数据：%s（adj=%s，可用键=%s）",
                        code, adj, ",".join(sorted(data.keys())) or "无")
        return [{"time": r[0], "open": float(r[1]), "close": float(r[2]),
                 "high": float(r[3]), "low": float(r[4]), "volume": float(r[5]),
                 "source": self.name} for r in rows]


__all__ = ["SinaSource", "TencentSource"]
