"""P0-5 资金/仓位原子化回归测试（2026-09-15）。

覆盖三件事：
1. 实盘（require_account=True）买入要求账户快照就绪，未就绪一律拒绝（不再用演示级
   100 万总资产放行真单）；卖出/平仓不受限；模拟盘（require_account=False）不受影响。
2. 快照喂入后做「可用资金充足性」校验。
3. 并发买入不超仓：占比闸门把「在途买入」计入分子/分母，且校验+计数+在途登记在同一
   把锁内原子完成。

注意：后端测试须逐文件运行（同进程全量会硬崩溃）。
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from gateway.risk import DEMO_TOTAL_ASSETS, RiskManager


# ---- 1. 快照就绪闸门 --------------------------------------------------------
def test_live_buy_rejected_without_snapshot():
    """实盘买入 + 快照未就绪 → 拒绝（绝不用演示值 100 万放行）。"""
    rm = RiskManager(max_amount=1_000_000)
    assert rm.data_source == "demo" and rm.total_assets == DEMO_TOTAL_ASSETS
    ok, reason = rm.check_order("600519.SH", 100, 100, "buy", require_account=True)
    assert not ok and "账户快照未就绪" in reason


def test_live_sell_allowed_without_snapshot():
    """快照未就绪时卖出/平仓放行（降风险方向不应被堵）。"""
    rm = RiskManager(max_amount=1_000_000)
    ok, reason = rm.check_order("600519.SH", 100, 100, "sell", require_account=True)
    assert ok, reason


def test_paper_buy_unaffected_by_snapshot_gate():
    """模拟盘（require_account=False）无券商账户，快照永不就绪也不应被拦。"""
    rm = RiskManager(max_amount=1_000_000)
    ok, reason = rm.check_order("600519.SH", 100, 100, "buy", require_account=False)
    assert ok, reason


def test_live_buy_allowed_after_snapshot_fed():
    """账户快照喂入后 data_source=live，实盘买入恢复放行。"""
    rm = RiskManager(max_amount=1_000_000)
    rm.feed_account_snapshot({}, 1_000_000.0, available_cash=500_000.0)
    assert rm.data_source == "live"
    ok, reason = rm.check_order("600519.SH", 100, 100, "buy", require_account=True)
    assert ok, reason


# ---- 2. 可用资金校验 --------------------------------------------------------
def test_live_buy_rejected_when_cash_insufficient():
    rm = RiskManager(max_amount=1_000_000)
    rm.feed_account_snapshot({}, 1_000_000.0, available_cash=5_000.0)
    ok, reason = rm.check_order("600519.SH", 100, 100, "buy", require_account=True)  # 需 10000
    assert not ok and "可用资金不足" in reason


def test_cash_check_skipped_when_not_fed():
    """未喂入现金（available_cash=None）时不做资金校验，避免误拒。"""
    rm = RiskManager(max_amount=1_000_000)
    rm.feed_account_snapshot({}, 1_000_000.0)          # 只喂持仓/总资产
    assert rm._cash_known is False
    ok, reason = rm.check_order("600519.SH", 100, 100, "buy", require_account=True)
    assert ok, reason


# ---- 3. 并发不超仓（在途登记 + 锁）-----------------------------------------
def test_concurrent_buys_do_not_exceed_position_ratio():
    """20 笔并发同标的买入，最终放行金额不得超过单票占比上限。

    单票占比 10% / 总资产 100 万 → 上限 10 万；每笔 100×100=1 万 → 恰好 10 笔通过。
    若占比闸门不计入「在途买入」（回退到只看 positions_value），20 笔会全部通过。
    """
    rm = RiskManager(max_amount=1_000_000, min_qty=100,
                     max_single_position_ratio=0.1, max_position_ratio=1.0)
    rm.feed_account_snapshot({}, 1_000_000.0, available_cash=10_000_000.0)

    def _one(_):
        return rm.check_order("600519.SH", 100, 100, "buy", require_account=True)[0]

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_one, range(20)))

    passed = sum(1 for r in results if r)
    assert passed == 10, f"放行 {passed} 笔，应为 10 笔（10 万 / 1 万）"
    # 在途金额恰为上限，未越过
    assert rm._pending_for("600519.SH") == 100_000.0


def test_precheck_does_not_reserve_pending():
    """预检只判断不预留：连续预检不应把额度耗光。"""
    rm = RiskManager(max_amount=1_000_000, max_single_position_ratio=0.1,
                     max_position_ratio=1.0)
    rm.feed_account_snapshot({}, 1_000_000.0, available_cash=10_000_000.0)
    for _ in range(20):
        ok, _r = rm.precheck_order("600519.SH", 100, 100, "buy", require_account=True)
        assert ok
    assert rm._pending_for("600519.SH") == 0.0


def test_pending_expires_by_ttl():
    """在途登记按 TTL 过期，不会永久占额。"""
    rm = RiskManager(max_amount=1_000_000, max_single_position_ratio=0.1,
                     max_position_ratio=1.0)
    rm.feed_account_snapshot({}, 1_000_000.0, available_cash=10_000_000.0)
    rm._pending_ttl = 0.01
    assert rm.check_order("600519.SH", 100, 100, "buy", require_account=True)[0]
    assert rm._pending_for("600519.SH") == 10_000.0
    import time as _t
    _t.sleep(0.05)
    assert rm.check_order("000001.SZ", 100, 100, "buy", require_account=True)[0]
    # 过期后旧在途被清理
    assert rm._pending_for("600519.SH") == 0.0


def test_require_snapshot_tunable_can_disable_gate():
    """require_snapshot_for_buy=0 时关闭就绪闸门（联调逃生通道）。"""
    rm = RiskManager(max_amount=1_000_000)
    rm.update_from({"require_snapshot_for_buy": 0})
    ok, reason = rm.check_order("600519.SH", 100, 100, "buy", require_account=True)
    assert ok, reason
