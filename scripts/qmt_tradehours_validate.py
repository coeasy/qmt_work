# -*- coding: utf-8 -*-
"""交易时段大 QMT 接口可行性验证（**只读**，供 09:38 定时任务调用）。

为什么要单独一个入口
--------------------
此前"行情通了"的判断散落在 ``output/rea_func_level/`` 的一次性探针里，
而且那些探针有两个会给出**错误结论**的毛病：

* 半帧跨 recv 时丢字节 ⇒ 交易时段大帧会被读成乱码，报"没有推送"；
* ``subscribeQuote`` 的 ACK 被算成行情推送 ⇒ 收盘后也会报"有推送"。

生产传输 ``XtqRpcTransport`` 已修掉这两处；本脚本只做 **编排 + 证据留存**，
不复刻协议逻辑，也不做任何写操作。

三态纪律（贯穿每个检查项）
--------------------------
``ok``          真跑通了，且有内容判据；
``unavailable`` 环境/前置条件缺失（端口没监听、连不上、配置读不到）——
                **不等于"接口不通"**；
``skipped``     本轮没要求做这项检查。
绝不把"没查"写成"查了没事"。

退出码（约定 0=就绪 / 2=需人工动作或未证实 / 1=环境缺失）
--------------------------------------------------------
0  任一通路拿到**确实带行情字段**的数据（推送 proven 或拉取 quote_proven）；
2  环境在、但两条通路都未证实 / 监控未开 / 端口方案未落盘；
1  Python 依赖或安装目录都拿不到，本次验证不成立。

★ 两条行情轴必须分开报，不能互相顶替（2026-11-09 盘前加固）：
  * ``push_proven``  58670 ``subscribeQuote`` 抓到带行情字段的推送帧；
  * ``pull_proven``  58600 ``getMarketData`` / 58670 ``getFullTick`` 拉到带行情字段的结果体。

  旧版把退出码 0 只绑在 ``capture_58670.proven`` 上：即使 58600 拉取型行情
  完全可用，只要 ``XtModel.exe`` 没起就一律判"未证实"——这是**把环境缺失
  当成接口不通**的另一种写法。反过来也不能用"代码表非空"顶数：
  ``getStockListInSector`` 返回 5224 个代码只算 ``code_list_proven``（证券代码表），
  不是行情。证据分级统一由 ``xtq_rpc.classify_payload`` 给出。

用法::

    backend/runtimes/cp311/python.exe scripts/qmt_tradehours_validate.py \
        [--parents P:/stock] [--seconds 60] [--code 600036.SH]

硬约束：只订阅/查询/读配置；不下单、不撤单、不改 broker.ini / Config.xml、
不打开自动监控、不把任何文件放进 export_data、不启动任何客户端进程。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import socket
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))       # cp311 的 _pth 忽略 PYTHONPATH

OUT_DIR = ROOT / "output"


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dumpable(value, limit: int = 2000):
    """把任意应答压成可落盘的 JSON；bytes 只报长度，不泄露二进制内容。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


def _section(name: str, fn) -> dict:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — 单项失败不能拖垮整轮证据
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=3)}


def _ports() -> dict:
    """活体监听盘点。netstat 不可用时如实报 unavailable，不返回空字典。"""
    from connectors.install_registry import _win_listening_ports

    listeners = _win_listening_ports()
    if listeners is None:
        return {"status": "unavailable",
                "note": "netstat 无输出/不可用：本轮没有做过监听检查",
                "ports": {}}
    wanted = (58600, 58610, 58670)
    present = {str(p): sorted(set(listeners.get(p, []))) for p in wanted}
    return {"status": "ok", "ports": present,
            "all_listening_ports": len(listeners),
            "note": "58670 未 LISTENING 时按环境缺失处理，不判定为接口不通"}


