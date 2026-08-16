"""LLM Provider 抽象：OpenAI 兼容 / Anthropic。

统一接口 `chat(messages, tools) -> dict`：
- messages: [{role, content}]（role ∈ system/user/assistant）
- tools: 显式工具注册表导出的工具定义列表
- 返回 {role, content, tool_calls?: [{id, name, arguments_json}]}

缺 API Key 时构造即抛 `AgentNotConfigured`（端点转 503），绝不返回假数据。
网络调用走 httpx.AsyncClient，真实请求真实 LLM；本模块不含任何 mock。
"""
from __future__ import annotations

import json

from .errors import AgentNotConfigured


class Provider:
    kind = "base"

    def __init__(self, api_key: str = "", model: str = "", base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    async def chat(self, messages: list[dict], tools=None) -> dict:
        raise NotImplementedError


class OpenAIProvider(Provider):
    kind = "openai"

    def __init__(self, api_key: str = "", model: str = "gpt-4o-mini",
                 base_url: str = "https://api.openai.com/v1"):
        if not api_key:
            raise AgentNotConfigured("OpenAI API Key 未配置")
        super().__init__(api_key, model, base_url)

    async def chat(self, messages: list[dict], tools=None) -> dict:
        import httpx

        payload = {
            "model": self.model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"], "description": t["description"],
                    "parameters": t["parameters"]}}
                for t in tools
            ]
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                self.base_url.rstrip("/") + "/chat/completions",
                json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> dict:
        msg = data["choices"][0]["message"]
        out = {"role": "assistant", "content": msg.get("content") or ""}
        tc = msg.get("tool_calls")
        if tc:
            out["tool_calls"] = [{
                "id": c["id"], "name": c["function"]["name"],
                "arguments_json": c["function"]["arguments"],
            } for c in tc]
        return out


class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(self, api_key: str = "",
                 model: str = "claude-3-5-sonnet-20241022",
                 base_url: str = "https://api.anthropic.com"):
        if not api_key:
            raise AgentNotConfigured("Anthropic API Key 未配置")
        super().__init__(api_key, model, base_url)

    async def chat(self, messages: list[dict], tools=None) -> dict:
        import httpx

        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        conv = [{"role": m["role"], "content": m["content"]}
                for m in messages if m["role"] != "system"]
        payload = {
            "model": self.model, "max_tokens": 4096,
            "system": system, "messages": conv,
        }
        if tools:
            payload["tools"] = [{
                "name": t["name"], "description": t["description"],
                "input_schema": t["parameters"],
            } for t in tools]
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                self.base_url.rstrip("/") + "/v1/messages",
                json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return self._parse(data)

    @staticmethod
    def _parse(data: dict) -> dict:
        blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        out = {"role": "assistant", "content": text}
        tcs = [b for b in blocks if b.get("type") == "tool_use"]
        if tcs:
            out["tool_calls"] = [{
                "id": b["id"], "name": b["name"],
                "arguments_json": json.dumps(b.get("input", {}), ensure_ascii=False),
            } for b in tcs]
        return out


def build_provider(kind: str, api_key: str = "", model: str = "",
                   base_url: str = "") -> Provider:
    if kind == "openai":
        return OpenAIProvider(
            api_key=api_key, model=model or "gpt-4o-mini",
            base_url=base_url or "https://api.openai.com/v1")
    if kind == "anthropic":
        return AnthropicProvider(
            api_key=api_key, model=model or "claude-3-5-sonnet-20241022",
            base_url=base_url or "https://api.anthropic.com")
    raise ValueError(f"未知 provider: {kind}")
