#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""幂等重生 docs/API接口文档.md —— 纯读 backend/tests/contracts/{rest_endpoints,mcp_tools,ws_events}.json。

不与运行中的 FastAPI 进程交互，只依据已固化的契约文件生成，保证「计数随契约」、与 CI 漂移门禁一致。
用法：python backend/tests/contracts/gen_api_doc.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
OUT = os.path.join(REPO, "docs", "API接口文档.md")

REST_FILE = os.path.join(HERE, "rest_endpoints.json")
MCP_FILE = os.path.join(HERE, "mcp_tools.json")
WS_FILE = os.path.join(HERE, "ws_events.json")

METHOD_LETTER = {"GET": "G", "POST": "P", "PUT": "U", "DELETE": "D", "PATCH": "X"}

# REST 资源（/api/v1/<resource>/...）-> 中文标签；缺失时回退为资源名本身
RESOURCE_LABELS = {
    "account": "账户",
    "alerts": "告警",
    "algo": "algo",
    "api-keys": "api-keys",
    "audit": "审计",
    "backtest": "回测",
    "brokers": "券商连接",
    "capabilities": "能力自描述",
    "config": "配置",
    "data": "data",
    "datahub": "datahub",
    "factors": "factors",
    "health": "health",
    "limitup": "涨停监控",
    "live": "live",
    "market": "行情",
    "metrics": "指标",
    "notifications": "通知",
    "paper": "模拟盘",
    "platform": "platform",
    "quote-bus": "quote-bus",
    "ready": "ready",
    "rebalance": "再平衡",
    "reconcile": "reconcile",
    "reference": "reference",
    "research": "research",
    "runtime": "运行时/任务",
    "scheduler": "scheduler",
    "signal": "signal",
    "strategies": "策略",
    "strategy-market": "strategy-market",
    "sync": "sync",
    "target-portfolio": "目标持仓",
    "trade": "交易",
    "wal": "wal",
    "webhooks": "Webhook",
}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


METHOD_ORDER = ["GET", "POST", "PUT", "DELETE", "PATCH"]


def gen_rest(rest: dict):
    # 分组：资源 = /api/v1/ 后第一段
    groups = {}
    for key, tag in rest.items():
        method, path = key.split(" ", 1)
        assert path.startswith("/api/v1/"), key
        resource = path[len("/api/v1/"):].split("/")[0]
        groups.setdefault(resource, []).append((method, path, tag))

    lines = ["## REST API（%d 端点）" % len(rest), ""]
    lines.append("所有路径前缀为 `/api/v1`。按资源分组；组内先按方法（`G`=GET `P`=POST `U`=PUT `D`=DELETE `X`=PATCH）再按路径排序。")
    lines.append("")
    for resource in sorted(groups.keys()):
        items = groups[resource]
        # 组内：方法顺序 -> 路径
        items_sorted = sorted(items, key=lambda t: (METHOD_ORDER.index(t[0]), t[1]))
        label = RESOURCE_LABELS.get(resource, resource)
        lines.append("### %s（%s）· %d 端点" % (resource, label, len(items_sorted)))
        lines.append("")
        lines.append("| M | 路径 | 标签 |")
        lines.append("|---|------|------|")
        for method, path, tag in items_sorted:
            lines.append("| %s | `%s` | %s |" % (METHOD_LETTER[method], path, tag))
        lines.append("")
    return lines


def mcp_prefix(name: str):
    if "_" in name:
        return name.split("_", 1)[0] + "_*"
    return name


def gen_mcp(mcp: list):
    groups = {}
    for name in mcp:
        groups.setdefault(mcp_prefix(name), []).append(name)
    lines = ["## MCP 工具（%d 个）" % len(mcp), ""]
    lines.append("FastMCP Streamable HTTP，接入点 `http://<host>:<port>/mcp`，携带 `X-API-Key`。工具集与 REST 的 `agent-visible` 端点保持同步（能力漂移门禁校验）。")
    lines.append("")
    lines.append("按前缀分组（前缀 = 能力域）：")
    lines.append("")
    for prefix in sorted(groups.keys()):
        names = sorted(groups[prefix])
        lines.append("#### %s · %d 个" % (prefix, len(names)))
        lines.append("")
        lines.append("| 工具 |")
        lines.append("|------|")
        for n in names:
            lines.append("| `%s` |" % n)
        lines.append("")
    return lines


