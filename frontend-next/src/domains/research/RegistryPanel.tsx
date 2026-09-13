import { useEffect, useState } from "react";
import { Button, FormRow, Input, Panel, Select } from "@/design/primitives";
import { researchApi, type FactorInfo } from "@/services/api";
import s from "../domain.module.css";

/**
 * 因子注册表：后端统一指标引擎（app/indicators）的注册表浏览与单指标计算入口。
 * 指标计算已收敛后端单一真源，前端不自算。移植自旧 frontend/components/FactorRegistry.jsx。
 */

export function RegistryPanel() {
  const [items, setItems] = useState<FactorInfo[]>([]);
  const [err, setErr] = useState("");

  const [name, setName] = useState("");
  const [code, setCode] = useState("600519.SH");
  const [period, setPeriod] = useState("1d");
  const [count, setCount] = useState("120");
  const [win, setWin] = useState("20");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const [msg, setMsg] = useState<{ ok: boolean; t: string } | null>(null);

  useEffect(() => {
    researchApi
      .factorsList()
      .then((r) => setItems(Array.isArray(r) ? r : []))
      .catch((e) => setErr(e instanceof Error ? e.message : "因子清单暂不可用"));
  }, []);

  async function compute() {
    setBusy(true);
    setMsg(null);
    setResult(null);
    try {
      const params: Record<string, unknown> = {};
      if (win) params.win = Number(win) || undefined;
      // 新后端：基于真实 K 线的单指标计算走 /factors/from-kline（names 可含单个指标）
      const r = await researchApi.factorFromKline({
        symbol: code.trim(),
        names: [name.trim()],
        period,
        count: Number(count) || 120,
        params,
      });
      setResult(r);
      setMsg({ ok: true, t: "计算完成" });
    } catch (e) {
      setMsg({ ok: false, t: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  const periods = ["1d", "1w", "60m", "30m", "15m", "5m", "1m"] as const;

  return (
    <div className={s.page}>
      <Panel title="因子注册表" padded>
        <p className={s.muted} style={{ marginTop: 0 }}>
          后端统一指标引擎（app/indicators）的注册表浏览与单指标计算入口；指标计算已收敛后端单一真源，前端不自算。
        </p>

        <div className={s.form}>
          <FormRow label="指标名（如 ma / macd / boll）">
            <Input
              list="factor-names"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="指标名"
            />
            <datalist id="factor-names">
              {items.map((f) => (
                <option key={f.name} value={f.name} />
              ))}
            </datalist>
          </FormRow>
          <FormRow label="代码">
            <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="600519.SH" />
          </FormRow>
          <FormRow label="周期">
            <Select value={period} onChange={(e) => setPeriod(e.target.value)} options={periods.map((p) => ({ value: p, label: p }))} />
          </FormRow>
          <FormRow label="K 线根数">
            <Input mono value={count} onChange={(e) => setCount(e.target.value)} />
          </FormRow>
          <FormRow label="窗口参数 win（可选）">
            <Input mono value={win} onChange={(e) => setWin(e.target.value)} />
          </FormRow>
          <div className={s.actions}>
            <Button variant="primary" disabled={busy || !name.trim() || !code.trim()} onClick={compute}>
              {busy ? "计算中…" : "计算"}
            </Button>
          </div>
        </div>

        {err && <div className={s.noteError} style={{ marginTop: 12 }}>{err}</div>}
        {msg && <div className={msg.ok ? s.noteOk : s.noteError} style={{ marginTop: 12 }}>{msg.t}</div>}
        {result !== null && (
          <details open style={{ marginTop: 8 }}>
            <summary style={{ cursor: "pointer" }}>计算结果</summary>
            <pre className={s.mono} style={{ fontSize: 11, maxHeight: 260, overflow: "auto" }}>
              {JSON.stringify(result, null, 2)}
            </pre>
          </details>
        )}

        <div style={{ marginTop: 16 }}>
          <div className={s.muted}>已注册指标（{items.length}）</div>
          <div className={s.scroll} style={{ marginTop: 4 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
                  <th style={{ padding: "4px 6px" }}>名称</th>
                  <th>说明</th>
                  <th>默认参数</th>
                </tr>
              </thead>
              <tbody>
                {items.map((f, i) => (
                  <tr key={(f.name || String(i))} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "3px 6px" }} className="code">{f.name}</td>
                    <td>{f.desc || f.label || "—"}</td>
                    <td className="code">{f.params ? JSON.stringify(f.params) : (f.default_params ? JSON.stringify(f.default_params) : "—")}</td>
                  </tr>
                ))}
                {!items.length && !err && (
                  <tr><td colSpan={3} className={s.muted}>加载中…</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </Panel>
    </div>
  );
}
