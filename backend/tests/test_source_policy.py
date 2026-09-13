"""Source Policy 解析行为（D-J §J.1–J.4）：5 种 policy + explicit 不降级。"""
from app.screener.source_policy import resolve_policy, SourcePolicy
from _phase4_support import force_deps, REG_ALL


def test_auto_with_qmt_prefers_broker():
    with force_deps():
        r = resolve_policy("auto", "kline", commercial_mode=False,
                          registered=REG_ALL, qmt_connected=True)
        assert r.policy == SourcePolicy.AUTO
        assert r.chain[:1] == ("broker",)
        assert r.degraded is False


def test_auto_without_qmt_drops_broker_and_degrades():
    with force_deps():
        r = resolve_policy("auto", "kline", commercial_mode=False,
                          registered=REG_ALL, qmt_connected=False)
        assert "broker" not in r.chain
        assert r.degraded is True
        assert r.qmt_unavailable_reason == "no_broker_connected"
        assert r.chain[0] == "eltdx"


def test_prefer_qmt_same_chain_as_auto():
    with force_deps():
        ra = resolve_policy("auto", "kline", registered=REG_ALL, qmt_connected=False)
        rp = resolve_policy("prefer_qmt", "kline", registered=REG_ALL, qmt_connected=False)
        assert list(ra.chain) == list(rp.chain)


def test_explicit_does_not_degrade():
    with force_deps():
        r = resolve_policy("explicit:akshare", "kline",
                          registered=REG_ALL, qmt_connected=False)
        assert r.policy == SourcePolicy.EXPLICIT
        assert r.chain == ("akshare",)
        assert r.degraded is False


def test_qmt_only_without_connection_empty_chain():
    with force_deps():
        r = resolve_policy("qmt_only", "kline", registered={"broker"}, qmt_connected=False)
        assert r.chain == ()
        assert r.qmt_unavailable_reason == "no_broker_connected"


def test_local_only_empty_chain():
    with force_deps():
        r = resolve_policy("local_only", "kline")
        assert r.chain == ()
