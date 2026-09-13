"""默认能力链顺序即契约（D-J §J.6）：严格 QMT → eltdx → baostock → akshare。

改默认顺序必须同步改：runtime_config.data.provider_chain + 文档 + 本测试（三处同步）。
"""
from datasource.providers import DEFAULT_CAPABILITY_CHAINS


def test_default_kline_chain_order():
    k = list(DEFAULT_CAPABILITY_CHAINS["kline"])
    assert k[:4] == ["broker", "eltdx", "baostock", "akshare"], k


def test_default_kline_qfq_chain_order():
    q = list(DEFAULT_CAPABILITY_CHAINS["kline_qfq"])
    assert q[:4] == ["broker", "eltdx", "baostock", "akshare"], q


def test_default_chain_covers_key_capabilities():
    for cap in ("kline", "kline_qfq", "kline_hfq", "quote", "fundamental", "stock_list"):
        assert cap in DEFAULT_CAPABILITY_CHAINS, cap
        assert DEFAULT_CAPABILITY_CHAINS[cap][0] == "broker"
