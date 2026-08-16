"""显式工具注册表（阶段 5.3：tool schema 解耦）。

工具 = 名称 + 描述 + 参数 JSON schema + 异步 handler + 权限级别。
Agent 与 MCP 共用同一注册表，避免散落各处的函数签名漂移。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict            # JSON schema（properties/required/type）
    handler: callable           # async (args: dict) -> JSON-serializable
    permission: str = "read"     # read | trade | admin


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, parameters: dict,
                 handler, permission: str = "read") -> None:
        self._tools[name] = Tool(
            name=name, description=description, parameters=parameters,
            handler=handler, permission=permission)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self) -> list[dict]:
        return [{
            "name": t.name, "description": t.description,
            "parameters": t.parameters, "permission": t.permission,
        } for t in self._tools.values()]

    async def call(self, name: str, arguments: dict):
        t = self._tools.get(name)
        if t is None:
            raise KeyError(f"未知工具：{name}")
        return await t.handler(arguments)
