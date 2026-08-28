"""板块归类工具：从代码前缀推断交易所 / 板块层级。

原位于 app/routes/market.py，因 app/datasource/eltdx_source.py 也依赖它而被迫反向
import routes（分层异味）。抽出到独立模块，二者统一引用，消除循环依赖风险。
"""
from typing import Optional


def classify_board(code: str) -> dict:
    """从代码前缀推断所属板块（行业信息的可行替代：交易所+板块层级）。

    返回 {board, exchange, market}。无法识别时给兜底值，绝不抛错。
    """
    c = (code or "").upper().strip()
    num = "".join(ch for ch in c if ch.isdigit())
    if c.endswith(".SH") or c.startswith("6") or c.startswith("9"):
        exchange = "上交所"
        if num.startswith("688"):
            board = "科创板"
        elif num.startswith("60") or num.startswith("90") or num.startswith("58"):
            board = "沪市主板"
        elif num.startswith("11"):
            board = "可转债(沪)"
        else:
            board = "沪市"
    elif c.endswith(".SZ") or c.startswith("0") or c.startswith("3") or c.startswith("2"):
        exchange = "深交所"
        if num.startswith("300") or num.startswith("301"):
            board = "创业板"
        elif num.startswith("00") or num.startswith("001") or num.startswith("002") or num.startswith("003"):
            board = "深市主板"
        elif num.startswith("12") or num.startswith("11"):
            board = "可转债(深)"
        else:
            board = "深市"
    elif c.endswith(".BJ") or c.startswith("8") or c.startswith("4"):
        exchange = "北交所"
        board = "北交所"
    else:
        exchange = "—"
        board = "—"
    return {"exchange": exchange, "board": board, "market": exchange}


_LIMIT_BOARDS = ("科创板", "创业板", "北交所")
# 涨跌停幅度（A股：主板 ±10%，科创/创业/北交 ±20%）
_LIMIT_RATIO_MAIN = 0.10
_LIMIT_RATIO_GEM = 0.20
# 无涨跌幅限制（可转债 / 新股上市前 5 日等）
_LIMIT_NONE = None


def limit_ratio(code: str, name: str = "") -> Optional[float]:
    """返回该代码所属板块的涨跌停幅度（0.10 / 0.20 / None 表示无涨跌幅限制）。

    规则覆盖：
    - 可转债（代码 11xxxx/12xxxx）：无涨跌幅限制 → None
    - ST / *ST（名称含 ST）：±5%
    - 科创板 / 创业板 / 北交所：±20%
    - 其余（主板等）：±10%

    已知局限（无法仅从代码判定，需上市日期）：
    - 主板 / 双创新股上市前 5 日无涨跌幅限制，此处按板块默认幅度返回。
    无法识别板块时按主板 ±10% 处理（保守，避免高估可交易空间）。
    """
    board = classify_board(code).get("board", "")
    if board.startswith("可转债"):
        return _LIMIT_NONE
    nm = (name or "").upper()
    if "ST" in nm:
        return 0.05
    return _LIMIT_RATIO_GEM if board in _LIMIT_BOARDS else _LIMIT_RATIO_MAIN


__all__ = ["classify_board", "limit_ratio"]

