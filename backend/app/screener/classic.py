"""经典选股策略（复刻 Sequoia-X 的形态/动量策略集）。

为什么独立成模块，而不是塞进 ``conditions.py`` 的条件树
-------------------------------------------------------
``conditions.py`` 是**单股、单值比较**的通用求值器（字段只有
close/open/high/low/volume，算子是 gt/lt/...，取值按 window 偏移）。而经典策略
本质上是**多日形态识别 + 横截面比较**，用条件树表达不了：

- 海龟「20 日新高」需要对区间取滚动最大值，而不只是「第 N 根的值」；
- 涨停/跌停识别需要逐日算涨跌幅再找极值；
- RPS（欧奈尔相对强度）是**全市场横截面排名** —— 单看一只股票的 K 线算不出来，
  必须先拿到全池的区间收益再排名。

因此这里实现为「策略函数」：每只股票给一段 K 线，返回 (是否命中, 明细指标)。
明细一并返回是为了让界面能解释「为什么选中它」（命中了哪几条），而不是黑箱。

数据源与口径
------------
- K 线按时间**升序**，最后一根为最新；消费 duck-typed Bar（close/open/high/low/
  volume/amount），与 ``engine.py`` 一致，不依赖具体类型。
- ``amount``（成交额，元）可能缺失，兜底按 ``close * volume * 100`` 估算
  （volume 单位为手）。
- 涨跌停阈值按 A 股通用 10% 设计（默认 9.8% 容差），ST/创业板/科创板各自不同，
  这里不猜板块，交由参数覆盖。

策略清单（对应 Sequoia-X）
--------------------------
turtle_trade        海龟突破：20 日新高 + 成交额过亿 + 阳线防诱多
ma_volume           均线放量突破：站上 MA20 + 短均多头 + 放量
high_tight_flag     高而窄的旗形整理突破：前期强势 + 窄幅整理 + 向上突破
limit_up_shakeout   涨停洗盘回踩确认：近期涨停 + 回踩不破位 + 回升确认
uptrend_limit_down  上升趋势中的跌停反包：MA20 之上且上行 + 昨日跌停 + 今日反包
rps_breakout        欧奈尔 RPS 突破：横截面 RPS 达阈值 + 创阶段新高
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "CLASSIC_STRATEGIES", "STRATEGY_IDS",
    "compute_rps", "period_return",
    "evaluate_classic", "run_classic", "strategy_meta",
]

# ---------------------------------------------------------------- 基础工具


def _val(bar: Any, name: str) -> Optional[float]:
    """读 Bar 字段并转 float；缺失/异常一律 None（不做 0 兜底）。"""
    try:
        v = getattr(bar, name, None)
    except Exception:  # noqa: BLE001 — 不同 Bar 实现的属性访问可能抛错
        return None
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _limit_pct(code: str, default: float) -> float:
    """按**板块**返回涨跌停阈值（正数，单位 %）—— 20cm 板块的关键。

    ★ 为什么不能写死 9.8（V11 R13）：A 股涨跌停幅度**按板块不同** ——
    主板 ±10%、创业板(300/301)/科创板(688)/北交所 ±20%、ST ±5%。
    项目里 :func:`datasource.board.limit_ratio` 已经算得很好，但选股引擎
    **完全没用它**，而是把阈值写死成 9.8% —— 于是对 20cm 板块：

    - ``limit_up_shakeout``（要求涨 >= 9.8%）会把 20cm 股票的**普通上涨**
      误判成涨停（阈值太低）；
    - ``uptrend_limit_down``（要求跌 <= -9.8%）在 20cm 板块**永远不成立**
      —— 它们跌停是 -20%，跌 -9.8% 根本不是跌停，该策略对创业板/科创板
      恒 0 命中。

    实测（2026-09-19，1500 只样本）：6 个策略里 ``uptrend_limit_down``
    是唯一 0 命中的，其余 5 个共命中 11 次。

    返回 ``default`` 的情形：板块无涨跌幅限制（可转债）或识别失败 ——
    此时保守沿用调用方给的默认幅度，不擅自放宽。
    """
    try:
        from datasource.board import limit_ratio
        r = limit_ratio(code)
    except Exception:  # noqa: BLE001 — 板块识别失败不得中断全市场扫描
        r = None
    if not r:          # None（无限制）/ 0（异常）
        return float(default)
    # 0.98 容差：除权、四舍五入导致实际涨跌幅略低于理论板幅（如 9.97%）
    return float(r) * 100.0 * 0.98


def _series(bars: Sequence[Any], name: str) -> List[Optional[float]]:
    return [_val(b, name) for b in bars]


def _clean(arr: Iterable[Optional[float]]) -> List[float]:
    return [v for v in arr if v is not None]


def _ma(arr: Sequence[Optional[float]], n: int) -> Optional[float]:
    """末 n 根简单均值；有效样本不足返回 None（宁缺勿算，避免用少量数据误导）。"""
    if n <= 0:
        return None
    tail = _clean(arr[-n:])
    if len(tail) < n:
        return None
    return sum(tail) / n


def _extreme(arr: Sequence[Optional[float]], n: int, mode: str) -> Optional[float]:
    tail = _clean(arr[-n:])
    if not tail:
        return None
    return max(tail) if mode == "max" else min(tail)


def _amount_at(bars: Sequence[Any], idx: int) -> Optional[float]:
    """成交额：优先 amount，缺失时用 close*volume*100 估算（volume 单位：手）。"""
    b = bars[idx]
    amt = _val(b, "amount")
    if amt:
        return amt
    close = _val(b, "close")
    vol = _val(b, "volume")
    if close and vol:
        return close * vol * 100.0
    return None


def period_return(bars: Sequence[Any], days: int) -> Optional[float]:
    """区间涨跌幅（%）：最新收盘相对 days 根之前。数据不足返回 None。"""
    if days <= 0 or len(bars) <= days:
        return None
    c_now = _val(bars[-1], "close")
    c_pre = _val(bars[-1 - days], "close")
    if not c_now or not c_pre:
        return None
    return (c_now / c_pre - 1.0) * 100.0


def _change_pct(bars: Sequence[Any], idx: int) -> Optional[float]:
    """第 idx 根相对前一根的涨跌幅（%）。"""
    if idx <= 0 or idx >= len(bars):
        return None
    c = _val(bars[idx], "close")
    p = _val(bars[idx - 1], "close")
    if not c or not p:
        return None
    return (c / p - 1.0) * 100.0


def compute_rps(returns_by_code: Dict[str, float]) -> Dict[str, float]:
    """欧奈尔 RPS：全池区间收益的**百分位排名**（0~100，越大越强）。

    RPS=90 表示跑赢池中 90% 的个股。这是横截面指标 —— 必须在全池上算，
    单只股票的 K 线里不含这个信息。有效样本不足 2 只时全部记 100（无参照系）。
    """
    items = [(c, r) for c, r in returns_by_code.items() if r is not None]
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 100.0}
    items.sort(key=lambda x: x[1])
    denom = n - 1
    return {code: round(i / denom * 100.0, 2) for i, (code, _r) in enumerate(items)}


# ---------------------------------------------------------------- 各策略实现
# 约定：返回 (是否命中, 明细 dict)。明细里的键同时用于界面解释命中原因。


def _st_turtle_trade(bars, p, code: str = "") -> Tuple[bool, Dict[str, Any]]:
    """海龟突破：创 N 日新高 + 成交额过阈值 + 阳线（防诱多）。"""
    n = int(p.get("breakout_days", 20))
    min_amount = float(p.get("min_amount", 1e8))
    if len(bars) < n + 1:
        return False, {"reason": "样本不足"}
    close = _val(bars[-1], "close")
    # ★ 突破的是「**前** N 日最高价」（不含当日）——若把当日也算进区间最大值，
    # 则 close >= max 会退化成「收盘必须等于当日最高价」，几乎永不成立。
    high_n = _extreme(_series(bars[:-1], "high"), n, "max")
    if not close or high_n is None:
        return False, {"reason": "行情缺失"}
    is_high = close > high_n
    amount = _amount_at(bars, -1)
    amount_ok = bool(amount and amount >= min_amount)
    open_ = _val(bars[-1], "open")
    yang = bool(open_ and close > open_)          # 阳线：收 > 开
    ok = is_high and amount_ok and yang
    return ok, {
        "close": close, "high_%d" % n: high_n, "is_new_high": is_high,
        "amount": amount, "amount_ok": amount_ok, "yang_line": yang,
        "reason": "创%d日新高+成交额达标+阳线" % n if ok else "未同时满足（新高/成交额/阳线）",
    }


def _st_ma_volume(bars, p, code: str = "") -> Tuple[bool, Dict[str, Any]]:
    """均线 + 放量突破：收盘站上 MA(ma_n)，短期均线上穿长期均线，成交量放大。"""
    ma_n = int(p.get("ma_n", 20))
    fast = int(p.get("fast_n", 5))
    vol_n = int(p.get("vol_ma_n", 5))
    vol_mult = float(p.get("vol_mult", 1.8))
    if len(bars) < max(ma_n, vol_n) + 1:
        return False, {"reason": "样本不足"}
    closes = _series(bars, "close")
    vols = _series(bars, "volume")
    close = _clean(closes[-1:])
    if not close:
        return False, {"reason": "行情缺失"}
    close = close[0]
    ma = _ma(closes, ma_n)
    ma_fast = _ma(closes, fast)
    vol = _clean(vols[-1:])
    vol_ma = _ma(vols, vol_n)
    if ma is None or ma_fast is None or not vol or vol_ma is None or vol_ma <= 0:
        return False, {"reason": "指标不足"}
    above_ma = close > ma
    golden = ma_fast > ma
    vol_ok = vol[0] >= vol_ma * vol_mult
    ok = above_ma and golden and vol_ok
    return ok, {
        "close": close, "ma%d" % ma_n: round(ma, 4), "ma%d" % fast: round(ma_fast, 4),
        "volume": vol[0], "vol_ma%d" % vol_n: round(vol_ma, 2),
        "vol_ratio": round(vol[0] / vol_ma, 3),
        "above_ma": above_ma, "golden_cross": golden, "volume_surge": vol_ok,
        "reason": "站上均线+多头+放量" if ok else "未同时满足（站上均线/多头/放量）",
    }


def _st_high_tight_flag(bars, p, code: str = "") -> Tuple[bool, Dict[str, Any]]:
    """高而窄的旗形整理突破：前期大幅上涨 → 窄幅横盘整理 → 放量向上突破。"""
    lookback = int(p.get("lookback", 60))
    flag_days = int(p.get("flag_days", 10))
    min_gain = float(p.get("min_prior_gain", 30.0))   # 前期最小涨幅 %
    tight = float(p.get("tight_pct", 15.0))           # 旗形振幅上限 %
    if len(bars) < lookback + flag_days:
        return False, {"reason": "样本不足"}
    # 1) 前期强势：回溯窗口内 最低价→最高价 的涨幅
    seg_low = _extreme(_series(bars[:-flag_days], "low"), lookback, "min")
    seg_high = _extreme(_series(bars[:-flag_days], "high"), lookback, "max")
    prior_gain = ((seg_high / seg_low - 1.0) * 100.0) if (seg_low and seg_high) else None
    strong = bool(prior_gain is not None and prior_gain >= min_gain)
    # 2) 旗形整理：最近 flag_days 根的振幅足够窄
    f_high = _extreme(_series(bars, "high"), flag_days, "max")
    f_low = _extreme(_series(bars, "low"), flag_days, "min")
    range_pct = ((f_high - f_low) / f_low * 100.0) if (f_low and f_high) else None
    tight_ok = bool(range_pct is not None and range_pct <= tight)
    # 3) 突破：最新收盘突破整理区高点（不含当天，避免自我比较）
    prev_high = _extreme(_series(bars[:-1], "high"), flag_days, "max")
    close = _val(bars[-1], "close")
    breakout = bool(close and prev_high and close > prev_high)
    ok = strong and tight_ok and breakout
    return ok, {
        "prior_gain_pct": None if prior_gain is None else round(prior_gain, 2),
        "flag_range_pct": None if range_pct is None else round(range_pct, 2),
        "breakout": breakout, "close": close, "flag_high": prev_high,
        "reason": "前期强势+窄幅整理+向上突破" if ok else "未同时满足（强势/窄幅/突破）",
    }


def _st_limit_up_shakeout(bars, p, code: str = "") -> Tuple[bool, Dict[str, Any]]:
    """涨停洗盘回踩确认：近期出现过涨停 → 之后回踩但不破涨停日最低价 → 再度转强。"""
    window = int(p.get("window", 10))
    # 阈值默认按**板块**自动取（20cm 板块是 19.6 而非 9.8）；用户显式传则尊重。
    _lu = p.get("limit_up_pct")
    lu_pct = float(_lu) if _lu is not None else _limit_pct(code, 9.8)
    if len(bars) < window + 2:
        return False, {"reason": "样本不足"}
    # 1) 找窗口内最近的涨停日
    lu_idx = None
    for i in range(len(bars) - 1, len(bars) - 1 - window, -1):
        cp = _change_pct(bars, i)
        if cp is not None and cp >= lu_pct:
            lu_idx = i
            break
    if lu_idx is None:
        return False, {"reason": "窗口内无涨停"}
    lu_low = _val(bars[lu_idx], "low")
    # 2) 回踩不破位：涨停之后的最低价未跌破涨停日最低价（留 buffer 容差）
    buffer = float(p.get("break_buffer", 0.0))       # 允许跌破的比例（默认 0 = 严格不破）
    after_lows = _clean(_val(b, "low") for b in bars[lu_idx + 1:])
    min_after = min(after_lows) if after_lows else None
    hold = bool(lu_low and min_after is not None
                and min_after >= lu_low * (1.0 - buffer))
    # 3) 再度转强：最新一根收阳且高于前收
    close = _val(bars[-1], "close")
    open_ = _val(bars[-1], "open")
    prev_close = _val(bars[-2], "close")
    recover = bool(close and open_ and prev_close and close > open_ and close > prev_close)
    ok = hold and recover
    return ok, {
        "limit_up_idx": lu_idx, "limit_up_low": lu_low,
        "lowest_after": min_after, "hold_support": hold, "recover": recover,
        "reason": "涨停后回踩不破位且再度转强" if ok else "破位或未转强",
    }


def _st_uptrend_limit_down(bars, p, code: str = "") -> Tuple[bool, Dict[str, Any]]:
    """上升趋势中的跌停反包：MA20 之上且均线向上 → 昨日跌停 → 今日收盘吞没昨日最高价。"""
    ma_n = int(p.get("ma_n", 20))
    slope_n = int(p.get("slope_n", 5))
    # 同上：跌停阈值按板块自动取。★ 写死 -9.8 会让该策略对创业板/科创板
    # **恒不成立**（它们跌停是 -20%），实测 1500 只样本下 0 命中即由此而来。
    _ld = p.get("limit_down_pct")
    ld_pct = float(_ld) if _ld is not None else -_limit_pct(code, 9.8)
    if len(bars) < ma_n + slope_n + 2:
        return False, {"reason": "样本不足"}
    closes = _series(bars, "close")
    close = _clean(closes[-1:])
    if not close:
        return False, {"reason": "行情缺失"}
    close = close[0]
    ma = _ma(closes, ma_n)
    ma_prev = _ma(closes[:-slope_n], ma_n)
    if ma is None or ma_prev is None:
        return False, {"reason": "指标不足"}
    uptrend = close > ma and ma > ma_prev           # 价在均线上 + 均线本身上行
    prev_cp = _change_pct(bars, len(bars) - 2)       # 昨日涨跌幅
    had_limit_down = bool(prev_cp is not None and prev_cp <= ld_pct)
    prev_high = _val(bars[-2], "high")
    engulf = bool(prev_high and close > prev_high)   # 反包：收复昨日最高价
    ok = uptrend and had_limit_down and engulf
    return ok, {
        "close": close, "ma%d" % ma_n: round(ma, 4), "uptrend": uptrend,
        "prev_change_pct": None if prev_cp is None else round(prev_cp, 2),
        "had_limit_down": had_limit_down, "engulf": engulf,
        "reason": "上升趋势+昨日跌停+今日反包" if ok else "未同时满足（趋势/跌停/反包）",
    }


def _st_rps_breakout(bars, p, rps: Optional[float], code: str = "") -> Tuple[bool, Dict[str, Any]]:
    """欧奈尔 RPS 突破：横截面 RPS 达阈值 + 创阶段新高（或站上均线）。"""
    min_rps = float(p.get("min_rps", 87.0))
    n = int(p.get("breakout_days", 20))
    if rps is None:
        return False, {"reason": "缺少 RPS（需全池计算）"}
    if len(bars) < n + 1:
        return False, {"reason": "样本不足"}
    close = _val(bars[-1], "close")
    # 同海龟：突破「前 N 日」最高价，不含当日
    high_n = _extreme(_series(bars[:-1], "high"), n, "max")
    if not close or high_n is None:
        return False, {"reason": "行情缺失"}
    rps_ok = rps >= min_rps
    is_high = close > high_n
    ok = rps_ok and is_high
    return ok, {
        "rps": rps, "min_rps": min_rps, "rps_ok": rps_ok,
        "close": close, "high_%d" % n: high_n, "is_new_high": is_high,
        "reason": "RPS达标+创%d日新高" % n if ok else "RPS或突破条件未满足",
    }


# ---------------------------------------------------------------- 注册表

CLASSIC_STRATEGIES: Dict[str, Dict[str, Any]] = {
    "turtle_trade": {
        "label": "海龟突破",
        "desc": "创 N 日新高 + 成交额过亿 + 阳线防诱多（经典海龟入场信号）",
        "params": {"breakout_days": 20, "min_amount": 1e8},
        "fn": _st_turtle_trade,
    },
    "ma_volume": {
        "label": "均线放量突破",
        "desc": "站上 MA20 且短均多头，伴随成交量显著放大",
        "params": {"ma_n": 20, "fast_n": 5, "vol_ma_n": 5, "vol_mult": 1.8},
        "fn": _st_ma_volume,
    },
    "high_tight_flag": {
        "label": "高窄旗形突破",
        "desc": "前期大幅上涨后窄幅横盘整理，再向上突破整理区高点",
        "params": {"lookback": 60, "flag_days": 10, "min_prior_gain": 30.0,
                   "tight_pct": 15.0},
        "fn": _st_high_tight_flag,
    },
    "limit_up_shakeout": {
        "label": "涨停洗盘回踩",
        "desc": "近期涨停后回踩不破涨停日最低价，随后再度转强确认",
        "params": {"window": 10, "limit_up_pct": None, "break_buffer": 0.0},
        "fn": _st_limit_up_shakeout,
    },
    "uptrend_limit_down": {
        "label": "上升趋势跌停反包",
        "desc": "均线之上且均线向上，昨日跌停后今日收盘吞没昨日最高价",
        "params": {"ma_n": 20, "slope_n": 5, "limit_down_pct": None},
        "fn": _st_uptrend_limit_down,
    },
    "rps_breakout": {
        "label": "RPS 强度突破",
        "desc": "欧奈尔相对强度 RPS 达阈值（横截面排名）且创阶段新高",
        "params": {"min_rps": 87.0, "breakout_days": 20, "rps_days": 20},
        "fn": _st_rps_breakout,
    },
}

STRATEGY_IDS: Tuple[str, ...] = tuple(CLASSIC_STRATEGIES.keys())


def strategy_meta(strategy_id: str) -> Optional[Dict[str, Any]]:
    """取策略元信息（不含可调用对象），供接口/界面列举策略用。"""
    s = CLASSIC_STRATEGIES.get(strategy_id)
    if not s:
        return None
    return {"id": strategy_id, "label": s["label"], "desc": s["desc"],
            "params": dict(s["params"])}


def list_strategies() -> List[Dict[str, Any]]:
    return [strategy_meta(sid) for sid in STRATEGY_IDS if strategy_meta(sid)]


# ---------------------------------------------------------------- 对外入口


def evaluate_classic(bars: Sequence[Any], strategy_id: str,
                     params: Optional[Dict[str, Any]] = None,
                     rps: Optional[float] = None,
                     code: str = "") -> Tuple[bool, Dict[str, Any]]:
    """对单只股票的 K 线执行一个经典策略。

    ``code``：标的代码，供**按板块**判定涨跌停幅度（20cm 板块是 ±20% 不是 ±10%）。
    缺省为空 ⇒ 策略退回保守的主板幅度，不会因缺代码而崩。

    返回 ``(是否命中, 明细)``。未知策略 / 数据不足都返回 ``(False, {...})``，
    **不抛异常** —— 全市场扫描时不能让一只股票的脏数据中断整轮。
    """
    spec = CLASSIC_STRATEGIES.get(strategy_id)
    if not spec:
        return False, {"reason": "未知策略：%s" % strategy_id}
    if not bars:
        return False, {"reason": "无K线数据"}
    p = dict(spec["params"])
    p.update(params or {})
    try:
        fn = spec["fn"]
        if strategy_id == "rps_breakout":
            return fn(bars, p, rps, code=code)
        return fn(bars, p, code=code)
    except Exception as exc:  # noqa: BLE001 — 单只失败不影响整轮扫描
        return False, {"reason": "计算异常：%s" % exc}


def run_classic(bars_by_code: Dict[str, Sequence[Any]], strategy_id: str,
                params: Optional[Dict[str, Any]] = None,
                limit: int = 0,
                names: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """对一批股票执行一个经典策略，返回命中列表（含明细，便于界面解释）。

    ``rps_breakout`` 会先在全池上算区间收益并排名得到 RPS —— 这是横截面指标，
    必须两步走：先排名，再逐只判断。

    ``names``：股票池的代码→名称映射（来自 ``resolve_universe``，已含本地名称表补全）。
    ★ 2026-09-20 补：此前本函数**完全不设 ``name`` 字段**，导致经典策略结果在界面
    「名称」列只能显示占位符，落库 ``screen_picks.name`` 也恒为 ``''``（实测）。
    与条件选股结果同构是硬要求 —— 两条链路的结果都进同一张表。
    """
    if strategy_id not in CLASSIC_STRATEGIES:
        return []
    p = dict(CLASSIC_STRATEGIES[strategy_id]["params"])
    p.update(params or {})

    rps_map: Dict[str, float] = {}
    if strategy_id == "rps_breakout":
        days = int(p.get("rps_days", 20))
        rets = {code: period_return(bars, days) for code, bars in bars_by_code.items()}
        rps_map = compute_rps({c: r for c, r in rets.items() if r is not None})

    out: List[Dict[str, Any]] = []
    for code, bars in bars_by_code.items():
        ok, detail = evaluate_classic(bars, strategy_id, p, rps_map.get(code),
                                      code=code)
        if not ok:
            continue
        row = {"code": code, "strategy": strategy_id}
        # 名称：查不到就留空（零 mock，绝不编造），由前端显式渲染成占位符。
        row["name"] = str((names or {}).get(code, "") or "")
        # 补 close / change_pct，使结果与既有条件选股**同构**：
        # 引擎按 _SORT_KEYS(score/change_pct/close/volume) 排序，界面也读这两个字段，
        # 缺了会导致经典策略结果排不了序、界面显示空白。
        row["close"] = _val(bars[-1], "close") if bars else None
        row["change_pct"] = period_return(bars, 1)
        row.update(detail)
        out.append(row)

    # 排序：海龟按成交额（越活跃越优先）；其余按当日涨跌幅
    if strategy_id == "turtle_trade":
        out.sort(key=lambda r: (r.get("amount") or 0), reverse=True)
    else:
        out.sort(key=lambda r: (r.get("change_pct") or 0), reverse=True)
    return out[:limit] if limit and limit > 0 else out
