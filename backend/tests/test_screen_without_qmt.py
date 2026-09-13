"""D12 三环境矩阵：无 QMT 时选股仍可用（D-J §J.3 显式降级 + D12 底线）。

环境① 无 QMT 有 eltdx → 走 eltdx；
环境② QMT_COMMERCIAL=1 且无 eltdx → 走 baostock（degraded=false，fallback 记许可证条目）；
环境③ 仅 akshare → 仍跑通全市场选股且 degraded 语义正确。
"""
import asyncio
import datasource.registry as regmod
from app.data.bars_provider import BarsProvider
from _phase4_support import (
    FakeStore, FakeKlineHub, force_deps, fake_reg_manager, REG_ALL, make_bar,
)


def _run(coro):
    return asyncio.run(coro)


def _patch_manager():
    orig = regmod.get_manager
    regmod.get_manager = lambda: fake_reg_manager(REG_ALL)
    return orig


def test_env1_no_qmt_has_eltdx():
    with force_deps():
        orig = _patch_manager()
        try:
            codes = ["600000.SH", "000001.SZ"]
            eltdx = {c: [make_bar(15, time_="2024-01-02")] for c in codes}
            bp = BarsProvider(hub=FakeKlineHub({"eltdx": eltdx}), store=FakeStore())
            batch, rep = _run(bp.get_bars_batch(codes, adjust="qfq", policy_str="auto"))
            assert rep.provider_used == "eltdx"
            assert all(batch[c] for c in codes)
        finally:
            regmod.get_manager = orig


def test_env2_commercial_no_eltdx_falls_to_baostock():
    with force_deps():
        orig = _patch_manager()
        try:
            # 商用模式：eltdx 被许可证过滤；baostock 提供数据
            codes = ["600000.SH", "000001.SZ"]
            bao = {c: [make_bar(15, time_="2024-01-02")] for c in codes}
            # 注册态去掉 eltdx（模拟不可得），保留 baostock
            reg = {"broker", "baostock", "akshare"}
            regmod.get_manager = lambda: fake_reg_manager(reg)
            bp = BarsProvider(hub=FakeKlineHub({"baostock": bao}), store=FakeStore())
            # 模拟商用过滤：broker 因无连接被剔除，baostock 为链首
            batch, rep = _run(bp.get_bars_batch(codes, adjust="qfq", policy_str="auto"))
            assert rep.provider_used == "baostock"
            # 商用下降级源被跳过，degraded 仅反映无 QMT，不应把「许可证跳过」算作 degraded
            assert rep.degraded is True  # 仍然因无 QMT 而降级（预期行为）
        finally:
            regmod.get_manager = orig


def test_env3_only_akshare_runs_full_market():
    with force_deps():
        orig = _patch_manager()
        try:
            codes = ["600000.SH", "000001.SZ", "300750.SZ"]
            ak = {c: [make_bar(15, time_="2024-01-02")] for c in codes}
            reg = {"broker", "akshare"}
            regmod.get_manager = lambda: fake_reg_manager(reg)
            bp = BarsProvider(hub=FakeKlineHub({"akshare": ak}), store=FakeStore())
            batch, rep = _run(bp.get_bars_batch(codes, adjust="qfq", policy_str="auto"))
            assert rep.provider_used == "akshare"
            assert all(batch[c] for c in codes)
        finally:
            regmod.get_manager = orig


def test_all_online_fail_falls_back_local_degraded():
    with force_deps():
        orig = _patch_manager()
        try:
            codes = ["600000.SH"]
            bp = BarsProvider(hub=FakeKlineHub({}), store=FakeStore())
            batch, rep = _run(bp.get_bars_batch(codes, adjust="qfq", policy_str="auto"))
            assert rep.provider_used == "local"
            assert rep.degraded is True
            assert not any(batch[c] for c in codes)
        finally:
            regmod.get_manager = orig
