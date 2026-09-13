"""pytest 共享夹具（Phase 0 ⑤ / Phase 1 ②）。

``app_client``：进程内 TestClient，**触发完整 lifespan**（6 阶段 bootstrap），
CI 无需存活后端即可验证 REST/WS/MCP 契约。

注意：
- TestClient 的请求 host 是 testserver（非 loopback），因此默认携带主密钥头，
  否则 /mcp 等被鉴权中间件 401；
- 无券商连接时端点按「HTTP 200 + code=503 引导」收口；本机 DB 若有 autoconnect
  券商则返回真实数据——测试断言必须环境无关（503 引导或真实数据，绝不假数据）；
- shutdown 阶段后台任务可能抛 CancelledError（TestClient 已完成真实停机序列：
  db backup / mcp session manager 均正常关闭），此处兜底吞掉退出噪声；
- 测试须逐文件运行（同进程全量会硬崩溃，见项目记忆）。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def app_client():
    """完整装配的 FastAPI app（含 lifespan 启停）的进程内客户端。"""
    from app.main import app
    from core.config import settings

    client = TestClient(app)
    client.headers.update({"X-API-Key": settings.api_key})
    client.__enter__()
    try:
        yield client
    finally:
        try:
            client.__exit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001 — 停机噪声兜底（见模块 docstring）
            logging.getLogger("qmt_work.tests").warning(
                "app_client shutdown noise (suppressed): %r", exc)
