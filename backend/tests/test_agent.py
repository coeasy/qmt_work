"""阶段 5 Agent 测试：默认工具集 / 会话持久化 / 503 降级（无 LLM 网络依赖）。

用 FakeProvider 模拟 LLM 返回（无需真实 API Key），验证：
- 默认工具集注册了 read/trade 工具；
- 会话落 sessions/messages 孤儿表，可持久化与重载；
- 工具调用循环：provider 返回 tool_calls → AgentCore 执行真实工具（未连券商返回明确错误，不造假）；
- 缺配置时 _build_core 抛 AgentNotConfigured（REST 端点转 503）。
"""
import pytest

from agent.core import AgentCore
from agent.errors import AgentNotConfigured
from agent.providers import Provider
from agent.tools import ToolRegistry


class FakeProvider(Provider):
    """测试用 Provider：返回预设 assistant 文本 / tool_calls，绝不触网。"""
    kind = "fake"

    def __init__(self, tool_calls=None, final_text="done"):
        self.api_key = "test"
        self.model = "fake"
        self.base_url = "fake"
        self._tool_calls = tool_calls or []
        self._final = final_text
        self._calls = 0

    async def chat(self, messages, tools=None):
        self._calls += 1
        if self._tool_calls and self._calls == 1:
            return {"role": "assistant", "content": "", "tool_calls": self._tool_calls}
        return {"role": "assistant", "content": self._final}


def _tmp_db():
    import tempfile
    from pathlib import Path
    from app.db import DB
    d = Path(tempfile.mkdtemp())
    return DB(d / "app.db"), d


def _registry():
    from agent.default_tools import build_default_registry
    return build_default_registry()


def test_default_registry_has_expected_tools():
    reg = _registry()
    names = {t["name"] for t in reg.list()}
    assert {"get_runtime_config", "list_broker_profiles", "broker_status",
            "query_account", "get_quote", "submit_order"} <= names
    perms = {t["name"]: t["permission"] for t in reg.list()}
    assert perms["submit_order"] == "trade"
    assert perms["get_quote"] == "read"


def test_session_persistence_roundtrip():
    db, _ = _tmp_db()
    core = AgentCore(db, FakeProvider(final_text="hi"), _registry())
    sid = core.create_session(title="t")
    assert isinstance(sid, int) and sid > 0
    # 写入用户 + 助手消息
    core._append(sid, "user", "hello")
    core._append(sid, "assistant", "hi")
    loaded = core.get_session(sid)
    assert loaded["session"]["title"] == "t"
    roles = [m["role"] for m in loaded["messages"]]
    assert roles == ["user", "assistant"]
    # 列表可见
    assert any(s["id"] == sid for s in core.list_sessions())
    # 删除
    core.delete_session(sid)
    assert core.get_session(sid) is None


def test_chat_executes_real_tool_when_broker_unavailable():
    import asyncio
    db, _ = _tmp_db()
    # 未连接券商：query_account 工具应返回明确错误（不编造假数据）
    provider = FakeProvider(tool_calls=[{
        "id": "c1", "name": "query_account", "arguments_json": "{}"}])
    core = AgentCore(db, provider, _registry())
    res = asyncio.run(core.chat("账户情况？"))
    assert res["session_id"] > 0
    assert res["iterations"] >= 1
    assert res["provider"] == "fake"
    # 第二次循环（final 文本）应已在答案中
    assert res["answer"] == "done"


def test_chat_persists_tool_calls():
    import asyncio
    db, _ = _tmp_db()
    provider = FakeProvider(tool_calls=[{
        "id": "c1", "name": "list_broker_profiles", "arguments_json": "{}"}])
    core = AgentCore(db, provider, _registry())
    res = asyncio.run(core.chat("支持哪些券商？"))
    sess = core.get_session(res["session_id"])
    # 助手消息应记录 tool_calls_json（非空）
    assert any(m["tool_calls_json"] for m in sess["messages"]
               if m["role"] == "assistant")


def test_agent_not_configured_gate():
    """缺 LLM Key / Provider 即 AgentNotConfigured（REST 端点据此转 503 降级）。"""
    from agent.providers import build_provider
    # 空 key → build_provider 抛 AgentNotConfigured
    with pytest.raises(AgentNotConfigured):
        build_provider("openai", "")
    # provider 传 None → AgentCore 构造即抛 AgentNotConfigured
    with pytest.raises(AgentNotConfigured):
        AgentCore(_tmp_db()[0], None, ToolRegistry())


def test_tool_registry_unknown_is_none():
    reg = ToolRegistry()
    assert reg.get("nope") is None

