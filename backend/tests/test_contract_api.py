"""API / 注册表 / 响应包 契约测试（CI 门禁，不依赖真实券商 SDK）。

覆盖三层契约：
1. 统一响应包（ok / err）形状；
2. 券商注册表（BrokerProfile 字段、内置档案集合、未知券商拒绝、能力协商与派生）；
3. REST 接口契约（用最小 FastAPI app + 测试态挂载，验证零 mock、业务码约定、
   未知券商 400、已知券商注册后出现在连接列表）。

约定强调：业务异常返回 **HTTP 200 + code != 0**（code=503 表示未连接券商）。
未连接任何券商时 `/brokers` 必须返回 `data: []`（绝不造假）。
"""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes._common import ok, err, state as _common_state
from xtquant_client.registry import (
    BrokerProfile, Registry, list_profiles, get_profile, create_adapter,
)

# ---------------- 1. 响应包契约 ----------------
def test_envelope_ok():
    assert ok({"x": 1}) == {"code": 0, "message": "ok", "data": {"x": 1}}
    assert ok(None)["code"] == 0 and ok(None)["message"] == "ok"

def test_envelope_err():
    e = err(503, "未连接")
    assert e == {"code": 503, "message": "未连接", "data": None}
    # 三参调用：extra 作为 data 透传
    e2 = err(400, "参数错误", {"field": "broker_id"})
    assert e2["code"] == 400 and e2["data"] == {"field": "broker_id"}

def test_envelope_business_error_is_http_200():
    """业务错误（如未知券商）走 HTTP 200 + code!=0，而非 HTTP 4xx/5xx。"""
    assert err(400, "x")["code"] == 400      # 路由直接返回 err 字典 → FastAPI 序列化为 200
    assert err(503, "x")["code"] == 503

# ---------------- 2. 券商注册表契约 ----------------
def test_builtin_profiles_present():
    profiles = list_profiles()
    assert len(profiles) >= 6, "至少应包含内置迅投系券商"
    ids = {p.id for p in profiles}
    for need in ("guojin", "huaxin", "yinhe", "zxjt", "xy", "gf"):
        assert need in ids, f"缺少内置券商档案：{need}"

def test_profile_required_fields():
    for p in list_profiles():
        assert p.id and p.name and p.adapter
        assert isinstance(p.supported_periods, list) and p.supported_periods
        assert isinstance(p.supported_account_types, list) and p.supported_account_types
        # 迅投系必须声明 xtquant SDK
        if p.adapter == "xtp":
            assert p.sdk_required == "xtquant"

def test_get_profile_unknown_none():
    assert get_profile("__no_such_broker__") is None

def test_create_adapter_unknown_raises():
    with pytest.raises(ValueError):
        create_adapter("__no_such_broker__", "", "123", "STOCK")

def test_effective_capabilities_derivation():
    """账户类型 OPTION/CREDIT/FUTURES 应派生出 option/credit/futures 能力。"""
    reg = Registry()
    guojin = reg.get("guojin")
    caps = reg.effective_capabilities(guojin)
    assert "quote" in caps and "trade" in caps
    assert "option" in caps and "credit" in caps and "futures" in caps

def test_negotiate_capabilities():
    reg = Registry()
    neg = reg.negotiate("guojin", ["quote", "trade", "teleport"])
    assert neg["found"] is True
    assert "quote" in neg["supported"]
    assert "teleport" in neg["unsupported"]
    assert set(neg["supported"]).isdisjoint(neg["unsupported"])

