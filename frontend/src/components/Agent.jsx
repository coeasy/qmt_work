import { useEffect, useRef, useState } from "react";
import { agentChat } from "../api.js";

export default function Agent() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const logRef = useRef(null);

  useEffect(() => {
    logRef.current && (logRef.current.scrollTop = logRef.current.scrollHeight);
  }, [messages]);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setMessages((m) => [...m, { role: "user", content: text }]);
    setInput(""); setBusy(true);
    const aiMsg = { role: "agent", content: "", tool: "" };
    setMessages((m) => [...m, aiMsg]);
    try {
      await agentChat(text, (evt) => {
        const d = evt.data || {};
        if (evt.event === "text" || evt.event === "message") {
          setMessages((m) => m.map((x, i) => (i === m.length - 1 && x.role === "agent"
            ? { ...x, content: x.content + (d.delta || "") } : x)));
        } else if (evt.event === "tool_call") {
          setMessages((m) => m.map((x, i) => (i === m.length - 1
            ? { ...x, tool: `🔧 ${d.tool} ${d.args ? JSON.stringify(d.args) : ""}` } : x)));
        } else if (evt.event === "done") {
          setBusy(false);
        } else if (evt.event === "error") {
          setMessages((m) => m.map((x, i) => (i === m.length - 1
            ? { ...x, content: x.content + "\n[错误] " + d.message } : x)));
          setBusy(false);
        }
      });
    } catch (e) {
      setMessages((m) => m.map((x, i) => (i === m.length - 1 ? { ...x, content: x.content + "\n[错误] " + e.message } : x)));
      setBusy(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h2 className="page-title">Agent 对话</h2>
      <p className="page-sub">自然语言驱动回测 / 行情 / 下单（未配置 LLM 时给出引导提示）</p>
      <div className="chat">
        <div className="chat-log" ref={logRef}>
          {messages.length === 0 && <p className="muted">试试：「帮我回测 600519.SH」 / 「查一下 000001.SZ 的行情」</p>}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.tool && <div className="muted" style={{ marginBottom: 4, fontSize: 12 }}>{m.tool}</div>}
              <div style={{ whiteSpace: "pre-wrap" }}>{m.content || "…"}</div>
            </div>
          ))}
        </div>
        <div className="chat-input">
          <input value={input} placeholder="输入指令…" disabled={busy}
            onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()} />
          <button onClick={send} disabled={busy}>{busy ? "思考中…" : "发送"}</button>
        </div>
      </div>
    </div>
  );
}
