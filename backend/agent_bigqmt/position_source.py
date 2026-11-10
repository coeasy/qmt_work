# -*- coding: utf-8 -*-
"""柜台持仓重建 —— 新规下持仓的唯一真相源（策略端，py3.6 兼容）。

为什么不能再有「本地持仓文件」
------------------------------
2026-10-23 起策略进程禁文件读写（``PermissionError: Forbidden FileIO``），
原先 ``position.json`` 那套「启动读文件、成交后写文件」直接报废：策略启动即报错。

替代方案不是「换个地方存」，而是**根本不用存**：
``get_trade_detail_data(ACCOUNT, TYPE, 'POSITION')`` 是官方柜台查询接口，新规明确允许，
启动时按它重建内存持仓，运行中由 ``deal_callback`` 增量更新。收益有三：

* 重启即对齐，天然免疫「文件损坏 / 文件落后于柜台」；
* 人工干预（手动割肉、打新、转托管）之后策略不会拿着旧数据瞎算；
* 少一个持久化通道 = 少一个随时会被 ban 的依赖。

★ 代价（必须在产品上说清）：**重启后策略会把账户里全部持仓认领为自己的**
⇒ 一个资金账号只能跑一个策略，多策略共用账号会互相踩踏。

★ py3.6 约束：本文件会被内联进 QMT 内置策略，禁用 dataclasses / f-string /
  海象运算符 / 内建泛型注解。
"""


class CounterUnavailable(Exception):
    """柜台查询失败。

    ★ 与「查询成功但没有持仓」是**两件事**：前者必须停止交易，后者是合法的空仓。
    把前者当后者，就是拿空仓当满仓去做调仓 —— 会重复建仓。
    """


def _field(obj, name, default=None):
    """兼容「对象属性」与「字典键」两种返回形态（不同终端/版本都出现过）。"""
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    if value is None and name.startswith("m_"):
        # 部分版本去掉 m_ 前缀
        short = name[2:]
        value = obj.get(short, default) if isinstance(obj, dict) else getattr(obj, short, default)
    return value


def normalize_code(obj):
    """柜台行 → ``600519.SH`` 形态。取不到就返回空串（由调用方跳过，不猜）。"""
    code = _field(obj, "m_strInstrumentID") or _field(obj, "stock_code") or _field(obj, "code")
    if not code:
        return ""
    text = str(code).strip().upper()
    if "." in text:
        return text
    market = _field(obj, "m_strExchangeID") or _field(obj, "exchange_id") or ""
    market = str(market).strip().upper()
    if not market:
        # 无交易所信息时不猜沪/深：宁可跳过这一行，也不要造出一个不存在的代码。
        return ""
    return text + "." + market


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class PositionSource(object):
    """持仓来源：启动重建 + 成交增量。"""

    def __init__(self, query, account_id, account_type="STOCK"):
        """:param query: 形如 ``get_trade_detail_data`` 的可调用对象
        （签名 ``query(account_id, account_type, 'POSITION')``）。
        """
        if not callable(query):
            raise TypeError("query 必须可调用（通常传 get_trade_detail_data）")
        if not account_id:
            raise ValueError("account_id 不能为空")
        self._query = query
        self.account_id = account_id
        self.account_type = account_type
        self._positions = {}
        self._ready = False
        self._last_error = ""

    # ------------------------------------------------------------------
    def rebuild(self):
        """从柜台重建。成功返回持仓快照；失败抛 ``CounterUnavailable``。"""
        try:
            rows = self._query(self.account_id, self.account_type, "POSITION")
        except Exception as exc:  # noqa: BLE001  —— 柜台异常一律归「不可用」
            self._ready = False
            self._last_error = "%s: %s" % (type(exc).__name__, exc)
            raise CounterUnavailable(self._last_error)
        if rows is None:
            # ★ None ≠ []：查询失败返回 None 时，绝不能当成「空仓」。
            self._ready = False
            self._last_error = "柜台返回 None（查询失败，不是空仓）"
            raise CounterUnavailable(self._last_error)

        positions = {}
        for row in rows:
            code = normalize_code(row)
            volume = _as_int(_field(row, "m_nVolume") or _field(row, "volume"))
            if not code or volume <= 0:
                continue
            available = _as_int(_field(row, "m_nCanUseVolume") or _field(row, "can_use_volume"),
                                volume)
            positions[code] = {
                "volume": volume,
                "available": max(0, min(available, volume)),
            }
        self._positions = positions
        self._ready = True
        self._last_error = ""
        return self.snapshot()

    # ------------------------------------------------------------------
    def apply_deal(self, deal):
        """成交回报增量更新（不落盘）。返回更新后的该标的持仓。"""
        code = normalize_code(deal)
        volume = _as_int(_field(deal, "m_nVolume") or _field(deal, "volume"))
        if not code or volume <= 0:
            return None
        side = str(_field(deal, "side") or _field(deal, "m_nDirection") or "").upper()
        is_sell = side in ("SELL", "S", "24", "2")
        current = self._positions.get(code, {"volume": 0, "available": 0})
        if is_sell:
            current["volume"] = max(0, current["volume"] - volume)
            current["available"] = max(0, current["available"] - volume)
        else:
            current["volume"] = current["volume"] + volume
        if current["volume"] <= 0:
            self._positions.pop(code, None)
            return None
        self._positions[code] = current
        return dict(current)

    # ------------------------------------------------------------------
    def snapshot(self):
        """深拷贝快照（调用方改它不会污染内部状态）。"""
        return dict((code, dict(pos)) for code, pos in self._positions.items())

    def volume_of(self, code):
        return self._positions.get(str(code).upper(), {}).get("volume", 0)

    def is_ready(self):
        return self._ready

    @property
    def last_error(self):
        return self._last_error

    def require(self):
        """交易前置守卫：未成功重建过就抛错（宁可不交易，也不瞎交易）。"""
        if not self._ready:
            raise CounterUnavailable(self._last_error or "持仓尚未从柜台重建")
        return self.snapshot()


__all__ = ["CounterUnavailable", "PositionSource", "normalize_code"]
