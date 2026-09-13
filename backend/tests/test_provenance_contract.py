"""选股响应溯源契约（§J.10）：必含 provenance / degraded / fallback_tried /
provider_policy_version / dataset_snapshot_id，绝不返回空列表冒充「无符合标的」。
"""
import asyncio
import datasource.registry as regmod
import app.screener.engine as engine_mod
from _phase4_support import FakeStore, force_deps, fake_reg_manager, REG_ALL, make_bar


def _run(coro):
    return asyncio.run(coro)


def test_scan_response_has_provenance_contract():
    with force_deps():
        store = FakeStore(
            stock_list=[{"code": "600000.SH", "name": "浦发"}],
            bars={"600000.SH": [make_bar(10, time_="t1"), make_bar(20, time_="t2")]})
        orig_gm = regmod.get_manager
        regmod.get_manager = lambda: fake_reg_manager(REG_ALL)
        try:
            cond = {"field": {"name": "close", "op": "gt", "value": 10, "window": -1}}
            # scan_async 首参即 store，直接注入 fake，避免触发真实 get_store()
            out = _run(engine_mod.scan_async(store, cond, source_policy="auto",
                                            universe="all", period="1d", adjust="qfq"))
        finally:
            regmod.get_manager = orig_gm

        for key in ("provenance", "degraded", "degraded_reason",
                    "fallback_tried", "provider_policy_version", "dataset_snapshot_id"):
            assert key in out, key
        assert out["provenance"]["provider_policy_version"] == "chain.v1"
        assert "provider_used" in out["provenance"]
        assert "universe" in out["provenance"]
        # 本地有数据 → 不应伪造「无符合标的」空结果（若真无命中，results=[] 但 provenance 明确）
        assert out["provenance"]["provider_used"] == "local"


def test_no_source_raises_runtime_error_not_empty_list():
    with force_deps():
        store = FakeStore(stock_list=[])  # 空池 + 无在线
        orig_gm = regmod.get_manager
        regmod.get_manager = lambda: fake_reg_manager(REG_ALL)
        try:
            cond = {"field": {"name": "close", "op": "gt", "value": 0, "window": -1}}
            raised = False
            try:
                _run(engine_mod.scan_async(store, cond, source_policy="auto", universe="all"))
            except RuntimeError:
                raised = True
            assert raised, "无数据源应抛 RuntimeError（路由转 503），而非返回空列表"
        finally:
            regmod.get_manager = orig_gm
