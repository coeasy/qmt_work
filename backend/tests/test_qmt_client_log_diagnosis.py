# -*- coding: utf-8 -*-
"""客户端日志诊断的**读取窗口**与**归因分流**回归护栏。

背景（2026-10-09 光大 QMT 真机实测，两个各自独立、都会让诊断失真的缺陷）：

D1  读取窗口过小（**诊断随当天时间漂移**）
    ``read_client_auth_flags`` 只读「头 512KB / 尾 256KB」，``_diagnose_one`` 只在
    「尾 256KB 的末 1200 行」里找拒绝行。而当日主日志 17.2MB / 103592 行：
      · 授权串 `receive module auth string` 在 **第 9719 行 / 偏移 1,573,933 B**
      · 拒绝行 `The XtQuantServer is not allowed to start.` 在偏移 1.81MB / 15.54MB
    两个固定窗口统统覆盖不到 ⇒ **早上诊断正确、晚上静默失效**，不报错、只表现为
    「无明确证据」。这不是判错，而是不判 —— 最难发现的一类。

D2  归因错误（**假告状**）
    ``_diagnose_one`` 用 `"not allowed" in ln` 粗判 ⇒ 把
    `The XtQuantServer is not allowed to start.`（客户端未获授权启动量化服务）
    误报成 `pid_not_allowed`（严格校验按 PID 拉黑了你的进程）。两者的处置方向
    完全不同：前者要「申请 xtquant 模块授权」，后者才要「加进程白名单」。
    按错误的归因排查必然徒劳。

本文件同时充当**反腐烂锚**：断言里保留「旧窗口确实读不到」（证明用例真的覆盖了
那个窗口缺陷）与「PID 拒绝仍能被正确识别」（证明分流没有为修 D2 而整体改坏）。
"""
from __future__ import annotations

import pytest

from xtquant_client.xtp import diagnostics as G


# ---------------------------------------------------------------- 日志构造桩

_FILLER = b"2026-10-09 08:00:00,000 [INFO] [0x00000001] filler padding padding\n"


def _filler(n_bytes: int) -> bytes:
    """造 n_bytes 量级的无关日志行（用于把标记推到固定窗口之外）。"""
    reps = n_bytes // len(_FILLER) + 1
    return _FILLER * reps


def _auth_line(flags: dict) -> bytes:
    """造一行「模块授权串」（真实形态：单行 49,910 B / 649 个 mdl_auth_* 键）。"""
    body = ", ".join('"%s" : %d' % (k, v) for k, v in flags.items())
    return ("2026-10-09 08:02:38,937 [INFO] [0x00000fb4] [auth log] [CProxyClient] "
            "receive module auth string : { " + body + " }\n").encode("utf-8")


def _write_log(tmp_path, chunks: list[bytes], name: str = "XtClient_20261009.log"):
    """把 chunks 依次写入 <tmp>/userdata/log/<name>，返回 (trade_dir, log_path)。"""
    trade_dir = tmp_path / "gd_qmt" / "userdata"
    d = trade_dir / "log"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"".join(chunks))
    return trade_dir, p


# ---------------------------------------------------------------- D1 读取窗口

def test_auth_string_found_beyond_legacy_head_window(tmp_path):
    """授权串在 600KB 之后 —— 旧「头 512KB」窗口读不到，新的标记扫描必须读到。"""
    flags = {
        "mdl_auth_xtquant": 0,
        "mdl_auth_gt_ipc_pair": 0,
        "mdl_auth_xttrader_strict_connection_check": 0,
        "mdl_auth_xtdata_strict_connection_check": 0,
        "mdl_auth_xtquant_no_pid_check": 0,
    }
    trade_dir, p = _write_log(tmp_path, [_filler(600_000), _auth_line(flags)])

    # 反腐烂锚：先证明这个用例**确实**落在了旧窗口之外（否则它只是在重复老路径）。
    head = G._head_text(p)
    assert G._AUTH_MARKER not in head, "用例前提失效：授权串已落回头部窗口内"

    got = G.read_client_auth_flags(str(trade_dir))
    assert got["found"] is True
    assert got["xtquant_auth"] == 0
    assert got["gt_ipc_pair"] == 0
    assert got["strict_xttrader"] == 0
    assert got["no_pid_check"] == 0
    assert len(got["flags"]) == len(flags)


