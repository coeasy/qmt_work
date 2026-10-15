"""XTPQuantAdapter 交易连接重试**不得泄漏 XtQuantTrader 实例**（2026-10-15 真机取证）。

## 缺陷回顾（实测复现，非推测）

本机两台 QMT（``userdata`` / ``userdata_mini``）的券商授权都把
``mdl_auth_gt_ipc_pair`` 关成了 0，客户端日志原文：

    CIPCManager::init, not auth:mdl_auth_gt_ipc_pair
    The XtQuantServer is not allowed to start.

⇒ 外部 xtquant 直连在这台机器上**永远** ``connect rc=-1``。用干净进程做**单次**
连接可以复现这一点（``scripts/probe_xtquant_once.py`` 实测：start rc=None、
connect rc=-1），说明券商拒绝是**真实成因**。

但平台侧报的却是另一个错误：

    交易连接异常：WaitingFreeWriter instances exceed maximum limit

干净进程单次连接**复现不出来**，说明它不是券商给的，而是本程序自己造的：

* ``start()`` 的重试循环每次都 ``XtQuantTrader(dir, sid)`` 新建实例，
  却**只在 ``connect()`` 失败时** ``stop()`` —— ``start()`` 抛异常时直接 continue，
  实例连同它内部的 writer 一起泄漏；
* 一轮最多 2 目录 × 6 session = 12 个实例，而守护每 30s 就来一轮；
* 几十轮后 SDK 的 writer 池被撑爆 ⇒ ``WaitingFreeWriter`` 反过来**盖住了真实的
  券商拒绝**（rc=-1），且**越重试越不可能恢复**（实测 reconnect_attempts 35/36）。

这是典型的「重试自伤」：本该是自救手段的重试，变成了故障放大器。

## 本文件锁死什么

1. 每次失败的尝试**一律释放**（``stop()``），包括 ``start()`` 抛异常的情形；
2. 撞上「资源池耗尽」类错误时**立即停止重试**，不再新建任何实例，
   且错误文案必须区分「券商拒绝」与「本机自己把资源占满了」（处置完全相反）；
3. 未耗尽时保留原有重试行为（不因这次修复误伤正常重试）。
"""
from __future__ import annotations

import sys
import types

import pytest

from xtquant_client.xtp import adapter as adapter_mod
from xtquant_client.xtp.adapter import XTPQuantAdapter

CLIENT_PATH = r"C:\fake\qmt\userdata"

#: 资源耗尽型错误的原文（取自 SDK 实际抛出的字符串）
EXHAUSTED_MSG = "WaitingFreeWriter instances exceed maximum limit"


class _FakeTrader:
    """记录所有被创建的实例，便于断言「建了几个 / 释放了几个」。"""

    instances: list = []
    start_raises: BaseException | None = None
    start_rc = None
    connect_rc = 0

    def __init__(self, path, session):
        self.path = path
        self.session = session
        self.stopped = 0
        self.subscribed = False
        _FakeTrader.instances.append(self)

    def start(self):
        if _FakeTrader.start_raises is not None:
            raise _FakeTrader.start_raises
        return _FakeTrader.start_rc

    def connect(self):
        return _FakeTrader.connect_rc

    def stop(self):
        self.stopped += 1

    def subscribe(self, acc):  # noqa: ANN001
        self.subscribed = True


class _FakeAccount:
    """``StockAccount`` 替身（成功路径会被实例化并 subscribe）。"""

    def __init__(self, account_id, account_type):
        self.account_id = account_id
        self.account_type = account_type


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeTrader.instances = []
    _FakeTrader.start_raises = None
    _FakeTrader.start_rc = None
    _FakeTrader.connect_rc = 0
    yield
    _FakeTrader.instances = []


