"""探针：验证新增策略运行容器 + 风控预检端点，并扫描全部 /api/v1 GET 路由是否有 500。"""
import sys
import traceback

import anyio
from fastapi.testclient import TestClient

from app.main import create_app

fails = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {extra}")
    if not cond:
        fails.append(name)


try:
    app = create_app()
except Exception:
    print("FATAL: app import failed")
    traceback.print_exc()
    sys.exit(1)

cm = TestClient(app)
try:
    with cm as c:
        c.headers.update({"X-API-Key": "qmt-dev-key"})

        r = c.get("/api/v1/health")
        check("health", r.status_code == 200, f"({r.status_code})")

        r = c.get("/api/v1/strategies/run")
        check("list runs", r.status_code == 200 and r.json().get("code") == 0,
              f"code={r.json().get('code')}")

        body = {
            "name": "probe-ma", "strategy_type": "ma_cross", "code": "600519.SH",
            "params": {"fast": 5, "slow": 20, "volume": 100}, "mode": "paper",
            "interval_seconds": 30,
        }
        r = c.post("/api/v1/strategies/run", json=body)
        j = r.json()
        check("create run", r.status_code == 200 and j.get("code") == 0,
              f"code={j.get('code')} msg={j.get('message')}")
        run_id = (j.get("data") or {}).get("id")
        check("create run has id", run_id is not None, f"id={run_id}")

        r = c.post("/api/v1/strategies/run/precheck",
                   json={"code": "600519.SH", "direction": "buy", "price": 100, "volume": 100})
        check("strategy precheck", r.status_code == 200 and r.json().get("code") == 0,
              f"data={r.json().get('data')}")

        r = c.post("/api/v1/trade/precheck",
                   json={"code": "600519.SH", "direction": "buy", "price": 100, "volume": 100})
        check("trade precheck", r.status_code == 200 and r.json().get("code") == 0,
              f"data={r.json().get('data')}")

        if run_id is not None:
            r = c.post(f"/api/v1/strategies/run/{run_id}/start")
            check("start run", r.status_code == 200 and r.json().get("code") == 0,
                  f"code={r.json().get('code')}")
            r = c.get(f"/api/v1/strategies/run/{run_id}/logs")
            check("run logs", r.status_code == 200 and r.json().get("code") == 0)
            r = c.post(f"/api/v1/strategies/run/{run_id}/stop")
            check("stop run", r.status_code == 200 and r.json().get("code") == 0)
            r = c.delete(f"/api/v1/strategies/run/{run_id}")
            check("delete run", r.status_code == 200 and r.json().get("code") == 0)

        r = c.post("/api/v1/strategies/run",
                   json={"strategy_type": "bogus", "code": "600519.SH"})
        check("reject bad type", r.json().get("code") == 400, f"code={r.json().get('code')}")

        five_hundreds = []
        for route in app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if not path.startswith("/api/v1"):
                continue
            if "GET" not in methods:
                continue
            try:
                rr = c.get(path)
                if rr.status_code == 500:
                    five_hundreds.append(path)
            except Exception as e:  # noqa: BLE001
                five_hundreds.append(f"{path} EXC {e}")
        check("no GET 500s", len(five_hundreds) == 0, f"bad={five_hundreds[:10]}")

        print("\n=== SUMMARY ===")
        if fails:
            print("FAILED:", fails)
            sys.exit(2)
        print("ALL PASS")
except Exception as e:
    if isinstance(e, anyio.get_cancelled_exc_class()) or "CancelledError" in type(e).__name__:
        # 忽略 TestClient 关闭时的 anyio 取消噪声
        print("\n=== SUMMARY (suppressed shutdown noise) ===")
        if fails:
            print("FAILED:", fails)
            sys.exit(2)
        print("ALL PASS")
    else:
        raise
