# -*- coding: utf-8 -*-
"""2026-10-23 新规三条限制的**可执行证明**（策略沙箱替身）。

券商公告原文（2026-10-23 收盘后生效）::

    一、关闭本地文件读写功能，包括 open、read、write 及相关第三方库。
    二、关闭外部网络连接功能，包括策略发起的 HTTP、TCP、UDP 等网络连接，
        MySQL、Redis、SQLite 等本机/局域网数据库连接，以及与局域网设备通信、
        IP/端口连接及相关第三方库。
    三、限制在虚拟机运行，包括腾讯云、阿里云等公有云服务器。

第三条是**部署形态**问题（物理机 + 内置 Python），代码里无从验证 —— 由
``DEPLOY.md`` 的合规对照表与体检脚本承载。前两条在这里被钉死。

为什么是**运行时替身**，而不是静态扫 ``open(``
--------------------------------------------
bundle 里**必须**留着旧文件桥的代码（新规生效前的产物还要能跑；删掉等于把两套
控制流合并成一套，风险更大）。所以静态扫描只能得出「代码里有 open」，得不出
「闭环路径走不到 open」—— 而后者才是真正要保证的事。假绿灯正是这么来的：
扫描通过 ⇒ 以为合规 ⇒ 真机上每拍抛 ``Forbidden FileIO``。

判据只有一条：**把禁止的 API 换成抛异常的替身，让闭环策略跑完整生命周期，
一个都不许碰到**。替身行为照抄真机（``PermissionError: Forbidden FileIO``）。
"""
from __future__ import annotations

import builtins
import contextlib
import importlib.util
import io
import json
import os
import socket
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

import gen_qmt_agent_bundle as gen  # noqa: E402
import check_bigqmt_agent_py36 as check  # noqa: E402

ACCOUNT = "8888"
NAV = 1000000.0
PRICE = 4.0

PLAN = {
    "id": "p-sandbox-01",
    "strat": "etf_rot",
    "window": ["00:00", "23:59"],
    "rule": {"type": "rebalance",
             "target": [{"code": "513100.SH", "w": 0.5},
                        {"code": "511990.SH", "w": 0.5}]},
    "guard": {"max_orders": 8, "max_turnover": 1.0, "stop_dd": 0.0},
}

CFG = {"account_id": ACCOUNT, "account_type": "etf", "trading_enabled": True}


# ---------------------------------------------------------------------------
# 沙箱替身：把「新规禁止的 API」换成抛异常的版本
# ---------------------------------------------------------------------------
#: 逐条对应公告。``os.stat``/``os.listdir`` 等一并算文件读写 —— 真机沙箱里
#: 连 ``os.path.exists`` 都会抛，只禁 ``open`` 会漏掉「探测型」访问。
_FILE_APIS = (
    (builtins, "open"), (io, "open"),
    (os, "remove"), (os, "rename"), (os, "replace"), (os, "makedirs"),
    (os, "mkdir"), (os, "rmdir"), (os, "listdir"), (os, "scandir"),
    (os, "stat"), (os, "walk"), (os, "chmod"), (os, "utime"),
    (os.path, "exists"), (os.path, "isdir"), (os.path, "isfile"),
    (os.path, "getsize"), (os.path, "getmtime"), (os.path, "islink"),
)

#: 公告第二条：策略发起的一切 IP/端口连接，回环**不豁免**。
_NET_APIS = (
    (socket, "socket"), (socket, "create_connection"), (socket, "socketpair"),
    (socket, "getaddrinfo"), (socket, "gethostbyname"),
    (sqlite3, "connect"),
)

#: 第三条 + 前两条的**共同后门**：起一个子进程，文件 IO 与网络就都归它了。
#: 静态侧由 ``check_bigqmt_agent_py36.py`` 的 G4 拦；这里兜住运行时。
_EXEC_APIS = (
    (os, "system"), (os, "popen"), (os, "startfile"),
    (os, "spawnv"), (os, "execv"), (os, "fork"),
)


def _chain(limit=10):
    """调用链（纯内存，不碰 linecache）—— 违规时告诉人「是哪个函数碰的」。"""
    frames = []
    try:
        f = sys._getframe(2)
        while f is not None and len(frames) < limit:
            frames.append("%s:%s(%s)" % (os.path.basename(f.f_code.co_filename),
                                         f.f_lineno, f.f_code.co_name))
            f = f.f_back
    except Exception:
        pass
    return frames


