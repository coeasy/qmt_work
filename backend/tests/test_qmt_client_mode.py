# -*- coding: utf-8 -*-
"""大 / 小 QMT 客户端「模式判定」与「按模式启动」的回归护栏。

背景（2026-10-09 真机实测，同一台「大小合一」安装上暴露的三个真实缺陷）：

B1  ``discover()`` 对**同一客户端根**只取「第一个被枚举到的进程」当代表，
    而枚举顺序不保证（psutil 按 PID、PowerShell 按 WMI 顺序）。于是
    「大客户端 + 独立行情」并存时候选模式会随机漂移，甚至建议
    ``userdata_mini`` —— 与实跑的大客户端相反，交易必然 ``rc=-1``。

B2  ``mode_from_proc("miniquote.exe")`` 返回 ``"mini"``：把**行情子进程**当成了
    极速版主程序。miniquote 既可由极速版拉起、也可作为完整版的「独立行情」子进程，
    它**不决定**交易数据目录。

B3  ``launch_client("mini")`` / ``("quote")`` 共用 ``mini_client_running``（并集），
    而该标志只要 58610 在监听就为真 ⇒ 大客户端带着独立行情在跑时，
    「启动小 QMT」被误报「已在运行」，``XtMiniQmt.exe`` **永远拉不起来**。
    （「假绿灯」的镜像：拿另一个事实冒充本事实。）

这些用例同时充当**反腐烂锚**：断言里保留正向锚（大客户端仍判 full、
XtMiniQmt 仍判 mini），避免为了让某条断言变绿而把判定整体改坏。
"""
from __future__ import annotations

import os

import pytest

from xtquant_client import discovery as D


# ---------------------------------------------------------------- 公共桩

def _probe_stub(**over):
    """`probe_environment(light=True)` 的最小可用替身（键与 _candidate 的消费面一致）。"""
    base = {
        "has_bin_x64": True, "xtquant_found": True, "xtquant_site": "site",
        "xtquant_importable": True, "import_error": "", "hint": "",
        "runtime_mode": "inproc", "bridge_feasible": True, "suggested_abi": "cp36",
    }
    base.update(over)
    return base


def _client_tree(tmp_path, *, mini=True, full=True):
    """造一个最小客户端目录（userdata / userdata_mini / bin.x64 + 主程序 exe）。"""
    root = tmp_path / "gd_qmt"
    (root / "bin.x64").mkdir(parents=True, exist_ok=True)
    if mini:
        (root / "userdata_mini").mkdir(exist_ok=True)
    if full:
        (root / "userdata").mkdir(exist_ok=True)
    for exe in ("XtItClient.exe", "XtMiniQmt.exe", "miniquote.exe"):
        (root / "bin.x64" / exe).write_bytes(b"")
    return root


def _patch_discover(monkeypatch, procs, tmp_path):
    """把 discover() 的外部输入（进程枚举 / 安装扫描 / 环境探测）全部桩化。"""
    monkeypatch.setattr(D, "_ps_enum", lambda: procs)
    monkeypatch.setattr(D, "_scan_installed", lambda: [])
    monkeypatch.setattr(D, "probe_environment", lambda p, light=True: _probe_stub())


# ---------------------------------------------------------------- B2 回归

def test_miniquote_is_not_a_mode_source():
    """行情子进程 miniquote 不决定交易数据目录 ⇒ 必须返回 ''（未判定）。"""
    assert D.mode_from_proc("miniquote.exe") == ""
    assert D.mode_from_proc("XtMiniQuote.exe") == ""
    # 正向锚：真正的主程序仍要判得出来（防止「全返回 ''」式假修复）
    assert D.mode_from_proc("XtMiniQmt.exe") == "mini"
    assert D.mode_from_proc("XtItClient.exe") == "full"
    assert D.mode_from_proc("") == ""


def test_full_and_mini_exe_hints_stay_disjoint():
    """关键词兜底表里不得再出现 miniquote（它属于行情，不属于任一交易模式）。"""
    assert "miniquote" not in D._MINI_EXE_HINT
    assert "miniquote" not in D._FULL_EXE_HINT


