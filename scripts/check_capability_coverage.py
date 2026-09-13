"""能力可达性核对（方案 §10 阶段 3 验收项）。

遍历后端 `/capabilities` 的每一条，确认它在前端有真实入口；没有入口的必须落在
**显式豁免清单**里（并在文档中标注「仅 API/MCP 可用」），否则就是「后端有能力、
界面上找不到」的生产不可达缺口 —— 这正是旧前端 AuditReconHub / AutomationHub
未注册那类问题的根因，需要机械化门禁而不是靠人记。

匹配方式：
- capability.path 去掉 `/api/v1` 前缀（前端基址就是这个前缀）；
- 路径参数 `{code}` 在前端通常是模板字符串，故按「非引号非空白」通配匹配；
- 在 frontend-next/src 下全部 .ts/.tsx 中搜索。

用法（须用后端托管解释器，导入 backend 需要 fastapi 等依赖）：

    backend/runtimes/cp311/python.exe scripts/check_capability_coverage.py
    backend/runtimes/cp311/python.exe scripts/check_capability_coverage.py --json out.json

退出码：0 = 无未标注缺口；1 = 存在未标注缺口（CI 可据此失败）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend-next"

API_PREFIX = "/api/v1"

# ---------------------------------------------------------------------------
# 显式豁免：按方案 §1.1 决策「前端移除、后端保留」的域。
# 这些端点**有意**不设前端页面，经 MCP 工具 / REST API 面向 Agent 与脚本，
# 必须在文档中标注「仅 API/MCP 可用」，否则使用者会困惑。
# 新增豁免必须在此写明理由 —— 这是门禁的意义所在，不允许静默忽略。
# ---------------------------------------------------------------------------
EXEMPT: dict[str, str] = {
    "backtest": "方案 §1.1：策略回测前端已移除，后端保留（仅 API/MCP）",
    "research": "方案 §1.1：因子研究前端已移除，后端保留（仅 API/MCP）",
    "factors": "方案 §1.1：因子研究前端已移除，后端保留（仅 API/MCP）",
    "strategy-market": "方案 §1.1：策略市场不设前端页面（仅 API/MCP）",
    "strategy": "方案 §1.1：策略运行后端保留，前端不建域（仅 API/MCP）",
    "position": "由 /trade/positions 与账户域页面承载，独立端点仅内部使用",
    "ws": "WebSocket 端点，非页面入口",
    "health": "基础设施端点，前端经 /live 探活而非直接调用",
    "metrics": "基础设施端点（Prometheus）",
    "capabilities": "自描述端点，前端不消费",
    # —— 以下为「前端不消费但并非缺陷」的端点，逐条核实后登记 ——
    "sync": "前端行情订阅走 WebSocket action（ws.ts 的 subscribe/unsubscribe），"
            "REST 订阅端点面向脚本/Agent",
    "signal": "**入站** webhook 由外部系统调用，前端不消费"
              "（出站 webhook 另由 /webhooks 页面承载；/signal/submit|mode|confirm 前端已覆盖）",
    "quote-bus": "行情总线统计，属运维诊断数据；如需观察应并入系统状态页，不单独立页",
}

# ---------------------------------------------------------------------------
# 未覆盖域的处置建议（让报告可行动，而不只是抛一堆路径）。
# 分三类：add-ui = 建议补前端入口；api-only = 建议标注仅 API/MCP；
#         decide = 涉及产品形态，需人工决策。
# ---------------------------------------------------------------------------
GAP_HINTS: dict[str, tuple[str, str]] = {
    "paper": ("add-ui", "账户域补「模拟盘」页：后端 PaperEngine 已完整，而前端 Signals 页"
                        "可切 paper 模式却无处查看模拟成交/持仓 —— 体验断裂"),
    "strategies": ("decide", "策略**运行**容器（非回测）：方案 §1.1 只决定移除回测/因子前端，"
                             "策略运行未定。补自动化域入口 or 明确标注仅 API/MCP"),
    "market": ("decide", "多为增强能力（导出/爬取/分析脚本/组合聚合/资金流快照回放）。"
                         "指标类已被 klinecharts 本地实现覆盖，导出类建议按需补"),
    "data": ("decide", "数据面管理端点（providers/health/chain）。与 market/providers 能力重叠，"
                       "建议核实后二选一，避免双份维护"),
    "datahub": ("decide", "数据中枢策略端点（policies），前端无对应页面"),
    "brokers": ("add-ui", "批量删除与单连接健康检查：连接管理页建议补齐（小改动）"),
    "notifications": ("add-ui", "通知批量删除：与 alerts/webhooks 的批量删除对齐（小改动）"),
    "quote-bus": ("api-only", "行情总线统计，属运维诊断（可并入系统状态页或标注仅 API）"),
    "sync": ("api-only", "前端行情订阅走 WebSocket action，REST 订阅端点面向脚本/Agent"),
    "signal": ("api-only", "入站 webhook 由**外部系统**回调，前端不消费（出站 webhook 另有页面）"),
    "target-portfolio": ("add-ui", "目标持仓差量同步端点；目标持仓页当前为 placeholder，"
                                   "补齐页面即覆盖"),
    "trade": ("add-ui", "交易域剩余端点，补进手动交易/委托页"),
}


def load_capabilities() -> list[dict]:
    """从后端能力注册表构建全量 capability（不依赖运行中的服务）。"""
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)          # 部分 routes 模块依赖相对资源路径
    from app.capabilities import build_capabilities  # noqa: PLC0415

    return [c.__dict__ if hasattr(c, "__dict__") else dict(c)
            for c in build_capabilities()]


def load_frontend_sources() -> list[tuple[str, str]]:
    """收集前端源码：(相对路径, 文本)。"""
    out: list[tuple[str, str]] = []
    if not FRONTEND.is_dir():
        return out
    for p in sorted(FRONTEND.glob("src/**/*")):
        if p.suffix in (".ts", ".tsx") and p.is_file():
            try:
                out.append((str(p.relative_to(ROOT)).replace("\\", "/"),
                            p.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
    return out


def cap_regex(path: str) -> re.Pattern:
    """capability 路径 → 前端代码中的匹配正则（{param} 按模板字符串通配）。"""
    p = path
    if p.startswith(API_PREFIX):
        p = p[len(API_PREFIX):]
    if not p:
        p = "/"
    segments = re.split(r"(\{[^}]+\})", p)
    body = ""
    for seg in segments:
        if seg.startswith("{") and seg.endswith("}"):
            body += r"[^\"'`\s]*"
        else:
            body += re.escape(seg)
    # 尾部边界：避免 /market/kline 命中 /market/kline-other
    return re.compile(body + r"(?=[\"'`\s/?&)|,;]|\Z)")


def main() -> int:
    ap = argparse.ArgumentParser(description="能力可达性核对")
    ap.add_argument("--json", help="把结果写入 JSON 文件")
    ap.add_argument("--verbose", action="store_true", help="打印每条未覆盖明细")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    caps = load_capabilities()
    sources = load_frontend_sources()
    print(f"后端 capabilities : {len(caps)}")
    print(f"前端源码文件      : {len(sources)}")

    covered: list[dict] = []
    exempted: list[dict] = []
    gaps: list[dict] = []

    for c in caps:
        path = str(c.get("path") or "")
        category = str(c.get("category") or _domain_of(path))
        rx = cap_regex(path)
        hit = next((f for f, text in sources if rx.search(text)), None)
        row = {
            "path": path,
            "method": str(c.get("method") or ""),
            "category": category,
            "agent_visible": bool(c.get("agent_visible")),
        }
        if hit:
            row["frontend"] = hit
            covered.append(row)
        elif category in EXEMPT:
            row["exempt_reason"] = EXEMPT[category]
            exempted.append(row)
        else:
            gaps.append(row)

    by_domain: dict[str, list[dict]] = defaultdict(list)
    for g in gaps:
        by_domain[g["category"]].append(g)

    print(f"\n[✓] 前端有入口     : {len(covered)}")
    print(f"[~] 豁免（仅API/MCP）: {len(exempted)}")
    print(f"[✗] 未覆盖且未豁免 : {len(gaps)}")

    if exempted:
        print("\n── 豁免明细（须在文档标注「仅 API/MCP 可用」）──")
        by_dom: dict[str, int] = defaultdict(int)
        for e in exempted:
            by_dom[e["category"]] += 1
        for dom, n in sorted(by_dom.items()):
            print(f"  {dom:<18} {n:>3} 条  {EXEMPT[dom]}")

    if gaps:
        print("\n── 未覆盖且未豁免（按处置建议分类）──")
        grouped: dict[str, list[dict]] = defaultdict(list)
        for g in gaps:
            kind, hint = GAP_HINTS.get(g["category"], ("decide", "未分类，需人工确认归属"))
            g["suggestion"] = kind
            g["hint"] = hint
            grouped[kind].append(g)

        label = {"add-ui": "建议补前端入口", "api-only": "建议标注仅 API/MCP",
                 "decide": "需人工决策"}
        for kind in ("add-ui", "api-only", "decide"):
            items = grouped.get(kind, [])
            if not items:
                continue
            print(f"\n  【{label[kind]}】{len(items)} 条")
            by_dom: dict[str, list[dict]] = defaultdict(list)
            for it in items:
                by_dom[it["category"]].append(it)
            for dom in sorted(by_dom):
                hint = GAP_HINTS.get(dom, ("", "未分类"))[1]
                print(f"    [{dom}] {len(by_dom[dom])} 条 — {hint}")
                if args.verbose:
                    for it in by_dom[dom]:
                        print(f"        {it['method']:<6} {it['path']}")
        print("\n  → 处理：补前端入口，或在本脚本 EXEMPT 中登记豁免并写明理由。")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"total": len(caps), "covered": covered,
             "exempted": exempted, "gaps": gaps},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {args.json}")

    return 1 if gaps else 0


def _domain_of(path: str) -> str:
    m = re.match(r"^/api/v1/([^/]+)", path)
    return m.group(1) if m else "unknown"


if __name__ == "__main__":
    sys.exit(main())