class Sandbox(object):
    """记录所有越界访问；一旦发生就按真机行为抛 ``PermissionError``。"""

    def __init__(self):
        self.hits = []

    def _deny(self, api, kind):
        def _raise(*_a, **_k):
            self.hits.append((api, _chain()))
            raise PermissionError("%s: %s" % (kind, api))
        return _raise


@contextlib.contextmanager
def sandbox(monkeypatch):
    box = Sandbox()
    for mod, name in _FILE_APIS:
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, box._deny(name, "Forbidden FileIO"),
                                raising=False)
    for mod, name in _NET_APIS:
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, box._deny(name, "Forbidden Network"),
                                raising=False)
    for mod, name in _EXEC_APIS:
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, box._deny(name, "Forbidden Process"),
                                raising=False)
    yield box


# ---------------------------------------------------------------------------
# 夹具（与 test_bigqmt_closed_loop_agent 同构，刻意重复以免跨文件耦合）
# ---------------------------------------------------------------------------
class FakeBroker(object):
    def __init__(self, nav=NAV, positions=None, orders=None, price=PRICE):
        self.nav = nav
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.price = price
        self.placements = []

    def get_trade_detail_data(self, account, account_type, kind):
        if kind == "POSITION":
            return self.positions
        if kind == "ACCOUNT":
            return [{"m_dBalance": self.nav}] if self.nav else []
        if kind == "ORDER":
            return self.orders
        return None

    def passorder(self, *args):
        self.placements.append({"cid": args[9] if len(args) > 9 else ""})
        return ["OID%d" % len(self.placements)]

    def get_full_tick(self, codes):
        return dict((c, {"lastPrice": self.price}) for c in codes)


class FakeCtx(object):
    def __init__(self, broker):
        self._broker = broker

    def get_full_tick(self, codes):
        return self._broker.get_full_tick(codes)


def _load(path: Path, tag=""):
    name = "qmt_sandbox_%s_%s" % (tag, uuid.uuid4().hex)
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle(tmp_path, plan=PLAN, cfg=CFG, tag=""):
    """生成 bundle 并导入 —— **必须在进入沙箱之前完成**（生成/导入要读文件）。"""
    text = gen.build(embed=cfg, stamp="test", plan=plan,
                     bundle_id="b-test" if plan else "")
    out = tmp_path / ("qmt_agent_%s.py" % (tag or uuid.uuid4().hex[:8]))
    out.write_text(text, encoding="utf-8", newline="\n")
    return _load(out, tag), out


def _wire(module, broker):
    module.get_trade_detail_data = broker.get_trade_detail_data
    module.passorder = broker.passorder
    return FakeCtx(broker)


def _records(capsys):
    out = capsys.readouterr().out
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("@@QMTW|"):
            rows.append(json.loads(line[len("@@QMTW|"):]))
    return rows


# ---------------------------------------------------------------------------
# 0. 先证明**替身本身是可证伪的** —— 探针不会失败等于没有探针
# ---------------------------------------------------------------------------
def test_sandbox_denies_file_io_and_network(monkeypatch, tmp_path):
    """替身必须真的拦得住；拦不住的沙箱会给出一个假的「合规」结论。"""
    with sandbox(monkeypatch) as box:
        with pytest.raises(PermissionError):
            open(str(tmp_path / "x.txt"), "w")
        with pytest.raises(PermissionError):
            os.makedirs(str(tmp_path / "d"))
        with pytest.raises(PermissionError):
            os.path.exists(str(tmp_path))
        with pytest.raises(PermissionError):
            socket.socket()
        with pytest.raises(PermissionError):
            sqlite3.connect(":memory:")
        with pytest.raises(PermissionError):
            os.system("echo hi")
    assert len(box.hits) == 6
    assert box.hits[0][0] == "open"
    assert box.hits[3][0] == "socket"
    assert box.hits[5][0] == "system"
    # 调用链要能指回「谁碰的」，否则报错只剩一句「有违规」，无法定位
    assert box.hits[0][1], "违规记录没有调用链"


