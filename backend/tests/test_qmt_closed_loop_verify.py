# -*- coding: utf-8 -*-
"""闭环策略体检工具（``scripts/qmt_closed_loop_verify.py``）的契约测试。

为什么单开一个工具而不是扩展 ``qmt_agent_verify.py``
---------------------------------------------------
旧工具判的是文件桥产物（``agent_status.json`` / ``probe_result.json``）。
新规下闭环模式**一个都不会生成** —— 拿旧判据去体检，结论永远是
「策略从未成功启动过」，而它可能正在正常下单。**用错的判据比没有判据更危险。**

所以这组测试重点锁三件事：
  1. 判据必须落在 ``@@QMTW|`` 记录上，且**先判活、再判事**；
  2. 退出码必须如实（0 就绪 / 2 需人工 / 1 环境缺失）——
     ``if check; then echo 就绪; fi`` 这种用法下，打 FAIL 却返回 0 就是静默失败；
  3. 「非闭环产物」必须被点名，否则用户会拿着旧 bundle 反复怀疑终端。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_qmt_agent_bundle as gen  # noqa: E402
import qmt_closed_loop_verify as verify  # noqa: E402

PREFIX = "@@QMTW|"
BOOT_ID = "001a1925a00e7-110"
CFG = {"account_id": "8888", "account_type": "etf", "trading_enabled": True}
PLAN = {
    "id": "p-test-01", "strat": "etf_rot", "window": ["09:35", "14:50"],
    "rule": {"type": "rebalance",
             "target": [{"code": "513100.SH", "w": 0.5}, {"code": "511990.SH", "w": 0.5}]},
    "guard": {"max_orders": 8, "max_turnover": 1.0, "stop_dd": 0.0},
}


def _closed_bundle(tmp_path) -> Path:
    text = gen.build(embed=CFG, stamp="test", plan=PLAN, bundle_id="b-test")
    out = tmp_path / "closed.py"
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


def _bridge_bundle(tmp_path) -> Path:
    text = gen.build(embed=CFG, stamp="test")
    out = tmp_path / "bridge.py"
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


def _line(kind, data, seq, ts=None, boot_id=BOOT_ID):
    return PREFIX + json.dumps({
        "k": kind, "s": seq, "b": boot_id,
        "t": time.time() if ts is None else ts, "d": data,
    }, ensure_ascii=False, separators=(",", ":"))


def _log(tmp_path, lines, name="XtClient_20261031.log", encoding="utf-8"):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding=encoding)
    return path


def _boot(ts=None, **over):
    data = {"mode": "closed", "bundle": "b-test", "plan_id": "p-test-01",
            "account": "8888", "window": ["09:35", "14:50"], "targets": 2,
            "nav": 1000000.0, "positions": 0, "trading_enabled": True,
            "readonly": "", "fatal": ""}
    data.update(over)
    return _line("BOOT", data, 1, ts)


def _run(tmp_path, bundle, lines, max_age_s=90.0, now=None, encoding="utf-8"):
    log = _log(tmp_path, lines, encoding=encoding)
    records, stats = verify.collect_records(str(log))
    return verify.evaluate(verify.inspect_bundle(str(bundle)), records, stats,
                           now=now, max_age_s=max_age_s)


# ---------------------------------------------------------------------------
# 1. bundle 侧判定
# ---------------------------------------------------------------------------
def test_inspect_flags_closed_bundle(tmp_path):
    info = verify.inspect_bundle(str(_closed_bundle(tmp_path)))
    assert info["ok"] is True
    assert info["closed_loop"] is True
    assert info["plan_id"] == "p-test-01"


def test_inspect_flags_bridge_bundle_as_not_closed(tmp_path):
    """没烘焙计划的 bundle 必须被点名 —— 否则用户会拿着旧产物怀疑终端。"""
    info = verify.inspect_bundle(str(_bridge_bundle(tmp_path)))
    assert info["ok"] is True
    assert info["closed_loop"] is False


def test_inspect_missing_bundle_is_env_error(tmp_path):
    info = verify.inspect_bundle(str(tmp_path / "nope.py"))
    assert info["exists"] is False
    code, lines, _ = verify.evaluate(info, [], {})
    assert code == verify.EXIT_ENV
    assert any("不存在" in ln for ln in lines)


def test_comment_mentioning_embedded_plan_is_not_closed(tmp_path):
    """注释里写着 EMBEDDED_PLAN 不算闭环产物 —— 判据必须是「赋值右侧非 None」。"""
    text = "# 这里解释 EMBEDDED_PLAN 是什么\nEMBEDDED_PLAN = None\n"
    assert verify._has_embedded_plan(text) is False


# ---------------------------------------------------------------------------
# 2. 日志侧判定
# ---------------------------------------------------------------------------
def test_no_records_means_not_running(tmp_path):
    bundle = _closed_bundle(tmp_path)
    code, lines, details = _run(tmp_path, bundle, ["[XtClient] 普通日志行"])
    assert code == verify.EXIT_ACTION
    assert details["records"] == 0
    assert any("一条 @@QMTW| 记录都没有" in ln for ln in lines)


def test_healthy_session_is_ready(tmp_path):
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [
        _boot(ts=now - 5),
        _line("STATUS", {"pending": 0, "orders_sent": 0, "halted": False,
                         "readonly": ""}, 2, ts=now - 2),
    ]
    code, out, details = _run(tmp_path, bundle, lines, now=now)
    assert code == verify.EXIT_OK, out
    assert details["alive"] is True
    assert details["boot_id"] == BOOT_ID
    assert details["plan_id"] == "p-test-01"
    assert any("闭环就绪" in ln for ln in out)


def test_stale_heartbeat_is_action(tmp_path):
    """心跳过期 ⇒ 需人工。注意网络类违规的处置是**停模**，日志里没有异常堆栈。"""
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [_boot(ts=now - 600), _line("STATUS", {}, 2, ts=now - 600)]
    code, out, details = _run(tmp_path, bundle, lines, now=now)
    assert code == verify.EXIT_ACTION
    assert details["alive"] is False
    assert any("心跳过期" in ln for ln in out)


def test_readonly_is_action(tmp_path):
    """只读 ⇒ 需人工。真机上的顺序是 READONLY(s=1) → BOOT(s=2)，
    所以**只读记录会出现在 BOOT 之前**，判据不能用 seq/时间戳卡边界。"""
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [
        _line("READONLY", {"reason": "柜台持仓查询失败（不是空仓）", "fatal": False}, 0,
              ts=now - 6),
        _boot(ts=now - 5, readonly="柜台持仓查询失败（不是空仓）"),
        _line("STATUS", {}, 3, ts=now - 2),
    ]
    code, out, details = _run(tmp_path, bundle, lines, now=now)
    assert code == verify.EXIT_ACTION
    assert "柜台持仓查询失败" in details["readonly"]
    assert any("只读" in ln for ln in out)


def test_same_seq_under_same_boot_is_deduped(tmp_path):
    """同一 ``(boot_id, seq)`` 只算一条 —— 这是跨重启去重的前提。

    这条用例的来历：写测试时 BOOT 与 READONLY 都用了 seq=1，结果 BOOT 被
    当成重复记录丢掉，判定直接跑偏。**它证明去重真的在生效**，而不是摆设。
    """
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [
        _line("READONLY", {"reason": "x"}, 1, ts=now - 6),
        _line("BOOT", {"plan_id": "p-test-01"}, 1, ts=now - 5),
        _line("STATUS", {}, 2, ts=now - 2),
    ]
    code, out, details = _run(tmp_path, bundle, lines, now=now)
    assert details["records"] == 2, "同 seq 未被去重"
    assert any("没有 BOOT" in ln for ln in out)


def test_recover_clears_readonly(tmp_path):
    """READONLY 之后有 RECOVER ⇒ 当前不是只读（历史状态不能污染当前判定）。"""
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [
        _boot(ts=now - 20),
        _line("READONLY", {"reason": "x"}, 2, ts=now - 18),
        _line("RECOVER", {"reason": "柜台持仓查询已恢复"}, 3, ts=now - 15),
        _line("STATUS", {}, 4, ts=now - 2),
    ]
    code, out, details = _run(tmp_path, bundle, lines, now=now)
    assert details["readonly"] == ""
    assert code == verify.EXIT_OK, out


def test_halt_is_action(tmp_path):
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [
        _boot(ts=now - 20),
        _line("HALT", {"reason": "委托 cw1 已 300 秒既未成交也未回报"}, 2, ts=now - 10),
        _line("STATUS", {}, 3, ts=now - 2),
    ]
    code, out, details = _run(tmp_path, bundle, lines, now=now)
    assert code == verify.EXIT_ACTION
    assert "300 秒" in details["halted"]
    assert any("已停机" in ln for ln in out)


def test_reject_points_at_account_type(tmp_path):
    """拒单要给出可操作的下一步：ETF 计划用 stock 类型会被 A 股正则拒掉。"""
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [
        _boot(ts=now - 20),
        _line("REJECT", {"cid": "cw1", "code": "513100.SH",
                         "reason": "不符合 stock 标的格式"}, 2, ts=now - 10),
        _line("STATUS", {}, 3, ts=now - 2),
    ]
    code, out, details = _run(tmp_path, bundle, lines, now=now)
    assert code == verify.EXIT_ACTION
    assert details["rejects"] == 1
    assert any("default_account_type" in ln for ln in out)


def test_records_before_boot_do_not_count(tmp_path):
    """上一次启动的 READONLY/REJECT 不能被算进本次会话。"""
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [
        _line("REJECT", {"reason": "上一轮历史拒单"}, 1, ts=now - 100, boot_id="old-boot"),
        _boot(ts=now - 10),
        _line("STATUS", {}, 2, ts=now - 2),
    ]
    code, out, details = _run(tmp_path, bundle, lines, now=now)
    assert details["rejects"] == 0
    assert details["boot_id"] == BOOT_ID
    assert code == verify.EXIT_OK, out


def test_boot_without_boot_record_is_action(tmp_path):
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [_line("STATUS", {}, 1, ts=now - 1)]
    code, out, _ = _run(tmp_path, bundle, lines, now=now)
    assert code == verify.EXIT_ACTION
    assert any("没有 BOOT" in ln for ln in out)


def test_gbk_log_is_readable(tmp_path):
    """XtClient 日志是 GBK；整文件 decode 会在第一处混码就炸掉整份日志。"""
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    lines = [_boot(ts=now - 5), _line("STATUS", {}, 2, ts=now - 2)]
    code, out, _ = _run(tmp_path, bundle, lines, now=now, encoding="gbk")
    assert code == verify.EXIT_OK, out


# ---------------------------------------------------------------------------
# 3. 日志目录选择（防「按 mtime 取到副日志」的假阴性）
# ---------------------------------------------------------------------------
def test_newest_log_skips_datasource_sibling(tmp_path):
    qmt = tmp_path / "qmt"
    logdir = qmt / "userdata" / "log"
    logdir.mkdir(parents=True)
    main = logdir / "XtClient_20261031.log"
    main.write_text("x\n", encoding="utf-8")
    sibling = logdir / "XtClient_datasource_20261031.log"
    sibling.write_text("y\n", encoding="utf-8")
    import os
    newer = time.time() + 60
    os.utime(str(sibling), (newer, newer))     # 副日志更新 ⇒ 老实现必然选错
    assert verify.newest_log(str(qmt)) == str(main)


def test_newest_log_missing_dir_returns_empty(tmp_path):
    assert verify.newest_log(str(tmp_path / "nope")) == ""


# ---------------------------------------------------------------------------
# 4. CLI 退出码必须如实
# ---------------------------------------------------------------------------
def test_cli_exit_code_is_action_when_not_running(tmp_path):
    bundle = _closed_bundle(tmp_path)
    log = _log(tmp_path, ["[XtClient] 普通行"])
    rc = verify.main(["--bundle", str(bundle), "--log", str(log)])
    assert rc == verify.EXIT_ACTION, "打 FAIL 却返回 0 就是静默失败"


def test_cli_exit_code_is_zero_when_ready(tmp_path):
    bundle = _closed_bundle(tmp_path)
    now = time.time()
    log = _log(tmp_path, [_boot(ts=now - 5), _line("STATUS", {}, 2, ts=now - 1)])
    assert verify.main(["--bundle", str(bundle), "--log", str(log)]) == verify.EXIT_OK


def test_cli_exit_code_is_env_when_bundle_missing(tmp_path):
    rc = verify.main(["--bundle", str(tmp_path / "nope.py"), "--log", "x.log"])
    assert rc == verify.EXIT_ENV


def test_cli_json_output_is_serializable(tmp_path, capsys):
    bundle = _closed_bundle(tmp_path)
    log = _log(tmp_path, ["[XtClient] 普通行"])
    verify.main(["--bundle", str(bundle), "--log", str(log), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit"] == verify.EXIT_ACTION
    assert payload["details"]["closed_loop"] is True
