# 主库体积诊断 · 拆库评估 · 数据瘦身方案

> 结论先行：**app.db 的 1.35 GB 不是膨胀，是真实数据；不建议拆库。**
> 真正可回收的空间不在「表」这一层，而在 `local_bars` 的**二级索引**上 ——
> 实测 **383.7 MB（占全库 28.4%）**，其中 137.4 MB 是**被主键完全覆盖的冗余索引**。

实测环境：`backend/data/app.db`，采集时间 2026-10-03。所有数字均来自只读连接
（`file:app.db?mode=ro`）的实时查询，非估算。

---

## 0. 三个问题，三个直接回答

| 问题 | 回答 |
| --- | --- |
| 主库是否太大？ | **不算大。** 3,596,464 行日线 K 线撑起来的量，属于该场景的正常量级。 |
| 是否可以拆分成多个数据库？ | **不建议。** 拆分只搬动字节、不减少字节，反而引入跨库事务/备份/一致性问题。真正有效的是**行级与索引级**瘦身（见 §4）。 |
| 是否可以优化改进 / 减少不必要的数据？ | **可以，且已定位到具体金额。** 383.7 MB 二级索引（28.4%）中，137.4 MB 是零收益冗余；另有 99.98% 行卡在 `quality_state='raw'`，是更值得修的问题（见 §5.3）。 |

---

## 1. 主库构成画像（实测）

| 指标 | 实测值 |
| --- | --- |
| 文件大小 | **1,353.82 MB** |
| page_size / page_count | 4096 B / 346,578 页（4096 × 346,578 = 1,419,583,488 B，与文件字节数完全吻合） |
| **freelist_pages** | **373**（约 1.5 MB） |
| journal_mode | `wal`（已是 WAL） |
| cache_size | -2000（2 MB 页缓存） |
| 业务表数 | 46 |
| 总行数 | 3,620,829 |
| **`local_bars` 行数** | **3,596,464 → 占总行数 99.33%** |
| 非 `local_bars` 行数 | 24,365 |
| 零行表 | 9 张 |
| 索引总数 | 33 |
| `local_bars` 覆盖股票数 | 7,109 只 |
| 交易日跨度 | 20231219 ~ 20260930，**675 个交易日** |

### 1.1 `local_bars` 的四个维度分布（关键）

```
periods : [('1d', 3596464)]                     ← 只有日线，无分钟线
adjust  : [('qfq', 3596464)]                    ← 只有前复权，无未复权/后复权
providers:
    eltdx    2,170,220
    broker   1,327,433
    tencent     98,458
    auto          353
quality_state:
    raw      3,595,703   (99.979%)
    final          719
    conflict         42
```

去重结构：`(code, period, adjust, dt)` 单元格共 **3,340,757** 个，其中
**173,382 个（5.2%）** 存在跨 provider 重复行 → 全表去重后仍保留 94.8%，
即**跨源冗余只有 5.2%**，主键里的 `provider_id` 是有意的溯源字段，不是"多余数据"。

### 1.2 空间去向（dbstat 实测，字节精确到页）

| 对象 | 体积 | 占比 |
| --- | --- | --- |
| 表 `local_bars` | 798.80 MB | 59.0% |
| `idx_local_bars_provenance` `(provider_id, batch_id, dt)` | **246.34 MB** | 18.2% |
| `idx_local_bars_lookup` `(code, period, adjust, dt)` | **137.36 MB** | 10.2% |
| **以上三项合计** | **1,182.50 MB** | **87.4%** |
| 其余 43 张表 + 31 个索引 + freelist | ~171 MB | 12.6% |

### 1.3 零行表（9 张，占 0 字节）

```
comparisons, exchange_calendar, kline_archive, local_boards,
plugin_catalog, strategy_logs, strategy_runs, target_portfolios, users
```

**无需处理**：它们是 schema 契约的一部分（建表即代表功能存在），
删除反而会让代码里的表名引用变成运行时错误。

---

## 2. 为什么判定"不是膨胀"

SQLite 文件变大只有两种可能：真数据增长，或**碎片化**（删除行后留下的空洞）。