def _check_port(port: int, method: str, params: dict, timeout: float = 5.0) -> dict:
    """对单个 IPC 端口打一次真实业务方法（TCP 能连不算就绪）。

    ★ ``status=ok`` 只代表**服务端应答了**，不代表有行情数据。有没有数据另给
      ``quote_proven``，由 ``classify_payload`` 按字段判据给出。旧版只有
      ``status`` 一列，``getFullTick`` 返回空也会和"拉到 tick"长得一样。
    """
    from connectors.transports.xtq_rpc import XtqRpcTransport, classify_payload

    t = XtqRpcTransport(port=port, timeout=timeout)
    try:
        doc = t._call_sync(method, params, timeout)
    except OSError as exc:
        return {"status": "unavailable", "port": port, "method": method,
                "error": str(exc), "quote_proven": False,
                "note": "连不上/对端未监听，不等于接口不存在"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "port": port, "method": method,
                "error": f"{type(exc).__name__}: {exc}", "quote_proven": False}
    params_doc = doc.get("params") if isinstance(doc.get("params"), dict) else {}
    eid = params_doc.get("ErrorID")
    grade = classify_payload(params_doc.get("result")) if "result" in params_doc \
        else classify_payload(None)
    return {
        "status": "ok" if eid is None else "unavailable",
        "port": port, "method": method,
        "status_code": doc.get("status"), "error_id": eid,
        "error_msg": str(params_doc.get("ErrorMsg") or ""),
        "doc_keys": sorted(str(k) for k in doc),
        "payload_kind": grade["kind"], "payload_size": grade["size"],
        "quote_proven": grade["quote_proven"],
        "price_keys": grade["price_keys"], "key_sets": grade["key_sets"],
        "doc": _dumpable(doc),
        "note": "status=ok 只说明服务端应答了；有没有行情数据看 quote_proven",
    }


def _market_probe_58600(code: str) -> dict:
    """58600 的**拉取型**行情/证券列表能力（58670 没起时仍能验的一条路）。

    实测（2026-11-09 盘前）：``getStockListInSector({})`` 在 58600 返回空，
    而 ``{"sectorName": "沪深A股"}`` 返回全量代码表 —— 与 58670 上
    "空参数返回 5224 个、带 sectorName 返回空" **正好相反**。所以不能只试一种
    形状就下"没有数据"的结论：空结果可能是参数没命中，也可能是查询失败。
    """
    from connectors.transports.xtq_rpc import XtqRpcTransport, classify_payload

    t = XtqRpcTransport(port=58600, timeout=2.5)
    # 交易时段才看得出 tick 有没有数据 ⇒ 盘前"全空"不能收窄形状集。
    # meta/region/param 三块形状来自 subscribeQuote 的实测结构，getMarketData
    # 是否同样吃嵌套文档尚无定论，一并扫；每个形状都单独记三态。
    calls = [
        ("getStockListInSector", {}),
        ("getStockListInSector", {"sectorName": "沪深A股"}),
        ("getInstrumentDetail", {"stockCode": code}),
        ("getMarketData", {"stockCode": code, "period": "tick", "count": 1}),
        ("getMarketData", {"stockMarkets": [code], "period": "tick", "count": 1}),
        ("getMarketData", {"stockCode": [code], "period": "tick", "count": 1}),
        ("getMarketData", {"stockCode": code, "period": "tick",
                           "startTime": "", "endTime": "", "count": 1,
                           "dividendType": "none"}),
        ("getMarketData", {"stockCode": code, "period": "1m",
                           "startTime": "", "endTime": "", "count": 3}),
        ("getMarketData", {"stockCode": code, "period": "1d",
                           "startTime": "", "endTime": "", "count": 3}),
        ("getMarketData", {"meta": {"stockCode": code, "period": "tick"},
                           "region": {"startTime": "", "endTime": "", "count": 1},
                           "param": {}}),
        ("getFullTick", {"stockMarkets": [code]}),
        ("getFullTick", {"stockCode": code}),
    ]
    quote_methods = frozenset({"getMarketData", "getFullTick"})
    rows = []
    for method, params in calls:
        # 日线 / 分钟线里带 open/high/low 也算"行情数据"，但**不是实时 tick**。
        # 09:38 要回答的是"实时 tick 通没通"，两种证据不能混成一句话。
        realtime = (method == "getFullTick" or
                    (method == "getMarketData" and
                     str(params.get("period", "")).lower() == "tick"))
        item = {"method": method, "params": params,
                "quote_eligible": method in quote_methods,
                "realtime_eligible": realtime}
        try:
            doc = t._call_sync(method, params, 2.5)
            payload = doc.get("params") if isinstance(doc.get("params"), dict) else {}
            result = payload.get("result")
            grade = classify_payload(result)
            item["status"] = "ok" if payload.get("ErrorID") is None else "unavailable"
            item["error_id"] = payload.get("ErrorID")
            item["result_kind"] = type(result).__name__
            item["result_len"] = len(result) if isinstance(result, (list, dict, str)) else None
            item["payload_kind"] = grade["kind"]
            item["quote_proven"] = bool(item["quote_eligible"] and grade["quote_proven"])
            item["tick_proven"] = bool(realtime and item["quote_proven"])
            item["price_keys"] = grade["price_keys"]
            item["key_sets"] = grade["key_sets"]
            item["sample"] = _dumpable(result, 220) if result else ""
        except Exception as exc:  # noqa: BLE001
            item["status"] = "unavailable"
            item["error"] = f"{type(exc).__name__}: {exc}"
            item["quote_proven"] = False
            item["tick_proven"] = False
        rows.append(item)
    nonempty = [r for r in rows if (r.get("result_len") or 0) > 0]
    quote_shapes = [r for r in rows if r.get("quote_proven")]
    tick_shapes = [r for r in rows if r.get("tick_proven")]
    return {
        "status": "ok" if any(r["status"] == "ok" for r in rows) else "unavailable",
        "port": 58600, "calls": rows,
        "nonempty_shapes": len(nonempty),
        "data_available": bool(nonempty),
        "quote_proven": bool(quote_shapes),
        "quote_shapes": [f"{r['method']} {str(r['params'])[:70]}" for r in quote_shapes],
        "tick_proven": bool(tick_shapes),
        "tick_shapes": [f"{r['method']} {str(r['params'])[:70]}" for r in tick_shapes],
        "code_list_proven": any(r.get("payload_kind") == "code_list" for r in rows),
        "note": ("代码表非空只算 code_list_proven，不算行情；quote_proven 只认 "
                 "带行情字段的结果体；日线/分钟线不计入 tick_proven（不是实时 tick）。"
                 "空结果只说明这一组参数没命中，本脚本不做'没有行情数据'的结论。")}


