"""历史 K 线 / 区间回补 / 分时 —— ``DataSourceManager`` 的 K 线族方法（P1-1 拆出）。

★ ``_accepts_kline_range`` 与这些方法同生共死（只有区间回补会用它），所以一起搬走；
  ``datasource.registry`` 仍 re-export 它（``tests/test_kline_range.py`` 从那里导入）。
★ ``bars_last_date`` 反过来：它是公开 API，实现落在 ``datasource/bars_util.py``，
  本模块 import 使用。
"""
from __future__ import annotations

import inspect
import logging
from typing import Optional

from datasource.bars_util import bars_last_date
from datasource.periods import (
    UnknownPeriodError,
    adjust_allowed_periods,
    normalize_period,
)

#: 与拆分前**同名**的 logger —— 日志的 logger 名与级别行为逐字不变。
log = logging.getLogger("qmt_work.datasource.registry")

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

class KlineMixin:
    """见模块 docstring。"""

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
