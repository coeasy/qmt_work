import { useState } from "react";
import { Badge, Button, FormRow, Input, Panel, Select } from "@/design/primitives";
import { researchApi, type ManyFactorsResult } from "@/services/api";
import { ConnSelect } from "./ConnSelect";
import s from "../domain.module.css";

/**
 * 因子 / 指标库（P1）：15 类技术指标，支持手动输入序列或基于真实券商 K 线计算。
 * 移植自旧 frontend/features/research/Factors.jsx。
 */

const INDICATORS: Array<{ name: string; label: string; extra: string; default: string }> = [
  { name: "sma", label: "SMA 简单均线", extra: "周期", default: "20" },
  { name: "ema", label: "EMA 指数均线", extra: "周期", default: "20" },
  { name: "rsi", label: "RSI 相对强弱", extra: "周期", default: "14" },
  { name: "macd", label: "MACD", extra: "快/慢/信号", default: "12,26,9" },
  { name: "boll", label: "BOLL 布林带", extra: "周期/标准差", default: "20,2" },
  { name: "atr", label: "ATR 真实波幅", extra: "周期", default: "14" },
  { name: "adx", label: "ADX 趋势强弱", extra: "周期", default: "14" },
  { name: "cci", label: "CCI 顺势指标", extra: "周期", default: "20" },
  { name: "kdj", label: "KDJ", extra: "周期", default: "9" },
  { name: "obv", label: "OBV 能量潮", extra: "", default: "" },
  { name: "vol_ma", label: "量MA", extra: "周期", default: "20" },
  { name: "returns", label: "收益率", extra: "", default: "" },
  { name: "log_returns", label: "对数收益率", extra: "", default: "" },
  { name: "zscore", label: "ZScore", extra: "周期", default: "20" },
  { name: "roc", label: "ROC 变动率", extra: "周期", default: "10" },
];

const MODELS = [
  { key: "kline", label: "基于券商 K 线" },
  { key: "manual", label: "手动输入数据" },
];

function parseNums(str: string): number[] {
  return str
    .split(/[,\s\n]+/)
    .map(Number)
    .filter((x) => !Number.isNaN(x));
}

