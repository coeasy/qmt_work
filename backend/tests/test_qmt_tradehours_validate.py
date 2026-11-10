# -*- coding: utf-8 -*-
"""交易时段验证脚本的**结论判据**回归锁（纯函数，不触 IPC、不写证据文件）。

为什么单独测：``_conclusion`` 决定退出码，而退出码是 09:38 定时任务唯一读到的
"通没通"。历史上这里出过两类错，方向相反但一样致命：

* **假绿** 把 ``getStockListInSector`` 的 5224 条代码表 / ``subscribeQuote`` 的
  ACK 当成"行情已通"；
* **假红** 把退出码 0 只绑在 ``capture_58670.proven`` 上 —— 只要 XtModel 没起，
  即便 58600 拉取型行情完全可用也一律判"未证实"，等于把环境缺失写成接口不通。

本文件只测纯函数，不连端口、不启动任何客户端进程。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qmt_tradehours_validate.py"
BACKEND = ROOT / "backend"


@pytest.fixture(scope="module")
def validator():
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(
        "_qmt_tradehours_validate_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _report(**overrides):
    """58670 未运行、58600 在监听、只拿到代码表的典型盘前现场。"""
    base = {
        "ports": {"status": "ok",
                  "ports": {"58600": ["18924"], "58610": [], "58670": []}},
        "probe_58600": {"status": "ok", "quote_proven": False,
                        "quote_shapes": [], "code_list_proven": True,
                        "nonempty_shapes": 1},
        "probe_58670_tag": {"status": "unavailable"},
        "probe_58670_fulltick": {"status": "unavailable", "quote_proven": False},
        "capture_58670": {"status": "unavailable", "proven": False},
        "capture_58600": {"status": "ok", "proven": False},
        "installs": {"status": "ok"},
        "file_orders": {"status": "ok"},
    }
    base.update(overrides)
    return base


def test_pull_quote_proven_is_enough_for_exit_zero(validator):
    """★ XtModel 没起 ≠ 行情不通：58600 拉到 tick 就该判 0。"""
    rep = _report(probe_58600={"status": "ok", "quote_proven": True,
                               "quote_shapes": ["getMarketData {...}"],
                               "code_list_proven": True})
    got = validator._conclusion(rep)
    assert got["exit_code"] == 0
    assert got["push_proven"] is False and got["pull_proven"] is True
    assert "拉取" in got["verdict"]


def test_push_quote_proven_is_exit_zero(validator):
    rep = _report(capture_58670={"status": "ok", "proven": True})
    got = validator._conclusion(rep)
    assert got["exit_code"] == 0
    assert got["push_proven"] is True
    assert "推送" in got["verdict"]


def test_code_list_alone_is_not_quote(validator):
    """★ 5224 个代码只是证券代码表，不能顶数成"行情已证实"。"""
    got = validator._conclusion(_report())
    assert got["exit_code"] == 2
    assert got["code_list_proven"] is True and got["pull_proven"] is False
    assert "不算行情数据" in got["verdict"]


def test_subscribe_ack_is_not_a_quote_push(validator):
    rep = _report(capture_58600={"status": "ok", "proven": False,
                                 "summary": {"verdict": "只有订阅应答，无推送"}})
    got = validator._conclusion(rep)
    assert got["exit_code"] == 2 and got["push_proven"] is False


def test_missing_58670_is_reported_as_environment_absence(validator):
    got = validator._conclusion(_report())
    assert got["channel_58670_missing"] is True
    assert "不是接口不通" in got["verdict"]
    assert "XtModel" in got["verdict"]


def test_no_listeners_and_no_installs_is_exit_one(validator):
    rep = _report(ports={"status": "ok", "ports": {"58600": [], "58670": []}},
                  installs={"status": "skipped"},
                  probe_58600={"status": "unavailable", "quote_proven": False,
                               "code_list_proven": False})
    got = validator._conclusion(rep)
    assert got["exit_code"] == 1
    assert "环境缺失" in got["verdict"]


def test_netstat_unavailable_is_exit_one_not_green(validator):
    """netstat 拿不到 ⇒ 本轮没查过监听，不能报"检查通过"。"""
    rep = _report(ports={"status": "unavailable", "ports": {}})
    got = validator._conclusion(rep)
    assert got["exit_code"] == 1
    assert got["push_proven"] is False and got["pull_proven"] is False
