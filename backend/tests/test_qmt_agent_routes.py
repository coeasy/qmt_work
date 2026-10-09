"""大 QMT Agent 部署路由集成测试（app.routes.qmt_agent）。

设计原则（对齐 test_remote_access_routes.py）：
- 不依赖完整 app lifespan（太重），用最小 FastAPI 装配 + monkeypatch
  把 QMT 目录 / agent 源码 / 工具链指向 tmp_path，避免污染真实盘位。
- 覆盖：status 探测、bundle 生成、deploy dry_run 与真写、diagnose、
  config 读写、幂等备份、tool 列出。
- 契约：所有响应走 envelope {code:0,data}；失败走 code!=0。

零 mock 契约纪律：
- 生成 bundle 一律走真实的 gen_qmt_agent_bundle.build()（不 mock）；
- 诊断判据一律走 qmt_agent_verify.bundle_health()（不 mock）；
- 只有路径解析（_agent_bigqmt_dir / _qmt_tools_dir / _find_qmt_dir）
  被 monkeypatch，因为它们本来就应该在开发态和测试态指向不同地方。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))



@pytest.fixture
def qa_module(tmp_path, monkeypatch):
    """加载 qmt_agent 路由模块并 monkeypatch 三个路径函数。

    - _find_qmt_dir -> 返回 tmp_path 下构造的伪 QMT 根
    - _agent_bigqmt_dir -> 指向真实 backend/agent_bigqmt（用于 bundle 生成）
    - _qmt_tools_dir -> 指向真实 scripts/（用于加载 gen_qmt_agent_bundle）
    """
    # 强制重新加载，避免测试间污染
    sys.modules.pop("app.routes.qmt_agent", None)
    import app.routes.qmt_agent as qa

    # 构造伪 QMT 根目录：bin.x64 + userdata + python + config/user/root
    fake_qmt = tmp_path / "fake_qmt"
    (fake_qmt / "bin.x64").mkdir(parents=True)
    (fake_qmt / "userdata").mkdir(parents=True)
    (fake_qmt / "python").mkdir(parents=True)
    (fake_qmt / "config" / "user" / "root").mkdir(parents=True)

    monkeypatch.setattr(qa, "_find_qmt_dir",
                        lambda explicit=None: str(fake_qmt))
    # 让 agent 源码 / 工具链走真实位置（bundle 需要真实分片）
    real_bigqmt = Path(BACKEND_DIR) / "agent_bigqmt"
    real_tools = Path(BACKEND_DIR).parent / "scripts"
    monkeypatch.setattr(qa, "_agent_bigqmt_dir", lambda: real_bigqmt)
    monkeypatch.setattr(qa, "_qmt_tools_dir", lambda: real_tools)

    app = FastAPI()
    app.include_router(qa.router, prefix="/api/v1")
    client = TestClient(app)
    return qa, client, fake_qmt


def test_status_reports_probe(qa_module):
    qa, client, fake_qmt = qa_module
    r = client.get("/api/v1/qmt-agent/status")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["qmt_dir"] == str(fake_qmt)
    assert data["agent_tools_available"] is True
    assert data["agent_bigqmt_available"] is True
    assert data["bundle_chars_estimate"] > 10000, "bundle 应该非空"
    assert data["deployed"]["exists"] is False, "初始状态未部署"
    assert "checked_at" in data


def test_bundle_returns_text(qa_module):
    _, client, _ = qa_module
    r = client.get("/api/v1/qmt-agent/bundle")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["text"], "bundle 文本不能为空"
    assert data["encoding"] == "utf-8"
    assert data["size_bytes"] > 10000
    # 关键契约：不含语法错误
    assert not data["problems"], data["problems"]


def test_deploy_dry_run_does_not_write(qa_module):
    _, client, fake_qmt = qa_module
    r = client.post("/api/v1/qmt-agent/deploy",
                    json={"filename": "QMT_WORK_AGENT.py",
                          "strategy": "QMT_WORK_AGENT",
                          "dry_run": True,
                          "txt_copy": False})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["ok"] is True
    assert data["dry_run"] is True
    # 关键契约：dry_run 不写盘
    target = fake_qmt / "python" / "QMT_WORK_AGENT.py"
    assert not target.exists(), "dry_run 不应该写文件"


def test_deploy_writes_and_backs_up(qa_module):
    _, client, fake_qmt = qa_module
    # 第一次部署
    r1 = client.post("/api/v1/qmt-agent/deploy",
                     json={"filename": "QMT_WORK_AGENT.py",
                           "strategy": "QMT_WORK_AGENT",
                           "dry_run": False,
                           "txt_copy": True})
    assert r1.status_code == 200
    d1 = r1.json()["data"]
    assert d1["ok"] is True
    assert d1["backup"] is None, "首次部署无旧版可备份"
    target = fake_qmt / "python" / "QMT_WORK_AGENT.py"
    assert target.is_file()
    txt = fake_qmt / "python" / "QMT_WORK_AGENT.txt"
    assert txt.is_file(), "txt_copy 应写出 .txt 副本"
    first_size = target.stat().st_size

    # 第二次部署（同一文件）：应触发备份
    import time
    time.sleep(1.1)  # epoch 秒变了，才能生成不同的 .bak.<epoch>
    r2 = client.post("/api/v1/qmt-agent/deploy",
                     json={"filename": "QMT_WORK_AGENT.py",
                           "strategy": "QMT_WORK_AGENT",
                           "dry_run": False,
                           "txt_copy": False})
    assert r2.status_code == 200
    d2 = r2.json()["data"]
    assert d2["ok"] is True
    assert d2["backup"], "第二次部署必须有备份"
    assert Path(d2["backup"]).is_file(), "备份文件必须真实存在"
    assert target.stat().st_size == first_size, "bundle 字节数应稳定"
    # registered_hint 必须存在，提醒用户注册动作
    assert "registered_hint" in d2
    assert "注册" in d2["registered_hint"]


def test_diagnose_reports_missing_bundle(qa_module):
    _, client, _ = qa_module
    r = client.post("/api/v1/qmt-agent/diagnose",
                    json={"strategy": "no_such_strategy"})
    # envelope 契约：HTTP 200 + code!=0
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 503
    assert "找到" in body["message"]


def test_diagnose_after_deploy(qa_module):
    _, client, _ = qa_module
    # 先部署一份
    client.post("/api/v1/qmt-agent/deploy",
                json={"filename": "qmt_work_agent.py",
                      "strategy": "qmt_work_agent",
                      "dry_run": False, "txt_copy": False})
    r = client.post("/api/v1/qmt-agent/diagnose",
                    json={"strategy": "qmt_work_agent"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["bundle"]["path"].endswith("qmt_work_agent.py")
    assert data["bundle"]["size_bytes"] > 10000
    # 编码判据：本地生成的 bundle 应为 UTF-8
    assert data["encoding_ok"] is True, data["encoding_note"]
    # problems 可能不为空（心跳未跑是合理的），但**编码**不应出问题
    encoding_problems = [p for p in data["problems"]
                         if p["source"] == "encoding"]
    assert not encoding_problems, encoding_problems


def test_config_round_trip(qa_module):
    _, client, fake_qmt = qa_module
    # 初始：不存在
    r0 = client.get("/api/v1/qmt-agent/config")
    assert r0.status_code == 200
    body0 = r0.json()
    assert body0["code"] == 0
    d0 = body0["data"]
    assert d0["exists"] is False
    # 模板路径应该指向 backend/agent_bigqmt/agent_config.example.json
    assert "template" in d0.get("source", "")
    # 写入
    sample = {"bridge_dir": str(fake_qmt / "bridge"),
              "transport": "file",
              "auth_token": "test-token-1234",
              "poll_interval_ms": 500}
    r1 = client.post("/api/v1/qmt-agent/config",
                     json={"config": sample})
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["code"] == 0
    assert body1["data"]["written"] > 0
    # 读回
    r2 = client.get("/api/v1/qmt-agent/config")
    body2 = r2.json()
    assert body2["code"] == 0
    d2 = body2["data"]
    assert d2["exists"] is True
    assert d2["data"]["auth_token"] == "test-token-1234"
    # 第二次写入：应触发备份
    import time
    time.sleep(1.1)
    r3 = client.post("/api/v1/qmt-agent/config",
                     json={"config": {**sample, "auth_token": "new"}})
    assert r3.status_code == 200


def test_deploy_missing_qmt_dir_returns_400(qa_module, monkeypatch):
    """若 _find_qmt_dir 返回 None（找不到 QMT），deploy 应返回 code=400。"""
    qa, client, _ = qa_module
    monkeypatch.setattr(qa, "_find_qmt_dir", lambda explicit=None: None)
    r = client.post("/api/v1/qmt-agent/deploy",
                    json={"filename": "qmt_work_agent.py",
                          "strategy": "qmt_work_agent",
                          "dry_run": False, "txt_copy": False})
    # envelope 契约：HTTP 200 + code=400
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 400
    assert "未找到 QMT" in body["message"]


def test_tools_listing(qa_module):
    """打包清单里应有的文件都要能被列出（工具链自证）。"""
    _, client, _ = qa_module
    r = client.get("/api/v1/qmt-agent/tools")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["tools"], "工具链列表不应为空"
    names = {t["name"] for t in data["tools"]}
    # 至少这些必须随包
    for must in ("gen_qmt_agent_bundle.py", "qmt_agent_deploy.py",
                 "qmt_agent_verify.py"):
        assert must in names, f"{must} 缺失"
    # 源码目录
    src_names = {s["name"] for s in data["sources"]}
    for must in ("BIGQMT_AGENT.py", "qmt_api.py"):
        assert must in src_names, f"{must} 缺失"


# ===========================================================================
# 下发机制
# ===========================================================================

def test_distribute_status_disabled_by_default(qa_module):
    """下发源未配置时 enabled=False。"""
    _, client, _ = qa_module
    r = client.get("/api/v1/qmt-agent/distribute/status")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    d = body["data"]
    assert d["enabled"] is False
    assert d["url"] == ""


def test_distribute_check_without_url_returns_400(qa_module):
    _, client, _ = qa_module
    r = client.post("/api/v1/qmt-agent/distribute/check")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 400
    assert "未配置下发源" in body["message"]


def test_distribute_check_with_bad_url_returns_502(qa_module):
    """远端 URL 不可达 → envelope 502，绝不抛异常。"""
    _, client, _ = qa_module
    r = client.post("/api/v1/qmt-agent/distribute/check",
                    params={"url": "http://127.0.0.1:1/manifest.json"})
    assert r.status_code == 200
    body = r.json()
    # 127.0.0.1:1 应立即 connection refused
    assert body["code"] == 502
    assert "远端拉取失败" in body["message"]


def test_distribute_pull_bad_url_returns_502(qa_module):
    _, client, _ = qa_module
    r = client.post("/api/v1/qmt-agent/distribute/pull",
                    params={"url": "http://127.0.0.1:1/bundle.py"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 502


def test_distribute_pull_rejects_garbage_content(qa_module, monkeypatch):
    """远端返回非 bundle 内容 → 拒绝写盘。"""
    qa, client, fake_qmt = qa_module

    def fake_get(url, timeout=10.0):
        return ("this is not a bundle, just random text\nprint('hello')\n", "")

    monkeypatch.setattr(qa, "_http_get_text", fake_get)
    r = client.post("/api/v1/qmt-agent/distribute/pull",
                    params={"url": "http://example.invalid/bundle.py"})
    assert r.status_code == 200
    body = r.json()
    # 应拒绝写盘
    assert body["code"] == 400
    assert "未通过体检" in body["message"]
    # 确认目标文件不存在
    target = fake_qmt / "python" / "qmt_work_agent.py"
    assert not target.exists(), "非合法内容绝不允许写盘"


def test_distribute_pull_dry_run_ok(qa_module, monkeypatch):
    """远端 bundle 合法（用本地生成的一次）→ 拉取并 dry_run 部署。"""
    qa, client, fake_qmt = qa_module
    # 用本地 bundle 作为"远端返回"
    text, _enc, _size, _p = qa._build_bundle_text()
    assert text, "本地 bundle 应非空"

    def fake_get(url, timeout=10.0):
        return (text, "")

    monkeypatch.setattr(qa, "_http_get_text", fake_get)
    r = client.post("/api/v1/qmt-agent/distribute/pull",
                    params={"url": "http://example.invalid/bundle.py",
                            "dry_run": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["ok"] is True
    assert data["dry_run"] is True
    assert data["size_bytes"] > 10000
    # dry_run 不写盘
    target = fake_qmt / "python" / "qmt_work_agent.py"
    assert not target.exists()


def test_distribute_pull_writes_when_dry_run_false(qa_module, monkeypatch):
    qa, client, fake_qmt = qa_module
    text, _enc, _size, _p = qa._build_bundle_text()
    assert text

    def fake_get(url, timeout=10.0):
        return (text, "")

    monkeypatch.setattr(qa, "_http_get_text", fake_get)
    r = client.post("/api/v1/qmt-agent/distribute/pull",
                    params={"url": "http://example.invalid/bundle.py",
                            "dry_run": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["ok"] is True
    target = fake_qmt / "python" / "qmt_work_agent.py"
    assert target.is_file()
    assert target.stat().st_size == data["size_bytes"]


def test_deploy_rejects_path_traversal_filename(qa_module):
    """``filename`` 会被拼进 ``sdir / filename``，不校验就是任意路径写。"""
    qa, client, fake_qmt = qa_module
    for bad in ("../../evil.py", "..\\..\\evil.py", "/abs/evil.py",
                ".hidden.py", "evil.txt"):
        r = client.post("/api/v1/qmt-agent/deploy",
                        json={"filename": bad, "dry_run": False})
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 400, f"{bad} 应被拒绝，实际 code={body['code']}"
    # 拒绝即不写盘：策略目录内外都不应出现越界文件
    assert not (fake_qmt.parent / "evil.py").exists()
    assert not (fake_qmt / "evil.py").exists()


def test_distribute_pull_rejects_path_traversal_filename(qa_module, monkeypatch):
    qa, client, fake_qmt = qa_module
    text, _enc, _size, _p = qa._build_bundle_text()
    monkeypatch.setattr(qa, "_http_get_text", lambda url, timeout=10.0: (text, ""))
    r = client.post("/api/v1/qmt-agent/distribute/pull",
                    params={"url": "http://example.invalid/bundle.py",
                            "filename": "../../evil.py", "dry_run": "false"})
    assert r.status_code == 200
    assert r.json()["code"] == 400
    assert not (fake_qmt.parent / "evil.py").exists()


# ==========================================================================
# QMT 目录探测：通用判据 + 环境变量语义 + 缓存（R31，2026-10-09）
# ==========================================================================
def _mk_qmt(root, markers=("bin.x64",), strategy=True):
    """在 root 下构造一个 QMT 根：可选 python/ 与若干次级标记。"""
    root.mkdir(parents=True, exist_ok=True)
    if strategy:
        (root / "python").mkdir(exist_ok=True)
    for m in markers:
        p = root / m
        p.parent.mkdir(parents=True, exist_ok=True)
        p.mkdir(exist_ok=True)
    return root


def test_is_qmt_dir_requires_python_strategy_dir(tmp_path):
    """只有次级标记、没有 ``python/`` ⇒ **不是** QMT 根。

    这是「通行脚本」的核心判据：任何券商版本的 QMT 都有 python\\ 策略目录，
    而随便一个带 bin.x64 的目录不是。反过来「有个叫 python 的文件夹」也不算
    （必须再命中一个安装标记），否则会把用户的 Python 工程误判成 QMT。
    """
    import app.routes.qmt_agent as qa

    only_markers = _mk_qmt(tmp_path / "no_python", strategy=False)
    assert qa._is_qmt_dir(str(only_markers)) is False

    only_python = tmp_path / "just_python"
    (only_python / "python").mkdir(parents=True)
    assert qa._is_qmt_dir(str(only_python)) is False

    good = _mk_qmt(tmp_path / "good")
    assert qa._is_qmt_dir(str(good)) is True


def test_is_qmt_dir_accepts_each_secondary_marker(tmp_path):
    """次级标记里**任一**命中即可 —— 覆盖大小 QMT 与各券商变体，不写死单一名单。"""
    import app.routes.qmt_agent as qa

    for i, marker in enumerate(qa._QMT_MARKERS_SECONDARY):
        if "/" in marker or "\\" in marker:
            # 目录型标记（config/user/root）由 _mk_qmt 的 parent.mkdir 处理
            r = tmp_path / f"m{i}"
            r.mkdir(); (r / "python").mkdir()
            p = r / marker.replace("\\", "/")
            p.mkdir(parents=True)
        else:
            r = _mk_qmt(tmp_path / f"m{i}", markers=(marker,))
        assert qa._is_qmt_dir(str(r)) is True, f"标记 {marker} 未被识别"


def test_find_qmt_dir_explicit_invalid_returns_none(tmp_path, monkeypatch):
    """``explicit`` 点名要的目录不是 QMT 根 ⇒ 返回 None（**不**偷偷换别的目录）。

    与 ``QMT_DIR`` 的语义不同：explicit 是调用方的明确意图，悄悄替换成另发现
    的目录会让 API 报出「部署成功」却写到了用户没指定的位置。
    """
    import app.routes.qmt_agent as qa

    monkeypatch.setattr(qa, "_candidate_roots", lambda: [str(_mk_qmt(tmp_path / "real"))])
    qa._reset_qmt_dir_cache()
    not_qmt = tmp_path / "notqmt"
    not_qmt.mkdir()
    assert qa._find_qmt_dir(str(not_qmt)) is None
    # 反向对照：explicit 合法时必须命中它，而不是扫描结果
    real = _mk_qmt(tmp_path / "explicit_ok")
    assert qa._find_qmt_dir(str(real)) == str(real)


def test_find_qmt_dir_env_invalid_falls_back_to_scan(tmp_path, monkeypatch):
    """``QMT_DIR`` 失效（换盘位/换券商后忘了清）时**回落全盘扫描**，不静默掐死探测。

    若沿用「env 一票否决」的写法，一个陈旧环境变量会让「自动探测」永久失效，
    界面永远显示「未找到 QMT」—— 用户几乎不可能把原因归到环境变量上。
    """
    import app.routes.qmt_agent as qa

    real = _mk_qmt(tmp_path / "scanned")
    monkeypatch.setattr(qa, "_candidate_roots", lambda: [str(real)])
    monkeypatch.setenv("QMT_DIR", str(tmp_path / "gone_away"))
    qa._reset_qmt_dir_cache()
    assert qa._find_qmt_dir() == str(real)


def test_find_qmt_dir_env_valid_wins_over_scan(tmp_path, monkeypatch):
    """``QMT_DIR`` 有效时优先于扫描结果（多套 QMT 并存时由用户拍板用哪套）。"""
    import app.routes.qmt_agent as qa

    env_qmt = _mk_qmt(tmp_path / "env_qmt")
    scanned = _mk_qmt(tmp_path / "scanned")
    monkeypatch.setattr(qa, "_candidate_roots", lambda: [str(scanned)])
    monkeypatch.setenv("QMT_DIR", str(env_qmt))
    qa._reset_qmt_dir_cache()
    assert qa._find_qmt_dir() == str(env_qmt)


def test_find_qmt_dir_cache_is_resettable(tmp_path, monkeypatch):
    """探测结果被缓存，但 ``_reset_qmt_dir_cache()`` 必须能清掉。

    ★ 缓存在这里是**进程级全局**：没有重置口子的话，一个用例的结论会泄漏到下一个
    用例，制造 R20 记录过的「假绿灯」。本用例同时钉死「清缓存后必须重新扫描」。
    """
    import app.routes.qmt_agent as qa

    first = _mk_qmt(tmp_path / "first")
    monkeypatch.setattr(qa, "_candidate_roots", lambda: [str(first)])
    qa._reset_qmt_dir_cache()
    assert qa._find_qmt_dir() == str(first)

    # 换掉候选根，但缓存仍在 TTL 内 ⇒ 仍回旧结果（这正是需要重置的原因）
    second = _mk_qmt(tmp_path / "second")
    monkeypatch.setattr(qa, "_candidate_roots", lambda: [str(second)])
    assert qa._find_qmt_dir() == str(first), "TTL 内应命中缓存"

    qa._reset_qmt_dir_cache()
    assert qa._find_qmt_dir() == str(second), "清缓存后必须重新扫描"


# ---------------------------------------------------------------------------
# 诊断对「agent 自检证据」的消费（2026-10-09 真机缺陷族 D3/D4）
#
# 现场：用户看到界面「诊断通过，agent 一切正常」，可 agent 的自检明明写着
#       ``异常项=['quote_call']``（get_full_tick 实调抛「无法连接行情服务！」），
#       而「能不能下单」根本无处可查。三处断链各自独立：
#         D3 ``probe_result.json`` 没有 ``ok`` 字段，后端却读它 ⇒ probe_ok 恒 False
#            （假告状：一切正常也报「自检未通过」）；
#         D4 ``problems`` 只看 bundle/config/心跳，**不看自检** ⇒ 行情实调失败被吞；
#         且「下单能力」没有独立呈现位。
# ---------------------------------------------------------------------------
def _seed_bridge(fake_qmt: Path, probe: dict, *, alive: bool = True) -> Path:
    """写 agent_config / 心跳 / 自检（模拟 agent 真跑过一轮留下的证据）。"""
    bridge = fake_qmt / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    (fake_qmt / "python" / "agent_config.json").write_text(json.dumps({
        "bridge_dir": str(bridge).replace("\\", "/"),
        "transport": "file", "auth_token": "t", "trading_enabled": False,
    }, ensure_ascii=False), encoding="utf-8")
    (bridge / "agent_status.json").write_text(json.dumps({
        "ts": int(time.time() * 1000), "alive": alive, "agent_ver": "1.2.0",
        "runtime_mode": "standalone_process",
    }, ensure_ascii=False), encoding="utf-8")
    (bridge / "probe_result.json").write_text(
        json.dumps(probe, ensure_ascii=False), encoding="utf-8")
    return bridge


def _deploy_and_diagnose(client) -> dict:
    client.post("/api/v1/qmt-agent/deploy",
                json={"filename": "qmt_work_agent.py",
                      "strategy": "qmt_work_agent",
                      "dry_run": False, "txt_copy": False})
    r = client.post("/api/v1/qmt-agent/diagnose",
                    json={"strategy": "qmt_work_agent"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0, body
    return body["data"]


def test_probe_ok_derived_from_steps_when_agent_omits_ok(qa_module):
    """旧版 agent 不写 ``ok`` ⇒ 必须从 ``steps`` 现算，**不得**恒报 False。

    （历史行为：`probe.get("ok")` → None → bool(None) → False，
     于是界面永远显示「自检未通过」，哪怕 agent 一切正常 —— 假告状。）
    """
    _, client, fake_qmt = qa_module
    _seed_bridge(fake_qmt, probe={
        "ts": 1, "runtime_mode": "standalone_process",
        "steps": [{"name": "python_version", "ok": True},
                  {"name": "bridge_dir_write", "ok": True}],
    })
    data = _deploy_and_diagnose(client)
    assert data["heartbeat"]["probe_ok"] is True, (
        "所有 step 都 ok 且 agent 未提供 ok 字段 ⇒ 必须判通过（否则是假告状）")

    # 反向锚：有失败 step 时必须 False（别把「读不到」修成「一律通过」）
    _seed_bridge(fake_qmt, probe={
        "ts": 1, "runtime_mode": "standalone_process",
        "steps": [{"name": "python_version", "ok": True},
                  {"name": "bridge_dir_write", "ok": False, "detail": "写失败"}],
    })
    data = _deploy_and_diagnose(client)
    assert data["heartbeat"]["probe_ok"] is False


def test_diagnose_surfaces_quote_failure_and_trading_capability(qa_module):
    """行情实调失败必须进 problems；下单不可用（独立进程模式使然）走 capabilities。"""
    _, client, fake_qmt = qa_module
    _seed_bridge(fake_qmt, probe={
        "ts": 1, "runtime_mode": "standalone_process",
        "ok": False,
        "bad_steps": ["quote_call", "order_funcs", "injected_funcs"],
        "capability": {
            "trading": {"available": False,
                        "reason": "未捕获终端注入的 passorder；独立进程模式拿不到（需公式模式）",
                        "expected_in_mode": True},
            "quote": {"available": False,
                      "reason": "调用 get_full_tick 抛错: Exception: 无法连接行情服务！",
                      "expected_in_mode": False},
        },
        "steps": [
            {"name": "python_version", "ok": True},
            {"name": "quote_call", "ok": False,
             "detail": "调用 get_full_tick 抛错: Exception: 无法连接行情服务！"},
            {"name": "order_funcs", "ok": False, "detail": "本进程**不能下单**"},
        ],
    })
    data = _deploy_and_diagnose(client)

    assert data["heartbeat"]["probe_ok"] is False

    # 1) 能力真相必须在响应里显式可读（前端据此渲染「下单/行情」两行）
    caps = data["capabilities"]
    assert caps["quote"]["available"] is False
    assert caps["trading"]["available"] is False
    assert caps["trading"]["expected_in_mode"] is True

    # 2) 行情失败是**故障**⇒ 必须进 problems
    sources = {p["source"] for p in data["problems"]}
    assert "probe/quote" in sources, sources
    assert any("无法连接行情服务" in p["msg"] for p in data["problems"]), data["problems"]

    # 3) 下单能力在独立进程模式不可用属**模式定义** ⇒ 不当故障告警
    #    （否则每次 standalone 运行都飘红，是反向的「假告状」）
    assert "probe/trading" not in sources, sources
    assert not any("下单" in p["msg"] for p in data["problems"]), data["problems"]


def test_diagnose_reports_trading_fault_when_formula_mode(qa_module):
    """公式模式下**没有** passorder 才是真故障 ⇒ 必须进 problems。"""
    _, client, fake_qmt = qa_module
    _seed_bridge(fake_qmt, probe={
        "ts": 1, "runtime_mode": "qmt_formula",
        "ok": False, "bad_steps": ["order_funcs"],
        "capability": {
            "trading": {"available": False,
                        "reason": "未捕获终端注入的 passorder；终端未注入（请确认本文件是 QMT 挂载的入口）",
                        "expected_in_mode": False},
            "quote": {"available": True, "reason": "实调成功", "expected_in_mode": False},
        },
        "steps": [{"name": "order_funcs", "ok": False, "detail": "不能下单"}],
    })
    data = _deploy_and_diagnose(client)
    sources = {p["source"] for p in data["problems"]}
    assert "probe/trading" in sources, sources
    assert "probe/quote" not in sources, sources


def test_diagnose_exposes_trade_surface_as_evidence(qa_module):
    """★ 下单接口面明细必须贯通到诊断响应（此前只落在 probe_result.json 里被脚本消费）。

    ``capabilities.trading`` 给**结论**（能不能下单），``trade_surface`` 给**依据**
    （终端注入了哪些下单入口、还缺哪些）。少了后者，用户只能读到「下单能力：不可用」，
    却无从知道缺的是 ``passorder`` 还是 ``cancel_order_stock`` —— 排障信息在 API 层断链。
    """
    _, client, fake_qmt = qa_module
    _seed_bridge(fake_qmt, probe={
        "ts": 1, "runtime_mode": "standalone_process",
        "ok": False, "bad_steps": ["order_funcs"],
        "capability": {
            "trading": {"available": False,
                        "reason": "未捕获终端注入的 passorder",
                        "expected_in_mode": True},
            "quote": {"available": True, "reason": "", "expected_in_mode": False},
        },
        "trade_surface": {"present": [],
                          "missing": ["passorder", "cancel_order_stock"],
                          "can_submit": False},
    })
    data = _deploy_and_diagnose(client)

    ts = data.get("trade_surface")
    assert ts is not None, "trade_surface 必须随诊断响应下发（界面据此展示依据）"
    assert ts["can_submit"] is False
    assert "passorder" in ts["missing"], ts
    assert ts["present"] == [], ts


def test_diagnose_trade_surface_absent_is_empty_not_missing(qa_module):
    """反腐烂锚：probe 没有 trade_surface 时，响应里的键仍要存在（空 dict）。

    若实现改成「没有就不给键」，前端 `trade_surface?.present` 与
    `trade_surface === undefined` 会被混为一谈 ⇒ 「旧版 agent」和
    「agent 上报了但确实一个入口都没有」两种情况无法区分。
    """
    _, client, fake_qmt = qa_module
    _seed_bridge(fake_qmt, probe={
        "ts": 1, "runtime_mode": "standalone_process", "ok": True,
        "steps": [{"name": "python_version", "ok": True}],
    })
    data = _deploy_and_diagnose(client)
    assert "trade_surface" in data, "键必须存在"
    assert data["trade_surface"] == {}, data["trade_surface"]


def test_bundle_pull_timeout_reads_settings(qa_module, monkeypatch):
    """★ 孤儿配置治理：`qmt_agent_bundle_timeout` 必须真的被拉取路径读取。

    此前该旋钮**只声明不读取**，拉取处硬编码 ``timeout=30.0`` ⇒ 运维改了环境变量
    毫无效果，且注释声称的默认 10s 与真实行为矛盾。接线后必须锁住：
    settings 改了，拉取超时就跟着变。
    """
    qa, _client, _fake = qa_module
    from core.config import settings as real_settings

    assert getattr(real_settings, "qmt_agent_bundle_timeout", None) is not None
    monkeypatch.setattr(real_settings, "qmt_agent_bundle_timeout", 7.5, raising=False)
    assert qa._bundle_timeout_from_settings() == 7.5


def test_bundle_pull_timeout_falls_back_on_garbage(qa_module, monkeypatch):
    """反腐烂锚：配置被填成非数字/0 时回退 30s，不得变成 0（0 = 立即超时）。"""
    qa, _client, _fake = qa_module
    from core.config import settings as real_settings

    for bad in (0, "abc", None, -1):
        monkeypatch.setattr(real_settings, "qmt_agent_bundle_timeout", bad,
                            raising=False)
        v = qa._bundle_timeout_from_settings()
        assert v == 30.0, "非法值 %r 应回退 30s，实得 %r" % (bad, v)
