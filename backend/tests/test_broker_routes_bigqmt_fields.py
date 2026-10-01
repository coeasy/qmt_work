"""M0 路由面校验：大 QMT 桥接配置的 REST 入参纪律（纯函数层）。

盯的是「配置错误必须在**提交时**被拦住并给出可执行文案」，而不是等到
运行期超时才让人猜。与 test_broker_dedupe.py 同一性质：直接测路由内部
校验函数，不起整站。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routes.broker import _validate_connector_key  # noqa: E402


def test_empty_key_passes_through_mini_path():
    """未启用桥接（miniQMT 老表单）不得被新校验误伤。"""
    assert _validate_connector_key("", "") == ""


def test_unknown_key_rejected_with_hint():
    msg = _validate_connector_key("qmt.big.bridge.mqtt", "C:/bridge")
    assert "未知 connector_key" in msg
    assert "qmt.big.bridge.file" in msg


def test_file_transport_requires_bridge_dir():
    msg = _validate_connector_key("qmt.big.bridge.file", "")
    assert "必须提供" in msg and "bridge_dir" in msg
    # 空白同样拒绝：两侧空格的路径是「一直超时」排障第一名的变种
    assert _validate_connector_key("qmt.big.bridge.file", "   ") != ""


def test_file_transport_with_dir_passes():
    assert _validate_connector_key("qmt.big.bridge.file", "C:/qmt_bridge") == ""


def test_redis_zmq_require_connection_param():
    assert "bridge_dir" in _validate_connector_key("qmt.big.bridge.redis", "")
    assert _validate_connector_key(
        "qmt.big.bridge.redis", "redis://127.0.0.1:6379/0") == ""
    assert "bridge_dir" in _validate_connector_key("qmt.big.bridge.zmq", "")
    assert _validate_connector_key(
        "qmt.big.bridge.zmq", "tcp://127.0.0.1:5555") == ""


def test_direct_bigqmt_path_does_not_require_bridge_dir():
    """路径 A（xtquant 直连大客户端）走 client_path，不该被桥校验拦住。"""
    assert _validate_connector_key("qmt.big.direct", "") == ""