def test_last_auth_string_wins_after_relogin(tmp_path):
    """重登会再下发一次授权串 ⇒ 必须取**最后一次**（首次的 1 早已失效）。"""
    first = {"mdl_auth_xtquant": 0, "mdl_auth_xttrader_strict_connection_check": 1}
    last = {"mdl_auth_xtquant": 1, "mdl_auth_xttrader_strict_connection_check": 0}
    trade_dir, _ = _write_log(tmp_path, [_auth_line(first), _auth_line(last)])
    got = G.read_client_auth_flags(str(trade_dir))
    assert got["xtquant_auth"] == 1
    assert got["strict_xttrader"] == 0


def test_scan_reads_marker_across_chunk_boundary(tmp_path):
    """标记行被 1MiB 分块边界切成两半时也不能漏（分块拼接的正确性）。"""
    pad = b"a" * (G._SCAN_CHUNK - 5) + b"\n"
    trade_dir, _ = _write_log(tmp_path, [pad, _auth_line({"mdl_auth_xtquant": 1})])
    got = G.read_client_auth_flags(str(trade_dir))
    assert got["found"] is True
    assert got["xtquant_auth"] == 1


def test_scan_respects_max_bytes_cap(tmp_path):
    """超过扫描上限的内容如实「扫不到」，而不是假装找到（不臆测）。"""
    trade_dir, p = _write_log(tmp_path, [_filler(G._SCAN_MAX_BYTES + 4096),
                                         _auth_line({"mdl_auth_xtquant": 1})])
    got = G.read_client_auth_flags(str(trade_dir))
    assert got["found"] is False
    assert got["flags"] == {}


def test_missing_or_unreadable_log_does_not_raise(tmp_path):
    """诊断自身绝不能崩（诊断崩了比诊断不到更糟）。"""
    assert G._scan_marker_lines(str(tmp_path / "nope.log"), ("x",)) == {"x": []}
    assert G._read_at(str(tmp_path / "nope.log"), 0, 100) == ""
    assert G._scan_marker_lines(str(tmp_path / "nope.log"), ()) == {}
    got = G.read_client_auth_flags(str(tmp_path / "no_such_dir"))
    assert got["found"] is False


# ---------------------------------------------------------------- D2 归因分流

def test_blocked_server_is_not_reported_as_pid_denied(tmp_path):
    """`XtQuantServer is not allowed to start` 是「服务未授权启动」，不是 PID 拉黑。"""
    blocked = (b"2026-10-09 08:02:44,541 [INFO] [0x00000fb4] [0x00001c38] "
               b"The XtQuantServer is not allowed to start.\n")
    auth = _auth_line({"mdl_auth_xtquant": 0, "mdl_auth_gt_ipc_pair": 0})
    trade_dir, _ = _write_log(tmp_path, [_auth_line({}), auth, blocked])

    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == "xtquant_server_blocked"
    assert "not allowed to start" in res["title"]
    # 反腐烂锚：该行里**没有** pid，正则必须不匹配（否则归因又会退化回 pid_not_allowed）。
    assert G._PID_DENY_RE.search(blocked.decode()) is None
    # 处置首句必须把用户从「加白名单」这条错路上拉回来。
    assert "不是**被 PID 白名单拒绝" in res["steps"]


def test_pid_denied_still_detected(tmp_path):
    """真正的 `pid X not allowed` 仍须判为 pid_not_allowed（分流没改坏正向）。"""
    deny = (b"2026-10-09 09:00:00,000 [INFO] [quant] "
            b"quant session 1, pid 4242 not allowed, return\n"
            b"2026-10-09 09:00:00,001 [INFO] [quant] XtQuantServer:: connect ret error-1\n")
    trade_dir, _ = _write_log(tmp_path, [
        _auth_line({"mdl_auth_xttrader_strict_connection_check": 1}), deny])

    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == "pid_not_allowed"
    assert "4242" in res["title"]