# ---------------------------------------------------------------- B1 回归

def test_pick_representative_proc_prefers_full():
    """大客户端与行情子进程同跑 → 代表必须是 full（与 _effective_trade_dir 口径一致）。"""
    procs = [
        {"root": "R", "pid": "9001", "name": "miniquote.exe"},
        {"root": "R", "pid": "11092", "name": "XtItClient.exe"},
    ]
    rep = D.pick_representative_proc(procs)
    assert rep["name"] == "XtItClient.exe"


def test_pick_representative_proc_prefers_full_over_mini():
    """大 + 小客户端同时运行 → full 优先（两处判定必须给同一结论）。"""
    procs = [
        {"root": "R", "pid": "100", "name": "XtMiniQmt.exe"},
        {"root": "R", "pid": "200", "name": "XtItClient.exe"},
    ]
    assert D.pick_representative_proc(procs)["name"] == "XtItClient.exe"


def test_pick_representative_proc_mini_when_only_mini():
    """只启动小 QMT → mini（正向锚：不能因为「full 优先」就把独占场景也判成 full）。"""
    procs = [{"root": "R", "pid": "500", "name": "XtMiniQmt.exe"},
             {"root": "R", "pid": "501", "name": "miniquote.exe"}]
    assert D.pick_representative_proc(procs)["name"] == "XtMiniQmt.exe"


def test_pick_representative_proc_is_deterministic():
    """同级候选按 PID 升序 ⇒ 同一组进程永远选出同一个代表（结果可复现）。"""
    a = [{"root": "R", "pid": "300", "name": "XtItClient.exe"},
         {"root": "R", "pid": "100", "name": "XtClient.exe"}]
    assert D.pick_representative_proc(a)["pid"] == "100"
    assert D.pick_representative_proc(list(reversed(a)))["pid"] == "100"


def test_pick_representative_proc_tolerates_bad_pid():
    """PID 可能非数字（异常进程枚举）⇒ 不得抛错。"""
    rep = D.pick_representative_proc([{"root": "R", "pid": "", "name": "miniquote.exe"}])
    assert rep["name"] == "miniquote.exe"


def test_discover_big_qmt_with_quote_subprocess_suggests_full(tmp_path, monkeypatch):
    """★ B1 主用例：大客户端 + 独立行情同跑 ⇒ 必须建议 full + userdata。

    旧实现在这里依赖枚举顺序：miniquote 排在前面时会给 mini + userdata_mini。
    """
    root = _client_tree(tmp_path)
    procs = [
        {"pid": "9001", "name": "miniquote.exe",
         "exe": str(root / "bin.x64" / "miniquote.exe")},
        {"pid": "11092", "name": "xtitclient.exe",
         "exe": str(root / "bin.x64" / "XtItClient.exe")},
    ]
    _patch_discover(monkeypatch, procs, tmp_path)
    cands = D.discover()
    assert len(cands) == 1, cands
    c = cands[0]
    assert c["client_mode"] == "full", c
    assert c["mode_source"] == "process"
    assert os.path.basename(c["client_path"]) == "userdata"


def test_discover_small_qmt_alone_suggests_mini(tmp_path, monkeypatch):
    """正向锚：只启动小 QMT（+ 行情）⇒ 建议 mini + userdata_mini。"""
    root = _client_tree(tmp_path)
    procs = [
        {"pid": "9000", "name": "xtminiqmt.exe",
         "exe": str(root / "bin.x64" / "XtMiniQmt.exe")},
        {"pid": "9001", "name": "miniquote.exe",
         "exe": str(root / "bin.x64" / "miniquote.exe")},
    ]
    _patch_discover(monkeypatch, procs, tmp_path)
    cands = D.discover()
    assert len(cands) == 1
    c = cands[0]
    assert c["client_mode"] == "mini"
    assert c["mode_source"] == "process"
    assert os.path.basename(c["client_path"]) == "userdata_mini"


