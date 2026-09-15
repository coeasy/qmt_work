"""qmt_work 本地数据仓（G1-4，SQLite 主源层）。

来源借鉴：通达信本地数据仓 + OpenBB Provider 抽象。目标：
- **离线可用**：断网时 K 线 / 股票列表 / 板块榜仍可查询（数据陈旧但明示）。
- **全市场选股**（G7）：选股引擎走本地向量化扫描，不逐股打远程接口。
- **降级兜底**（G1-6）：远程失败 → 查本地并标 ``stale=True + as_of``（降级≠造假）。

与 ``kline_cache`` 的分工：``kline_cache`` 是券商直连的**展示 TTL 缓存**（近年、
复权未入唯一键、每年归档）；本仓是**离线仓库**（``adjust`` 入主键、可全市场增量
同步、可配置磁盘清理策略）。两条线解耦，避免改动热路径。

线程安全：写经 ``_lock`` 串行；读走 ``DB.query``（自带连接锁）。所有表在
``app/db.py`` 迁移 v16 创建（新版本号，不影响已应用的 v1–v15）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from core.db import DB, get_db
from datasource.models import Bar, BarLite, BoardItem, StockInfo

log = logging.getLogger("qmt_work.datasource.local_store")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _num(v: Any) -> Optional[float]:
    """数值安全转换：None/''/非法 → None（绝不估算填充）。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


#: Canonical 选主的质量状态优先级（小者优先），与两处窗口函数 CASE 一一对应。
_QUALITY_RANK = {"validated": 0, "complete": 1, "match": 2}


def _canonical_key(row: Any) -> tuple:
    """Canonical 选主排序键 —— 逐项复刻窗口函数里的 ``ORDER BY``（小者优先）。

    1. 质量状态 ``validated > complete > match > 其他``（对应 CASE ... ELSE 3）；
    2. ``(provider_id = '') DESC`` —— 空 provider 排在前面（保持既有语义）；
    3. ``provider_id`` 升序。

    注：``local_bars.provider_id`` 是 ``NOT NULL DEFAULT ''``，故无需处理 NULL；
    ``quality_state`` 为 NULL 时与 SQL 的 ``ELSE 3`` 一致，落到最后一档。
    """
    return (
        _QUALITY_RANK.get(row["quality_state"], 3),
        0 if (row["provider_id"] or "") == "" else 1,
        row["provider_id"] or "",
    )


def _row_to_bar(row: Any, lite: bool = False) -> Union[Bar, BarLite]:
    """``sqlite3.Row`` → ``Bar``（``lite=True`` 时 → ``BarLite`` 轻量视图）。

    只喂模型真正声明的列，不让溯源列参与校验。两种目标的**取值同源**（同一个
    函数体、同一份列清单），避免拆成两个函数后各自漂移。

    ``lite`` 的取舍见 :class:`datasource.models.BarLite`：全市场批处理下逐行
    Pydantic 校验是最大单项成本，轻量视图约快 3 倍。默认 ``False`` 保持
    ``get_bars_batch`` 的对外契约（返回 ``Bar``）不变。
    """
    cls = BarLite if lite else Bar
    return cls(
        time=row["dt"], open=row["open"], high=row["high"], low=row["low"],
        close=row["close"], volume=row["volume"], amount=row["amount"],
    )