def test_evidence_found_beyond_legacy_tail_window(tmp_path):
    """拒绝行在被 1MB 无关日志顶到尾部窗口之外时，仍须找到证据（D1 对 _diagnose_one 同样成立）。"""
    blocked = (b"2026-10-09 08:02:44,541 [INFO] [0x00000fb4] "
               b"The XtQuantServer is not allowed to start.\n")
    trade_dir, p = _write_log(tmp_path, [
        _auth_line({"mdl_auth_xtquant": 0}), blocked, _filler(1_200_000)])

    # 反腐烂锚：证明标记确实不在旧「尾 256KB」窗口里。
    assert G._SERVER_BLOCKED_MARKER not in G._tail_text(p)

    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == "xtquant_server_blocked"


def test_xtquant_module_unauthorized_without_reject_lines(tmp_path):
    """无拒绝行、无严格校验，但 mdl_auth_xtquant=0 ⇒ 仍要给出「通道未开放」结论。"""
    trade_dir, _ = _write_log(tmp_path, [
        _auth_line({"mdl_auth_xtquant": 0, "mdl_auth_gt_ipc_pair": 0,
                    "mdl_auth_xttrader_strict_connection_check": 0})])
    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == "xtquant_module_unauthorized"


def test_strict_check_reported_when_flags_say_so(tmp_path):
    """授权串 strict=1 且无显式拒绝 ⇒ strict_check（保留原有正向路径）。"""
    trade_dir, _ = _write_log(tmp_path, [
        _auth_line({"mdl_auth_xtquant": 1,
                    "mdl_auth_xttrader_strict_connection_check": 1,
                    "mdl_auth_xtquant_no_pid_check": 0})])
    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == "strict_check"


def test_no_evidence_stays_empty_instead_of_guessing(tmp_path):
    """一切正常（xtquant 已授权、无严格校验、无拒绝行）⇒ reason 必须为空，不臆测。"""
    trade_dir, _ = _write_log(tmp_path, [
        _auth_line({"mdl_auth_xtquant": 1, "mdl_auth_gt_ipc_pair": 1,
                    "mdl_auth_xttrader_strict_connection_check": 0})])
    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == ""
    assert res["title"] == ""
    assert res["steps"] == ""


@pytest.mark.parametrize("reason", ["xtquant_server_blocked",
                                    "xtquant_module_unauthorized",
                                    "pid_not_allowed", "strict_check"])
def test_every_reason_carries_steps(tmp_path, reason):
    """任一归因都必须带处置步骤 —— 只报「为什么」不给「怎么办」等于没说。"""
    flags = {"mdl_auth_xtquant": 0}
    extra: list[bytes] = []
    if reason == "xtquant_server_blocked":
        extra = [b"The XtQuantServer is not allowed to start.\n"]
    elif reason == "pid_not_allowed":
        flags = {"mdl_auth_xttrader_strict_connection_check": 1}
        extra = [b"quant session 7, pid 999 not allowed, return\n"]
    elif reason == "strict_check":
        flags = {"mdl_auth_xtquant": 1,
                 "mdl_auth_xttrader_strict_connection_check": 1}
    trade_dir, _ = _write_log(tmp_path, [_auth_line(flags)] + extra)
    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == reason
    assert "联系券商" in res["steps"]


# ---------------------------------------------------------------- D3 真实措辞
# ★ 2026-11-10 国信真机（§21.R2）：拒绝行是
#   `[TC::COrderServiceQuantAdaptor::onConnected] quant session 900001, illegal pid 21664`
#   当日 `illegal pid` 133 行、`pid N not allowed` **0 行**；授权串却是
#   mdl_auth_xtquant=1 / 三个 strict_connection_check=0 / no_pid_check=0。
#   旧实现的 marker 集与正则都要求字面量 "not allowed" ⇒ 四个归因分支全落空，
#   reason 为空、无 title/evidence/steps，上层退回泛化四步（含已被证伪的换 session）。

_GUOXIN_FLAGS = {
    "mdl_auth_xtquant": 1,
    "mdl_auth_gt_ipc_pair": 0,
    "mdl_auth_xttrader_strict_connection_check": 0,
    "mdl_auth_xtdata_strict_connection_check": 0,
    "mdl_auth_xtquant_strict_connection_check": 0,
    "mdl_auth_xtquant_no_pid_check": 0,
}


