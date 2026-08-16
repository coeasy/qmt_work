import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { useBroker } from "../BrokerContext.jsx";

/* 阶段 5 智能助手（LLM Agent）：基于真实券商/运行期数据对话。
   缺 LLM 配置（agent_enabled=false 或空 key）后端返回 503，前端显示引导横幅，绝不造假回复。 */

export default function Agent() {
  const { activeId, brokers } = useBroker();
  const [status, setStatus] = useState(null);       // {enabled, configured, ...}
  const [sessions, setSessions] = useState([]);
  const [curId, setCurId] = useState(null);
  const [messages, setMessages] = useState([]);     // [{role, content}]
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState(null);
  const endRef = useRef(null);

  const refreshStatus = async () => {
    try { setStatus(await api.agentStatus()); } catch { setStatus({ configured: false }); }
  };
  const refreshSessions = async () => {
    try { const r = await api.agentSessions(); setSessions(r.sessions || []); } catch { /* ignore */ }
  };

  useEffect(() => { refreshStatus(); refreshSessions(); }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const openSession = async (id) => {
    setCurId(id);
    try {
      const r = await api.agentGetSession(id);
      setMessages((r.session?.messages || []).map((m) => ({
        role: m.role === "user" && m.content?.startsWith("[工具") ? "tool" : m.role,
        content: m.content,
      })));
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  };

  const newSession = async () => {
    try {
      const r = await api.agentCreateSession({ title: "" });
      setCurId(r.session_id);
      setMessages([]);
      await refreshSessions();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput(""); setLoading(true); setMsg(null);
    const next = [...messages, { role: "user", content: text }];
    setMessages(next);
    try {
      const r = await api.agentChat({ message: text, session_id: curId || undefined,
        conn_id: activeId || undefined });
      setCurId(r.session_id);
      setMessages([...next, { role: "assistant", content: r.answer }]);
      if (curId == null) await refreshSessions();
    } catch (e) {
      setMessages(next);
      setMsg({ ok: false, t: e.message });
    } finally { setLoading(false); }
  };

  const del = async (id) => {
    try {
      await api.agentDeleteSession(id);
      if (curId === id) { setCurId(null); setMessages([]); }
      await refreshSessions();
    } catch (e) { setMsg({ ok: false, t: e.message }); }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h2>智能助手</h2>
        <p>阶段 5 · LLM Agent 基于真实券商/运行期数据对话；未配置 LLM Key 时显示引导，绝不返回假回复。</p>
      </div>

      {status && !status.configured && (
        <div className="msg error" style={{ marginBottom: 16 }}>
          智能助手未配置：请在「设置」中开启 <b>agent_enabled</b> 并填写 LLM API Key（OpenAI / Anthropic 兼容）。
          当前所有对话请求将返回 503 引导。
        </div>
      )}

      <div className="card" style={{ display: "flex", gap: 16, maxWidth: 1100, minHeight: 520 }}>
        {/* 左：会话列表 */}
        <div style={{ width: 220, borderRight: "1px solid #22304a", paddingRight: 12 }}>
          <div className="btn-group" style={{ marginBottom: 12 }}>
            <button className="btn-primary btn-sm" onClick={newSession}>新建会话</button>
          </div>
          <div className="session-list">
            {sessions.length === 0 && <div className="metric-help">暂无历史会话</div>}
            {sessions.map((s) => (
              <div key={s.id} className={`session-item ${curId === s.id ? "active" : ""}`}
                onClick={() => openSession(s.id)}>
                <div className="session-title">{s.title || `会话 #${s.id}`}</div>
                <button className="btn-sm danger" onClick={(e) => { e.stopPropagation(); del(s.id); }}>
                  删
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* 右：对话 */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
          <div className="chat-log" style={{ flex: 1, overflowY: "auto", padding: 8 }}>
            {messages.length === 0 && (
              <div className="metric-help">问点什么吧：例如「当前账户持仓和可用资金是多少？」「列出支持的券商。」</div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`bubble ${m.role}`} style={{ marginBottom: 10 }}>
                <div className="bubble-meta">{m.role === "user" ? "你" : m.role === "assistant" ? "助手" : "工具"}</div>
                <div className="bubble-body">{m.content}</div>
              </div>
            ))}
            {loading && <div className="metric-help">助手思考中…</div>}
            <div ref={endRef} />
          </div>
          {msg && <div className={`msg ${msg.ok ? "success" : "error"}`}>{msg.t}</div>}
          <div className="chat-input" style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <input value={input} placeholder="输入消息，Enter 发送"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") send(); }} />
            <button className="btn-primary" onClick={send} disabled={loading || !status?.configured}>
              {loading ? "发送中…" : "发送"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
