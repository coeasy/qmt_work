#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下单 → 快速撤单 冒烟（交易链路验证，带强制撤单兜底）。

用途
----
验证「信号路由 → 风控 → 执行器 → broker/桥 → 账本或柜台」这条链路真的能
下单并撤回，而不是只看 health 绿灯。**默认走 paper 账本**（零资金风险）；
只有显式 ``--live`` 且四道前置全部满足时才碰真实柜台。

为什么必须先 paper 再 live
--------------------------
2026-11-10 现场实测：文件桥 broker 已连接，但 QMT 内 agent 以
``standalone_process`` 运行（``injected=[]``），拿不到终端注入的 ``passorder``；
且 ``XtModel`` 的行情服务器返回 ``account is expired``、``gd_qmt`` 账户
22453951 的 balance/available/marketvalue 全为 0。这种状态下 live 下单必然失败，
paper 仍可完整验证平台侧链路。**不得用 paper 的成功冒充 live 的成功。**

退出码
------
0  下单成功；**且**撤单已确认到终态 cancelled。
   ★ paper 下若委托已成交、或撤单回 503「模拟盘…券商柜台不存在」（刻意契约），
   也归 0，但 verdict / ``cancel_applicable=false`` 会明写「真实撤单未被验证」——
   不得据此宣称「撤单链路已验证」。
1  环境/前置不满足（服务未起、模式不符、live 前置不全）——**没有发单**
2  下单成功但撤单未确认（仅 live；★ 需人工立即处理，可能留有挂单）
3  下单请求本身失败（未产生委托）
4  参数/用法错误

硬约束
------
* ``--live`` 必须同时满足：agent ``injected`` 含 passorder、``trading_enabled``
  为真、``/signal/mode`` 已是 live、账户快照为 live。任一不满足直接退出 1，
  **不尝试**、不降级、不猜测。
* 单次运行最多发 1 笔委托；撤单失败最多重试 3 次，仍失败即退出 2 并打印醒目告警。
* 默认限价买单价**低于卖一价**（不主动追价），避免测试单成交。
* 不撤单成功就绝不退出 0；「下单成功」不等于「验证成功」。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "output")
BASE = "http://127.0.0.1:21118"
API = BASE + "/api/v1"

CANCEL_RETRIES = 3
POLL_TIMEOUT_S = 12.0


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _http(method: str, path: str, body: dict | None = None,
          timeout: float = 20.0) -> tuple[int, dict]:
    """返回 (http_status, 解析后的 JSON)。非 JSON 响应也如实返回。"""
    url = path if path.startswith("http") else API + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return 0, {"code": -1, "message": f"{type(exc).__name__}: {exc}"}
    try:
        return status, json.loads(raw)
    except Exception:  # noqa: BLE001
        return status, {"code": -1, "message": raw[:400]}


def _ok_body(doc: dict) -> dict:
    """信封 {code,message,data} → data（失败返回空 dict）。"""
    if isinstance(doc, dict) and doc.get("code") == 0 and isinstance(doc.get("data"), (dict, list)):
        return doc["data"] if isinstance(doc["data"], dict) else {"items": doc["data"]}
    return {}


def _msg(doc: dict) -> str:
    return str((doc or {}).get("message") or "")


def _find_order(order_id: str) -> dict:
    _, doc = _http("GET", "/trade/orders")
    for item in (_ok_body(doc).get("items") or []):
        if str(item.get("order_id")) == str(order_id):
            return item
    return {}


