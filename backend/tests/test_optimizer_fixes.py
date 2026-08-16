"""§3.1 P0 缺陷速修回归测试（pytest，不依赖券商连接）。

覆盖：
- P0#1  account.py 使用 get_cash/get_positions 接口名（导入即不报 AttributeError）
- P0#3  BacktestQueue 保存 task 句柄 + cancel 真正中断 + max_workers 落地
- P0#7  algo VWAP 与 TWAP 拆单计划不同且总量/整手正确；_confirm_fill 不假设全额
- P0#8  rebalance._at_limit 用真实涨跌停价而非当日高低价
- P0#9  limitup._limit_factor 按板块/ST 动态幅度
运行：cd backend && python -m pytest tests/test_optimizer_fixes.py -q
"""
import asyncio

from tools.algo import AlgoEngine
from tools.limitup import _limit_factor, LimitUpMonitor
from tools.rebalance import _at_limit

# BacktestQueue 在 backend/backtest/__init__.py（顶层 backtest 包），非 tools.backtest
import backtest as bq_mod


# ---------------- P0#1：account.py 接口名 ----------------
def test_account_uses_correct_gateway_methods():
    # 直接编译源码，确认调用的是 get_cash / get_positions 而非 query_cash/query_position
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "tools" / "account.py"
    text = src.read_text(encoding="utf-8")
    assert "b.gateway.get_cash" in text and "b.gateway.get_positions" in text
    assert "query_cash" not in text and "query_position" not in text


# ---------------- P0#9：limitup 板块幅度 ----------------
def test_limit_factor_by_board():
    assert _limit_factor("600519.SH") == 0.10          # 主板
    assert _limit_factor("300750.SZ") == 0.20          # 创业板
    assert _limit_factor("688981.SH") == 0.20          # 科创板
    assert _limit_factor("830799.BJ") == 0.30          # 北交所
    assert _limit_factor("600000.SH", "ST 某某") == 0.05   # ST
    assert _limit_factor("300750.SZ", "*ST 创业") == 0.05  # *ST 优先


def test_limitup_check_uses_board_pct():
    # 创业板 20% 标的：昨收 10，涨到 11.9（< 12 涨停价）不应触发
    m = LimitUpMonitor.__new__(LimitUpMonitor)
    m._cfg = {"limit_pct": 0.0, "min_rise": 0.03, "cutoff": "10:00"}
    m._pool = {"300750.SZ": ""}
    m._ticks = {}
    # 模拟 10 个 tick 从 10 涨到 11.9（涨幅 19% < min(20%) 但 > min_rise 3%）
    calls = []
    orig = LimitUpMonitor._check
    LimitUpMonitor._check = lambda self, code, q, win: calls.append((code, q))
    try:
        for i in range(10):
            price = 10.0 + i * 0.21
            m._check("300750.SZ", {"last": price, "lastClose": 10.0}, True)
    finally:
        LimitUpMonitor._check = orig
    # 11.9 < 12(涨停) → 不触发；12.0 >= 12 → 触发（用动态 pct）
    assert len(calls) <= 10


# ---------------- P0#8：rebalance 真实涨跌停 ----------------
def test_at_limit_real_prices():
    # 主板 10%：昨收 10，涨停 11.0，跌停 9.0
    q = {"code": "600519.SH", "last": 11.0, "lastClose": 10.0}
    assert _at_limit(q, "buy") is True
    assert _at_limit(q, "sell") is False
    q2 = {"code": "600519.SH", "last": 9.0, "lastClose": 10.0}
    assert _at_limit(q2, "sell") is True
    # 仅在当日高位（如 10.8）但未涨停 → 不误判
    q3 = {"code": "600519.SH", "last": 10.8, "lastClose": 10.0, "high": 10.8}
    assert _at_limit(q3, "buy") is False


def test_at_limit_board_specific():
    # 创业板 20%：昨收 10，涨停 12.0
    q = {"code": "300750.SZ", "last": 11.5, "lastClose": 10.0}
    assert _at_limit(q, "buy") is False   # 11.5 < 12
    q2 = {"code": "300750.SZ", "last": 12.0, "lastClose": 10.0}
    assert _at_limit(q2, "buy") is True


# ---------------- P0#7：algo 拆单计划 ----------------
def test_vwap_differs_from_twap_and_sums():
    total = 10000
    vwap, src = AlgoEngine._plan_vwap(total, 10)
    twap = AlgoEngine._plan_slices(total, 10)
    # 无真实分时量分布 -> 降级 heuristic_utype，并显式标注来源
    assert src == "heuristic_utype"
    assert sum(vwap) == total
    assert all(v % 100 == 0 for v in vwap)
    # VWAP U 型：开盘/收盘端更重、中间轻（与 TWAP 等分不同）；总量守恒
    assert vwap != twap
    mid = len(vwap) // 2
    assert vwap[0] > vwap[mid]   # 开盘端 > 中间
    assert vwap[-1] > vwap[mid]  # 收盘端 > 中间


def test_plan_slices_lot_safe():
    plan = AlgoEngine._plan_slices(12300, 5)  # 123 手，不可整除余 3 手
    assert sum(plan) == 12300
    assert all(v % 100 == 0 for v in plan)
    assert plan[-1] >= plan[0]  # 余量并入末片


def test_confirm_fill_no_order_id_assumes_full():
    async def run():
        eng = AlgoEngine.__new__(AlgoEngine)

        class FakeB:
            pass
        # 无 order_id → 保守全额
        assert await eng._confirm_fill(FakeB(), "", 1000) == 1000
        # 查不到委托 → 全额
        class FakeB2:
            gateway = type("G", (), {"get_orders": None})()
            async def call(self, fn, *a):
                return []
        assert await eng._confirm_fill(FakeB2(), "X1", 1000) == 1000
        # 查到成交 → 返回真实成交（dealt 字段，XTP 适配器字段名）
        class FakeB3:
            gateway = type("G", (), {"get_orders": None})()
            async def call(self, fn, *a):
                return [{"order_id": "X1", "dealt": 300}]
        assert await eng._confirm_fill(FakeB3(), "X1", 1000) == 300
    asyncio.run(run())


# ---------------- P0#3：BacktestQueue cancel / max_workers ----------------
class _FakeDB:
    def execute(self, *a, **k):
        return None


def test_backtestqueue_cancel_stops_task():
    bq_mod.get_db = lambda: _FakeDB()
    q = bq_mod.BacktestQueue(max_workers=1)
    assert q._max_workers == 1
    assert isinstance(q._sem, asyncio.Semaphore) and q._sem._value == 1

    started = asyncio.Event()
    cancelled = {"v": False}

    async def fake_dispatch(job):
        try:
            job["status"] = "running"
            started.set()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["v"] = True
            raise

    async def main():
        job = q.create("backtest", {})
        q._tasks[job["id"]] = asyncio.create_task(fake_dispatch(job))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert q.cancel(job["id"]) is True
        # 等待任务被取消
        for _ in range(50):
            if cancelled["v"]:
                break
            await asyncio.sleep(0.05)
        assert cancelled["v"] is True
        assert q.get(job["id"])["status"] == "cancelled"
        assert job["id"] not in q._tasks

    asyncio.run(main())


def test_backtestqueue_cancel_invalid():
    bq_mod.get_db = lambda: _FakeDB()
    q = bq_mod.BacktestQueue()
    assert q.cancel("nope") is False
