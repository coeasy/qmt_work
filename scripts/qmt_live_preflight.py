#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实盘下单**前一键预检**：回答「现在到底能不能下实盘」，逐项给出下一步动作。

为什么需要它（2026-10-12 实测教训）：
    ``POST /signal/mode {"mode":"live"}`` **没有任何护栏**（只校验模式名），
     Operators 切到 live 后下单，看到的第一个错误来自风控 P0-5
    （「账户快照未就绪」）—— 那是**第三层**阻塞，前两层（agent 没被公式
    模式挂载 ⇒ 无 passorder；``trading_enabled=false``）在前面静静躺着。
    本工具把整条阻塞链一次列全，避免 Operator 一层一层试错。

判据（全部可证伪；拿不到证据时标「未知」而**不断言缺失**——过期快照只能
说「陈旧快照里未见」，这是 TD-33 的诚实性纪律）：

  1. QMT 客户端进程在跑（XtItClient.exe）—— 挂载/运行策略的前提
  2. Agent 心跳新鲜（≤45s，见 qmt_agent_verify.STALE_S）
  3. probe 快照新鲜 —— 不新鲜时 4/5/6 一律降级「未知」
  4. 运行时形态 = qmt_formula（standalone_process 拿不到 passorder，属常态不是 bug）
  5. passorder 已注入（下单）；cancel 已注入（撤单）
  6. get_trade_detail_data 已注入（委托/成交/账户查询 ⇒ 账户快照来源）
  7. trading_enabled=true（agent_config.json，持久真源；与运行态不一致 ⇒ 需重启策略）
  8. 部署件新鲜（与当前源码现生成的 bundle 内容一致，防部署漂移假绿灯）
  9. 注册树已登记 + 自动运行（startupAutorun，QMT 重启零人工）
 10. 平台信号模式（paper = 就绪后的最后一步动作；live = 就绪）
 11. 账户快照已喂入风控（DB account_snapshot 最新行 ≤10min，P0-5 放行前提）

用法：
    python scripts/qmt_live_preflight.py [--bridge-dir DIR] [--qmt-dir DIR]
        [--strategy NAME] [--api BASE] [--db PATH] [--json]

退出码：0 = 无阻塞（可以实盘）；1 = 存在阻塞；2 = 环境错误（用法/依赖缺失）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qmt_agent_verify as ver  # noqa: E402  （同目录工具，复用其判据常量与采集）

#: 账户快照喂入风控的新鲜度上限（秒）。SyncEngine 周期同步；超龄视为未喂入。
SNAPSHOT_MAX_AGE_S = 600

#: 后端默认地址（开发态 21118；生产用 --api 覆盖）。
DEFAULT_API = "http://127.0.0.1:21118"

#: QMT 客户端主进程名。
QMT_PROC = "XtItClient.exe"

#: 运行时形态：standalone_process = 外部进程（无注入）；qmt_formula = 公式挂载。
MODE_IN_PROCESS = "qmt_formula"