# ---------------------------------------------------------------- B3 回归

def _stub_shell_probe(monkeypatch, **flags):
    """把「进程/端口探测」换成给定结论（launch_client 经 _shell_attr 动态取它）。"""
    from xtquant_client import xtp as xtp_mod
    payload = {"running_exes": [], "full_client_running": False,
               "miniqmt_running": False, "miniquote_running": False,
               "mini_client_running": False, "quote_ports": [], "trade_ports": []}
    payload.update(flags)
    monkeypatch.setattr(xtp_mod, "_probe_quote_service", lambda p: dict(payload))


def _record_popen(monkeypatch):
    """记录（而非真的执行）Popen 调用，避免用例在测试机上拉起 GUI。"""
    calls: list[list[str]] = []

    class _Fake:
        def __init__(self, args, **kw):
            calls.append(list(args))

    monkeypatch.setattr("subprocess.Popen", _Fake)
    return calls


def test_launch_mini_not_blocked_by_quote_subprocess(tmp_path, monkeypatch):
    """★ B3 主用例：大客户端 + 独立行情在跑，仍必须能拉起极速版。

    旧实现：`mini_client_running` 为真 ⇒ 返回 already_running=True，永不启动。
    """
    from xtquant_client.xtp import launch_client
    root = _client_tree(tmp_path)
    _stub_shell_probe(monkeypatch,
                      full_client_running=True, miniquote_running=True,
                      mini_client_running=True, running_exes=["XtItClient.exe"])
    calls = _record_popen(monkeypatch)
    r = launch_client(str(root / "userdata"), "mini")
    assert r["already_running"] is False, r
    assert r["launched"] is True, r
    assert calls and os.path.basename(calls[0][0]).lower() == "xtminiqmt.exe"


def test_launch_mini_reports_already_running_when_miniqmt_up(tmp_path, monkeypatch):
    """正向锚：极速版**自己在跑**时仍要如实报「已在运行」，且不重复拉起。"""
    from xtquant_client.xtp import launch_client
    root = _client_tree(tmp_path)
    _stub_shell_probe(monkeypatch, full_client_running=True, miniqmt_running=True,
                      mini_client_running=True, running_exes=["XtMiniQmt.exe"])
    calls = _record_popen(monkeypatch)
    r = launch_client(str(root / "userdata"), "mini")
    assert r["already_running"] is True and r["launched"] is False, r
    assert calls == []


def test_launch_quote_gated_by_miniquote_flag(tmp_path, monkeypatch):
    """`quote` 模式只认 miniquote —— 极速版在跑不该挡住「补行情服务」。"""
    from xtquant_client.xtp import launch_client
    root = _client_tree(tmp_path)
    _stub_shell_probe(monkeypatch, miniqmt_running=True, mini_client_running=True,
                      miniquote_running=False, running_exes=["XtMiniQmt.exe"])
    calls = _record_popen(monkeypatch)
    r = launch_client(str(root / "userdata_mini"), "quote")
    assert r["already_running"] is False and r["launched"] is True, r
    assert calls and os.path.basename(calls[0][0]).lower() == "miniquote.exe"


def test_launch_full_reports_already_running(tmp_path, monkeypatch):
    """大客户端在跑 ⇒ full 模式如实报「已在运行」。"""
    from xtquant_client.xtp import launch_client
    root = _client_tree(tmp_path)
    _stub_shell_probe(monkeypatch, full_client_running=True,
                      running_exes=["XtItClient.exe"])
    calls = _record_popen(monkeypatch)
    r = launch_client(str(root / "userdata"), "full")
    assert r["already_running"] is True and r["launched"] is False
    assert calls == []


