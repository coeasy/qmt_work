"""C10 回归测试：XTPQuantAdapter.close() 必须真正释放交易会话。

缺陷背景：旧实现 close() 仅把 self._trader 置 None，不调 trader.stop()——
反复连接/断开会累计残留交易 session 与监听端口，最终同账号会话被占满，
后续连接报「session 被占用」，表现为「连接不上 QMT」。

本测试用 mock trader 直接构造适配器，断言：
- close() 确实调用 trader.stop()（释放 SDK 会话/端口）
- _connected / _trader / _acc 全部复位
- seq→oid 等映射被清空（避免对已 stop 的 trader 继续调用）
- trader.stop() 抛异常时被吞掉，close() 不向上抛（资源清理不因 SDK 异常中断）
"""
import threading
from unittest import mock

from xtquant_client.xtp import XTPQuantAdapter


def _adapter_with_trader(stop_raises: bool = False):
    trader = mock.Mock()
    if stop_raises:
        trader.stop.side_effect = RuntimeError("sdk stop failed")
    a = XTPQuantAdapter(client_path=r"C:\fake\qmt\userdata_mini",
                        account_id="123456", account_type="STOCK", session_id=7)
    a._trader = trader
    a._acc = mock.Mock()
    a._connected = True
    # 预置映射数据，close() 后应被清空（避免对已 stop 的 trader 继续调用）
    with a._map_lock:
        a._seq_to_oid[1] = "O1"
        a._oid_to_seq["O1"] = 1
        a._pending_resp[1] = (threading.Event(), [])
    return a, trader


def test_close_calls_stop_and_resets_state():
    a, trader = _adapter_with_trader()
    assert a.is_connected()
    a.close()
    # 会话/端口必须释放
    trader.stop.assert_called_once()
    # 状态全部复位
    assert a._trader is None
    assert a._acc is None
    assert not a.is_connected()
    # 映射清空，避免回调/竞态复用已停用句柄
    with a._map_lock:
        assert not a._seq_to_oid and not a._oid_to_seq and not a._pending_resp


def test_close_is_safe_when_stop_raises():
    """SDK stop() 抛异常不得阻断 close()（资源清理不因 SDK 异常中断）。"""
    a, trader = _adapter_with_trader(stop_raises=True)
    a.close()  # 不应抛异常
    trader.stop.assert_called_once()
    assert a._trader is None
    assert a._acc is None
    assert not a.is_connected()


def test_close_without_trader_is_noop():
    """未建立交易会话时 close() 必须幂等（不调 stop、不抛错）。"""
    a = XTPQuantAdapter(client_path=r"C:\fake\qmt\userdata_mini",
                        account_id="", account_type="STOCK")
    a.close()  # 不应抛异常，也不应调用任何 SDK stop
    assert a._trader is None
    assert not a.is_connected()
    # 无 trader 时绝不触发 SDK 调用
    assert a._acc is None