def _qmt_running():
    """QMT 客户端是否在跑。非 Windows / tasklist 不可用 → None（未知，不断言）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % QMT_PROC],
            capture_output=True, timeout=10)
        text = out.stdout.decode("gbk", "replace")
        return QMT_PROC.lower() in text.lower()
    except Exception:  # noqa: BLE001  探测失败不得让预检崩
        return None


def _api_mode(api_base: str):
    """平台信号模式。返回 (mode, err)；mode=None = 未取到（后端没起不算阻塞）。"""
    url = api_base.rstrip("/") + "/api/v1/signal/mode"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return (body.get("data") or {}).get("mode"), ""
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)


def _snapshot_age_s(db_path: str):
    """最新 account_snapshot 行年龄（秒）。返回 (age, err)；age=None = 读不到。"""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % Path(db_path).as_posix(),
                               uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT ts FROM account_snapshot ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)
    if not row or not row[0]:
        return None, "account_snapshot 无任何行（风控从未收到账户快照）"
    try:
        age = (datetime.now() - datetime.fromisoformat(row[0])).total_seconds()
    except Exception as exc:  # noqa: BLE001
        return None, "ts 解析失败 %r: %s" % (row[0], exc)
    return age, ""


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="实盘下单前一键预检（阻塞链全列 + 每项下一步动作）")
    ap.add_argument("--bridge-dir",
                    default=os.environ.get("QMT_BRIDGE_DIR", ver.DEFAULT_BRIDGE))
    ap.add_argument("--qmt-dir", default=os.environ.get("QMT_DIR"),
                    help="QMT 安装目录（默认自动探测；用于核对注册树与部署件）")
    ap.add_argument("--strategy", default="qmt_work_agent", help="策略名")
    ap.add_argument("--api", default=DEFAULT_API,
                    help="后端 base URL（默认开发态 %(default)s；查询信号模式）")
    ap.add_argument("--db", default=os.environ.get("QMT_DB_PATH"),
                    help="主库路径（核对账户快照；不给则跳过该项）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    qmt = args.qmt_dir
    if not qmt:
        try:
            import qmt_agent_deploy as dep  # noqa: E402
            qmt = dep.find_qmt_dir(None)
        except Exception:  # noqa: BLE001
            qmt = None

    # ---- 采集（复用 qmt_agent_verify 的同一套判据，不造第二真源）----
    try:
        data = ver.collect(args.bridge_dir, qmt, args.strategy)
        _ok, _lines, details = ver.evaluate(data)
    except Exception as exc:  # noqa: BLE001
        print("[FAIL] 采集失败：%s: %s" % (type(exc).__name__, exc))
        return 2

    probe = data.get("probe") or {}
    reg = data.get("reg") or {}
    bundle = data.get("bundle") or {}
    probe_stale = bool(details.get("probe_stale"))
    injected = details.get("injected") or []
    runtime_mode = probe.get("runtime_mode") or ""

    checks = []  # {level, name, ok, detail, action}

    def add(level, name, ok, detail, action=""):
        checks.append({"level": level, "name": name, "ok": ok,
                       "detail": detail, "action": action})

    stale_note = "陈旧快照里未见 —— **不能断言当前缺失**，运行策略后重测"

    # 1. QMT 客户端进程
    running = _qmt_running()
    if running is None:
        add("info", "QMT 客户端进程", None, "tasklist 不可用（非 Windows？）")
    elif running:
        add("ok", "QMT 客户端进程", True, "%s 在跑" % QMT_PROC)
    else:
        add("block", "QMT 客户端进程", False, "%s 未运行" % QMT_PROC,
            "先启动大 QMT 客户端（挂载/运行策略都要在客户端里操作）")

    # 2. 心跳新鲜
    if details.get("alive"):
        add("ok", "Agent 心跳", True, "心跳新鲜（≤%ds）" % ver.STALE_S)
    else:
        add("block", "Agent 心跳", False, "心跳过期或缺失",
            "在 QMT「模型交易」里运行策略；确认桥目录 %s 正确" % args.bridge_dir)

    # 3. probe 快照新鲜度（决定 4/5/6 是断言还是「未知」）
    if probe_stale:
        add("info", "probe 快照", None, stale_note)

    # 4. 运行时形态
    if runtime_mode == MODE_IN_PROCESS:
        add("ok", "运行时形态", True, runtime_mode)
    elif runtime_mode:
        add("block", "运行时形态", False, "%s（拿不到终端注入函数）" % runtime_mode,
            "QMT 客户端「模型研究 → 右键 → 导入本地策略」挂载 "
            "QMT_WORK_AGENT.py 并运行（见实盘操作手册 Phase 2）")
    else:
        add("info", "运行时形态", None, stale_note)

    # 5/6. 注入函数（name 用函数名本身，宽度按最长名 get_trade_detail_data 定）
    for fn, why in (("passorder", "下单"), ("cancel", "撤单"),
                    ("get_trade_detail_data", "委托/成交/账户查询")):
        if fn in injected:
            add("ok", fn, True, "已捕获（%s）" % why)
        elif probe_stale:
            add("info", fn, None, stale_note)
        else:
            add("block", fn, False, "未捕获（%s不可用）" % why,
                "同「运行时形态」：以公式策略形态 in-process 挂载后重测")

    # 7. trading_enabled（配置文件 = 持久真源；运行态不一致 ⇒ 需重启策略）
    cfg_path = os.path.join(qmt, "python", "agent_config.json") if qmt else None
    cfg_trading = None
    if cfg_path and os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                cfg_trading = bool(json.load(fh).get("trading_enabled"))
        except Exception as exc:  # noqa: BLE001
            add("info", "trading_enabled", None, "配置读取失败: %s" % exc)
    if cfg_trading is True:
        if details.get("trading_enabled") is False:
            add("act", "trading_enabled", True,
                "配置已开但运行态未生效",
                "重启策略（QMT 里停止再运行）后重跑本工具")
        else:
            add("ok", "trading_enabled", True, "已开启")
    elif cfg_trading is False:
        add("block", "trading_enabled", False, "agent_config.json 为 false",
            "python scripts/qmt_agent_deploy.py config --trading "
            "（或手改 agent_config.json）后重启策略")
    else:
        add("info", "trading_enabled", None, "未找到配置文件")

    # 8. 部署新鲜度（防「源码已修、部署件滞后」的漂移假绿灯）
    bpath = bundle.get("path")
    if bpath:
        fresh = ver.bundle_freshness(bpath)
        if fresh.get("fresh") is True:
            add("ok", "部署件新鲜度", True, "与现生成 bundle 一致")
        elif fresh.get("fresh") is False:
            add("block", "部署件新鲜度", False,
                "部署件与源码漂移（%s，首个差异行 %s）"
                % (fresh.get("reason"), fresh.get("first_diff_line")),
                "重新部署：python scripts/qmt_agent_deploy.py deploy --force")
        else:
            add("info", "部署件新鲜度", None, "无法判定: %s" % fresh.get("reason"))
    else:
        add("info", "部署件新鲜度", None,
            "bundle 未校验（未提供 --qmt-dir 或文件缺失）")

    # 9. 注册树 + 自动运行
    if reg.get("registered") is True:
        add("ok", "注册树登记", True, "已登记")
        if reg.get("autorun") is not True:
            add("act", "自动运行", False, "未开自动运行（startupAutorun）",
                "「模型交易」里右键策略勾选「自动运行」，QMT 重启才零人工")
    elif reg.get("registered") is False:
        add("block", "注册树登记", False, "注册树未登记",
            "GUI 导入本地策略（register 子命令有分步指引）："
            "python scripts/qmt_agent_deploy.py register --reveal")
    else:
        add("info", "注册树登记", None, "未校验（缺 QMT 目录或主日志）")

    # 10. 平台信号模式
    mode, mode_err = _api_mode(args.api)
    if mode == "live":
        add("ok", "平台信号模式", True, "live")
    elif mode == "paper":
        add("act", "平台信号模式", False, "paper（其余就绪后的最后一步）",
            "curl -X POST %s/api/v1/signal/mode -d '{\"mode\":\"live\"}'"
            % args.api)
    elif mode == "dry_run":
        add("act", "平台信号模式", False, "dry_run（预演态）",
            "切 live：POST %s/api/v1/signal/mode {\"mode\":\"live\"}" % args.api)
    else:
        add("info", "平台信号模式", None, "未取到（%s）" % (mode_err or "后端未启动"))

    # 11. 账户快照（P0-5 放行前提）
    if args.db:
        age, snap_err = _snapshot_age_s(args.db)
        if age is None:
            add("block", "账户快照", False, snap_err,
                "确认 agent 注入了 get_trade_detail_data 且 SyncEngine "
                "在同步活跃连接；无快照时 P0-5 会拒实盘买入")
        elif age <= SNAPSHOT_MAX_AGE_S:
            add("ok", "账户快照", True, "最新快照 %.0fs 前" % age)
        else:
            add("block", "账户快照", False, "最新快照已 %.0fs（>上限 %ds）"
                % (age, SNAPSHOT_MAX_AGE_S),
                "等一个同步周期；仍不更新则查 SyncEngine 对活跃连接的账户拉取")
    else:
        add("info", "账户快照", None, "未提供 --db，跳过（P0-5 就绪度未核）")

    # ---- 汇总 ----
    n_block = sum(1 for c in checks if c["level"] == "block")
    n_act = sum(1 for c in checks if c["level"] == "act")
    n_unk = sum(1 for c in checks if c["level"] == "info")

    if args.json:
        print(json.dumps({"blockers": n_block, "actions": n_act,
                          "unknown": n_unk, "checks": checks,
                          "qmt_dir": qmt, "bridge_dir": args.bridge_dir},
                         ensure_ascii=False))
        return 1 if n_block else 0

    print("=" * 68)
    print("实盘下单预检 · %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 68)
    mark = {"ok": "[v]", "block": "[x]", "act": "[!]", "info": "[-]"}
    for c in checks:
        print(" %s %-21s %s" % (mark[c["level"]], c["name"],
                                "就绪" if c["ok"] is True else
                                ("阻塞" if c["level"] == "block" else
                                 ("待动作" if c["level"] == "act" else "未知"))))
        if c["detail"]:
            print("       %s" % c["detail"])
        if c["action"]:
            print("       → %s" % c["action"])
    print("-" * 68)
    verdict = "可以实盘" if n_block == 0 else "不可实盘（%d 项阻塞）" % n_block
    print("结论: %s · 阻塞 %d · 待动作 %d · 未知 %d"
          % (verdict, n_block, n_act, n_unk))
    return 1 if n_block else 0


if __name__ == "__main__":
    sys.exit(main())
