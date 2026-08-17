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
import threading
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
        # 阶段 3：fsync 批量/后台化（见 append）。
        self._io_lock = threading.Lock()     # 保护 fh 的所有文件操作（写/刷/seek/truncate/fsync）
        self._fsync_evt = threading.Event()
        self._fsync_thread = threading.Thread(
            target=self._fsync_worker, daemon=True, name="wal-fsync")
        self._unfsynced = 0                  # 自上次 fsync 以来累计未刷行数
        self._last_fsync = 0.0
        self._fsync_interval = 0.2           # 最多攒 200ms
        self._fsync_count = 64               # 或攒满 64 条即刷
        self._corrupt_lines = 0              # 阶段 3：损坏行计数（读路径告警用）
        self._fsync_thread.start()

    def _fsync(self) -> None:
        """同步 fsync（close/checkpoint/后台 worker 共用，受 io_lock 保护）。"""
        with self._io_lock:
            try:
                os.fsync(self._fh.fileno())
            except (OSError, ValueError):
                pass

    def _fsync_worker(self) -> None:
        """后台线程：事件触发后批量执行一次 fsync，把 append 的 fsync 从热路径挪走。"""
        while not self._closed:
            self._fsync_evt.wait(timeout=0.5)
            self._fsync_evt.clear()
            if self._closed:
                return
            self._fsync()
            self._last_fsync = time.time()

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
        with self._io_lock:
            self._fh.write(line)
            self._fh.flush()   # 写入 OS page cache：进程崩溃不丢（仅断电丢失，靠批量 fsync 兜底）
        self._unfsynced += 1
        # 阶段 3：fsync 批量/后台化——不再每条都同步 fsync（高频下磁盘压力大）。
        # 攒满阈值或距上次超时后，置事件交给后台线程执行批量 fsync；
        # close()/checkpoint() 兜底同步 fsync，保证关键状态落盘。
        now = time.time()
        if now - self._last_fsync >= self._fsync_interval or self._unfsynced >= self._fsync_count:
            self._unfsynced = 0
            self._fsync_evt.set()
        self._maybe_checkpoint()

    def _maybe_checkpoint(self) -> None:
        """WAL 超过阈值时归档并截断，避免无限增长（A1）。"""
        try:
            if self.path.stat().st_size >= self._threshold:
                self.checkpoint()
        except Exception:  # noqa: BLE001
            pass

    def checkpoint(self) -> None:
        """将当前 WAL 归档到 .snapshot.jsonl（覆盖写 + 原子 rename），并截断主 WAL。

        阶段 0-C（F3）：原实现用 ``open(snap, "a")`` 追加（快照无限增长），且「写 snapshot
        成功但 truncate 前崩溃」会让已归档记录仍留在主 WAL → 双重复放。现改为：
        先读全主 WAL → 写临时文件并 fsync → ``os.replace`` 原子覆盖旧快照 → 再 truncate。
        ``replay()``/``all_records()`` 另按记录内容去重，彻底消除双重复放窗口。
        """
        snap = self.path.with_suffix(".snapshot.jsonl")
        tmp = self.path.with_suffix(".snapshot.jsonl.tmp")
        # 注意：绝不能 `with self._fh:` —— File 作为上下文管理器退出时会 close() 句柄，
        # checkpoint 后主 WAL 句柄即失效，后续 append 抛「I/O operation on closed file」。
        with self._io_lock:
            self._fh.flush()
            self._fh.seek(0)
            lines = self._fh.readlines()
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, snap)  # 原子覆盖旧快照（进程崩溃不会留下半份快照）
        with self._io_lock:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.flush()
        self._fsync()
        log.info("wal checkpoint: %d lines archived to %s", len(lines), snap)

    @staticmethod
    def _rec_key(rec: dict) -> tuple:
        """记录去重键：内容级指纹（ts/op/entity/id + 规范化 payload）。"""
        return (rec.get("ts"), rec.get("op"), rec.get("entity"),
                str(rec.get("entity_id") or ""),
                json.dumps(rec.get("payload") or {}, sort_keys=True, ensure_ascii=False))

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
                # 阶段 3：损坏行记录并告警，而非静默跳过——损坏往往是崩溃残留/磁盘问题前兆，
                # 静默跳过会让运营对「丢记录」毫无感知。
                self._corrupt_lines += 1
                if self._corrupt_lines <= 10:
                    log.warning("WAL 损坏行（已跳过 %d 行）：%.120r", self._corrupt_lines, line)
                elif self._corrupt_lines == 11:
                    log.warning("WAL 损坏行过多（>10），后续不再逐条告警")

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
        """重放：优先快照（历史归档），再主 WAL 增量（A1 轮转）。

        阶段 0-C（F3）：按记录内容去重——「写快照成功但 truncate 前崩溃」时主 WAL 仍含
        已归档记录，快照重放后跳过主 WAL 中的重复，避免同一记录被应用两次。
        阶段 3：返回损坏行计数（corrupt），便于监控 WAL 完整性。
        """
        handlers = handlers or self._replay_handlers
        summary: dict[str, int] = {}
        snap = self.path.with_suffix(".snapshot.jsonl")
        seen: set = set()
        if snap.exists():
            try:
                with open(snap, "r", encoding="utf-8") as f:
                    for rec in self._iter_records(f):
                        seen.add(self._rec_key(rec))
                        self._apply(rec, handlers, summary)
            except Exception as exc:  # noqa: BLE001
                log.warning("wal snapshot replay failed: %s", exc)
        for rec in self._iter_records(self._fh):
            if self._rec_key(rec) in seen:
                continue  # 双重复放窗口：主 WAL 仍含已归档记录
            self._apply(rec, handlers, summary)
        return {"replayed": sum(summary.values()), "by_entity": summary,
                "corrupt": self._corrupt_lines}

    def snapshot(self) -> list[dict]:
        """读取全部记录，供外部状态机用于启动恢复。"""
        out = []
        for rec in self._iter_records(self._fh):
            out.append(rec)
        return out

    def all_records(self) -> list[dict]:
        """返回主 WAL + 快照归档的全部记录（A2 对账用）。

        阶段 0-C（F3）：同样按记录内容去重，避免「checkpoint 崩溃窗口」下同一记录
        在快照与主 WAL 中重复出现，导致对账/重放重复处理。
        """
        out: list[dict] = []
        snap = self.path.with_suffix(".snapshot.jsonl")
        seen: set = set()
        if snap.exists():
            try:
                with open(snap, "r", encoding="utf-8") as f:
                    for rec in self._iter_records(f):
                        seen.add(self._rec_key(rec))
                        out.append(rec)
            except Exception:  # noqa: BLE001
                pass
        for rec in self._iter_records(self._fh):
            if self._rec_key(rec) in seen:
                continue
            out.append(rec)
        return out

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._fsync()   # 阶段 3：兜底同步 fsync 未刷数据（关闭前关键状态必须落盘）
            self._fh.close()

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
