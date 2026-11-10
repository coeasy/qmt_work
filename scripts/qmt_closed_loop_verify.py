#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""闭环策略的「运行前体检 + 运行后核验」（新规形态，2026-10-31）。

为什么不能复用 ``qmt_agent_verify.py``
--------------------------------------
那个脚本判的是**文件桥**的产物：``agent_status.json`` / ``probe_result.json`` /
``events.ndjson``。新规（2026-10-23 起）策略进程禁文件读写，闭环模式下这三份
文件**一个都不会生成** —— 拿旧脚本去体检，结论永远是「策略从未成功启动过」，
而实际上它可能正在正常下单。**用错的判据比没有判据更危险。**

闭环模式的唯一证据链是客户端日志里的 ``@@QMTW|`` 行：

    BOOT     本次启动的计划/账户/净值/持仓数/注入面（**判活的第一条**）
    STATUS   周期心跳（uptime / nav / pending / orders_sent / halted）
    DAY      跨日复位
    ORDER    已发出的一单（带 cid）
    REJECT   下单被拒（含原因）
    TRADE    成交回报
    LOST     柜台查不到该 cid ⇒ 允许重新决策
    HALT     风控停机（回撤/在途久未成交）
    READONLY 只读（计划非法/过期/柜台不可用）
    RECOVER  只读解除
    STOP     进程退出

退出码（★ 必须如实，否则 ``if check; then echo 就绪; fi`` 会误判）
------------------------------------------------------------------
0  就绪（有新鲜 BOOT + 新鲜 STATUS，无 READONLY/HALT/REJECT 未决）
2  需要人工动作（未运行 / 心跳过期 / 只读 / 停机 / 有拒单）
1  环境缺失（bundle 不存在、日志目录找不到 —— 这类不是策略的问题）

用法::

    python scripts/qmt_closed_loop_verify.py --bundle dist/qmt_work_agent.py
    python scripts/qmt_closed_loop_verify.py --bundle X.py --qmt-dir P:/stock/gd_qmt
    python scripts/qmt_closed_loop_verify.py --bundle X.py --log <XtClient_20261031.log>
    python scripts/qmt_closed_loop_verify.py --bundle X.py --json
