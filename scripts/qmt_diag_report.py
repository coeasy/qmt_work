# -*- coding: utf-8 -*-
"""生成结构化 diag_report.json —— 大 QMT 桥接诊断的一站式快照。

用法：
  python scripts/qmt_diag_report.py --qmt-dir P:\\stock\\gd_qmt \
      --out output/diag_report.json

为什么必须结构化：命令行打印给人看就够了，但**分享排障日志**和**给开发者定位**
都要一份机器可读的快照。这份 JSON 汇总三条诊断链路：

  probe   = qmt_strategy_list_probe.py --json   （注册树状态）
  verify  = qmt_agent_verify.py --json         （心跳 + 能力面）
  inspect = qmt_agent_deploy.py inspect --force（bundle + agent_config.json）

外加汇总的 ``problems[]`` 数组，前端/开发者扫一眼就知道该怎么修。

退出码：
  0  = 三条链路全部通过（registered=true + verify.ok=true + 无 PermissionError）
  2  = 有异常但可解析（写入 JSON，仍 exit 2）
  1  = 环境不成立（找不到 QMT / 找不到 python）
"""
from __future__ import print_function

import argparse
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _run(args_list, timeout=30):
    """跑一个子进程，返回 (returncode, stdout, stderr)。失败不抛。"""
    try:
        p = subprocess.Popen(
            args_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 —— 单个子进程挂了不能让整个报告生成失败
        return -1, "", "subprocess exception: %s: %s" % (type(exc).__name__, exc)


def collect(qmt_dir):
    """并行跑三条诊断链路，返回汇总 dict。"""
    problems = []

    # --- probe ---
    try:
        rc_probe, out_probe, err_probe = _run(
            [sys.executable, os.path.join(_HERE, "qmt_strategy_list_probe.py"),
             "--qmt-dir", qmt_dir, "--json"])
    except Exception as exc:  # noqa: BLE001 —— 报告生成不能因为一条链路炸掉
        rc_probe, out_probe, err_probe = -1, "", "subprocess raised: %s: %s" % (
            type(exc).__name__, exc)
    probe = None
    if rc_probe < 0 or not out_probe.strip():
        probe = {"error": "probe 子进程失败 (rc=%d): %s" % (rc_probe, err_probe[:500]),
                 "raw": out_probe[:500]}
    else:
        try:
            probe = json.loads(out_probe)
        except Exception as exc:
            probe = {"error": "probe JSON 解析失败: %s" % exc, "raw": out_probe[:500]}
    if not probe.get("registered"):
        problems.append({
            "source": "probe",
            "msg": "策略 '%s' 未在 QMT 注册树里 —— 模型交易里看不到它"
                   % (probe.get("target") or "?"),
            "fix": "走 QMT GUI「模型研究」→ 右键「导入本地策略」选 %s，重启 QMT"
                   % (probe.get("target_file") or "qmt_work_agent.py"),
        })
    if not probe.get("file_present"):
        problems.append({
            "source": "probe",
            "msg": "策略文件不存在: %s" % (probe.get("target_file") or "?"),
            "fix": "跑 deploy_qmt_work_agent.bat 或 python scripts/qmt_agent_deploy.py deploy",
        })
    if not probe.get("autorun"):
        problems.append({
            "source": "probe",
            "msg": "策略未启用 startupAutorun —— QMT 启动不会自动拉起",
            "fix": "在「模型交易」里选中策略 → 勾选「自动运行」",
        })

    # --- verify ---
    try:
        rc_verify, out_verify, err_verify = _run(
            [sys.executable, os.path.join(_HERE, "qmt_agent_verify.py"),
             "--qmt-dir", qmt_dir, "--json"])
    except Exception as exc:  # noqa: BLE001
        rc_verify, out_verify, err_verify = -1, "", "subprocess raised: %s: %s" % (
            type(exc).__name__, exc)
    verify = None
    if rc_verify < 0 or not out_verify.strip():
        verify = {"error": "verify 子进程失败 (rc=%d): %s" % (rc_verify, err_verify[:500]),
                  "raw": out_verify[:500]}
    else:
        try:
            verify = json.loads(out_verify)
        except Exception as exc:
            verify = {"error": "verify JSON 解析失败: %s" % exc, "raw": out_verify[:500]}
    if not verify.get("ok"):
        # details 里有 problems 列表，逐条搬进汇总
        details = verify.get("details") or {}
        for msg in (details.get("problems") or []):
            problems.append({"source": "verify", "msg": msg})

    # --- inspect ---
    try:
        rc_inspect, out_inspect, err_inspect = _run(
            [sys.executable, os.path.join(_HERE, "qmt_agent_deploy.py"),
             "inspect", "--force", "--qmt-dir", qmt_dir])
    except Exception as exc:  # noqa: BLE001
        rc_inspect, out_inspect, err_inspect = -1, "", "subprocess raised: %s: %s" % (
            type(exc).__name__, exc)
    inspect = {"rc": rc_inspect, "stdout": out_inspect, "stderr": err_inspect}
    if "PermissionError" in (out_inspect + err_inspect):
        problems.append({
            "source": "inspect",
            "msg": "agent_config.json / bundle 被 QMT 独占锁定",
            "fix": "关闭 QMT 客户端再跑一次 inspect；或用 --force 只看 bundle 状态",
        })

    # 三态判定：probe.registered=True + verify.ok=True + 无 inspect PermissionError
    # 任一为假（含降级为 error dict 时字段缺失）→ ok=False
    ok = bool(
        probe.get("registered") and verify.get("ok")
        and not any(p["source"] == "inspect" and "PermissionError" in p["msg"]
                    for p in problems)
    )

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "qmt_dir": qmt_dir,
        "python": sys.executable,
        "probe": probe,
        "verify": verify,
        "inspect": inspect,
        "problems": problems,
        "ok": ok,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成结构化 diag_report.json")
    ap.add_argument("--qmt-dir", default=None, help="QMT 安装目录")
    ap.add_argument("--out", default=None,
                    help="输出 JSON 文件路径（默认 output/diag_report.json）")
    ap.add_argument("--stdout", action="store_true",
                    help="同时把 JSON 打印到 stdout（默认只写文件）")
    args = ap.parse_args(argv)

    sys.path.insert(0, _HERE)
    import qmt_agent_deploy as dep  # noqa: E402

    qmt = dep.find_qmt_dir(args.qmt_dir)
    if not qmt:
        sys.stderr.write("[FAIL] 未找到 QMT 安装目录（用 --qmt-dir 指定）\n")
        return 1

    report = collect(qmt)

    out = args.out or os.path.join(
        os.path.dirname(_HERE), "output", "diag_report.json")
    d = os.path.dirname(out)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    if args.stdout:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print("[OK] 已写入 %s (ok=%s, problems=%d)"
              % (out, report["ok"], len(report["problems"])))

    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