def test_launch_hint_names_the_mode(tmp_path, monkeypatch):
    """提示文案必须点名是**哪个模式**已在运行（旧文案统一说「极速版/独立行情」，无法分辨）。"""
    from xtquant_client.xtp import launch_client
    root = _client_tree(tmp_path)
    _stub_shell_probe(monkeypatch, miniqmt_running=True, running_exes=["XtMiniQmt.exe"])
    r = launch_client(str(root / "userdata_mini"), "mini")
    assert "小客户端" in r["hint"] or "小 QMT" in r["hint"], r


@pytest.mark.parametrize("bad", ["Full", "miniqmt", "", "all", "full "])
def test_launch_rejects_unknown_mode_instead_of_silently_quoting(bad, tmp_path, monkeypatch):
    """★ B6：非法 mode 必须如实报错，**绝不静默去启动 miniquote**。

    旧实现 `... else _QUOTE_EXE_NAMES` 把任何非 full/mini 的值都当 quote，
    而「已运行」判定又落到默认的 full 标志 —— 找 A 的 exe、判 B 在不在跑。
    """
    from xtquant_client.xtp import launch_client
    root = _client_tree(tmp_path)
    _stub_shell_probe(monkeypatch, full_client_running=True,
                      running_exes=["XtItClient.exe"])
    calls = _record_popen(monkeypatch)
    r = launch_client(str(root / "userdata"), bad)
    assert r["launched"] is False and r["already_running"] is False, r
    assert "未知客户端模式" in r["hint"], r
    assert calls == [], "非法模式绝不应拉起任何进程"
    # 正向锚：合法模式仍然正常工作
    ok = launch_client(str(root / "userdata"), "quote")
    assert ok["already_running"] is False and ok["launched"] is True


# ---------------------------------------------------------------- 探测面分裂标志

def test_probe_quote_service_splits_miniqmt_and_miniquote(monkeypatch):
    """`_probe_quote_service` 必须给出**按进程区分**的两个标志。

    桩化的 tasklist 输出模拟「大客户端 + 独立行情」：miniqmt 不在、miniquote 在。
    """
    from xtquant_client.xtp import env as E

    csv = (
        '"XtItClient.exe","11092","Console","1","1 K"\n'
        '"miniquote.exe","9001","Console","1","1 K"\n'
    )

    class _R:
        stdout = csv

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _R())
    monkeypatch.setattr(E, "_scan_qmt_listeners", lambda: [
        {"port": 58600, "pid": 11092, "process": "xtitclient.exe"},
    ])
    res = E._probe_quote_service("C:/x/userdata")
    assert res["full_client_running"] is True
    assert res["miniquote_running"] is True
    assert res["miniqmt_running"] is False          # ★ 关键：不把行情当极速版
    assert res["mini_client_running"] is True        # 并集语义保留（兼容既有消费方）
    assert res["client_type"] == "both"


def test_probe_quote_service_miniqmt_only(monkeypatch):
    """只跑极速版（+ 行情）⇒ miniqmt / miniquote 均为真，full 为假。"""
    from xtquant_client.xtp import env as E

    csv = ('"XtMiniQmt.exe","9000","Console","1","1 K"\n'
           '"miniquote.exe","9001","Console","1","1 K"\n')

    class _R:
        stdout = csv

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _R())
    monkeypatch.setattr(E, "_scan_qmt_listeners", lambda: [
        {"port": 58610, "pid": 9001, "process": "miniquote.exe"}])
    res = E._probe_quote_service("C:/x/userdata_mini")
    assert res["miniqmt_running"] is True
    assert res["miniquote_running"] is True
    assert res["full_client_running"] is False
    assert res["client_type"] == "mini"


def test_mode_label_exported_from_shell():
    """模式中文名经壳模块可导入（前后端/日志共用一份文案，避免三种叫法）。"""
    from xtquant_client import xtp as xtp_mod
    assert xtp_mod._MODE_LABEL["full"] and xtp_mod._MODE_LABEL["mini"]
    assert "大 QMT" in xtp_mod._MODE_LABEL["full"]
    assert "小 QMT" in xtp_mod._MODE_LABEL["mini"]


