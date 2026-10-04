"""许可证链过滤（D-J §J.5）：商用模式按**实际传输后端**动态过滤 TDX 源。

2026-10-04 语义变更：TDX 传输层切换为 easy_tdx（MIT，商用安全），
eltdx 仅为 easy_tdx 缺失时的回退（Research-Only，禁止商用）。因此：
- 后端 = easy_tdx（默认）：商用模式下 tdx 源**保留**在链里；
- 后端 = eltdx（回退）：商用模式下 tdx 源被过滤，退化为 baostock/akshare；
- 非商用（个人研究）：两种后端均可用。

业务代码零例外、degraded=false。
"""
from app.screener.source_policy import resolve_policy
from _phase4_support import force_deps, REG_ALL


def test_commercial_keeps_tdx_when_easy_tdx_backend():
    with force_deps():
        # 默认后端即 easy_tdx（MIT）：商用不因许可证过滤 tdx
        r = resolve_policy("auto", "kline", commercial_mode=True,
                           registered=REG_ALL, qmt_connected=True)
        assert r.chain[0] == "broker", r.chain
        assert "tdx" in r.chain, r.chain
        assert r.degraded is False


def test_commercial_skips_tdx_when_eltdx_fallback(monkeypatch):
    import datasource.tdx_transport as tt
    monkeypatch.setattr(tt, "active_backend", lambda: "eltdx")
    with force_deps():
        # 回退到 eltdx（Research-Only）：商用必须过滤
        r = resolve_policy("auto", "kline", commercial_mode=True,
                           registered=REG_ALL, qmt_connected=True)
        assert r.chain[0] == "broker", r.chain
        assert "tdx" not in r.chain, r.chain
        assert "akshare" in r.chain, r.chain


def test_noncommercial_keeps_tdx():
    with force_deps():
        r = resolve_policy("auto", "kline", commercial_mode=False,
                           registered=REG_ALL, qmt_connected=False)
        assert r.chain[0] == "tdx", r.chain
        assert r.degraded is True  # 仅因无 QMT，非许可证


def test_commercial_with_qmt_keeps_broker_first():
    with force_deps():
        r = resolve_policy("auto", "kline", commercial_mode=True,
                           registered=REG_ALL, qmt_connected=True)
        assert r.chain[0] == "broker", r.chain
