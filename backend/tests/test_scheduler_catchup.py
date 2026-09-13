"""V9 Phase 7 DoD：启动 catch-up —— 崩溃/停机时的 queued/running 任务重新入队。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _phase7_support import tmp_db  # noqa: F401,E402
from app.runtime.jobs import JobRuntime  # noqa: E402


def _insert_job(db, job_id, kind="sync", status="running"):
    db.execute(
        "INSERT OR REPLACE INTO runtime_jobs "
        "(id, kind, name, priority, status, progress, message, created_at,"
        " result_json, error, params_json, lease_owner, lease_until,"
        " heartbeat_at, checkpoint_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, kind, "测试任务", 5, status, 0, "", "2026-09-10T09:00:00",
         "{}", None, json.dumps({"limit": 1}), "owner-x", 0.0, None, "{}"))
    db._conn.commit()


def test_running_jobs_requeued_on_attach(tmp_db):
    _insert_job(tmp_db, "sync-0001", "sync", "running")
    rt = JobRuntime(db=tmp_db)
    rt.attach_db(tmp_db)
    got = rt.get("sync-0001")
    assert got is not None
    assert got["status"] == "queued"            # 重新入队
    assert "补跑" in got["message"] or "租约" in got["message"]
    assert got["checkpoint"] == {}              # checkpoint 从账本恢复


def test_unknown_kind_not_faked(tmp_db):
    """未知 runner 类型不伪造恢复：显式 failed 供运维处理（零 mock 铁律）。"""
    _insert_job(tmp_db, "zzz-9999", "no_such_kind", "queued")
    rt = JobRuntime(db=tmp_db)
    rt.attach_db(tmp_db)
    rows = tmp_db.query("SELECT status, error FROM runtime_jobs WHERE id='zzz-9999'")
    assert rows[0]["status"] == "failed"
    assert "无法恢复" in (rows[0]["error"] or "")


def test_finished_jobs_untouched(tmp_db):
    _insert_job(tmp_db, "sync-0002", "sync", "done")
    rt = JobRuntime(db=tmp_db)
    rt.attach_db(tmp_db)
    assert rt.get("sync-0002") is None          # 终态任务不重跑
