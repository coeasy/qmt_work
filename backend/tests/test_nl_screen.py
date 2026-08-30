"""G8 自然语言选股测试（规则式 NL → 可编辑条件树）。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import pytest

from app.agent.nl_screen import parse_nl


def test_nl_volume_surge():
    out = parse_nl("近20日放量上涨")
    assert any("放量" in r for r in out["rules"])
    assert any("上涨" in r for r in out["rules"])
    leaves = out["conditions"]["and"]
    assert len(leaves) == 2
    # 放量 → compare 叶子（VOLUME > VOL-MA）
    surge = leaves[0]["compare"]
    assert surge["left"]["name"] == "volume"
    assert surge["right"]["name"] == "volume_ma"
    assert surge["right"]["params"] == {"win": 20}


def test_nl_oversold():
    out = parse_nl("RSI 超卖")
    leaf = out["conditions"]["and"][0]
    assert leaf["indicator"]["name"] == "rsi"
    assert leaf["indicator"]["op"] == "lt" and leaf["indicator"]["value"] == 30


def test_nl_golden_cross():
    out = parse_nl("均线金叉")
    c = out["conditions"]["and"][0]["compare"]
    assert c["left"]["name"] == "ma" and c["left"]["params"] == {"win": 5}
    assert c["right"]["name"] == "ma" and c["right"]["params"] == {"win": 10}
    assert c["op"] == "gt"


def test_nl_above_ma_with_window():
    out = parse_nl("站上20日均线")
    c = out["conditions"]["and"][0]["compare"]
    assert c["left"]["name"] == "close"
    assert c["right"]["params"] == {"win": 20}
    assert c["op"] == "gt"


def test_nl_unsupported_honest():
    """基本面语义 → unsupported 提示，不伪造条件。"""
    out = parse_nl("国资重仓、市值50-500亿")
    assert out["conditions"] == {}
    assert any("市值" in u for u in out["unsupported"])
    assert any("国资" in u for u in out["unsupported"])


def test_nl_empty_text():
    with pytest.raises(ValueError):
        parse_nl("  ")


def test_nl_no_rule_matches():
    with pytest.raises(ValueError):
        parse_nl("随便说点什么")


def test_nl_conditions_compatible_with_evaluator():
    """NL 产物可直接进 G7 求值器（条件树可编辑回显即兑现）。"""
    from app.screener.conditions import evaluate
    bars = [{"open": 10 + i * 0.1, "high": 11 + i * 0.1, "low": 9 + i * 0.1,
             "close": 10 + i * 0.2, "volume": 100000 + i * 1000} for i in range(40)]
    out = parse_nl("上涨")
    hit, score, total = evaluate(out["conditions"], bars)
    assert total >= 1
    assert hit is True
