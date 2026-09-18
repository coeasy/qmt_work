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


def _is_di_param(p) -> bool:
    """该参数是不是 FastAPI **依赖注入项**（调用方不该提供，必须由框架解析）。

    ★ 为什么必须单独识别：能力自省把 ``ctx: AppContext = Depends(get_ctx)`` 当成
    **普通 string 参数**收进了 ``param_schema``（默认值为 ``Depends`` 对象）。
    于是自动生成的工具把它编进函数签名，缺省值被 ``_TYPE_ANN`` 规则压成空串 ``""``，
    调用时就把 ``ctx=""`` 原样传给 handler —— 但凡 handler 用到 ``ctx.db`` /
    ``ctx.broker_manager`` 就抛 ``'str' object has no attribute 'db'``。

    实测（2026-09-19）：``get_alerts_rules`` / ``get_notifications`` 等**凡是依赖 ctx 的
    自动工具全部调不通**，而协议层握手与 ``tools/list`` 全部正常 —— 所以只看握手
    「PASS」是发现不了的（这正是 :mod:`scripts.verify_packaged_mcp` 早先漏掉它的原因）。
    """
    # ⚠️ 不能用 ``isinstance(d, Depends)``：新版 FastAPI 的 ``Depends`` 是**函数**
    # 而非类，``isinstance`` 会抛 ``TypeError: arg 2 must be a type`` —— 而这段跑在
    # 工具注册循环里，一抛就是**整个自动暴露失败**（实测工具数 120 → 68）。
    # 改用鸭子类型：注入项的共同特征是带 ``.dependency`` 可调用属性。
    d = p.default
    if d is None:
        return False
    return hasattr(d, "dependency") and callable(getattr(d, "dependency", None))


def _di_callable(p):
    """取出依赖注入项的可调用对象（``Depends(get_ctx)`` → ``get_ctx``）。"""
    dep = getattr(p.default, "dependency", None)
    return dep if callable(dep) else None


def _make_tool_func(endpoint, cap, name: str):
    """动态构造一个签名与 handler 对齐的 async 函数，注册为 MCP tool。

    **依赖注入参数不进签名**：它们不是调用方该提供的入参，改为在调用时由
    ``_DI`` 表解析后注入（见 :func:`_is_di_param` 的踩坑记录）。
    """
    params = [p for p in cap.param_schema if not _is_di_param(p)]
    di = {p.name: _di_callable(p) for p in cap.param_schema
          if _is_di_param(p) and _di_callable(p) is not None}

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

    # 依赖注入解析：同步/异步依赖都支持（get_ctx 为同步零参）。
    di_block = ""
    if di:
        di_block = (
            "    for _n, _f in _DI.items():\n"
            "        _v = _f()\n"
            "        if _isawaitable(_v):\n"
            "            _v = await _v\n"
            "        _kwargs[_n] = _v\n"
        )

    src = (
        f"async def {name}({sig}):\n"
        f"    _kwargs = {dict_literal}\n"
        f"    _kwargs = {{k: v for k, v in _kwargs.items() if v is not None}}\n"
        + di_block +
        f"    _res = await _ENDPOINT(**_kwargs)\n"
        f"    if isinstance(_res, dict) and 'code' in _res and 'data' in _res:\n"
        f"        return _res['data'] if _res['code'] == 0 else _res\n"
        f"    return _res\n"
    )
    ns: dict = {"_ENDPOINT": endpoint, "_DI": di,
                "_isawaitable": inspect.isawaitable}
    try:
        exec(compile(src, f"<auto_tool:{name}>", "exec"), ns)  # noqa: S102 —— 编译自注册表元数据，非用户输入
    except SyntaxError as exc:  # noqa: BLE001
        raise RuntimeError(f"生成自动 tool 失败 {name}: {exc}\n{src}") from exc
    func = ns[name]
    func.__doc__ = cap.summary or name
    func.__name__ = name
    return func


def _get_existing_tool_names(mcp) -> set[str]:
    """获取已注册的 tool 名称集合（H2 版本守卫）。

    FastMCP 各版本的 tool 管理内部结构不同：
    - 2.14.x: mcp._tool_manager._tools (dict, 同步可读)
    - >= 2.15: get_tools() 可能改为同步返回
    - 未来: 可能完全私有化

    策略：依次尝试私有属性 → 公共 API → 空集降级（碰撞检测跳过，不影响功能）。
    """
    # 方式1：FastMCP 2.14.x 的私有属性
    try:
        tm = getattr(mcp, "_tool_manager", None)
        if tm is not None:
            tools = getattr(tm, "_tools", None)
            if tools is not None and isinstance(tools, dict):
                return set(tools.keys())
    except Exception:  # noqa: BLE001
        pass

    # 方式2：尝试公共 get_tools() API（同步版本）
    try:
        gt = getattr(mcp, "get_tools", None)
        if gt is not None:
            import inspect
            if not inspect.iscoroutinefunction(gt):
                result = gt()
                if isinstance(result, dict):
                    return set(result.keys())
                if isinstance(result, (list, set)):
                    names = set()
                    for t in result:
                        name = getattr(t, "name", None) or (t if isinstance(t, str) else None)
                        if name:
                            names.add(name)
                    return names
    except Exception:  # noqa: BLE001
        pass

    # 方式3：降级为空集 —— 碰撞检测失效，但功能不受影响
    # 同名 tool 注册会被 FastMCP 拒绝（覆盖），仅日志告警
    import logging
    logging.getLogger("qmt_work.mcp").debug(
        "无法获取已注册 tool 列表（FastMCP 版本兼容降级），碰撞检测跳过")
    return set()


def register_auto_tools(mcp) -> int:
    """把 agent_visible 的 GET 端点注册为 MCP tool。返回新增工具数。"""
    existing = _get_existing_tool_names(mcp)

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
