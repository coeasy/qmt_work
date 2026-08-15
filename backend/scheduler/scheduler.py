"""TaskScheduler：轻量定时任务调度器（纯 Python / asyncio，逻辑与券商无关）。

调度规格 `cron` 同时支持两种写法：
  1) 标准 5 段 cron 字符串，如 ``"0 9 * * *"`` / ``"*/5 * * * *"``
     （支持 ``*``、具体值、区间 ``a-b``、步长 ``*/N``、逗号列表）。
  2) 简单规格 dict（或 JSON 字符串）：
     - ``{"every_minutes": N}``   每隔 N 分钟
     - ``{"daily_at": "09:30"}`` 每天固定时刻

动作（action）通过可插拔的 ``executor`` 执行，便于测试：
  - ``webhook``：POST payload 到 ``payload["url"]``（默认用 stdlib urllib）。
  - ``backtest``：仅记录（真实接线由 integrator 完成）。
  - ``shutdown`` / ``restart``：置位对应请求标志位（由 integrator 真正终止进程）。

全部为真实逻辑，不返回任何假数据。
"""
import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta

log = logging.getLogger("qmt_work.scheduler")

# cron 各字段取值范围（minute/hour/day/month/dow）
_FIELD_RANGE = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
# dow 在 cron 中 0=周日；Python weekday() 中 0=周一。统一换算。
_EPOCH = datetime(1970, 1, 1)


