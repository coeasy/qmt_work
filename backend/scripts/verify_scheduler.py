"""验证 durable scheduler 修复：捕获启动日志，检查 ready vs 失败。"""
import io
import logging
import os

os.chdir(r"p:/github_public/qmt_work/backend")

buf = io.StringIO()
handler = logging.StreamHandler(buf)
handler.setLevel(logging.INFO)
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import contextlib

from app.main import app

lines = []
with contextlib.ExitStack() as stack:
    client = stack.enter_context(TestClient(app))

log_out = buf.getvalue()
for ln in log_out.splitlines():
    if "durable" in ln or "schedule" in ln or "market sync" in ln:
        lines.append(ln)

print("=== scheduler-related logs ===")
print("\n".join(lines) if lines else "(none)")
print("=== RESULT:", "PASS" if any("durable scheduler ready" in l for l in lines)
      and not any("durable scheduler 启动失败" in l for l in lines) else "FAIL", "===")
