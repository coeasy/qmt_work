"""WAL（Write-Ahead Log）与启动恢复。

关键状态变更先写 WAL，再修改内存状态；进程崩溃后 lifespan 启动时重放，
恢复委托、算法单、条件单、涨停单等运行态。

文件格式：append-only JSONL，每行一个 dict：
{
    "ts": 1712345678.123,     # 时间戳
    "op": "submit",           # create/update/cancel/fill/error
    "entity": "order",        # order/algo/condition/limitup/rebalance
    "entity_id": "...",
    "payload": {...}
}
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("qmt_work.wal")


class WAL:
    def __init__(self, path: Path | str, replay_handlers: dict[str, Callable[[dict], Any]] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(".lock")
        self._replay_handlers = replay_handlers or {}
        self._closed = False
        self._fh = open(self.path, "a+", encoding="utf-8")
        self._threshold = 10 * 1024 * 1024  # 10MB 触发 checkpoint（A1）

    def append(self, op: str, entity: str, entity_id: str, payload: dict) -> None:
        if self._closed:
            return
        record = {
            "ts": time.time(),
            "op": op,
            "entity": entity,
            "entity_id": entity_id,
            "payload": payload,
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        self._fh.write(line)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._maybe_checkpoint()

    def _maybe_checkpoint(self) -> None:
        """WAL 超过阈值时归档并截断，避免无限增长（A1）。"""
        try:
            if self.path.stat().st_size >= self._threshold:
                self.checkpoint()
        except Exception:  # noqa: BLE001
            pass

    def checkpoint(self) -> None:
        """将当前 WAL 内容归档到 .snapshot.jsonl，并截断主 WAL。"""
        snap = self.path.with_suffix(".snapshot.jsonl")
        size = self.path.stat().st_size
        with open(snap, "a", encoding="utf-8") as f:
            self._fh.flush()
            self._fh.seek(0)
            for line in self._fh:
                f.write(line)
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.flush()
        log.info("wal checkpoint: %d bytes archived to %s", size, snap)

    def _iter_records(self, fh):
        fh.flush()
        fh.seek(0)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    def _apply(self, rec: dict, handlers: dict, summary: dict) -> None:
        entity = rec.get("entity", "unknown")
        summary[entity] = summary.get(entity, 0) + 1
        h = handlers.get(entity)
        if h:
            try:
                h(rec)
            except Exception:  # noqa: BLE001
                pass

    def replay(self, handlers: dict[str, Callable[[dict], Any]] | None = None) -> dict:
        """重放：优先快照（历史归档），再主 WAL 增量（A1 轮转）。"""
        handlers = handlers or self._replay_handlers
        summary: dict[str, int] = {}
        snap = self.path.with_suffix(".snapshot.jsonl")
        if snap.exists():
            try:
                with open(snap, "r", encoding="utf-8") as f:
                    for rec in self._iter_records(f):
                        self._apply(rec, handlers, summary)
            except Exception as exc:  # noqa: BLE001
                log.warning("wal snapshot replay failed: %s", exc)
        for rec in self._iter_records(self._fh):
            self._apply(rec, handlers, summary)
        return {"replayed": sum(summary.values()), "by_entity": summary}

    def snapshot(self) -> list[dict]:
        """读取全部记录，供外部状态机用于启动恢复。"""
        self._fh.flush()
        self._fh.seek(0)
        out = []
        for line in self._fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def all_records(self) -> list[dict]:
        """返回主 WAL + 快照归档的全部记录（A2 对账用）。"""
        out: list[dict] = []
        snap = self.path.with_suffix(".snapshot.jsonl")
        if snap.exists():
            try:
                with open(snap, "r", encoding="utf-8") as f:
                    for rec in self._iter_records(f):
                        out.append(rec)
            except Exception:  # noqa: BLE001
                pass
        out.extend(self._iter_records(self._fh))
        return out

    def close(self) -> None:
        if not self._closed:
            self._fh.close()
            self._closed = True

    def __enter__(self) -> WAL:
        return self

    def __exit__(self, *args) -> None:
        self.close()


class WALRecoverable:
    """混入基类：任何需要 WAL 恢复的实体状态机可继承，自动 append。"""

    def __init__(self, wal: WAL | None, entity: str):
        self.wal = wal
        self.entity_type = entity

    def wal_append(self, op: str, entity_id: str, payload: dict):
        if self.wal is not None:
            self.wal.append(op, self.entity_type, entity_id, payload)
