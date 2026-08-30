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

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from app.db import DB, get_db
from app.datasource.models import Bar, BoardItem, StockInfo

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
    # K 线（主键 code/period/adjust/dt）
    # ------------------------------------------------------------------
    def upsert_bars(
        self,
        code: str,
        bars: List[Union[Bar, dict]],
        period: str = "1d",
        adjust: str = "",
    ) -> int:
        """批量写入 / 覆盖 K 线（INSERT OR REPLACE，同 (code,period,adjust,dt) 幂等）。"""
        rows: List[tuple] = []
        for b in bars:
            d = b.model_dump() if isinstance(b, Bar) else dict(b)
            rows.append((
                code, period, adjust, str(d.get("time", "")),
                _num(d.get("open")), _num(d.get("high")), _num(d.get("low")),
                _num(d.get("close")), _num(d.get("volume")), _num(d.get("amount")),
                _now(),
            ))
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO local_bars "
                "(code,period,adjust,dt,open,high,low,close,volume,amount,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
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
        """按时间升序取本地 K 线 → 标准模型 ``Bar`` 列表（可只传 limit 取最近 N 根）。"""
        sql = ("SELECT dt AS time, open, high, low, close, volume, amount "
               "FROM local_bars WHERE code=? AND period=? AND adjust=?")
        params: List[Any] = [code, period, adjust]
        if start:
            sql += " AND dt >= ?"
            params.append(start)
        if end:
            sql += " AND dt <= ?"
            params.append(end)
        sql += " ORDER BY dt ASC"
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._db.query(sql, tuple(params))
        return [Bar.model_validate(r) for r in rows]

    def count_bars(self, code: str, period: str = "1d", adjust: str = "") -> int:
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM local_bars "
            "WHERE code=? AND period=? AND adjust=?", (code, period, adjust))
        return int(rows[0]["n"]) if rows else 0

    def latest_dt(self, code: str, period: str = "1d", adjust: str = "") -> Optional[str]:
        """最近一根 K 线日期（G1-5 增量同步的续传游标）。"""
        rows = self._db.query(
            "SELECT MAX(dt) AS m FROM local_bars "
            "WHERE code=? AND period=? AND adjust=?", (code, period, adjust))
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
