"""桥接连接失败诊断：打印 runtime 选择、异常原始形态、init_error 原值、子进程 stderr。

用途：定位「桥接子进程握手失败：None」这类失真报错的真实来源。
运行：python diag_bridge.py [client_path] [account_id]
"""
import logging
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.DEBUG,
                    format="%(levelname)s %(name)s %(message)s")

from xtquant_client.bridge_client import BridgeAdapter  # noqa: E402
from xtquant_client.runtime import select_runtime  # noqa: E402

CP = sys.argv[1] if len(sys.argv) > 1 else r"P:/stock/gd_qmt/userdata_mini"
ACC = sys.argv[2] if len(sys.argv) > 2 else ""

print("=" * 70)
print("client_path:", CP, "exists:", os.path.isdir(CP))
print("account_id:", repr(ACC), "(空=行情模式)")

a = BridgeAdapter(CP, ACC, "STOCK", broker_id="gf")
try:
    site = a._xtquant_site()
    print("xtquant_site:", site)
except Exception as exc:  # noqa: BLE001
    print("xtquant_site FAILED:", type(exc).__name__, exc)
    site = None

if site:
    try:
        rt = select_runtime(site, a.min_version)
        print("select_runtime:", rt)
    except Exception as exc:  # noqa: BLE001
        print("select_runtime FAILED:", type(exc).__name__, exc)

print("-" * 70)
print(">>> start()")
try:
    a.start()
    print("START OK, is_connected =", a.is_connected())
except BaseException as exc:  # noqa: BLE001
    print("EXC_TYPE :", type(exc).__name__)
    print("EXC_ARGS :", repr(exc.args))
    print("EXC_STR  :", repr(str(exc)))
    print("TRACEBACK:")
    traceback.print_exc()

print("-" * 70)
print("_init_error  :", repr(getattr(a, "_init_error", "<none>")))
try:
    print("_stderr_buf  :", repr(a._stderr_buf))
    print("_stderr_tail :", repr(a._stderr_tail(30)))
except Exception as exc:  # noqa: BLE001
    print("stderr read failed:", exc)
print("proc         :", a._proc, "rc =",
      (a._proc.poll() if a._proc else "N/A"))
print("=" * 70)
try:
    a.close()
except Exception:  # noqa: BLE001
    pass
