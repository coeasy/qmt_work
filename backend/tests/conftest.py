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
- 测试**建议**逐文件运行。⚠️ 更正（2026-09-15 实测）：同进程全量**并不会硬崩溃**。
  修复前有 **5 条顺序依赖的假失败**（`test_risk_regression` / `test_screen_without_qmt` /
  `test_ws_contract`×3，单独跑该文件时全部通过），根因**有两类**，均已结构性修掉：
  1. **active context 未恢复** → `_restore_active_context`（见该夹具 docstring）；
  2. **进程级单例 `core.state.state` 被 lifespan 就地改写且从不还原** →
     `_restore_process_state`（见该夹具 docstring）。
  修复后全量同进程为 **0 failed**，测试集与用例顺序无关。
  收集数见 ``scripts/ci_reconcile.py::EXPECTED_TESTS``（权威计数）。
  **全量同进程跑适合作「差分兜底」**（出现失败即说明有新增回归）。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 进程级单例 core.state.state 的快照 / 恢复（V11 R7：第二类顺序依赖根因）
# ---------------------------------------------------------------------------
#: 任何测试执行**之前**的干净基线（由 pytest_configure 采集）。
_PROCESS_STATE_BASELINE: dict | None = None
#: ``app_client`` lifespan 生效后的状态（由 app_client 夹具采集）。
_APP_CLIENT_STATE: dict | None = None


def _snapshot_state() -> dict:
    """快照进程级单例 ``core.state.state`` 的全部槽位（浅拷贝，48 个键）。"""
    from core.state import state

    return dict(vars(state))


def _apply_state(snapshot: dict) -> None:
    """把 ``core.state.state`` 的槽位**就地**重置为给定快照。"""
    from core.state import state

    d = vars(state)
    d.clear()
    d.update(snapshot)


def pytest_configure(config):  # noqa: ARG001 — pytest 钩子签名
    """在任何 fixture / 用例之前采集干净基线。

    ``core.state.state`` 是**模块级单例**，``main.create_app`` 的 lifespan 会
    **就地**写入 ``broker_manager`` / ``db`` / ``risk`` 等槽位，且**从不还原**。
    基线必须在 lifespan 跑之前取，否则基线本身就是被污染的状态。
    """
    global _PROCESS_STATE_BASELINE
    if _PROCESS_STATE_BASELINE is None:
        _PROCESS_STATE_BASELINE = _snapshot_state()


@pytest.fixture(autouse=True)
def _restore_process_state(request):
    """每个用例前后把 ``core.state.state`` 重置到**本用例应有的基线**。

    背景（2026-09-15 实测，最小复现 ``test_appcontext_injection → test_screen_without_qmt``
    → 1 failed）：``app_client`` 是 ``scope="session"``（lifespan 单次约 9s，37 个用例
    不能逐例重启），它的 lifespan 会把进程级单例 ``core.state.state`` 就地改写 ——
    其中 ``broker_manager`` 是**真实 BrokerManager 且首个连接 ``connected=True``**。
    此后任何用例的 ``BarsProvider._qmt_connected()`` 都会返回 ``True``，
    于是 ``resolve_policy`` 不再剔除 ``broker``、``degraded`` 恒为 ``False`` ——
    与「无 QMT 时降级」类断言的自身前提直接冲突。

    规则：
    - 用例**不使用** ``app_client`` → 恢复到**会话开始前的干净基线**；
    - 用例**使用** ``app_client`` → 恢复到 lifespan 生效后的状态（``_APP_CLIENT_STATE``），
      保证同一 session 内各 app_client 用例看到同一基线。

    这样「用例的初始全局状态」只由它自己是否使用 ``app_client`` 决定，
    与**执行顺序无关**；用例若需要「无券商连接」等前提，应自行显式设置
    （见 ``test_screen_without_qmt.py::test_env2_*``），不得依赖环境残留。
    """
    baseline = _APP_CLIENT_STATE if "app_client" in request.fixturenames else _PROCESS_STATE_BASELINE
    if baseline is not None:
        _apply_state(baseline)
    yield
    if baseline is not None:
        _apply_state(baseline)


@pytest.fixture(autouse=True)
def _restore_active_context():
    """每个用例前后**精确恢复**进程级 active context（V11 R7：P1-4 的单点结构修复）。

    背景（2026-09-15 实测并确定性复现）：``set_active_context`` 写的是模块级全局；
    多个用例在 teardown 里 ``set_active_context(AppContext())`` / ``set_active_context(None)``
    —— 那是「换成空实例 / 置空」，**不是恢复**。而 ``app_client`` 是 ``scope="session"``，
    lifespan 只在首次使用时跑一次，于是此后所有用 ``app_client`` 的文件槽位全为 None，
    产生 5 条**顺序依赖假失败**（同进程全量独有、单独跑该文件全过）。

    复现（修复前）：
      ``appcontext_injection → paper → ws_contract``        → 3 failed
      ``appcontext_injection → e2e_flows → risk_regression`` → 1 failed

    本夹具让**整个测试集与用例顺序无关**，无需逐个用例自觉恢复。
    用例若想显式切换，请用 ``core.context.use_context(...)``（自动恢复）。
    """
    from core import context as _ctx_mod

    prev = _ctx_mod.active_context_or_none()
    yield
    _ctx_mod.set_active_context(prev)


@pytest.fixture(scope="session")
def app_client():
    """完整装配的 FastAPI app（含 lifespan 启停）的进程内客户端。"""
    global _APP_CLIENT_STATE
    from app.main import app
    from core.config import settings

    client = TestClient(app)
    client.headers.update({"X-API-Key": settings.api_key})
    client.__enter__()
    # lifespan 生效后的进程级单例快照：供 _restore_process_state 给「用 app_client 的用例」
    # 复用（session 作用域下 lifespan 只跑一次，用例之间必须回到同一基线）。
    _APP_CLIENT_STATE = _snapshot_state()
    try:
        yield client
    finally:
        try:
            client.__exit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001 — 停机噪声兜底（见模块 docstring）
            logging.getLogger("qmt_work.tests").warning(
                "app_client shutdown noise (suppressed): %r", exc)
