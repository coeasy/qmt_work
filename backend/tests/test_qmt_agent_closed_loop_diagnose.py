# -*- coding: utf-8 -*-
"""``/qmt-agent/diagnose`` 与 ``/qmt-agent/closed-loop`` 的**闭环判据**契约测试。

为什么这组测试必须存在
----------------------
新规（2026-10-23 起）禁策略进程读写文件。闭环模式下 ``agent_status.json`` /
``probe_result.json`` / ``events.ndjson`` **一个都不会生成**，而旧诊断只看那三份
文件 —— 于是一条**正在正常下单**的策略会被报成

    「QMT 客户端在跑但 agent 策略未运行（心跳 stale），请在 QMT 里点「运行」」

这是与「假绿灯」对称的反向故障：假红灯一样会让人去修没坏的东西，而且这次
「修」的动作（去点运行）会让用户把一个好策略重启掉，把当天的仓位计划打断。

闭环的唯一证据链是客户端日志里的 ``@@QMTW|`` 行，所以本组用例锁的是：

1. 闭环产物 ⇒ 心跳**必须**从日志推导（不许再看 status 文件）；
2. 闭环产物 ⇒ 不许再报「策略未运行」这类假告警；
3. 旧文件桥产物 ⇒ 判据**不许**被改坏（两套并存，按产物形态分叉）；
4. 判定结论必须与 ``scripts/qmt_closed_loop_verify.py`` 同源（不另写一套）；
5. 能力真相只从 ``BOOT.injected`` 取，**不许**拿 ``trading_enabled`` 冒充。

零 mock 纪律：日志行一律用真实生产者 ``closed_loop.emit()`` 构造，bundle 一律
用真实 ``gen_qmt_agent_bundle.build()`` 生成，判定一律走真实
``qmt_closed_loop_verify.evaluate()``。只 monkeypatch 路径解析与进程探测
（``_find_qmt_dir`` / ``_qmt_running``），因为它们本来就该在测试里指向别处。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT / "backend"
for _p in (str(ROOT / "scripts"), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gen_qmt_agent_bundle as gen  # noqa: E402
from agent_bigqmt.closed_loop import emit  # noqa: E402

PLAN = {
    "id": "p-route-01",
    "strat": "etf_rot",
    "window": ["09:30", "14:55"],
    "rule": {"type": "rebalance",
             "target": [{"code": "513100.SH", "w": 0.5},
                        {"code": "511990.SH", "w": 0.5}]},
    "guard": {"max_orders": 8, "max_turnover": 1.0, "stop_dd": 0.0},
}

CFG = {"account_id": "8888", "account_type": "etf", "trading_enabled": True}

BOOT_ID = "boot-route-1"

BOOT_DATA = {
    "mode": "closed", "bundle": "b-route", "agent_ver": "1.3.0",
    "py": "3.6.8", "boot_id": BOOT_ID, "standalone": False,
    "plan_id": "p-route-01", "strat": "etf_rot",
    "window": ["09:30", "14:55"], "targets": 2,
    "guard": PLAN["guard"], "account": "8888",
    "default_account_type": "etf", "query_account_type": "STOCK",
    "trading_enabled": True, "nav": 1000000.0, "positions": 0,
    "injected": ["get_trade_detail_data", "passorder", "get_full_tick"],
    "readonly": "", "fatal": "",
}

STATUS_DATA = {
    "mode": "closed", "bundle": "b-route", "agent_ver": "1.3.0",
    "py": "3.6.8", "uptime_s": 12, "nav": 1000000.0, "positions": 0,
    "pending": 0, "pending_cids": [], "orders_sent": 0, "turnover": 0.0,
    "halted": False, "in_window": True, "readonly": "",
    "plan_id": "p-route-01", "window": ["09:30", "14:55"], "last_error": "",
}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _line(kind, data, seq, ts=None, boot_id=BOOT_ID):
    """用**真实生产者**造一行日志（不手拼 JSON —— 格式一改这里就该跟着红）。"""
    return emit(kind, data, seq, boot_id, ts=ts)


def _write_log(qmt: Path, lines, name="XtClient_20261031.log") -> Path:
    log_dir = qmt / "userdata" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def _deploy(qmt: Path, closed_loop: bool = True) -> Path:
    """按真实生成器产出一份 bundle 并放到策略目录。"""
    if closed_loop:
        text = gen.build(embed=CFG, stamp="test", plan=PLAN, bundle_id="b-route")
    else:
        text = gen.build(embed=CFG, stamp="test", plan=None, bundle_id="")
    target = qmt / "python" / "qmt_work_agent.py"
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


@pytest.fixture
def qa(tmp_path, monkeypatch):
    """加载路由模块并把路径/进程探测指到 tmp_path 造的伪 QMT 根。"""
    sys.modules.pop("app.routes.qmt_agent", None)
    import app.routes.qmt_agent as module

    fake_qmt = tmp_path / "fake_qmt"
    (fake_qmt / "bin.x64").mkdir(parents=True)
    (fake_qmt / "userdata").mkdir(parents=True)
    (fake_qmt / "python").mkdir(parents=True)
    (fake_qmt / "config" / "user" / "root").mkdir(parents=True)

    monkeypatch.setattr(module, "_find_qmt_dir",
                        lambda explicit=None: str(fake_qmt))
    monkeypatch.setattr(module, "_agent_bigqmt_dir",
                        lambda: BACKEND_DIR / "agent_bigqmt")
    monkeypatch.setattr(module, "_qmt_tools_dir", lambda: ROOT / "scripts")
    # 默认「客户端没在跑」：那一步会额外产生「策略未运行」告警，
    # 由专门的用例显式打开，避免污染其它断言。
    monkeypatch.setattr(module, "_qmt_running", lambda: [])

    app = FastAPI()
    app.include_router(module.router, prefix="/api/v1")
    return module, TestClient(app), fake_qmt


def _diagnose(client, strategy="qmt_work_agent"):
    r = client.post("/api/v1/qmt-agent/diagnose", json={"strategy": strategy})
    assert r.status_code == 200
    return r.json()


def _sources(body):
    return [p.get("source") for p in (body.get("data") or {}).get("problems") or []]


def _msgs(body):
    return " | ".join(str(p.get("msg") or "")
                      for p in (body.get("data") or {}).get("problems") or [])


# ---------------------------------------------------------------------------
# 1. 闭环：心跳必须来自日志
# ---------------------------------------------------------------------------
def test_diagnose_closed_loop_heartbeat_comes_from_log(qa):
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    now = time.time()
    _write_log(qmt, [_line("BOOT", BOOT_DATA, 1, ts=now - 5),
                     _line("STATUS", STATUS_DATA, 2, ts=now)])

    body = _diagnose(client)
    assert body["code"] == 0
    data = body["data"]
    state = data["closed_loop"]
    assert state is not None, "闭环产物必须被识别出来"
    assert state["mode"] == "closed_loop"
    assert state["available"] is True
    assert state["plan_id"] == "p-route-01"
    assert state["alive"] is True
    assert state["exit_code"] == 0, state["findings"]
    assert state["boot_id"] == BOOT_ID
    # 心跳与旧模式**同形**：前端只换数据来源，不学第二套字段名
    hb = data["heartbeat"]
    assert hb["runtime_mode"] == "closed_loop"
    assert hb["alive"] is True
    assert hb["agent_ver"] == "1.3.0"
    assert hb["ts"] and hb["ts"][:2] == "20", "ts 必须是 ISO 本地时间串"
    assert hb["in_window"] is True


def test_diagnose_closed_loop_does_not_cry_strategy_not_running(qa):
    """闭环产物 + 客户端在跑 + **没有** agent_status.json ⇒ 不许报「策略未运行」。"""
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    now = time.time()
    _write_log(qmt, [_line("BOOT", BOOT_DATA, 1, ts=now - 5),
                     _line("STATUS", STATUS_DATA, 2, ts=now)])
    module._qmt_running = lambda: ["XtItClient.exe"]

    body = _diagnose(client)
    data = body["data"]
    assert not (qmt / "python" / "agent_status.json").exists(), \
        "闭环不写文件 —— 这条用例的前提就是这份文件不存在"
    assert "未运行" not in _msgs(body), _msgs(body)
    assert data["heartbeat"]["alive"] is True


def test_diagnose_closed_loop_stale_heartbeat_is_action(qa):
    """心跳过期必须报出来（否则「停了」和「在跑」看起来一样）。"""
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    old = time.time() - 3600
    _write_log(qmt, [_line("BOOT", BOOT_DATA, 1, ts=old - 5),
                     _line("STATUS", STATUS_DATA, 2, ts=old)])

    body = _diagnose(client)
    data = body["data"]
    assert data["closed_loop"]["alive"] is False
    assert data["closed_loop"]["exit_code"] == 2
    assert "closed_loop" in _sources(body)
    assert "心跳过期" in _msgs(body), _msgs(body)


def test_diagnose_closed_loop_no_records_says_so(qa):
    """日志里一条记录都没有 ⇒ 结论是「没以闭环形态跑过」，不是「运行正常」。"""
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    _write_log(qmt, ["2026-10-31 09:30:00.001 [INFO] 终端启动"])   # 纯噪音

    body = _diagnose(client)
    data = body["data"]
    assert data["closed_loop"]["exit_code"] == 2
    assert "一条" in _msgs(body), _msgs(body)


# ---------------------------------------------------------------------------
# 2. 闭环：结论与脚本同源
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,payload,needle", [
    ("READONLY", {"reason": "柜台不可用：CounterUnavailable"}, "只读"),
    ("HALT", {"reason": "回撤超限 6.2%"}, "停机"),
    ("REJECT", {"cid": "cw1", "reason": "code 不符合 stock 标的格式"}, "被拒"),
])
def test_diagnose_closed_loop_attention_records_become_problems(qa, kind, payload,
                                                               needle):
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    now = time.time()
    _write_log(qmt, [_line("BOOT", BOOT_DATA, 1, ts=now - 10),
                     _line(kind, payload, 2, ts=now - 8),
                     _line("STATUS", STATUS_DATA, 3, ts=now)])

    body = _diagnose(client)
    data = body["data"]
    assert data["closed_loop"]["exit_code"] == 2
    assert needle in _msgs(body), _msgs(body)
    # 判定原文必须原样带出（界面与命令行同一句话）
    assert any(needle in str(x) for x in data["closed_loop"]["findings"])


def test_diagnose_closed_loop_standalone_is_flagged(qa):
    """独立进程形态：终端不注入任何交易函数 ⇒ 必须明说「一单都发不出去」。"""
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    boot = dict(BOOT_DATA, standalone=True, injected=["get_full_tick"])
    now = time.time()
    _write_log(qmt, [_line("BOOT", boot, 1, ts=now - 3),
                     _line("STATUS", STATUS_DATA, 2, ts=now)])

    body = _diagnose(client)
    data = body["data"]
    assert data["closed_loop"]["standalone"] is True
    assert data["closed_loop"]["exit_code"] == 2
    assert "独立进程" in _msgs(body), _msgs(body)


# ---------------------------------------------------------------------------
# 3. 能力真相：只看 BOOT.injected，不看 trading_enabled
# ---------------------------------------------------------------------------
def test_capability_trading_available_when_passorder_injected(qa):
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    now = time.time()
    _write_log(qmt, [_line("BOOT", BOOT_DATA, 1, ts=now - 3),
                     _line("STATUS", STATUS_DATA, 2, ts=now)])

    data = _diagnose(client)["data"]
    cap = data["capabilities"]["trading"]
    assert cap["available"] is True
    assert cap["source"] == "closed_loop/boot.injected"
    assert "passorder" in data["trade_surface"]["present"]
    assert data["trade_surface"]["can_submit"] is True


def test_capability_trading_unavailable_without_passorder(qa):
    """``trading_enabled=True`` 但终端没注入 passorder ⇒ 结论必须是「不能下单」。

    ★ 这条是防「拿配置开关冒充能力」的：独立进程模式下 trading_enabled 仍为
      true，若拿它当判据，界面会把「一单都发不出去」显示成「可交易」。
    """
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    boot = dict(BOOT_DATA, standalone=True, trading_enabled=True,
                injected=["get_full_tick"])
    now = time.time()
    _write_log(qmt, [_line("BOOT", boot, 1, ts=now - 3),
                     _line("STATUS", STATUS_DATA, 2, ts=now)])

    data = _diagnose(client)["data"]
    cap = data["capabilities"]["trading"]
    assert cap["available"] is False
    assert data["trade_surface"]["can_submit"] is False
    assert data["trade_surface"]["missing"] == ["passorder"]


def test_closed_loop_ignores_missing_bridge_dir(qa):
    """闭环不写文件桥 ⇒ 「bridge_dir 不存在」是无关配置，不许当故障报。"""
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    cfg_path = qmt / "python" / "agent_config.json"
    cfg_path.write_text(json.dumps({"bridge_dir": str(qmt / "no_such_dir")}),
                        encoding="utf-8")
    now = time.time()
    _write_log(qmt, [_line("BOOT", BOOT_DATA, 1, ts=now - 3),
                     _line("STATUS", STATUS_DATA, 2, ts=now)])

    body = _diagnose(client)
    assert "bridge_dir 不存在" not in _msgs(body), _msgs(body)


# ---------------------------------------------------------------------------
# 4. 旧文件桥判据不许被改坏（两套并存）
# ---------------------------------------------------------------------------
def test_file_bridge_bundle_still_uses_status_file(qa):
    """非闭环产物 ⇒ ``closed_loop`` 为 None，心跳仍来自 agent_status.json。"""
    module, client, qmt = qa
    _deploy(qmt, closed_loop=False)
    bridge = qmt / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    (qmt / "python" / "agent_config.json").write_text(
        json.dumps({"bridge_dir": str(bridge), "transport": "file"}),
        encoding="utf-8")
    (bridge / "agent_status.json").write_text(
        json.dumps({"alive": True, "ts": "2026-10-31T09:30:05",
                    "runtime_mode": "standalone", "agent_ver": "1.2.0"}),
        encoding="utf-8")

    data = _diagnose(client)["data"]
    assert data["closed_loop"] is None
    assert data["heartbeat"]["alive"] is True
    assert data["heartbeat"]["agent_ver"] == "1.2.0"
    assert "runtime_mode" not in data["heartbeat"] or \
        data["heartbeat"]["runtime_mode"] != "closed_loop"


def test_file_bridge_bundle_keeps_bridge_dir_problem(qa):
    """旧模式的「bridge_dir 不存在」仍然是真故障 —— 分叉只针对闭环。"""
    module, client, qmt = qa
    _deploy(qmt, closed_loop=False)
    (qmt / "python" / "agent_config.json").write_text(
        json.dumps({"bridge_dir": str(qmt / "no_such_dir")}), encoding="utf-8")

    body = _diagnose(client)
    assert "bridge_dir 不存在" in _msgs(body), _msgs(body)


# ---------------------------------------------------------------------------
# 5. GET /closed-loop（可高频轮询的运行态端点）
# ---------------------------------------------------------------------------
def _closed_loop(client, **params):
    r = client.get("/api/v1/qmt-agent/closed-loop", params=params)
    assert r.status_code == 200
    return r.json()


def test_closed_loop_endpoint_reports_state(qa):
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    now = time.time()
    _write_log(qmt, [_line("BOOT", BOOT_DATA, 1, ts=now - 5),
                     _line("ORDER", {"cid": "cw1", "code": "513100.SH"}, 2,
                           ts=now - 4),
                     _line("STATUS", dict(STATUS_DATA, orders_sent=1), 3, ts=now)])

    body = _closed_loop(client)
    assert body["code"] == 0
    data = body["data"]
    assert data["bundle"]["path"].endswith("qmt_work_agent.py")
    state = data["closed_loop"]
    assert state["alive"] is True
    assert state["exit_code"] == 0, state["findings"]
    assert state["orders"] == 1
    assert data["checked_at"]


def test_closed_loop_endpoint_none_for_file_bridge_bundle(qa):
    module, client, qmt = qa
    _deploy(qmt, closed_loop=False)
    body = _closed_loop(client)
    assert body["code"] == 0
    assert body["data"]["closed_loop"] is None


def test_closed_loop_endpoint_honours_explicit_log(qa):
    """指定历史日志时必须读它，而不是「mtime 最新的那份」。"""
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    now = time.time()
    old_log = _write_log(
        qmt, [_line("BOOT", BOOT_DATA, 1, ts=now - 5),
              _line("REJECT", {"cid": "cw9", "reason": "资金不足"}, 2, ts=now)],
        name="XtClient_20261030.log")
    # 另一份更新的日志：心跳新鲜且无拒单。若实现退回 newest_log 就会读到它。
    _write_log(qmt, [_line("BOOT", BOOT_DATA, 1, ts=now - 5),
                     _line("STATUS", STATUS_DATA, 2, ts=now)],
               name="XtClient_20261031.log")

    body = _closed_loop(client, log_path=str(old_log))
    state = body["data"]["closed_loop"]
    assert state["log_path"] == str(old_log)
    assert state["rejects"] == 1
    assert state["exit_code"] == 2


def test_closed_loop_endpoint_requires_bundle(qa):
    module, client, qmt = qa
    body = _closed_loop(client)
    assert body["code"] == 503
    assert "找到" in body["message"]


def test_closed_loop_endpoint_requires_qmt_dir(qa, monkeypatch):
    module, client, qmt = qa
    monkeypatch.setattr(module, "_find_qmt_dir", lambda explicit=None: None)
    body = _closed_loop(client)
    assert body["code"] == 400


# ---------------------------------------------------------------------------
# 6. 判据不可用时必须说「判不出来」
# ---------------------------------------------------------------------------
def test_diagnose_reports_unavailable_verifier_instead_of_fallback(qa, monkeypatch):
    """闭环体检脚本缺失 ⇒ 明确报「无法判定」，**不许**静默退回旧判据。

    静默退化正是这个 bug 的成因：旧判据在闭环下必然得出「未运行」。
    """
    module, client, qmt = qa
    _deploy(qmt, closed_loop=True)
    monkeypatch.setattr(module, "_closed_loop_verify", lambda: None)
    # 客户端在跑：最容易叠出「策略未运行」这句假结论的场景
    module._qmt_running = lambda: ["XtItClient.exe"]

    body = _diagnose(client)
    data = body["data"]
    assert data["closed_loop"]["available"] is False
    assert "无法按闭环口径判定" in _msgs(body), _msgs(body)
    # 三态第二态：判不出来时**不许**再说「未运行」（自相矛盾的假红灯）
    assert data["heartbeat"]["known"] is False
    assert "未运行" not in _msgs(body), _msgs(body)
