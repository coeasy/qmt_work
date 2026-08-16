"""阶段 5 Agent：LLM 决策 × 真实券商通道。

重建（非深化）：原 agent/ 后端已删，会话落预留孤儿表 sessions/messages/llm_config。
设计原则（与全局一致）：
- 真实 LLM Provider（OpenAI 兼容 / Anthropic），缺 API Key 即 503 降级，绝不造假回复；
- 显式工具注册表（名称/参数 schema/权限），降低对 FastMCP 内部结构升级脆性；
- 会话持久化到 SQLite 孤儿表，前端可加载历史会话。
"""
from .core import AgentCore, SYSTEM_PROMPT
from .errors import AgentNotConfigured
from .providers import AnthropicProvider, OpenAIProvider, Provider, build_provider
from .tools import Tool, ToolRegistry

__all__ = [
    "AgentCore", "SYSTEM_PROMPT", "AgentNotConfigured",
    "Provider", "OpenAIProvider", "AnthropicProvider", "build_provider",
    "Tool", "ToolRegistry",
]