# ---------------------------------------------------------------------------
# 1. 第一条 + 第二条：闭环跑完整生命周期，一个禁止 API 都不许碰
# ---------------------------------------------------------------------------
def test_closed_loop_lifecycle_touches_no_file_and_no_socket(tmp_path, monkeypatch,
                                                             capsys):
    """init → 多拍 tick → 成交/委托回报 → 跨日复位 → 心跳：全程零 IO、零外联。

    这是本组测试的核心：它同时覆盖「启动期」（读配置/自检/心跳）与「运行期」
    （事件回传/持仓重建/在途核销）两条最容易偷偷退回文件 IO 的路径。
    """
    module, _ = _bundle(tmp_path)
    broker = FakeBroker()
    ctx = _wire(module, broker)
    t0 = time.time()

    with sandbox(monkeypatch) as box:
        module.init(ctx)
        for i in range(5):
            module.tick(t0 + 10 + i)
        # 回报：委托 + 成交（成交会走持仓增量与在途核销）
        cid = list(module._STATE.get("pending") or {})
        remark = cid[0] if cid else "p-sandbox-01-1"
        module.order_callback(ctx, {"m_strRemark": remark, "m_nVolume": 100,
                                    "m_strInstrumentID": "513100"})
        module.deal_callback(ctx, {"m_strRemark": remark, "m_nVolume": 100,
                                   "m_strInstrumentID": "513100"})
        # 跨日复位 + 心跳（STATUS_INTERVAL_S=10s，推到 1 小时后必然触发）
        module.tick(t0 + 3600)

    assert box.hits == [], (
        "闭环路径碰了禁止 API（新规第一/二条）:\n%s"
        % "\n".join("  - %s ← %s" % (api, " ← ".join(chain[:4]))
                    for api, chain in box.hits))
    kinds = [r["k"] for r in _records(capsys)]
    assert "BOOT" in kinds, "沙箱里跑完却没有 BOOT 回传 —— 外部等于瞎了"
    assert "STATUS" in kinds, "心跳没走日志行"


def test_closed_loop_emits_without_any_writable_path(tmp_path, monkeypatch, capsys):
    """配置里的 ``bridge_dir`` 指向一个**不可写**位置也照跑 —— 闭环不依赖它。

    ★ 这条挡的是「某天有人为了兼容把心跳塞回 bridge_dir」：那种改动在本机
      （目录可写）不会红，只有在真机沙箱里才炸，而炸的表现是「静默无事件」。
    """
    cfg = dict(CFG)
    cfg["bridge_dir"] = "/nonexistent/forbidden/bridge"
    module, _ = _bundle(tmp_path, cfg=cfg)
    ctx = _wire(module, FakeBroker())

    with sandbox(monkeypatch) as box:
        module.init(ctx)
        module.tick(time.time() + 15)

    assert box.hits == []
    assert [r["k"] for r in _records(capsys)]


# ---------------------------------------------------------------------------
# 2. 反向对照：旧文件桥在沙箱里**必然**违规 —— 证明上面的绿灯不是真空
# ---------------------------------------------------------------------------
def test_bridge_mode_does_hit_forbidden_apis_so_harness_is_not_vacuous(
        tmp_path, monkeypatch):
    """同一套替身下，旧文件桥模式必须**当场撞墙**。

    如果这条也「通过」（即 bridge 模式零违规），说明替身根本没生效 ——
    那么上一条的绿灯就是假的。这是本仓「探针必须可证伪」纪律的直接应用。
    """
    module, _ = _bundle(tmp_path, plan=None, cfg=None, tag="bridge")
    module.EMBEDDED_PLAN = None          # 明确退回文件桥模式
    module.EMBEDDED_CONFIG = None
    ctx = _wire(module, FakeBroker())

    with sandbox(monkeypatch) as box:
        with pytest.raises(PermissionError):
            module.init(ctx)

    assert box.hits, "文件桥模式在沙箱里居然零违规 —— 替身没生效，结论不可信"


