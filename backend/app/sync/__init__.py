"""qmt_work 同步任务包（G1-5）。

入口：``app.sync.bars.BarsSyncer`` —— 全市场/单标的日线窗口同步。
- 窗口增量：拉取每标的最新 ``lookback`` 根 K 线（eltdx 按 count 取数，无 date-range
  接口），经 ``LocalStore.upsert_bars`` 按主键 (code,period,adjust,dt) 幂等合并，
  重复日期自动覆盖不膨胀。
- 复权：默认 ``qfq``，复权维度入主键，同一标的前/后复权各自独立存储。
- 并发闸门：信号量限并发（G6 JobRuntime 落地前先本地收敛，避免打爆远端）。
- 默认抓取器走真实 ``DataSourceManager.get_kline``（auto 链），测试注入假抓取器，
  保证测试密闭且不触碰真实网络（零 mock 铁律：生产路径全真实）。
"""
from app.sync.bars import BarsSyncer, SyncOutcome, SyncSummary, weekday_calendar

__all__ = ["BarsSyncer", "SyncOutcome", "SyncSummary", "weekday_calendar"]
