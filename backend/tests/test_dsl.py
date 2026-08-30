"""G2-4 公式 DSL 解析测试（类通达信公式 → 选股条件 JSON）。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import pytest

from app.indicators.dsl import parse
from app.screener.conditions import evaluate


def _bars(n=40):
    return [{"open": 10 + i * 0.1, "high": 11 + i * 0.1, "low": 9 + i * 0.1,
             "close": 10 + i * 0.2, "volume": 100000 + i * 1000} for i in range(n)]


def test_parse_field_compare():
    cond = parse("C > 15")
    assert cond == {"field": {"name": "close", "op": "gt", "value": 15.0, "window": -1}}


def test_parse_indicator_call():
    cond = parse("RSI(14) < 30")
    leaf = cond["indicator"]
    assert leaf["name"] == "rsi"
    assert leaf["params"] == {"win": 14}      # period 参数经 win 口径
    assert leaf["output"] == "rsi"
    assert leaf["op"] == "lt" and leaf["value"] == 30.0


def test_parse_and_or_nested():
    cond = parse("C > MA(20) AND (VOLUME > 100000 OR KDJ.K(9) > 80)")
    assert "and" in cond
    lhs, rhs = cond["and"]
    # C > MA(20) → compare 叶子（双序列）
    assert lhs["compare"]["left"]["kind"] == "field" and lhs["compare"]["left"]["name"] == "close"
    assert lhs["compare"]["right"]["kind"] == "indicator" and lhs["compare"]["right"]["name"] == "ma"
    assert "or" in rhs
    kdj = rhs["or"][1]["indicator"]
    assert kdj["name"] == "kdj" and kdj["output"] == "k" and kdj["params"] == {"n": 9}


def test_parse_macd_dot_output():
    cond = parse("MACD.DIF(12,26,9) > 0")
    leaf = cond["indicator"]
    assert leaf["name"] == "macd"
    assert leaf["output"] == "dif"
    assert leaf["params"] == {}               # macd 无参数声明，args 忽略
    assert leaf["value"] == 0.0


def test_parse_not():
    cond = parse("NOT WR(14) < -80")
    assert "not" in cond
    assert cond["not"]["indicator"]["name"] == "wr"


def test_parse_operators():
    for op_str, op_key in ((">=", "gte"), ("<=", "lte"), ("==", "eq"), ("!=", "ne"), (">", "gt")):
        assert parse(f"C {op_str} 5")["field"]["op"] == op_key


def test_parse_errors():
    with pytest.raises(ValueError):
        parse("")
    with pytest.raises(ValueError):
        parse("C >")                       # 缺右操作数
    with pytest.raises(ValueError):
        parse("FOO(5) > 1")                # 未知指标
    with pytest.raises(ValueError):
        parse("C > 1 + 2")                 # 不支持算术


def test_parse_flip_numeric_left():
    """5 < C 等价 C > 5（数值在左自动翻转）。"""
    assert parse("5 < C") == {"field": {"name": "close", "op": "gt", "value": 5.0, "window": -1}}


def test_dsl_conditions_roundtrip_into_evaluator():
    """DSL 产物可直接进 G7 求值器。"""
    bars = _bars()
    cond = parse("C > 12 AND RSI(14) < 90")
    hit, score, total = evaluate(cond, bars)
    assert total == 2
    assert hit is True or hit is False     # 不抛异常即可（值取决于数据）


def test_dsl_parse_and_eval_known():
    """已知数据：C 单调上涨 → ROC(10) > 0 必命中。"""
    bars = _bars(40)
    cond = parse("ROC(10) > 0")
    hit, score, total = evaluate(cond, bars)
    assert hit is True and score == 1 and total == 1
