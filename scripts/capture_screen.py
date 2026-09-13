"""Windows 屏幕截图（纯标准库：ctypes + GDI + 手写 PNG）。

用途：桌面客户端本地验证时截图留证，无需 PIL/pywin32。
用法：python capture_screen.py <输出.png> [宽] [高]
"""
import ctypes
import struct
import sys
import zlib
from ctypes import wintypes

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0


def _png(path: str, width: int, height: int, rgb_rows: list[bytes]) -> None:
    raw = b"".join(b"\x00" + row for row in rgb_rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        fh.write(chunk(b"IEND", b""))


# 最近一次抓屏的统计信息（供调用方判定「是否真的渲染了内容」）：
#   distinct_colors = 采样到的不同 RGB 值个数。纯色画面（白屏/黑屏）该值极小，
#   可据此识别「窗口在、页面没渲染出来」这类故障。
LAST_STATS: dict = {"distinct_colors": 0, "width": 0, "height": 0}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_int),
                ("biHeight", ctypes.c_int), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_int),
                ("biYPelsPerMeter", ctypes.c_int), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]

PW_RENDERFULLCONTENT = 0x00000002  # 让 Chromium/Electron 合成窗口把内容真正画出来


_DECLARED = False


def _dlls():
    """取 user32/gdi32，并**一次性声明所有句柄相关签名**（模块内幂等）。

    为什么必须声明：ctypes 默认把返回值当 C int、把入参当 C int，而 64 位 Windows 的
    HDC/HBITMAP/HWND 句柄**可能带高位**（实测 `CreateCompatibleDC` 返回
    0xfffffffff201149c）。不声明就会：
      ① 返回值被截断成负数再传给后续调用；
      ② 把大句柄当入参传给未声明签名的函数时抛
         `argument 1: OverflowError: int too long to convert`。
    这类故障与句柄的具体数值有关，表现为「同一个脚本有时成功有时失败」的抖动——
    本项目实测开发态成功、打包客户端失败，就是撞在这个点上。
    """
    global _DECLARED
    user32, gdi32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32")
    if _DECLARED:
        return user32, gdi32
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                             wintypes.DWORD]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER),
                                wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    _DECLARED = True
    return user32, gdi32


def _rows_from_dc(gdi32, mem, bmp, width: int, height: int) -> list:
    """从内存 DC 取出自上而下的 RGB 行数据。"""
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = width, -height  # 负值 = 自上而下
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, BI_RGB
    stride = width * 4
    buf = ctypes.create_string_buffer(stride * height)
    gdi32.GetDIBits(mem, bmp, 0, height, buf, ctypes.byref(bi), DIB_RGB_COLORS)
    data = bytes(buf)
    return [bytes(b for i in range(0, stride, 4)
                  for b in (data[y * stride + i + 2], data[y * stride + i + 1],
                            data[y * stride + i]))
            for y in range(height)]


def _save(path: str, width: int, height: int, rows) -> tuple[int, int]:
    _png(path, width, height, rows)
    # 采样统计（每隔 16 像素取一点）：不同颜色数太少即为纯色画面 → 页面未渲染
    sampled = {rows[y][i:i + 3] for y in range(0, height, 16) for i in range(0, width * 3, 48)}
    LAST_STATS.update(distinct_colors=len(sampled), width=width, height=height)
    return width, height


def capture(path: str, width: int = 0, height: int = 0,
            x: int = 0, y: int = 0) -> tuple[int, int]:
    """抓屏保存为 PNG。

    (x, y) 为屏幕坐标原点偏移，(width, height) 为截取尺寸；不传尺寸时取整屏。
    注意：这是「屏幕上看得见什么就抓什么」——目标窗口被遮挡时会抓到遮挡者。
    要稳定抓某个窗口请用 capture_window()。
    """
    user32, gdi32 = _dlls()
    if not width:
        width = user32.GetSystemMetrics(0)
    if not height:
        height = user32.GetSystemMetrics(1)

    screen = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, width, height)
    gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, width, height, screen, int(x), int(y), SRCCOPY)

    rows = _rows_from_dc(gdi32, mem, bmp, width, height)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, screen)
    return _save(path, width, height, rows)


def capture_window(path: str, hwnd: int) -> tuple[int, int]:
    """抓取指定窗口自身的画面（不受遮挡影响）。

    为什么不能直接用整屏 BitBlt：屏幕 DC 只有「当前屏幕上可见的像素」，窗口被别的
    窗口挡住时截到的是遮挡者——本地验证时就曾把工具窗口截成了客户端截图。PrintWindow
    让目标窗口把自身内容画进我们提供的 DC，因此与前后层无关。
    """
    user32, gdi32 = _dlls()
    rect = wintypes.RECT()
    handle = wintypes.HWND(hwnd)
    if not user32.GetWindowRect(handle, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect 失败 hwnd={hwnd} err={ctypes.get_last_error()}")
    width = max(1, rect.right - rect.left)
    height = max(1, rect.bottom - rect.top)

    screen = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, width, height)
    gdi32.SelectObject(mem, bmp)
    # 打印失败（少数窗口拒绝 PrintWindow）时退回屏幕搬运，至少能出图
    if not user32.PrintWindow(handle, mem, PW_RENDERFULLCONTENT):
        gdi32.BitBlt(mem, 0, 0, width, height, screen, rect.left, rect.top, SRCCOPY)

    rows = _rows_from_dc(gdi32, mem, bmp, width, height)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, screen)
    return _save(path, width, height, rows)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "screen.png"
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    h = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    x = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    y = int(sys.argv[5]) if len(sys.argv) > 5 else 0
    size = capture(out, w, h, x, y)
    print(f"saved {out} {size[0]}x{size[1]} (origin {x},{y})")
