#!/usr/bin/env python
"""大 QMT 行情/交易接口巡检引擎（常驻脚本，带 mission 分档）。

为什么要有这个文件
------------------
2026-11-11 的三次定时巡检（09:30 / 10:00 / 11:00）各自在 ``output/`` 下
**重新生成**了一份探针（v1 36KB → v2 58KB → 11:00 再补一份 supplement）。后果：

* 三次跑的是三个不同程序，跨轮对比要靠事后补差分代码；
* 同一事实在不同轮里写法不同（``pid 24240`` 写死、``9 次`` 写死），可比性丢失；
* 11:00 那轮为了加两个轴又写了独立脚本，结果要手工合并进 JSON。

本文件把巡检收敛成**一个**引擎 + 三个 mission：

======  ======  ================================================================
mission 时段    职责
======  ======  ================================================================
preopen 09:30   开盘前：环境/登录态/授权位/端口矩阵/DBF —— **不扫方法面**
market  10:00   盘中：行情导出面（58600+58341 方法面、官方端口判活、Formula 实时日志）
trade   11:00   盘中：交易通道面（总线只读探测、PID 闸增量、全量 LISTENING 归属）
======  ======  ================================================================

三条硬规则（本轮新增，直接来自三次运行暴露的问题）
--------------------------------------------------
R1 **单次缺失不得称为「漂移」**。2026-11-11 08:33 曾观测到 58600 的
   ``getMarketData`` 返回 200005，据此在 §28 写了「方法面会漂移」；但 10:04 /
   10:24 / 10:26 / 11:04 四次连续采样全部 ``exists=True``。⇒ 存在性必须跨轮
   累积判定：``stable_present`` / ``stable_absent`` / ``flapping`` /
   ``transient_missing``，**只有 flapping 才允许说「漂移」**。
R2 **授权按登录下发** ⇒ 客户端重启（pid 变化）必须视为新的授权会话，
   授权位要重新采样，不能沿用上一轮结论。
R3 **未查的轴记 skipped，不记 unavailable**。跨 mission 缺轴若记成
   ``unreadable``，会伪装成「可比但读不到」；mission 声明覆盖哪些轴，
   没覆盖的明确 ``skipped``。

安全边界（与历次探针一致，逐条断言）
------------------------------------
* 只发只读方法名；``callFormula`` 只允许 ``gettradedetail`` 与不存在的校准命令，
  **绝不**发送 ``passorder`` / 撤单 / 下单（``_guard`` 断言式白名单，发帧前抛错）。
* 不改 ``broker.ini`` / ``Config.xml`` / ``xtdata.ini`` / ``isAutoBind`` /
  ``is_automonitor`` / ``orderplugin.conf``；不写 ``whitelist.json``；不改日志。
* 不启停任何客户端进程。
* DBF readiness 只读，**绝不**往监控目录写文件（armed 时写文件即等于下单）。
* 三态 ``ok`` / ``unavailable`` / ``skipped``；「未检查」绝不写成「已检查通过」。
* 任何异常都必须写出证据文件（``failed_preconditions`` 非空），不得静默退出。

用法::

    python scripts/qmt_iface_check.py --mission market
    python scripts/qmt_iface_check.py --mission trade --no-rpc
    python scripts/qmt_iface_check.py --mission preopen --print-summary
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from connectors.transports.xtq_rpc import (  # noqa: E402
    _FrameReader, bson_encode, build_request, classify_payload,
)
from connectors.qmt_file_order import (  # noqa: E402
    CLIENT_DBF_COLUMNS_BINARY, CLIENT_INSERT_TIME_FORMAT_BINARY, DBF_COLUMNS,
    INSERT_TIME_FORMAT, INSERT_TIME_FORMAT_EVIDENCE, file_order_readiness,
)

HOST = "127.0.0.1"
CLIENT = Path(os.environ.get("QMT_CLIENT_ROOT", r"P:/stock/guoxin_iquant_11"))
CLIENT_ALT = Path(os.environ.get("QMT_CLIENT_ROOT_ALT", r"P:/stock/gd_qmt"))
PORTS = (58600, 58610, 58670, 64000, 58620, 63100)
EXTRA_PORTS = (58341, 8086)
NOT_FOUND = 200005
REQ_ID = "qmtwork_iface_check_ro_v3"
CODE = "600000.SH"
SCHEMA = "qmt_work.iface_check.v1"

# ---- mission 声明：覆盖哪些轴（R3 的依据） ---------------------------------
MISSION_AXES: dict[str, tuple[str, ...]] = {
    "preopen": ("env", "auth", "ports", "dbf"),
    "market": ("env", "ports", "surface", "official_ports", "formula_realtime",
               "upstream", "dbf"),
    "trade": ("env", "auth", "ports", "bus", "pid_gate", "listening_inventory",
              "dbf"),
}
ALL_AXES = ("env", "auth", "ports", "surface", "official_ports",
            "formula_realtime", "upstream", "bus", "pid_gate",
            "listening_inventory", "dbf")

# ---- 安全守卫：本引擎允许发送的全部方法名 ----------------------------------
READONLY_PULL_FUNCS = frozenset({
    "getTag", "checkVersion", "getDataDir", "getAppBinDir",
    "getSectorList", "getStockListInSector", "getInstrumentDetail",
    "getMarketData", "getTradingDates", "getDividFactors", "getFullTick",
})
ALLOWED_FUNCS = READONLY_PULL_FUNCS | frozenset({
    "callFormula", "__qmtwork_calibration_unknown__",
})
FORBIDDEN = ("passorder", "passOrder", "placeOrder", "order_stock",
             "cancel", "撤单", "清仓", "下单")
TRADE_BUS_READONLY_COMMANDS = frozenset({"gettradedetail",
                                         "__qmtwork_unknown_cmd__"})


def _guard(func: str, params) -> None:
    """断言式白名单；任何非只读命令在发帧前就抛错。"""
    blob = repr((func, params)).lower()
    for bad in FORBIDDEN:
        if bad.lower() in blob:
            raise RuntimeError(f"拒绝发送非只读命令: {bad!r}")
    if func not in ALLOWED_FUNCS:
        raise RuntimeError(f"方法不在只读白名单: {func!r}")
    if func == "callFormula":
        cmd = params[1] if isinstance(params, (list, tuple)) and len(params) > 1 else None
        if cmd not in TRADE_BUS_READONLY_COMMANDS:
            raise RuntimeError(f"callFormula 只允许只读查询命令，实际: {cmd!r}")


def _shrink(obj, depth: int = 0):
    if depth > 3:
        return "<deep>"
    if isinstance(obj, (bytes, bytearray)):
        return f"<bytes {len(obj)}>"
    if isinstance(obj, dict):
        return {str(k): _shrink(v, depth + 1) for k, v in list(obj.items())[:8]}
    if isinstance(obj, (list, tuple)):
        return [_shrink(v, depth + 1) for v in list(obj)[:4]]
    if isinstance(obj, str):
        return obj[:160]
    return obj


# ---------------------------------------------------------------------------
# 端口 / RPC
# ---------------------------------------------------------------------------
def port_state(port: int, timeout: float = 1.2) -> dict:
    """逐个判定 listening / refused / timeout。**timeout != refused**。"""
    rec: dict = {"port": port, "state": "unavailable", "detail": ""}
    t0 = time.monotonic()
    try:
        s = socket.create_connection((HOST, port), timeout=timeout)
        s.close()
        rec["state"] = "ok"
        rec["detail"] = "TCP connect 成功（端口在监听并受理连接）"
    except ConnectionRefusedError:
        rec["detail"] = "ConnectionRefusedError ⇒ 本机该端口未监听（明确拒绝，服务不在）"
    except (TimeoutError, socket.timeout):
        rec["detail"] = ("TimeoutError ⇒ 端口不可用（连接超时）；"
                         "不能据此断定服务未启动，防火墙丢包/backlog 满同样如此")
    except OSError as exc:
        rec["detail"] = (f"{type(exc).__name__}: {exc} "
                         f"(winerror={getattr(exc, 'winerror', None)})")
    rec["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return rec


def rpc_call(port: int, func: str, params, *,
             deadline_s: float = 1.2, sock_timeout: float = 0.4) -> dict:
    _guard(func, params)
    rec: dict = {"port": port, "func": func, "state": "skipped"}
    try:
        sock = socket.create_connection((HOST, port), timeout=1.0)
    except ConnectionRefusedError as exc:
        rec.update(state="unavailable", detail=f"ConnectionRefusedError ⇒ 端口未监听: {exc}")
        return rec
    except (TimeoutError, socket.timeout) as exc:
        rec.update(state="unavailable", detail=f"TimeoutError ⇒ 端口不可用: {exc}")
        return rec
    except OSError as exc:
        rec.update(state="unavailable", detail=f"connect {type(exc).__name__}: {exc}")
        return rec
    try:
        sock.settimeout(sock_timeout)
        sock.sendall(build_request(func, params, seq=1))
        reader = _FrameReader(sock)
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            f = reader.read()
            if f is None or f.is_heartbeat:
                continue
            doc = f.decode()
            p = doc.get("params") or {}
            rec["state"] = "ok"          # 有应答 = 已检查（不代表方法存在）
            rec["status"] = doc.get("status")
            if isinstance(p, dict):
                rec["ErrorID"] = p.get("ErrorID")
                rec["ErrorMsg"] = p.get("ErrorMsg")
                rec["result_keys"] = sorted(str(k) for k in p)[:14]
                rec["method_exists"] = p.get("ErrorID") != NOT_FOUND
                result = p.get("result")
                rec["result_class"] = classify_payload(result)
                rec["result_sample"] = _shrink(result)
            return rec
        rec.update(state="unavailable", detail=f"已连接但 {deadline_s}s 内无业务应答")
        return rec
    except Exception as exc:  # noqa: BLE001 探针异常不得冒泡
        rec.update(state="unavailable", detail=f"{type(exc).__name__}: {exc}")
        return rec
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 进程 / 连接
# ---------------------------------------------------------------------------
def process_snapshot() -> dict:
    try:
        txt = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=25).stdout.decode(
                                 "utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"state": "skipped", "detail": str(exc)}
    want = {"XtItClient.exe": None, "XtMiniQuote.exe": None,
            "XtModel.exe": None, "miniquote.exe": None}
    for line in txt.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if not cols:
            continue
        name = cols[0].strip('"')
        for k in want:
            if name.lower() == k.lower() and len(cols) >= 2:
                want[k] = {"pid": int(cols[1]) if cols[1].isdigit() else cols[1],
                           "mem": cols[4] if len(cols) > 4 else ""}
    return {k: ({"state": "ok", "running": True, **v} if v else
                {"state": "unavailable", "running": False})
            for k, v in want.items()}


def _cim_processes() -> dict[int, dict]:
    ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,Name,"
          "ExecutablePath,ParentProcessId | ConvertTo-Json -Compress")
    try:
        raw = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=45).stdout.decode(
                                 "utf-8", errors="replace").strip()
        data = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(data, dict):
        data = [data]
    out = {}
    for row in data:
        pid = row.get("ProcessId")
        if isinstance(pid, int):
            out[pid] = {"name": row.get("Name") or "",
                        "path": (row.get("ExecutablePath") or "").replace("\\", "/"),
                        "ppid": row.get("ParentProcessId")}
    return out


def process_attribution(procs: dict[int, dict] | None = None) -> dict:
    """动态取进程安装归属，绝不写死 PID/路径。"""
    procs = _cim_processes() if procs is None else procs
    if not procs:
        return {"state": "skipped", "running": [],
                "detail": "Get-CimInstance 不可用"}
    running = []
    for pid, info in procs.items():
        name = info["name"]
        if not re.match(r"(?i)^(XtItClient|XtMiniQuote|XtModel|miniquote)\.exe$", name):
            continue
        path = info["path"]
        install = ("guoxin_iquant_11"
                   if path.lower().startswith(str(CLIENT).replace("\\", "/").lower())
                   else "gd_qmt"
                   if path.lower().startswith(str(CLIENT_ALT).replace("\\", "/").lower())
                   else "other")
        running.append({"pid": pid, "name": name, "path": path,
                        "install": install})
    return {
        "state": "ok",
        "source": "PowerShell Get-CimInstance Win32_Process",
        "running": running,
        "client_running_by_install": {
            label: any(r["install"] == label and r["name"].lower() == "xtitclient.exe"
                       for r in running)
            for label in ("guoxin_iquant_11", "gd_qmt")
        },
    }


def _netstat_tcp() -> str:
    return subprocess.run(["netstat", "-ano", "-p", "tcp"],
                          capture_output=True, timeout=25).stdout.decode(
                              "utf-8", errors="replace")


def netstat_listen(*ports: int) -> dict:
    try:
        txt = _netstat_tcp()
    except Exception as exc:  # noqa: BLE001
        return {"state": "skipped", "detail": f"{type(exc).__name__}: {exc}"}
    out: dict = {"state": "ok"}
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[-1].isdigit() and "LISTENING" in line:
            local = parts[1]
            if ":" in local:
                p = int(local.rsplit(":", 1)[1])
                if p in ports:
                    out.setdefault(str(p), []).append(
                        {"local": local, "pid": int(parts[-1])})
    return out


def listening_inventory(procs: dict[int, dict] | None = None) -> dict:
    """全量 TCP LISTENING + 归属，用来判定端口矩阵是否完备。"""
    try:
        txt = _netstat_tcp()
    except Exception as exc:  # noqa: BLE001
        return {"state": "skipped", "detail": f"{type(exc).__name__}: {exc}"}
    procs = _cim_processes() if procs is None else procs
    rows: list[dict] = []
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        if not parts[4].isdigit():
            continue
        local = parts[1]
        if ":" not in local:
            continue
        pid = int(parts[4])
        info = procs.get(pid) or {}
        path = info.get("path", "")
        name = info.get("name", "")
        low = path.lower()
        if low.startswith(str(CLIENT).replace("\\", "/").lower()):
            klass = "qmt_install_guoxin"
        elif low.startswith(str(CLIENT_ALT).replace("\\", "/").lower()):
            klass = "qmt_install_gd"
        elif "guosenpassguard" in low:
            klass = "broker_security_component"
        else:
            klass = "other"
        rows.append({"local": local, "port": int(local.rsplit(":", 1)[1]),
                     "pid": pid, "name": name, "path": path, "class": klass})
    relevant = [r for r in rows if r["class"] != "other"
                or r["port"] in PORTS or r["port"] in EXTRA_PORTS]
    for r in relevant:
        r["in_probe_matrix"] = r["port"] in PORTS or r["port"] in EXTRA_PORTS
    return {
        "state": "ok",
        "source": "netstat -ano -p tcp + Get-CimInstance Win32_Process",
        "total_listening_endpoints": len(rows),
        "total_distinct_listening_ports": len({r["port"] for r in rows}),
        "qmt_or_broker_owned": relevant,
        "qmt_owned_not_in_matrix": [r for r in relevant
                                    if r["class"] != "other" and not r["in_probe_matrix"]],
        "boundary": ("只覆盖 netstat 可见的 TCP LISTENING；不覆盖 UDP、共享内存、"
                     "命名管道、进程内调用、出站连接 ⇒「完备」仅指本机 TCP 监听端点"),
    }


def _established(needle: str) -> dict:
    try:
        txt = _netstat_tcp()
    except Exception as exc:  # noqa: BLE001
        return {"state": "skipped", "detail": str(exc)}
    hits = [l.strip() for l in txt.splitlines()
            if needle in l and "ESTABLISHED" in l]
    return {"state": "ok" if hits else "unavailable", "count": len(hits),
            "lines": hits[:6]}


def gts_established() -> dict:
    rec = _established("59.36.9.32:56000")
    rec["meaning"] = ("柜台级在线证据；只证明与交易服务的 TCP 连接存在，"
                      "不证明有下单权限")
    return rec


def quote_upstream_established() -> dict:
    rec = _established(":55300")
    rec["meaning"] = ("仅证明客户端与行情云有 ESTABLISHED 连接；"
                      "连接存在 ≠ 有数据 ≠ 本地 RPC 可取到 tick")
    return rec


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log_path_for_today() -> Path:
    return CLIENT / "userdata" / "log" / ("XtClient_%s.log"
                                          % datetime.now().strftime("%Y%m%d"))


def formula_log_path_for_today() -> Path:
    return CLIENT / "userdata" / "log" / ("XtClient_Formula_%s.log"
                                          % datetime.now().strftime("%Y%m%d"))


AUTH_KEYS = ("mdl_auth_xtquant", "mdl_auth_xtquant_no_pid_check",
             "mdl_auth_broker_whitelist", "mdl_auth_gt_ipc_pair",
             "mdl_auth_xtquant_strict_connection_check")


def log_facts(path: Path) -> dict:
    out: dict = {"file": str(path).replace("\\", "/"), "state": "skipped"}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        out["detail"] = f"{type(exc).__name__}: {exc}"
        return out
    text = raw.decode("gbk", errors="replace")
    lines = text.splitlines()
    out.update({"state": "ok", "bytes": len(raw), "lines": len(lines),
                "decode": "gbk / errors=replace（混合编码）",
                "note": "行号以本次读取快照为准（日志仍在增长）"})

    def hits(pattern: str):
        return [(n, l.strip()) for n, l in enumerate(lines, 1)
                if re.search(pattern, l)]

    login = hits(r"onTradeServerLoginSuccess")
    out["trade_login"] = {
        "state": "ok" if login else "unavailable",
        "count": len(login),
        "first": {"line": login[0][0], "text": login[0][1][:200]} if login else None,
        "last": {"line": login[-1][0], "text": login[-1][1][:200]} if login else None,
    }
    acc = [(n, l.strip()) for n, l in enumerate(lines, 1) if "account_num" in l]
    nums = sorted({m.group(1) for _, l in acc
                   for m in [re.search(r"account_num\s*=\s*(\d+)", l)] if m})
    out["refresh_accounts"] = {
        "state": "ok" if acc else "unavailable", "count": len(acc),
        "account_num_values": nums,
        "last": {"line": acc[-1][0], "text": acc[-1][1][-180:]} if acc else None,
    }
    auth = []
    for n, l in enumerate(lines, 1):
        if "receive module auth string" not in l:
            continue
        vals = {}
        for k in AUTH_KEYS:
            m = re.search(r'"' + re.escape(k) + r'"\s*:\s*([0-9]+)', l)
            vals[k] = int(m.group(1)) if m else None
        auth.append({"line": n, "time": l[:23], "bytes": len(l), "values": vals})
    out["auth_string_sessions"] = auth
    out["auth_bits_latest"] = auth[-1]["values"] if auth else None
    out["auth_bits_stable_across_sessions"] = (
        len(auth) > 1 and all(a["values"] == auth[0]["values"] for a in auth))

    illegal = hits(r"quant session \d+, illegal pid \d+")
    connected = hits(r"quant session \d+, pid \d+ connected")
    out["pid_gate"] = {
        "illegal_pid_count": len(illegal),
        "pid_connected_count": len(connected),
        "illegal_pid_lines": [n for n, _ in illegal],
        "illegal_pid_pids": sorted({m.group(1) for _, l in illegal
                                    for m in [re.search(r"illegal pid (\d+)", l)] if m}),
        "illegal_pid_time_range": ([illegal[0][1][:23], illegal[-1][1][:23]]
                                   if illegal else []),
        "illegal_IP_count": sum(1 for l in lines
                                if "illegal IP" in l or "illegal Ip" in l),
        "note": ("当日累计计数；归属靠 log_delta 的增量游标判定，"
                 "不得把历史累计算成本轮新产生"),
    }
    cipc = hits(r"CIPCManager::init")
    out["cipc_manager"] = {
        "state": "unavailable" if cipc else "skipped",
        "init_lines": [{"line": n, "text": l[-140:]} for n, l in cipc],
        "userdata_ipc_dir_exists": (CLIENT / "userdata_ipc").is_dir(),
        "init_auth_granted": any("not auth" not in l for _, l in cipc),
    }
    out["account_expired_count"] = sum(1 for l in lines if "account is expired" in l)
    out["whitelist_manager_count"] = sum(1 for l in lines if "CWhiteListManager" in l)
    out["quant_framework_whitelist_query_count"] = sum(
        1 for l in lines if "QueryFrameworkWhiteList" in l)
    out["wholequote_lines"] = [l[:160] for _, l in hits(r"wholequote")][:6]
    return out


def log_delta(path: Path, before: int) -> dict:
    """只扫「本次探测之后新增的字节」，解决 PID 拒绝行的归属问题。"""
    try:
        after = path.stat().st_size
    except OSError as exc:
        return {"state": "skipped", "detail": f"{type(exc).__name__}: {exc}"}
    if after < before:
        return {"state": "skipped", "before": before, "after": after,
                "detail": "日志被截断（size<offset）⇒ 游标失效，本轮不做归属判定"}
    delta = b""
    if after > before:
        with path.open("rb") as fh:
            fh.seek(before)
            delta = fh.read(after - before)
    ls = delta.decode("gbk", errors="replace").splitlines()
    illegal = [l[-160:] for l in ls
               if re.search(r"quant session \d+, illegal pid \d+", l)]
    connected = [l[-160:] for l in ls
                 if re.search(r"quant session \d+, pid \d+ connected", l)]
    return {"state": "ok", "before_bytes": before, "after_bytes": after,
            "delta_bytes": after - before, "delta_lines": len(ls),
            "illegal_pid_in_delta": len(illegal),
            "pid_connected_in_delta": len(connected),
            "illegal_pid_samples": illegal[:4],
            "detail": ("增量里没有新的 PID 闸拒绝/放行行 ⇒ 本轮只读 RPC 未触发新的 PID 鉴权"
                       if not illegal and not connected else
                       "增量里出现 PID 闸行 ⇒ 需逐行核对 session 归属，不得直接归因")}


def formula_realtime(path: Path) -> dict:
    """客户端内部 Formula 引擎的实时计算日志。

    ★ 这是「数据在客户端内部是活的」的唯一直接证据；但它**不是** tick 证明，
      周期字段未知 ⇒ 不得写进 tick_proven。
    """
    out: dict = {"file": str(path).replace("\\", "/"), "state": "skipped"}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        out["detail"] = f"{type(exc).__name__}: {exc}"
        return out
    text = raw.decode("gbk", errors="replace")
    lines = text.splitlines()
    complete = [(n, l) for n, l in enumerate(lines, 1) if "data complete, start calc" in l]
    sent = [(n, l) for n, l in enumerate(lines, 1) if "send result" in l]
    hours: dict[str, int] = {}
    for _, l in complete:
        m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}):", l.strip())
        if m:
            hours[m.group(1)] = hours.get(m.group(1), 0) + 1
    reqids = sorted({m.group(1) for _, l in complete
                     for m in [re.search(r"\[model\] (\w+),", l)] if m})
    out.update({
        "state": "ok" if complete else "unavailable",
        "log_bytes": len(raw), "log_lines": len(lines),
        "realtime_data_complete_count": len(complete),
        "realtime_send_result_count": len(sent),
        "hourly_distribution": dict(sorted(hours.items())),
        "distinct_realtime_reqids": reqids[:12],
        "first": ({"line": complete[0][0], "text": complete[0][1][:190]}
                  if complete else None),
        "last": ({"line": complete[-1][0], "text": complete[-1][1][:190]}
                 if complete else None),
        "meaning": ("交易时段内客户端内部公式模型持续收到实时数据并完成计算 ⇒ "
                    "上游行情源与客户端内部数据链是活的；行情不可用不是"
                    "「没开盘/上游断流」，而是「没有可用的对外接口把数据导出来」"),
        "boundary": ("这是客户端进程内的公式模型日志，不等于外部进程可通过 RPC "
                     "取到 tick，也不证明 tick 级数据完备（可能只是分钟级 K 线）⇒ "
                     "本轴不得写进 tick_proven"),
    })
    return out


# ---------------------------------------------------------------------------
# 方法面
# ---------------------------------------------------------------------------
def surface_cases() -> dict:
    """同一套只读方法名，用于 58600 / 58341 直接对照。"""
    return {
        "getTag": [],
        "checkVersion": [],
        "getDataDir": [],
        "getAppBinDir": [],
        "getSectorList": [],
        "getStockListInSector": ["沪深A股"],
        "getInstrumentDetail": [CODE],
        "getMarketData_1d": [CODE, "1d", 0, 5],
        "getMarketData_1m": [CODE, "1m", 0, 5],
        "getTradingDates": [CODE, "", "", 5, "1d"],
        "getDividFactors": [CODE],
        "getFullTick": [CODE],
        "__qmtwork_calibration_unknown__": [],
    }


def run_surface(port: int, cases: dict | None = None) -> dict:
    out = {}
    for name, params in (cases or surface_cases()).items():
        func = "getMarketData" if name.startswith("getMarketData") else name
        rec = rpc_call(port, func, params)
        rec["request_params"] = repr(params)[:160]
        if name.startswith("getMarketData"):
            rec["period"], rec["bar_or_tick"] = params[1], "bar"
        elif name == "getFullTick":
            rec["period"], rec["bar_or_tick"] = "tick", "tick"
        rc = rec.get("result_class") or {}
        rec["method_available"] = "ok" if rec.get("method_exists") else "unavailable"
        rec["data_available"] = (
            "skipped" if not rec.get("method_exists")
            else ("ok" if rc.get("size", 0) > 0 else "unavailable"))
        out[name] = rec
    return out


def official_port_liveness(ports_state: dict) -> dict:
    """官方实时端口只在**监听时**才发只读行情判活。

    ★ 不用交易总线 gettradedetail 当行情闸门 —— 那是分类错误：
      总线通不通与能否取到实时行情是两根独立的轴。
    """
    cases = {
        "getTag": [],
        "getInstrumentDetail": [CODE],
        "getMarketData_1d": [CODE, "1d", 0, 5],
        "getFullTick": [CODE],
        "__qmtwork_calibration_unknown__": [],
    }
    out: dict = {}
    for port in (58610, 58670, 64000):
        st = (ports_state.get(str(port)) or {}).get("state")
        if st != "ok":
            out[str(port)] = {"state": "skipped",
                              "detail": "端口 unavailable，未发起 RPC"}
            continue
        out[str(port)] = run_surface(port, cases)
    return out


def trade_bus_probe(port: int) -> dict:
    inner = bson_encode({"accountid": "", "accounttype": "STOCK",
                         "datatype": "ACCOUNT", "strategyname": ""})
    gd = rpc_call(port, "callFormula", [REQ_ID, "gettradedetail", inner])
    un = rpc_call(port, "callFormula",
                  [REQ_ID, "__qmtwork_unknown_cmd__", bson_encode({})])
    for rec in (gd, un):
        rec["bus_available"] = "ok" if rec.get("method_exists") else "unavailable"
    return {"gettradedetail": gd, "unknown_command": un}


# ---------------------------------------------------------------------------
# R1 判据：跨轮存在性累积（单次缺失 ≠ 漂移）
# ---------------------------------------------------------------------------
PRESENCE_LABELS = ("stable_present", "stable_absent", "flapping",
                   "transient_missing", "first_sample", "unknown")


def classify_presence(series: list) -> str:
    """按时间正序的存在性序列判稳定性。

    * 全部 True  ⇒ stable_present
    * 全部 False ⇒ stable_absent
    * 有 True 有 False ⇒ flapping（**只有这一种才允许说「漂移」**）
    * 最近一次是 False 但历史只有 True ⇒ transient_missing
    """
    vals = [v for v in series if isinstance(v, bool)]
    if not vals:
        return "unknown"
    if len(vals) == 1:
        return "first_sample"
    if all(vals):
        return "stable_present"
    if not any(vals):
        return "stable_absent"
    if vals[-1] is False and all(vals[:-1]):
        return "transient_missing"
    return "flapping"


def drift_claim_allowed(label: str) -> bool:
    """R1：只有 flapping 才允许对外说「方法面漂移」。"""
    return label == "flapping"


def _surface_existence(doc: dict) -> dict:
    market = (doc or {}).get("market") or {}
    surf = market.get("surface_58600") or {}
    round_a = surf.get("round_A") or surf
    out = {}
    for name, rec in (round_a or {}).items():
        if isinstance(rec, dict) and "method_exists" in rec:
            out[name] = bool(rec["method_exists"])
    return out


def _client_pid(doc: dict):
    procs = ((doc or {}).get("preconditions") or {}).get("processes") or {}
    rec = procs.get("XtItClient.exe") or {}
    pid = rec.get("pid")
    return pid if isinstance(pid, int) else None


def load_history(pattern: str = "qmt_iface_check_*.json", limit: int = 6) -> list[dict]:
    """按 sampled_at 正序返回历史证据（用于 R1 累积与 R2 会话边界）。"""
    out = []
    for p in sorted((ROOT / "output").glob(pattern)):
        if p.name.startswith("_draft"):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        out.append({"file": p.name, "sampled_at": doc.get("sampled_at") or "",
                    "mission": doc.get("mission"),
                    "surface": _surface_existence(doc),
                    "client_pid": _client_pid(doc),
                    "auth_bits": (((doc.get("preconditions") or {})
                                   .get("log") or {}).get("auth_bits_latest"))})
    out.sort(key=lambda d: d["sampled_at"])
    return out[-limit:]


def stability_report(history: list[dict], current_surface: dict) -> dict:
    """把当前采样拼到历史后面，逐方法给稳定性标签。"""
    names = sorted({n for h in history for n in h["surface"]} | set(current_surface))
    methods = {}
    for name in names:
        # ★ 只把**真正采过**该方法的轮次算进序列：没采该轴的 mission 不贡献样本，
        #   否则会留下 None 冒充「读了但读不到」。
        series = [h["surface"][name] for h in history if name in h["surface"]]
        series.append(current_surface.get(name))
        label = classify_presence(series)
        methods[name] = {
            "series": series,
            "label": label,
            "drift_claim_allowed": drift_claim_allowed(label),
            "note": ("单次缺失不足以称漂移；只有 flapping 才算" if label != "flapping"
                     else "存在性在历史采样中反复变化 ⇒ 可称漂移"),
        }
    return {
        "state": "ok" if history else "first_sample",
        "samples_used": len(history),
        "sample_files": [h["file"] for h in history],
        "methods": methods,
        "rule": ("R1：跨轮累积判定；stable_present / stable_absent / flapping / "
                 "transient_missing。只有 flapping 允许说「漂移」"),
    }


def session_boundary(history: list[dict], current_pid) -> dict:
    """R2：客户端重启 ⇒ 新的授权会话，授权位必须重新采样。"""
    prev = [h for h in history if h.get("client_pid") is not None]
    if not prev:
        return {"state": "first_sample", "current_pid": current_pid,
                "note": "无历史 pid 可比，按新会话处理"}
    last = prev[-1]
    restarted = (current_pid is not None and last["client_pid"] != current_pid)
    return {
        "state": "ok",
        "current_pid": current_pid,
        "previous_pid": last["client_pid"],
        "previous_file": last["file"],
        "client_restarted": restarted,
        "note": ("客户端 pid 变了 ⇒ 视为新的登录/授权会话，授权位必须重新采样，"
                 "不得沿用上一轮结论" if restarted else
                 "pid 未变 ⇒ 同一会话；授权位仍重新读取但预期不变"),
    }


# ---------------------------------------------------------------------------
# DBF / 配置
# ---------------------------------------------------------------------------
def dbf_facts(client_running_by_install: dict) -> dict:
    out: dict = {}
    for label, root in (("guoxin_iquant_11", CLIENT), ("gd_qmt", CLIENT_ALT)):
        userdata = str(root / "userdata")
        client_running = client_running_by_install.get(label, False)
        try:
            r = file_order_readiness(userdata, client_running=client_running)
            for step in r.get("steps", []):
                step["state"] = "ok" if step.get("ok") else "unavailable"
            r["state"] = "ok" if r.get("ready") else "unavailable"
            r["client_running_reported"] = client_running
            r["conclusion"] = (
                "无法产生委托：is_automonitor=0 ⇒ 文件单总开关关闭，"
                "往监控目录写 DBF 只会躺在目录里" if not r.get("armed")
                else "armed 为真，但本轮未做任何写探测/未投单")
            r["probe_write_skipped"] = True
            out[label] = r
        except Exception as exc:  # noqa: BLE001
            out[label] = {"state": "unavailable",
                          "error": f"{type(exc).__name__}: {exc}"}
    out["insert_time_format"] = {
        "ours": INSERT_TIME_FORMAT,
        "client_binary": CLIENT_INSERT_TIME_FORMAT_BINARY,
        "evidence": dict(INSERT_TIME_FORMAT_EVIDENCE),
    }
    out["columns_verified"] = tuple(DBF_COLUMNS) == CLIENT_DBF_COLUMNS_BINARY
    out["honesty"] = ("readiness 全程只读；未向监控目录写入任何文件"
                      "（armed 时写文件即等于下单）；probe_write_skipped=True")
    return out


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def _axis_state(doc: dict, axis: str) -> str:
    """把每个轴压成三态；结构缺失/未采一律 skipped，**绝不猜成 ok**。"""
    try:
        if axis == "env":
            procs = doc["preconditions"]["processes"]
            return "ok" if procs["XtItClient.exe"].get("running") else "unavailable"
        if axis == "auth":
            return ("ok" if doc["preconditions"]["log"].get("auth_bits_latest")
                    else "unavailable")
        if axis == "ports":
            return ("ok" if doc["market"]["port_matrix_summary"]["ok_listening"]
                    else "unavailable")
        if axis == "surface":
            surf = (doc["market"].get("surface_58600") or {}).get("round_A") or {}
            return ("ok" if any(isinstance(r, dict) and r.get("method_exists")
                                for r in surf.values()) else "unavailable")
        if axis == "official_ports":
            rec = doc["market"].get("official_ports") or {}
            return ("ok" if any(isinstance(v, dict) and v.get("state") == "ok"
                                for v in rec.values()) else "unavailable")
        if axis == "formula_realtime":
            return (doc["market"].get("formula_realtime") or {}).get("state", "skipped")
        if axis == "upstream":
            return (doc["market"].get("upstream_quote_server") or {}).get("state",
                                                                         "skipped")
        if axis == "bus":
            buses = doc["trade"].get("bus_probe") or {}
            return ("ok" if any(
                isinstance(v, dict) and isinstance(v.get("gettradedetail"), dict)
                and v["gettradedetail"].get("method_exists")
                for v in buses.values()) else "unavailable")
        if axis == "pid_gate":
            return (doc["trade"].get("pid_gate") or {}).get("state", "skipped")
        if axis == "listening_inventory":
            return (doc["trade"].get("listening_inventory") or {}).get("state",
                                                                       "skipped")
        if axis == "dbf":
            return doc["dbf"].get("state", "skipped")
    except (KeyError, AttributeError, TypeError):
        return "skipped"
    return "skipped"


def build_doc(mission: str, *, no_rpc: bool, started: str) -> dict:
    axes = MISSION_AXES[mission]
    skipped_axes = [a for a in ALL_AXES if a not in axes]

    procs_cim = _cim_processes()
    processes = process_snapshot()
    attribution = process_attribution(procs_cim)
    client_pid = processes.get("XtItClient.exe", {}).get("pid")
    client_pid = client_pid if isinstance(client_pid, int) else None

    log_path = log_path_for_today()
    log_before = log_path.stat().st_size if log_path.exists() else 0

    doc: dict = {
        "schema": SCHEMA,
        "mission": mission,
        "sampled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": started,
        "mode": ("只读探测" + ("（--no-rpc：不发任何 RPC 帧）" if no_rpc else "")
                 + "；未改配置、未投单、未启停客户端"),
        "axes_covered": list(axes),
        "axes_skipped_by_mission": skipped_axes,
        "commands_sent": (["只读方法名：getTag/checkVersion/getDataDir/getAppBinDir/"
                           "getSectorList/getStockListInSector/getInstrumentDetail/"
                           "getMarketData/getTradingDates/getDividFactors/getFullTick"
                           "（仅在对应 mission 内）"]
                          if not no_rpc else []),
        "commands_never_sent": ["passorder", "passOrder", "placeOrder",
                                "order_stock", "cancel", "任何撤单/清仓命令"],
        "constraints_honored": [
            "未伪造凭证/授权；未绕过 PID 白名单",
            "未改 broker.ini / Config.xml / xtdata.ini / isAutoBind / "
            "is_automonitor / orderplugin.conf",
            "未发送 passorder 或任何下单撤单命令",
            "未启停任何客户端进程",
            "DBF 只读，未向监控目录写入任何文件",
        ],
        "preconditions": {"processes": processes,
                          "process_install_attribution": attribution,
                          "client_pid": client_pid,
                          "log": {}},
        "market": {}, "trade": {}, "dbf": {},
    }

    # ---- 端口（所有 mission 都要，它是判定其余轴的前提） ----
    ports_state = {str(p): port_state(p) for p in PORTS}
    extra_state = {str(p): port_state(p) for p in EXTRA_PORTS}
    ok_listening = sorted(int(k) for k, v in ports_state.items() if v["state"] == "ok")
    timeout = sorted(int(k) for k, v in ports_state.items()
                     if v["state"] == "unavailable"
                     and "TimeoutError" in v.get("detail", ""))
    refused = sorted(int(k) for k, v in ports_state.items()
                     if v["state"] == "unavailable"
                     and "ConnectionRefusedError" in v.get("detail", ""))
    doc["market"].update({
        "state_semantics": ("方法记录 state=ok 只表示「收到应答、已检查」，"
                            "不代表方法存在或有数据；存在性看 method_exists/ErrorID；"
                            "端口 state=ok 才表示端口在监听"),
        "ports": ports_state,
        "extra_ports": extra_state,
        "port_matrix_summary": {
            "checked": len(PORTS), "ok_listening": ok_listening,
            "timeout": timeout, "refused": refused,
            "wording": (f"{len(timeout)} 个端口 TimeoutError（端口不可用），"
                        f"{len(refused)} 个 ConnectionRefusedError；"
                        "超时不能写成「服务未监听/拒绝」"),
        },
        "config": {"file": str(CLIENT / "config" / "xtdata.ini").replace("\\", "/"),
                   "note": "官方 xtdata 入口是 58670（XtModel），不是 58600 / 58341"},
    })

    # ---- 环境与登录态 ----
    doc["preconditions"]["gts_established"] = gts_established()
    doc["preconditions"]["log"] = log_facts(log_path)
    if "upstream" in axes:
        doc["market"]["upstream_quote_server"] = quote_upstream_established()

    # ---- 方法面 ----
    if "surface" in axes and not no_rpc:
        round_a = run_surface(58600)
        round_b = run_surface(58600)
        doc["market"]["surface_58600"] = {
            "round_A": round_a,
            "round_B": round_b,
            "intra_run_drift": sum(
                1 for k in round_a
                if round_a[k].get("method_exists") != round_b[k].get("method_exists")),
            "note": ("A/B 同轮两次采样用于区分「跨轮漂移」与「单次运行内漂移」；"
                     "intra_run_drift=0 表示同一轮内方法面稳定"),
        }
        doc["market"]["surface_58341_miniquote"] = run_surface(58341)
        doc["market"]["method_absence_calibration"] = {
            "anchor": "__qmtwork_calibration_unknown__",
            "criterion": f"ErrorID={NOT_FOUND}「未找到处理函数」= 方法不存在",
            "positive_control": "同一端口上 getInstrumentDetail 若返回 status=0 则证明总线在应答",
        }
    elif "surface" in axes and no_rpc:
        doc["market"]["surface_58600"] = {"state": "skipped",
                                          "detail": "--no-rpc：未发起 RPC"}

    if "official_ports" in axes and not no_rpc:
        doc["market"]["official_ports"] = official_port_liveness(ports_state)

    if "formula_realtime" in axes:
        doc["market"]["formula_realtime"] = formula_realtime(
            formula_log_path_for_today())

    # ---- 行情证据分级 ----
    tick_proven = False
    surf = (doc["market"].get("surface_58600") or {}).get("round_A") or {}
    tick_rec = surf.get("getFullTick")
    if isinstance(tick_rec, dict) and tick_rec.get("method_exists"):
        rc = tick_rec.get("result_class") or {}
        tick_proven = bool(rc.get("quote_proven") and rc.get("size", 0) > 0)
    doc["market"]["evidence_grading"] = {
        "bar_fields_not_realtime": ("日线/分钟线同样带 open/high/low ⇒ "
                                    "不能靠字段名推实时，必须 period 参与分级"),
        "realtime_eligible": bool(
            isinstance(tick_rec, dict) and tick_rec.get("method_exists")),
        "tick_proven": tick_proven,
        "why": ("58600 无 getFullTick（200005）⇒ 无 tick 级证据；"
                "Formula 引擎日志只证明进程内有实时计算，不构成对外可取 tick"
                if not tick_proven else "getFullTick 返回非空且含报价字段"),
    }
    doc["market"]["verdict"] = "ok" if tick_proven else "unavailable"

    # ---- 交易总线 ----
    if "bus" in axes and not no_rpc:
        doc["trade"]["bus_probe"] = {
            str(p): trade_bus_probe(p)
            for p in (*ok_listening, 58341, 8086)
        }
        doc["trade"]["bus_conclusion"] = (
            "可达端口上 callFormula('gettradedetail') 是否存在由 method_exists 逐项给出；"
            "58600 无该方法（200005）⇒ 它不是下单入口，与 §24 枚举一致")
    elif "bus" in axes:
        doc["trade"]["bus_probe"] = {"state": "skipped", "detail": "--no-rpc：未发起 RPC"}
        doc["trade"]["bus_conclusion"] = "本轮未探测（--no-rpc）"

    # ---- PID 闸增量 ----
    if "pid_gate" in axes:
        doc["trade"]["pid_gate"] = {
            "state": "ok",
            "cumulative": doc["preconditions"]["log"].get("pid_gate"),
            "delta_this_round": log_delta(log_path, log_before),
            "rule": ("增量里出现 illegal pid 才能归因到本轮；"
                     "当日累计不得当作本轮结果"),
        }

    if "listening_inventory" in axes:
        doc["trade"]["listening_inventory"] = listening_inventory(procs_cim)

    # ---- DBF ----
    doc["dbf"] = dbf_facts(attribution.get("client_running_by_install", {}))
    armed = bool((doc["dbf"].get("guoxin_iquant_11") or {}).get("armed"))
    doc["dbf"]["state"] = "ok" if armed else "unavailable"

    # ---- 历史累积：R1 / R2 ----
    history = load_history()
    current_surface = _surface_existence(doc)
    if current_surface:
        doc["stability"] = stability_report(history, current_surface)
    else:
        doc["stability"] = {"state": "skipped",
                            "detail": "本 mission 未采方法面，不做 R1 判定",
                            "methods": {}}
    doc["session_boundary"] = session_boundary(history, client_pid)

    # ---- 逐轴三态汇总（R3：没覆盖的轴明确 skipped，不记 unavailable） ----
    doc["axes_state"] = {
        a: (_axis_state(doc, a) if a in axes else "skipped") for a in ALL_AXES
    }

    # ---- 结论 ----
    auth_bits = doc["preconditions"]["log"].get("auth_bits_latest") or {}
    no_pid_check = auth_bits.get("mdl_auth_xtquant_no_pid_check")
    market_ok = doc["market"]["verdict"] == "ok"
    trade_ok = any(
        isinstance(v, dict) and v.get("gettradedetail", {}).get("method_exists")
        for v in (doc["trade"].get("bus_probe") or {}).values()
    )
    doc["verdicts"] = {
        "market_can_use_now": {
            "state": "ok" if market_ok else "unavailable",
            "answer": ("能。已取得 tick 级非空数据" if market_ok else
                       "不能。大 QMT 自身的实时行情当前不可用。"),
            "blocked_at": None if market_ok else (
                "官方入口 58670（XtModel.exe）未运行/未监听；"
                "58600 与 miniquote 58341 均无 getFullTick"),
        },
        "trade_can_use_now": {
            "state": "ok" if trade_ok else "unavailable",
            "answer": ("能。callFormula 总线可用" if trade_ok else
                       "不能。程序化交易通道当前不可用（GUI 手工下单不受此结论约束）。"),
            "blocked_at": None if trade_ok else (
                f"PID 闸授权位 no_pid_check={no_pid_check} + 交易总线端口未绑定 "
                "+ is_automonitor=%s" % (doc["dbf"].get("is_automonitor_current")
                                          or {}).get("value")),
        },
        "can_place_order_directly": {
            "state": "ok" if (market_ok and trade_ok) else "unavailable",
            "answer": ("前置已齐，但仍需当次明确批准才能投单" if (market_ok and trade_ok)
                       else "不能。被证实的真实下单路径仍为 0 条。"),
            "blocked_at": None if (market_ok and trade_ok) else
            "授权位/总线/DBF 开关/行情四项前置未满足；且用户未当次批准",
        },
        "gui_manual_order": {
            "state": doc["preconditions"]["gts_established"].get("state", "skipped"),
            "answer": ("柜台级在线（GTS ESTABLISHED + 登录成功 + refreshAccounts），"
                       "GUI 手工可下单；这是人工通道，不构成程序化接口可用，"
                       "也不等于已验证成交"),
        },
        "real_order_attempt": {
            "state": "skipped",
            "answer": "本轮未投单（含模拟/纸面）；前置未全绿且用户未当次批准",
        },
    }

    # ---- 未满足的前置（失败也必须写） ----
    failed = []
    if not doc["preconditions"]["processes"]["XtItClient.exe"].get("running"):
        failed.append("XtItClient.exe 未运行")
    if doc["preconditions"]["gts_established"].get("state") != "ok":
        failed.append("GTS 未建立（59.36.9.32:56000）")
    if not auth_bits:
        failed.append("当日日志里没有 auth string，授权位未知")
    if no_pid_check != 1:
        failed.append(f"PID 闸授权位 mdl_auth_xtquant_no_pid_check={no_pid_check}（需券商侧开通）")
    if auth_bits.get("mdl_auth_broker_whitelist") != 1:
        failed.append("框架白名单模块 mdl_auth_broker_whitelist=0（未启用）")
    if auth_bits.get("mdl_auth_gt_ipc_pair") != 1:
        failed.append("XtModel 配对 IPC mdl_auth_gt_ipc_pair=0（未授权）")
    if not market_ok:
        failed.append("行情：无 tick 级证据")
    if not trade_ok:
        failed.append("交易：总线不可用/无下单入口")
    if not armed:
        failed.append("DBF：is_automonitor=0，文件单无法产生委托")
    if not doc["dbf"].get("columns_verified"):
        failed.append("DBF 列名与二进制证据不一致")
    doc["failed_preconditions"] = failed

    # ---- 下一步（有证据支持的） ----
    doc["next_steps"] = _next_steps(doc, mission)
    doc["not_viable"] = [
        "直接调 COrderFuncService::orderStock（private + 参数类 0 导出 ctor）",
        "调 TradeHub::placeOrder（出站转发插件，非入站门）",
        "伪造 whitelist.json / 改授权串 / 改日志（伪造凭证）",
        "授权位为 0 时强行起 CIPCManager（绕过授权）",
        "往 IP 白名单方向改（当日 illegal IP 命中 0 次）",
    ]
    doc["not_proven"] = [
        "节假日日历未接入 ⇒ 交易日判定只按 weekday，未做 holiday 校验",
        "UDP / 共享内存 / 命名管道 / 进程内调用形态的入站通道（只枚举 TCP LISTENING）",
        "客户端内嵌 Jupyter kernel 的行情侧 xtdata Python API 是否可用（未执行，需批准）",
        "Formula 引擎 REALTIME 数据的时间粒度（分钟线还是 tick），日志未给周期字段",
    ]
    return doc


def _next_steps(doc: dict, mission: str) -> list[dict]:
    steps: list[dict] = []
    market_ok = doc["market"]["verdict"] == "ok"
    fr = doc["market"].get("formula_realtime") or {}
    if not market_ok and fr.get("realtime_data_complete_count", 0) > 0:
        steps.append({
            "id": "M0b", "track": "market", "state": "ok",
            "action": "停止把「换交易时点 / 排查上游行情源」当行情修复手段",
            "expect": "不再消耗轮次；直接推进导出路径",
            "evidence_basis": (f"Formula 引擎本轮 {fr.get('realtime_data_complete_count')} 条"
                               "实时计算完成 ⇒ 数据在客户端内部是活的"),
        })
        steps.append({
            "id": "M1b", "track": "market", "state": "skipped",
            "action": "（需用户当次明确批准）在客户端内嵌 Jupyter kernel 内只跑 xtdata 只读行情探测",
            "expect": "验证行情侧 Python API 能否绕开 58600 的空结果；判据看 period=tick 且非空",
            "needs_approval": True,
            "caveat": ("会创建 kernel 进程 ⇒ 属客户端状态变更；"
                       "★ 绝不调用 passorder/xttrader 等交易 API"),
        })
    steps.append({
        "id": "M1", "track": "market", "state": "skipped",
        "action": "启动 xtmodel/XtModel.exe 让 58670 起来",
        "expect": "58670 监听 + IPythonApiServer listen finished:true",
        "needs_approval": False,
        "caveat": "启动≠可用：昨日日志显示本地 tdl/sl 为空、wholequote login:false",
    })
    if not doc["dbf"].get("guoxin_iquant_11", {}).get("armed"):
        steps.append({
            "id": "T1", "track": "trade", "state": "skipped",
            "action": "开启 is_automonitor=1，走 DBF 文件单",
            "expect": "file_order_readiness 全绿 → 生成委托文件",
            "needs_approval": True,
            "caveat": "armed 后往监控目录写文件就等于下单；需先有小样本 + 快速撤单预案",
        })
    steps.append({
        "id": "T2", "track": "trade", "state": "skipped",
        "action": "向国信申请柜台侧授权（no_pid_check 或 broker_whitelist）",
        "expect": "授权位变 1 **且** 日志出现 pid connected（两处独立证据）",
        "needs_approval": True,
        "caveat": "业务开通，非技术手段；只看授权位变 1 就说「能下单」是错的",
    })
    steps.append({
        "id": "T3", "track": "trade", "state": "skipped",
        "action": "内置策略（路径 B/B′）部署 + 本地桥，替代外部 RPC",
        "expect": "策略进程内 passorder 可用，后端经本地 TCP 桥下发",
        "needs_approval": True,
        "caveat": "需 GUI 人工部署公式；四道 live 前置未复核",
    })
    return steps


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _out_path(mission: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return ROOT / "output" / f"qmt_iface_check_{stamp}_{mission}.json"


def _write(doc: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _emergency_doc(mission: str, exc: BaseException) -> dict:
    return {
        "schema": SCHEMA, "mission": mission,
        "sampled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "探针异常，仍写出证据",
        "error": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc()[-2000:],
        "failed_preconditions": ["探针自身异常，本轮无有效采样"],
        "verdicts": {
            "market_can_use_now": {"state": "skipped", "answer": "未检查（探针异常）"},
            "trade_can_use_now": {"state": "skipped", "answer": "未检查（探针异常）"},
            "can_place_order_directly": {"state": "skipped",
                                         "answer": "未检查（探针异常）"},
        },
        "not_proven": ["本轮全部结论未取得"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="大 QMT 行情/交易接口巡检引擎")
    ap.add_argument("--mission", choices=sorted(MISSION_AXES), default="market")
    ap.add_argument("--no-rpc", action="store_true",
                    help="只做静态检查（netstat/CIM/日志），不发任何 RPC 帧")
    ap.add_argument("--out", default=None, help="输出路径（默认 output/qmt_iface_check_<stamp>_<mission>.json）")
    ap.add_argument("--print-summary", action="store_true")
    args = ap.parse_args(argv)

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = Path(args.out) if args.out else _out_path(args.mission)
    try:
        doc = build_doc(args.mission, no_rpc=args.no_rpc, started=started)
    except BaseException as exc:  # noqa: BLE001 异常也必须落盘
        doc = _emergency_doc(args.mission, exc)
        _write(doc, path)
        print(f"[FAIL] 探针异常，已写出证据: {path}")
        print(doc["error"])
        return 1

    _write(doc, path)
    print(f"written: {path}")
    print(f"mission: {args.mission}  axes: {doc['axes_covered']}")
    print(f"failed_preconditions: {len(doc['failed_preconditions'])}")
    if args.print_summary:
        for k, v in doc["verdicts"].items():
            print(f"  {k}: [{v.get('state')}] {v.get('answer')}")
        st = doc.get("stability") or {}
        if st.get("methods"):
            for name, rec in st["methods"].items():
                print(f"  stability {name}: {rec['label']} series={rec['series']}")
        print(f"  session_boundary: {json.dumps(doc['session_boundary'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
