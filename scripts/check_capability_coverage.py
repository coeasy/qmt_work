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
    # —— reference：能力已由 MCP 覆盖，前端不建页 ——
    # 原前端 `services/api/system.ts::referenceApi` 是**死代码**（UI 无任何调用方），
    # 已于 R38 删除；这 4 个端点仍由 MCP 工具对外提供，故登记豁免而不是删端点。
    "reference": "MCP 工具 trading_calendar / sector_list / sector_stocks / financial_summary "
                 "覆盖同一能力，前端不设页（仅 API/MCP；原前端 referenceApi 为死代码已删除）",
}

# ---------------------------------------------------------------------------
# 按**精确路径**登记的豁免（V11 决策 2，2026-09-15）。
# 比 category 粒度更严：只豁免下列具体端点，同域未来新增的端点仍会被门禁抓出。
# 键 = "<METHOD> <path>"；METHOD 大写，path 与 capabilities 一致（含 /api/v1 前缀）。
# 处置口径：全部标注「仅 API/MCP 可用」。
# ---------------------------------------------------------------------------
EXEMPT_PATHS: dict[str, str] = {
    # —— data：与 market/providers 能力重叠，前端只保留一侧入口 ——
    # ★ R19 第 3 轮：原登记「GET /api/v1/data/providers」为豁免，理由是「前端保留的
    #   另一侧（market/providers）已承载」。实测**方向反了**：UI 真正接的是本端点
    #   （`SystemStatus.tsx::dataProviders()` 的数据源矩阵），而 market/providers
    #   从未被调用。故删除本豁免，并把 market/providers 登记为仅 API/MCP。
    #   （这条过期豁免是被新增的「豁免反腐烂」检查抓出来的。）
    "GET /api/v1/data/providers/health":
        "V11 决策：数据面管理端点（健康检查）与 market/providers 重叠（仅 API/MCP）",
    "POST /api/v1/data/chain":
        "V11 决策：数据源链覆盖为运维/脚本向操作，前端不设页（仅 API/MCP）",
    # —— datahub ——
    # ★ R19 第 3 轮：原登记「GET /api/v1/datahub/policies」为豁免（理由「运维/脚本向，
    #   前端不设页」）。实测已由 `SystemStatus.tsx::datahubPolicies()`（限流策略卡）
    #   承载，故删除该豁免 —— 留着它会让「仅 API/MCP」的声明变成谎话。
    # —— market：增强能力（指标类已由 klinecharts 本地实现覆盖）——
    "POST /api/v1/market/moneyflow/snapshot":
        "V11 决策：资金流快照回放为增强能力（仅 API/MCP）",
    "GET /api/v1/market/moneyflow/replay":
        "V11 决策：资金流快照回放为增强能力（仅 API/MCP）",
    "GET /api/v1/market/datasets/snapshots":
        "V11 决策：数据集快照为运维/脚本向（仅 API/MCP）",
    "POST /api/v1/market/kline/export":
        "V11 决策：K 线导出为批量/脚本向能力（仅 API/MCP）",
    "GET /api/v1/market/kline/export":
        "V11 决策：K 线导出结果读取（仅 API/MCP）",
    "POST /api/v1/market/kline/sync":
        "V11 决策：K 线批量同步为运维向（仅 API/MCP）",
    "POST /api/v1/market/crawl":
        "V11 决策：行情爬取为运维/脚本向（仅 API/MCP）",
    "GET /api/v1/market/indicators":
        "V11 决策：指标清单/计算已由 klinecharts 本地实现（chart.createIndicator）覆盖，"
        "后端端点供 MCP/脚本复用（仅 API/MCP）",
    "GET /api/v1/market/indicators/calc":
        "V11 决策：指标计算已由 klinecharts 本地实现覆盖（仅 API/MCP）",
    "GET /api/v1/market/chart-spec":
        "V11 决策：图表规格为 MCP/脚本向（仅 API/MCP）",
    "POST /api/v1/market/portfolio/aggregate":
        "V11 决策：组合聚合为分析脚本向（仅 API/MCP）",
    "POST /api/v1/market/export":
        "V11 决策：通用导出为批量/脚本向（仅 API/MCP）",
    "GET /api/v1/market/analysis/scripts":
        "V11 决策：分析脚本清单为脚本向（仅 API/MCP）",
    "POST /api/v1/market/analysis/run":
        "V11 决策：分析脚本执行为脚本向（仅 API/MCP）",
    # —— strategies：策略运行容器（前端不建域，与回测/因子同口径）——
    "POST /api/v1/strategies/generate":
        "V11 决策：策略**运行**容器前端不建域，后端保留（仅 API/MCP）",
    "POST /api/v1/strategies/save":
        "V11 决策：策略保存属运行容器，前端不建域（仅 API/MCP）",
    "GET /api/v1/strategies/run":
        "V11 决策：策略运行列表，前端不建域（仅 API/MCP）",
    "POST /api/v1/strategies/run":
        "V11 决策：策略运行创建，前端不建域（仅 API/MCP）",
    "GET /api/v1/strategies/run/{run_id}":
        "V11 决策：策略运行详情，前端不建域（仅 API/MCP）",
    "POST /api/v1/strategies/run/{run_id}/start":
        "V11 决策：策略运行启动，前端不建域（仅 API/MCP）",
    "POST /api/v1/strategies/run/{run_id}/stop":
        "V11 决策：策略运行停止，前端不建域（仅 API/MCP）",
    "DELETE /api/v1/strategies/run/{run_id}":
        "V11 决策：策略运行删除，前端不建域（仅 API/MCP）",
    "POST /api/v1/strategies/run/batch-delete":
        "V11 决策：策略运行批量删除，前端不建域（仅 API/MCP）",
    "GET /api/v1/strategies/run/{run_id}/logs":
        "V11 决策：策略运行日志，前端不建域（仅 API/MCP）",
    "POST /api/v1/strategies/run/precheck":
        "V11 决策：策略运行风控预检，前端不建域（仅 API/MCP）",

    # -----------------------------------------------------------------------
    # R19 第 3 轮：门禁加固后**新暴露**的「无真实 UI 入口」清单（21 条）。
    #
    # 旧判据把「api 客户端里声明了该路径」当作前端入口。下面这些端点的声明
    # **没有任何调用点**（死声明），旧判据因此把它们算作「已覆盖」—— 门禁长期输出
    # 「未覆盖且未豁免 : 0」，是典型的**假绿灯**。判据改为「声明不算入口、接线才算」
    # 之后逐条回查代码，确认确实没有 UI 入口，按项目惯例登记为「仅 API/MCP」。
    #
    # 处置口径与 V11 决策一致。**将来补了 UI 入口，应删除对应的豁免条目** ——
    # 那时门禁会因「已接线」而通过，留着条目只会让清单腐烂（脚本会报 stale）。
    # -----------------------------------------------------------------------
    # —— 基础设施探活：UI 自身即宿主，探活由桌面壳/启动脚本直接 HTTP 调用 ——
    "GET /api/v1/live":
        "R19-3：基础设施探活，由启动脚本/桌面壳直连，UI 不消费",
    "GET /api/v1/ready":
        "R19-3：就绪探针（依赖装配完成度），由启动脚本/壳消费，UI 不消费",
    "GET /api/v1/platform/status":
        "R19-3：平台总览（运维/诊断向）；UI 系统状态页读 /capabilities 与 /health 系列",
    # —— 券商运行时管理：运维/脚本向 ——
    "GET /api/v1/brokers/runtimes":
        "R19-3：ABI 运行时矩阵（排障用）；UI 经连接管理页的 /brokers/diag 承载",
    "POST /api/v1/brokers/version-info":
        "R19-3：客户端版本画像（内部会 spawn 子进程探测），运维/脚本向",
    # ★ R22（2026-10-09）：原登记「POST /api/v1/brokers/launch」为豁免（理由「UI 不直接起
    #   进程」），但前端已接线 brokerApi.launchClient（Brokers.tsx 三按钮：启动大/小 QMT、
    #   仅补行情），豁免因此过期。删除本条 —— 让门禁把「启动客户端」自然计入前端入口。
    # —— 账户分析 / 批量交易：分析脚本向；UI 用 Positions/AssetSummary 自有实时口径 ——
    "GET /api/v1/account/aggregate":
        "R19-3：多账户聚合视图为分析/Agent 向；UI 账户页按单连接展示",
    "GET /api/v1/account/pnl":
        "R19-3：盈亏分析为分析/Agent 向；UI 浮盈由 Positions/AssetSummary 实时计算",
    "GET /api/v1/account/slippage":
        "R19-3：滑点分析（成交价 vs 当日 open/close/avg）为分析/Agent 向",
    "POST /api/v1/account/batch/order":
        "R19-3：多账户批量下单 UI 未建入口（仅 API/MCP）",
    "POST /api/v1/account/batch/cancel":
        "R19-3：多账户批量撤单 UI 未建入口（仅 API/MCP）",
    # —— 行情元信息 / 增强：UI 已有同能力替代路径 ——
    "GET /api/v1/market/providers":
        "R19-3：Provider 能力目录（运维向）；UI 的数据源矩阵走 /data/providers",
    "GET /api/v1/market/sources":
        "R19-3：行情源可用性列表，UI 未接线（仅 API/MCP）",
    "GET /api/v1/market/periods":
        "R19-3：可用周期清单与 UI 本地常量（shared/periods.ts）同源；端点供脚本/Agent 复用",
    "GET /api/v1/market/breadth":
        "R19-3：市场广度已由 /market/overview 承载，UI 不单独调用",
    "GET /api/v1/market/indices":
        "R19-3：主要指数由 WS 订阅 + /market/overview 提供；端点供脚本/Agent",
    "GET /api/v1/market/board/lookup":
        "R19-3：板块名→代码反查，UI 未接线（仅 API/MCP）",
    "GET /api/v1/market/board/kline":
        "R19-3：板块 K 线；UI 统一走 /market/kline 承载",
    "GET /api/v1/market/capital":
        "R19-3：批量流通股本+涨跌停价；UI 经 /market/stock-info 单只获取，批量端点为脚本向",
    # —— 配置重置：设置页为逐项保存，不做一键重置 ——
    "POST /api/v1/config/runtime/reset":
        "R19-3：运行时配置重置为运维向；设置页逐项保存，不提供一键重置",
    "POST /api/v1/config/ui/reset":
        "R19-3：恢复默认外观未接线；设置页通过预设皮肤应用（仅 API/MCP）",
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


# ---------------------------------------------------------------------------
# 「前端有入口」的**最小证据**（R19 第 3 轮加固）
# ---------------------------------------------------------------------------
# 旧判据：只要前端源码里出现该路径字面量就算「有入口」。但路径字面量的**唯一**
# 出现处通常就是 `services/api/*.ts` 的声明行 —— 于是一条**没有任何调用点**的
# 死声明，也能把一个后端能力判成「界面可达」。
#
# 实测（R19 第 3 轮）有 **21 个端点**的入口证据**全部**来自死声明
# （accountApi.pnl / marketApi.indices / marketApi.quote / systemApi.ready /
#   brokerApi.launch / researchApi.factorCompute … ），门禁因此长期输出
# 「未覆盖且未豁免 : 0」—— 正是本项目定义的**假绿灯**：绿灯不是没问题，
# 而是判据太松。
#
# 新判据（满足其一即算入口）：
#   ① 路径字面量出现在 `services/api/` **之外**的前端源码里（页面/钩子直接写 URL）；
#   ② 该路径由某个 api 客户端方法声明，且**该方法名在客户端之外有 `.name(` 调用点**。
# 一句话：**声明不算入口，接线才算。**
#
# 已声明的宽松边界（诚实标注）：② 按**方法名**匹配调用点，因此「同名方法」之间
# 不区分所属对象；若某个 api 方法名恰好与业务代码里的某次 `.同名(` 调用撞名，
# 会被判为已接线。这类误判方向是「偏向通过」而非「偏向报红」，可以接受；真要
# 精确到对象需要类型信息，超出静态扫描的能力范围。
API_DIR_REL = "frontend-next/src/services/api/"
_API_OBJ_RE = re.compile(r"^export const (\w+)\s*=")
_API_METHOD_RE = re.compile(r"^\s{2,}(\w+)\s*:\s*(?:async\s*)?\(")
# ★ 2026-10-03 修（与 `check_api_contract_drift.py` 同一类**假绿灯**）：
#   原正则的泛型段写的是 ``[^(\n]*`` —— **不允许跨行**。而 `system.ts` 为类型排版，
#   把 `remoteAccessStatus` / `setRemoteAccessMode` 的返回类型排成了 6~10 行
#   （``http.get<{\n ... \n}>("/remote-access/status")``），于是这两个端点
#   **既没被认作已声明、也没被认作未覆盖**，直接落进「未覆盖且未豁免」——
#   而它们明明在 `RemoteAccess.tsx` 里有真实调用点。
#   也就是说：门禁报红报的是**扫描器看不懂**，不是**前端没接线**。
#   泛型段改成「直到第一个 `(`」（``[^()]*``），嵌套尖括号天然成立。
_HTTP_CALL_RE = re.compile(
    r"""\bhttp\.(?:get|post|patch|put|delete|del)\b[^()]*\(\s*([`"'])([^`"']*)\1""")


def api_client_declarations() -> list[tuple[str, str, str]]:
    """扫描 `services/api/*.ts`：返回 (相对路径, 声明它的方法名, 该方法的路径字面量)。"""
    out: list[tuple[str, str, str]] = []
    api_dir = FRONTEND / "src" / "services" / "api"
    for f in sorted(api_dir.glob("*.ts")):
        rel = str(f.relative_to(ROOT)).replace("\\", "/")
        lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        entries: list[tuple[str, int, int]] = []
        obj: str | None = None
        meth: str | None = None
        start = 0
        for i, line in enumerate(lines):
            m = _API_OBJ_RE.match(line)
            if m:
                if meth:
                    entries.append((meth, start, i))
                obj, meth, start = m.group(1), None, i
                continue
            if obj and re.match(r"^\}\s*;?\s*$", line):
                if meth:
                    entries.append((meth, start, i))
                obj, meth = None, None
                continue
            if obj:
                m2 = _API_METHOD_RE.match(line)
                if m2:
                    if meth:
                        entries.append((meth, start, i))
                    meth, start = m2.group(1), i
        if meth:
            entries.append((meth, start, len(lines)))
        for name, s, e in entries:
            call = _HTTP_CALL_RE.search("\n".join(lines[s:e + 1]))
            if call:
                out.append((rel, name, call.group(2)))
    return out


def api_client_call_sites(sources: list[tuple[str, str]],
                          method_names: set[str]) -> set[str]:
    """在 `services/api/` **之外**搜索 ``.name(``，返回确有调用点的方法名集合。"""
    names = sorted(n for n in method_names if n)
    if not names:
        return set()
    rx = re.compile(r"\.\s*(%s)\s*\(" % "|".join(re.escape(n) for n in names))
    used: set[str] = set()
    for f, text in sources:
        if f.startswith(API_DIR_REL):
            continue
        used.update(rx.findall(text))
    return used


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

    # 「前端有入口」的证据：见头部 §「最小证据」注释。
    decls = api_client_declarations()
    used_methods = api_client_call_sites(sources, {m for _f, m, _p in decls})
    parser_ok = bool(decls) and bool(used_methods)
    if not parser_ok:
        # 解析脱节时**绝不静默**：退回旧判据（宽松），并高声告知 ——
        # 「门禁因解析失败而恒绿」比红灯更糟。
        print("[!] api 客户端解析异常（decls=%d / used=%d）：本轮退回宽松判据，"
              "请检查正则与代码形态是否脱节" % (len(decls), len(used_methods)))
    path_methods: dict[str, set[str]] = defaultdict(set)
    for _f, m, p in decls:
        path_methods[p].add(m)

    def entry_evidence(path: str) -> str | None:
        rx = cap_regex(path)
        for f, text in sources:
            if f.startswith(API_DIR_REL):
                continue
            if rx.search(text):
                return f                      # ① 客户端之外直接写了该 URL
        if not parser_ok:
            for f, text in sources:           # 退回旧判据（含 api 目录）
                if rx.search(text):
                    return f
        for raw, methods in path_methods.items():
            live = methods & used_methods
            if live and rx.search(raw):
                return "api-call:" + raw + "#" + ",".join(sorted(live))   # ② 声明且已接线
        return None

    covered: list[dict] = []
    exempted: list[dict] = []
    gaps: list[dict] = []

    for c in caps:
        path = str(c.get("path") or "")
        category = str(c.get("category") or _domain_of(path))
        hit = entry_evidence(path)
        row = {
            "path": path,
            "method": str(c.get("method") or ""),
            "category": category,
            "agent_visible": bool(c.get("agent_visible")),
        }
        if hit:
            row["frontend"] = hit
            covered.append(row)
        elif f"{row['method']} {path}" in EXEMPT_PATHS:
            # 精确路径豁免（V11 决策 2）：粒度最细，优先于 category 级豁免。
            row["exempt_reason"] = EXEMPT_PATHS[f"{row['method']} {path}"]
            exempted.append(row)
        elif category in EXEMPT:
            row["exempt_reason"] = EXEMPT[category]
            exempted.append(row)
        else:
            gaps.append(row)

    by_domain: dict[str, list[dict]] = defaultdict(list)
    for g in gaps:
        by_domain[g["category"]].append(g)

    # 豁免清单**反腐烂**：路径级豁免只要过期了就报红 ——
    #   (a) 后端已经没有这个端点了（豁免指向空气）；
    #   (b) 该端点已有**真实**前端入口（补了 UI 却忘了删豁免）。
    # 没有这两条，「累积的豁免清单」会慢慢变成一份没人敢删的谎话清单。
    known = {f"{r['method']} {r['path']}" for r in covered + exempted + gaps}
    covered_keys = {f"{r['method']} {r['path']}" for r in covered}
    stale_exempt = [
        f"{k}（后端已无此端点）" if k not in known
        else f"{k}（已有真实前端入口，应删除该豁免并让门禁自然通过）"
        for k in sorted(EXEMPT_PATHS) if k not in known or k in covered_keys
    ]

    print(f"\n[✓] 前端有入口     : {len(covered)}")
    print(f"[~] 豁免（仅API/MCP）: {len(exempted)}")
    print(f"[✗] 未覆盖且未豁免 : {len(gaps)}")
    print(f"[✗] 过期豁免       : {len(stale_exempt)}")
    for s in stale_exempt:
        print(f"      - {s}")

    if exempted:
        print("\n── 豁免明细（须在文档标注「仅 API/MCP 可用」）──")
        # 按 (category, 理由) 分组：路径级豁免与 category 级豁免可共存，理由可能不同。
        groups: dict[tuple[str, str], int] = defaultdict(int)
        for e in exempted:
            groups[(e["category"], str(e.get("exempt_reason") or ""))] += 1
        for (dom, reason), n in sorted(groups.items()):
            print(f"  {dom:<18} {n:>3} 条  {reason}")

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

    return 1 if (gaps or stale_exempt) else 0


def _domain_of(path: str) -> str:
    m = re.match(r"^/api/v1/([^/]+)", path)
    return m.group(1) if m else "unknown"


if __name__ == "__main__":
    sys.exit(main())
