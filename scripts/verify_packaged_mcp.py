#!/usr/bin/env python3
"""打包态 MCP 端到端验证：真的按 MCP 协议握手并调用一个工具。

为什么需要它
------------
``GET /api/v1/capabilities/mcp`` 只证明「后端能自省出工具清单」，**不能证明**
Agent 真的能通过 MCP 协议调通 —— 这两者之间还隔着：

- streamable-http 传输与会话头（``Mcp-Session-Id`` / ``Accept``）；
- FastMCP 的 initialize → notifications/initialized 握手顺序；
- 打包态是否把 ``mcp_server`` 及其依赖真的打进了 EXE（PyInstaller 隐藏导入漏了
  就会在协议层 500，而 REST 自省走的是另一条代码路径，照样能返回清单）。

因此本脚本**只走协议层**：initialize → tools/list → tools/call，任一步失败即退出非 0。

用法
----
    python scripts/verify_packaged_mcp.py [--base http://127.0.0.1:21118] [--tool get_health]

默认挑一个只读工具调用（不碰交易、不落任何订单）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:21118"
ENDPOINT = "/mcp"

# 只读候选：**全部**会被调用（绝不选交易/下单类）。
#
# ★ 必须至少包含一个「handler 依赖 ctx」的工具：这类工具曾因依赖注入参数被当成
# 普通入参（``ctx=""``）而一调就炸，而握手/tools/list 全绿 —— 只调不依赖 ctx 的
# 工具永远发现不了。``get_alerts_rules`` / ``get_notifications`` 就是这条探针。
SAFE_CANDIDATES = (
    "get_health", "get_platform_status", "get_market_boards",
    "get_reference_trading_calendar", "get_runtime_jobs",
    "get_alerts_rules", "get_notifications", "get_notifications_logs",
)


def _rpc(base: str, payload: dict, session: str | None = None, timeout: float = 20.0,
         allow_empty: bool = False):
    url = base.rstrip("/") + ENDPOINT
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        # streamable-http：两种响应都要能接（JSON 或 SSE）
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        sid = resp.headers.get("Mcp-Session-Id") or session
    # 通知类请求（notifications/*）返回 202 空体属正常
    if allow_empty and not raw.strip():
        return {}, sid
    # SSE 形态：取第一个 data: 行
    if raw.lstrip().startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    return json.loads(raw), sid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--tool", default="", help="要调用的工具名（默认自动挑一个只读工具）")
    ap.add_argument("--args", default="{}", help="工具参数 JSON")
    args = ap.parse_args()

    fails: list[str] = []

    # 1) initialize
    try:
        resp, sid = _rpc(args.base, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "verify-packaged-mcp", "version": "1.0"},
            },
        })
    except urllib.error.HTTPError as exc:
        print(f"[FAIL] initialize HTTP {exc.code}: {exc.read()[:300]!r}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] initialize 异常：{exc}")
        return 1
    if "result" not in resp:
        print(f"[FAIL] initialize 无 result：{resp}")
        return 1
    print(f"[PASS] initialize -> protocolVersion="
          f"{resp['result'].get('protocolVersion')} session={bool(sid)}")

    # 2) notifications/initialized（无响应体要求的通知）
    try:
        _rpc(args.base, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid,
             allow_empty=True)
    except Exception as exc:  # noqa: BLE001 — 通知失败不致命，但记一笔
        print(f"[WARN] notifications/initialized 异常（继续）：{exc}")

    # 3) tools/list
    try:
        resp, sid = _rpc(args.base, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] tools/list 异常：{exc}")
        return 1
    tools = (resp.get("result") or {}).get("tools") or []
    if not tools:
        print(f"[FAIL] tools/list 返回空：{resp}")
        return 1
    names = [t.get("name") for t in tools]
    print(f"[PASS] tools/list -> {len(tools)} 个工具")

    # 与 REST 自省口径对齐（双真源漂移检查）
    try:
        with urllib.request.urlopen(args.base.rstrip("/") + "/api/v1/capabilities/mcp",
                                    timeout=15) as r2:
            rest = json.loads(r2.read().decode("utf-8"))["data"]["tools"]
    except Exception as exc:  # noqa: BLE001
        rest = None
        print(f"[WARN] REST 自省不可比对：{exc}")
    if rest is not None:
        if set(rest) == set(names):
            print(f"[PASS] 协议层与 REST 自省一致（{len(names)} 个）")
        else:
            diff = sorted(set(rest) ^ set(names))
            fails.append(f"双真源漂移：{diff[:8]}")

    # 4) tools/call（只读）
    # ★ 逐个跑 SAFE_CANDIDATES 里**存在**的工具，而不是只跑第一个：
    #   MCP 的错误是**带内**返回的（``result.isError = true`` + content 里的
    #   "Error calling tool ..."），握手与 tools/list 照样全绿。只调一个工具、
    #   且只检查「有没有 result」，就正好漏掉「工具在但一调就炸」这类缺陷
    #   （实测 get_alerts_rules 报 'str' object has no attribute 'db' 却被判 PASS）。
    if args.tool:
        targets = [args.tool]
    else:
        targets = [c for c in SAFE_CANDIDATES if c in names]
    if not targets:
        fails.append("找不到可安全调用的工具（候选均不在清单中）")

    for tool in targets:
        call_args = json.loads(args.args) if (args.args and args.args != "{}") else {}
        try:
            resp, _sid = _rpc(args.base, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": tool, "arguments": call_args},
            }, sid)
        except Exception as exc:  # noqa: BLE001
            fails.append(f"tools/call({tool}) 异常：{exc}")
            continue
        if resp.get("error"):
            fails.append(f"tools/call({tool}) 返回错误：{resp['error']}")
            continue
        if "result" not in resp:
            fails.append(f"tools/call({tool}) 响应异常：{str(resp)[:200]}")
            continue
        result = resp["result"] or {}
        preview = json.dumps(result, ensure_ascii=False)[:160]
        if result.get("isError"):
            fails.append(f"tools/call({tool}) 工具报错：{preview}")
        else:
            print(f"[PASS] tools/call -> {tool}  {preview}")

    if fails:
        print("\n".join(f"[FAIL] {f}" for f in fails))
        return 1
    print("MCP END-TO-END OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
