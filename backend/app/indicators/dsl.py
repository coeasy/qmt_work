"""G2-4 公式 DSL 解析层：类通达信公式 → 选股条件 JSON。

语法（v1，最小可用的递归下降）：
    expr     := or_expr
    or_expr  := and_expr (OR and_expr)*
    and_expr := not_expr (AND not_expr)*
    not_expr := NOT not_expr | atom
    atom     := '(' expr ')' | compare
    compare  := operand op operand
    operand  := number | symbol | call
    call     := NAME '.' OUTPUT '(' [args] ')'      # KDJ.K(9) / MACD.DIF() / BOLL.upper(20,2)
    symbol   := C | O | H | L | V（=close/open/high/low/volume）
    op       := > | >= | < | <= | == | !=

示例：
    "C > MA(20) AND RSI(14) < 30"
    "C > MA(20) AND (VOLUME > MA(VOLUME,20)*1.5 OR KDJ.K(9) > 80)"
    "MACD.DIF(12,26,9) > 0 AND NOT WR(14) < -80"

输出：与 G7 screener 兼容的 conditions JSON（{and:[..]}/{or:[..]}，叶子
{indicator:...}/{field:...}），窗口一律最新（-1）。多输出指标经 `NAME.OUTPUT`
取值；单输出指标 `NAME(n)` 即取首个输出。
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

from app.indicators import get_indicator, list_indicators

_SYMBOLS = {"C": "close", "O": "open", "H": "high", "L": "low", "V": "volume",
            "CLOSE": "close", "OPEN": "open", "HIGH": "high", "LOW": "low",
            "VOLUME": "volume"}
_KEYWORDS = {"AND", "OR", "NOT"}
_OPS = {"<=", ">=", "!=", "==", ">", "<"}


class _Tok:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: Any):
        self.kind = kind          # num / id / op / lp / rp / comma
        self.value = value


_TOKEN_RE = re.compile(
    r"\s*(?:(<=|>=|!=|==|>|<)|(-?[0-9]+(?:\.[0-9]+)?)|([A-Za-z_][A-Za-z0-9_.]*)|(\()|(\))|(,))")

_OP_MAP = {"<=": "lte", ">=": "gte", "!=": "ne", "==": "eq", ">": "gt", "<": "lt"}
_FLIP = {"gt": "lt", "gte": "lte", "lt": "gt", "lte": "gte", "eq": "eq", "ne": "ne"}


def _tokenize(text: str) -> List[_Tok]:
    toks: List[_Tok] = []
    i = 0
    while i < len(text):
        m = _TOKEN_RE.match(text, i)
        if not m:
            raise ValueError(f"无法解析的位置 {i}：「{text[i:i+12]}…」")
        op, num, ident, lp, rp, comma = m.groups()
        if op:
            toks.append(_Tok("op", op))
        elif num:
            toks.append(_Tok("num", float(num)))
        elif ident:
            toks.append(_Tok("id", ident))
        elif lp:
            toks.append(_Tok("lp", "("))
        elif rp:
            toks.append(_Tok("rp", ")"))
        elif comma:
            toks.append(_Tok("comma", ","))
        i = m.end()
    return toks


class _Parser:
    def __init__(self, toks: List[_Tok]):
        self._t = toks
        self._i = 0

    def _peek(self) -> Optional[_Tok]:
        return self._t[self._i] if self._i < len(self._t) else None

    def _next(self) -> _Tok:
        tok = self._peek()
        if tok is None:
            raise ValueError("公式意外结束")
        self._i += 1
        return tok

    def parse(self) -> dict:
        node = self._or()
        if self._peek() is not None:
            raise ValueError(f"公式结尾有多余内容：「{self._peek().value}」")
        return node

    # ---- 组合 ----
    def _or(self) -> dict:
        left = self._and()
        while self._peek() is not None and self._peek().kind == "id" and \
                self._peek().value.upper() == "OR":
            self._next()
            right = self._and()
            left = {"or": [left, right]}
        return left

    def _and(self) -> dict:
        left = self._not()
        while self._peek() is not None and self._peek().kind == "id" and \
                self._peek().value.upper() == "AND":
            self._next()
            right = self._not()
            left = {"and": [left, right]}
        return left

    def _not(self) -> dict:
        if self._peek() is not None and self._peek().kind == "id" and \
                self._peek().value.upper() == "NOT":
            self._next()
            inner = self._not()
            return {"not": inner}
        return self._atom()

    def _atom(self) -> dict:
        tok = self._peek()
        if tok is not None and tok.kind == "lp":
            self._next()
            inner = self._or()
            self._next()  # rp
            return inner
        return self._compare()

    # ---- 比较 ----
    def _compare(self) -> dict:
        left = self._operand()
        op_tok = self._next()
        if op_tok.kind != "op":
            raise ValueError(f"期望比较运算符，实际「{op_tok.value}」")
        right = self._operand()
        return self._leaf(left, op_tok.value, right)

    def _operand(self) -> dict:
        tok = self._peek()
        if tok is None:
            raise ValueError("表达式意外结束（期望数值/字段/指标）")
        if tok.kind == "num":
            self._next()
            return {"kind": "num", "value": tok.value}
        if tok.kind == "id":
            return self._id_or_call()
        raise ValueError(f"期望操作数，实际「{tok.value}」")

    def _id_or_call(self) -> dict:
        raw = str(self._next().value).upper()
        # NAME.OUTPUT 点号已在同一 token 内（如 MACD.DIF）
        output: Optional[str] = None
        name = raw
        if "." in raw:
            name, _, output = raw.partition(".")
        if self._peek() is not None and self._peek().kind == "lp":
            return self._call(name, output)
        # 字段符号
        if output is not None:
            raise ValueError(f"字段 {name} 不支持输出列 {output}")
        if name in _SYMBOLS:
            return {"kind": "field", "name": _SYMBOLS[name]}
        if name in _KEYWORDS:
            raise ValueError(f"关键字 {name} 位置非法")
        raise ValueError(f"未知符号/指标：{name}（字段可选 C/O/H/L/V）")

    def _call(self, name: str, output: Optional[str]) -> dict:
        self._next()  # lp
        args: List[float] = []
        if self._peek() is not None and self._peek().kind != "rp":
            args.append(self._num_arg())
            while self._peek() is not None and self._peek().kind == "comma":
                self._next()
                args.append(self._num_arg())
        if self._peek() is None or self._peek().kind != "rp":
            raise ValueError(f"指标 {name} 缺少右括号")
        self._next()  # rp
        return {"kind": "ind", "name": name, "output": output, "args": args}

    def _num_arg(self) -> float:
        tok = self._peek()
        if tok is None or tok.kind != "num":
            raise ValueError("指标参数须为数字")
        self._next()
        return tok.value

    # ---- 叶子 ----
    def _series(self, operand: dict) -> dict:
        """操作数 → compare 叶子用的序列操作数。"""
        if operand["kind"] == "field":
            return {"kind": "field", "name": operand["name"], "window": -1}
        spec = get_indicator(operand["name"].lower())
        params = self._ind_params(spec, operand["args"])
        out = (operand["output"] or "").lower() or (spec.outputs[0] if spec.outputs else "")
        return {"kind": "indicator", "name": operand["name"].lower(),
                "params": params, "output": out, "window": -1}

    @staticmethod
    def _ind_params(spec, args: List[float]) -> dict:
        params = {}
        for idx, arg in enumerate(args):
            if idx >= len(spec.params):
                break
            p = spec.params[idx]
            params["win" if p.name == "period" else p.name] = arg
        return params

    def _leaf(self, left: dict, op: str, right: dict) -> dict:
        if left["kind"] == "num" and right["kind"] == "num":
            raise ValueError("比较两侧都是纯数字（缺少字段/指标）")
        op_key = _OP_MAP[op]
        # 双序列比较（C > MA(20) / KDJ.K > MA(5) 等）→ compare 叶子
        if left["kind"] != "num" and right["kind"] != "num":
            return {"compare": {"left": self._series(left), "op": op_key,
                                "right": self._series(right)}}
        # 数值须在右：序列在右且数值在左 → 交换并翻转运算符
        if left["kind"] == "num" and right["kind"] != "num":
            left, right = right, left
            op_key = _FLIP[op_key]
        # 序列 vs 数值 → field / indicator 叶子
        name = left["name"]
        if left["kind"] == "field":
            return {"field": {"name": name, "op": op_key, "value": right["value"], "window": -1}}
        try:
            spec = get_indicator(name.lower())   # 注册表名为小写
        except KeyError as exc:
            raise ValueError(f"未知指标：{name.lower()}（可用：{available_indicator_hint()}）") from exc
        params = self._ind_params(spec, left["args"])
        out = left["output"].lower() if left["output"] else (spec.outputs[0] if spec.outputs else "")
        if left["output"] is not None and out not in spec.outputs:
            raise ValueError(f"指标 {name.lower()} 无输出列 {left['output']}（可选 {spec.outputs}）")
        return {"indicator": {"name": name.lower(), "params": params, "output": out,
                              "op": op_key, "value": right["value"], "window": -1}}


def parse(text: str) -> dict:
    """公式字符串 → screener conditions JSON。语法错误抛 ValueError。"""
    if not text or not text.strip():
        raise ValueError("公式为空")
    toks = _tokenize(text)
    return _Parser(toks).parse()


def available_indicator_hint() -> str:
    return "、".join(sorted(i["name"] for i in list_indicators()))


__all__ = ["parse"]