def test_illegal_pid_wording_from_real_guoxin_log_is_diagnosed(tmp_path):
    """真实现场四个分支本应全空 → 修复后必须判 pid_not_allowed 并给出证据。"""
    decisive = (b"2026-11-10 20:55:57,449 [TC::COrderServiceQuantAdaptor::onConnected] "
                b"quant session 900001, illegal pid 21664\n")
    trade_dir, _ = _write_log(tmp_path, [
        _auth_line(_GUOXIN_FLAGS),
        b"2026-11-10 20:53:57,909 CIPCManager::init, not auth:mdl_auth_gt_ipc_pair\n",
        b"2026-11-10 20:53:57,910 Allow the XtQuantServer to start.\n",
        decisive,
        b"2026-11-10 20:55:57,450 [quant]XtQuantServer:: connect ret error-1\n",
    ], name="XtClient_20261110.log")

    # 反腐烂锚：证明这条真实证据**确实**不含旧判据依赖的字面量。
    assert b"not allowed" not in decisive
    assert G._PID_DENY_RE.search(decisive.decode()) is not None

    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == "pid_not_allowed"
    assert res["pid_deny_wording"] == "illegal pid"
    assert res["session_retry_useless"] is True
    assert "21664" in res["title"]
    assert any("illegal pid 21664" in e for e in res["evidence"])
    assert "换 session_id 无效" in res["steps"]
    # 「服务未获授权启动」的拒绝行本日志 0 命中，绝不能被误判成它。
    assert res["reason"] != "xtquant_server_blocked"


def test_illegal_pid_is_case_insensitive_and_accepts_pid_zero(tmp_path):
    """cp36/Jupyter 上报 `illegal pid 0`；日志大小写也不由我们控制。"""
    trade_dir, _ = _write_log(tmp_path, [
        _auth_line(_GUOXIN_FLAGS),
        b"2026-11-10 17:05:29 quant session 28729, illegal pid 23776\n",
        b"2026-11-10 17:09:05 quant session 28945, Illegal Pid 0\n",
        b"2026-11-10 17:12:18 quant session 31415, ILLEGAL PID 0\n",
    ], name="XtClient_20261110.log")

    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == "pid_not_allowed"
    assert res["pid_deny_wording"] == "illegal pid"
    assert len(res["evidence"]) >= 2
    # 扩了正则，「服务未授权启动」仍不得被误归为 PID 拉黑（D2 护栏不退化）。
    assert G._PID_DENY_RE.search("The XtQuantServer is not allowed to start.") is None


def test_old_and_new_pid_wordings_coexist_without_duplicate_evidence(tmp_path):
    """两套措辞并存（或同一行同时命中两个 marker）时证据不重复、归因不分叉。"""
    trade_dir, _ = _write_log(tmp_path, [
        _auth_line(_GUOXIN_FLAGS),
        b"2026-11-10 08:27:01 quant session 1, pid 4242 not allowed, return\n",
        b"2026-11-10 20:55:57 quant session 900001, illegal pid 21664 not allowed\n",
    ], name="XtClient_20261110.log")

    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == "pid_not_allowed"
    assert res["session_retry_useless"] is True
    # 第二行同时含 "not allowed" 与 "illegal pid"，去重后只能出现一次。
    assert len(set(res["evidence"])) == len(res["evidence"]), res["evidence"]
    assert any("4242" in e for e in res["evidence"])
    assert any("21664" in e for e in res["evidence"])
    assert "联系券商" in res["steps"]


# ------------------------------------------------ §22.R7 早停：增量游标 + PID 拒绝归属
# 背景：客户端按调用进程 PID 拒绝时，换 session / 换目录都不会改变结果，但旧实现
# 仍会跑完 6 session × 2 目录（最多 12 次 connect、最长 45s）。要早停就必须先解决
# 「怎么证明这条拒绝行是**这次**尝试产生的」—— 当日日志 17MB+，历史里躺着上一天的
# `illegal pid`，直接扫尾窗口必然误杀。故用「尝试前取游标 → 只扫增量 → 按 session 归属」。

_DENY_HIST = b"2026-11-09 09:00:00 quant session 3, illegal pid 1111\n"


