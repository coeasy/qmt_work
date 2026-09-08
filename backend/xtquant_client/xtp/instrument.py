"""合约：名称表 / 涨跌停 / 板块 / 日历 / 财务 / L2（InstrumentMixin，自原 xtp.py 逐行搬移）。"""

from ..base import BrokerNotConnectedError, BrokerSDKError



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
            if params and params[0].lower().startswith("market"):
                cal = self._xtdata.get_trading_calendar("SH", start or "", end or "")
            else:
                cal = self._xtdata.get_trading_calendar(start or "", end or "")
            return [str(d) for d in (cal or [])]
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"交易日历获取失败：{exc}") from exc

    def get_financial(self, code: str) -> dict:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        fields = ["EPS", "BPS", "OPERATE_INCOME", "TOTAL_OPERATE_INCOME",
                  "PARENT_NETPROFIT", "TOTAL_OPERATE_EXPENSE", "ROE", "CAPITAL",
                  "TOTAL_OPERATE_INCOME_YOY", "PARENT_NETPROFIT_YOY"]
        try:
            df = self._xtdata.get_stock_financial([code], fields, "", "",
                                                  report_type="report_time")
        except Exception as exc:  # noqa: BLE001
            raise BrokerNotConnectedError(f"财务数据获取失败：{exc}") from exc
        frame = (df or {}).get(code)
        if frame is None or len(frame) == 0:
            return {"code": code, "detail": "无财务数据（数据权限或代码无效）"}
        row = frame.iloc[-1]
        out = {"code": code, "report_time": str(frame.index[-1])[:10]}
        for f in fields:
            try:
                v = row.get(f)
                out[f] = None if v is None else round(float(v), 4)
            except Exception:  # noqa: BLE001
                out[f] = None
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
