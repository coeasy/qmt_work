"""Phase 6 durable JobRuntime contract tests using real SQLite."""
import asyncio

from app.runtime.jobs import JobRuntime, JobSpec
from core.db import DB


def test_job_state_and_lease_are_persisted(tmp_path):
    db = DB(tmp_path / "jobs.db")

    async def runner(job):
        job["report"](50, "half")
        await asyncio.sleep(0.01)
        return {"value": 1}

    async def run():
        rt = JobRuntime(db=db, owner="test-owner")
        rid = rt.submit(JobSpec(kind="report", name="durable", runner=runner))
        while rt.get(rid)["status"] != "done":
            await asyncio.sleep(0.01)
        row = db.query_one("SELECT status,result_json,lease_owner,heartbeat_at "
                           "FROM runtime_jobs WHERE id=?", (rid,))
        assert row["status"] == "done"
        assert '"value": 1' in row["result_json"]
        assert row["lease_owner"] == ""
        assert row["heartbeat_at"] is not None

    asyncio.run(run())
    db._conn.close()


def test_startup_catchup_requeues_persisted_job(tmp_path):
    db = DB(tmp_path / "jobs.db")
    db.execute(
        "INSERT INTO runtime_jobs (id,kind,name,status,params_json,created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("screen-0001", "screen", "catchup", "running", '{"conditions": {}}', "now"),
    )
    rt = JobRuntime(owner="new-owner")
    rt.attach_db(db)
    job = rt.get("screen-0001")
    assert job["status"] == "queued"
    assert "启动补跑" in job["message"]
    db._conn.close()
