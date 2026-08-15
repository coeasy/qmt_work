"""SQLite 自动备份（防单点损坏）：启动时 + 周期性 + 关闭前各一次。

- 备份目标：<db 同目录>/backups/app.YYYYMMDD_HHMMSS.db（连同 -wal/-shm 一并复制，
  保证 WAL 模式下备份点一致）。
- 保留最近 `keep` 份（默认 10），超出自动清理最旧。
- 由 app.main 生命周期驱动：启动备份一次、后台周期任务、关闭前再备份一次。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger("qmt_work.db_backup")


class DBBackup:
    def __init__(self, db_path: Path, keep: int = 10,
                 backups_dir: Path | None = None, interval: float = 3600.0):
        self.db_path = Path(db_path)
        self.keep = max(1, int(keep))
        self.backups_dir = Path(backups_dir) if backups_dir else (self.db_path.parent / "backups")
        self.interval = max(60.0, float(interval))
        self._task: asyncio.Task | None = None
        self._stop = False

    # ---------------- 备份操作 ----------------
    def _size_str(self, p: Path) -> str:
        try:
            sz = p.stat().st_size
        except OSError:
            return "?"
        return f"{sz / 1024:.1f}KB" if sz < 1024 * 1024 else f"{sz / 1024 / 1024:.1f}MB"

    def backup_once(self, reason: str = "manual") -> str | None:
        """执行一次备份，返回目标路径；失败返回 None。"""
        if not self.db_path.exists():
            return None
        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            dst = self.backups_dir / f"app.{ts}.db"
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(self.db_path) + suffix)
                if src.exists():
                    shutil.copy2(src, str(dst) + suffix)
            self._prune()
            log.info("数据库备份完成（%s）：%s [%s]", reason, dst.name, self._size_str(dst))
            return str(dst)
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库备份失败（%s）：%s", reason, exc)
            return None

    def _prune(self) -> None:
        """保留最近 keep 份，清理其余。"""
        files = sorted(
            self.backups_dir.glob("app.*.db"),
            key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[self.keep:]:
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(f) + suffix)
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass

    def list_backups(self) -> list[dict]:
        """返回现有备份清单（含大小/时间），供运维接口展示。"""
        if not self.backups_dir.exists():
            return []
        out = []
        for f in sorted(self.backups_dir.glob("app.*.db"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                st = f.stat()
                out.append({
                    "name": f.name, "size": st.st_size,
                    "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                })
            except OSError:
                continue
        return out

    # ---------------- 周期任务 ----------------
    async def start(self) -> None:
        """启动后台周期备份任务（事件循环内）。"""
        self._stop = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            while not self._stop:
                await asyncio.sleep(self.interval)
                if self._stop:
                    break
                self.backup_once("periodic")
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("db backup loop error: %s", exc)

    async def stop(self) -> None:
        """停止周期任务，并在关闭前再备份一次。"""
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.backup_once("shutdown")
