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


def test_bridge_start_idempotent_no_duplicate_subprocess():
    a = _make_adapter()
    try:
        a.start()
        pid1 = a._proc.pid
        a.start()  # 幂等：不应重新拉起第二个子进程
        assert a._proc is not None and a._proc.pid == pid1, \
            "重复 start 不应创建第二个子进程（防同账户多连接/子进程泄漏）"
        assert a.is_connected()
    finally:
        a.close()
    assert a._proc is None


def test_bridge_is_connected_false_after_subprocess_death():
    a = _make_adapter()
    try:
        a.start()
        assert a.is_connected()
        a._proc.kill()  # 模拟子进程崩溃/被强杀
        a._proc.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            if not a.is_connected():
                break
            time.sleep(0.05)
        assert not a.is_connected(), "子进程死亡后 is_connected 应返回 False（健康监控据此重连）"
    finally:
        a.close()
    assert a._proc is None


def test_bridge_conn_state_pushed_on_sdk_disconnect():
    """SDK 断开（服务端状态泵轮询到）→ 服务端推送 conn_state:false → 客户端 is_connected 翻转。"""
    a = _make_adapter()
    try:
        a.start()
        assert a.is_connected()
        a._rpc("_simulate_disconnect", [])  # 服务端 MockAdapter 翻转 is_connected=False
        # 状态泵（2s 间隔）轮询到变化后推送 conn_state:false，客户端 _connected 随之翻转
        deadline = time.time() + 10
        while time.time() < deadline:
            if not a.is_connected():
                break
            time.sleep(0.05)
        assert not a.is_connected(), "SDK 断开后客户端 is_connected 应为 False"
    finally:
        a.close()
    assert a._proc is None


def test_bridge_conn_state_immediate_on_call_failure():
    """真实查询抛 BrokerNotConnectedError → 服务端立即推送 conn_state:false（不等状态泵轮询）。"""
    a = _make_adapter()
    try:
        a.start()
        assert a.is_connected()
        raised = False
        try:
            a._rpc("_simulate_disconnect_error", [])
        except Exception as exc:  # noqa: BLE001
            raised = True
            assert "SDK 已断开" in str(exc), str(exc)
        assert raised
        deadline = time.time() + 10
        while time.time() < deadline:
            if not a.is_connected():
                break
            time.sleep(0.05)
        assert not a.is_connected(), "查询失败后应经 conn_state 立即把 is_connected 翻为 False"
    finally:
        a.close()
    assert a._proc is None


def test_bridge_start_force_restart_when_sdk_disconnected():
    """SDK 断开后 start() 应强制重启子进程（新 PID）并恢复连接，而不是复用旧子进程。"""
    a = _make_adapter()
    try:
        a.start()
        pid1 = a._proc.pid
        assert a.is_connected()
        a._rpc("_simulate_disconnect", [])
        deadline = time.time() + 10
        while time.time() < deadline:
            if not a.is_connected():
                break
            time.sleep(0.05)
        assert not a.is_connected()
        # SDK 断开 → start() 必须强制重启子进程（旧子进程里的陈旧连接态会挡住自愈）
        a.start()
        assert a._proc is not None and a._proc.pid != pid1, \
            "SDK 断开后 start 应重启子进程（新 PID），否则无法清掉卡死的 SDK 状态"
        assert a.is_connected(), "强制重启后应恢复连接"
        q = a.get_quote("600519.SH")  # 新子进程应能正常服务
        assert q["last"] == 10.0, q
    finally:
        a.close()
    assert a._proc is None


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