@pytest.mark.parametrize("mode", ["full", "mini", "quote"])
def test_launch_mode_label_covers_all_modes(mode):
    from xtquant_client.xtp import _MODE_LABEL
    assert mode in _MODE_LABEL


# ---------------------------------------------------------------- B4 / B5 回归
# rc=-1 失败文案必须锚在「首选判定的模式/目录」上，而不是回退循环停下的那个。

def _patch_trade_failure(monkeypatch, tmp_path, primary_mode):
    """把 start() 的前置依赖全部桩化，只保留「连不上」这条真实分支。"""
    import sys
    import types

    from xtquant_client.xtp import adapter as A

    root = tmp_path / "gd_qmt"
    (root / "userdata").mkdir(parents=True, exist_ok=True)
    (root / "userdata_mini").mkdir(exist_ok=True)
    full, mini = str(root / "userdata"), str(root / "userdata_mini")

    # xtquant 包替身：绕开「本机没装 xtquant」的现实差异
    fake_pkg = types.ModuleType("xtquant")
    fake_xtdata = types.ModuleType("xtquant.xtdata")
    fake_pkg.xtdata = fake_xtdata
    monkeypatch.setitem(sys.modules, "xtquant", fake_pkg)
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake_xtdata)

    class _FakeTrader:
        def __init__(self, path, session):
            self.path, self.session = path, session

        def start(self):
            return None            # xtquant 真实语义：start() 无返回值

        def connect(self):
            return -1              # 客户端拒绝（极简模式未开）的真实返回

        def stop(self):
            return None

    class _FakeAcc:
        def __init__(self, aid, atype):
            self.account_id, self.atype = aid, atype

    monkeypatch.setattr(A, "_resolve_xtquant_path", lambda p: None)
    monkeypatch.setattr(A, "_load_trader_api", lambda: (_FakeTrader, {"STOCK": _FakeAcc}))
    monkeypatch.setattr(A, "_probe_xtdata", lambda xtdata, p: (True, "ok"))
    monkeypatch.setattr(A, "_running_client_exes", lambda: ["XtItClient.exe"])

    def _eff(path, mode="auto"):
        # 首选按 primary_mode 解析；回退请求另一模式
        if mode == "mini":
            return mini, "mini"
        if mode == "full":
            return full, "full"
        return (full, "full") if primary_mode == "full" else (mini, "mini")

    monkeypatch.setattr(A, "_effective_trade_dir", _eff)
    seen: dict[str, str] = {}

    def _login_log(trade_dir):
        seen["dir"] = trade_dir
        return "STUB_LOG"

    monkeypatch.setattr(A, "_latest_login_log", _login_log)
    monkeypatch.setattr(A, "_shell_attr", lambda name: (lambda *a, **k: {}))
    return A, full, mini, seen


def test_trade_failure_reports_primary_mode_not_last_tried(tmp_path, monkeypatch):
    """★ B4：实跑大客户端时文案必须说「首选判定 @full」，不能报成 @mini。

    旧实现把「回退循环最后停下的模式」当成「解析判定」，把用户指向极简版配置。
    """
    A, full, mini, _seen = _patch_trade_failure(monkeypatch, tmp_path, "full")
    ad = A.XTPQuantAdapter(full, "22453951", account_type="STOCK", client_mode="auto")
    with pytest.raises(Exception) as ei:
        ad.start()
    msg = str(ei.value)
    assert "首选判定 @full" in msg, msg
    # 反向锚：必须同时如实说明「回退后停在 @mini」，不能把回退事实抹掉
    assert "回退后停在 @mini" in msg, msg
    assert "大客户端" in msg


def test_trade_failure_log_evidence_uses_primary_dir(tmp_path, monkeypatch):
    """★ B5：日志取证用**首选目录**，不是最后试过的目录。

    旧实现取到 userdata_mini/log 下数天前的 XtMiniQmt 日志 —— 与当前运行的
    大客户端毫无关系，真正的证据（今日 XtClient_*.log）被丢掉。
    """
    A, full, mini, seen = _patch_trade_failure(monkeypatch, tmp_path, "full")
    ad = A.XTPQuantAdapter(full, "22453951", account_type="STOCK", client_mode="auto")
    with pytest.raises(Exception):
        ad.start()
    assert seen.get("dir") == full, seen
    assert seen.get("dir") != mini