@pytest.fixture
def _patched(monkeypatch):
    """把连接路径上一切真实 SDK / 磁盘访问换成桩，只留重试循环本身。"""
    monkeypatch.setattr(adapter_mod, "_resolve_xtquant_path", lambda p: "")
    monkeypatch.setattr(adapter_mod, "_load_trader_api",
                        lambda: (_FakeTrader, {"STOCK": _FakeAccount}))
    monkeypatch.setattr(adapter_mod, "_probe_xtdata", lambda xt, p: (True, ""))
    # 只有一个候选目录 ⇒ 一轮 6 次 session 尝试，断言好写也够覆盖泄漏路径
    monkeypatch.setattr(adapter_mod, "_effective_trade_dir",
                        lambda p, mode: (p, mode if mode != "auto" else "full"))
    monkeypatch.setattr(adapter_mod, "_running_client_exes", lambda: [])
    monkeypatch.setattr(adapter_mod, "_latest_login_log", lambda d: "")
    monkeypatch.setattr(adapter_mod, "_shell_attr",
                        lambda name: (lambda *a, **k: {}))
    # `import xtquant.xtdata` / `from xtquant.xttrader import …` 必须在桩环境下可解析。
    # 父模块要有 `__path__`，否则 `from xtquant.xttrader import X` 会抛
    # 「'xtquant' is not a package」（成功路径会走到 register_callback）。
    fake_xtdata = types.ModuleType("xtquant.xtdata")
    xt_parent = types.ModuleType("xtquant")
    xt_parent.__path__ = []          # 声明为包，子模块 import 才不会报「not a package」
    xttrader = types.ModuleType("xtquant.xttrader")
    xttrader.XtQuantTraderCallback = object
    monkeypatch.setitem(sys.modules, "xtquant", xt_parent)
    monkeypatch.setitem(sys.modules, "xtquant.xtdata", fake_xtdata)
    monkeypatch.setitem(sys.modules, "xtquant.xttrader", xttrader)
    return fake_xtdata


def _adapter() -> XTPQuantAdapter:
    return XTPQuantAdapter(client_path=CLIENT_PATH, account_id="22453951",
                           account_type="STOCK", session_id=0, client_mode="full")


def test_start_exception_must_release_every_attempt(_patched):
    """★ 核心修复一：``start()`` **抛异常**时也必须 stop()（旧实现只 continue）。

    否则每轮最多泄漏 12 个实例 —— 几十轮后 SDK writer 池被撑爆，
    真实错误（rc=-1）被 ``WaitingFreeWriter`` 盖住。
    """
    _FakeTrader.start_raises = RuntimeError("sdk internal error")
    a = _adapter()
    with pytest.raises(Exception) as ei:
        a.start()
    assert "sdk internal error" in str(ei.value)
    assert len(_FakeTrader.instances) == 6, "应按 6 个 session 依次尝试"
    assert all(t.stopped == 1 for t in _FakeTrader.instances), (
        "每次失败的尝试都必须释放，否则就是把重试变成泄漏")


def test_connect_failure_releases_and_reports_broker_rejection(_patched):
    """``connect()`` 非 0：正常重试 6 次、全部释放，且报**券商侧**原因。"""
    _FakeTrader.connect_rc = -1
    a = _adapter()
    with pytest.raises(Exception) as ei:
        a.start()
    assert len(_FakeTrader.instances) == 6
    assert all(t.stopped == 1 for t in _FakeTrader.instances)
    # 未撞资源耗尽 ⇒ 保留原有的「交易连接失败（session_id … rc）」口径
    assert "交易连接失败" in str(ei.value), str(ei.value)
    assert "资源已耗尽" not in str(ei.value)


def test_resource_exhausted_stops_retrying_immediately(_patched):
    """★ 核心修复二：撞上资源池耗尽 ⇒ **立即停止**，不再新建任何实例。

    继续重试只会把池占得更满 —— 这是「重试自伤」的关键：它让一个本来
    「重启本程序即可恢复」的问题变成了永不恢复。
    """
    _FakeTrader.start_raises = RuntimeError(EXHAUSTED_MSG)
    a = _adapter()
    with pytest.raises(Exception):
        a.start()
    assert len(_FakeTrader.instances) == 1, (
        "资源耗尽后必须立刻中止重试，不得再新建 XtQuantTrader")
    assert _FakeTrader.instances[0].stopped == 1


def test_resource_exhausted_error_names_the_real_cause(_patched):
    """错误文案必须区分「券商拒绝」与「本机资源耗尽」—— 处置完全相反。

    混为一谈会让用户反复重启 QMT 客户端，而真正的修法是**重启本程序**。
    """
    _FakeTrader.start_raises = RuntimeError(EXHAUSTED_MSG)
    a = _adapter()
    with pytest.raises(Exception) as ei:
        a.start()
    msg = str(ei.value)
    assert "资源已耗尽" in msg, msg
    assert "重启本程序" in msg, msg
    assert "不是重启 QMT 客户端" in msg, msg


def test_successful_connect_keeps_trader_and_does_not_stop(_patched):
    """对照组：真连上时不得把 trader 释放掉（防修复过度）。"""
    _FakeTrader.connect_rc = 0
    a = _adapter()
    a.start()
    assert a.is_connected()
    assert len(_FakeTrader.instances) == 1
    assert _FakeTrader.instances[0].stopped == 0, "成功的会话绝不能被 stop()"
