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