def test_log_cursor_is_file_size_so_history_is_excluded(tmp_path):
    """游标 = 尝试前的文件长度；历史拒绝行落在游标之前，不得被算进本次尝试。"""
    trade_dir, p = _write_log(tmp_path, [_auth_line(_GUOXIN_FLAGS), _DENY_HIST],
                              name="XtClient_20261110.log")
    cur = G.log_cursor(str(trade_dir))
    assert cur["log"].endswith("XtClient_20261110.log")
    assert cur["offset"] == p.stat().st_size

    # 尝试前：增量里空无一物 ⇒ 不早停
    assert G.pid_denial_since(cur)["denied"] is False

    # 本次尝试写进一条新拒绝行 ⇒ 只认这一条
    with open(p, "ab") as f:
        f.write(b"2026-11-10 22:05:56 quant session 7, illegal pid 24620\n")
    got = G.pid_denial_since(cur, sessions=[7])
    assert got["denied"] is True
    assert got["wording"] == "illegal pid"
    assert got["sessions"] == [7]
    assert len(got["lines"]) == 1 and "24620" in got["lines"][0]
    # 游标必须前进，否则同一段会被反复扫描
    assert got["next_offset"] > cur["offset"]


def test_pid_denial_since_rejects_denial_belonging_to_other_session(tmp_path):
    """增量里有拒绝行、但属于**别的** session ⇒ 不能据此早停（宁可继续重试）。"""
    trade_dir, p = _write_log(tmp_path, [_auth_line(_GUOXIN_FLAGS)],
                              name="XtClient_20261110.log")
    cur = G.log_cursor(str(trade_dir))
    with open(p, "ab") as f:
        f.write(b"2026-11-10 22:06:00 quant session 3, illegal pid 999\n")
    got = G.pid_denial_since(cur, sessions=[7])
    assert got["denied"] is False
    assert got["sessions"] == [3]
    # 反向锚：不限 session 时同一段就是有效证据（证明不是解析失败，而是归属过滤）
    assert G.pid_denial_since(cur)["denied"] is True


def test_pid_denial_since_accepts_line_without_session_token(tmp_path):
    """无法归属会话的措辞（无 `quant session`）仍算拒绝：无法归属 ≠ 不是拒绝。"""
    trade_dir, p = _write_log(tmp_path, [_auth_line(_GUOXIN_FLAGS)],
                              name="XtClient_20261110.log")
    cur = G.log_cursor(str(trade_dir))
    with open(p, "ab") as f:
        f.write(b"2026-11-10 22:06:01 [TC::COrderService] pid 24620 not allowed\n")
    got = G.pid_denial_since(cur, sessions=[7])
    assert got["denied"] is True
    assert got["sessions"] == []
    assert got["wording"] == "pid not allowed"


def test_pid_denial_since_recovers_when_log_is_truncated(tmp_path):
    """日志被轮转/截断（size < offset）时从 0 重扫，而不是永久失明。"""
    trade_dir, p = _write_log(tmp_path, [_auth_line(_GUOXIN_FLAGS), _DENY_HIST],
                              name="XtClient_20261110.log")
    cur = G.log_cursor(str(trade_dir))
    p.write_bytes(b"2026-11-10 22:07:00 quant session 7, illegal pid 24620\n")
    got = G.pid_denial_since(cur, sessions=[7])
    assert got["denied"] is True


def test_pid_denial_since_is_silent_when_log_missing():
    """没有日志时不抛异常、不臆断（诊断崩掉比诊断不到更糟）。"""
    assert G.log_cursor("")["log"] == ""
    assert G.pid_denial_since({})["denied"] is False
    assert G.pid_denial_since({"log": "nope.log", "offset": 0})["denied"] is False


# ------------------------------------------------ §22.R7 三轴证据：互不顶替

