"""公式 DSL 选股（P1-36）：类通达信公式 → 条件树 → 同引擎求值。

验证：公式可解析为 conditions JSON，且经 scan_async 跑通（本地仓兜底路径）。
"""
import asyncio
import datasource.registry as regmod
import app.screener.engine as engine_mod
from app.indicators.dsl import parse
from _phase4_support import FakeStore, force_deps, fake_reg_manager, REG_ALL, make_bar


def _run(coro):
    return asyncio.run(coro)


def test_formula_parses_to_conditions():
    cond = parse("C > MA(20)")
    assert isinstance(cond, dict)
    assert ("field" in cond) or ("indicator" in cond) or ("compare" in cond)


def test_compound_formula_has_and():
    cond = parse("RSI(14) < 30 AND C > MA(5)")
    assert "and" in cond


def test_formula_drives_scan():
    with force_deps():
        store = FakeStore(
            stock_list=[{"code": "600000.SH", "name": "浦发"}],
            bars={"600000.SH": [make_bar(10, time_=f"t{i}") for i in range(20)] +
                  [make_bar(25, time_="t21")]})
        orig_gm = regmod.get_manager
        regmod.get_manager = lambda: fake_reg_manager(REG_ALL)
        try:
            cond = parse("C > 20")
            # 直接注入 fake store，避免触发真实 get_store()
            out = _run(engine_mod.scan_async(store, cond, source_policy="local_only",
                                            universe="all", offline=True))
        finally:
            regmod.get_manager = orig_gm
        assert "results" in out
        assert out["provenance"]["provider_used"] == "local"
        # 末根 close=25 > 20 应命中
        assert any(r["code"] == "600000.SH" for r in out["results"])
