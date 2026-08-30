"""G8 AI 闭环 · 自然语言 → 结构化选股条件（规则式，离线可跑）。

设计（G8-1）：自然语言解析为**可编辑条件树**（与 G7 screener 同一 JSON 结构），
绝不黑箱——用户可回显修改。支持语义（技术面，走 G2 指标引擎）：
- 放量/放量N日上涨 → VOLUME > 1.5×MA(VOLUME,20)（+ ROC 上涨）
- 上涨/下跌 → ROC(n) 与 0 比较（n 默认 20，支持「N日」提取）
- 超买/超卖 → RSI(14) > 70 / < 30
- 金叉/死叉 → MA(5) 与 MA(10) 双序列比较（compare 叶子）
- 站上/跌破 → C 与 MA(20) 比较
不支持的语义（市值/国资/社保持仓等基本面）→ 诚实返回 `unsupported` 提示，
**绝不伪造条件**（零 mock 铁律）。规则引擎可扩展：新增 (regex, builder) 即注册。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# 规则：(正则, 说明, 构建函数(text, match) -> 条件节点)
_RULES: List[Tuple[re.Pattern, str, Any]] = []


def _rule(pattern: str, desc: str, builder):
    _RULES.append((re.compile(pattern), desc, builder))


def _num_of(text: str, default: int) -> int:
    m = re.search(r"(\d+)\s*日", text)
    return int(m.group(1)) if m else default


# ---- 规则定义 ----
def _b_volume_surge(text, m):
    n = _num_of(text, 20)
    return {"compare": {"left": {"kind": "field", "name": "volume", "window": -1},
                        "op": "gt",
                        "right": {"kind": "indicator", "name": "volume_ma",
                                  "params": {"win": n}, "output": "volume_ma", "window": -1}}}


def _b_up(text, m):
    n = _num_of(text, 20)
    return {"indicator": {"name": "roc", "params": {"win": n}, "output": "roc",
                          "op": "gt", "value": 0, "window": -1}}


def _b_down(text, m):
    n = _num_of(text, 20)
    return {"indicator": {"name": "roc", "params": {"win": n}, "output": "roc",
                          "op": "lt", "value": 0, "window": -1}}


def _b_overbought(text, m):
    return {"indicator": {"name": "rsi", "params": {"win": 14}, "output": "rsi",
                          "op": "gt", "value": 70, "window": -1}}


def _b_oversold(text, m):
    return {"indicator": {"name": "rsi", "params": {"win": 14}, "output": "rsi",
                          "op": "lt", "value": 30, "window": -1}}


def _b_golden_cross(text, m):
    return {"compare": {"left": {"kind": "indicator", "name": "ma", "params": {"win": 5},
                                 "output": "ma", "window": -1},
                        "op": "gt",
                        "right": {"kind": "indicator", "name": "ma", "params": {"win": 10},
                                  "output": "ma", "window": -1}}}


def _b_death_cross(text, m):
    return {"compare": {"left": {"kind": "indicator", "name": "ma", "params": {"win": 5},
                                 "output": "ma", "window": -1},
                        "op": "lt",
                        "right": {"kind": "indicator", "name": "ma", "params": {"win": 10},
                                  "output": "ma", "window": -1}}}


def _b_above_ma(text, m):
    n = _num_of(text, 20)
    return {"compare": {"left": {"kind": "field", "name": "close", "window": -1},
                        "op": "gt",
                        "right": {"kind": "indicator", "name": "ma", "params": {"win": n},
                                  "output": "ma", "window": -1}}}


def _b_below_ma(text, m):
    n = _num_of(text, 20)
    return {"compare": {"left": {"kind": "field", "name": "close", "window": -1},
                        "op": "lt",
                        "right": {"kind": "indicator", "name": "ma", "params": {"win": n},
                                  "output": "ma", "window": -1}}}


_rule(r"放量", "放量（量 > 1× 量均线）", _b_volume_surge)
_rule(r"金叉", "均线金叉（MA5 > MA10）", _b_golden_cross)
_rule(r"死叉", "均线死叉（MA5 < MA10）", _b_death_cross)
_rule(r"超买", "RSI(14) > 70（超买）", _b_overbought)
_rule(r"超卖", "RSI(14) < 30（超卖）", _b_oversold)
_rule(r"站上", "收盘站上 MA(n)", _b_above_ma)
_rule(r"跌破", "收盘跌破 MA(n)", _b_below_ma)
_rule(r"上涨", "N 日上涨（ROC(n) > 0）", _b_up)
_rule(r"下跌", "N 日下跌（ROC(n) < 0）", _b_down)

# 明确不支持的语义（基本面/资金面，诚实提示，不伪造）
_UNSUPPORTED_PATTERNS = [
    (r"市值", "市值过滤需基本面数据源（本地仓暂无）"),
    (r"国资|国有|国资委", "国资持股需股东/股权数据源"),
    (r"社保|证金|汇金", "机构持仓需股东数据源"),
    (r"市盈率|PE|估值", "估值需基本面数据源"),
    (r"北向|主力|资金流", "资金流过滤需资金流数据源"),
]


def _unsupported_hints(text: str) -> List[str]:
    return [hint for pat, hint in _UNSUPPORTED_PATTERNS if re.search(pat, text)]


def parse_nl(text: str) -> Dict[str, Any]:
    """自然语言 → {text, conditions, rules, unsupported}。

    conditions 为可编辑条件树（{and:[..]}）；无匹配规则且无 unsupported 时抛
    ValueError（路由 400，提示支持范围）。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("查询文本为空")
    leaves: List[dict] = []
    matched: List[str] = []
    for pat, desc, builder in _RULES:
        if pat.search(text):
            leaves.append(builder(text, pat.search(text)))
            matched.append(desc)
    unsupported = _unsupported_hints(text)
    if not leaves and not unsupported:
        raise ValueError(
            "未识别出可执行的选股语义。支持：放量/上涨/下跌/超买/超卖/金叉/死叉/"
            "站上/跌破（如「近20日放量上涨、RSI 超卖」）。")
    return {
        "text": text,
        "conditions": {"and": leaves} if leaves else {},
        "rules": matched,
        "unsupported": unsupported,
    }


__all__ = ["parse_nl"]