# ---------------------------------------------------------------------------
# 3. 闭环 + 独立进程：**不得**导入 xtdata（否则等于主动连本地行情服务）
# ---------------------------------------------------------------------------
def test_closed_standalone_never_imports_xtdata(tmp_path, monkeypatch, capsys):
    """``run_standalone`` 在闭环模式下必须跳过 xtdata。

    xtdata 取行情要连 ``127.0.0.1:58610``；公告第二条把「策略发起的 TCP」一并
    关闭，回环不豁免，处置是**停模 + 防外挂弹窗**。而闭环在独立进程下本来就
    拿不到终端注入的查询函数（注定只读）—— 导入 xtdata 换不来任何能力，
    只换来一次违规。
    """
    module, _ = _bundle(tmp_path)
    calls = []

    def _fake_import():
        calls.append(1)
        return None

    module._import_xtdata = _fake_import
    # 让主循环第一拍就退出：handlebar 抛 KeyboardInterrupt 会被当作正常退出
    module.handlebar = lambda _ctx: (_ for _ in ()).throw(KeyboardInterrupt())

    rc = module.run_standalone(["QMT_WORK_AGENT.py", str(tmp_path / "userdata")])

    assert calls == [], "闭环模式下导入了 xtdata —— 会连 127.0.0.1:58610（违规）"
    assert rc == 0
    out = capsys.readouterr().out
    assert "跳过(闭环模式不连本地行情服务)" in out, (
        "没有把「为什么没导入 xtdata」说出来，用户会以为行情模块坏了")


def test_bridge_standalone_still_imports_xtdata(tmp_path, monkeypatch):
    """反向对照：旧文件桥模式**仍然**导入 xtdata（本机自测/新规前要用）。

    两条路径必须在同一个开关上分叉；否则「不导入」可能是被别的 bug 顺带实现的，
    哪天修好那个 bug，违规就回来了。
    """
    module, _ = _bundle(tmp_path, plan=None, cfg=None, tag="bridge")
    module.EMBEDDED_PLAN = None
    module.EMBEDDED_CONFIG = None
    calls = []

    module._import_xtdata = lambda: (calls.append(1), None)[1]

    rc = module.run_standalone(["QMT_WORK_AGENT.py", str(tmp_path / "nope")])

    assert calls == [1], "文件桥模式没有导入 xtdata —— 分叉判据被改坏了"
    assert rc == 2   # 没有 agent_config.json ⇒ 初始化失败，如实返回 2


# ---------------------------------------------------------------------------
# 4. 静态闸门 G4：白名单管不住「白名单模块里的越界入口」
# ---------------------------------------------------------------------------
def _gate(tmp_path, source):
    """把一段源码丢进临时 agent 目录，跑真实闸门，返回错误行。"""
    d = tmp_path / ("gate_%s" % uuid.uuid4().hex[:8])
    d.mkdir()
    (d / "BIGQMT_AGENT.py").write_text(source, encoding="utf-8")
    return check.check_dir(d)


@pytest.mark.parametrize("source,needle", [
    ("import os\nos.system('calc')\n", "os.system"),
    ("import os\nos.popen('dir')\n", "os.popen"),
    ("import os\nos.startfile('a.exe')\n", "os.startfile"),
    ("__import__('socket')\n", "动态导入"),
    ("import importlib\nimportlib.import_module('urllib.request')\n", "动态导入"),
    ("eval('1+1')\n", "eval()"),
])
def test_g4_gate_blocks_exec_and_dynamic_import(tmp_path, source, needle):
    """G4 必须**真的会红** —— 否则它就是一条只会打印 OK 的装饰。

    这些写法都能绕过 G2 的 import 白名单：``os`` 本身是白名单模块，
    ``__import__`` 和 ``importlib`` 用的是字符串。新规下起一个子进程去读写文件
    或发包，等价于把三条限制一起绕过。
    """
    errs = _gate(tmp_path, source)
    assert any("G4" in e and needle in e for e in errs), (
        "G4 没拦住 %r，实际错误: %s" % (source.strip(), errs))


def test_g4_gate_accepts_the_real_agent_dir():
    """真实目录必须零错误 —— 否则闸门一上线就把主流程堵死。"""
    assert check.check_dir() == []


def test_g4_gate_allows_variable_dynamic_import(tmp_path):
    """``__import__(name)``（变量）不拦：``self_probe`` 就是这么探测 xtquant 的。

    这条是**误伤回归测试**：静态闸门最容易犯的错是拿形状当语义 —— 把「所有
    ``__import__``」一律判违规，会顺手把自检探针一起封掉。
    """
    src = "def probe(n):\n    return __import__(n)\n"
    assert _gate(tmp_path, src) == []
