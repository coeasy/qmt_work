# 数据同步冷热分层改造方案（PLAN_COLD_HOT_SYNC）

> 目标：把"离线数据同步 / EOD 日线"从「单一 `local_bars` 全量维护」改为**冷热分层**——
> 按数据条件自动判定每只标的**是否需要同步热数据 / 冷数据 / 两者都不要**，热数据只维护
> 最近 `bars_hot_days` 天（默认 92），冷数据（历史）只在缺失时一次性补齐，日常不同步。

## 0. 现状与根因

代码里**已经存在一套冷热基础设施**，但只在「缓存/读取层」生效，批量同步层并未接入：

| 层 | 表 / 文件 | 冷热是否生效 | 写入者 |
|----|-----------|--------------|--------|
| 缓存/读取层 | `kline_cache`(热, 主库) + `kline_archive`(`bars_cold.db`, 冷) | ✅ 已生效（`KlineCache` 按 `bars_hot_days` 路由；`get` 透明合并热+冷） | `fetch_kline_cached` / `MarketSync._refresh_hot` |
| 批量同步层 | `local_bars`(主库, 单表) | ❌ 不分层（incremental 写最近 `lookback` 根；full 写全部历史，都进同一张表） | `BarsSyncer`（EOD、`system.sync_bars`、手动"同步"） |

问题：
1. `local_bars` 已是 395 万行 / `app.db` 1.56GB，全部历史挤在一张表里，热路径与历史混存，
   写入放大与 WAL 膨胀无法隔离。
2. EOD 每天把 `lookback=120` 根整窗口重写一遍（即便已裁剪，仍反复触碰热表全量行）。
3. **历史从未被"冷化"**：`BarsSyncer` 不认识 `bars_cold.db`，全量回补的结果也写进 `local_bars`，
   再靠 `MarketSync.archive_rollover` 把旧行搬到 `bars_cold.db`——但这只搬"当前已在热表"的行，
   全新安装时热表只有 92 天，历史**永不被补齐**。
4. 没有"冷数据是否齐全"的判定，无法做到"缺失才补、齐全则跳过"。

## 1. 设计原则

- **复用既有冷仓**：热→`local_bars`（维持现有 bulk 消费者：股票池/快照/degrade），冷→`bars_cold.db` 的
  `kline_archive`（与 `KlineCache` 同一张表、同一 `UNIQUE(code,period,adjust,dt)`）。
  ⇒ **图表/回测走 `fetch_kline_cached` 会自动合并冷仓，无需改任何读取方**（这是本方案能低风险落地的关键）。
- **不迁移 `local_bars` 消费者**：股票池（`codes_with_bars`）、数据集快照（`publish_local_bars`）、
  degrade 路径继续读 `local_bars`（热）。`publish_local_bars` 仅做"覆盖区间/行数 UNION 冷仓"的小幅增强。
- **自动判定，不靠游标表**：与现有 `_filter_fresh`/`_filter_backfilled` 一致，用"库里实际已有的起止日期"
  当事实游标，崩了也不丢进度。新增 `cold_complete:<code>` 标记防止重复补齐冷数据。
- **向后兼容**：`BarsSyncer` 保留 `mode`（incremental/full）语义；新增 `zone` 参数（auto/hot/cold/both），
  默认 `auto` = 热必做 + 冷按需。旧调用（不传 zone）行为等价于 `zone="hot"`（日常维护语义不变）。

## 2. 冷热边界与自动判定

边界：`hot_cutoff = today - bars_hot_days`（单一真源同 `KlineCache.hot_cutoff`，避免两套边界漂移）。

每只标的的同步计划 `SyncPlan`：

```
needs_hot  = 本地热表最新一根 < 预期交易日        # 日常要拉新的一天
needs_cold = (上市日 < hot_cutoff)               # 该股确实"有冷区间"
           AND 冷仓最早一根 > 上市日/区间起点       # 冷区间还没补齐
           AND not cold_complete[code]            # 没被标记为已补齐
```

