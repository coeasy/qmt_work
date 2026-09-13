"""WS 事件消费门禁（方案 §10 阶段 3）。

目标不是要求每个后台事件都有独立页面，而是把三件事分开核对：

1. 后端实际发出的事件词汇来自 `backend/tests/contracts/ws_events.json`；
2. 前端 `services/wsEvents.ts` 有逐项消费策略登记，避免新增事件静默漏掉；
3. `SystemLog.tsx` 注册全量 `quoteSocket.onMessage`，因此任意事件至少可观测。

领域级消费者目前只有行情（quotes/snapshot）与成交（deal/order）；其余引擎事件
先由系统日志兜底，脚本明确显示为 `system-log`，不把「可观测」伪装成「业务已处理」。
后续为策略/条件单/告警添加专用页面时，只需把 registry 的 handler 改成专用类型。

用法：
    backend/runtimes/cp311/python.exe scripts/check_ws_consumption.py
    ... scripts/check_ws_consumption.py --json output/ws_consumption.json

退出码：0 = 基线、前端登记、全量日志兜底均一致；1 = 有遗漏。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "backend/tests/contracts/ws_events.json"
REGISTRY = ROOT / "frontend-next/src/services/wsEvents.ts"
SYSTEM_LOG = ROOT / "frontend-next/src/domains/system/SystemLog.tsx"


def backend_events() -> set[str]:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    out: set[str] = set()
    for channel, types in data.items():
        out.add(channel)
        out.update(str(t) for t in (types or []))
    return out


def frontend_registry() -> dict[str, str]:
    src = REGISTRY.read_text(encoding="utf-8")
    # 只解析 Record 对象中真正的 "event": "handler" 行；避免把注释/函数误算进去。
    return dict(re.findall(r'^\s*"([^"]+)"\s*:\s*"([^"]+)"\s*,?\s*$',
                           src, flags=re.MULTILINE))


def main() -> int:
    ap = argparse.ArgumentParser(description="核对后端 WS 事件与前端消费登记")
    ap.add_argument("--json", help="写出 JSON 报告")
    args = ap.parse_args()

    back = backend_events()
    reg = frontend_registry()
    missing = sorted(back - set(reg))
    stale = sorted(set(reg) - back)
    log_src = SYSTEM_LOG.read_text(encoding="utf-8")
    generic_ok = "quoteSocket.onMessage" in log_src

    domain = sorted(k for k, v in reg.items() if v != "system-log" and k in back)
    fallback = sorted(k for k in back if reg.get(k) == "system-log")

    print(f"后端 WS 事件词汇 : {len(back)}")
    print(f"前端策略登记     : {len(reg)}")
    print(f"领域级消费者     : {len(domain)} ({', '.join(domain) or '无'})")
    print(f"SystemLog 兜底    : {len(fallback)}")
    print(f"未登记事件       : {len(missing)}")
    print(f"过期登记         : {len(stale)}")
    print(f"全量日志监听     : {'✓' if generic_ok else '✗'}")

    if missing:
        print("\n[✗] 后端新增但前端未登记：")
        for x in missing:
            print(f"  - {x}")
    if stale:
        print("\n[✗] 前端登记但后端基线不存在（可能是拼写错误或基线未刷新）：")
        for x in stale:
            print(f"  - {x}")
    if not generic_ok:
        print("\n[✗] SystemLog 未发现 quoteSocket.onMessage 全量监听")

    result = {
        "backend_events": sorted(back),
        "frontend_registry": reg,
        "domain_consumers": domain,
        "system_log_fallback": fallback,
        "missing": missing,
        "stale": stale,
        "generic_observer": generic_ok,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n已写出 {args.json}")

    if missing or stale or not generic_ok:
        print("\nWS CONSUMPTION FAILED")
        return 1
    print("\nWS CONSUMPTION OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
