"""``/api/v1/qmt/*`` 路由集成测试（安装台账 / 端口方案 / 文件单）。

设计原则（对齐 test_qmt_agent_routes.py）：
- 最小 FastAPI 装配 + ``EnvelopeMiddleware``。**必须**挂中间件：``err(503, ...)``
  的 HTTP 状态码由中间件提升（``middleware/envelope.py::_promote_status``），
  裸 ``include_router`` 测到的是"裸 router 的 200"，状态码断言会变成假绿。
- 用 tmp_path 造**真**安装目录（``bin.x64/XtItClient.exe`` + ``config/broker.ini``
  + ``userdata/users/<账号>/Config.xml``），不 mock 业务逻辑 —— 发现、读端口、
  读监控设置、写 DBF、回读核对全部走真实实现。
- 只替换两处：``export_dir``（staging 落 tmp_path）与 ``_win_listening_ports``
  （不能让单测依赖本机真在跑 QMT）。它们本来就该在测试态指向不同地方。

盯的不变量：
* roots 三态：``explicit`` / ``from_connectors`` / ``none`` —— 第三种**不能**
  被说成"扫了全盘没找到"；
* 安装根与父目录两种写法都必须能发现（只调 ``discover`` 会漏掉前者）；
* 活体探针不可用时 ``live_probe="unavailable"``，**不得**变成绿灯；
* 端口方案自撞/非法 stride ⇒ 400，不返回"看着能跑"的表；
* ``armed`` 反映 ``is_automonitor``，全关时必须给 warning；
* stage **只落 staging**：``allow_monitor_dir`` 一律拒绝，staging 落在监控目录内
  必须拒绝生成（生成即下单）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.middleware.envelope import EnvelopeMiddleware  # noqa: E402

BROKER_INI = """[broker]
pid=1
port=56500

[formulaworker]
pbserverport=59600
serverport=58600

