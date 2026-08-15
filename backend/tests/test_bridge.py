"""BridgeAdapter 完整 IPC 传输集成测试（不依赖真实 xtquant）。

拉起 tests.fake_bridge_server 子进程，验证：
- 启动握手（_ping）/ is_connected
- 行情/账户/交易 RPC 代理正确
- 订阅事件（quote）从子进程回推到本地回调
- 关闭时子进程退出、资源清理

通过显式传入 runtime（mode=bridge）强制走桥接路径，覆盖 ABI 不匹配场景。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from xtquant_client.bridge_client import BridgeAdapter  # noqa: E402


def _make_adapter():
    return BridgeAdapter(
        client_path="", account_id="MOCK", account_type="STOCK",
        python_exe=sys.executable,
        server_module="tests.fake_bridge_server",
        runtime={"python_exe": sys.executable, "abi": 311, "mode": "bridge"},
        backend_dir=ROOT)


def test_bridge_handshake_and_queries():
    a = _make_adapter()
    try:
        a.start()
        assert a.is_connected(), "桥接子进程应已连接"
        q = a.get_quote("600519.SH")
        assert q["last"] == 10.0, q
        k = a.get_kline("600519.SH", "1d", 10)
        assert isinstance(k, list) and k, k
        acc = a.get_account()
        assert acc["assets"] == 100.0, acc
        pos = a.get_positions()
        assert pos and pos[0]["code"] == "600519.SH", pos
        o = a.place_order("600519.SH", "buy", "limit", 0, 100, "batch", "")
        assert o["order_id"] == "MOCK1", o
        c = a.cancel_order("O1")
        assert c["status"] == "cancel_submitted", c
    finally:
        a.close()
    assert a._proc is None, "关闭后子进程应被清理"


def test_bridge_subscribe_event():
    a = _make_adapter()
    got = []
    try:
        a.start()
        a.subscribe_quote(["600519.SH"], lambda evt: got.append(evt))
        # 等子进程推送的 quote 事件经 reader 线程回传
        for _ in range(60):
            if got:
                break
            time.sleep(0.05)
    finally:
        a.close()
    assert got, "未收到子进程推送的 quote 事件"
    assert got[0]["data"]["code"] == "600519.SH", got[0]


def test_bridge_init_error_propagates():
    # 用一个启动即抛错的替身：复用 fake server 但 account_id 触发异常？
    # 这里用不存在的 server_module 验证启动失败能被捕获为 BrokerNotConnectedError。
    a = BridgeAdapter(
        client_path="", account_id="MOCK",
        python_exe=sys.executable,
        server_module="tests.nonexistent_module_xyz",
        runtime={"python_exe": sys.executable, "abi": 311, "mode": "bridge"},
        backend_dir=ROOT)
    raised = False
    try:
        a.start()
    except Exception as exc:  # noqa: BLE001
        raised = True
        assert "握手失败" in str(exc) or "启动失败" in str(exc), str(exc)
    finally:
        a.close()
    assert raised, "非法 server_module 应导致 start 抛错"