def _capture(port: int, code: str, seconds: float) -> dict:
    """订阅 + 抓推送。只有 proven=True 才能说"实时行情已证实"。"""
    from connectors.transports.xtq_rpc import XtqRpcTransport

    t = XtqRpcTransport(port=port, timeout=5.0)
    try:
        frames = t.capture(code, seconds=seconds, period="tick")
    except OSError as exc:
        return {"status": "unavailable", "port": port, "code": code,
                "error": str(exc), "proven": False,
                "note": "订阅通道未就绪（端口没起），不能判成行情不通"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "port": port, "code": code,
                "error": f"{type(exc).__name__}: {exc}", "proven": False}
    summary = XtqRpcTransport.summarize_capture(frames)
    # 证据量控制：只留前 8 帧的完整 doc，其余保留分类与键集。
    evidence = []
    for i, f in enumerate(frames[:8]):
        item = {k: v for k, v in f.items() if k != "doc"}
        if "doc" in f:
            item["doc"] = _dumpable(f["doc"], 1200)
        evidence.append(item)
    return {"status": "ok", "port": port, "code": code,
            "seconds": seconds, "summary": summary,
            "proven": bool(summary["proven"]), "frames_sample": evidence,
            "note": "ACK（含 status 的应答帧）不计入行情；未识别帧保留键集供人工核对"}


def _installs(parents: list[str]) -> dict:
    from connectors.install_registry import audit, discover, plan_ports

    if not parents:
        return {"status": "skipped", "note": "没有给安装父目录，跳过安装台账核查"}
    found = discover(parents)
    rep = audit(found, probe=None)      # 活体已在 _ports 里单独记，避免重复跑 netstat
    try:
        plan = plan_ports(found, stride=100)
    except Exception as exc:  # noqa: BLE001
        plan = {"error": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok", "parents": parents, "count": len(found),
            "installs": [i.to_dict() for i in found],
            "declared_conflicts": len(rep["declared_conflicts"]),
            "ok_basis": rep["ok_basis"], "note": rep.get("note", ""),
            "port_plan": plan,
            "plan_applied": False,
            "plan_note": "本脚本不改 broker.ini；落盘需用户明确批准"}


def _file_orders(parents: list[str]) -> dict:
    from connectors.install_registry import discover
    from connectors.qmt_file_order import batch_order_of, find_monitor_settings

    if not parents:
        return {"status": "skipped", "note": "没有安装父目录，跳过文件单通道核查"}
    accounts, batches = [], []
    for inst in discover(parents):
        for name, setting in find_monitor_settings(inst.userdata):
            accounts.append({"install": inst.label, "account": name,
                             "monitor_path": setting.monitor_path,
                             "armed": setting.armed})
        for name, batch in batch_order_of(inst.userdata):
            batches.append({"install": inst.label, "account": name,
                            "monitor_path": batch["monitor_path"],
                            "key_enable": batch["key_enable"]})
    return {"status": "ok", "dbf_accounts": accounts,
            "dbf_armed": sum(1 for a in accounts if a["armed"]),
            "batch_order_accounts": batches,
            "batch_order_enabled": sum(1 for b in batches if b["key_enable"]),
            "note": "DBF 文件单与批量单 CSV 是两条通道；两者都未启用时丢文件不会产生委托"}


