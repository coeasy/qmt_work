"""G3-2 · MCP 自动暴露生成器。

遍历能力注册表（app.capabilities），对「agent_visible 的 GET 只读端点」自动生成
MCP tool，使上一轮新增的行情能力（boards/etfs/rotation/overview/indices 等）无需
手工在 tools/ 注册即可被 Agent 看见 —— 把 REST 覆盖 177 端点 → MCP 覆盖从 31.6% 跃迁。

设计纪律（对齐 G0 协议红线与 G8 风控）：
- 仅自动暴露 GET 只读端点；写入型（POST/PUT/DELETE）一律保持人工确认护栏，不自动暴露。
- 已存在同名手工 tool（含 market 域 6 个端点的名称回退）自动跳过，杜绝重复工具。
- 直接调用路由 handler（同一进程内），绕开 HTTP/鉴权/限流；handler 返回统一
  {code,message,data} 包时自动解包为 data；非零业务码（如 503 未连券商）原样返回，
  绝不伪造数据（零 mock 铁律）。
- 所有工具逻辑均为 qmt_work 独立实现，不复制任何 AGPL 项目源码。
"""
from __future__ import annotations

import inspect

from app.capabilities import agent_visible_reads, tool_name_for

_TYPE_ANN = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
}


def _make_tool_func(endpoint, cap, name: str):
    """动态构造一个签名与 handler 对齐的 async 函数，注册为 MCP tool。"""
    params = cap.param_schema
    sig_parts: list[str] = []
    for p in params:
        ann = _TYPE_ANN.get(p.type, "str")
        if p.required:
            sig_parts.append(f"{p.name}: {ann}")
        else:
            default = p.default
            if p.type == "array":
                default = default if isinstance(default, list) else []
            elif p.type == "string":
                default = default if isinstance(default, str) else ""
            sig_parts.append(f"{p.name}: {ann} = {default!r}")
    sig = ", ".join(sig_parts)
    argnames = [p.name for p in params]
    # 构造 {arg: arg} 字典字面量；exec 命名空间内可访问局部变量 arg
    dict_literal = "{" + ", ".join(f"{a!r}: {a}" for a in argnames) + "}"

    src = (
        f"async def {name}({sig}):\n"
        f"    _kwargs = {dict_literal}\n"
        f"    _kwargs = {{k: v for k, v in _kwargs.items() if v is not None}}\n"
        f"    _res = await _ENDPOINT(**_kwargs)\n"
        f"    if isinstance(_res, dict) and 'code' in _res and 'data' in _res:\n"
        f"        return _res['data'] if _res['code'] == 0 else _res\n"
        f"    return _res\n"
    )
    ns: dict = {"_ENDPOINT": endpoint}
    try:
        exec(compile(src, f"<auto_tool:{name}>", "exec"), ns)
    except SyntaxError as exc:  # noqa: BLE001
        raise RuntimeError(f"生成自动 tool 失败 {name}: {exc}\n{src}") from exc
    func = ns[name]
    func.__doc__ = cap.summary or name
    func.__name__ = name
    return func


def register_auto_tools(mcp) -> int:
    """把 agent_visible 的 GET 端点注册为 MCP tool。返回新增工具数。"""
    # FastMCP 2.14.7 的 get_tools() 是协程，无法在同步注册期 await；
    # 直接读内部 _tool_manager._tools（dict）拿到已注册手工 tool 名，用于碰撞跳过。
    existing = set()
    try:
        tm = getattr(mcp, "_tool_manager", None)
        if tm is not None and getattr(tm, "_tools", None) is not None:
            existing = set(tm._tools.keys())
    except Exception:  # noqa: BLE001
        existing = set()

    added = 0
    for cap in agent_visible_reads():
        name = tool_name_for(cap)
        if not name or name in existing:
            continue  # 已存在手工 tool（含 market 回退名）→ 跳过，不重复
        try:
            func = _make_tool_func(cap.endpoint, cap, name)
        except Exception as exc:  # noqa: BLE001
            # 单个端点生成失败不应拖垮整体注册
            import logging
            logging.getLogger("qmt_work.mcp").warning(
                "跳过自动 tool %s: %s", name, exc)
            continue
        try:
            mcp.tool(name=name, description=cap.summary or name)(func)
            existing.add(name)
            added += 1
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger("qmt_work.mcp").warning(
                "注册自动 tool 失败 %s: %s", name, exc)
    return added


# 防止未使用导入告警（inspect 供未来扩展参数自省）
_ = inspect
