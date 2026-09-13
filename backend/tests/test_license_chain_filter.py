"""许可证链过滤（D-J §J.5）：商用模式跳过 commercial_ok=False 的源（如 eltdx Research-Only）。

非商用（个人研究）下 eltdx 全功能可用；商用下链路自动跳过 eltdx，退化为 baostock/akshare，
业务代码零例外、degraded=false。
"""
from app.screener.source_policy import resolve_policy
from _phase4_support import force_deps, REG_ALL


def test_commercial_skips_eltdx():
    with force_deps():
        # 商用且有 QMT：broker 仍为首选（不降级），eltdx 仅因许可证被过滤
        r = resolve_policy("auto", "kline", commercial_mode=True,
                          registered=REG_ALL, qmt_connected=True)
        assert "eltdx" not in r.chain, r.chain
        assert "akshare" in r.chain, r.chain
        assert r.chain[0] == "broker"
        assert r.degraded is False


def test_noncommercial_keeps_eltdx():
    with force_deps():
        r = resolve_policy("auto", "kline", commercial_mode=False,
                          registered=REG_ALL, qmt_connected=False)
        assert r.chain[0] == "eltdx", r.chain
        assert r.degraded is True  # 仅因无 QMT，非许可证


def test_commercial_with_qmt_keeps_broker():
    with force_deps():
        r = resolve_policy("auto", "kline", commercial_mode=True,
                          registered=REG_ALL, qmt_connected=True)
        assert r.chain[0] == "broker", r.chain
        assert "eltdx" not in r.chain