def _conclusion(report: dict) -> dict:
    """把证据压成结论。**纯函数**（无 IO），便于回归测试锁住防假绿口径。

    三条最容易写错的话：
    * 58670 没起 ⇒ 推送轴 ``unavailable``，**不是**"行情接口不通"，更不能因此
      把 58600 拉到的行情也判成"未证实"；
    * 代码表 5224 条 ⇒ 只算 ``code_list_proven``，**不是**行情数据；
    * 退出码 0 只给"确实看到行情字段"，不给"服务端应答了 / 端口在监听"。
    """
    ports = report.get("ports") or {}
    listeners = ports.get("ports") or {}
    p58600 = bool(listeners.get("58600"))
    p58670 = bool(listeners.get("58670"))
    capture70 = report.get("capture_58670") or {}
    capture00 = report.get("capture_58600") or {}
    probe00 = report.get("probe_58600") or {}
    fulltick = report.get("probe_58670_fulltick") or {}

    push_proven = bool(capture70.get("proven") or capture00.get("proven"))
    pull_proven = bool(probe00.get("quote_proven") or fulltick.get("quote_proven"))
    # getFullTick 本身就是 tick 级；getMarketData 只有 period=tick 才算。
    pull_tick_proven = bool(probe00.get("tick_proven") or fulltick.get("quote_proven"))
    realtime_tick_proven = bool(push_proven or pull_tick_proven)
    code_list_proven = bool(probe00.get("code_list_proven"))
    env_ok = ports.get("status") == "ok" and (
        p58600 or p58670 or (report.get("installs") or {}).get("status") == "ok")

    axes = {
        "push_proven": push_proven,
        "pull_proven": pull_proven,
        "realtime_tick_proven": realtime_tick_proven,
        "code_list_proven": code_list_proven,
        "channel_58600_listening": p58600,
        "channel_58670_listening": p58670,
        "channel_58670_missing": not p58670,
    }
    if not env_ok:
        return {**axes, "exit_code": 1,
                "verdict": "环境缺失：拿不到监听状态/安装目录，本次验证不成立"}
    if push_proven or pull_proven:
        axis = "推送" if push_proven else "拉取"
        both = "、拉取通路也已证实" if (push_proven and pull_proven) else ""
        tail = ("；实时 tick 推送已证实" if push_proven
                else "；实时 tick 级数据已由拉取通路证实" if pull_tick_proven
                else "；但只证实非 tick 级数据，实时 tick 仍未证实")
        return {**axes, "exit_code": 0,
                "verdict": ("行情接口已证实可用（%s通路拿到带行情字段的数据%s）"
                            % (axis, both)) + tail}

    if not p58670 and not p58600:
        reason = "58600/58670 均未 LISTENING"
    elif not p58670:
        reason = ("58670 未 LISTENING（XtModel 未运行，属环境缺失，不是接口不通）；"
                  "58600 拉取形状未命中行情字段")
    else:
        reason = "58670 在监听但订阅抓帧未出现行情字段；拉取形状亦未命中"
    if code_list_proven:
        reason += "；证券代码表已取到（不算行情数据）"
    return {**axes, "exit_code": 2,
            "verdict": "实时行情未证实：" + reason + "。需人工动作或继续参数形状扫描"}


