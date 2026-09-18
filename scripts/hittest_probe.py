"""判定「拖动区有没有生效」—— 直接问窗口，绕开遮挡。

## 为什么不能用 `drag_probe.py` 的结论

`drag_probe.py` 会先用 `WindowFromPoint` 确认「鼠标下就是本窗口」再拖，被遮挡时
直接返回 `valid: false`（这是**正确的**保守设计，避免把「拖了别的窗口」当成「没动」）。
但本机 IDE 常盖在客户端上 ⇒ 它只能报「被遮挡」，拿不到结论。

## 本探针的关键认识

`WM_NCHITTEST` 是**直接 SendMessage 给目标窗口**的，Windows 不会替你判断遮挡 ⇒
即使窗口被完全盖住，返回值依然真实反映「这个点算不算拖动区」：

- **`2` = HTCAPTION** ⇒ Chromium 已把 `-webkit-app-region: drag` 报给窗口，可拖；
- **`1` = HTCLIENT**  ⇒ 没生效（R11 修前的实测值恒为 1）。

所以「拖动区是否生效」这一步**不依赖遮挡**；只有「真的拖一下窗口会不会动」才依赖，
而后者只是前者的推论（`HTCLIENT` 永远拖不动）。

## ⚠️⚠️ 唯一的假阴性来源：必须等「应用页」

加载页（`title` 含「正在启动」）**没有 `.titlebar`**，此时标题栏位置返回
`HTCLIENT(1)` 是**正确**的。首版探针没等应用页就拿这 4 个 1 当结论 ⇒ 假阴性。
本版：① 先等 `window-ready.txt`（只在应用 URL 上写）+ 标题不含「正在启动」；
② 超时则明确报 `INCONCLUSIVE`，**不打印结论性读数**。

## 另一处必须堵的坑：隔离 profile

首版没传 `--user-data-dir`，于是走默认 userData（`%APPDATA%/qmt_work`）。
实测那次窗口**停在加载页**、且默认 userData 下只有空 `logs/`、无任何失败痕迹。
`client_start_test.py` 一直传 `--user-data-dir=output/client_test/profile` 并稳定进应用页，
故本版对齐它（`output/hittest/profile`），把「环境差异」这个变量消掉。

用法：backend/runtimes/cp311/python.exe output/hittest_probe.py
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import client_start_test as C  # noqa: E402

u = ctypes.windll.user32
for _fn in ("WindowFromPoint", "GetAncestor", "GetForegroundWindow"):
    getattr(u, _fn).restype = ctypes.c_void_p
u.SendMessageW.restype = ctypes.c_long
u.GetWindowLongW.restype = ctypes.c_long
LOADING_MARK = "正在启动"
APP_TITLE = "qmt_work · 量化交易终端"
OUT_DIR = ROOT / "output" / "hittest"
PROFILE = OUT_DIR / "profile"

HT_NAME = {0: "HTNOWHERE", 1: "HTCLIENT", 2: "HTCAPTION", 3: "HTSYSMENU",
           8: "HTMINBUTTON", 9: "HTMAXBUTTON", 10: "HTLEFT", 11: "HTRIGHT",
           12: "HTTOP", 13: "HTTOPLEFT", 14: "HTTOPRIGHT", 15: "HTBOTTOM",
           17: "HTCLOSE"}


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def rect_of(hwnd) -> tuple[int, int, int, int]:
    r = RECT()
    u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def title_of(hwnd) -> str:
    n = u.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
    buf = ctypes.create_unicode_buffer(n + 2)
    u.GetWindowTextW(ctypes.c_void_p(hwnd), buf, n + 2)
    return buf.value


def hittest(hwnd, x, y) -> int:
    """WM_NCHITTEST 的 lParam 是**屏幕坐标**（低字 x / 高字 y）。"""
    lp = ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)
    return u.SendMessageW(ctypes.c_void_p(hwnd), 0x0084, 0, lp)


def runs(seq: list[tuple[int, int]]) -> list[dict]:
    """把 [(x, ht)] 压成 [(x_from, x_to, ht)]，便于看「哪一段是可拖区」。"""
    out: list[dict] = []
    for x, ht in seq:
        if out and out[-1]["ht"] == ht:
            out[-1]["x_to"] = x
        else:
            out.append({"x_from": x, "x_to": x, "ht": ht})
    return out


def fresh(path: Path, since: float) -> bool:
    try:
        return path.exists() and path.stat().st_mtime >= since
    except OSError:
        return False


def tail(path: Path, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                         .splitlines()[-lines:])
    except Exception:  # noqa: BLE001
        return "(无内容)"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    C.kill_image("qmt_work.exe")
    time.sleep(0.8)
    started = time.time()
    env = C.clean_env({"QMT_CLIENT_TEST_MODE": "1", "QMT_CLIENT_SAFE_MODE": "1"})
    argv = [str(C.CLIENT_EXE), f"--user-data-dir={PROFILE}"]
    proc = subprocess.Popen(argv, cwd=str(C.CLIENT_EXE.parent), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out: dict = {"points": [], "scan_titlebar": [], "scan_vertical": [], "steps": []}
    try:
        # ---------- ① 等主窗口 ----------
        hwnd, t0 = None, time.time()
        while time.time() - t0 < 60:
            if proc.poll() is not None:
                out["error"] = f"客户端提前退出 rc={proc.returncode}"
                break
            w = C.pick_main_window(proc.pid)
            if w:
                hwnd = w["hwnd"]
                out["window_appear_after"] = round(time.time() - started, 2)
                break
            time.sleep(0.5)
        if not hwnd:
            out.setdefault("error", "未找到主窗口")
            out["verdict"] = "INCONCLUSIVE"
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 1

        # ---------- ② 等应用页（这是唯一的假阴性来源） ----------
        marker = PROFILE / "window-ready.txt"
        deadline, state = time.time() + 240, "timeout"
        while time.time() < deadline:
            title = title_of(hwnd)
            if LOADING_MARK not in title and (APP_TITLE in title or fresh(marker, started)):
                state = "app"
                break
            if proc.poll() is not None:
                state = f"exited rc={proc.returncode}"
                break
            time.sleep(1)
        out["app_page_state"] = state
        out["app_page_after"] = round(time.time() - started, 2)
        out["title"] = title_of(hwnd)
        out["window_ready_txt"] = fresh(marker, started)
        if state != "app":
            # 停在加载页 ⇒ 标题栏本就没有 .titlebar，此时 HTCLIENT 是**正确**的，
            # 打印它会被当成「问题 6 未修」，所以这里只给诊断、不给结论。
            out["verdict"] = "INCONCLUSIVE（未进入应用页，本次读数无效）"
            out["diag"] = {
                "electron_alive": proc.poll() is None,
                "profile": str(PROFILE),
                "startup-error.log": (tail(PROFILE / "startup-error.log", 25)
                                      if (PROFILE / "startup-error.log").exists()
                                      else "(未产生)"),
                "port.txt": (PROFILE / "port.txt").read_text().strip()
                if (PROFILE / "port.txt").exists() else "(未产生)",
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 2

        time.sleep(1.5)
        left, top, w, h = rect_of(hwnd)
        out["rect"] = [left, top, w, h]
        # 仅作参考：标题栏那点被谁盖着（不影响 WM_NCHITTEST 的结论）
        under = u.WindowFromPoint(POINT(left + 90, top + 14))
        root = int(u.GetAncestor(ctypes.c_void_p(under), 2) or under or 0) if under else 0
        out["window_under_titlebar_is_self"] = (root == hwnd)
        if not out["window_under_titlebar_is_self"]:
            n = u.GetWindowTextLengthW(ctypes.c_void_p(root)) if root else 0
            buf = ctypes.create_unicode_buffer(n + 2)
            if root:
                u.GetWindowTextW(ctypes.c_void_p(root), buf, n + 2)
            out["note"] = (f"标题栏被「{buf.value}」遮挡 —— 真实拖拽会打到它，"
                           f"故只报 WM_NCHITTEST（它直接发给本窗口，不受遮挡影响）")

        # ---------- ③ 标题栏横扫：标出可拖区边界 ----------
        y_title = top + 14
        seq = [(x, hittest(hwnd, x, y_title))
               for x in range(left + 3, left + w - 2, max(8, w // 60))]
        out["scan_titlebar"] = [dict(r, ht_name=HT_NAME.get(r["ht"], str(r["ht"])))
                                for r in runs(seq)]
        out["scan_titlebar_y"] = y_title
        # ---------- ④ 纵向扫描：标出拖动区高度 ----------
        x_mid = left + w // 2
        vseq = [(y, hittest(hwnd, x_mid, y)) for y in range(top + 2, top + 62, 4)]
        out["scan_vertical"] = [dict(r, ht_name=HT_NAME.get(r["ht"], str(r["ht"])))
                                for r in runs(vseq)]
        out["scan_vertical_x"] = x_mid
        # ---------- ⑤ 定点读数（与首版口径对齐，便于对比） ----------
        for label, x, y in [
            ("标题栏-品牌区", left + 90, top + 14),
            ("标题栏-中段", left + w // 2, top + 14),
            ("标题栏-右端(按钮区)", left + w - 30, top + 14),
            ("内容区(应不可拖)", left + w // 2, top + h // 2),
        ]:
            ht = hittest(hwnd, x, y)
            out["points"].append({
                "where": label, "screen": [x, y], "nchittest": ht,
                "meaning": f"{HT_NAME.get(ht, '?')}",
            })
        captions = [r for r in out["scan_titlebar"] if r["ht"] == 2]
        if captions and out["window_under_titlebar_is_self"]:
            r0 = rect_of(hwnd)
            u.SetCursorPos(left + 90, top + 14)
            time.sleep(0.25)
            u.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.1)
            for i in range(1, 15):
                u.SetCursorPos(left + 90 + 150 * i // 14, top + 14 + 70 * i // 14)
                time.sleep(0.03)
            time.sleep(0.1)
            u.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(0.6)
            r1 = rect_of(hwnd)
            out["steps"].append({"rect_before": list(r0), "rect_after": list(r1),
                                 "delta": [r1[0] - r0[0], r1[1] - r0[1]]})
        # ---------- ⑥ 结论 ----------
        if captions:
            out["verdict"] = (f"PASS —— 标题栏有 {len(captions)} 段 HTCAPTION"
                              f"（共 {sum(r['x_to'] - r['x_from'] + 1 for r in captions)}px）"
                              f"，问题 6 已修")
        else:
            out["verdict"] = ("FAIL —— 应用页标题栏全为 HTCLIENT，"
                              "draggable region 仍未上报给窗口过程")
    finally:
        try:
            C.kill_image("qmt_work.exe")
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