- 新股（上市 < `bars_hot_days` 天前）→ `needs_cold=False`（根本没有冷区间）。
- 退市/长期停牌 → `needs_hot=False`（无新数据），`needs_cold` 按历史是否齐全判定。
- `cold_complete[code]` 在冷区间补齐后写入 `local_sync_meta`；之后日常同步直接跳过冷。

## 3. BarsSyncer 改造（核心）— 已落地

`app/sync/bars.py`：
- `__init__` 新增 `zone: str = "auto"`（取值 `auto`/`hot`/`cold`/`both`），从 `settings.bars_hot_days` 读边界；
  `mode="full"` 在未显式给 zone 时等价于 `zone="both"`（向后兼容）。
- 新增 `SyncPlan` dataclass（`hot: bool`, `cold: bool`）+ `_build_plans(codes) -> (plans, skipped_fresh, skipped_cold)`。
  判定逻辑落在 `_build_plans` 内（非独立纯函数）：
  - `do_hot = zone != "cold"`，且当 `hot_target` 已有最新一根时置 False（热已够新）。
  - `zone in (cold, both)`：`do_cold = (code not in cold_complete) and not _covered(code)`
    （`_covered` = 热表或冷仓任一最早根 ≤ 目标起点 ⇒ 历史已齐，整只跳过）；covered 时连带 `do_hot=False`
    （保留旧 `mode=full` 断点续传整只跳过语义）。
  - `zone == "auto"`：`do_cold = (not cold_complete) and has_cold_interval and (cold_earliest 缺失或晚于目标起点)`，
    其中 `has_cold_interval = 热表最早一根存在且 < hot_cutoff`（**新股不补冷**）。
- `sync_one` → `_sync_one_zones(code, plan)`：热写 `local_bars`（沿用 `upsert_bars`）；冷经 `ColdStore` 单例写
  `bars_cold.db.kline_archive`（不可用降级主库 `kline_archive`），补齐后写 `cold_complete:<period>:<adjust>:<code>` 标记。
- `sync_stock_list`：先 `_build_plans` 批量判定，再分派；全跳过时提前返回 `SyncSummary(..., skipped_complete=skipped_cold, skipped_fresh=skipped_fresh)`
  （避免被作业调度误判空转失败）。汇总字段 `cold_written`/`skipped_complete` 已加。
- `full` 模式语义收敛为"冷区间补齐"（`zone="cold"/"both"` 的底层实现），不再把历史塞进热表。

## 4. 上游接线 — 已落地

- `app/runtime/eod.py`：主同步与 fallback 两处 `BarsSyncer(...)` 均加 `zone="hot"`（廉价，只维护最近窗口）。
- `app/runtime/jobs.py` `_sync_bars_runner`：透传 `zone`（`zone = params.get("zone") or ("both" if mode=="full" else "hot")`），
  旧调用（只传 mode）行为不变：incremental→只热，full→全量。显式 zone 永远优先。
- `app/runtime/system_jobs.py`：
  - `sch-default-sync-bars` 默认调度 `params` 加 `"zone": "hot"`（与冷调度同 `local_bars` 互斥组错峰）。
  - **新增** `sch-default-sync-cold` 调度：复用 `system.sync_bars` + `params={"zone":"cold","mode":"full","full_years":12,"concurrency":4}`，
    `cron: "0 3 * * 0"`（每周日凌晨，避开交易时段）。
- 前端 `domains/system/OfflineData.tsx`：手动同步新增 `zone` 选择（热维护 / 补齐冷历史 / 热+冷全量 / 自动判定），
  透传给 `system.sync_bars` 的 `params.zone`；"全量回补历史"按钮仍走 `mode="full"`（等价 `zone="both"`）。

## 5. 数据集快照 — 已落地

