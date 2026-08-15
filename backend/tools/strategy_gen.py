"""策略模板库（借鉴 QMT-MCP 的 generate_ma_strategy / save_qmt_strategy 能力）。

- generate_strategy：生成 QMT 可运行的策略代码（ma_cross / macd / rsi / limitup 四类模板）
- save_qmt_strategy：把策略代码写入 QMT 客户端本地目录（默认 userdata_mini 同级 mpython）
"""
import logging
import os

log = logging.getLogger("qmt_work")

_TEMPLATES = {
    "ma_cross": """# -*- coding: utf-8 -*-
# 双均线金叉/死叉策略（由 QMT Agent 平台生成）
import numpy as np
from xtquant.xttype import StockAccount
from xtquant.xttrader import XtQuantTrader
from xtquant import xtconstant

CLIENT_PATH = "{client_path}"
ACCOUNT = "{account_id}"
CODE = "{code}"
FAST = {fast}
SLOW = {slow}
VOLUME = {volume}

trader = XtQuantTrader(CLIENT_PATH, 1)
trader.start()
acc = StockAccount(ACCOUNT)
trader.subscribe(acc)

def on_bar(datas):
    bars = datas.get(CODE, {{}})
    closes = [b.close for _, b in sorted(bars.items())]
    if len(closes) < SLOW + 1:
        return
    fast_ma = np.mean(closes[-FAST:])
    slow_ma = np.mean(closes[-SLOW:])
    prev_fast = np.mean(closes[-FAST-1:-1])
    prev_slow = np.mean(closes[-SLOW-1:-1])
    pos = trader.query_stock_positions(acc)
    held = any(p.stock_code == CODE and p.volume > 0 for p in pos)
    if prev_fast <= prev_slow and fast_ma > slow_ma and not held:
        trader.order_stock(acc, CODE, xtconstant.STOCK_BUY,
                           xtconstant.FIX_PRICE, closes[-1], VOLUME, 'ma_cross', '')
    elif prev_fast >= prev_slow and fast_ma < slow_ma and held:
        for p in pos:
            if p.stock_code == CODE and p.volume > 0:
                trader.order_stock(acc, CODE, xtconstant.STOCK_SELL,
                                   xtconstant.FIX_PRICE, closes[-1], p.volume, 'ma_cross', '')
""",
    "macd": """# -*- coding: utf-8 -*-
# MACD 金叉/死叉策略（由 QMT Agent 平台生成）
import numpy as np
from xtquant.xttype import StockAccount
from xtquant.xttrader import XtQuantTrader
from xtquant import xtconstant

CLIENT_PATH = "{client_path}"
ACCOUNT = "{account_id}"
CODE = "{code}"
FAST, SLOW, SIGNAL = {fast}, {slow}, {signal}
VOLUME = {volume}

trader = XtQuantTrader(CLIENT_PATH, 1)
trader.start()
acc = StockAccount(ACCOUNT)
trader.subscribe(acc)

def ema(vals, n):
    w = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * w + out[-1] * (1 - w))
    return out

def on_bar(datas):
    bars = datas.get(CODE, {{}})
    closes = [b.close for _, b in sorted(bars.items())]
    if len(closes) < SLOW + SIGNAL + 2:
        return
    dif = [e1 - e2 for e1, e2 in zip(ema(closes, FAST), ema(closes, SLOW))]
    dea = ema(dif, SIGNAL)
    if len(dea) < 2:
        return
    cross_up = dea[-2] <= dif[-2] and dea[-1] > dif[-1]
    cross_dn = dea[-2] >= dif[-2] and dea[-1] < dif[-1]
    pos = trader.query_stock_positions(acc)
    held = any(p.stock_code == CODE and p.volume > 0 for p in pos)
    if cross_up and not held:
        trader.order_stock(acc, CODE, xtconstant.STOCK_BUY,
                           xtconstant.FIX_PRICE, closes[-1], VOLUME, 'macd', '')
    elif cross_dn and held:
        for p in pos:
            if p.stock_code == CODE and p.volume > 0:
                trader.order_stock(acc, CODE, xtconstant.STOCK_SELL,
                                   xtconstant.FIX_PRICE, closes[-1], p.volume, 'macd', '')
""",
    "rsi": """# -*- coding: utf-8 -*-
# RSI 超买超卖策略（由 QMT Agent 平台生成）
import numpy as np
from xtquant.xttype import StockAccount
from xtquant.xttrader import XtQuantTrader
from xtquant import xtconstant

CLIENT_PATH = "{client_path}"
ACCOUNT = "{account_id}"
CODE = "{code}"
PERIOD = {period}
BUY_AT, SELL_AT = {buy_at}, {sell_at}
VOLUME = {volume}

trader = XtQuantTrader(CLIENT_PATH, 1)
trader.start()
acc = StockAccount(ACCOUNT)
trader.subscribe(acc)

def rsi(closes, n):
    if len(closes) < n + 1:
        return 50.0
    diffs = np.diff(closes[-n-1:])
    gain = np.mean(np.clip(diffs, 0, None))
    loss = -np.mean(np.clip(diffs, None, 0))
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)

def on_bar(datas):
    bars = datas.get(CODE, {{}})
    closes = [b.close for _, b in sorted(bars.items())]
    if len(closes) < PERIOD + 2:
        return
    r = rsi(closes, PERIOD)
    pos = trader.query_stock_positions(acc)
    held = any(p.stock_code == CODE and p.volume > 0 for p in pos)
    if r < BUY_AT and not held:
        trader.order_stock(acc, CODE, xtconstant.STOCK_BUY,
                           xtconstant.FIX_PRICE, closes[-1], VOLUME, 'rsi', '')
    elif r > SELL_AT and held:
        for p in pos:
            if p.stock_code == CODE and p.volume > 0:
                trader.order_stock(acc, CODE, xtconstant.STOCK_SELL,
                                   xtconstant.FIX_PRICE, closes[-1], p.volume, 'rsi', '')
""",
    "limitup": """# -*- coding: utf-8 -*-
# 涨停监控打板策略（由 QMT Agent 平台生成，三因子：涨停+时间窗+tick涨幅）
import time
from xtquant.xttype import StockAccount
from xtquant.xttrader import XtQuantTrader
from xtquant import xtconstant, xtdata

CLIENT_PATH = "{client_path}"
ACCOUNT = "{account_id}"
CODES = [{codes}]
LIMIT_PCT = {limit_pct}
CUTOFF = "{cutoff}"
BUY_VOLUME = {buy_volume}

trader = XtQuantTrader(CLIENT_PATH, 1)
trader.start()
acc = StockAccount(ACCOUNT)
trader.subscribe(acc)
bought = set()

def on_tick(datas):
    now = time.strftime('%H:%M')
    if now > CUTOFF:
        return
    for code in CODES:
        if code in bought:
            continue
        ticks = xtdata.get_full_tick([code])
        t = (ticks or {{}}).get(code)
        if not t:
            continue
        last, lc = t.get('lastPrice'), t.get('lastClose')
        if not last or not lc:
            continue
        if last >= round(lc * (1 + LIMIT_PCT), 2):
            trader.order_stock(acc, code, xtconstant.STOCK_BUY,
                               xtconstant.FIX_PRICE, last, BUY_VOLUME, 'limitup', '')
            bought.add(code)

for c in CODES:
    xtdata.subscribe_quote(c, period='tick', count=0, callback=on_tick)
while True:
    time.sleep(1)
""",
}


