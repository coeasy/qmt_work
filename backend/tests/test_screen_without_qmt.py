"""D12 三环境矩阵：无 QMT 时选股仍可用（D-J §J.3 显式降级 + D12 底线）。

环境① 无 QMT 有 eltdx → 走 eltdx；
环境② 商用模式（``QMT_COMMERCIAL=1``）→ eltdx 因 ``commercial_ok=False`` 被**许可证跳过**，
       降级到 baostock；``degraded=True`` 且 reason 记 ``no_broker_connected``
       （许可证跳过**不**计入 degraded —— 那是设计内的过滤，不是降级）；
环境③ 仅 akshare → 仍跑通全市场选股且 degraded 语义正确。

★ V11 R7：本文件原先有**两类**问题，均已修：
1. 环境②的「去掉 eltdx」设置**零效果** —— 替身缺 ``_declared_map()``，
   使 ``BarsProvider`` 里同 try 的 ``registered`` 被静默丢弃为 ``None``；
   且商用模式根本没打开（替身 ``_commercial_mode`` 恒为 False）。
   现改为「注册态**包含** eltdx，靠真正的许可证过滤剔除」，并加对照用例
   （``test_env2b_*``）证明是许可证在起作用。
2. 依赖环境残留的「无券商连接」全局态 → 同进程全量跑顺序一变即假失败。
   现统一用 ``no_qmt()`` 显式建立前提。
"""
import asyncio
import datasource.registry as regmod
from app.data.bars_provider import BarsProvider
from _phase4_support import (
    FakeStore, FakeKlineHub, force_deps, fake_reg_manager, no_qmt, REG_ALL, make_bar,
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
    """环境② 商用模式：eltdx 被**许可证过滤** → 降级到 baostock。

    契约：① 商用模式下 ``commercial_ok=False`` 的源被**跳过**（不报错、不被尝试）；
          ② 无 QMT 连接 → ``broker`` 被剔除、``degraded=True``、reason ``no_broker_connected``；
          ③ 许可证跳过**不**计入 degraded。
    """
    with force_deps(), no_qmt():
        orig = _patch_manager()
        try:
            codes = ["600000.SH", "000001.SZ"]
            bao = {c: [make_bar(15, time_="2024-01-02")] for c in codes}
            # 注册态**包含** eltdx：能否入选完全由许可证决定（这才是环境②要验的）
            regmod.get_manager = lambda: fake_reg_manager(REG_ALL, commercial_mode=True)
            bp = BarsProvider(hub=FakeKlineHub({"baostock": bao}), store=FakeStore())
            batch, rep = _run(bp.get_bars_batch(codes, adjust="qfq", policy_str="auto"))
            assert rep.provider_used == "baostock"
            assert all(batch[c] for c in codes)
            # ① 许可证过滤：eltdx 不应被**尝试**（而非「尝试后为空」）
            assert not any(t.startswith("eltdx") for t in rep.fallback_tried), rep.fallback_tried
            # ② 无 QMT → broker 被剔除并记因
            assert rep.degraded is True
            assert rep.fallback_tried[0] == "qmt:no_broker_connected"
            # ③ 许可证跳过不是降级原因
            assert rep.degraded_reason is None
        finally:
            regmod.get_manager = orig


def test_env2b_non_commercial_keeps_eltdx():
    """环境②的**对照实验**：同样注册态下关掉商用模式 → eltdx 重新入选。

    没有这条对照，``test_env2`` 即使「许可证过滤」失效也可能碰巧通过
    （例如 eltdx 只是被注册态排除、或尝试后返回空）—— 那就成了假信心用例。
    """
    with force_deps(), no_qmt():
        orig = _patch_manager()
        try:
            codes = ["600000.SH", "000001.SZ"]
            eltdx = {c: [make_bar(15, time_="2024-01-02")] for c in codes}
            bao = {c: [make_bar(20, time_="2024-01-02")] for c in codes}
            regmod.get_manager = lambda: fake_reg_manager(REG_ALL, commercial_mode=False)
            bp = BarsProvider(hub=FakeKlineHub({"eltdx": eltdx, "baostock": bao}),
                              store=FakeStore())
            batch, rep = _run(bp.get_bars_batch(codes, adjust="qfq", policy_str="auto"))
            assert rep.provider_used == "eltdx"        # 非商用 → eltdx 可用
            assert batch[codes[0]][0].close == 15
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


def test_local_batch_runs_off_event_loop():
    """本地仓批量取数必须离开事件循环（全市场规模会阻塞所有请求）。

    2026-09-14：实测 ``/market/screen`` 全市场（6982 只 × 250 根 ≈ 170 万个 Bar
    对象）请求耗时 13.67s，期间并发 ``/health`` 等待 **12.26s**（事件循环停摆），
    而同一次请求里纯求值只占 163ms。本用例把「必须离开事件循环线程」固定为契约
    （回退成同步调用即失败）。
    """
    import threading

    main_thread = threading.current_thread()
    seen = {}

    class _RecordingStore(FakeStore):
        def get_bars_batch(self, codes, period="1d", adjust="", limit=250, lite=False):
            seen["thread"] = threading.current_thread()
            return {c: [make_bar(15, time_="2024-01-02")] for c in codes}

    with force_deps():
        orig = _patch_manager()
        try:
            bp = BarsProvider(hub=FakeKlineHub({}), store=_RecordingStore())
            batch, rep = _run(bp.get_bars_batch(
                ["600000.SH"], adjust="qfq", policy_str="local_only"))
        finally:
            regmod.get_manager = orig

    assert seen.get("thread") is not None, "本地仓批量取数未被调用，用例没覆盖到目标路径"
    assert seen["thread"] is not main_thread, (
        "本地仓批量取数仍在事件循环线程执行——全市场选股会阻塞所有 HTTP 请求")
    assert rep.provider_used == "local"
    assert batch["600000.SH"]