def gen_ws(ws: dict):
    n_channels = len(ws)
    n_sub = sum(1 for v in ws.values() if v)
    lines = ["## WebSocket 事件（%d 渠道 / %d 子类型）" % (n_channels, n_sub), ""]
    lines.append("连接 `ws://<host>:<port>/api/v1/ws?token=<API-KEY>`。服务端先推全量快照，再补发订阅代码最近 30s 行情（断线重连缺口）。客户端动作：`subscribe` / `unsubscribe` / `ping`（→ `pong`）。")
    lines.append("")
    lines.append("| 渠道 | 子类型（payload 形状见多语言接入指南） |")
    lines.append("|------|------------------------------------------|")
    for channel in sorted(ws.keys()):
        subs = ws[channel]
        cell = "、".join("`%s`" % s for s in subs) if subs else "—"
        lines.append("| `%s` | %s |" % (channel, cell))
    lines.append("")
    return lines


def main():
    rest = load_json(REST_FILE)
    mcp = load_json(MCP_FILE)
    ws = load_json(WS_FILE)

    out = []
    out.append("# API 接口文档（自动生成 · 反映真实契约）")
    out.append("")
    out.append("> 本文档由 `backend/tests/contracts/{rest_endpoints,mcp_tools,ws_events}.json` **自动生成**，是 qmt_work 当前 REST / MCP / WebSocket 接口的**真实清单**。")
    out.append("")
    out.append("> 契约基线由 CI 门禁固化（`check_capability_drift.py` / `ci_reconcile.py`）：删除或重命名任一接口即红灯。**计数随代码变化**，以契约文件为准，不要手抄。")
    out.append("")
    out.append("> 鉴权、scope、错误语义、跨语言（Python / Node / curl）示例见 [`多语言接入指南.md`](多语言接入指南.md)；本文档只列端点/工具/事件本体。")
    out.append("")
    out.append("## 规模速览")
    out.append("")
    out.append("| 接口面 | 数量 | 来源 |")
    out.append("|--------|------|------|")
    out.append("| REST 端点 | **%d** | `rest_endpoints.json` |" % len(rest))
    out.append("| MCP 工具 | **%d** | `mcp_tools.json` |" % len(mcp))
    out.append("| WebSocket 渠道 | **%d** 渠道 / %d 子类型 | `ws_events.json` |" % (len(ws), sum(1 for v in ws.values() if v)))
    out.append("")
    out.append("## 通用约定（简明，详细见多语言接入指南）")
    out.append("")
    out.append("- **鉴权**：REST / MCP 均带 `X-API-Key` 头（由 `QMT_API_KEY` 配置，生产务必修改）；WebSocket 连接带 `?token=<API-KEY>` 查询参数。")
    out.append("- **零 mock**：未连接券商时行情/交易/账户等端点返回 **HTTP 503** + 可操作引导；券商可用但被拒（风控/柜台拒单/非交易时段）返回 **HTTP 400** + 真实原因。**绝不把失败包成 `code=0`**，否则拒单会被前端显示成「已报」= 假成功。")
    out.append("- **统一响应包裹**：`{ code, message, data }`，`code !== 0` 即错误。健康检查探针（`/live` `/health` `/ready` `/metrics`）额外带 `service/version`。")
    out.append("- **错误归因**：错误 `message` 给出根因分类（未连接券商 / 柜台拒单 / 风控拦截 / 参数错误 / 内部异常），前端按分类给出不同引导，不做「网络错误」兜底。")
    out.append("")
    out.extend(gen_rest(rest))
    out.append("---")
    out.append("")
    out.extend(gen_mcp(mcp))
    out.append("---")
    out.append("")
    out.extend(gen_ws(ws))
    out.append("---")
    out.append("")
    out.append("## 端点/工具/事件完整机读清单")
    out.append("")
    out.append("- REST：`backend/tests/contracts/rest_endpoints.json`")
    out.append("- MCP：`backend/tests/contracts/mcp_tools.json`")
    out.append("- WebSocket：`backend/tests/contracts/ws_events.json`")
    out.append("")
    out.append("能力自描述运行期端点：`GET /api/v1/capabilities`（REST 总览）、`GET /api/v1/capabilities/mcp`（MCP 工具分组计数）。桌面客户端「系统 → MCP 工具」页可浏览并一键复制。")
    out.append("")

    text = "\n".join(out)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print("written: %s" % OUT)
    print("REST=%d MCP=%d WS(channels=%d, subtypes=%d)" % (
        len(rest), len(mcp), len(ws), sum(1 for v in ws.values() if v)))


if __name__ == "__main__":
    main()
