"""多 QMT 安装注册表：**不静默择一**，撞端口必须响亮。

背景（2026-11-08 实测）：本机两套 QMT 同时运行，``config/broker.ini`` 逐字节相同，
两个 ``XtItClient.exe`` 同时 ``LISTENING 0.0.0.0:58600`` —— 连接落到哪家不确定。
对只读行情是"数据可能来自另一家"，对写操作就是串户。

盯的不变量：
* ``discover`` 只认 ``bin.x64/XtItClient.exe``，非安装目录**安静跳过**（正常情况）；
* ``read_declared_ports`` 只认 ``port`` / ``pbserverport`` / ``serverport`` 三个键，
  非法值**跳过而不是当 0**（"没配"与"配了 0"是两类故障）；
* ``conflicts`` 只比对配置里的**字面值**，不做启发式推断；
* ``plan_ports`` 必须给出互不冲突的方案，方案自撞就抛错（不返回"看着能跑"的表）；
* ``parse_netstat_listeners`` 只算 ``LISTENING`` —— 把 ESTABLISHED 的临时端口
  算成监听会让冲突检测满屏假红。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.install_registry import (  # noqa: E402
    InstallError,
    audit,
    conflicts,
    decode_console_bytes,
    discover,
    double_bound,
    find_install_root,
    load_install,
    parse_netstat_listeners,
    plan_ports,
    read_declared_ports,
    read_xtdata_addr,
)

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

XTDATA_INI = """[client_xtdata]
address=127.0.0.1:58610
"""


def _make_install(root: Path, *, broker: str = BROKER_INI, xtdata: str = XTDATA_INI,
                  mini: bool = False, datacenter: bool = False,
                  pyd_year: int = 2025) -> Path:
    (root / "bin.x64").mkdir(parents=True)
    (root / "bin.x64" / "XtItClient.exe").write_bytes(b"MZ" + b"\x00" * 32)
    xt = root / "bin.x64" / "Lib" / "site-packages" / "xtquant"
    xt.mkdir(parents=True)
    pyd = xt / "xtpythonclient.cp311-win_amd64.pyd"
    pyd.write_bytes(b"\x00" * 16)
    import datetime
    import os
    ts = datetime.datetime(pyd_year, 6, 1).timestamp()
    os.utime(pyd, (ts, ts))
    if datacenter:
        (xt / "xtdatacenter.py").write_text("# stub\n", encoding="utf-8")
    if xtdata is not None:
        (xt / "xtdata.ini").write_text(xtdata, encoding="utf-8")
    if mini:
        (root / "bin.x64" / "XtMiniQmt.exe").write_bytes(b"MZ")
        (root / "bin.x64" / "minibroker.exe").write_bytes(b"MZ")
    (root / "config").mkdir()
    (root / "config" / "broker.ini").write_text(broker, encoding="utf-8")
    return root


def test_discover_only_recognises_client_exe(tmp_path):
    _make_install(tmp_path / "broker_a")
    (tmp_path / "not_qmt").mkdir()
    (tmp_path / "not_qmt" / "readme.txt").write_text("x", encoding="utf-8")

    found = discover([str(tmp_path)])
    assert [i.label for i in found] == ["broker_a"], "非安装目录必须被安静跳过"


def test_load_install_rejects_non_install(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(InstallError):
        load_install(str(tmp_path / "empty"))


def test_read_declared_ports_only_port_keys(tmp_path):
    ini = _make_install(tmp_path / "a") / "config" / "broker.ini"
    ports = read_declared_ports(str(ini))
    assert ports == {
        "broker.port": 56500,
        "formulaworker.pbserverport": 59600,
        "formulaworker.serverport": 58600,
        "minibroker.port": 55500,
    }
    assert "broker.pid" not in ports, "pid 不是端口，不能被当成端口"


def test_read_declared_ports_skips_invalid_instead_of_zero(tmp_path):
    ini = tmp_path / "broker.ini"
    ini.write_text("[a]\nport=abc\n[b]\nport=0\n[c]\nport=70000\n[d]\nport=8080\n",
                   encoding="utf-8")
    assert read_declared_ports(str(ini)) == {"d.port": 8080}, \
        "非法/越界端口必须跳过 —— 当 0 会让'没配'伪装成'配了但没起'"


def test_read_xtdata_addr(tmp_path):
    _make_install(tmp_path / "a")
    addr = read_xtdata_addr(str(tmp_path / "a" / "bin.x64" / "Lib" / "site-packages"
                                 / "xtquant" / "xtdata.ini"))
    assert addr == "127.0.0.1:58610"
    assert read_xtdata_addr(str(tmp_path / "missing.ini")) == ""


def test_generation_and_mini_capability(tmp_path):
    a = load_install(str(_make_install(tmp_path / "gd", mini=True, datacenter=True,
                                       pyd_year=2025)))
    b = load_install(str(_make_install(tmp_path / "gx", mini=False, datacenter=False,
                                       pyd_year=2023)))
    assert a.generation == "G2025" and a.supports_mini is True
    assert b.generation == "G2023" and b.supports_mini is False, \
        "缺 XtMiniQmt.exe/minibroker.exe 就不能声称支持极简模式"


def test_conflicts_detects_identical_declared_ports(tmp_path):
    a = load_install(str(_make_install(tmp_path / "a")))
    b = load_install(str(_make_install(tmp_path / "b")))
    got = conflicts([a, b])
    ports = sorted(c.port for c in got)
    assert ports == [55500, 56500, 58600, 59600], f"应报出全部撞车端口，实际 {ports}"
    assert all({c.left, c.right} == {"a", "b"} for c in got)
    assert "58600" in got[0].describe() or any("58600" in c.describe() for c in got)


def test_conflicts_only_reports_shared_ports(tmp_path):
    other = BROKER_INI.replace("58600", "58699")
    a = load_install(str(_make_install(tmp_path / "a")))
    b = load_install(str(_make_install(tmp_path / "b", broker=other)))
    # 只有 serverport 改过 ⇒ 其余三处仍然撞
    assert {c.port for c in conflicts([a, b])} == {56500, 59600, 55500}
    assert 58600 not in {c.port for c in conflicts([a, b])}


def test_plan_ports_is_disjoint_and_shifts_xtdata(tmp_path):
    a = load_install(str(_make_install(tmp_path / "a")))
    b = load_install(str(_make_install(tmp_path / "b")))
    plan = plan_ports([a, b], stride=100)

    assert plan["a"]["formulaworker.serverport"] == 58600, "第 0 套保持原样"
    assert plan["b"]["formulaworker.serverport"] == 58700
    assert plan["a"]["__xtdata_port__"] == 58610
    assert plan["b"]["__xtdata_port__"] == 58710, "xtdata 端口必须与 broker 成对偏移"

    flat = [p for row in plan.values() for p in row.values()]
    assert len(flat) == len(set(flat)), f"端口方案内部自撞: {flat}"


def test_plan_ports_rejects_self_collision(tmp_path):
    """stride 把两套的端口**推到同一个号**时必须响亮失败。"""
    a_ini = BROKER_INI
    b_ini = BROKER_INI.replace("58600", "58599")   # 与 a 相差 1
    a = load_install(str(_make_install(tmp_path / "a", broker=a_ini)))
    b = load_install(str(_make_install(tmp_path / "b", broker=b_ini)))
    # stride=1 ⇒ b 的 58599 变成 58600，与 a 的 58600 撞
    with pytest.raises(InstallError, match="自撞"):
        plan_ports([a, b], stride=1)


def test_plan_ports_rejects_overflow(tmp_path):
    a = load_install(str(_make_install(tmp_path / "a")))
    b = load_install(str(_make_install(tmp_path / "b")))
    with pytest.raises(InstallError, match="超出端口上限"):
        plan_ports([a, b], stride=65535)


def test_plan_ports_rejects_duplicate_labels(tmp_path):
    """两套安装目录同名 ⇒ 按 label 做键会静默覆盖，必须直接判错。

    少了这一条，界面会显示 count=4 却只给 3 套端口方案，而没有任何报错 ——
    多券商并行里"悄悄漏配一家"比明着失败更危险。
    """
    from dataclasses import replace

    a = load_install(str(_make_install(tmp_path / "x" / "qmt")))
    b = replace(load_install(str(_make_install(tmp_path / "y" / "qmt"))),
                label=a.label)
    assert a.label == b.label and a.root != b.root
    with pytest.raises(InstallError, match="同名"):
        plan_ports([a, b])


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_plan_ports_rejects_nonpositive_stride(tmp_path, bad):
    a = load_install(str(_make_install(tmp_path / "a")))
    with pytest.raises(InstallError, match="stride"):
        plan_ports([a], stride=bad)


def test_parse_netstat_listeners_ignores_established():
    text = (
        "  TCP    0.0.0.0:58600          0.0.0.0:0              LISTENING       24544\n"
        "  TCP    0.0.0.0:58600          0.0.0.0:0              LISTENING       8828\n"
        "  TCP    192.168.1.19:56080     59.36.9.32:56000       ESTABLISHED     8828\n"
        "  TCP    127.0.0.1:8888         0.0.0.0:0              LISTENING       13028\n"
    )
    got = parse_netstat_listeners(text)
    assert got[58600] == ["24544", "8828"]
    assert 56080 not in got, "ESTABLISHED 的临时端口不是监听，算进去会满屏假红"
    assert got[8888] == ["13028"]


def test_double_bound_flags_shared_port():
    assert double_bound({58600: ["1", "2"], 8888: ["3"]}) == {58600: ["1", "2"]}


def test_decode_console_bytes_survives_gbk_netstat():
    """netstat 输出是 OEM 代码页 —— 按 UTF-8 硬解会抛错并让 stdout 变空。

    这是**静默假绿**的入口：两个进程明明在抢 58600，审计却报"无冲突"。
    """
    raw = ("  TCP    0.0.0.0:58600          0.0.0.0:0              LISTENING       24544\n"
           "  TCP    0.0.0.0:58600          0.0.0.0:0              LISTENING       8828\n"
           "本地地址          外部地址        状态           PID\n").encode("cp936")
    text = decode_console_bytes(raw)
    assert "LISTENING" in text and "本地地址" in text
    assert double_bound(parse_netstat_listeners(text)) == {58600: ["24544", "8828"]}


def test_decode_console_bytes_handles_utf8_and_garbage():
    assert decode_console_bytes("端口".encode("utf-8")) == "端口"
    assert decode_console_bytes(b"\xff\xfe\x00bad") != "", "任何字节都必须解出非空文本"


def test_audit_reports_conflict_and_is_not_ok(tmp_path):
    a = load_install(str(_make_install(tmp_path / "a")))
    b = load_install(str(_make_install(tmp_path / "b")))
    rep = audit([a, b])
    assert rep["ok"] is False
    assert rep["declared_conflicts"], "静态审计必须报出撞车"
    assert rep["double_bound"] == {}, "没传探针就不该凭空断言活体状态"


def test_audit_with_probe_marks_double_bound(tmp_path):
    a = load_install(str(_make_install(tmp_path / "a")))
    rep = audit([a], probe=lambda: {58600: ["24544", "8828"]})
    assert rep["ok"] is False
    assert rep["double_bound"]["58600"] == ["24544", "8828"]


def test_audit_clean_single_install(tmp_path):
    a = load_install(str(_make_install(tmp_path / "a")))
    rep = audit([a], probe=lambda: {58600: ["24544"]})
    assert rep["ok"] is True
    assert rep["live_probe"] == "ok" and rep["live_checked"] is True


# ----------------------------------------------------------------------
# 探针三态：没检查 ≠ 检查了且无冲突
# ----------------------------------------------------------------------
def test_audit_without_probe_says_skipped_not_checked(tmp_path):
    """不传探针时必须是 "skipped"，不能让人读成"已核对无冲突"。"""
    rep = audit([load_install(str(_make_install(tmp_path / "a")))])
    assert rep["live_probe"] == "skipped"
    assert rep["live_checked"] is False


def test_audit_probe_unavailable_is_not_a_green_light(tmp_path):
    """探针拿不到结果（非 Windows / netstat 失败）必须是 "unavailable"。

    ★ 这是本轮修掉的静默假绿：``_win_listening_ports`` 原先在失败时返回 ``{}``，
    ``double_bound({})`` 得到空集 ⇒ 审计报 ``ok=True``。用户看到的绿灯含义是
    "检查过了，没有重复绑定"，而事实是"根本没检查"。
    """
    rep = audit([load_install(str(_make_install(tmp_path / "a")))], probe=lambda: None)
    assert rep["live_probe"] == "unavailable"
    assert rep["live_checked"] is False, "没拿到结果就不能声称检查过"
    assert rep["double_bound"] == {}
    assert rep["ok"] is False, "★ 要求了活体检查却没做成 ⇒ 不能判绿"
    assert rep["ok_basis"] == "live_unavailable"
    assert "没" in rep["note"] or "不能" in rep["note"]


def test_audit_static_only_says_so_explicitly(tmp_path):
    """没要求活体检查时可以报静态通过，但必须写明"仅静态"。"""
    rep = audit([load_install(str(_make_install(tmp_path / "a")))])
    assert rep["ok"] is True
    assert rep["ok_basis"] == "static_only"
    assert "未查活体" in rep["note"]


def test_audit_live_ok_has_static_plus_live_basis(tmp_path):
    rep = audit([load_install(str(_make_install(tmp_path / "a")))],
                probe=lambda: {58600: ["1"]})
    assert rep["ok"] is True and rep["ok_basis"] == "static_plus_live"
    assert "note" not in rep


def test_win_probe_returns_none_when_unavailable(monkeypatch):
    """netstat 不可用/无输出 ⇒ None（而不是 {}）。"""
    import subprocess

    import connectors.install_registry as reg

    def _boom(*a, **kw):
        raise OSError("netstat not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert reg._win_listening_ports() is None

    class _CP:
        stdout = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _CP())
    assert reg._win_listening_ports() is None, "空输出同样等于没检查到"


# ----------------------------------------------------------------------
# 从任意客户端路径回溯安装根（不写死盘符）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("rel", ["", "userdata", "userdata_mini", "bin.x64",
                                 "bin.x64/Lib/site-packages/xtquant"])
def test_find_install_root_walks_up_from_any_entry(tmp_path, rel):
    root = _make_install(tmp_path / "gd_qmt")
    start = str(root / rel) if rel else str(root)
    assert Path(find_install_root(start) or "").name == "gd_qmt"


def test_find_install_root_returns_none_outside_install(tmp_path):
    plain = tmp_path / "not_a_client"
    plain.mkdir()
    assert find_install_root(str(plain)) is None
    assert find_install_root("") is None


def test_find_install_root_does_not_walk_forever(tmp_path):
    """超过 max_up 层就放弃 —— 别为了找个 exe 一路爬到盘根。"""
    root = _make_install(tmp_path / "deep")
    deep = root / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    assert find_install_root(str(deep), max_up=2) is None
    assert find_install_root(str(deep), max_up=8) is not None
