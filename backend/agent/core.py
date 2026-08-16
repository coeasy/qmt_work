"""Agent 核心：会话持久化（复用孤儿表）+ 工具调用循环。

- 会话落 sessions / messages（阶段 5 重建，非深化）；
- 对话循环：调用 LLM Provider → 若有 tool_calls 则依次执行注册表工具 → 注入结果
  → 再次调用，直至模型不再请求工具或达到 max_iterations；
- 真实 LLM，缺 Provider/Key 构造 AgentCore 即抛 AgentNotConfigured（端点转 503）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .errors import AgentNotConfigured
from .tools import ToolRegistry

SYSTEM_PROMPT = (
    "你是 qmt_work 量化交易平台的智能助手。你可以调用工具查询真实行情、账户、"
    "券商档案与运行状态，并基于这些真实数据给出分析建议。严禁编造数据；"
    "若工具未返回所需信息，应明确说明无法获取。"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentCore:
    def __init__(self, db, provider, tool_registry: ToolRegistry,
                 system_prompt: str = SYSTEM_PROMPT, max_iterations: int = 4):
        if provider is None:
            raise AgentNotConfigured("Agent 未配置：缺少 LLM Provider / API Key")
        self.db = db
        self.provider = provider
        self.provider_kind = getattr(provider, "kind", "unknown")
        self.tools = tool_registry
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations

    # ---------------- 会话持久化 ----------------
    def create_session(self, title: str = "", llm_snapshot: str = "") -> int:
        return self.db.insert("sessions", {
            "user_id": None, "title": title,
            "llm_config_snapshot": llm_snapshot, "created_at": _now()})

    def list_sessions(self) -> list[dict]:
        return self.db.query(
            "SELECT id,title,llm_config_snapshot,created_at FROM sessions ORDER BY id DESC")

    def get_session(self, sid: int) -> dict | None:
        s = self.db.query_one(
            "SELECT id,title,llm_config_snapshot,created_at FROM sessions WHERE id=?",
            (sid,))
        if s is None:
            return None
        msgs = self.db.query(
            "SELECT role,content,tool_calls_json,created_at FROM messages "
            "WHERE session_id=? ORDER BY id ASC", (sid,))
        return {"session": dict(s), "messages": [dict(m) for m in msgs]}

    def delete_session(self, sid: int) -> None:
        self.db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))

    def _append(self, sid: int, role: str, content: str,
                tool_calls_json: str = "") -> None:
        self.db.insert("messages", {
            "session_id": sid, "role": role, "content": content,
            "tool_calls_json": tool_calls_json, "created_at": _now()})

    # ---------------- 对话循环 ----------------
    async def chat(self, message: str, session_id: int | None = None,
                   conn_id: str | None = None) -> dict:
        if session_id is None:
            session_id = self.create_session(
                title=(message or "")[:40], llm_snapshot=self.provider_kind)

        messages = [{"role": "system", "content": self.system_prompt}]
        for m in self.db.query(
                "SELECT role,content FROM messages WHERE session_id=? ORDER BY id ASC",
                (session_id,)):
            messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": message})

        tool_defs = self.tools.list()
        last_content = ""
        iterations = 0
        while iterations < self.max_iterations:
            iterations += 1
            resp = await self.provider.chat(messages, tool_defs)
            content = resp.get("content", "") or ""
            tc = resp.get("tool_calls")
            tc_json = json.dumps(tc, ensure_ascii=False) if tc else ""
            self._append(session_id, "assistant", content, tc_json)
            messages.append({"role": "assistant", "content": content})
            last_content = content
            if not tc:
                break
            # 顺序执行工具调用（并行 tool_use 为后续增强）
            for call in tc:
                name = call["name"]
                try:
                    args = json.loads(call.get("arguments_json") or "{}")
                except Exception:  # noqa: BLE001
                    args = {}
                try:
                    result = await self.tools.call(name, args)
                    result_str = json.dumps(result, ensure_ascii=False, default=str)
                except Exception as exc:  # noqa: BLE001
                    result_str = f"工具执行错误：{exc}"
                tool_msg = f"[工具 {name} 返回]\n{result_str}"
                messages.append({"role": "user", "content": tool_msg})
                self._append(session_id, "user", tool_msg)

        return {
            "session_id": session_id,
            "answer": last_content,
            "iterations": iterations,
            "provider": self.provider_kind,
        }
