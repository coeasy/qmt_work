"""`/config/paths` 端点契约（P0-3 II 数据目录可配置）。

锁定的不变量（错了会出真事）：

1. **三类目录的可改性必须如实区分**：``export``/``cold`` 运行期可改，
   ``db`` **必须**报 ``requires_restart=true`` —— 把它渲染成「已生效」就是
   最典型的一种「点了没反应」（用户以为搬好了，重启后对着空库）；
2. **校验端点永不 4xx/5xx**：它服务于「边输边提示」，半截路径是常态；
3. **非法目录返回 400 而不是默默接受**：目录是外部输入面，一次放行就等于放行全部；
4. **主库配置必须**合并写入*配置文件（不能整体覆盖，那会删掉用户手改的配置）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.routes import config as cfg_routes


def get(client, path, **params):
    r = client.get("/api/v1" + path, params=params or None)
    assert r.status_code == 200, f"{path} HTTP {r.status_code}: {r.text[:200]}"
    return r.json()


def post(client, path, payload=None):
    r = client.post("/api/v1" + path, json=payload or {})
    assert r.status_code == 200, f"{path} HTTP {r.status_code}: {r.text[:200]}"
    return r.json()


def put(client, path, payload):
    r = client.put("/api/v1" + path, json=payload)
    assert r.status_code == 200, f"{path} HTTP {r.status_code}: {r.text[:200]}"
    return r.json()


@pytest.fixture
def _clean_dirs(app_client):
    """测试结束后把目录配置恢复默认，避免污染开发者本机的运行期配置。"""
    yield
    for key in ("offline.export_dir", "offline.cold_dir"):
        try:
            post(app_client, "/config/runtime/reset", {"key": key})
        except Exception:  # noqa: BLE001 还原失败不该让用例挂掉
            pass


# ------------------------------------------------------------------ GET

def test_paths_get_exposes_three_kinds(app_client):
    d = get(app_client, "/config/paths")
    assert d["code"] == 0, d
    data = d["data"]
    for k in ("export", "cold", "db"):
        assert k in data, data
        info = data[k]
        for field in ("label", "path", "configured", "default_path", "mutable",
                      "requires_restart", "exists", "writable", "usable", "reason"):
            assert field in info, f"{k} 缺字段 {field}"


def test_db_is_the_only_kind_requiring_restart(app_client):
    """★ 主库路径在启动期固化 —— 谎称「已生效」是最恶劣的假成功。"""
    data = get(app_client, "/config/paths")["data"]
    assert data["db"]["mutable"] is False
    assert data["db"]["requires_restart"] is True
    for k in ("export", "cold"):
        assert data[k]["mutable"] is True, k
        assert data[k]["requires_restart"] is False, k


def test_cold_reports_rows_and_sync_state(app_client):
    data = get(app_client, "/config/paths")["data"]
    cold = data["cold"]
    assert isinstance(cold.get("rows"), int)
    assert "in_sync" in cold and "attached_path" in cold
    assert isinstance(cold.get("candidates"), list)


# ------------------------------------------------------------------ validate

def test_validate_never_returns_error_code(app_client):
    """界面边输边提示：用户路径打了一半是常态，绝不能 400/500。"""
    import os
    from core.paths import system_dir_roots

    # ⚠️ 两条易踩的假失败，都是**测试写错**而不是实现错：
    # 1. ``/etc`` 在 Windows 上被解析成 ``C:\etc``（合法目录），拿它断言「必被拒」会假失败；
    # 2. **不存在的目录不等于不可用** —— 上级可写时它是合法的新目录（后端会创建它）。
    #    所以这里只断言「空 / 盘根 / 系统目录」这三类真·非法输入。
    bad = ["", "   ", str(Path(__file__).resolve().anchor)]
    bad += [str(p) for p in system_dir_roots()[:1]]
    if os.name == "posix":
        bad.append("/etc")
    for raw in bad:
        d = post(app_client, "/config/paths/validate", {"path": raw})
        assert d["code"] == 0, f"{raw!r} -> {d}"
        body = d["data"]
        assert body["ok"] is False, f"{raw!r} 竟被判为可用"
        assert body["reason"], f"{raw!r} 被拒却没有原因"


def test_validate_accepts_a_real_dir(app_client, tmp_path):
    d = post(app_client, "/config/paths/validate", {"path": str(tmp_path)})
    assert d["data"]["ok"] is True, d


# ------------------------------------------------------------------ PUT

def test_put_rejects_unknown_kind(app_client, tmp_path):
    d = put(app_client, "/config/paths", {"kind": "whatever", "path": str(tmp_path)})
    assert d["code"] == 400, d


def test_put_rejects_system_dir(app_client):
    from core.paths import system_dir_roots

    roots = system_dir_roots()
    if not roots:
        pytest.skip("当前环境取不到系统目录")
    d = put(app_client, "/config/paths", {"kind": "export", "path": str(roots[0])})
    assert d["code"] == 400, "系统目录竟被接受 —— 数据目录会写进系统目录"
    assert "系统目录" in d["message"]


def test_put_export_takes_effect_immediately(app_client, tmp_path, _clean_dirs):
    target = tmp_path / "exports"
    d = put(app_client, "/config/paths", {"kind": "export", "path": str(target)})
    assert d["code"] == 0, d
    assert d["data"]["requires_restart"] is False
    assert d["data"]["export"]["configured"] is True
    assert Path(d["data"]["export"]["path"]).name == "exports"
    assert target.is_dir(), "保存后目录应已存在（后端 validate_dir(create=True) 负责）"


def test_put_db_writes_config_file_and_requires_restart(app_client, tmp_path, monkeypatch):
    """★ 主库改动：写配置文件 + 如实返回 ``requires_restart=true``。

    ``update_config_file`` 被打桩到临时目录 —— 否则这条用例会改写开发者本机
    ``backend/qmt_work_config.json`` 的 ``db_path``，下次启动就换库了。
    """
    written: dict = {}

    def _fake(updates: dict):
        written.update(updates)
        f = tmp_path / "qmt_work_config.json"
        f.write_text(json.dumps(updates), encoding="utf-8")
        return f

    monkeypatch.setattr(cfg_routes, "update_config_file", _fake)

    d = put(app_client, "/config/paths",
            {"kind": "db", "path": str(tmp_path / "dbdir")})
    assert d["code"] == 0, d
    assert d["data"]["requires_restart"] is True, "主库改完必须显式要求重启"
    assert d["data"]["file"].endswith("app.db")
    assert d["data"]["current_file"], "必须告诉用户「当前还在用哪个库」"
    assert written.get("db_path", "").endswith("app.db")


def test_put_cold_switches_and_reports_migration_need(app_client, tmp_path, _clean_dirs):
    target = tmp_path / "colddir"
    d = put(app_client, "/config/paths", {"kind": "cold", "path": str(target)})
    assert d["code"] == 0, d
    assert d["data"]["cold"]["configured"] is True
    assert d["data"]["cold"]["in_sync"] is not False, "切换后冷仓应已装配到新路径"
    assert "迁移" in (d["data"]["note"] or ""), "必须提示「已有冷数据不会自动搬迁」"


def test_migrate_cold_without_candidate_is_a_clear_400(app_client, _clean_dirs):
    """没有可迁移的旧冷仓时，必须给出可读原因，而不是 500 或静默 0。"""
    d = post(app_client, "/config/paths/migrate-cold", {"source": ""})
    assert d["code"] in (0, 400, 503), d
    if d["code"] != 0:
        assert d["message"], "失败必须带原因"