def test_trade_failure_suggestion_follows_primary_mode(tmp_path, monkeypatch):
    """模式感知建议：大客户端场景不得只推「去装极速版」（换数据目录/换登录体系）。"""
    A, full, _mini, _seen = _patch_trade_failure(monkeypatch, tmp_path, "full")
    ad = A.XTPQuantAdapter(full, "22453951", account_type="STOCK", client_mode="auto")
    with pytest.raises(Exception) as ei:
        ad.start()
    msg = str(ei.value)
    assert "以极简模式重新登录完整版大客户端" in msg, msg
    # 正向锚：仍保留「换极速版」这条备选路径（不能因为改文案就删掉出路）
    assert "XtMiniQmt.exe" in msg



# ---------------------------------------------------------------- 路由层：非法 mode 如实 400

def _call_launch(body: dict) -> dict:
    """直接调用路由处理函数（非法入参在进入线程池前就返回，故 ctx 不会被触及）。"""
    import asyncio

    from app.routes import broker as B

    class _Ctx:  # 占位：本条路径不会触碰 ctx
        pass

    return asyncio.run(B.launch_broker_client(body, _Ctx()))


@pytest.mark.parametrize("bad", ["auto", "AUTO", "both", "miniqmt", "小qmt", "full2"])
def test_launch_route_rejects_unknown_mode_with_400(bad):
    """★ 路由层：非法 mode 必须 400 + 列出可选值，而不是 200 + 「就是没启动」。

    旧实现把 body 里的 mode 原样透传，`launch_client` 虽然已不再静默当 quote，
    但仍以 200 + launched=false 返回 ⇒ 调用方写错枚举值只会看到「没启动」，
    无从知道是自己传错了。请求参数非法属**客户端错误**，须如实 400。
    """
    r = _call_launch({"client_path": r"P:/not/a/client", "mode": bad})
    assert r["code"] == 400, r
    assert "未知客户端模式" in r["message"], r
    for valid in ("full", "mini", "quote"):
        assert valid in r["message"], r


@pytest.mark.parametrize("bad", ["auto", "both"])
def test_launch_route_400_happens_before_any_process_probe(bad, monkeypatch):
    """反腐烂锚：非法 mode 必须在**探测/拉起之前**就被拦下，不能先探测再报错。"""
    touched: list = []
    from xtquant_client import xtp as xtp_mod
    monkeypatch.setattr(xtp_mod, "_probe_quote_service",
                        lambda p: touched.append(p) or {})
    r = _call_launch({"client_path": r"P:/not/a/client", "mode": bad})
    assert r["code"] == 400
    assert touched == [], "非法 mode 不应触发任何进程/端口探测"


def test_launch_route_requires_client_path():
    """缺 client_path：400 且文案点名该参数（与既有实现口径一致）。"""
    r = _call_launch({"mode": "full"})
    assert r["code"] == 400, r
    assert "client_path" in r["message"]


@pytest.mark.parametrize("mode", ["full", "mini", "quote", "FULL", " Mini ", "QUOTE"])
def test_launch_route_accepts_valid_mode_and_normalizes_case(mode, tmp_path, monkeypatch):
    """正面锚：合法 mode（含大小写与首尾空格）必须放行 —— 防止校验写成「过严」。"""
    root = _client_tree(tmp_path)
    _stub_shell_probe(monkeypatch)          # 无任何客户端在跑 ⇒ 走真正拉起分支
    calls = _record_popen(monkeypatch)
    r = _call_launch({"client_path": str(root / "userdata"), "mode": mode})
    assert r["code"] == 0, r
    assert r["data"]["launched"] is True, r
    assert len(calls) == 1, calls