export function FactorsPanel() {
  const [mode, setMode] = useState<"kline" | "manual">("kline");
  const [connId, setConnId] = useState("");
  const [selected, setSelected] = useState<string[]>(["sma", "rsi", "macd"]);
  const [symbol, setSymbol] = useState("");
  const [period, setPeriod] = useState("1d");
  const [count, setCount] = useState("250");
  const [raw, setRaw] = useState("");
  const [results, setResults] = useState<ManyFactorsResult | null>(null);
  const [resultInfo, setResultInfo] = useState<{ symbol?: string; period?: string; count?: number; source?: string } | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; t: string } | null>(null);
  const [loading, setLoading] = useState(false);

  function toggle(name: string) {
    setSelected((prev) =>
      prev.includes(name) ? prev.filter((x) => x !== name) : [...prev, name],
    );
  }

  async function handleCompute() {
    setLoading(true);
    setMsg(null);
    setResults(null);
    setResultInfo(null);
    try {
      const names = selected.filter(Boolean);
      if (names.length === 0) throw new Error("请至少选择一个指标");
      if (mode === "manual") {
        const vals = parseNums(raw);
        if (vals.length < 5) throw new Error("请至少输入 5 个数值，逗号分隔");
        const r = await researchApi.computeManyFactors({ names, values: vals });
        setResults(r);
      } else {
        if (!symbol) throw new Error("请输入股票代码");
        const r = await researchApi.factorFromKline({
          symbol,
          names,
          period,
          count: Number(count) || 250,
          broker_id: connId || undefined,
        });
        setResults(r.values);
        setResultInfo({ symbol: r.symbol, period: r.period, count: r.count, source: r.source });
      }
    } catch (e) {
      setMsg({ ok: false, t: e instanceof Error ? e.message : String(e) });
    } finally {
      setLoading(false);
    }
  }

  const periods = ["1m", "5m", "15m", "30m", "60m", "1d"] as const;

  return (
    <div className={s.page}>
      <Panel title="因子 / 指标库" padded>
        <p className={s.muted} style={{ marginTop: 0 }}>
          15 类技术指标（基于真实 K 线或手动输入数据）；指标计算收敛至后端单一真源，前端不自算。
        </p>

        <div className={s.form}>
          <FormRow label="模式">
            <Select
              value={mode}
              onChange={(e) => setMode(e.target.value as "kline" | "manual")}
              options={MODELS.map((m) => ({ value: m.key, label: m.label }))}
            />
          </FormRow>

          {mode === "kline" && (
            <>
              <div className={s.inline}>
                <FormRow label="股票代码">
                  <Input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="600519.SH" />
                </FormRow>
                <FormRow label="K 线周期">
                  <Select
                    value={period}
                    onChange={(e) => setPeriod(e.target.value)}
                    options={periods.map((p) => ({ value: p, label: p }))}
                  />
                </FormRow>
                <FormRow label="数据条数">
                  <Input mono value={count} onChange={(e) => setCount(e.target.value)} />
                </FormRow>
                <FormRow label="活跃连接">
                  <ConnSelect value={connId} onChange={setConnId} />
                </FormRow>
              </div>
            </>
          )}

          {mode === "manual" && (
            <FormRow label="收盘价序列（逗号/换行分隔，至少 5 个）">
              <textarea
                rows={4}
                value={raw}
                onChange={(e) => setRaw(e.target.value)}
                placeholder="12.5,12.8,13.1,12.9,13.5,14.0,13.7,14.2,14.5,14.1,14.8,15.0,14.6,15.2,15.5"
                className={s.mono}
                style={{
                  width: "100%",
                  background: "var(--bg-3)",
                  border: "1px solid var(--border)",
                  borderRadius: "var(--radius-sm)",
                  color: "var(--text)",
                  padding: 8,
                }}
              />
            </FormRow>
          )}

          <FormRow label="选择指标">
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {INDICATORS.map((ind) => (
                <Button
                  key={ind.name}
                  size="sm"
                  variant={selected.includes(ind.name) ? "primary" : "default"}
                  onClick={() => toggle(ind.name)}
                >
                  {ind.label}
                </Button>
              ))}
            </div>
          </FormRow>

          <div className={s.actions}>
            <Button
              variant="primary"
              onClick={handleCompute}
              disabled={loading || selected.length === 0}
            >
              {loading ? "计算中…" : "计算指标"}
            </Button>
          </div>
        </div>

        {msg && (
          <div className={msg.ok ? s.noteOk : s.noteError} style={{ marginTop: 12 }}>
            {msg.t}
          </div>
        )}

        {results && (
          <div style={{ marginTop: 16 }}>
            <div className={s.toolbar}>
              <strong>计算结果</strong>
              {resultInfo?.symbol && (
                <Badge tone="info">
                  {resultInfo.symbol} · {resultInfo.period} · {resultInfo.count} 条
                </Badge>
              )}
              {resultInfo?.source && <Badge tone="neutral">{resultInfo.source}</Badge>}
            </div>
            <div className={s.scroll} style={{ marginTop: 8 }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr style={{ color: "var(--text-dim)", textAlign: "left" }}>
                    <th style={{ padding: "4px 6px" }}>指标</th>
                    <th>最新值</th>
                    <th>数据点数</th>
                    <th>前 10 个值</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(results).map(([name, data]) => {
                    const arr = Array.isArray(data) ? data : [];
                    const latest = arr.length ? arr[arr.length - 1] : null;
                    return (
                      <tr key={name} style={{ borderBottom: "1px solid var(--border)" }}>
                        <td style={{ padding: "3px 6px" }}>
                          <span className="tag">{name}</span>
                        </td>
                        <td>
                          <strong>{latest !== null && latest !== undefined ? (latest as number).toFixed(4) : "N/A"}</strong>
                        </td>
                        <td>{arr.length}</td>
                        <td style={{ fontSize: 11, color: "var(--text-dim)" }}>
                          {arr
                            .slice(0, 10)
                            .map((v) => (typeof v === "number" ? v.toFixed(3) : String(v)))
                            .join(", ")}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