# ---------------- 3. REST 接口契约（最小 app + 测试态） ----------------
class _FakeBrokerManager:
    """测试态 BrokerManager：仅记录 add_connection，不触达真实 SDK/子进程。"""
    def __init__(self):
        self._conns = []
        self._active_id = None

    def add_connection(self, cfg, autoconnect=True):
        if not cfg.conn_id:
            cfg.conn_id = f"test-conn-{len(self._conns) + 1}"
        # 轻量伪造连接对象：仅含 diagnostics 端点读取的属性
        adapter = type("A", (), {"broker_name": "测试券商", "adapter_id": "xtp"})()
        bridge = type("B", (), {"pump_running": lambda: False})()
        conn = type("Conn", (), {
            "cfg": cfg, "connected": False, "bridge": bridge, "adapter": adapter,
        })()
        self._conns.append(conn)
        return conn

    def all_connections(self):
        return self._conns

    def status_list(self):
        out = []
        for conn in self._conns:
            cfg = conn.cfg
            out.append({
                "conn_id": cfg.conn_id, "name": cfg.name or cfg.broker_id,
                "broker_id": cfg.broker_id, "broker_name": "测试券商",
                "account_id": cfg.account_id, "account_type": cfg.account_type,
                "connected": False, "active": False,
                "health_status": "disconnected", "reconnect_attempts": 0,
                "last_error": "", "adapter": "xtp", "client_version": "v1",
                "supported_periods": ["1d"], "supported_account_types": ["STOCK"],
            })
        return out


def _patch_state(fake_bm):
    import app.state as state_mod
    s = state_mod.state
    saved = {}
    patch = {
        "broker_manager": fake_bm,
        "db": object(),            # 真值 → health 的 db 检查通过
        "started_at": time.time(),
        "backtest_queue": None,
        "limitup_monitor": None,
        "algo_engine": None,
        "ws_manager": None,
        "sync_engine": None,
        "health_monitor": None,
    }
    for k, v in patch.items():
        saved[k] = getattr(s, k, None)
        setattr(s, k, v)
    return saved


def _restore_state(saved):
    import app.state as state_mod
    s = state_mod.state
    for k, v in saved.items():
        setattr(s, k, v)


@pytest.fixture
def client():
    from app.routes.broker import router as broker_router
    from app.routes.health import router as health_router
    app = FastAPI()
    app.include_router(broker_router)
    app.include_router(health_router)
    fake_bm = _FakeBrokerManager()
    saved = _patch_state(fake_bm)
    c = TestClient(app)
    try:
        yield c
    finally:
        _restore_state(saved)


def test_http_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["status"] in ("pass", "fail", "warn")
    assert body["data"]["service"] == "qmt_work"

def test_http_live(client):
    r = client.get("/live")
    assert r.status_code == 200
    assert r.json()["code"] == 0
    assert r.json()["data"]["status"] == "ok"

def test_http_brokers_zero_mock(client):
    """未连接任何券商时必须返回空列表，绝不返回模拟数据。"""
    r = client.get("/brokers")
    assert r.status_code == 200
    assert r.json()["code"] == 0
    assert r.json()["data"] == []

def test_http_broker_profiles(client):
    r = client.get("/brokers/profiles")
    assert r.status_code == 200
    assert r.json()["code"] == 0
    data = r.json()["data"]
    assert isinstance(data, list) and len(data) >= 6
    ids = {p["id"] for p in data}
    assert "guojin" in ids

def test_http_add_unknown_broker_400(client):
    """未知券商：业务码 400，但 HTTP 仍为 200（统一响应包约定）。"""
    r = client.post("/brokers", json={"broker_id": "nope", "client_path": "x"})
    assert r.status_code == 200
    assert r.json()["code"] == 400

def test_http_add_known_broker_registers(client):
    """已知券商 + autoconnect=false：注册成功并出现在连接列表。"""
    r = client.post("/brokers", json={
        "broker_id": "guojin",
        "client_path": r"C:\国金证券QMT交易端\userdata_mini",
        "account_id": "", "account_type": "STOCK", "autoconnect": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["conn_id"]
    # 随后列表应含 1 条
    r2 = client.get("/brokers")
    assert r2.json()["code"] == 0
    assert len(r2.json()["data"]) == 1
    assert r2.json()["data"][0]["broker_id"] == "guojin"

def test_http_broker_diagnostics_shallow(client):
    """/brokers/diagnostics 浅层：返回宿主 ABI、随包运行时、连接列表（零 mock）。"""
    r = client.get("/brokers/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    d = body["data"]
    # 结构契约
    assert isinstance(d["host_abi"], int)
    assert isinstance(d["host_python"], str) and d["host_python"]
    assert isinstance(d["bundled_runtimes"], dict)
    assert isinstance(d["connections"], list)
    assert "generated_at" in d
    # 零 mock：无连接时 connections 为空
    assert d["connections"] == []