- `app/services/dataset_store.py` `count_table`（覆盖度报表）：`local_bars` 分支保留自身 `rows`/`codes`
  （避免与冷仓重复计数），但把冷仓 `kline_archive` 起止日期**并入覆盖区间**（`first_dt`/`last_dt` 取并集），
  并新增 `out["cold"] = {rows, codes, first_dt, last_dt}` 块。冷仓不可读则退化仅热表。
- `app/datasource/snapshots.py` `publish_local_bars`：不改 per-batch `checksum`/`coverage_start/end`/`row_count`
  （掺冷数据会让校验和失真），仅把**本批次标的的冷仓覆盖**补进 manifest 的 `cold_coverage` 字段
  （新增静态方法 `_cold_coverage_for_codes`，分批 IN ≤500 统计冷仓 `kline_archive`）。

## 6. 验证与门禁 — 已落地

- 新增 `tests/test_sync_cold_hot.py`（5 用例全过）：
  1. `_build_plans` 四场景（新股 / 老股冷齐全 / 老股缺冷且陈旧 / 老股缺冷但热新鲜）；
  2. `zone="hot"` 只写 `local_bars`、冷仓 0 行；
  3. `zone="cold"` 只写冷仓 6 行、热表 0 行、打 `cold_complete`；
  4. 冷补齐完成后二次同步 `skipped_complete≥1` 且冷仓不增长；
  5. 端到端 `KlineCache.get` 经冷仓透明读到 6 根。
- 回归（已全绿，2026-10-13 复跑）：`test_sync_cold_hot.py` 5、`test_sync_bars.py` 21、
  `test_sync_bars_full.py` 13、`test_sync_bars_broker_fallback.py` 7、`test_sync_bars_write_trim.py` 10、
  `test_bars_batch_pivot.py` 6、`test_bars_batch_read_path.py` 9、`test_market_sync.py` 22、
  `test_eod_pipeline.py` 6 —— 合计 99 例通过。
- **本轮修复的潜伏 P0 死锁**（§7 风险表已补）：`_sync_one_zones` 外层持 `self._sem` 又调
  `sync_one` 内层再取**同一把不可重入** `asyncio.Semaphore` ⇒ 槽占满即永久挂死，
  且日志零输出。已改为「槽只由叶子（`sync_one` / `_sync_one_cold`）取，编排层不占槽」。
- 门禁：14/14 全绿；`ci_reconcile.EXPECTED_TESTS` = 2508。
- 真机小批量实证（待做）：`zone="hot"` 只动热表；`zone="cold"` 后图表历史可见。

## 7. 风险与缓解

- **并发槽自我阻塞（已发生，P0）**：`_sync_one_zones` 曾「外层持槽 → 调 `sync_one` 内层再取槽」，
  而 `asyncio.Semaphore` **不可重入** ⇒ 槽占满后永久挂死，`gather` 无超时、日志零输出、
  进程仍活着（`test_sync_many_concurrency_bounded` 曾 25s 超时挂死）。
  缓解：槽**只由叶子取**（`sync_one` 热 / `_sync_one_cold` 冷），编排层 `_sync_one_zones` 不占槽。
  注意冷路径**不能**豁免占槽——冷区间取数比热窗口更重，无闸门时
  `zone=cold/both` 会让数千标的同时打券商区间接口。
- **冷仓写冲突**：`BarsSyncer` 与 `MarketSync.archive_rollover` 可能同时写 `kline_archive`——
  二者都走 `UNIQUE(code,period,adjust,dt) ... INSERT OR REPLACE`，幂等无冲突。
- **`local_bars` 体积不再增长历史**：热表恒定 ~92 天，WAL 只搬热数据 ⇒ 主库不再随历史膨胀。
- **不破坏现有 bulk 消费者**：热表内容/语义不变，仅冷数据挪到共享冷仓。

## 8. 不在本轮范围

- `local_bars` → `kline_cache` 的单库存合（更大范围的"双轨合并"史诗，单独立项）。
- 多 provider 冗余回收（canonical 回收，另见待办）。
- 冷仓自身的压缩/清理策略（保留现状：永久保留）。
