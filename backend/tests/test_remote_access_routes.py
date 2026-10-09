"""远程访问路由集成测试（app.routes.remote_access）。

设计：
- 不依赖 app_client 完整 lifespan（太重），用最小 FastAPI 装配 + mock ctx 直接打路由。
- config_file / exe_dir 全部重定向到 tmp_path，避免污染 backend/qmt_work_config.json。
- 覆盖：status 查询、mode 切换（合法/非法/前置校验）、api-key 重置、totp enable/verify、modes 列表。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


from core.context import AppContext, set_active_context, active_context_or_none  # noqa: E402


def _get_settings():
    """动态获取当前 settings 单例（避免模块级导入在 test_remote_access_tiers 重导入后失效）。"""
    from core.config import settings as _s
    return _s


# ---------------- 夹具 ----------------

@pytest.fixture
def tmp_config_dir(tmp_path):
    """把 config_file / exe_dir 重定向到 tmp_path，并清理 sys.modules 让路由模块重新导入。"""
    # 清空 remote_access 路由模块缓存，让 import 时拿新的 config_file 绑定
    sys.modules.pop("app.routes.remote_access", None)
    sys.modules.pop("app.routes", None)
    # 用 pytest 的 monkeypatch 风格手工替换（不影响其他测试）
    import app.routes.remote_access as ra

    _orig_config_file = ra.config_file
    _orig_exe_dir = ra.exe_dir

    def fake_config_file():
        return tmp_path / "qmt_work_config.json"

    def fake_exe_dir():
        return tmp_path

    ra.config_file = fake_config_file
    ra.exe_dir = fake_exe_dir

    # 同时替换 core.config 的（供 route 内部 audit_log 间接调用时也能拿到正确路径）
    import core.config as cc
    _orig_cc_config_file = cc.config_file
    _orig_cc_exe_dir = cc.exe_dir
    cc.config_file = fake_config_file
    cc.exe_dir = fake_exe_dir

    # 同时更新 update_config_file 内部的引用（它通过模块级 config_file 调用）
    _orig_update = cc.update_config_file

    def patched_update(updates):
        f = fake_config_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, json.JSONDecodeError):
                data = {}
        data.update(updates)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return f

    ra.update_config_file = patched_update
    cc.update_config_file = patched_update

    yield tmp_path

    ra.config_file = _orig_config_file
    ra.exe_dir = _orig_exe_dir
    cc.config_file = _orig_cc_config_file
    cc.exe_dir = _orig_cc_exe_dir
    cc.update_config_file = _orig_update


@pytest.fixture
def mock_ctx():
    """最小可用的 AppContext：signal_router 为 None（未初始化）。"""
    ctx = AppContext()
    prev = active_context_or_none()
    set_active_context(ctx)
    yield ctx
    set_active_context(prev)


@pytest.fixture
def client(tmp_config_dir, mock_ctx):
    """最小 FastAPI app：只挂载 remote_access 路由，走真实路由分发（不经完整 lifespan）。"""
    import app.routes.remote_access as ra
    app = FastAPI()
    app.include_router(ra.router, prefix="/api/v1")
    return TestClient(app)


# ---------------- 用例 ----------------

def test_status_returns_full_state(client):
    r = client.get("/api/v1/remote-access/status")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["mode"] in ("off", "lan", "wan")
    assert data["label"] in ("单机（仅本机）", "内网（局域网多设备）", "公网（远程访问）")
    assert "host" in data and "port" in data
    assert "api_key" in data and "configured" in data["api_key"]
    assert "totp" in data and "enabled" in data["totp"]
    assert "warnings" in data
    assert data["available_modes"] == ["off", "lan", "wan"]


def test_status_modes_list(client):
    r = client.get("/api/v1/remote-access/modes")
    assert r.status_code == 200
    items = r.json()["data"]
    assert len(items) == 3
    modes = {i["mode"] for i in items}
    assert modes == {"off", "lan", "wan"}
    # wan 必须强制 TOTP + 强密钥 + signal 默认 paper
    wan = next(i for i in items if i["mode"] == "wan")
    assert wan["force_totp"] is True
    assert wan["force_api_key"] is True
    assert wan["signal_default"] == "paper"


def test_set_mode_rejects_illegal(client):
    r = client.post("/api/v1/remote-access/mode", json={"mode": "nonsense"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 400
    assert "非法档位" in body["message"]


def test_set_mode_lan_rejects_default_key(client):
    """lan 档 + 默认密钥 → 400（前置校验）。"""
    with patch.object(_get_settings(), "api_key", "qmt-dev-key"):
        with patch.object(_get_settings(), "remote_access", "off"):
            r = client.post("/api/v1/remote-access/mode", json={"mode": "lan"})
            assert r.json()["code"] == 400
            assert "强 API Key" in r.json()["message"] or "默认" in r.json()["message"]


def test_set_mode_lan_accepts_custom_key(tmp_config_dir, client):
    """lan 档 + 自定义密钥：写入配置文件并返回 requires_restart。"""
    with patch.object(_get_settings(), "api_key", "my-strong-key-12345"):
        with patch.object(_get_settings(), "remote_access", "off"):
            r = client.post("/api/v1/remote-access/mode", json={"mode": "lan"})
            body = r.json()
            assert body["code"] == 0, body
            assert body["data"]["mode"] == "lan"
            assert body["data"]["requires_restart"] is True
    # 验证配置文件已写入
    cfg_path = tmp_config_dir / "qmt_work_config.json"
    assert cfg_path.exists()
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["remote_access"] == "lan"


def test_set_mode_wan_rejects_without_totp(client):
    """wan 档 + 无 TOTP → 400。"""
    with patch.object(_get_settings(), "api_key", "my-strong-key-12345"):
        with patch.object(_get_settings(), "remote_access", "off"):
            with patch.object(_get_settings(), "totp_secret", ""):
                r = client.post("/api/v1/remote-access/mode", json={"mode": "wan"})
                assert r.json()["code"] == 400
                assert "TOTP" in r.json()["message"]


def test_set_mode_wan_accepts_with_totp(tmp_config_dir, client):
    """wan 档 + 强密钥 + TOTP：写入配置文件，signal.mode 自动设为 paper。"""
    with patch.object(_get_settings(), "api_key", "my-strong-key-12345"):
        with patch.object(_get_settings(), "remote_access", "off"):
            with patch.object(_get_settings(), "totp_secret", "JBSWY3DPEHPK3PXP"):
                r = client.post("/api/v1/remote-access/mode", json={"mode": "wan"})
                assert r.json()["code"] == 0
                data = r.json()["data"]
                assert data["mode"] == "wan"
                # wan 档强制 signal.mode=paper（即使 signal_router 不可用，字段也应为 paper）
                assert data["signal_mode"] == "paper"
                # signal_router 不可用时 auto_set 为 False，但 signal_mode 仍报告 paper
                # （signal_router 为 None 时无法实际写入，但返回字段提示用户）
    cfg_path = tmp_config_dir / "qmt_work_config.json"
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["remote_access"] == "wan"


def test_set_mode_off_always_ok(client):
    """切回 off 档永远允许（无论密钥状态）。"""
    with patch.object(_get_settings(), "api_key", "qmt-dev-key"):
        with patch.object(_get_settings(), "remote_access", "wan"):
            with patch.object(_get_settings(), "totp_secret", ""):
                r = client.post("/api/v1/remote-access/mode", json={"mode": "off"})
                assert r.json()["code"] == 0
                assert r.json()["data"]["mode"] == "off"


def test_reset_api_key_returns_onetime_value(tmp_config_dir, client):
    """重置 API Key：返回 40 字符新密钥 + requires_restart，写入配置文件。"""
    r = client.post("/api/v1/remote-access/api-key")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    new_key = body["data"]["api_key"]
    assert len(new_key) == 40
    assert new_key != "qmt-dev-key"
    assert body["data"]["requires_restart"] is True
    assert "仅此一次" in body["data"]["message"]
    # 写入配置文件
    cfg_path = tmp_config_dir / "qmt_work_config.json"
    assert cfg_path.exists()
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["api_key"] == new_key


def test_totp_enable_returns_secret_and_url(tmp_config_dir, client):
    """启用 TOTP：返回 base32 secret + otpauth URL + requires_restart。"""
    with patch.object(_get_settings(), "totp_secret", ""):
        r = client.post("/api/v1/remote-access/totp/enable")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 0
        secret = body["data"]["secret"]
        assert len(secret) >= 16
        # BASE32 字符集校验
        assert all(c.isupper() or c.isdigit() for c in secret)
        assert "otpauth://" in body["data"]["otpauth_url"]
        assert secret in body["data"]["otpauth_url"]
        assert body["data"]["requires_restart"] is True
    # 写入配置文件
    cfg_path = tmp_config_dir / "qmt_work_config.json"
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["totp_secret"] == secret


def test_totp_enable_conflicts_when_already_enabled(client):
    """TOTP 已启用时再次调用 → 409 冲突（避免覆盖用户现有绑定）。"""
    with patch.object(_get_settings(), "totp_secret", "EXISTING_SECRET"):
        r = client.post("/api/v1/remote-access/totp/enable")
        assert r.json()["code"] == 409
        assert "已启用" in r.json()["message"]


def test_totp_verify_rejects_when_disabled(client):
    """TOTP 未启用时校验 → 400。"""
    with patch.object(_get_settings(), "totp_secret", ""):
        r = client.post("/api/v1/remote-access/totp/verify", json={"code": "123456"})
        assert r.json()["code"] == 400
        assert "未启用" in r.json()["message"]


def test_totp_verify_accepts_valid_code(client):
    """TOTP 启用 + 当前有效码 → 校验通过。"""
    from gateway.totp import totp_at
    secret = "JBSWY3DPEHPK3PXP"
    current = totp_at(secret)
    with patch.object(_get_settings(), "totp_secret", secret):
        r = client.post("/api/v1/remote-access/totp/verify", json={"code": current})
        assert r.json()["code"] == 0
        assert r.json()["data"]["verified"] is True


def test_totp_verify_rejects_invalid_code(client):
    """TOTP 启用 + 错误码 → 400。"""
    with patch.object(_get_settings(), "totp_secret", "JBSWY3DPEHPK3PXP"):
        r = client.post("/api/v1/remote-access/totp/verify", json={"code": "000000"})
        assert r.json()["code"] == 400
        assert "校验失败" in r.json()["message"] or "TOTP" in r.json()["message"]


def test_totp_verify_missing_code_field(client):
    """缺 code 字段 → 400。"""
    with patch.object(_get_settings(), "totp_secret", "JBSWY3DPEHPK3PXP"):
        r = client.post("/api/v1/remote-access/totp/verify", json={})
        assert r.json()["code"] == 400


def test_status_signals_warnings_when_wan_live(client):
    """wan 档 + signal.mode=live + 默认密钥/无 TOTP → warnings 数组含风险条目。"""
    from unittest.mock import MagicMock
    sr = MagicMock()
    sr.mode = "live"
    sr.totp_secret = ""

    with patch.object(_get_settings(), "api_key", "qmt-dev-key"):
        with patch.object(_get_settings(), "remote_access", "wan"):
            with patch.object(_get_settings(), "totp_secret", ""):
                # 让 ctx.signal_router 返回 mock
                mock_ctx = active_context_or_none()
                if mock_ctx is not None:
                    mock_ctx.signal_router = sr
                r = client.get("/api/v1/remote-access/status")
                body = r.json()
                assert body["code"] == 0
                warnings = body["data"]["warnings"]
                # 至少含 2 条警告（默认密钥 + 无 TOTP）
                assert len(warnings) >= 2
                # 含 wan+live 警告
                assert any("live" in w for w in warnings)