def _live_preconditions(conn_id: str) -> tuple[bool, list[str]]:
    """live 四道前置；返回 (是否全绿, 未满足项说明)。"""
    gaps: list[str] = []
    _, h = _http("GET", "/health")
    brokers = (_ok_body(h) or {}).get("brokers") or []
    hit = [b for b in brokers if str(b.get("conn_id")) == conn_id]
    if not hit or not hit[0].get("connected"):
        gaps.append(f"broker {conn_id} 未连接")

    _, m = _http("GET", "/signal/mode")
    mode = str((_ok_body(m) or {}).get("mode") or "")
    if mode != "live":
        gaps.append(f"/signal/mode={mode!r}，需先切 live")

    status_path = os.path.join(
        os.path.expanduser("~"), "qmt_work", "bigqmt_bridge", "agent_status.json")
    if not os.path.exists(status_path):
        gaps.append("agent_status.json 不存在（QMT 内策略未运行）")
    else:
        try:
            st = json.load(open(status_path, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"agent_status.json 读不出：{exc}")
        else:
            if not st.get("alive"):
                gaps.append("agent alive=false")
            if not st.get("trading_enabled"):
                gaps.append("agent trading_enabled=false")
            injected = st.get("injected") or []
            if "passorder" not in injected:
                gaps.append(
                    f"agent injected 缺 passorder（runtime_mode={st.get('runtime_mode')}）"
                    "⇒ 策略以独立进程运行，需在 QMT 内切公式模式")
    return (not gaps), gaps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下单→快速撤单冒烟")
    ap.add_argument("--code", default="513090", help="标的（默认 513090 ETF）")
    ap.add_argument("--volume", type=int, default=100, help="数量（默认 100 股）")
    ap.add_argument("--price", type=float, default=1.500,
                    help="限价（默认 1.500，故意低于市价，避免成交）")
    ap.add_argument("--price-type", default="limit")
    ap.add_argument("--conn-id", default="90c700f265d6", help="broker conn_id")
    ap.add_argument("--live", action="store_true",
                    help="真实柜台（需四道前置全绿，否则拒绝执行）")
    ap.add_argument("--json-out", default="", help="证据 JSON 路径")
    args = ap.parse_args(argv)

    report: dict = {
        "ts": _now(), "mode": "live" if args.live else "paper",
        "code": args.code, "volume": args.volume, "price": args.price,
        "conn_id": args.conn_id, "steps": [], "exit_code": None,
    }

    def step(name: str, ok: bool | None, detail: object) -> None:
        report["steps"].append({"step": name, "ok": ok, "detail": detail,
                                "at": _now()})

    # ---- 1. 服务可达 ----
    status, h = _http("GET", "/health", timeout=8)
    if status != 200 or (h or {}).get("code") != 0:
        step("health", False, {"http": status, "message": _msg(h)})
        report["exit_code"] = 1
        return _finish(report, args.json_out, "服务未就绪（health 非 200）")
    step("health", True, {"version": (_ok_body(h) or {}).get("version")})

    # ---- 2. live 前置（paper 不看，但仍记录 broker 状态） ----
    if args.live:
        ok, gaps = _live_preconditions(args.conn_id)
        step("live_preconditions", ok, gaps or "全绿")
        if not ok:
            report["exit_code"] = 1
            return _finish(report, args.json_out,
                           "live 前置未满足，已拒绝发单：" + "；".join(gaps))

    # ---- 3. 预检 ----
    status, pre = _http("POST", "/trade/precheck", {
        "code": args.code, "direction": "buy", "volume": args.volume,
        "price": args.price, "price_type": args.price_type,
        "conn_id": args.conn_id,
    })
    pdata = _ok_body(pre)
    allowed = bool(pdata.get("allowed"))
    step("precheck", allowed, pdata or {"http": status, "message": _msg(pre)})
    if not allowed:
        report["exit_code"] = 3
        return _finish(report, args.json_out, "预检不允许，未发单")

    # ---- 4. 下单 ----
    remark = "order-smoke-%s-%s" % (report["mode"], dt.datetime.now().strftime("%H%M%S"))
    status, od = _http("POST", "/trade/order", {
        "code": args.code, "direction": "buy", "volume": args.volume,
        "price": args.price, "price_type": args.price_type,
        "conn_id": args.conn_id, "remark": remark,
    })
    odata = _ok_body(od)
    order_id = str(odata.get("order_id") or odata.get("id") or "")
    step("place", bool(order_id), odata or {"http": status, "message": _msg(od)})
    if not order_id:
        # pending_confirmation（大额 TOTP）也算「未发单」，如实区分
        if odata.get("pending_confirmation"):
            report["exit_code"] = 1
            return _finish(report, args.json_out,
                           "下单进入 TOTP 待确认（未发单）；本冒烟不做二次确认")
        report["exit_code"] = 3
        return _finish(report, args.json_out, "下单失败：" + _msg(od))
    report["order_id"] = order_id
    report["remark"] = remark

    # ---- 5. 等委托可见 ----
    deadline = time.time() + POLL_TIMEOUT_S
    seen: dict = {}
    while time.time() < deadline:
        seen = _find_order(order_id)
        if seen:
            break
        time.sleep(0.4)
    step("order_visible", bool(seen), seen or "超时未见委托（可能已被柜台拒绝）")
    report["order_before_cancel"] = seen

    # ---- 5.5 paper 下已成交 ⇒ 撤单不适用 ----
    # ★ 2026-11-10 实测：paper 引擎按限价**立即全额成交**（PAPER-35 filled=100），
    #   此时去撤单会拿到 503「该委托号属于模拟盘…券商柜台不存在这笔委托」——
    #   这是**刻意契约**（撤单只对券商实盘有效），不是缺陷，更不能报成
    #   「下单成功但撤单失败、可能留有挂单」。paper 下不存在挂单风险。
    before_status = str((seen or {}).get("status") or "").lower()
    if not args.live and before_status in ("filled", "已成交", "全部成交"):
        step("cancel", None,
             "paper 委托已成交（%s），撤单不适用：模拟盘无柜台挂单，503 属刻意契约"
             % before_status)
        report["cancel_applicable"] = False
        report["exit_code"] = 0
        return _finish(report, args.json_out,
                       "paper 下单成功且已成交；撤单在模拟盘不适用（未验证真实撤单）")

    # ---- 6. 撤单（最多重试 3 次） ----
    cancel_attempts = []
    cancelled = False
    for i in range(CANCEL_RETRIES):
        status, cd = _http("POST", "/trade/cancel",
                           {"order_id": order_id, "conn_id": args.conn_id})
        cdata = _ok_body(cd)
        cancel_attempts.append({"attempt": i + 1, "http": status,
                                "message": _msg(cd), "data": cdata})
        if status == 200 and (cd or {}).get("code") == 0:
            # 撤单受理 ≠ 已撤；必须回读终态
            for _ in range(12):
                cur = _find_order(order_id)
                st = str(cur.get("status") or "").lower()
                if st in ("cancelled", "canceled", "已撤", "已撤单"):
                    cancelled = True
                    seen = cur
                    break
                time.sleep(0.4)
            if cancelled:
                break
        time.sleep(0.6)
    step("cancel", cancelled, {"attempts": cancel_attempts,
                               "final_status": (seen or {}).get("status")})
    report["cancel_attempts"] = cancel_attempts
    report["order_after_cancel"] = seen

    # paper 未成交时的撤单同样不适用（503 明说「模拟盘…券商柜台不存在」）：
    # 不把它算成失败，但必须写清「真实撤单未被验证」，绝不冒充通过。
    if not args.live and not cancelled:
        paper_only = all(
            (a.get("http") == 503) and ("模拟盘" in str(a.get("message") or ""))
            for a in cancel_attempts)
        if paper_only:
            step("cancel", None,
                 "paper 撤单不适用：模拟盘委托不在券商柜台，真实撤单链路未被验证")
            report["cancel_applicable"] = False
            report["exit_code"] = 0
            return _finish(report, args.json_out,
                           "paper 下单成功；撤单在模拟盘不适用（真实撤单未验证）")

    if cancelled:
        report["exit_code"] = 0
        report["cancel_applicable"] = True
        return _finish(report, args.json_out, "下单并成功撤单")
    report["exit_code"] = 2
    return _finish(report, args.json_out,
                   "★ 下单成功但撤单未确认，可能留有挂单，需人工立即核对")


def _finish(report: dict, json_out: str, verdict: str) -> int:
    report["verdict"] = verdict
    # ★ 不能用 `report.get("exit_code") or 1`：exit_code=0 是**合法成功值**，
    #   会被 `or` 当成假值改写成 1，把「下单并撤单成功」谎报成「环境不满足」。
    #   2026-11-10 实测踩中（paper 已成交 + 撤单不适用 = 0 → 打印成 1）。
    raw_code = report.get("exit_code")
    code = 1 if raw_code is None else int(raw_code)
    report["exit_code_meaning"] = {
        0: ("下单并撤单成功" if report.get("cancel_applicable") is not False
            else "下单成功；撤单不适用（paper 已成交，真实撤单未验证）"),
        1: "环境/前置不满足（未发单）",
        2: "下单成功但撤单未确认（需人工）", 3: "下单失败（未产生委托）",
        4: "参数错误",
    }.get(code, "未知")
    os.makedirs(OUT_DIR, exist_ok=True)
    out = json_out or os.path.join(
        OUT_DIR, "qmt_order_smoke_%s.json" % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:  # noqa: BLE001
        print("[WARN] 证据写入失败：%s" % exc)
    print("[%s] %s" % (report["ts"], verdict))
    print("  mode=%s code=%s vol=%s price=%s order_id=%s" % (
        report["mode"], report["code"], report["volume"], report["price"],
        report.get("order_id", "-")))
    for item in report["steps"]:
        flag = {True: "OK", False: "FAIL", None: "SKIP"}[item["ok"]]
        print("  [%s] %-18s %s" % (flag, item["step"], str(item["detail"])[:150]))
    print("  evidence: %s" % out)
    print("  exit_code: %d (%s)" % (code, report["exit_code_meaning"]))
    return code


if __name__ == "__main__":
    sys.exit(main())
