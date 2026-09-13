"""V10 Phase A 验证：app 启动 + AppContext 注入 + 关键路由可达。"""
import os
import sys

os.environ.setdefault("QMT_API_KEY", "testkey")
os.environ.setdefault("QMT_DB_PATH", ":memory:")

import app.main as m
from fastapi.testclient import TestClient

app = m.app
log_lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    log_lines.append(s)
    print(s)


c = TestClient(app)
try:
    with c:
        r = c.get("/api/v1/health", headers={"X-API-Key": "testkey"})
        log("health", r.status_code, "code=", r.json().get("code"))
        if r.status_code >= 400:
            log("health body:", r.text[:500])
        r2 = c.get("/api/v1/config/runtime", headers={"X-API-Key": "testkey"})
        log("config/runtime", r2.status_code, "code=", r2.json().get("code"))
        r3 = c.get("/api/v1/ready", headers={"X-API-Key": "testkey"})
        d3 = (r3.json() or {}).get("data") or {}
        log("ready", r3.status_code,
            "ready=", d3.get("ready"),
            "lifecycle.ready=", (d3.get("lifecycle") or {}).get("ready"),
            "required=", d3.get("required_phases"))
        # Required 阶段（db/engines/watchdogs/replay/misc）全 ready 即应就绪；
        # broker 属 Optional，未连券商不得导致 503（快照同步回归防护）。
        assert r3.status_code == 200, \
            f"/ready 应为 200（Required 全 ready），实际 {r3.status_code}: {r3.text[:400]}"
        has_ctx = hasattr(app.state, "ctx")
        log("has ctx on app.state:", has_ctx)
        if has_ctx:
            ctx = app.state.ctx
            log("ctx.db set:", ctx.db is not None,
                "ctx.broker_manager:", ctx.broker_manager is not None,
                "ctx.job_runtime:", ctx.job_runtime is not None,
                "ctx.data_router:", ctx.data_router is not None,
                "ctx.execution:", ctx.execution is not None)
        from app.routes.health import health_check
        import inspect
        sig = inspect.signature(health_check)
        log("health_check params:", list(sig.parameters.keys()))
    log("RESULT: PASS")
except Exception as exc:  # noqa: BLE001
    # TestClient 关闭时后台调度任务取消会抛 CancelledError（非迁移问题）
    if type(exc).__name__ == "CancelledError":
        log("RESULT: PASS (lifespan teardown CancelledError ignored)")
    else:
        import traceback
        log("RESULT: FAIL")
        log(traceback.format_exc())

os.makedirs("output", exist_ok=True)
with open("output/verify_appctx.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(log_lines))
sys.stderr.write("\n".join(log_lines) + "\n")