def main() -> int:
    ap = argparse.ArgumentParser(description="交易时段大 QMT 接口只读验证")
    ap.add_argument("--parents", default=os.environ.get("QMT_INSTALL_PARENTS", "P:/stock"),
                    help="安装父目录，逗号分隔；可用环境变量 QMT_INSTALL_PARENTS 覆盖")
    ap.add_argument("--seconds", type=float, default=60.0, help="订阅抓帧时长")
    ap.add_argument("--code", default="600036.SH")
    args = ap.parse_args()

    parents = [p.strip() for p in str(args.parents).replace(";", ",").split(",")
               if p.strip() and Path(p.strip()).is_dir()]
    parent_note = None
    if not parents:
        parent_note = ("没有可用的安装父目录（--parents / QMT_INSTALL_PARENTS）："
                       "安装台账与文件单通道记为 skipped，不作为'没有安装'的结论")

    report: dict = {
        "timestamp": _now(),
        "tool": "scripts/qmt_tradehours_validate.py",
        "scope": "read_only",
        "constraints": ["不下单", "不撤单", "不改 broker.ini/Config.xml",
                        "不打开自动监控", "不交付文件单", "不启动客户端进程"],
        "params": {"code": args.code, "seconds": args.seconds,
                   "parents": parents, "parents_source": "cli/env, verified is_dir"},
    }

    report["ports"] = _section("ports", _ports)
    listeners = report["ports"].get("ports", {})
    p58600 = bool(listeners.get("58600"))
    p58670 = bool(listeners.get("58670"))

    report["probe_58600"] = _section(
        "probe_58600",
        lambda: (_market_probe_58600(args.code)
                 if p58600 else {"status": "unavailable",
                                 "note": "58600 未 LISTENING，跳过业务调用"}))
    report["probe_58670_tag"] = _section(
        "probe_58670_tag",
        lambda: (_check_port(58670, "getTag", {})
                 if p58670 else {"status": "unavailable", "quote_proven": False,
                                 "note": "58670 未 LISTENING，跳过 getTag 判活"}))
    report["probe_58670_fulltick"] = _section(
        "probe_58670_fulltick",
        lambda: (_check_port(58670, "getFullTick", {"stockMarkets": [args.code]})
                 if p58670 else {"status": "unavailable", "quote_proven": False,
                                 "note": "58670 未 LISTENING，跳过 getFullTick"}))
    report["capture_58670"] = _section(
        "capture_58670",
        lambda: (_capture(58670, args.code, args.seconds)
                 if p58670 else {"status": "unavailable", "proven": False,
                                 "note": "58670 未 LISTENING：XtModel.exe 未运行。"
                                         "这是环境缺失，不是'行情接口不通'；"
                                         "本次脚本不会自动启动任何客户端进程"}))
    report["capture_58600"] = _section(
        "capture_58600",
        lambda: (_capture(58600, args.code, min(6.0, args.seconds))
                 if p58600 else {"status": "unavailable", "proven": False,
                                 "note": "58600 未 LISTENING，跳过推送能力探测"}))
    report["installs"] = _section("installs", lambda: _installs(parents))
    report["file_orders"] = _section("file_orders", lambda: _file_orders(parents))
    if parent_note:
        report["parents_note"] = parent_note

    conclusion = _conclusion(report)
    report["axes"] = {k: conclusion[k] for k in (
        "push_proven", "pull_proven", "realtime_tick_proven", "code_list_proven",
        "channel_58600_listening", "channel_58670_listening",
        "channel_58670_missing")}
    report["verdict"] = conclusion["verdict"]
    exit_code = conclusion["exit_code"]
    verdict = conclusion["verdict"]
    report["exit_code"] = exit_code
    report["exit_code_meaning"] = {0: "就绪且已证实", 1: "环境缺失",
                                   2: "需人工动作或未证实"}[exit_code]
    report["pending_human_actions"] = [
        ("（推送轴未证实）" if not conclusion["push_proven"] else "（推送轴已证实，仍需守住）")
        + "交易时段保持 58670/XtModel 可用并复跑订阅抓取（本轮结论见 capture_58670）",
        "端口方案落盘 broker.ini 需用户明确批准（本脚本从不写配置）",
        "在客户端界面打开 is_automonitor 并导出下单模板 DBF",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / ("qmt_tradehours_validate_%s.json" %
                     _dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[%s] %s" % (_now(), verdict))
    print("  ports: " + _dumpable(report["ports"].get("ports", {}), 300))
    print("  axes:  " + _dumpable(report["axes"], 300))
    for key in ("probe_58600", "probe_58670_tag", "probe_58670_fulltick",
                "capture_58670", "capture_58600", "installs", "file_orders"):
        sec = report.get(key, {})
        extra = ""
        if key in ("capture_58670", "capture_58600"):
            extra = " | " + str(sec.get("summary", {}).get("verdict", sec.get("note", "")))
        elif key == "probe_58600":
            extra = (" | 非空形状 %s | 行情形状 %s | tick 形状 %s | 代码表 %s"
                     % (sec.get("nonempty_shapes", "?"),
                        len(sec.get("quote_shapes") or []),
                        len(sec.get("tick_shapes") or []),
                        sec.get("code_list_proven", "?")))
        elif key == "probe_58670_fulltick":
            extra = " | quote_proven=%s" % sec.get("quote_proven", "?")
        print("  %-24s %s%s" % (key, sec.get("status", "?"), extra))
    print("  evidence: %s" % out)
    print("  exit_code: %d (%s)" % (exit_code, report["exit_code_meaning"]))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