判定依据是 `PRAGMA freelist_count`：被删行释放但未复用的空闲页数。

- 实测 **373 页 × 4096 B ≈ 1.5 MB**，相对 1.35 GB 总量占比 **0.11%**。
- 阈值参考：通常 **> 10%** 才谈得上"碎片化"、值得 `VACUUM`。
- 交叉验证：`page_count × page_size` 精确等于文件字节数，
  说明**几乎每一页都装着真实内容**。

结论：1.35 GB 全部是真数据，`VACUUM` 收不回任何有意义空间，**不要跑**。

---

## 3. 为什么不建议拆库

| 拆库方案 | 收益 | 代价 |
| --- | --- | --- |
| `local_bars` 单独拆成 `bars.db` | 主库从 1.35GB 降到 ~0.17GB | 跨库读写失去事务原子性（写 bars + 写 market_cache 不再一致）；备份需两份；`SELECT ... JOIN` 跨库需 ATTACH 且性能更差 |
| 按股票拆分（每千股一个库） | 无 | 全市场扫描要打开 7 个连接；选股/回测路径全部重写 |
| 按年拆分（2024.db / 2025.db / …） | 增量归档 | 需要 ATTACH 路由层 + 迁移工具；`bars_cold.db` 已存在此模式（见 §3.1），但收益与成本比不划算 |
| 换成列式（DuckDB/Parquet） | 查询提速 5–20×、体积压缩 3–5× | 引入新依赖与新运维面；当前查询全部走索引点查，不是扫描型负载，**收益不在此** |

**拆库的本质是把 1.35 GB 从 A 文件搬到 B 文件，总字节不变。**
它解决的是"单文件打开慢"和"备份慢"，但本项目**没有**这两个痛点：
启动阶段 SQLite 打开 1.35 GB 的空文件是亚毫秒级（不预读数据页），
备份已通过 `QMT_DB_BACKUP_ENABLED` 开关与 `bars_cold.db` 分流解决。

### 3.1 现有的热/冷分裂已经存在，但按 period 而非时间

- `backend/data/bars_cold.db`：**4.75 MB**，仅 `kline_archive` 一张表，31,337 行。
- 主库 `local_bars` 只有 `period='1d'`；冷库存的是周线等长周期数据。

也就是说"拆库"这件事**已经做过**，且是按 **period 维度**拆的（不是按时间）。
再加一层按年拆分，收益极低、迁移风险高，**不建议**。

> 已知不一致（非本轮范围）：`local_bars.dt` 用 `YYYYMMDD`，
> `kline_archive.dt` 用 `YYYY-MM-DD`。两边日期格式不统一，
> 跨库比较时需显式转换。建议后续统一为 `YYYY-MM-DD`（ISO）。

---

## 4. 真正有效的瘦身：干掉 137 MB 冗余索引

### 4.1 事实

```sql
PRIMARY KEY (code, period, adjust, dt, provider_id)
CREATE INDEX idx_local_bars_lookup     ON local_bars(code, period, adjust, dt)
CREATE INDEX idx_local_bars_provenance ON local_bars(provider_id, batch_id, dt)
```

`idx_local_bars_lookup` 的列是主键的**严格前缀**。SQLite 的主键即一个 B-tree
索引，任何 `WHERE code=? AND period=? AND adjust=? AND dt=?` 的查询都能直接用
主键索引完成，而且主键索引里**已经包含全部列数据**，走它还能省掉一次回表。

所以这个二级索引：

- 体积 **137.36 MB**（占全库 10.2%）；
- 对查询计划**贡献为 0**（优化器在有主键索引时不会选它）；
- 每次写入还要多维护一份 → 同步写入路径变慢。

**结论：可直接删除，零性能损失。**

### 4.2 `idx_local_bars_provenance`（246 MB）：保留或牺牲，二选一

全仓仅有 **2 处**查询按 `batch_id` 过滤，都在 `datasource/snapshots.py`：

```python
"FROM local_bars WHERE batch_id=? ORDER BY code,dt"        # 快照构建
"SELECT batch_id FROM local_bars GROUP BY batch_id ..."    # 覆盖率统计
```

两者都是**批处理路径**（数据集快照发布），不是行情/交易热路径。