def test_probe_xtdata_distinguishes_refused_from_timeout():
    """`refused` 才能说「未监听」；`timeout` 只能说「不可用」，不得断言进程未启动。"""
    def _refused(addr, timeout):
        raise ConnectionRefusedError(10061, "refused")

    def _timeout(addr, timeout):
        raise TimeoutError("timed out")

    ref = G.probe_xtdata_service(_connect=_refused)
    assert ref["listening"] is False and ref["cause"] == "refused"
    assert "未监听" in ref["detail"]

    tmo = G.probe_xtdata_service(_connect=_timeout)
    assert tmo["listening"] is False and tmo["cause"] == "timeout"
    assert "不能" in tmo["detail"] and "断定客户端未启动" in tmo["detail"]
    assert "未监听" not in tmo["detail"]

    ok = G.probe_xtdata_service(_connect=lambda addr, timeout: None)
    assert ok["listening"] is True and ok["cause"] == "ok"
    # 反向锚：可连接 ≠ 行情已证实
    assert "不等于行情已证实" in ok["detail"]


def test_tri_state_axes_never_substitute_for_each_other():
    """三轴并列：58610 未绑定 ≠ 交易会话失败 ≠ SDK 导入失败，任一轴都不得代表其余。"""
    bound = {"address": "127.0.0.1:58610", "listening": True, "cause": "ok",
             "detail": "端口可连接（服务已绑定）"}
    tri = G.diagnose_tri_state("", trader_rc=-1, sdk_import_ok=None,
                               xtdata_probe=bound)
    assert tri["independent_axes"] is True
    assert tri["xtdata_service_bound"]["state"] == "bound"
    assert tri["trader_session_ok"]["state"] == "failed"
    # 未检查就是未检查，不得因为别的轴 ok 就填成 ok
    assert tri["sdk_import_ok"]["state"] == "unchecked"

    unusable = {"address": "127.0.0.1:58610", "listening": False,
                "cause": "timeout", "detail": "无应答"}
    tri2 = G.diagnose_tri_state("", trader_rc=0, sdk_import_ok=True,
                                xtdata_probe=unusable)
    assert tri2["xtdata_service_bound"]["state"] == "unusable"
    assert tri2["trader_session_ok"]["state"] == "ok"
    assert tri2["sdk_import_ok"]["state"] == "ok"


def test_attach_tri_state_states_market_side_fact_only_when_unusable(tmp_path, monkeypatch):
    """行情侧结论必须由**实测**给出：不可用就如实说不可用，可连接就不下结论。"""
    trade_dir, _ = _write_log(tmp_path, [
        _auth_line(_GUOXIN_FLAGS),
        b"2026-11-10 22:05:56 quant session 7, illegal pid 24620\n",
    ], name="XtClient_20261110.log")
    res = G._diagnose_one(str(trade_dir))
    assert res["reason"] == "pid_not_allowed"

    monkeypatch.setattr(G, "probe_xtdata_service", lambda **k: {
        "address": "127.0.0.1:58610", "listening": False,
        "cause": "timeout", "detail": "无应答"})
    got = G._attach_tri_state(dict(res), str(trade_dir))
    assert got["tri_state"]["xtdata_service_bound"]["state"] == "unusable"
    assert "行情侧实测" in got["steps"]
    assert "不可用" in got["steps"]

    monkeypatch.setattr(G, "probe_xtdata_service", lambda **k: {
        "address": "127.0.0.1:58610", "listening": True,
        "cause": "ok", "detail": "端口可连接（服务已绑定）"})
    got2 = G._attach_tri_state(dict(res), str(trade_dir))
    assert got2["tri_state"]["xtdata_service_bound"]["state"] == "bound"
    # 反向锚：可连接时**不得**补写「行情不可用」，也不得改写成「行情可用」
    assert "行情侧实测" not in got2["steps"]


def test_steps_never_claim_xtdata_unaffected_without_evidence(tmp_path):
    """★ 旧文案无条件断言「行情（xtdata）不受影响」——58610 未监听时那是假话。"""
    trade_dir, _ = _write_log(tmp_path, [
        _auth_line(_GUOXIN_FLAGS),
        b"2026-11-10 22:05:56 quant session 7, illegal pid 24620\n",
    ], name="XtClient_20261110.log")
    res = G._diagnose_one(str(trade_dir))
    assert "行情（xtdata）不受影响" not in res["steps"]
    assert "行情功能可继续使用" not in res["steps"]
    # 但仍须说明「本条只裁决交易通道」，不能因为删掉假话就把边界也删了
    assert "只裁决**交易通道**" in res["steps"]