"""
from __future__ import print_function

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_BACKEND = os.path.join(_ROOT, "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

PREFIX = "@@QMTW|"

#: 心跳过期阈值（秒）。闭环 tick 默认 3s、STATUS 默认 10s ⇒ 90s 是很宽的界。
DEFAULT_MAX_AGE_S = 90.0

#: 判定为「需要人工看一眼」的记录类型。
_ATTENTION = ("READONLY", "HALT", "REJECT")

EXIT_OK = 0
EXIT_ENV = 1
EXIT_ACTION = 2


# ---------------------------------------------------------------------------
# 1. bundle 侧：是不是闭环产物、编码能不能跑
# ---------------------------------------------------------------------------
def inspect_bundle(path):
    """→ dict(ok, encoding_ok, closed_loop, plan_id, problems)。

    编码判定直接复用 ``qmt_agent_verify.bundle_health`` —— 那里已经踩过
    「gbk+LF 必然 SyntaxError」的坑（报错位置指向 docstring 里的中文，
    极易误判成「某个中文字写错了」）。判据只能有一份。
    """
    out = {"path": path, "exists": os.path.isfile(path), "ok": False,
           "encoding_ok": None, "syntax_ok": None, "closed_loop": False,
           "plan_id": "", "problems": []}
    if not out["exists"]:
        out["problems"].append("bundle 文件不存在: %s" % path)
        return out
    try:
        import qmt_agent_verify as verify
        health = verify.bundle_health(path)
        out["encoding_ok"] = health.get("encoding_ok")
        out["syntax_ok"] = health.get("syntax_ok")
        out["ok"] = bool(health.get("ok"))
        if not health.get("ok"):
            note = health.get("encoding_note") or health.get("syntax_note") or ""
            out["problems"].append("bundle 体检未通过: %s" % (note or health))
    except Exception as exc:                     # noqa: BLE001
        out["problems"].append("无法体检 bundle（qmt_agent_verify 不可用）: %s" % exc)

    text = _read_bundle_text(path)
    # ★ 判据用「赋值右侧不是 None」，不能用「文件里出现 EMBEDDED_PLAN 字样」——
    #   注释里也写着这个名字，那样每份旧 bundle 都会被判成闭环产物。
    out["closed_loop"] = _has_embedded_plan(text)
    out["plan_id"] = _embedded_plan_id(text)
    return out


def _read_bundle_text(path, limit=2 * 1024 * 1024):
    with open(path, "rb") as fh:
        raw = fh.read(limit)
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _has_embedded_plan(text):
    """bundle 是否真的烘焙了计划（而不是保留 ``EMBEDDED_PLAN = None`` 占位）。"""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("EMBEDDED_PLAN"):
            continue
        if "=" not in stripped:
            continue
        rhs = stripped.split("=", 1)[1].strip()
        if rhs and rhs != "None":
            return True
    return False


def _embedded_plan_id(text):
    """从 bundle 里捞出 plan id（只做**展示**用，判据不依赖它）。

    ★ 不能用「第一行 json.loads」：``pprint.pformat`` 会把长 dict 折成多行，
      首行是 ``EMBEDDED_PLAN = {'id': 'p-test-01',`` —— 半个对象，解析必失败。
      这里按括号配平累积到完整字面量，再用 ``ast.literal_eval``（**不是** json：
      源码里是 Python 字面量，``True``/``None`` 都不是 JSON）。
    """
    import ast

    marker = "EMBEDDED_PLAN = "
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(marker):
            continue
        chunk = [stripped[len(marker):]]
        depth = _bracket_delta(chunk[0])
        cursor = index + 1
        while depth > 0 and cursor < len(lines):
            nxt = lines[cursor].strip()
            chunk.append(nxt)
            depth += _bracket_delta(nxt)
            cursor += 1
        literal = "\n".join(chunk)
        if literal.strip() == "None":
            return ""
        try:
            payload = ast.literal_eval(literal)
        except (ValueError, SyntaxError):
            return ""
        if isinstance(payload, dict):
            return str(payload.get("id") or "")
    return ""


def _bracket_delta(text):
    return (text.count("{") + text.count("[") + text.count("(")
            - text.count("}") - text.count("]") - text.count(")"))


# ---------------------------------------------------------------------------
# 2. 日志侧：把 @@QMTW| 行捞出来
# ---------------------------------------------------------------------------
def newest_log(qmt_dir):
    """XtClient 主日志（**必须排除** ``XtClient_datasource_*`` 这类副日志）。

    ★ 副日志里没有策略输出。按 mtime 取「最新」会取到副日志，然后把
      「策略在跑」误报成「一行记录都没有」—— 这类假阴性比报错更难查。
    """
    log_dir = os.path.join(qmt_dir, "userdata", "log")
    if not os.path.isdir(log_dir):
        return ""
    best, best_mtime = "", -1.0
    for name in os.listdir(log_dir):
        low = name.lower()
        if not low.startswith("xtclient"):
            continue
        if "_datasource" in low or not low.endswith(".log"):
            continue
        path = os.path.join(log_dir, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def collect_records(log_path):
    """读日志 → ``AgentRecord`` 列表（按行解码，utf-8 失败退 gbk）。

    ★ 不整文件 ``decode()``：XtClient 日志里混着 GBK 与 UTF-8 两段内容
      （客户端自己写 GBK、策略 stdout 走 UTF-8 的情况实测存在），
      整文件解码会在第一处混码就炸掉整份日志。
    """
    from connectors.bigqmt_log_tail import LogTail

    tail = LogTail()
    records = []
    try:
        with open(log_path, "rb") as fh:
            for raw in fh:
                if PREFIX.encode("ascii") not in raw:
                    tail.stats.ignored += 1
                    continue
                line = _decode_line(raw)
                record = tail.feed_line(line)
                if record is not None:
                    records.append(record)
    except OSError as exc:
        return [], {"error": str(exc)}
    return records, tail.stats.as_dict()


def _decode_line(raw):
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 3. 判定
# ---------------------------------------------------------------------------
def evaluate(bundle, records, stats, now=None, max_age_s=DEFAULT_MAX_AGE_S):
    """→ (exit_code, lines, details)。

    ★ 判据顺序有意为之：先「有没有产物」，再「活不活」，最后才是「有没有事」。
      反过来会把「策略根本没跑」说成「策略运行正常但有个历史拒单」。
    """
    now = time.time() if now is None else float(now)
    details = {
        "bundle_ok": bool(bundle.get("ok")),
        "closed_loop": bool(bundle.get("closed_loop")),
        "plan_id": bundle.get("plan_id", ""),
        "records": len(records),
        "boot_id": "",
        "alive": False,
        "standalone": False,
        "last_age_s": None,
        "readonly": "",
        "halted": "",
        "orders": 0,
        "rejects": 0,
        "settled": 0,
        "done": False,
        "window_closed": False,
        "in_window_hint": False,
        "kinds": [],
        "problems": list(bundle.get("problems") or []),
        "stats": stats or {},
    }
    lines = []

    if not bundle.get("exists"):
        return EXIT_ENV, ["[FAIL] bundle 不存在，无法核验: %s" % bundle.get("path")], details
    if not bundle.get("ok"):
        return EXIT_ENV, ["[FAIL] bundle 体检未通过，先修产物再谈运行"] + \
            ["  - %s" % p for p in details["problems"]], details
    if not bundle.get("closed_loop"):
        lines.append("[FAIL] 这份 bundle **不是**闭环产物（EMBEDDED_PLAN 仍是 None）")
        lines.append("       → 它跑的是旧文件桥模式；请用 gen_qmt_agent_bundle.py --plan 重新生成")
        return EXIT_ACTION, lines, details

    if not records:
        lines.append("[FAIL] 日志里一条 @@QMTW| 记录都没有 ⇒ 策略没有以闭环形态运行过")
        lines.append("       下一步：① 确认策略已在「模型交易」里注册并运行（拷贝文件不算注册）；")
        lines.append("               ② 确认日志路径正确（本工具默认取 userdata/log/XtClient_*.log）；")
        lines.append("               ③ 闭环需要**公式模式**挂载，独立进程模式拿不到柜台接口。")
        return EXIT_ACTION, lines, details

    boots = [r for r in records if r.kind == "BOOT"]
    if not boots:
        lines.append("[FAIL] 有 @@QMTW| 行但没有 BOOT —— 说明启动阶段就没走完")
        lines.append("       已见类型: %s" % ", ".join(sorted(set(r.kind for r in records))))
        return EXIT_ACTION, lines, details

    boot = boots[-1]
    details["boot_id"] = boot.boot_id
    boot_data = boot.data or {}
    details["plan_id"] = boot_data.get("plan_id") or details["plan_id"]

    lines.append("[OK] BOOT: plan=%s bundle=%s boot_id=%s"
                 % (details["plan_id"] or "?",
                    boot_data.get("bundle") or "?",
                    boot.boot_id or "?"))
    lines.append("     账户=%s 窗口=%s 目标=%d 净值=%s 持仓=%s trading=%s"
                 % (boot_data.get("account") or "?",
                    "-".join(boot_data.get("window") or []) or "?",
                    int(boot_data.get("targets") or 0),
                    boot_data.get("nav"),
                    boot_data.get("positions"),
                    boot_data.get("trading_enabled")))

    # ★ 运行形态必须单独判：独立进程下终端**不会**注入任何交易函数，
    #   此时策略只能只读。这是闭环「看起来在跑、一单不发」的第一号成因，
    #   而且它跟「计划写错了」的表现一模一样 —— 必须能分开。
    standalone = bool(boot_data.get("standalone"))
    details["standalone"] = standalone
    code = EXIT_OK
    if standalone:
        code = EXIT_ACTION
        lines.append("[FAIL] 运行形态是**独立进程**（standalone=true）—— "
                     "终端不会注入任何交易函数")
        lines.append("       → 闭环必须用**公式模式（内置Python）**挂载。改法：")
        lines.append("         在 QMT 策略编辑器里把类型改成「内置Python」并重新编译，")
        lines.append("         然后重启策略（不要选「原生Python/独立进程」）。")
    if boot_data.get("default_account_type") and \
            str(boot_data.get("default_account_type")).lower() != "stock":
        lines.append("     标的类型=%s（柜台查询类型=%s）"
                     % (boot_data.get("default_account_type"),
                        boot_data.get("query_account_type") or "STOCK"))

    last = records[-1]
    age = max(0.0, now - float(last.ts or now))
    details["last_age_s"] = round(age, 1)
    if age <= max_age_s:
        details["alive"] = True
        lines.append("[OK] 心跳新鲜: 最后一条 %s 距今 %.1fs（阈值 %.0fs）"
                     % (last.kind, age, max_age_s))
    else:
        lines.append("[FAIL] 心跳过期: 最后一条 %s 距今 %.0fs（阈值 %.0fs）"
                     % (last.kind, age, max_age_s))
        lines.append("       → 策略可能已停止/被停模；注意网络类违规的处置是**停模**，"
                     "不是报错，日志里不会有异常堆栈。")

    # 只看**本次启动**（同一 boot_id）的状态，避免上一次会话的只读/停机污染当前判定。
    # ★ 判据用 boot_id 而不是时间戳或 seq：READONLY 常出现在 BOOT **之前**
    #   （实测 s=1 READONLY → s=2 BOOT），用 seq/ts 卡边界会把本次的只读漏掉。
    since_boot = [r for r in records if r.boot_id == boot.boot_id]
    for record in since_boot:
        if record.kind == "READONLY":
            details["readonly"] = str((record.data or {}).get("reason") or "")
        elif record.kind == "RECOVER":
            details["readonly"] = ""
        elif record.kind == "HALT":
            details["halted"] = str((record.data or {}).get("reason") or "")
    details["orders"] = sum(1 for r in since_boot if r.kind == "ORDER")
    details["rejects"] = sum(1 for r in since_boot if r.kind == "REJECT")
    details["settled"] = sum(1 for r in since_boot if r.kind == "SETTLED")
    details["done"] = any(r.kind == "DONE" for r in since_boot)
    details["window_closed"] = any(r.kind == "WINDOW_CLOSED" for r in since_boot)
    details["kinds"] = sorted(set(r.kind for r in since_boot))
    # 「窗内但一单未发」是最容易被误读成「策略没在跑」的形态，单独标出来。
    details["in_window_hint"] = bool(
        last.kind == "STATUS"
        and (last.data or {}).get("in_window")
        and int((last.data or {}).get("orders_sent") or 0) == 0)

    lines.append("     本次会话: ORDER=%d REJECT=%d SETTLED=%d 已到位=%s 窗口已收=%s"
                 % (details["orders"], details["rejects"], details["settled"],
                    "是" if details["done"] else "否",
                    "是" if details["window_closed"] else "否"))
    if last.kind == "STATUS":
        lines.append("     在途: %s 笔 %s｜已发 %s 笔｜换手 %s｜窗内=%s"
                     % ((last.data or {}).get("pending", "?"),
                        (last.data or {}).get("pending_cids") or [],
                        (last.data or {}).get("orders_sent", "?"),
                        (last.data or {}).get("turnover", "?"),
                        (last.data or {}).get("in_window", "?")))

    if details["readonly"]:
        code = EXIT_ACTION
        lines.append("[FAIL] 只读: %s" % details["readonly"])
    if details["halted"]:
        code = EXIT_ACTION
        lines.append("[FAIL] 已停机: %s" % details["halted"])
    if details["rejects"]:
        code = EXIT_ACTION
        rejects = [r for r in since_boot if r.kind == "REJECT"]
        lines.append("[FAIL] %d 笔下单被拒（最后一笔: %s）"
                     % (details["rejects"],
                        (rejects[-1].data or {}).get("reason") or "?"))
        lines.append("       → 常见原因：账户类型不匹配（ETF 计划要用 default_account_type=etf，"
                     "否则 5xxxxx.SH 会被 A 股正则拒掉）")
    if not details["alive"]:
        code = EXIT_ACTION
    if code == EXIT_OK:
        lines.append("[OK] 闭环就绪：有计划、有心跳、无只读、无停机、无拒单")
        if details["orders"] == 0 and details["in_window_hint"]:
            lines.append("     [i] 窗口内但一单未发：检查 guard.max_turnover 是否小于"
                         "目标权重合计（从空仓建满仓本身就是 100% 换手）")
    return code, lines, details


# ---------------------------------------------------------------------------
# 4. CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="闭环策略体检（新规形态）")
    ap.add_argument("--bundle", required=True, help="部署用的 bundle 路径")
    ap.add_argument("--qmt-dir", default=os.environ.get("QMT_DIR", ""),
                    help="QMT 安装根目录（用它自动找 userdata/log 下最新主日志）")
    ap.add_argument("--log", default="", help="直接指定日志文件（优先于 --qmt-dir）")
    ap.add_argument("--max-age-s", type=float, default=DEFAULT_MAX_AGE_S,
                    help="心跳过期阈值（秒），默认 %.0f" % DEFAULT_MAX_AGE_S)
    ap.add_argument("--json", action="store_true", help="输出 JSON（给自动化消费）")
    args = ap.parse_args(argv)

    bundle = inspect_bundle(os.path.abspath(args.bundle))

    log_path = args.log
    if not log_path and args.qmt_dir:
        log_path = newest_log(args.qmt_dir)
    if not log_path:
        lines = ["[FAIL] 找不到日志文件：请用 --log 指定，或用 --qmt-dir 指向 QMT 根目录"]
        if args.json:
            print(json.dumps({"exit": EXIT_ENV, "lines": lines,
                              "details": {"bundle": bundle}}, ensure_ascii=False))
        else:
            print("\n".join(lines))
        return EXIT_ENV

    records, stats = collect_records(log_path)
    code, lines, details = evaluate(bundle, records, stats,
                                    max_age_s=args.max_age_s)
    details["log"] = log_path
    if args.json:
        print(json.dumps({"exit": code, "lines": lines, "details": details},
                         ensure_ascii=False, indent=2))
    else:
        print("闭环体检: bundle=%s" % os.path.basename(args.bundle))
        print("日志: %s" % log_path)
        print("-" * 70)
        for line in lines:
            print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
