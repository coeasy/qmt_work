"""V9 Phase 7（D-F）：Durable Schedule 存储与调度器。

- ``ScheduleStore``：schedules 表 CRUD（迁移 v24），next_run_at 落库 → 重启不丢相位；
- ``ScheduleRunner``：后台循环，到期触发 → 提交 JobRuntime；
  misfire 策略（P1-19/P1-21）：
    * ``catch_up``：错过的触发点逐个补跑（跨日 backfill，上限防雪崩）；
    * ``coalesce``：错过多次只跑一次（默认）；
    * ``skip``：错过即放弃，等下一个相位。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from app.runtime.cron import CronExpr

log = logging.getLogger("qmt_work.runtime.schedules")

#: catch_up 单次唤醒最多补跑数（防长停机后雪崩）
MAX_CATCHUP = 8


class ScheduleStore:
    """schedules 表持久化（幂等：不存在时由迁移 v24 创建）。"""

    def __init__(self, db):
        self._db = db

    def create(self, kind: str, cron: str, *, name: str = "",
               misfire_policy: str = "coalesce", enabled: bool = True,
               params: Optional[dict] = None,
               schedule_id: str = "") -> dict:
        expr = CronExpr.parse(cron)      # 提前校验
        if misfire_policy not in ("catch_up", "coalesce", "skip"):
            raise ValueError(f"invalid misfire_policy: {misfire_policy}")
        sid = schedule_id or f"sch-{uuid.uuid4().hex[:10]}"
        row = {
            "id": sid, "name": name or kind, "kind": kind, "cron": expr.raw,
            "timezone": "Asia/Shanghai", "enabled": 1 if enabled else 0,
            "misfire_policy": misfire_policy,
            "params_json": json.dumps(params or {}, ensure_ascii=False),
            "last_run_at": "", "next_run_at": _iso(expr.next_after(datetime.now())),
        }
        self._db.upsert("schedules", row)
        return self.get(sid)  # type: ignore[return-value]

    def get(self, sid: str) -> Optional[dict]:
        rows = self._db.query("SELECT * FROM schedules WHERE id=?", (sid,))
        return self._decorate(rows[0]) if rows else None

    def list(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM schedules"
        if enabled_only:
            sql += " WHERE enabled=1"
        rows = self._db.query(sql + " ORDER BY created_at")
        return [self._decorate(r) for r in rows]

    def update(self, sid: str, **fields) -> Optional[dict]:
        cur = self.get(sid)
        if cur is None:
            return None
        if "cron" in fields and fields["cron"]:
            CronExpr.parse(fields["cron"])            # 校验
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if "params" in fields:
            fields["params_json"] = json.dumps(fields.pop("params") or {},
                                                ensure_ascii=False)
        allowed = {"name", "cron", "enabled", "misfire_policy",
                   "params_json", "last_run_at", "next_run_at"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return cur
        sets["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._db.upsert("schedules", {**cur_raw(cur), **sets})
        return self.get(sid)

    def delete(self, sid: str) -> bool:
        cur = self.get(sid)
        if cur is None:
            return False
        self._db.execute("DELETE FROM schedules WHERE id=?", (sid,))
        return True

    def _decorate(self, row: dict) -> dict:
        row = dict(row)
        try:
            row["params"] = json.loads(row.get("params_json") or "{}")
        except (TypeError, ValueError):
            row["params"] = {}
        row["enabled"] = bool(row.get("enabled"))
        return row


def cur_raw(row: dict) -> dict:
    """落库形态（去掉 decorate 出来的派生键）。"""
    raw = {k: v for k, v in row.items() if k != "params"}
    raw["params_json"] = json.dumps(row.get("params") or {}, ensure_ascii=False)
    return raw


def _iso(dt: Optional[datetime]) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


class ScheduleRunner:
    """调度循环：每 tick 检查到期 schedule → 按 misfire 策略提交 JobRuntime。"""

    def __init__(self, store: ScheduleStore, job_runtime, *, tick_seconds: float = 30.0):
        self._store = store
        self._jobs = job_runtime
        self._tick = max(5.0, float(tick_seconds))
        self._task = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = _ensure_loop_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio_CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        log.info("schedule runner started (tick=%ss)", self._tick)
        while not self._stopping:
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("schedule tick failed: %s", exc)
            await _sleep(self._tick)

    # ------------------------------------------------------------------
    def tick_once(self, now: Optional[datetime] = None) -> list[str]:
        """一次调度检查（测试可直接调用）。返回本次提交的 job_id 列表。"""
        now = now or datetime.now()
        submitted: list[str] = []
        for sch in self._store.list(enabled_only=True):
            try:
                submitted.extend(self._fire(sch, now))
            except Exception as exc:  # noqa: BLE001
                log.warning("schedule %s fire failed: %s", sch["id"], exc)
        return submitted

    def _fire(self, sch: dict, now: datetime) -> list[str]:
        expr = CronExpr.parse(sch["cron"])
        policy = sch.get("misfire_policy") or "coalesce"
        last_s = sch.get("next_run_at") or ""
        # 无相位（新建/历史损坏）：重算下一相位，不立即触发
        if not last_s:
            self._store.update(sch["id"], next_run_at=_iso(expr.next_after(now)))
            return []
        try:
            due = datetime.fromisoformat(last_s)
        except ValueError:
            self._store.update(sch["id"], next_run_at=_iso(expr.next_after(now)))
            return []
        if due > now:
            return []                              # 未到期
        submitted: list[str] = []
        if policy == "skip":
            missed = [due]
        elif policy == "coalesce":
            missed = [due]                          # 错过多次合并为一次
        else:  # catch_up：逐个补跑（上限 MAX_CATCHUP 防雪崩）
            missed = []
            cur = due
            while cur <= now and len(missed) < MAX_CATCHUP:
                missed.append(cur)
                nxt = expr.next_after(cur)
                if nxt is None:
                    break
                cur = nxt
        for fire_at in missed:
            job_id = self._submit(sch, fire_at)
            if job_id:
                submitted.append(job_id)
        # 相位推进：以「当前时间」计算下一次（skip/coalesce），
        # catch_up 已把错过序列补完，同样从 now 起算下一相位。
        self._store.update(
            sch["id"], last_run_at=_iso(missed[-1]) if missed else "",
            next_run_at=_iso(expr.next_after(now)))
        return submitted

    def _submit(self, sch: dict, fire_at: datetime) -> str:
        from app.runtime.jobs import JobSpec
        from app.runtime.system_jobs import runner_for
        factory = runner_for(sch["kind"])
        params = dict(sch.get("params") or {})
        params.setdefault("_scheduled_at", fire_at.isoformat(timespec="seconds"))
        params.setdefault("_schedule_id", sch["id"])
        if factory is not None:
            runner = factory(params)
        else:
            # 非 system kind：按既有 JobRuntime runner 工厂约定（sync/screen/...）
            from app.runtime.jobs import runner_factory_for
            base = runner_factory_for(sch["kind"])
            runner = base(params) if base else _noop_runner
        return self._jobs.submit(JobSpec(
            kind=sch["kind"], name=sch.get("name") or sch["kind"],
            runner=runner, priority=3, params=params))


async def _noop_runner(job) -> dict:
    await job.report(100, "noop")
    return {"ok": True, "noop": True}


# 轻量别名（测试 monkeypatch 友好）
import asyncio as _asyncio  # noqa: E402
asyncio_CancelledError = _asyncio.CancelledError


async def _sleep(seconds: float) -> None:
    await _asyncio.sleep(seconds)


def _ensure_loop_task(coro):
    import asyncio
    return asyncio.ensure_future(coro)


__all__ = ["ScheduleStore", "ScheduleRunner", "MAX_CATCHUP"]
