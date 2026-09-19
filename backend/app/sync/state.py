"""离线同步状态落库（``sync_state`` 表）—— 让「上次同步跑成什么样」重启后还在。

## 为什么必须落库

行情定时刷新的结果此前只写在进程内存里（``AppContext._market_sync_last``）：

- **重启即丢**：客户端一重启，「上次同步什么时候跑的、跑了几只、成功多少」全部归零，
  界面只能显示「未知」—— 而用户正是靠这条信息判断「今天的数据到底同步了没有」；
- **崩溃即丢**：刷新过程中崩了，连「跑到一半」这件事都看不到。

## 与 `screen_runs` 的关系

同一个设计原则：**流程的每一次运行都要留下痕迹**，尤其是失败与部分成功的那些。
只记录成功的运行，等于把「没跑」和「跑了但失败」混为一谈。

## 表结构

复用既有的 ``sync_state`` 表（``stream`` 唯一键），并扩展 ``detail_json`` 列
（见 ``core/db_migrations.py`` 的 ``EXTRA_COLUMNS``）承载完整结果。
"""
from __future__ import annotations

import json
import logging

import core.db as db_mod
from core.clock import now_iso

log = logging.getLogger("qmt_work.sync.state")

#: 行情定时刷新（热窗口）的流名
STREAM_MARKET_SYNC = "market.sync"
#: 全量/增量日线同步（``system.sync_bars``）的流名
STREAM_SYNC_BARS = "sync.bars"


def record_run(stream: str, *, status: str = "ok", detail: dict | None = None,
               last_ts: str = "") -> dict:
    """记录一次同步运行。**失败也要调用**（``status`` 传 ``error``/``partial``）。

    返回写回的记录（便于调用方直接回显）。落库失败只记日志，不抛 ——
    状态记录是观测增强项，不该把同步本身带崩。
    """
    payload = dict(detail or {})
    payload.setdefault("status", status)
    payload.setdefault("at", now_iso())
    ts = last_ts or str(payload.get("at") or now_iso())
    try:
        db_mod.get_db().execute(
            "INSERT INTO sync_state (stream, last_seq, last_ts, status, detail_json)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(stream) DO UPDATE SET"
            "   last_ts=excluded.last_ts, status=excluded.status,"
            "   detail_json=excluded.detail_json",
            (stream, 0, ts, status, json.dumps(payload, ensure_ascii=False)),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("sync_state 落库失败（已降级，不影响同步）：%s", exc)
        return {"stream": stream, "status": status, "detail": payload, "persisted": False,
                "error": str(exc)}
    return {"stream": stream, "status": status, "detail": payload, "persisted": True,
            "error": ""}


def last_run(stream: str) -> dict | None:
    """读最近一次该流的运行记录；没有记录返回 ``None``（前端显示「尚无记录」）。"""
    try:
        row = db_mod.get_db().query_one(
            "SELECT stream, last_ts, status, detail_json FROM sync_state WHERE stream = ?",
            (stream,))
    except Exception as exc:  # noqa: BLE001
        log.debug("sync_state 读取失败：%s", exc)
        return None
    if not row:
        return None
    try:
        detail = json.loads(row.get("detail_json") or "{}")
    except (TypeError, ValueError):
        detail = {}
    return {"stream": row.get("stream", stream), "last_ts": row.get("last_ts") or "",
            "status": row.get("status") or "", "detail": detail}


def all_runs() -> list[dict]:
    """全部流的最新状态（运维视图用）。"""
    try:
        rows = db_mod.get_db().query(
            "SELECT stream FROM sync_state ORDER BY stream")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        rec = last_run(str(r.get("stream") or ""))
        if rec:
            out.append(rec)
    return out
