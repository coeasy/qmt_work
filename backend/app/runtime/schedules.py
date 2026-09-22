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
from zoneinfo import ZoneInfo

from app.runtime.cron import CronExpr
from core.clock import local_now, now_iso, parse_iso
from core.clock import to_iso as _iso  # 唯一实现在 core.clock（V11 R8 收敛）

log = logging.getLogger("qmt_work.runtime.schedules")

#: catch_up 单次唤醒最多补跑数（防长停机后雪崩）
MAX_CATCHUP = 8


def _tzinfo(name: str):
    """解析时区；未知/空返回 ``None``（调用方按本地时间解释，**绝不**因此崩调度）。"""
    name = str(name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 未知时区名 / 无 tzdata
        log.warning("调度时区 %r 无法解析（%s），按本地时间解释 cron", name, exc)
        return None


def next_after_tz(expr: CronExpr, dt: datetime, tzname: str = "") -> datetime:
    """按 ``tzname`` 解释 cron 的「几点几分」，但**返回本地 naive 时间**。

    ## 为什么时区此前是装饰性的

    ``schedules.timezone`` 列一直写着 ``Asia/Shanghai``，但全代码**从不读取**它
    ⇒ 调度在任意时区的机器上都会按**机器本地时间**触发。对国内用户看不出问题
    （本地就是上海），但把客户端带到东京/纽约就变成「收盘后同步」跑在当地的 16:00。

    ## 为什么返回本地 naive 而不是带偏移

    全项目时间戳统一是 **naive 本地**（``core/clock.local_now`` / ``parse_iso``
    都是这个口径）。若这里存一个带 ``+09:00`` 的字符串，其余比较点
    （``parse_iso(next_run_at)`` vs ``local_now()``）会按本地去读 ⇒ 偏移被丢弃，
    触发时间整整差一个小时，而且只在跨时区机器上出现 —— 最难查的那类问题。
    所以：**解释**用时区，**存储**用本地。
    """
    tz = _tzinfo(tzname)
    if tz is None:
        return expr.next_after(dt)
    local_tz = dt.astimezone().tzinfo or tz        # naive → 认定为本机时区
    wall = dt.replace(tzinfo=local_tz).astimezone(tz)
    nxt = expr.next_after(wall)
    if nxt is None:
        return nxt
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(local_tz).replace(tzinfo=None)


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
        tzname = "Asia/Shanghai"
        # V11 R8：**显式**写 created_at/updated_at。
        # 旧写法省略这两列 → 走建表时的 DEFAULT ``datetime('now','localtime')``
        # （产出 "2026-09-16 18:57:49"，**空格分隔**），而 update() 写的是
        # "2026-09-16T18:57:49"（**T 分隔**）→ **同一列两种形状**。显式写即让
        # Python 成为唯一真源，DEFAULT 永不生效（不改表结构，零迁移风险）。
        row = {
            "id": sid, "name": name or kind, "kind": kind, "cron": expr.raw,
            "timezone": tzname, "enabled": 1 if enabled else 0,
            "misfire_policy": misfire_policy,
            "params_json": json.dumps(params or {}, ensure_ascii=False),
            # ★ 按 tzname 解释 cron —— 此前这里写死 expr.next_after(local_now())，
            #   timezone 列于是成了纯装饰（裁定：保留并让它生效）。
            "last_run_at": "", "next_run_at": _iso(next_after_tz(expr, local_now(), tzname)),
            "created_at": now_iso(), "updated_at": now_iso(),
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
        new_tz = str(fields.get("timezone") or "").strip()
        if new_tz and _tzinfo(new_tz) is None:
            raise ValueError(f"未知时区：{new_tz}")
        new_cron = str(fields.get("cron") or "").strip()
        if new_cron:
            CronExpr.parse(new_cron)                  # 校验
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if "params" in fields:
            fields["params_json"] = json.dumps(fields.pop("params") or {},
                                                ensure_ascii=False)
        # ★ cron 或 timezone 任一变化都必须**立刻重算相位**。
        #   保留旧的 next_run_at 会让新时间在旧相位到达前一次都不触发 ——
        #   界面上表现为「改了触发时间没反应」，而调度本身看起来完全正常。
        if new_cron or new_tz:
            expr = CronExpr.parse(new_cron or str(cur.get("cron") or ""))
            fields["next_run_at"] = _iso(next_after_tz(
                expr, local_now(), new_tz or str(cur.get("timezone") or "")))
        allowed = {"name", "cron", "enabled", "misfire_policy", "timezone",
                   "params_json", "last_run_at", "next_run_at"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return cur
        sets["updated_at"] = now_iso()
        self._db.upsert("schedules", {**cur_raw(cur), **sets})
        return self.get(sid)

    def delete(self, sid: str) -> bool:
        cur = self.get(sid)
        if cur is None:
            return False
        self._db.execute("DELETE FROM schedules WHERE id=?", (sid,))
        return True

    def normalize_phases(self) -> list[str]:
        """把「与自己的 cron 对不上」的 ``next_run_at`` 清空，交给调度器重算。

        ## 为什么必须有这一步

        ``next_run_at`` 是**相位**（下次触发的时刻），它由 ``cron`` 派生。任何直接
        改 ``cron`` 而不同步重算相位的路径都会留下一个「配置说 16:00、相位却指着
        15:30」的调度 —— 而 ``ScheduleRunner._fire`` 是**按相位**触发的，于是它真的
        会在 15:30 跑，配置看起来完全正常。

        这不是假设：迁移 v27 把默认同步从 ``30 15`` 改到 ``0 16``、选股从 ``0 16``
        改到 ``15 16``，但 ``UPDATE schedules SET cron=...`` 没有重算 ``next_run_at``。
        实测（2026-09-20 真实库）：同步相位仍停在 15:30、选股相位停在 16:00 ——
        等于把「先同步、再选股」的顺序**倒着**执行一次，选股会读到半更新的日线，
        正是 v27 想避免的事。而这两条链路的 ``last_run_at`` 都是空，说明它一次都
        还没跑过，**下个工作日就会按错的时间跑**。

        ## 判据

        ``next_run_at`` 是合法相位的充要条件：把它减 1 秒再求「下一次触发」，
        应当**回到它自己**（相位是它自己之前最近的一个 cron 触发点）。
        与 cron 无关的、落在过去的、星期几不对的值都会被这条判据识破。

        清空（而非直接重算）是刻意的：``_fire`` 对空相位的行为是「重算下一相位、
        **不立即触发**」，语义正好是「按当前配置重新对表」。同时保留了过去相位的
        误触发能力 —— 已到期该补跑的调度仍会正常补跑。

        返回被重置的调度 id 列表。
        """
        from datetime import timedelta

        fixed: list[str] = []
        for sch in self.list():
            sid = str(sch.get("id") or "")
            phase_s = str(sch.get("next_run_at") or "").strip()
            if not phase_s:
                continue                      # 无相位：调度器自己会重算
            try:
                expr = CronExpr.parse(str(sch.get("cron") or ""))
            except Exception:                 # noqa: BLE001 cron 本身非法 → 不动它
                continue
            tzname = str(sch.get("timezone") or "")
            phase = parse_iso(phase_s)
            if phase is None:
                self.update(sid, next_run_at="")
                fixed.append(sid)
                continue
            probe = next_after_tz(expr, phase - timedelta(seconds=1), tzname)
            if probe is None or probe != phase:
                log.warning(
                    "调度 %s(%s) 相位与 cron 不一致：next_run_at=%s 但 cron=%s 的下一次是 %s"
                    " —— 已清空相位，按当前 cron 重新对表",
                    sid, sch.get("name") or sch.get("kind"), phase_s,
                    sch.get("cron"), probe.isoformat(timespec="seconds") if probe else "None")
                self.update(sid, next_run_at="")
                fixed.append(sid)
        return fixed

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
        now = now or local_now()
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
        # ★ cron 的「几点」按本调度自己的时区解释（此前从不读取 ⇒ 装饰性字段）
        tzname = str(sch.get("timezone") or "")
        last_s = sch.get("next_run_at") or ""
        # 无相位（新建/历史损坏）：重算下一相位，不立即触发
        if not last_s:
            self._store.update(sch["id"], next_run_at=_iso(next_after_tz(expr, now, tzname)))
            return []
        # V11 R8：经 core.clock.parse_iso 宽容解析（裸值/空格分隔/带偏移均可），
        # 并与 tick_once 传入的 naive ``now`` 同口径 —— 旧写法 fromisoformat 遇到
        # 带偏移的历史值会在 ``due > now`` 处抛 TypeError。
        due = parse_iso(last_s)
        if due is None:
            self._store.update(sch["id"], next_run_at=_iso(next_after_tz(expr, now, tzname)))
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
                nxt = next_after_tz(expr, cur, tzname)
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
            next_run_at=_iso(next_after_tz(expr, now, tzname)))
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