class TaskScheduler:
    """定时任务调度器：基于 SQLite 持久化，纯逻辑可单测。"""

    def __init__(self):
        self.db = None
        self._task = None
        self._running = False
        self._in_run = False          # 防重入（避免重叠执行）
        self.shutdown_requested = False
        self.restart_requested = False
        self.executor = self._default_executor
        self._lock = threading.Lock()

    # ---------------- 持久化初始化 ----------------
    def init(self, db) -> "TaskScheduler":
        """绑定 DB 并自建 scheduled_tasks 表（幂等）。"""
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_tasks ("
            "id TEXT PRIMARY KEY, name TEXT, cron TEXT, action TEXT, "
            "payload_json TEXT, enabled INT, last_run TEXT, next_run TEXT, "
            "status TEXT, created_at TEXT)"
        )
        return self

    # ---------------- 表 <-> 记录 ----------------
    @staticmethod
    def _normalize_row(row) -> dict:
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload_json") or "{}")
        except (ValueError, TypeError):
            d["payload"] = {}
        d["enabled"] = bool(d.get("enabled"))
        return d

    def list_tasks(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM scheduled_tasks ORDER BY created_at ASC")
        return [self._normalize_row(r) for r in rows]

    def get_task(self, task_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM scheduled_tasks WHERE id=?", (task_id,))
        return self._normalize_row(row) if row else None

    def remove_task(self, task_id: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
        return cur.rowcount > 0

    def enable_task(self, task_id: str, enabled: bool = True) -> dict | None:
        cur = self.db.execute(
            "UPDATE scheduled_tasks SET enabled=? WHERE id=?",
            (1 if enabled else 0, task_id))
        if cur.rowcount == 0:
            return None
        return self.get_task(task_id)

    # ---------------- 新增任务 ----------------
    def add_task(self, name: str, cron, action: str, payload=None,
                 enabled: bool = True, now: datetime | None = None) -> dict:
        """持久化一条定时任务，立即计算并写入 next_run。返回记录 dict。"""
        now = now or datetime.now()
        if not isinstance(cron, str):
            cron_str = json.dumps(cron, ensure_ascii=False)
        else:
            cron_str = cron
        nxt = self.compute_next_run(cron_str, now)
        rec = {
            "id": uuid.uuid4().hex,
            "name": name,
            "cron": cron_str,
            "action": action,
            "payload_json": json.dumps(payload or {}, ensure_ascii=False),
            "enabled": 1 if enabled else 0,
            "last_run": None,
            "next_run": nxt.isoformat(),
            "status": "idle",
            "created_at": now.isoformat(),
        }
        self.db.insert("scheduled_tasks", rec)
        return self.get_task(rec["id"])

    # ---------------- 调度规格解析 ----------------
    @staticmethod
    def _parse_field(field: str, lo: int, hi: int) -> set[int]:
        """解析单个 cron 字段：支持 * / a-b / */N / 逗号列表。"""
        out: set[int] = set()
        for part in field.split(","):
            part = part.strip()
            if not part:
                continue
            if "/" in part:
                base, step_s = part.split("/", 1)
                step = int(step_s)
                start = lo if base.strip() == "*" else int(base)
                out.update(range(start, hi + 1, step))
            elif "-" in part:
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            elif part == "*":
                out.update(range(lo, hi + 1))
            else:
                out.add(int(part))
        return out

    @classmethod
    def _parse_cron(cls, cron: str) -> tuple[set, set, set, set, set]:
        """解析 5 段 cron 字符串，返回 (分钟, 小时, 日, 月, 星期) 集合。"""
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError(f"非法 cron 表达式（需 5 段）: {cron!r}")
        mins = cls._parse_field(parts[0], *_FIELD_RANGE[0])
        hours = cls._parse_field(parts[1], *_FIELD_RANGE[1])
        days = cls._parse_field(parts[2], *_FIELD_RANGE[2])
        months = cls._parse_field(parts[3], *_FIELD_RANGE[3])
        # cron dow: 0=周日..6=周六 -> Python weekday 0=周一..6=周日
        dows_cron = cls._parse_field(parts[4], *_FIELD_RANGE[4])
        dows = {(d + 6) % 7 for d in dows_cron}
        return mins, hours, days, months, dows

    @staticmethod
    def _floor_minute(dt: datetime) -> datetime:
        return dt.replace(second=0, microsecond=0)

    @classmethod
    def _next_cron(cls, fields, after: datetime) -> datetime:
        mins, hours, days, months, dows = fields
        t = cls._floor_minute(after) + timedelta(minutes=1)
        guard = t + timedelta(days=366 * 4)
        while t <= guard:
            if t.month not in months:
                if t.month == 12:
                    t = t.replace(year=t.year + 1, month=1, day=1,
                                  hour=0, minute=0, second=0, microsecond=0)
                else:
                    t = t.replace(month=t.month + 1, day=1,
                                  hour=0, minute=0, second=0, microsecond=0)
                continue
            if t.day not in days or t.weekday() not in dows:
                t = (t + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                continue
            if t.hour not in hours:
                t = (t + timedelta(hours=1)).replace(
                    minute=0, second=0, microsecond=0)
                continue
            if t.minute not in mins:
                t += timedelta(minutes=1)
                continue
            return t
        raise RuntimeError(f"无法在 4 年内找到匹配的 cron 时间: {fields}")

    @classmethod
    def compute_next_run(cls, spec, after: datetime) -> datetime:
        """根据调度规格计算 after 之后的下一次执行时间（naive datetime）。"""
        spec_dict = None
        if isinstance(spec, dict):
            spec_dict = spec
        elif isinstance(spec, str):
            s = spec.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    spec_dict = json.loads(s)
                except (ValueError, TypeError):
                    spec_dict = None
        if isinstance(spec_dict, dict):
            if "every_minutes" in spec_dict:
                n = int(spec_dict["every_minutes"])
                base = cls._floor_minute(after)
                elapsed = int((base - _EPOCH).total_seconds() // 60)
                next_min = ((elapsed // n) + 1) * n
                return _EPOCH + timedelta(minutes=next_min)
            if "daily_at" in spec_dict:
                h, m = map(int, str(spec_dict["daily_at"]).split(":"))
                cand = after.replace(hour=h, minute=m, second=0, microsecond=0)
                if cand <= after:
                    cand += timedelta(days=1)
                return cand
            raise ValueError(f"不支持的简单调度规格: {spec_dict!r}")
        # 否则按 cron 字符串解析
        if not isinstance(spec, str):
            raise ValueError(f"无法识别的调度规格: {spec!r}")
        return cls._next_cron(cls._parse_cron(spec), after)

    # ---------------- 执行 ----------------
    def _default_executor(self, action: str, payload: dict):
        """默认执行器（可被测试替换为自定义 callable）。"""
        if action == "webhook":
            url = (payload or {}).get("url")
            if url:
                self._post_webhook(url, payload)
        elif action == "backtest":
            log.info("scheduled backtest submit: %s", payload)
        elif action in ("shutdown", "restart"):
            log.info("scheduled %s requested", action)
        return None

    @staticmethod
    def _post_webhook(url: str, payload: dict) -> None:
        """向 webhook URL POST payload（优先 httpx，回退 urllib）。"""
        body = json.dumps(payload.get("body", payload), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        try:
            import httpx  # noqa
            with httpx.Client(timeout=10.0) as client:
                client.post(url, content=body, headers=headers)
            return
        except ImportError:
            pass
        import urllib.request
        req = urllib.request.Request(  # noqa: S310
            url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10.0) as resp:  # noqa: S310
            _ = resp.read()

    def run_due(self, now: datetime | None = None) -> list[dict]:
        """执行所有到点的启用任务，并重算 next_run。返回本次执行结果列表。"""
        if self._in_run:
            return []
        self._in_run = True
        try:
            now = now or datetime.now()
            due = [
                t for t in self.list_tasks()
                if t["enabled"] and t["next_run"]
                and datetime.fromisoformat(t["next_run"]) <= now
            ]
            results = []
            for t in due:
                action = t["action"]
                payload = t.get("payload") or {}
                # 置位停机/重启标志（不在此终止进程，交给 integrator）
                if action == "shutdown":
                    self.request_shutdown()
                elif action == "restart":
                    self.request_restart()
                status = "ok"
                result = None
                try:
                    result = self.executor(action, payload)
                except Exception as exc:  # noqa: BLE001
                    status = "error"
                    result = {"error": str(exc)}
                    log.warning("task %s (%s) 执行失败: %s", t["id"], action, exc)
                nxt = self.compute_next_run(t["cron"], now)
                self.db.execute(
                    "UPDATE scheduled_tasks SET last_run=?, next_run=?, status=? "
                    "WHERE id=?",
                    (now.isoformat(), nxt.isoformat(), status, t["id"]))
                results.append({
                    "id": t["id"], "action": action,
                    "result": result, "next_run": nxt.isoformat(),
                })
            return results
        finally:
            self._in_run = False

    # ---------------- 停机/重启请求标志 ----------------
    def request_shutdown(self) -> None:
        self.shutdown_requested = True

    def request_restart(self) -> None:
        self.restart_requested = True

    def clear_flags(self) -> None:
        self.shutdown_requested = False
        self.restart_requested = False

    # ---------------- 异步循环 ----------------
    async def start(self, interval: float = 30.0) -> None:
        """启动后台轮询循环（防重叠由 run_due 内部标志保证）。"""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.get_event_loop().create_task(self._loop(interval))

    async def _loop(self, interval: float) -> None:
        while self._running:
            try:
                self.run_due()
            except Exception as exc:  # noqa: BLE001
                log.warning("scheduler loop error: %s", exc)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
