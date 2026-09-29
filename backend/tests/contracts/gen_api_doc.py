#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""幂等重生 docs/API接口文档.md —— 纯读 backend/tests/contracts/{rest_endpoints,mcp_tools,ws_events}.json。

不与运行中的 FastAPI 进程交互，只依据已固化的契约文件生成，保证「计数随契约」、与 CI 漂移门禁一致。
用法：python backend/tests/contracts/gen_api_doc.py

说明文字（REST 资源用途 / MCP 前缀能力域 / WS 渠道含义）维护在本文件的三个 DESC 表中；
渠道名 / 端点 / 工具名一律来自契约文件，缺失说明时回退为「—」，新增接口后请补一行说明。
WS 渠道说明的口径与 docs/多语言接入指南.md 保持一致（以指南为准修订）。
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
    "algo": "算法单",
    "api-keys": "API 密钥",
    "audit": "审计",
    "backtest": "回测",
    "brokers": "券商连接",
    "capabilities": "能力自描述",
    "config": "配置",
    "data": "数据源",
    "datahub": "数据策略",
    "factors": "因子",
    "health": "健康",
    "limitup": "涨停监控",
    "live": "存活探针",
    "market": "行情",
    "metrics": "指标暴露",
    "notifications": "通知",
    "paper": "模拟盘",
    "platform": "平台状态",
    "quote-bus": "行情总线",
    "ready": "就绪探针",
    "rebalance": "再平衡",
    "reconcile": "对账",
    "reference": "参考数据",
    "research": "研究分析",
    "runtime": "任务运行时",
    "scheduler": "调度器",
    "signal": "交易信号",
    "strategies": "策略",
    "strategy-market": "策略市场",
    "sync": "行情订阅",
    "target-portfolio": "目标持仓",
    "trade": "交易",
    "wal": "WAL",
    "webhooks": "Webhook",
}

# REST 资源 -> 一句话用途（渲染为组说明；缺失时省略）
REST_GROUP_DESC = {
    "account": "账户总览、盈亏与滑点分析；批量下单 / 撤单 / 重连。",
    "alerts": "告警规则的增删改查、批量删除、测试推送与历史查询。",
    "algo": "算法拆单：提交、暂停、恢复与撤销。",
    "api-keys": "API Key 的创建、轮换、启用/禁用、批量删除与清理未使用。",
    "audit": "审计日志查询与完整性校验。",
    "backtest": "回测作业的增删改查与参数扫描（sweep）。",
    "brokers": "券商连接全生命周期：配置、自动检测、启动、连接/断开/激活、健康与诊断。",
    "capabilities": "运行期能力自描述：REST / MCP 能力总览（agent-visible 口径）。",
    "config": "路径 / 风控 / 运行时 / UI 配置的读写、导入导出，及备份、迁移、熔断、回滚等运维动作。",
    "data": "数据源清单、健康检查与链路诊断。",
    "datahub": "数据面策略（policies）查询。",
    "factors": "因子计算：单标的、批量、从 K 线衍生。",
    "health": "健康探针。",
    "limitup": "涨停监控的启停、状态查询与打板池管理。",
    "live": "存活探针（进程活着即 200）。",
    "market": "行情全景：K线 / 分时 / tick、板块与成分、资金流、选股（表达式 / 经典 / 自然语言）、指标计算、导出与同步。",
    "metrics": "Prometheus 指标暴露。",
    "notifications": "通知渠道的增删改查、测试发送与投递日志。",
    "paper": "模拟盘：账户 / 持仓 / 成交 / 绩效查询，下单与重置。",
    "platform": "平台运行状态。",
    "quote-bus": "行情总线（quote-bus）统计。",
    "ready": "就绪探针（关键依赖就绪才返回 200）。",
    "rebalance": "按目标持仓生成并执行再平衡。",
    "reconcile": "委托对账核销与 WAL 检查点。",
    "reference": "静态参考数据：交易日历、财务摘要、板块列表与板块成分。",
    "research": "研究分析：归因、相关性、因子 IC、分位分析、组合回测与 walk-forward。",
    "runtime": "作业与定时任务的增删改查、手动触发与取消。",
    "scheduler": "调度器优雅关停。",
    "signal": "交易信号提交 / 二次确认、模式切换（dry_run / paper / live）与 webhook 接入。",
    "strategies": "策略生成、保存、预检与运行生命周期（启动 / 停止 / 日志 / 删除）。",
    "strategy-market": "策略市场：目录浏览、导入导出、安装与发布。",
    "sync": "行情订阅注册。",
    "target-portfolio": "目标持仓计划的增删改查与同步执行。",
    "trade": "下单 / 撤单 / 预检、条件单管理与委托 / 成交 / 持仓查询。",
    "wal": "WAL 统计与手动检查点。",
    "webhooks": "出站 Webhook 订阅的增删改查、测试与投递记录。",
}