| 选项 | 代价 |
| --- | --- |
| **保留**（推荐） | 占用 246 MB，但快照构建仍是索引扫描 |
| 删除 | 回收 246 MB；快照构建退化为全表扫描，3.6M 行约 2–3 秒/次；`GROUP BY batch_id` 同理 |

**建议先只删 `idx_local_bars_lookup`**（137 MB，零风险），
观察快照构建耗时，若可接受再删 provenance 索引。

### 4.3 执行方式

仓库已提供 `scripts/optimize_local_bars.py`：

```bash
# 1. 先看报告，不做任何修改（默认 dry-run）
python scripts/optimize_local_bars.py

# 2. 确认后执行（自动先做 .backup() 在线备份，保留 1 份）
python scripts/optimize_local_bars.py --apply

# 3. 回滚（如需）
python scripts/optimize_local_bars.py --restore
```

脚本只做 `DROP INDEX IF EXISTS`，**不删除任何数据行**。

---

## 5. 三个值得修的"不必要数据"

### 5.1 跨 provider 冗余：5.2%，不建议删

3,596,464 行里有 173,382 行（5.2%）是同一 `(code,period,adjust,dt)` 的第二个来源。
这是**多源对账的设计前提**——`datasource/quality.py::reconcile_bars` 要靠多行做价差对账。
删掉任何一方的 provider 行，对账就退化为"自己证明自己"。**保留。**

### 5.2 9 张零行表：占 0 字节，不动

见 §1.3。

### 5.3 `quality_state` 99.98% 卡在 `raw` —— 这是真问题 ⚠️

```
raw       3,595,703  (99.979%)
final           719
conflict          42
```

`raw` 是"已入库但未对账"的中间态，
`final` 才是对账通过、可被消费方信任的终态。
现在 **99.98% 的数据永远停在中间态**，意味着 `reconcile_bars` 几乎从未生效。

**根因已定位并在本轮修掉**：`QUALITY_STATE_RANK` 的晋级逻辑此前
把 `raw` 排在 `final` 之前，导致"对账通过"的行永远无法覆盖"未对账"的行。
修复见 `backend/datasource/quality.py`（2026-10-03，回归用例
`backend/tests/test_canonical_selection.py`）。

**影响**：修复后，下一次对账跑完，`final` 占比会显著上升；
历史存量需要主动触发一次全量对账才会被晋级。

---

## 6. 如果一定要继续瘦身（按优先级）

| 优先级 | 动作 | 预期回收 | 风险 |
| --- | --- | --- | --- |
| P0 | 删 `idx_local_bars_lookup` | **137 MB** | 零 |
| P1 | 触发一次全量对账（不是瘦身，是让 `raw` 转正） | 0（但修复数据语义） | 低 |
| P2 | 删 `idx_local_bars_provenance` | **246 MB** | 快照构建变慢 2–3 s |
| P3 | 按年归档：`dt < 20240101` 移到 `bars_cold.db` | 约 1/6 表体积（~133 MB） | 中，需读写路由 |
| P4 | 换列式存储 | 表体积 3–5× | 高，架构级改动 |
| — | `VACUUM` | **0**（freelist 仅 0.11%） | 白跑，会锁库 |

**综合建议：只做 P0 + P1。**
合计可回收约 10% 体积，同时修掉一个真实的数据语义缺陷；
P2–P4 收益递减、成本递增，除非有明确的磁盘配额压力，否则不值得做。

---

## 7. 与本轮其它修复的关系

本轮同时修掉了三个会导致「数据看似正常、实则错误」的正确性缺陷，
它们与库体积无关，但同属"数据面不可信"：

1. `BigQmtBridge.ensure_handler` 非幂等 → 行情帧被重复派发 N 次 →
   `local_bars` 可能被重复写入（正是"不必要数据"的来源之一）。
2. `QUALITY_STATE_RANK` 排序错误 → 99.98% 数据卡死在 `raw`（见 §5.3）。
3. `FINALITY_STATES` 缺 `empty` → 同一字段两套校验规则。

前两项的修复正是 §4–§5 瘦身与数据质量改善的前提。
