# -*- coding: utf-8 -*-
"""结构化 diag_report.json 的回归锁。

锁定：
  * scripts/qmt_diag_report.py 存在且非空
  * scripts/qmt_strategy_list_probe.py 支持 --json（供报告聚合消费）
  * diag_qmt_work_agent.bat 调用了 qmt_diag_report.py（不是只 print）
  * qmt_diag_report.collect() 能正确处理三个子命令返回的 JSON
    （含失败降级：subprocess 抛异常、JSON 解析失败、verify.ok=False 等）

历史教训：命令行输出给人看就够了，但分享排障日志、给开发者定位、CI 汇总
都要机器可读的快照。若把这条链路悄悄删掉，用户排障退化为「复制粘贴终端输出」，
开发者再也没法把 problems[] 喂给前端。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

DIAG_REPORT = SCRIPTS / "qmt_diag_report.py"
PROBE = SCRIPTS / "qmt_strategy_list_probe.py"
DIAG_BAT = ROOT / "diag_qmt_work_agent.bat"


# --------------------------------------------------------------------------
# 静态检查：文件与 argparse 参数
# --------------------------------------------------------------------------

def test_diag_report_script_exists():
    assert DIAG_REPORT.exists(), f"qmt_diag_report.py 缺失: {DIAG_REPORT}"
    assert DIAG_REPORT.stat().st_size > 1000, "qmt_diag_report.py 太小，疑似被清空"


def test_probe_has_json_flag():
    """qmt_strategy_list_probe.py 必须暴露 --json 参数（供报告聚合消费）。"""
    opts = _argparse_options(PROBE)
    assert "--json" in opts, f"qmt_strategy_list_probe.py 缺 --json，现有: {sorted(opts)}"


def test_diag_bat_calls_report_script():
    """diag_qmt_work_agent.bat 必须真的调 qmt_diag_report.py，不能只 print 一句。"""
    text = DIAG_BAT.read_text(encoding="utf-8", errors="replace")
    assert "qmt_diag_report.py" in text, "diag_qmt_work_agent.bat 未调 qmt_diag_report.py"
    assert "diag_report.json" in text, "diag_qmt_work_agent.bat 未指定 diag_report.json 输出路径"


def test_diag_report_collect_returns_structured_json(tmp_path, monkeypatch):
    """collect() 必须产出包含 timestamp / qmt_dir / probe / verify / inspect / problems[] 的结构。"""
    # 用 sys.executable 跑一个不存在的探针子命令 —— 强制走「子进程失败降级」路径，
    # 同时验证 problems[] 不会因为 JSON 解析失败而炸掉整个报告
    spec = importlib.util.spec_from_file_location("qmt_diag_report", DIAG_REPORT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    # monkeypatch _run 让子进程返回伪造输出（避免真的跑三次 subprocess）
    def fake_run(args, timeout=30):
        cmd = " ".join(args)
        if "qmt_strategy_list_probe.py" in cmd:
            return 2, json.dumps({
                "ok": False, "qmt_dir": "P:\\fake", "target": "qmt_work_agent",
                "target_file": "P:\\fake\\python\\qmt_work_agent.py",
                "file_present": True, "file_size": 52538,
                "registered": False, "autorun": False,
                "listed_count": 0, "autorun_count": 0,
                "missing_in_list": ["qmt_work_agent"],
                "extra_in_list": [],
            }), ""
        if "qmt_agent_verify.py" in cmd:
            return 2, json.dumps({
                "ok": False,
                "details": {"problems": ["策略未启动", "心跳已过期"]},
            }), ""
        if "qmt_agent_deploy.py" in cmd:
            return 0, "[FAIL] 未找到 agent_config.json", ""
        return -1, "", "unknown"

    monkeypatch.setattr(mod, "_run", fake_run)
    report = mod.collect("P:\\fake")

    # 结构完整性
    assert set(["timestamp", "qmt_dir", "python", "probe", "verify",
                "inspect", "problems", "ok"]).issubset(report.keys())
    assert report["qmt_dir"] == "P:\\fake"
    # probe / verify 是结构化 dict（不是字符串）
    assert isinstance(report["probe"], dict) and report["probe"]["registered"] is False
    assert isinstance(report["verify"], dict) and report["verify"]["ok"] is False
    assert isinstance(report["inspect"], dict) and "stdout" in report["inspect"]
    # problems[] 必须聚合三条链路的问题
    problems = report["problems"]
    assert isinstance(problems, list) and len(problems) >= 2
    sources = {p["source"] for p in problems}
    assert "probe" in sources, "probe 的问题未汇入 problems[]"
    assert "verify" in sources, "verify 的问题未汇入 problems[]"
    # ok=False（因为 registered=False）
    assert report["ok"] is False
    # JSON 序列化不炸
    json.dumps(report, ensure_ascii=False)


def test_diag_report_collect_degrades_on_subprocess_exception(tmp_path, monkeypatch):
    """单个子命令抛异常不能让整个报告生成失败（降级为 error 字段，不炸 collect）。"""
    spec = importlib.util.spec_from_file_location("qmt_diag_report", DIAG_REPORT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    def always_raise(args, timeout=30):
        raise RuntimeError("子进程启动失败（模拟 QMT 独占锁）")

    monkeypatch.setattr(mod, "_run", always_raise)
    report = mod.collect("P:\\fake")

    assert report["probe"].get("error", "").startswith("probe"), (
        f"probe 降级失败：{report['probe']}")
    assert report["verify"].get("error", "").startswith("verify"), (
        f"verify 降级失败：{report['verify']}")
    assert report["ok"] is False
    # problems[] 至少有 verify 的兜底（因为 verify.ok 不存在被视为 False）
    assert isinstance(report["problems"], list)


def _argparse_options(path: pathlib.Path) -> set[str]:
    """从 argparse 定义的 add_argument 提取所有 opt 字符串（AST 静态检查）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    opts: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
            continue
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                opts.add(a.value)
    return opts
