"""标的分类画像（纯函数，零网络）。

检索与分析链路的画像层：把任意证券代码归类为
index / etf / stock / bond / board / unknown，并给出交易所与板块层级。

关键点：**必须双因子判定（代码段 × 交易所后缀）**——
000001.SH 是上证指数，000001.SZ 是平安银行；仅按数字前缀必然误判。
板块层级细分复用 board.classify_board（单一真源，不另造口径）。
"""
from __future__ import annotations

from typing import Optional

from app.datasource.board import classify_board

# 类型 → 展示标签（前端徽章文案的单一真源，i18n 前端另有英文）
TYPE_LABELS = {
    "stock": "股票",
    "etf": "ETF",
    "index": "指数",
    "board": "板块",
    "bond": "债券",
    "unknown": "标的",
}


def _norm(code: str) -> str:
    c = (code or "").upper().strip()
    return c


def _digits(code: str) -> str:
    return "".join(ch for ch in _norm(code) if ch.isdigit())


def classify_instrument(code: str, name: str = "") -> dict:
    """标的分类画像：{type, exchange, board, label}。无法识别给 unknown，绝不抛错。

    判定优先级（高 → 低）：
    1. 板块指数：880/881 前缀（TDX 板块，交易所后缀可无）
    2. 指数：000xxx.SH / 399xxx.SZ / 899xxx.BJ（交易所双因子）
    3. ETF：51x/56x/58x.SH、15x/16x.SZ（交易所双因子）
    4. 可转债：11xxxx.SH / 12xxxx.SZ
    5. 股票：按 classify_board 细分主板/科创/创业/北交
    """
    c = _norm(code)
    num = _digits(code)
    base = classify_board(c)
    out = {"type": "unknown", "exchange": base.get("exchange", "—"),
           "board": base.get("board", "—"), "label": TYPE_LABELS["unknown"]}

    def _set(t: str):
        out["type"] = t
        out["label"] = TYPE_LABELS[t]
        return out

    if not num:
        return out

    # 1) TDX 板块指数（行业 881 / 概念·统计 880）
    if num.startswith("881") or num.startswith("880"):
        _set("board")
        out["exchange"] = "板块"
        out["board"] = "行业板块" if num.startswith("881") else "概念/统计板块"
        return out

    # 2) 指数：必须结合交易所（000 沪=指数，深=主板股票）
    is_sh = c.endswith(".SH")
    is_sz = c.endswith(".SZ")
    is_bj = c.endswith(".BJ")
    if (is_sh and num.startswith("000")) or (is_sz and num.startswith("399")) \
            or (is_bj and num.startswith("899")):
        _set("index")
        return out

    # 3) ETF：沪 51/56/58 段、深 15/16 段（双因子）
    if is_sh and num[:2] in ("51", "56", "58"):
        _set("etf")
        return out
    if is_sz and num[:2] in ("15", "16"):
        _set("etf")
        return out

    # 4) 可转债（沪 11 / 深 12）
    if (is_sh and num.startswith("11")) or (is_sz and num.startswith("12")):
        _set("bond")
        out["board"] = "可转债"
        return out

    # 5) 股票（含北交所）：classify_board 已给出细分板块
    if num[:2] in ("60", "68", "00", "30") or num[0] in ("8", "4"):
        _set("stock")
        return out

    return out


def instrument_type_label(t: Optional[str]) -> str:
    """type → 中文标签（未知类型统一兜底）。"""
    return TYPE_LABELS.get(t or "", TYPE_LABELS["unknown"])
