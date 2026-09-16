"""护栏：Win32 句柄签名必须声明在**会被复用**的 CDLL 对象上。

背景（R8 实测，代价是一整轮误诊）：``ctypes.WinDLL("user32")`` **每次调用都返回新的
CDLL 对象**，而 ``argtypes`` 只是挂在对象上的普通属性、**不共享**：

    >>> a = ctypes.WinDLL("user32"); a.SetWindowPos.argtypes = [...]
    >>> b = ctypes.WinDLL("user32")
    >>> a is b, b.SetWindowPos.argtypes
    (False, None)

所以「先建新对象、再判一个模块级布尔量提前 return」的写法，只在**第一次**调用真正生效，
之后每次都退化成「未声明签名」：

- ``SetWindowPos(hwnd, HWND_TOPMOST, …)`` 的 ``HWND_TOPMOST = -1`` 会按 C ``int``
  传成 ``0xFFFFFFFF``（而非 ``0xFFFFFFFFFFFFFFFF``）→ 调用返回 0、
  窗口 ``WS_EX_TOPMOST`` 恒为 False；
- 64 位句柄被截断（``CreateCompatibleDC`` 实测返回 ``0xfffffffff201149c``）→
  ``OverflowError: int too long to convert``。

这类故障表现为「同一份脚本有时成功有时失败」，极难定位（本项目为此误诊过两轮）。
本护栏把不变量钉死：**取 DLL 的入口函数必须幂等，且返回对象上的签名仍然在。**
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="仅 Windows 提供 user32/gdi32")


def _load(name: str):
    """从仓库根 ``scripts/`` 载入脚本模块（与 client_start_test 的用法一致）。"""
    sys.path.insert(0, str(SCRIPTS))
    try:
        return __import__(name)
    finally:
        sys.path.remove(str(SCRIPTS))


def test_client_start_test_win32_is_cached_and_keeps_signatures():
    mod = _load("client_start_test")
    a, b = mod._win32(), mod._win32()
    assert a is b, "每次调用都新建 CDLL —— argtypes 会丢，签名静默失效"
    for fn in ("SetWindowPos", "BringWindowToTop", "GetWindowRect", "ShowWindow"):
        assert getattr(a, fn).argtypes is not None, f"user32.{fn} 未声明 argtypes"
    assert a.SetWindowPos.restype is not None


def test_capture_screen_dlls_are_cached_and_keep_signatures():
    mod = _load("capture_screen")
    u1, g1 = mod._dlls()
    u2, g2 = mod._dlls()
    assert u1 is u2 and g1 is g2, "每次调用都新建 CDLL —— argtypes 会丢，句柄会被截断"
    assert u2.PrintWindow.argtypes is not None
    assert u2.GetWindowRect.restype is not None
    assert g2.CreateCompatibleDC.restype is not None
    assert g2.GetDIBits.argtypes is not None