def generate_strategy(strategy_type: str, code: str = "600519.SH",
                      client_path: str = "", account_id: str = "",
                      params: dict | None = None) -> dict:
    """生成 QMT 可运行的策略代码。strategy_type: ma_cross/macd/rsi/limitup。"""
    st = (strategy_type or "").strip().lower()
    if st not in _TEMPLATES:
        raise ValueError(f"未知策略类型：{st}，支持 {list(_TEMPLATES)}")
    p = dict(params or {})
    if st == "ma_cross":
        ctx = {"client_path": client_path, "account_id": account_id, "code": code,
               "fast": int(p.get("fast", 5)), "slow": int(p.get("slow", 20)),
               "volume": int(p.get("volume", 100))}
    elif st == "macd":
        ctx = {"client_path": client_path, "account_id": account_id, "code": code,
               "fast": int(p.get("fast", 12)), "slow": int(p.get("slow", 26)),
               "signal": int(p.get("signal", 9)), "volume": int(p.get("volume", 100))}
    elif st == "rsi":
        ctx = {"client_path": client_path, "account_id": account_id, "code": code,
               "period": int(p.get("period", 14)), "buy_at": float(p.get("buy_at", 30)),
               "sell_at": float(p.get("sell_at", 70)), "volume": int(p.get("volume", 100))}
    else:  # limitup
        codes = ",".join(f'"{c.strip()}"' for c in
                         str(p.get("codes", code)).replace("，", ",").split(",") if c.strip())
        ctx = {"client_path": client_path, "account_id": account_id, "codes": codes,
               "limit_pct": float(p.get("limit_pct", 0.1)),
               "cutoff": str(p.get("cutoff", "10:00")),
               "buy_volume": int(p.get("buy_volume", 100))}
    return {"strategy_type": st, "code": code, "params": ctx, "content": _TEMPLATES[st].format(**ctx)}


def save_qmt_strategy(filename: str, content: str, client_path: str = "") -> dict:
    """把策略代码写入 QMT 客户端本地 mpython 目录（QMT 策略端可导入运行）。

    目录规则：client_path（userdata_mini 目录）同级存在则写 {client_path}/mpython；
    否则写当前目录下的 qmt_strategies/。
    """
    if not filename.endswith(".py"):
        filename += ".py"
    target = ""
    if client_path and os.path.isdir(client_path):
        mpython = os.path.join(client_path, "mpython")
        os.makedirs(mpython, exist_ok=True)
        target = os.path.join(mpython, filename)
    else:
        base = os.path.join(os.getcwd(), "qmt_strategies")
        os.makedirs(base, exist_ok=True)
        target = os.path.join(base, filename)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    return {"filename": os.path.basename(target), "path": target,
            "dir": os.path.dirname(target), "size": len(content)}


def register_strategy_tools(mcp):
    @mcp.tool()
    async def generate_strategy(strategy_type: str, code: str = "600519.SH",
                                client_path: str = "", account_id: str = "",
                                params: dict | None = None) -> dict:
        """生成 QMT 策略代码（ma_cross/macd/rsi/limitup 四类模板），返回代码内容。"""
        return generate_strategy(strategy_type, code, client_path, account_id, params)

    @mcp.tool()
    async def save_qmt_strategy(filename: str, content: str, client_path: str = "") -> dict:
        """把策略代码保存到 QMT 客户端 mpython 目录（QMT 内可导入运行）。"""
        return save_qmt_strategy(filename, content, client_path)
