"""一次性 xtquant 交易连接探针（诊断工具，不进产品链路）。

用途：判定「WaitingFreeWriter instances exceed maximum limit」到底是
① 券商侧授权/客户端问题（任何进程都连不上），还是
② qmt_work 反复 `adapter.start()` 泄漏 XtQuantTrader 实例导致的**自伤**
   （干净进程第一次就能连上）。

两者表现完全一样（都是 `交易连接异常：WaitingFreeWriter...`），但处置相反：
① 只能找券商；② 修掉泄漏即可。所以必须先分清。

用法：
    python scripts/probe_xtquant_once.py --client-path P:\\stock\\gd_qmt\\userdata
          [--session 100] [--python backend/.venv/Scripts/python.exe]

只做**一次**连接尝试，不动任何持久化配置，打印 rc/connect rc 与异常原文。
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-path", required=True)
    ap.add_argument("--session", type=int, default=100)
    ap.add_argument("--mode", default="full", choices=["full", "mini", "auto"])
    args = ap.parse_args()

    cp = args.client_path
    sp = os.path.join(os.path.dirname(os.path.dirname(cp)), "bin.x64", "Lib", "site-packages")
    # 客户端根目录：client_path 可能是 <root>/userdata 或 <root>/userdata_mini
    root = os.path.dirname(cp) if os.path.basename(cp).lower().startswith("userdata") else cp
    sp2 = os.path.join(root, "bin.x64", "Lib", "site-packages")
    for cand in (sp2, sp):
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    print(f"[env] python={sys.version.split()[0]} site-packages={sp2 if os.path.isdir(sp2) else sp}")

    t0 = time.time()
    # 与产品同一兼容口径（xtquant_client/xtp/_common.py::_load_trader_api）：
    # 账户类在旧版客户端里藏在 xtquant.xttype，不在 xttrader/xt_trader。
    try:
        import xtquant.xt_trader as _acc_mod  # type: ignore
        from xtquant.xt_trader import XtQuantTrader  # type: ignore
    except ImportError:
        import xtquant.xttype as _acc_mod  # type: ignore
        from xtquant.xttrader import XtQuantTrader  # type: ignore
    StockAccount = getattr(_acc_mod, "StockAccount", None)
    if StockAccount is None:
        print("[FATAL] xtquant 缺少 StockAccount（客户端 SDK 不完整）")
        return 2
    print(f"[env] trader={XtQuantTrader.__module__} acc={_acc_mod.__name__}")

    acc_id = os.environ.get("QMT_PROBE_ACCOUNT", "")
    trader = None
    try:
        trader = XtQuantTrader(cp, args.session)
        rc = trader.start()
        print(f"[start] rc={rc!r} ({type(rc).__name__})  {time.time() - t0:.2f}s")
        crc = trader.connect()
        print(f"[connect] rc={crc!r}")
        if crc == 0:
            print("[RESULT] 连接成功 —— WaitingFreeWriter 是**重试泄漏**导致的自伤，可修")
            if acc_id:
                acc = StockAccount(acc_id, "STOCK")
                try:
                    asset = trader.query_stock_asset(acc)
                    print(f"[asset] {asset}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[asset] 查询失败：{exc}")
            return 0
        print(f"[RESULT] connect 失败 rc={crc} —— 券商侧/客户端侧问题（非重试自伤）")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[EXCEPTION] {type(exc).__name__}: {exc}")
        msg = str(exc)
        if "WaitingFreeWriter" in msg or "maximum limit" in msg:
            print("[RESULT] 干净进程首次即撞 WaitingFreeWriter ⇒ 该资源池不在本进程，"
                  "是**客户端侧/全局**限制，重试无用（只会更糟）")
        return 3
    finally:
        if trader is not None:
            try:
                trader.stop()
            except Exception:  # noqa: BLE001
                pass
            print("[cleanup] trader.stop() 已调用")


if __name__ == "__main__":
    raise SystemExit(main())