# MCP 前缀（能力域）-> 一句话用途
MCP_PREFIX_DESC = {
    "account_*": "账户状态。",
    "algo_*": "算法单生命周期。",
    "analyze_*": "交易分析（贡献度 / 滑点）。",
    "attribute_*": "绩效归因。",
    "broker_*": "券商状态。",
    "cancel_*": "撤单（按单号 / 价格）。",
    "compare_*": "回测结果对比。",
    "condition_*": "条件单管理。",
    "factor_*": "因子分析（相关矩阵 / IC / 分位）。",
    "financial_*": "财务摘要。",
    "generate_*": "生成（策略 / 再平衡方案）。",
    "get_*": "只读查询（行情 / 账户 / 配置 / 任务 / 系统状态等）。",
    "l2_*": "Level-2 逐笔成交。",
    "limitup_*": "涨停监控与打板池。",
    "list_*": "券商列举。",
    "monitor_*": "账户监控。",
    "monthly_*": "月度盈亏。",
    "net_*": "净值序列。",
    "order_*": "按目标仓位下单。",
    "place_*": "下单。",
    "portfolio_*": "组合回测。",
    "query_*": "账户查询（资金 / 委托 / 成交 / 持仓）。",
    "run_*": "运行回测。",
    "save_*": "保存策略。",
    "search_*": "标的搜索。",
    "sector_*": "板块与成分股。",
    "sensitivity_*": "参数敏感性分析。",
    "target_*": "目标持仓管理。",
    "trading_*": "交易日历。",
    "walk_*": "walk-forward 滚动验证。",
}

# WS 渠道 -> 含义（口径同 docs/多语言接入指南.md §5；缺失时回退「—」）
WS_CHANNEL_DESC = {
    "account": "账户净值周期快照（SyncEngine 周期任务广播）。",
    "alert": "告警规则命中。",
    "algo_alert": "算法单异常告警。",
    "algo_slice": "算法单拆单进度。",
    "broker.connected": "券商连接建立（含重连成功）。",
    "broker.disconnected": "券商连接断开 / 进入退避重试（`health_status=needs_action` 表示需人工处理）。",
    "condition_created": "条件单创建。",
    "condition_expired": "条件单到期。",
    "condition_failed": "条件单失败（含原因）。",
    "condition_order": "条件单已转委托。",
    "condition_settled": "条件单结算完成。",
    "condition_triggered": "条件单触发。",
    "deal": "新成交。",
    "heartbeat": "心跳。",
    "limitup": "涨停监控状态变化。",
    "limitup_order": "涨停打板下单。",
    "order": "委托新增 / 状态变化。",
    "order.timeout": "委托超时自动撤单。",
    "pong": "ping 应答。",
    "quotes": "行情微批帧（默认 100ms 聚合）。",
    "quotes_replay": "断线重连后的补发帧。",
    "reconcile": "委托对账核销结果。",
    "risk": "风控触发 / 熔断状态变化。",
    "risk.blocked": "风控拦截（附原因）。",
    "signal_dry_run": "信号路由结果（预演模式）。",
    "signal_live": "信号路由结果（实盘模式）。",
    "signal_paper": "信号路由结果（模拟盘模式）。",
    "signal_pending": "信号等待二次确认。",
    "snapshot": "账户快照（净值 / 持仓 / 资金）。",
    "system": "系统级消息。",
}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


METHOD_ORDER = ["GET", "POST", "PUT", "DELETE", "PATCH"]


def gen_rest(rest: dict):
    # 分组：资源 = /api/v1/ 后第一段
    groups = {}
    for key in rest:
        method, path = key.split(" ", 1)
        assert path.startswith("/api/v1/"), key
        resource = path[len("/api/v1/"):].split("/")[0]
        groups.setdefault(resource, []).append((method, path))

    lines = ["## REST API（%d 端点）" % len(rest), ""]
    lines.append("所有路径前缀为 `/api/v1`，按资源分组；组内先按方法（`G`=GET `P`=POST `U`=PUT `D`=DELETE `X`=PATCH）再按路径排序。")
    lines.append("")
    for resource in sorted(groups.keys()):
        items = groups[resource]
        items_sorted = sorted(items, key=lambda t: (METHOD_ORDER.index(t[0]), t[1]))
        label = RESOURCE_LABELS.get(resource, resource)
        lines.append("### %s（%s）· %d 端点" % (resource, label, len(items_sorted)))
        lines.append("")
        desc = REST_GROUP_DESC.get(resource)
        if desc:
            lines.append(desc)
            lines.append("")
        lines.append("| M | 路径 |")
        lines.append("|---|------|")
        for method, path in items_sorted:
            lines.append("| %s | `%s` |" % (METHOD_LETTER[method], path))
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
        desc = MCP_PREFIX_DESC.get(prefix)
        if desc:
            lines.append(desc)
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
    lines.append("连接 `ws://<host>:<port>/api/v1/ws?token=<API-KEY>`（本机回环免 token）。服务端先推全量快照，再补发订阅代码最近 30s 行情（断线重连缺口）。客户端动作：`subscribe` / `unsubscribe` / `ping`（→ `pong`）。")
    lines.append("")
    lines.append("payload 的 `data.type` 子类型（如 `order`→`order_event`）**不是**独立频道名；`order` / `deal` / `account` / `risk` 四类频道事件同时投递出站 Webhook。")
    lines.append("")
    lines.append("| 渠道 | 子类型 | 说明 |")
    lines.append("|------|--------|------|")
    for channel in sorted(ws.keys()):
        subs = ws[channel]
        cell = "、".join("`%s`" % s for s in subs) if subs else "—"
        desc = WS_CHANNEL_DESC.get(channel, "—")
        lines.append("| `%s` | %s | %s |" % (channel, cell, desc))
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
    out.append("> 鉴权、scope、错误语义、payload 形状与跨语言（Python / Node / curl）示例见 [`多语言接入指南.md`](多语言接入指南.md)。")
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
