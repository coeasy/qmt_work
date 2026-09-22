"""合约：名称表 / 涨跌停 / 板块 / 日历 / 财务 / L2（InstrumentMixin，自原 xtp.py 逐行搬移）。"""

from ..base import BrokerNotConnectedError, BrokerSDKError
from core.errors import swallow

#: 财务接口的「无数据」统一文案。**必须可操作**：告诉用户下一步做什么，
#: 而不是只说「失败」（此前这类空洞文案让用户以为平台坏了）。
_NO_FINANCIAL_DETAIL = (
    "无财务数据：券商终端未返回该标的的财务指标。"
    "请在 QMT 客户端下载财务数据，或确认账号已开通财务数据权限。"
)


def _no_financial(code: str) -> dict:
    return {"code": code, "detail": _NO_FINANCIAL_DETAIL}


def _as_report_date(value) -> str:
    """报告期规范化：**只认 ``YYYYMMDD`` / ``YYYY-MM-DD`` 这类真日期**，否则返回 ``""``。

    ★★ 这是本模块最重要的一条不变量（2026-09-19 首次踩、2026-09-21 复发）：
    一旦放宽成「取前 10 个字符」，``PARENT_NETPROFIT`` 被 ``[:10]`` 截断成
    ``PARENT_NET`` 就会当报告期透出到界面 —— 用户看到「报告期：PARENT_NET」，
    而真相是「解析把最后一个指标名当成了报告期」。**宁可留空**（界面渲染 ``—``），
    也绝不透出一个看起来像值、实际是字段名的伪值。
    """
    from datetime import datetime as _dt
    s = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(s) != 8:
        return ""
    try:
        _dt.strptime(s, "%Y%m%d")
    except ValueError:
        return ""
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _num_or_none(value):
    """转 float；取不到或为 ``NaN`` 时返回 ``None``。

    ⚠️ NaN 必须转 None：``round(float("nan"), 4)`` 得到 ``nan``，会被 json 编码成
    裸 ``NaN`` —— 那是**非法 JSON**，浏览器 ``JSON.parse`` 直接抛错，整页数据全废。
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, 4)


def _flatten_numbers(obj) -> list:
    """把 DataFrame / Series / 嵌套 list / 标量摊平成一维列表（只保留能转 float 的）。"""
    out: list = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        if cur is None:
            continue
        if isinstance(cur, (list, tuple)):
            stack.extend(reversed(list(cur)))
            continue
        if hasattr(cur, "tolist"):
            try:
                stack.extend(reversed(list(cur.tolist())))
            except Exception as exc:  # noqa: BLE001
                # 摊平失败就跳过这一支：本函数的契约是「尽力取出一个数」，
                # 取不到由调用方按 None 处理（界面显示 —），绝不因此抛出去。
                swallow(exc, why="财务指标表摊平失败，跳过该支")
            continue
        f = _num_or_none(cur)
        if f is not None:
            out.append(f)
    return out


def _latest_from_table(value) -> tuple:
    """「一个指标一张表」形态下取最新一期：返回 ``(值, 报告期)``。

    ``value`` 可能是 DataFrame / Series / 标量。表为空（``0 × 0``）= 该指标无数据。

    日期轴可能是**索引**也可能是**列**（不同 xtquant 版本不一致，本机实测
    ``get_financial_data`` 返回 10 张 ``0 × 0`` 空表，拿不到非空样本 ⇒ 只能两种
    都判、不能赌一种）。都判不出来时**报告期留空**、值取最后一个数 —— 不编造。
    """
    if not hasattr(value, "shape"):  # 标量
        return _num_or_none(value), ""
    if 0 in tuple(getattr(value, "shape", ()) or ()):
        return None, ""  # 空表 = 无数据（本机实测就是这一种）
    for axis_name in ("index", "columns"):
        axis = getattr(value, axis_name, None)
        if axis is None:
            continue
        labels = [str(x) for x in list(axis)]
        dates = [x for x in labels if _as_report_date(x)]
        if not dates:
            continue
        label = dates[-1]
        try:
            sel = value.loc[label] if axis_name == "index" else value[label]
        except Exception:  # noqa: BLE001
            return None, _as_report_date(label)
        nums = _flatten_numbers(sel)
        return (nums[-1] if nums else None), _as_report_date(label)
    # 没有日期轴：退化为「最后一个数」，报告期留空（绝不拿指标名冒充日期）
    nums = _flatten_numbers(value)
    return (nums[-1] if nums else None), ""


def _row_from_field_map(frame: dict, fields) -> tuple:
    """``{指标: 值 | 表}`` 形态 → ``(指标字典, 报告期)``。

    报告期取**所有指标里出现的最大日期**（各指标的报告期通常一致，取最大即最新）。
    """
    row: dict = {}
    periods: list = []
    for f in fields:
        val, period = _latest_from_table(frame.get(f))
        row[f] = val
        if period:
            periods.append(period)
    return row, (max(periods) if periods else "")


class InstrumentMixin:


    def get_instrument_detail(self, code: str) -> dict:
        """合约详情：名称 / 涨停价 / 跌停价 / 昨收（用于涨停板精确涨停价与名称）。"""
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            d = self._xtdata.get_instrument_detail(code) or {}
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"合约详情获取失败：{exc}") from exc
        return {
            "code": code,
            "name": d.get("instrument_name") or code,
            "up_limit_price": d.get("up_limit_price"),
            "down_limit_price": d.get("down_limit_price"),
            "pre_close": d.get("pre_close_price"),
            "exchange": d.get("exchange_id"),
        }

    def get_stock_list(self, sector: str = "沪深A股") -> list[dict]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        codes = self._xtdata.get_stock_list_in_sector(sector) or []
        out = []
        for c in codes[:3000]:  # 名称逐个查询较耗时，限量保证响应
            name = self._name(c)
            out.append({"code": c, "name": name})
        return out

    def _name(self, code: str) -> str:
        if code in self._name_cache:
            return self._name_cache[code]
        name = code
        try:
            detail = self._xtdata.get_instrument_detail(code)
            if detail and detail.get("instrument_name"):
                name = detail["instrument_name"]
        except Exception:  # noqa: BLE001
            pass
        self._name_cache[code] = name
        return name

    def search_stocks(self, keyword: str, limit: int = 20) -> list[dict]:
        kw = (keyword or "").strip().upper()
        if not kw:
            return []
        # 代码匹配优先（快）
        codes = self._xtdata.get_stock_list_in_sector("沪深A股") if self._xtdata else []
        hits = []
        for c in codes:
            if kw in c.upper():
                hits.append({"code": c, "name": self._name(c)})
                if len(hits) >= limit:
                    return hits
        # 名称匹配
        for c in codes:
            if len(hits) >= limit:
                break
            name = self._name(c)
            if kw in name.upper() and c not in {h["code"] for h in hits}:
                hits.append({"code": c, "name": name})
        return hits

    # ---------------- 参考数据 / L2（真实 xtdata 调用） ----------------
    def get_sector_list(self) -> list[str]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            return [s for s in (self._xtdata.get_sector_list() or []) if s]
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"板块列表获取失败：{exc}") from exc

    def get_sector_stocks(self, sector: str = "沪深A股") -> list[str]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            return [c for c in (self._xtdata.get_stock_list_in_sector(sector) or []) if c]
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"板块成分获取失败：{exc}") from exc

    @staticmethod
    def _compact_date(value) -> str:
        """归一化为 xtdata 要求的 YYYYMMDD。

        实测缺陷：xtdata 内部用 ``strptime(x, '%Y%m%d')`` 解析，前端按 ISO 传
        ``2026-01-01`` 直接抛 "time data ... does not match"，交易日历面板永久报错。
        此处统一吞掉分隔符并截断到前 8 位（容忍 ``2026-01-01 00:00:00`` / 时间戳），
        非法值返回空串（xtdata 空串 = 不限制边界）。
        """
        if value is None:
            return ""
        s = str(value).strip()
        if not s:
            return ""
        digits = "".join(ch for ch in s if ch.isdigit())
        return digits[:8] if len(digits) >= 8 else ""

    def get_trading_calendar(self, start: str = "", end: str = "") -> list[str]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            # 多版本签名兼容：
            # - 旧 SDK：get_trading_calendar(market, start_time='', end_time='', tradetimes=False)
            #   首个位置参数必填为 market，乱填 start 会查错市场；
            # - 新 SDK：可能 get_trading_calendar(start_time, end_time, ...) 无 market。
            # 用 inspect 探测首参名决定调用方式（SH/SZ 交易日一致，取 SH 即可）。
            import inspect
            sig = inspect.signature(self._xtdata.get_trading_calendar)
            params = [p for p in sig.parameters]
            s, e = self._compact_date(start), self._compact_date(end)
            if params and params[0].lower().startswith("market"):
                cal = self._xtdata.get_trading_calendar("SH", s, e)
            else:
                cal = self._xtdata.get_trading_calendar(s, e)
            return [str(d) for d in (cal or [])]
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"交易日历获取失败：{exc}") from exc

    def get_financial(self, code: str) -> dict:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        fields = ["EPS", "BPS", "OPERATE_INCOME", "TOTAL_OPERATE_INCOME",
                  "PARENT_NETPROFIT", "TOTAL_OPERATE_EXPENSE", "ROE", "CAPITAL",
                  "TOTAL_OPERATE_INCOME_YOY", "PARENT_NETPROFIT_YOY"]
        # 多版本方法名兼容：新 SDK 为 get_stock_financial，部分版本仅有 get_financial_data。
        # 实测缺陷：直接写死方法名在缺该接口的券商终端上报
        # "module 'xtquant.xtdata' has no attribute 'get_stock_financial'"，
        # 财务面板永久报错。此处探测可用方法名，全缺时给出可操作的明确文案。
        fn = None
        for name in ("get_stock_financial", "get_financial_data"):
            cand = getattr(self._xtdata, name, None)
            if callable(cand):
                fn, fn_name = cand, name
                break
        if fn is None:
            raise BrokerNotConnectedError(
                "财务数据获取失败：当前券商终端 xtquant 版本不含财务接口"
                "（get_stock_financial / get_financial_data 均缺失），"
                "请升级 QMT 客户端或改用数据中心的历史财务源")
        try:
            try:
                df = fn([code], fields, "", "", report_type="report_time")
            except TypeError:
                # 旧签名不支持 report_type 关键字
                df = fn([code], fields, "", "")
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"财务数据获取失败（{fn_name}）：{exc}") from exc
        frame = (df or {}).get(code) if isinstance(df, dict) else df
        if frame is None:
            return {"code": code, "detail": "无财务数据（数据权限或代码无效）"}

        # 三种返回形态兼容（**朝向必须先判**，见下）：
        # - pandas.DataFrame（新 SDK get_stock_financial）
        # - 纯 dict（旧 SDK get_financial_data）
        #
        # ★ 实测缺陷（2026-09-19，本机 xtquant）：`get_stock_financial` 返回的
        # DataFrame **行是指标、列是报告期**（与「行=报告期」的直觉相反）。
        # 原实现一律取 ``frame.iloc[-1]`` 当「最新一期」，于是把**最后一个指标名**
        # 当成报告期 —— 实测 ``600519.SH`` 的 ``report_time`` 返回 ``"PARENT_NET"``
        # （``PARENT_NETPROFIT`` 被 ``[:10]`` 截断），且 ``EPS/BPS/ROE`` 全为 ``None``
        # ⇒ 估值维度（PE/PB）永远 ``unavailable``，界面上「基本面」永久空白。
        #
        # 判据不能靠「有没有 iloc」（两种朝向都有），只能看**索引里是不是指标名**。
        if hasattr(frame, "iloc"):
            if len(frame) == 0:
                return _no_financial(code)
            idx = [str(x) for x in list(frame.index)]
            if set(idx) & set(fields):
                # 转置朝向：行=指标，列=报告期 ⇒ 取最后一个报告期这一列
                col = list(frame.columns)[-1] if len(frame.columns) else None
                if col is None:
                    return _no_financial(code)
                row, report_time = frame[col], _as_report_date(col)
            else:
                # 常规朝向：行=报告期，列=指标
                row, report_time = frame.iloc[-1], _as_report_date(frame.index[-1])
        elif isinstance(frame, dict):
            items = list(frame.items())
            if not items:
                return _no_financial(code)
            if {str(k) for k in frame} & set(fields):
                # ★★ 实测形态（2026-09-21，本机 QMT 只有旧接口 ``get_financial_data``，
                # 无 ``get_stock_financial``）：返回 ``{代码: {指标: 值}}`` ——
                # **键是指标名，每个指标各自一张表**，而不是「报告期 → 指标字典」。
                #
                # 旧实现把 ``items[-1]`` 当「最新一期」，于是：
                #   ① 报告期取到**最后一个指标名** ``PARENT_NETPROFIT_YOY[:10]``
                #      ⇒ ``report_time = "PARENT_NET"``（实测 /reference/financial 原样输出）；
                #   ② ``row`` 是那张指标表（DataFrame），``row.get("EPS")`` 取不到任何列
                #      ⇒ 10 个指标**全是 null** ⇒ 估值维度永远 unavailable。
                # 界面表现：「基本面」里估值一栏空白，而报告期显示一个莫名其妙的
                # ``PARENT_NET`` —— 用户完全无从判断这是解析错了还是没有数据。
                row, report_time = _row_from_field_map(frame, fields)
                if all(v is None for v in row.values()):
                    return _no_financial(code)
            else:
                # 旧文档假设的形态：{报告期: {指标: 值}}
                k, v = items[-1]
                row = v if isinstance(v, dict) else {"value": v}
                report_time = _as_report_date(k)
        else:
            return _no_financial(code)

        out = {"code": code, "report_time": report_time}
        for f in fields:
            out[f] = _num_or_none(row.get(f))
        return out

    def get_l2_transactions(self, code: str, count: int = 100) -> list[dict]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            # 多版本参数顺序兼容：旧 SDK 为
            # get_l2_transaction(field_list=[], stock_code='', start_time='', end_time='',
            #                   count=-1)，首参是 field_list。用关键字 stock_code=/count=
            # 调用最稳；个别版本若关键字不接收则退化为位置参数。
            try:
                data = self._xtdata.get_l2_transaction(stock_code=code, count=int(count))
            except TypeError:
                data = self._xtdata.get_l2_transaction([], code, "", "", int(count))
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"L2 逐笔获取失败：{exc}") from exc
        df = (data or {}).get(code)
        if df is None or len(df) == 0:
            return []
        out = []
        for idx, row in df.iterrows():
            out.append({
                "time": str(idx)[11:19],
                "price": self._f(row.get("price")),
                "volume": int(row.get("volume") or 0),
                "type": "buy" if int(row.get("buyorsell") or 0) == 0 else "sell",
            })
        return out


__all__ = [
    'InstrumentMixin',
]
