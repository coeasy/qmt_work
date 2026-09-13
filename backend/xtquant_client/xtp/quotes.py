"""行情：tick / K 线 / 订阅（QuotesMixin，自原 xtp.py 逐行搬移）。"""

from datetime import datetime

from ..base import BrokerNotConnectedError, BrokerSDKError
from ._common import _dget, _normalize_kline_period, log


class QuotesMixin:


    def get_quote(self, code: str) -> dict:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        tick = self._xtdata.get_full_tick([code]).get(code)
        if not tick:
            raise BrokerNotConnectedError(f"未获取到 {code} 行情（客户端未运行或代码无效）")
        return self._norm_quote(code, tick)

    def get_full_tick(self, codes: list[str]) -> dict:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        try:
            raw = self._xtdata.get_full_tick(list(codes)) or {}
        except Exception as exc:  # noqa: BLE001
            # 行情服务未认证 / 非交易时段 / 客户端未订阅代码时，xtquant 的
            # get_full_tick 可能抛 JSONDecodeError（内部把错误当响应解析）或
            # 其它 SDK 异常。此时不应向上抛协议级错误（会被桥接包装成含糊的
            # "Expecting value: 连接异常"），而应返回空行情，由调用方给出
            # 「客户端已连但行情未就绪」的明确诊断。
            log.warning("get_full_tick 异常（cast 未认证/非交易时段?）：%s: %s",
                        type(exc).__name__, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        out = {}
        for c in list(codes):
            t = raw.get(c)
            if t and isinstance(t, dict):
                out[c] = self._norm_quote(c, t)
        return out

    def _norm_quote(self, code: str, tick: dict) -> dict:
        def _lst(v, i):
            return v[i] if isinstance(v, (list, tuple)) and len(v) > i else (v if not isinstance(v, (list, tuple)) else None)
        # 五档买卖盘：xtquant get_full_tick 的 bidPrice/askPrice/bidVolume/askVolume
        # 均为长度 5 的数组（买一~买五 / 卖一~卖五）。归一化为 bids/asks 数组供前端展示。
        bid_prices = _dget(tick, "bidPrice", "bid_price") or []
        ask_prices = _dget(tick, "askPrice", "ask_price") or []
        bid_vols = _dget(tick, "bidVolume", "bid_volume") or []
        ask_vols = _dget(tick, "askVolume", "ask_volume") or []
        bids = [{"price": _lst(bid_prices, i), "volume": _lst(bid_vols, i)}
                for i in range(5)]
        asks = [{"price": _lst(ask_prices, i), "volume": _lst(ask_vols, i)}
                for i in range(5)]
        # 键名兼容：tick 快照用 CamelCase（lastPrice/lastClose/bidPrice），K 线 bar 用
        # 小写（close/open/high/low/volume/amount）。last 缺失用 close 兜底（K 线 bar
        # 无最新价字段）、lastClose 缺失用 preClose/pre_close 兜底，避免旧版 K 线订阅
        # 推送的行情 last/lastClose 为 None。
        return {
            "code": code,
            "last": _dget(tick, "lastPrice", "close"),
            "open": _dget(tick, "open", "Open"),
            "high": _dget(tick, "high", "High"),
            "low": _dget(tick, "low", "Low"),
            "lastClose": _dget(tick, "lastClose", "preClose", "pre_close"),
            "volume": _dget(tick, "volume", "Volume"),
            "amount": _dget(tick, "amount", "Amount"),
            "bid": _lst(_dget(tick, "bidPrice", "bid_price"), 0),
            "ask": _lst(_dget(tick, "askPrice", "ask_price"), 0),
            "bid_vol": _lst(_dget(tick, "bidVolume", "bid_volume"), 0),
            "ask_vol": _lst(_dget(tick, "askVolume", "ask_volume"), 0),
            "bids": bids,
            "asks": asks,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }

    def _kline_lookback_days(self, period: str, count: int) -> int:
        """K 线 count 换算成回看日历天数，供下载预热窗口使用（含缓冲）。"""
        need = {
            "1m": count // 240, "5m": count // 48, "15m": count // 16,
            "30m": count // 8, "1h": count // 4, "1d": count,
            "1w": count * 7, "1mon": count * 30,
        }.get(period, count * 2)
        return max(need + 2, 1)

    def _warm_kline_cache(self, code: str, period: str, count: int,
                          start: str, end: str) -> bool:
        """本地缓存无数据（行情服务离线/未预热）时用 download_history_data 预热。

        仅在 get_kline 空结果时触发一次：先下载历史数据到本地缓存，主进程随后
        重查 get_market_data 即可读到。download 每次都会快速增量（本地已有则复用），
        典型耗时 ~1s，远低于 RPC 超时（30s），不会阻塞。多版本签名容错。
        """
        try:
            from datetime import timedelta
            fn = self._xtdata.download_history_data
            if not start:
                try:
                    from datetime import datetime
                    d0 = datetime.now() - timedelta(days=self._kline_lookback_days(period, count))
                    start = d0.strftime("%Y%m%d")
                except Exception:  # noqa: BLE001
                    start = ""
            # 多签名兼容：download_history_data(stock_code, period, start, end)
            # 或 download_history_data(stock_list, period, start, end)
            try:
                fn(code, period, start or "", end or "")
            except TypeError:
                fn([code], period, start or "", end or "")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("get_kline 预热下载失败（忽略）：%s", exc)
            return False

    def get_kline(self, code: str, period: str, count: int,
                  start: str = "", end: str = "", adjust: str | None = None) -> list[dict]:
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        # 迅投协议周期：各版本 xtdata 均用 "1h"（本地缓存集合 {1m,5m,15m,30m,1h,1d}），
        # 平台展示用 "60m" 只是别名——必须归一化，否则旧版 get_market_data("60m") 失败。
        period = _normalize_kline_period(period)
        field_list = ["open", "high", "low", "close", "volume", "amount"]
        # 复权打通（D9 / P0-15之③）：dividend_type 参数化，qfq->front、hfq->back、
        # 其余->none。原先硬编码 "none" 导致 QMT 历史 K 线永远不复权，与选股/其他源
        # 口径不一致（选股读到的 QMT 行情是未复权，而其他源是 qfq）。
        dividend_type = {"qfq": "front", "hfq": "back"}.get(adjust or "", "none")

        def _fetch() -> dict:
            try:
                return self._xtdata.get_market_data(
                    field_list=field_list, stock_list=[code], period=period,
                    start_time=start or "", end_time=end or "", count=int(count),
                    dividend_type=dividend_type, fill_data=True)
            except Exception as exc:  # noqa: BLE001
                raise BrokerNotConnectedError(f"K 线获取失败：{exc}") from exc

        data = _fetch()
        # 空结果兜底：本地缓存未就绪（行情服务离线 / 从未下载过该标的）时，首次
        # get_market_data 恒返回空。自动下载预热并重查一次，避免「非交易时段
        # 查看历史 K 线恒为空」。download 已就绪时增量很快，重查命中缓存。
        if not isinstance(data, dict) or not data:
            if self._warm_kline_cache(code, period, int(count), start, end):
                data = _fetch()
        if not isinstance(data, dict) or not data:
            return []
        # 迅投 get_market_data(field_list=..., stock_list=[...]) 返回
        # {字段名: DataFrame}——每个 DataFrame 的 index=股票代码、columns=日期。
        # 注意不是 {code: DataFrame}！旧实现按 data.get(code) 解析永远取不到，
        # 导致 get_kline 恒返回空条（K 线为空的根因，见阶段排查）。
        df0 = next(iter(data.values()))
        if df0 is None or len(df0) == 0:
            return []
        dates = list(df0.columns)
        out = []
        for dt in dates:
            bar = {"time": str(dt)[:19]}
            for fld in field_list:
                sub = data.get(fld)
                val = None
                if sub is not None and code in sub.index and dt in sub.columns:
                    val = self._f(sub.loc[code, dt])
                    # NaN 归一为 None，避免 JSON 序列化 nan
                    if val is not None and val != val:
                        val = None
                bar[fld] = val
            out.append(bar)
        return out

    @staticmethod
    def _f(v):
        try:
            return None if v is None else round(float(v), 4)
        except Exception:  # noqa: BLE001
            return None

    def get_tick(self, code: str) -> dict:
        return self.get_quote(code)

    def subscribe_quote(self, codes: list[str], on_tick, period: str = "1m") -> None:
        """订阅实时行情。

        阶段 0-A（C8）：
        - period 可指定 "tick"（真实逐笔/快照）或 "1m"（1 分钟 K 线）；默认 1m 保持兼容。
        - 去重订阅（重复 symbols 不重复调 SDK，避免回调累积）。
        - 回调异常不再 ``except: pass`` 静默吞掉，改为记日志便于排查。
        - 回调内**不**同步重调 get_full_tick（避免阻塞推送线程），优先使用回调带入的 bar。
        """
        if self._xtdata is None:
            raise BrokerSDKError("xtquant", "pip install xtquant")
        period = _normalize_kline_period(period)  # "60m" -> "1h"（迅投协议）
        # 去重并保持顺序
        codes = list(dict.fromkeys(codes))

        def _cb(datas):
            try:
                for code, per in (datas or {}).items():
                    bars = None
                    if isinstance(per, dict):
                        # 新版 xtdata：{code: {period: [bars]}} —— 取任一有数据的周期最后一根
                        for _p, _bars in per.items():
                            if isinstance(_bars, list) and _bars:
                                bars = _bars
                                break
                    elif isinstance(per, list) and per:
                        # 旧版 xtdata：{code: [data1, data2, ...]} —— 值直接是 bar 列表
                        bars = per
                    if not bars:
                        continue
                    bar = bars[-1]
                    if not isinstance(bar, dict):
                        continue
                    on_tick({"type": "quote", "data": self._norm_quote(code, bar)})
            except Exception as exc:  # noqa: BLE001
                log.warning("subscribe_quote callback error: %s", exc)

        for c in codes:
            try:
                self._xtdata.subscribe_quote(c, period=period, count=0, callback=_cb)
            except Exception as exc:  # noqa: BLE001
                log.warning("subscribe_quote failed for %s: %s", c, exc)


__all__ = [
    'QuotesMixin',
]
