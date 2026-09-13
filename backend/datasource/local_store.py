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
from datasource.models import Bar, BoardItem, StockInfo

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
    ) -> Dict[str, List[Bar]]:
        """批量取多标的 K 线：单条 ``WHERE code IN (...)`` + 窗口函数选主，
        分块 SQL 取回整批，替代逐只 ``get_bars`` 的 N 次循环（P3 / Phase B）。

        设计目标：把「逐只循环 = N 次 SQL」降到「O(块数) 次 SQL」
        （约 5000 只 → 6 块）。SQLite 变量上限安全分块（每块 900 只）。
        Canonical 选主逻辑与 ``get_bars`` 完全一致（质量状态 + 具名 provider 优先），
        每块内对每标的最近 ``limit`` 根按时间降序取头、升序返回。
        """
        if not codes:
            return {}
        seen: "dict[str, None]" = {}
        for c in codes:
            seen[str(c)] = None
        uniq = list(seen.keys())
        out: Dict[str, List[Bar]] = {c: [] for c in uniq}

        _CHUNK = 900  # 低于 SQLite 默认变量上限，避免 "too many SQL variables"
        for i in range(0, len(uniq), _CHUNK):
            chunk = uniq[i:i + _CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            inner = (
                "SELECT code, dt, open, high, low, close, volume, amount, provider_id, "
                "quality_state, ROW_NUMBER() OVER ("
                "PARTITION BY code, dt ORDER BY "
                "CASE quality_state WHEN 'validated' THEN 0 WHEN 'complete' THEN 1 "
                "WHEN 'match' THEN 2 ELSE 3 END, (provider_id = '') DESC, provider_id"
                ") AS rn FROM local_bars "
                f"WHERE code IN ({placeholders}) AND period=? AND adjust=?"
            )
            if limit and limit > 0:
                outer = (
                    "SELECT code, dt AS time, open, high, low, close, volume, amount "
                    "FROM (SELECT code, dt, open, high, low, close, volume, amount, "
                    "ROW_NUMBER() OVER (PARTITION BY code ORDER BY dt DESC) AS rk "
                    f"FROM ({inner}) WHERE rn=1) WHERE rk<=? ORDER BY code, time ASC"
                )
                params: List[Any] = list(chunk) + [period, adjust, int(limit)]
            else:
                outer = (
                    "SELECT code, dt AS time, open, high, low, close, volume, amount "
                    f"FROM ({inner}) WHERE rn=1 ORDER BY code, time ASC"
                )
                params = list(chunk) + [period, adjust]
            rows = self._db.execute(outer, tuple(params)).fetchall()
            for r in rows:
                d = dict(r)
                out.setdefault(d["code"], []).append(Bar.model_validate(d))
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