[minibroker]
pid=1
port=55500
"""


def _config_xml(monitor: str, backup: str, *, automonitor: str = "0") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<TTUserConfigFile>\n'
        '    <DbfMonitorSetting is_incexport="0" useringbuffer="0"'
        ' need_remove_exprotfile_fristmonitor="1" is_usesettingfeedbackval="0"'
        ' orderidfeedbackval="-1" is_automonitor="%s" is_clear="0"'
        ' monitor_path="%s" clear_date="" is_sharedread="0" backup_path="%s"/>\n'
        '    <CsvMonitorSetting is_automonitor="0" monitor_path="%s"/>\n'
        '    <batchOrderSetting key_sequence="" monitor_path="%s" key_enable="0"/>\n'
        '</TTUserConfigFile>\n' % (automonitor, monitor, backup, monitor, monitor)
    ).encode("utf-8")


def _make_install(root: Path, *, account: str = "410001005814",
                  automonitor: str = "0") -> Path:
    """造一套最小可识别的 QMT 安装。"""
    (root / "bin.x64").mkdir(parents=True)
    (root / "bin.x64" / "XtItClient.exe").write_bytes(b"MZ" + b"\x00" * 32)
    (root / "config").mkdir(parents=True)
    (root / "config" / "broker.ini").write_text(BROKER_INI, encoding="utf-8")
    udir = root / "userdata" / "users" / account
    udir.mkdir(parents=True)
    (udir / "Config.xml").write_bytes(_config_xml(
        str(root / "export_data"), str(root / "export_data_backup"),
        automonitor=automonitor))
    return root


class _FakeManager:
    """只提供 ``all_connections()`` 的上下文替身（用来测 roots 的三态）。"""

    def __init__(self, conns=None):
        self._conns = list(conns or [])

    def all_connections(self):
        return list(self._conns)


class _FakeCtx:
    def __init__(self, conns=None):
        self.broker_manager = _FakeManager(conns)


class _Conn:
    def __init__(self, client_path: str = "", bridge_dir: str = ""):
        self.cfg = type("Cfg", (), {"client_path": client_path,
                                    "bridge_dir": bridge_dir})()


@pytest.fixture
def qc(tmp_path, monkeypatch):
    """装配路由 + 把 staging 指到 tmp_path；返回 (module, client, staging)。"""
    sys.modules.pop("app.routes.qmt_client", None)
    import app.routes.qmt_client as mod
    from core.context import get_ctx

    staging = tmp_path / "staging"
    monkeypatch.setattr(mod, "export_dir", lambda: staging)

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/v1")
    app.add_middleware(EnvelopeMiddleware)
    app.dependency_overrides[get_ctx] = lambda: _FakeCtx()
    return mod, TestClient(app), staging


def _rows(n: int = 1) -> list[dict]:
    return [{"stock_code": "600036.SH", "volume": 100, "order_type": 23,
             "price_type": 5, "mode_price": 10.5,
             "account_id": "410001005814"} for _ in range(n)]


# ----------------------------------------------------------------------
# roots 三态
# ----------------------------------------------------------------------
def test_installs_without_roots_says_none(qc):
    """没有任何 roots 来源时必须是 source="none" + hint，不能假装"扫过"。"""
    _, client, _ = qc
    r = client.get("/api/v1/qmt/installs")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["source"] == "none"
    assert data["count"] == 0
    assert data["hint"], "没有 roots 时必须告诉用户怎么给"
    assert data["live_probe"] == "skipped"


def test_installs_roots_from_configured_connector(qc, tmp_path):
    """从已配置连接的 client_path 回溯安装根，再取父目录 —— 通用、不写死盘符。"""
    mod, client, _ = qc
    from core.context import get_ctx
    inst = _make_install(tmp_path / "stock" / "gd_qmt")
    # 连接里存的是 userdata 路径（真实项目里就是这样），必须能回溯到安装根
    client.app.dependency_overrides[get_ctx] = lambda: _FakeCtx(
        [_Conn(client_path=str(inst / "userdata"))])

    data = client.get("/api/v1/qmt/installs").json()["data"]
    assert data["source"] == "from_connectors"
    assert data["count"] == 1
    assert Path(data["installs"][0]["root"]).name == "gd_qmt"


def test_installs_accepts_parent_and_install_root(qc, tmp_path):
    """roots 给**父目录**和给**安装根本身**都必须能发现。

    只调 ``discover``（语义=扫子目录）时，用户把安装根填进来会得到空列表 ——
    界面显示"没找到任何 QMT"，而路径完全正确。
    """
    _, client, _ = qc
    parent = tmp_path / "stock"
    _make_install(parent / "gd_qmt")
    _make_install(parent / "guoxin_11", account="15624979679")

    by_parent = client.get("/api/v1/qmt/installs", params={"roots": str(parent)}).json()["data"]
    assert by_parent["count"] == 2 and by_parent["source"] == "explicit"

    by_self = client.get("/api/v1/qmt/installs",
                         params={"roots": str(parent / "gd_qmt")}).json()["data"]
    assert by_self["count"] == 1
    assert Path(by_self["installs"][0]["root"]).name == "gd_qmt"


@pytest.mark.parametrize("rel", ["userdata", "bin.x64",
                                 "bin.x64/XtItClient.exe"])
def test_installs_accepts_any_client_path_as_root(qc, tmp_path, rel):
    """用户把 userdata / bin.x64 / exe 本身填进 roots 也必须能回溯到安装根。

    §15.3 承诺"任意入口向上定位"，但旧实现把 find_install_root 的 max_up 设成 0，
    这三种路径都会被误当成父目录交给 discover ⇒ 界面显示"没找到任何 QMT"。
    """
    _, client, _ = qc
    inst = _make_install(tmp_path / "stock" / "gd_qmt")
    data = client.get("/api/v1/qmt/installs",
                      params={"roots": str(inst / rel)}).json()["data"]
    assert data["count"] == 1, f"roots={rel} 没有回溯到安装根"
    assert Path(data["installs"][0]["root"]).name == "gd_qmt"


def test_installs_dedups_overlapping_roots(qc, tmp_path):
    """父目录与它自己的子安装同时传入时不能重复计数。"""
    _, client, _ = qc
    parent = tmp_path / "stock"
    _make_install(parent / "gd_qmt")
    data = client.get("/api/v1/qmt/installs", params={
        "roots": f"{parent},{parent / 'gd_qmt'}"}).json()["data"]
    assert data["count"] == 1


def test_installs_empty_dir_reports_hint_not_silence(qc, tmp_path):
    """目录存在但没有安装 ⇒ count=0 + hint，明确"判据是 bin.x64/XtItClient.exe"。"""
    _, client, _ = qc
    empty = tmp_path / "empty"
    empty.mkdir()
    data = client.get("/api/v1/qmt/installs", params={"roots": str(empty)}).json()["data"]
    assert data["count"] == 0
    assert "XtItClient.exe" in data["hint"]


# ----------------------------------------------------------------------
# 活体探针三态
# ----------------------------------------------------------------------
def test_installs_live_unavailable_is_not_a_green_light(qc, tmp_path, monkeypatch):
    """netstat 拿不到结果 ⇒ live_probe="unavailable"，绝不能读成"无冲突"。"""
    mod, client, _ = qc
    _make_install(tmp_path / "stock" / "gd_qmt")
    import connectors.install_registry as reg
    monkeypatch.setattr(reg, "_win_listening_ports", lambda: None)

    data = client.get("/api/v1/qmt/installs",
                      params={"roots": str(tmp_path / "stock"), "live": 1}).json()["data"]
    assert data["live_probe"] == "unavailable"
    assert data["live_checked"] is False
    assert data["double_bound"] == {}
    assert data["ok"] is False, "★ 要求了活体检查却没做成，ok 不能还是绿灯"
    assert data["ok_basis"] == "live_unavailable"


def test_installs_live_detects_double_bound(qc, tmp_path, monkeypatch):
    """两个 PID 抢同一端口 = 串户风险，必须报出来。"""
    mod, client, _ = qc
    _make_install(tmp_path / "stock" / "gd_qmt")
    import connectors.install_registry as reg
    monkeypatch.setattr(reg, "_win_listening_ports",
                        lambda: {58600: ["24544", "8828"], 8086: ["8828"]})

    data = client.get("/api/v1/qmt/installs",
                      params={"roots": str(tmp_path / "stock"), "live": 1}).json()["data"]
    assert data["live_probe"] == "ok" and data["live_checked"] is True
    assert data["double_bound"]["58600"] == ["24544", "8828"]
    assert data["ok"] is False


def test_installs_declared_conflict_between_two_installs(qc, tmp_path):
    """两套安装声明了同一组端口 ⇒ 静态审计就要报红（无需活体探针）。"""
    _, client, _ = qc
    parent = tmp_path / "stock"
    _make_install(parent / "a")
    _make_install(parent / "b")
    data = client.get("/api/v1/qmt/installs", params={"roots": str(parent)}).json()["data"]
    assert data["count"] == 2
    assert data["declared_conflicts"], "同组端口的两个安装必须报冲突"
    assert data["ok"] is False


# ----------------------------------------------------------------------
# 端口方案
# ----------------------------------------------------------------------
def test_port_plan_offsets_each_install(qc, tmp_path):
    _, client, _ = qc
    parent = tmp_path / "stock"
    _make_install(parent / "a")
    _make_install(parent / "b")
    data = client.get("/api/v1/qmt/installs/port-plan",
                      params={"roots": str(parent), "stride": 100}).json()["data"]
    assert data["count"] == 2
    plan = data["plan"]
    assert len(plan) == 2
    serverports = [v["formulaworker.serverport"] for v in plan.values()]
    assert len(set(serverports)) == 2, "两套安装的 serverport 必须错开"
    assert max(serverports) - min(serverports) == 100
    assert "不改任何 broker.ini" in data["note"], "方案不等于落盘，必须说清楚"


def test_port_plan_rejects_bad_stride(qc, tmp_path):
    _, client, _ = qc
    _make_install(tmp_path / "stock" / "a")
    body = client.get("/api/v1/qmt/installs/port-plan",
                      params={"roots": str(tmp_path / "stock"), "stride": 0}).json()
    assert body["code"] == 400
    assert "stride" in body["message"]


def test_port_plan_overflow_is_400_not_a_broken_table(qc, tmp_path):
    """stride 大到端口溢出时必须报错，而不是返回一张跑不了的表。"""
    _, client, _ = qc
    parent = tmp_path / "stock"
    _make_install(parent / "a")
    _make_install(parent / "b")
    body = client.get("/api/v1/qmt/installs/port-plan",
                      params={"roots": str(parent), "stride": 60000}).json()
    assert body["code"] == 400
    assert "端口" in body["message"]


# ----------------------------------------------------------------------
# 文件单监控目录
# ----------------------------------------------------------------------
def test_monitors_reports_armed_false_and_warns(qc, tmp_path):
    """实测四套安装全部 is_automonitor=0 ⇒ 必须给 warning，别让人以为会下单。"""
    _, client, _ = qc
    _make_install(tmp_path / "stock" / "a", automonitor="0")
    data = client.get("/api/v1/qmt/file-order/monitors",
                      params={"roots": str(tmp_path / "stock")}).json()["data"]
    assert data["total"] == 1
    assert data["armed_count"] == 0
    assert data["accounts"][0]["armed"] is False
    assert "自动监控" in data["warning"]
    assert data["monitor_paths"], "监控目录要一并返回，调用方才能做自检"


def test_monitors_reports_armed_true_without_warning(qc, tmp_path):
    _, client, _ = qc
    _make_install(tmp_path / "stock" / "a", automonitor="1")
    data = client.get("/api/v1/qmt/file-order/monitors",
                      params={"roots": str(tmp_path / "stock")}).json()["data"]
    assert data["armed_count"] == 1
    assert "warning" not in data


def test_monitors_reports_batch_order_channel(qc, tmp_path):
    """实测四套安装的 Config.xml 都有 <batchOrderSetting>：外部下单不止 DBF 一条。

    字段集未解 ⇒ 必须标 implemented=False、key_enable 如实为 0，
    不能让"发现了通道"被读成"能用它下单"。
    """
    _, client, _ = qc
    _make_install(tmp_path / "stock" / "a")
    data = client.get("/api/v1/qmt/file-order/monitors",
                      params={"roots": str(tmp_path / "stock")}).json()["data"]
    assert data["channels"]["dbf_file_order"] == 1
    assert data["channels"]["batch_order_csv"] == 1
    assert data["batch_order_enabled"] == 0
    assert data["batch_order"][0]["implemented"] is False
    assert "不提供写入" in data["batch_order_note"]


def test_monitors_lists_every_account(qc, tmp_path):
    """一个安装多个登录账号时，每个账号各有自己的监控目录，漏一个就漏一个下单口。"""
    _, client, _ = qc
    root = _make_install(tmp_path / "stock" / "a", account="111")
    second = root / "userdata" / "users" / "222"
    second.mkdir()
    (second / "Config.xml").write_bytes(_config_xml(
        str(root / "export_data"), str(root / "export_data_backup"), automonitor="1"))
    data = client.get("/api/v1/qmt/file-order/monitors",
                      params={"roots": str(tmp_path / "stock")}).json()["data"]
    assert sorted(r["account"] for r in data["accounts"]) == ["111", "222"]
    assert data["armed_count"] == 1


# ----------------------------------------------------------------------
# stage：只落 staging
# ----------------------------------------------------------------------
def test_stage_writes_dbf_and_reads_back(qc):
    mod, client, staging = qc
    body = client.post("/api/v1/qmt/file-order/stage", json={"rows": _rows(2)}).json()
    assert body["code"] == 0, body
    data = body["data"]
    assert Path(data["path"]).parent == staging / mod.STAGING_SUBDIR
    assert data["rows"] == 2
    assert data["columns"][0] == "ORDER_TYPE"
    assert len(data["readback"]) == 2
    assert data["readback"][0]["STOCK_CODE"] == "600036.SH"
    assert data["readback"][0]["MODE_PRICE"] == "10.5", "C 型列浮点不得被取整"
    assert "未交付" in data["delivery"]


def test_stage_rejects_allow_monitor_dir(qc):
    """API 不提供"一步到位的交付"：这一步只能由人在客户端里做。"""
    _, client, _ = qc
    body = client.post("/api/v1/qmt/file-order/stage",
                       json={"rows": _rows(), "allow_monitor_dir": True}).json()
    assert body["code"] == 400
    assert "allow_monitor_dir" in body["message"]


def test_stage_rejects_bad_rows(qc):
    _, client, _ = qc
    assert client.post("/api/v1/qmt/file-order/stage", json={"rows": []}).json()["code"] == 400
    assert client.post("/api/v1/qmt/file-order/stage", json={}).json()["code"] == 400
    bad = client.post("/api/v1/qmt/file-order/stage",
                      json={"rows": [{"stock_code": "600036", "volume": 0,
                                      "order_type": 23, "price_type": 5}]}).json()
    assert bad["code"] == 400 and "校验失败" in bad["message"]


def test_stage_rejects_too_many_rows(qc):
    """文件单是丢文件即下单 —— 一次几千行等于批量误单，必须设上限并如实报错。"""
    mod, client, _ = qc
    body = client.post("/api/v1/qmt/file-order/stage",
                       json={"rows": _rows(mod.MAX_STAGE_ROWS + 1)}).json()
    assert body["code"] == 400
    assert str(mod.MAX_STAGE_ROWS) in body["message"]


def test_stage_refuses_when_staging_inside_monitor_dir(qc, tmp_path, monkeypatch):
    """staging 落在监控目录内 ⇒ 生成即下单，必须拒绝。

    真实场景：用户把 ``export_dir`` 指到了某个客户端的 ``export_data``。
    """
    mod, client, staging = qc
    root = _make_install(tmp_path / "stock" / "a")
    monkeypatch.setattr(mod, "export_dir", lambda: root / "export_data")

    body = client.post("/api/v1/qmt/file-order/stage",
                       params={"roots": str(tmp_path / "stock")},
                       json={"rows": _rows()}).json()
    assert body["code"] == 400
    assert "生成即等于下单" in body["message"]
    assert not (root / "export_data").exists(), "拒绝时不能留下目录或文件"


def test_stage_sanitizes_name(qc):
    """name 里的路径成分被 basename 掉 —— 不允许跳出 staging。"""
    mod, client, staging = qc
    data = client.post("/api/v1/qmt/file-order/stage",
                       json={"rows": _rows(), "name": "../../evil.dbf"}).json()["data"]
    assert Path(data["path"]).parent == staging / mod.STAGING_SUBDIR
    assert Path(data["path"]).name == "evil.dbf"


def test_stage_autoname_ends_with_dbf(qc):
    _, client, _ = qc
    data = client.post("/api/v1/qmt/file-order/stage",
                       json={"rows": _rows(), "name": "mytest"}).json()["data"]
    assert data["name"].endswith(".dbf")


# ----------------------------------------------------------------------
# 能力暴露边界
# ----------------------------------------------------------------------
def test_qmt_endpoints_are_not_auto_exposed_to_agent():
    """``/qmt/*`` 是**宿主级诊断**：枚举安装目录、读 broker.ini、跑 netstat。

    它不产生委托，但会枚举文件系统并拉起子进程 —— 自动暴露给 Agent 等于给它
    一个任意目录探测器。故 ``qmt`` 域整体进 ``_EXEC_DENY_DOMAINS``，
    REST 可用、MCP tool 不生成。这条断言防的是"以后有人顺手把它加回白名单"。
    """
    from app.capabilities import build_capabilities

    qmt = [c for c in build_capabilities() if c.path.startswith("/api/v1/qmt/")]
    assert qmt, "路由没注册上？"
    leaked = [f"{c.method} {c.path}" for c in qmt if c.agent_visible]
    assert not leaked, f"这些端点被自动暴露给 Agent 了：{leaked}"


# ----------------------------------------------------------------------
# 模板校准：列宽不能靠猜
# ----------------------------------------------------------------------
def _write_template(path: Path, cols, widths=None) -> Path:
    """写一份模板 DBF（列名/列宽可指定），模拟客户端导出的模板。"""
    from connectors.qmt_file_order import DbfField, write_dbf

    widths = widths or {}
    fields = [DbfField(name=c, type="C", length=widths.get(c, 16)) for c in cols]
    write_dbf(str(path), [{c: "x" for c in cols}], fields)
    return path


def test_schema_reports_missing_columns(qc):
    """模板只带 3 列 ⇒ usable=false，并逐条列出缺哪 12 列。

    缺列时**不能**按部分列写：客户端按列序读，少一列就是后面全部错位。
    """
    mod, client, staging = qc
    staging.mkdir(parents=True, exist_ok=True)
    tpl = _write_template(staging / "tpl.dbf", ["STOCK_CODE", "VOLUME", "ORDER_TYPE"])
    data = client.post("/api/v1/qmt/file-order/schema",
                       json={"template_path": str(tpl)}).json()["data"]
    assert data["usable"] is False
    assert data["count"] == 3
    assert "ACCOUNT_ID" in data["missing"]
    assert len(data["missing"]) == 12
    assert data["extra"] == []
    assert "错位" in data["note"]


def test_schema_full_template_is_usable(qc):
    mod, client, staging = qc
    staging.mkdir(parents=True, exist_ok=True)
    from connectors.qmt_file_order import DBF_COLUMNS
    tpl = _write_template(staging / "tpl.dbf", DBF_COLUMNS)
    data = client.post("/api/v1/qmt/file-order/schema",
                       json={"template_path": str(tpl)}).json()["data"]
    assert data["usable"] is True
    assert data["missing"] == []
    assert data["count"] == len(DBF_COLUMNS)


def test_schema_refuses_path_outside_allowed_dirs(qc, tmp_path):
    """不允许当任意文件读取器用：模板必须在安装目录或 export_dir 下。"""
    mod, client, staging = qc
    outside = tmp_path / "elsewhere.dbf"
    outside.write_bytes(b"\x03" + b"\x00" * 40)
    body = client.post("/api/v1/qmt/file-order/schema",
                       json={"template_path": str(outside)}).json()
    assert body["code"] == 400
    assert "只允许" in body["message"]


def test_schema_rejects_missing_and_empty(qc):
    _, client, _ = qc
    assert client.post("/api/v1/qmt/file-order/schema", json={}).json()["code"] == 400


def test_stage_rejects_partial_template(qc):
    """模板缺列 ⇒ stage 必须拒绝，而不是按部分列写出一张错位的表。"""
    mod, client, staging = qc
    staging.mkdir(parents=True, exist_ok=True)
    tpl = _write_template(staging / "tpl.dbf", ["STOCK_CODE", "VOLUME"])
    body = client.post("/api/v1/qmt/file-order/stage",
                       json={"rows": _rows(), "template_path": str(tpl)}).json()
    assert body["code"] == 400
    assert "缺列" in body["message"]


def test_stage_uses_template_widths(qc):
    """带模板时按**模板的真实列宽**写，而不是默认值。"""
    mod, client, staging = qc
    staging.mkdir(parents=True, exist_ok=True)
    from connectors.qmt_file_order import DBF_COLUMNS, read_schema
    # INSERTTIME 默认值是 23 字符，模板列宽必须给够；给 16 会被静默截断成
    # "2026-11-09 08:39" —— 真实踩过，现在应当直接拒绝（见下一条例）。
    tpl = _write_template(staging / "tpl.dbf", DBF_COLUMNS,
                          widths={"STOCK_CODE": 10, "ACCOUNT_ID": 12,
                                  "INSERTTIME": 24})
    data = client.post("/api/v1/qmt/file-order/stage",
                       json={"rows": _rows(), "template_path": str(tpl),
                             "name": "calibrated"}).json()["data"]
    written = {f.name: f.length for f in read_schema(data["path"])}
    assert written["STOCK_CODE"] == 10
    assert written["ACCOUNT_ID"] == 12
    assert data["readback"][0]["STOCK_CODE"] == "600036.SH"


def test_stage_refuses_template_that_would_truncate(qc):
    """★ 列宽不足时拒绝写单，而不是把时间戳/价格悄悄切短。

    DBF 对截断毫无怨言，客户端也照样读 —— 这种"文件合法但内容错"是最难查的。
    """
    mod, client, staging = qc
    staging.mkdir(parents=True, exist_ok=True)
    from connectors.qmt_file_order import DBF_COLUMNS
    tpl = _write_template(staging / "narrow.dbf", DBF_COLUMNS,
                          widths={"INSERTTIME": 16, "MODE_PRICE": 2})
    body = client.post("/api/v1/qmt/file-order/stage",
                       json={"rows": _rows(), "template_path": str(tpl),
                             "name": "narrow"}).json()
    assert body["code"] == 400
    assert "截断" in body["message"]


def test_stage_refuses_silent_overwrite(qc):
    """待核对文件被同名覆盖 = 人核对过的那份被换掉，交付时看不出异常。"""
    _, client, _ = qc
    payload = {"rows": _rows(), "name": "review_me.dbf"}
    assert client.post("/api/v1/qmt/file-order/stage", json=payload).json()["code"] == 0
    again = client.post("/api/v1/qmt/file-order/stage", json=payload).json()
    assert again["code"] == 400
    assert "已存在" in again["message"]


def test_stage_warns_when_monitor_check_is_vacuous(qc):
    """roots 解析不出来且附近无 Config.xml ⇒ monitor_checked=false + 明确 warning。"""
    _, client, _ = qc
    data = client.post("/api/v1/qmt/file-order/stage",
                       json={"rows": _rows()}).json()["data"]
    assert data["monitor_checked"] is False
    assert "没有做过" in data["warning"]


def test_stage_detects_monitor_dir_without_roots(qc, tmp_path, monkeypatch):
    """★ 不给 roots 也要能挡住：从 staging 路径自身向上读 Config.xml。

    旧实现只从 roots 扫到的安装里取监控目录，roots 为空时检查是在空集上做的。
    """
    mod, client, staging = qc
    root = _make_install(tmp_path / "stock" / "a")
    # export_dir 指到监控目录内部；不传 roots，连接里也不配置任何客户端
    monkeypatch.setattr(mod, "export_dir", lambda: root / "export_data")
    body = client.post("/api/v1/qmt/file-order/stage",
                       json={"rows": _rows()}).json()
    assert body["code"] == 400
    assert "生成即等于下单" in body["message"]
