# -*- coding: utf-8 -*-
"""下单→快速撤单冒烟脚本的**判据**回归锁（纯函数 + 假 HTTP，不碰真服务）。

锁两件历史上真实踩到的事：

1. ``_finish`` 用 ``int(report.get("exit_code") or 1)`` —— ``0`` 被 ``or`` 当成假值
   改写成 ``1``，把「下单并撤单成功」谎报成「环境/前置不满足」。2026-11-10 实测踩中。
2. paper 引擎按限价**立即成交**，撤单回 503「模拟盘…券商柜台不存在」。这是刻意契约，
   不是「撤单失败、可能留有挂单」；脚本若把它报成 exit 2，就是**假红**，会诱导人工
   去追一笔根本不存在的挂单。

同时锁 live 护栏：四道前置缺一即拒绝发单，绝不静默降级。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qmt_order_smoke.py"
BACKEND = ROOT / "backend"


@pytest.fixture(scope="module")
def smoke():
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location("_qmt_order_smoke_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _base_report(**over):
    rep = {
        "ts": "2026-11-10 08:28:00", "mode": "paper", "code": "513090",
        "volume": 100, "price": 1.5, "conn_id": "c1",
        "steps": [], "exit_code": 0,
    }
    rep.update(over)
    return rep


def test_finish_keeps_exit_zero(smoke, tmp_path):
    """★ exit_code=0 是合法成功值，不能被 `or 1` 吞成 1。"""
    out = tmp_path / "e.json"
    code = smoke._finish(_base_report(exit_code=0), str(out), "下单并撤单成功")
    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["exit_code"] == 0
    assert doc["exit_code_meaning"] == "下单并撤单成功"


def test_finish_marks_paper_cancel_not_applicable(smoke, tmp_path):
    """paper 已成交 ⇒ 撤单不适用；meaning 必须写清「真实撤单未验证」。"""
    out = tmp_path / "e.json"
    rep = _base_report(exit_code=0, cancel_applicable=False)
    code = smoke._finish(rep, str(out), "paper 下单成功且已成交；撤单在模拟盘不适用")
    assert code == 0
    meaning = json.loads(out.read_text(encoding="utf-8"))["exit_code_meaning"]
    assert "撤单不适用" in meaning
    assert "未验证" in meaning


def test_finish_defaults_to_one_when_exit_code_missing(smoke, tmp_path):
    rep = _base_report()
    rep.pop("exit_code")
    assert smoke._finish(rep, str(tmp_path / "e.json"), "x") == 1


def test_live_preconditions_lists_every_gap(smoke, monkeypatch):
    """四道前置缺项必须逐条列出，而不是一句「不可用」。"""
    monkeypatch.setattr(smoke, "_http", lambda *a, **k: (200, {
        "code": 0,
        "data": {"brokers": [{"conn_id": "c1", "connected": False}]},
    }))
    monkeypatch.setattr(smoke.os.path, "exists", lambda p: False)
    ok, gaps = smoke._live_preconditions("c1")
    assert ok is False
    joined = "；".join(gaps)
    assert "未连接" in joined
    assert "需先切 live" in joined
    assert "agent_status.json" in joined


def test_live_refuses_without_placing(smoke, monkeypatch, tmp_path):
    """live 前置不全 ⇒ exit 1 且**没有任何下单请求**。"""
    calls: list[str] = []

    def fake_http(method, path, body=None, timeout=20.0):
        calls.append(path)
        if path == "/health":
            return 200, {"code": 0, "data": {"version": "x", "brokers": []}}
        return 200, {"code": 0, "data": {}}

    monkeypatch.setattr(smoke, "_http", fake_http)
    monkeypatch.setattr(smoke.os.path, "exists", lambda p: False)
    code = smoke.main(["--live", "--conn-id", "c1",
                       "--json-out", str(tmp_path / "live.json")])
    assert code == 1
    assert "/trade/order" not in calls, "前置不全时绝不能发单"
    assert "/trade/precheck" not in calls, "前置不全时连预检都不该跑"


def test_paper_filled_is_success_not_hang(smoke, monkeypatch, tmp_path):
    """paper 立即成交 ⇒ exit 0 + cancel_applicable=False（不得报「留有挂单」）。"""
    def fake_http(method, path, body=None, timeout=20.0):
        if path == "/health":
            return 200, {"code": 0, "data": {"version": "x", "brokers": []}}
        if path == "/trade/precheck":
            return 200, {"code": 0, "data": {"allowed": True, "reason": "ok"}}
        if path == "/trade/order":
            return 200, {"code": 0, "data": {"order_id": "PAPER-99"}}
        if path == "/trade/orders":
            return 200, {"code": 0, "data": {"items": [
                {"order_id": "PAPER-99", "status": "filled", "filled": 100}]}}
        if path == "/trade/cancel":
            return 503, {"code": 503, "message": "该委托号属于「模拟盘」（本地撮合），"
                                                "券商柜台不存在这笔委托，无可撤销的挂单。"}
        return 200, {"code": 0, "data": {}}

    monkeypatch.setattr(smoke, "_http", fake_http)
    out = tmp_path / "paper.json"
    code = smoke.main(["--json-out", str(out)])
    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["cancel_applicable"] is False
    assert doc["order_before_cancel"]["status"] == "filled"
    cancel_step = [s for s in doc["steps"] if s["step"] == "cancel"][0]
    assert cancel_step["ok"] is None, "不适用必须是 SKIP，不是 FAIL"
    assert "撤单不适用" in str(cancel_step["detail"])


def test_health_down_is_exit_one(smoke, monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, "_http", lambda *a, **k: (0, {"code": -1, "message": "conn refused"}))
    code = smoke.main(["--json-out", str(tmp_path / "down.json")])
    assert code == 1
