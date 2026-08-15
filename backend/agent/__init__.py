"""Agent Core + LLM Provider 抽象（§4.8/§4.3 Phase 4）。

- Provider 抽象：统一 chat_stream(messages, tools) -> AsyncIterator[event]
- OpenAI 兼容适配器：任意 base_url + /chat/completions（CodeBuddy/DeepSeek/通义/智谱/Kimi/Ollama/vLLM/自定义）
- Anthropic 适配器：/v1/messages（Claude，tool_use 格式差异处理）
- Agent Core：标准 ReAct 循环，工具复用同进程 FastMCP 工具（mcp.get_tools()）
- 未配置 LLM 时返回友好提示（零强制，不影响传统页面/MCP/REST）
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

log = logging.getLogger("qmt_work.agent")


@dataclass
class LLMConfig:
    provider: str = "openai"      # openai | anthropic
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    timeout: float = 60.0


# ---------- Provider 抽象 ----------

class LLMProvider:
    async def chat_stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[dict]:
        """yield: {"type":"text","delta":str} | {"type":"tool_call","id","name","arguments":dict} | {"type":"done","finish_reason"}"""
        raise NotImplementedError


def _openai_tool_spec(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
    }} for t in tools]


class OpenAICompatProvider(LLMProvider):
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    async def chat_stream(self, messages, tools):
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.cfg.model, "messages": messages,
            "temperature": self.cfg.temperature, "stream": True,
        }
        if tools:
            payload["tools"] = _openai_tool_spec(tools)
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        yield {"type": "text", "delta": delta["content"]}
                    for tc in delta.get("tool_calls") or []:
                        yield {"type": "tool_call",
                               "id": tc.get("id") or "",
                               "name": (tc.get("function") or {}).get("name") or "",
                               "arguments": (tc.get("function") or {}).get("arguments") or ""}
                    if choices[0].get("finish_reason"):
                        yield {"type": "done", "finish_reason": choices[0]["finish_reason"]}


class AnthropicProvider(LLMProvider):
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    async def chat_stream(self, messages, tools):
        url = self.cfg.base_url.rstrip("/") + "/messages"
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        claude_msgs = [{"role": m["role"], "content": m["content"]}
                       for m in messages if m["role"] in ("user", "assistant")]
        payload = {"model": self.cfg.model, "max_tokens": 4096,
                   "temperature": self.cfg.temperature, "stream": True,
                   "messages": claude_msgs}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                                 "input_schema": t.get("input_schema", {"type": "object", "properties": {}})}
                                for t in tools]
        headers = {"x-api-key": self.cfg.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        cur_tool = {}
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("event: content_block_delta"):
                        pass
                    elif line.startswith("data:"):
                        try:
                            evt = json.loads(line[5:])
                        except Exception:
                            continue
                        dt = evt.get("delta") or {}
                        if evt.get("type") == "content_block_delta" and dt.get("type") == "text_delta":
                            yield {"type": "text", "delta": dt.get("text", "")}
                        elif dt.get("type") == "input_json_delta":
                            cur_tool.setdefault("arguments", "")
                            cur_tool["arguments"] += dt.get("partial_json", "")
                        elif evt.get("type") == "content_block_start" and dt.get("type") == "tool_use":
                            cur_tool = {"id": dt.get("id") or "", "name": dt.get("name") or "",
                                        "arguments": ""}
                    elif line.startswith("event: message_delta"):
                        yield {"type": "done", "finish_reason": "stop"}
                if cur_tool.get("name"):
                    yield {"type": "tool_call", "id": cur_tool.get("id", ""),
                           "name": cur_tool.get("name", ""), "arguments": cur_tool.get("arguments", "{}")}
                if not cur_tool:
                    yield {"type": "done", "finish_reason": "stop"}


def build_provider(cfg: LLMConfig) -> LLMProvider | None:
    if not cfg.api_key or not cfg.model or not cfg.base_url:
        return None
    if cfg.provider == "anthropic":
        return AnthropicProvider(cfg)
    return OpenAICompatProvider(cfg)


# ---------- Agent Core（ReAct，工具复用同进程 FastMCP 工具）----------

class AgentCore:
    MAX_ITERS = 8

    def __init__(self, provider: LLMProvider, tools: list):
        self.provider = provider
        self.tools = tools
        self._tool_map = {t.name: t for t in tools}

    def tool_specs(self) -> list[dict]:
        specs = []
        for t in self.tools:
            schema = getattr(t, "parameters", None)
            if schema is None:
                schema = getattr(t, "input_model", None)
            if hasattr(schema, "model_json_schema"):
                schema = schema.model_json_schema()
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            specs.append({"name": t.name, "description": getattr(t, "description", ""),
                          "input_schema": schema})
        return specs

    async def _execute_tool(self, name: str, arguments: str) -> str:
        tool = self._tool_map.get(name)
        if tool is None:
            return json.dumps({"ok": False, "reason": f"unknown tool {name}"}, ensure_ascii=False)
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        except Exception:
            args = {}
        try:
            result = await tool.fn(**args)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False)

    async def run(self, user_message: str, session_id: str = "") -> AsyncIterator[dict]:
        """SSE 事件：text / tool_call / tool_result / done"""
        system = (
            "你是 QMT 量化 Agent。可用工具负责行情查询、下单撤单、账户查询、回测与多方案对比。"
            "交易类操作（place_order/cancel_order）必须先向用户确认方向、数量、价格后再执行。"
            "回答简洁，涉及数字给具体值。"
        )
        messages: list[dict] = [{"role": "user", "content": user_message}]
        for _ in range(self.MAX_ITERS):
            collected: list[dict] = []
            full_text = ""
            tool_calls: list[dict] = []
            async for evt in self.provider.chat_stream(messages, self.tool_specs()):
                etype = evt.get("type")
                if etype == "text":
                    full_text += evt["delta"]
                    yield {"type": "text", "delta": evt["delta"]}
                elif etype == "tool_call":
                    name = evt.get("name", "")
                    if name:
                        tool_calls.append(evt)
                        yield {"type": "tool_call", "name": name,
                               "arguments": evt.get("arguments", "")}
            if not tool_calls:
                if full_text:
                    messages.append({"role": "assistant", "content": full_text})
                else:
                    full_text = "（未配置 LLM Provider 或模型未返回内容）"
                    yield {"type": "text", "delta": full_text}
                yield {"type": "done", "message": full_text}
                return
            # 执行工具并回填
            assistant_msg = {"role": "assistant", "content": full_text or None,
                             "tool_calls": [{"id": tc["id"], "type": "function",
                                             "function": {"name": tc["name"],
                                                          "arguments": tc.get("arguments", "{}")}}
                                            for tc in tool_calls]}
            messages.append(assistant_msg)
            for tc in tool_calls:
                result = await self._execute_tool(tc["name"], tc.get("arguments", ""))
                yield {"type": "tool_result", "name": tc["name"], "result": result}
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        yield {"type": "done", "message": "已达到最大工具调用轮数，请简化任务后重试。"}