class LocalStore:
    """本地数据仓访问层（日线 / 股票列表 / 板块榜 / 同步元数据）。"""

    def __init__(self, db: DB):
        self._db = db
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # K 线（主键 code/period/adjust/dt/provider_id —— 多源隔离，V9 §10.3）
    # ------------------------------------------------------------------
    def upsert_bars(
        self,
        code: str,
        bars: List[Union[Bar, dict]],
        period: str = "1d",
        adjust: str = "",
        provider_id: str = "",
        batch_id: str = "",
        schema_version: str = "1",
        quality_state: str = "unknown",
    ) -> int:
        """批量写入 / 覆盖 K 线并保存 provider/batch 溯源信息。

        ``checksum`` 是单行规范化内容的 SHA-256，不是对缺失字段的估算；它只用于
        重复同步和多源对账时发现内容变化。旧调用方不传溯源参数时仍可写入，但质量
        状态会明确保留为 ``unknown``，不会被伪标成 validated。
        """
        rows: List[tuple] = []
        for b in bars:
            d = b.model_dump() if isinstance(b, Bar) else dict(b)
            values = {
                "time": str(d.get("time", "")),
                "open": _num(d.get("open")),
                "high": _num(d.get("high")),
                "low": _num(d.get("low")),
                "close": _num(d.get("close")),
                "volume": _num(d.get("volume")),
                "amount": _num(d.get("amount")),
            }
            checksum = hashlib.sha256(
                json.dumps(values, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            rows.append((
                code, period, adjust, values["time"], values["open"], values["high"],
                values["low"], values["close"], values["volume"], values["amount"],
                _now(), provider_id, batch_id, checksum, schema_version, quality_state,
            ))
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO local_bars "
                "(code,period,adjust,dt,open,high,low,close,volume,amount,fetched_at,"
                "provider_id,batch_id,checksum,schema_version,quality_state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def get_bars(
        self,
        code: str,
        period: str = "1d",
        adjust: str = "",
        limit: int = 250,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[Bar]:
        """按时间升序取本地 K 线 → 标准模型 ``Bar`` 列表（可只传 limit 取最近 N 根）。

        V9 §10.3：多源 Raw 不互相覆盖（provider_id 入主键）后，同一 dt 可能存在
        多个 provider 的行。读取即 Canonical 选主：按质量状态
        （validated > complete > match > 其他）优先，其次优先具名 provider，
        保证消费方每根 K 线只看到一条确定性的主值。
        """
        inner = ("SELECT dt, open, high, low, close, volume, amount, provider_id, "
                 "quality_state, ROW_NUMBER() OVER (PARTITION BY dt ORDER BY "
                 "CASE quality_state WHEN 'validated' THEN 0 WHEN 'complete' THEN 1 "
                 "WHEN 'match' THEN 2 ELSE 3 END, (provider_id = '') DESC, provider_id"
                 ") AS rn FROM local_bars WHERE code=? AND period=? AND adjust=?")
        params: List[Any] = [code, period, adjust]
        if start:
            inner += " AND dt >= ?"
            params.append(start)
        if end:
            inner += " AND dt <= ?"
            params.append(end)
        sql = (f"SELECT dt AS time, open, high, low, close, volume, amount "
               f"FROM ({inner}) WHERE rn=1")
        if limit and limit > 0:
            # latest-N 的语义是「窗口内最近 N 根」，不能先升序 LIMIT 而返回最早数据。
            sql += " ORDER BY time DESC LIMIT ?"
            params.append(int(limit))
            rows = self._db.query(sql, tuple(params))
            rows.reverse()
            return [Bar.model_validate(r) for r in rows]
        sql += " ORDER BY time ASC"
        rows = self._db.query(sql, tuple(params))
        return [Bar.model_validate(r) for r in rows]

    def get_bars_batch(
        self,
        codes: List[str],
        period: str = "1d",
        adjust: str = "",
        limit: int = 250,
        lite: bool = False,
    ) -> Dict[str, List[Bar]]:
        """批量取多标的 K 线：单条 ``WHERE code IN (...)`` + 索引序取回，
        分块 SQL 取回整批，替代逐只 ``get_bars`` 的 N 次循环（P3 / Phase B）。

        设计目标：把「逐只循环 = N 次 SQL」降到「O(块数) 次 SQL」
        （约 5000 只 → 6 块）。SQLite 变量上限安全分块（每块 900 只）。
        Canonical 选主逻辑与 ``get_bars`` 完全一致（质量状态 + 具名 provider 优先），
        每块内对每标的最近 ``limit`` 根按时间升序返回。

        2026-09-14 吞吐优化（全市场选股请求自身 ~14.9s → ~10.3s，API 实测中位）：
        原实现用**双层窗口函数**（内层按 ``code, dt`` 去重、外层按 ``code`` 取最近 N）。
        在 117 万行真实库上执行计划出现 **3 次 ``USE TEMP B-TREE FOR ORDER BY``**，
        SQL 侧单次 ~9s。现改为 ``ORDER BY code, dt`` 直接命中 ``idx_local_bars_lookup``
        （执行计划为纯 ``SEARCH local_bars USING INDEX``，**零排序**）：同一
        ``(code, dt)`` 的多 provider 行在结果中天然相邻，选主下沉到 Python 侧一趟
        分组比较。选主键由 :func:`_canonical_key` 逐项复刻原窗口的 ``ORDER BY``，
        故结果逐行等价（已用 92 万行实测新旧结果完全一致）。

        2026-09-15 吞吐优化 2（``lite=True``）：117 万行三段成本实测
        = SQL 取回 ~3.6s + Python 选主 ~0.8s + ``Bar`` 构造 ~4.9s，构造是最大单项。
        ``lite=True`` 改构造 :class:`~datasource.models.BarLite`（``__slots__``
        轻量视图，字段集与 ``Bar`` 一致但跳过 Pydantic 校验），同规模实测
        ~1.9s（约 3 倍快），**选股结果逐行等价**（``tests/test_bar_lite.py`` 钉住）。
        取数行数不可再压缩——本库每标的仅 ~168 根，``limit=250`` 根本不生效，
        所以「缩减取数根数」方向无效。

        2026-09-15 吞吐优化 3（取数连接与消费方式）：改走
        :meth:`core.db.DB.readonly_conn` 的**独占只读连接** + 游标**流式**消费
        （原先 ``DB.execute()`` 是主连接 + 写锁，``fetchall()`` 还在锁外跑在主连接上）。
        并发写负载下单次批量读由 3925ms 回到接近空载水平（19.5× → 1.6×），且不再
        拖慢写者（单次写入 p99 73.6ms → 1.6ms）；流式另省掉整批行列表物化。细节与实测见
        ``DB.readonly_conn`` 与 ``tests/test_bars_batch_read_path.py``。

        ``lite`` 默认 ``False``：对外契约仍返回 ``Bar``；仅**进程内批处理消费方**
        （选股引擎）显式传 ``True``。返回类型因此随 ``lite`` 变化，调用方按属性
        访问即可（两者字段同名同义）。
        """
        if not codes:
            return {}
        seen: "dict[str, None]" = {}
        for c in codes:
            seen[str(c)] = None
        uniq = list(seen.keys())
        out: Dict[str, List[Bar]] = {c: [] for c in uniq}

        _CHUNK = 900  # 低于 SQLite 默认变量上限，避免 "too many SQL variables"
        # 2026-09-15：整批改走**独占只读连接 + 游标流式消费**。
        # 原先用 ``DB.execute()``：那是「主连接 + 写锁」，且 ``fetchall()`` 还在
        # 锁外跑在主连接上——既与每次写争抢同一连接的互斥量，又违反 db.py 自己
        # 写明的「主连接上的读必须与写互斥」。实测（3.2 万行小样）空载 201ms、
        # 并发写负载下 3925ms（**19.5×**，20s 只完成 3 轮），且把写者 p99 从
        # 0.15ms 推到 73.6ms。收益与边界见 :meth:`core.db.DB.readonly_conn`。
        # 另：流式消费不再把整批行物化成列表（117 万行时省约 0.9s）。
        # 句柄在**分块循环外**开一次，避免每块重连。
        with self._db.readonly_conn() as conn:
            for i in range(0, len(uniq), _CHUNK):
                chunk = uniq[i:i + _CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                sql = (
                    "SELECT code, dt, open, high, low, close, volume, amount, "
                    "provider_id, quality_state FROM local_bars "
                    f"WHERE code IN ({placeholders}) AND period=? AND adjust=? "
                    "ORDER BY code, dt"
                )
                cur_key: Any = None
                best: Any = None
                best_key: Any = None
                for r in conn.execute(sql, tuple(list(chunk) + [period, adjust])):
                    key = (r["code"], r["dt"])
                    if key != cur_key:
                        if best is not None:
                            out.setdefault(best["code"], []).append(
                                _row_to_bar(best, lite))
                        cur_key, best = key, r
                        best_key = _canonical_key(r)
                    else:
                        k = _canonical_key(r)
                        if k < best_key:
                            best_key, best = k, r
                if best is not None:
                    out.setdefault(best["code"], []).append(
                        _row_to_bar(best, lite))

        if limit and limit > 0:
            # 行已按 dt 升序，尾部即最近 limit 根（与 get_bars 的 latest-N 语义一致）。
            for c, bars in out.items():
                if len(bars) > limit:
                    del bars[:-limit]
        return out

    def count_bars(self, code: str, period: str = "1d", adjust: str = "") -> int:
        # 多源隔离后同一 dt 可能有多行，覆盖度按「交易日数」计（DISTINCT dt）。
        rows = self._db.query(
            "SELECT COUNT(DISTINCT dt) AS n FROM local_bars "
            "WHERE code=? AND period=? AND adjust=?", (code, period, adjust))
        return int(rows[0]["n"]) if rows else 0

    def latest_dt(self, code: str, period: str = "1d", adjust: str = "") -> Optional[str]:
        """最近一根 K 线日期（G1-5 增量同步的续传游标）。"""
        rows = self._db.query(
            "SELECT MAX(dt) AS m FROM local_bars "
            "WHERE code=? AND period=? AND adjust=?", (code, period, adjust))
        return rows[0]["m"] if rows and rows[0]["m"] else None

    def latest_bar_dt(self) -> Optional[str]:
        """全市场 K 线最近一根日期（选股溯源 as_of 之用）。表不存在返回 None。"""
        try:
            rows = self._db.query("SELECT MAX(dt) AS m FROM local_bars")
        except Exception:  # noqa: BLE001
            return None
        return rows[0]["m"] if rows and rows[0]["m"] else None

    # ------------------------------------------------------------------
    # 全市场股票列表
    # ------------------------------------------------------------------
    def upsert_stock_list(self, items: List[Union[StockInfo, dict]]) -> int:
        rows = []
        for it in items:
            d = it.model_dump() if isinstance(it, StockInfo) else dict(it)
            rows.append((str(d.get("code", "")), str(d.get("name") or ""),
                         str(d.get("category") or ""), _now()))
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO local_stock_list "
                "(code,name,category,updated_at) VALUES (?,?,?,?)", rows)
        return len(rows)

    def get_stock_list(self) -> List[dict]:
        return self._db.query(
            "SELECT code, name, category FROM local_stock_list ORDER BY code")

    # ------------------------------------------------------------------
    # 板块榜（kind = industry/concept/stat…）
    # ------------------------------------------------------------------
    def upsert_boards(self, kind: str, items: List[Union[BoardItem, dict]]) -> int:
        rows = []
        for it in items:
            d = it.model_dump() if isinstance(it, BoardItem) else dict(it)
            rows.append((kind, str(d.get("code", "")), str(d.get("name") or ""),
                         _num(d.get("last")), _num(d.get("change_pct")),
                         _num(d.get("amount")), _now()))
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO local_boards "
                "(kind,code,name,last,change_pct,amount,updated_at) "
                "VALUES (?,?,?,?,?,?,?)", rows)
        return len(rows)

    def get_boards(self, kind: str) -> List[dict]:
        return self._db.query(
            "SELECT code, name, last, change_pct, amount FROM local_boards "
            "WHERE kind=? ORDER BY change_pct DESC", (kind,))

    # ------------------------------------------------------------------
    # 同步元数据（G1-5 增量同步 / 降级时间戳）
    # ------------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO local_sync_meta (key,value,updated_at) "
                "VALUES (?,?,?)", (key, str(value), _now()))

    def get_meta(self, key: str, default: Any = None) -> Any:
        rows = self._db.query("SELECT value FROM local_sync_meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def last_updated(self, table: str, where: str = "",
                     params: tuple = ()) -> Optional[str]:
        """表内最新 updated_at（G1-6 降级 as_of 的行级兜底：无同步元数据时，
        用数据实际落库时间标「数据截至」，保证 stale 必有 as_of——降级≠造假）。"""
        sql = f"SELECT MAX(updated_at) AS m FROM {table}"
        if where:
            sql += f" WHERE {where}"
        rows = self._db.query(sql, params)
        return rows[0]["m"] if rows and rows[0]["m"] else None

    # ------------------------------------------------------------------
    # 运维
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """各仓计数 + 最近同步时间（供运维页 / 冒烟验证）。"""
        out: Dict[str, Any] = {}
        for table in ("local_bars", "local_stock_list", "local_boards", "local_sync_meta"):
            rows = self._db.query(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(rows[0]["n"]) if rows else 0
        out["last_sync"] = self.get_meta("last_sync_at")   # 未同步过 → None
        return out

    def clear(self) -> None:
        """清空全部本地仓数据（运维/测试用；生产调用前需二次确认）。"""
        with self._lock:
            for table in ("local_bars", "local_stock_list", "local_boards", "local_sync_meta"):
                self._db.execute(f"DELETE FROM {table}")


# ---------------------------------------------------------------------------
# 单例（与 app.db 的 get_db() 绑定，保证与路由/迁移同库）
# ---------------------------------------------------------------------------
_store: Optional[LocalStore] = None
_store_lock = threading.Lock()


def get_store() -> LocalStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = LocalStore(get_db())
    return _store


__all__ = ["LocalStore", "get_store"]
