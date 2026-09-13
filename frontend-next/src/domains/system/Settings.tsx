import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  Input,
  Panel,
  Spinner,
  Tabs,
} from "@/design/primitives";
import { systemApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { useUiStore, type ThemePref } from "@/stores/ui";
import type { ConfigHistoryRow, RiskConfig, RuntimeConfig } from "@/shared/types";
import s from "../domain.module.css";

function asText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** 把输入串还原为「数字/布尔/字符串」——后端 set_many 会做类型校验，前端只做合理猜测 */
function parseValue(raw: string, fallback: unknown): unknown {
  const t = raw.trim();
  if (t === "") return "";
  if (typeof fallback === "number") {
    const n = Number(t);
    return Number.isNaN(n) ? t : n;
  }
  if (typeof fallback === "boolean") {
    if (t === "true" || t === "1") return true;
    if (t === "false" || t === "0") return false;
  }
  return t;
}

/**
 * 设置：运行时引擎参数（热更新） + 风控参数。
 *
 * ★ 契约要点（config.py）：
 *   - GET /config/runtime 返回 {参数名: {value, default, desc, ...}}，本页按此结构渲染
 *   - PUT /config/runtime 只提交**改动过的**键；后端校验类型与下限，非法返回 400
 *   - 历史在 /config/runtime/history 的 rows 里；回滚 body 是 {id}（**不是** version）
 *   - 风控：GET/PUT /config/risk；/config/risk/daily 给实时用量与熔断状态；
 *     /config/risk/circuit 支持 action=trip|reset（手动熔断/解除）
 *
 * 热更新意味着改完立即影响在跑的引擎，本页对「保存」做显式提示。
 */
export function Settings() {
  const [tab, setTab] = useState<"engine" | "risk" | "ui">("engine");

  const cfg = useAsync<RuntimeConfig>(() => systemApi.config(), []);
  const risk = useAsync<RiskConfig>(() => systemApi.riskConfig(), []);
  const hist = useAsync<{ rows: ConfigHistoryRow[] }>(() => systemApi.configHistory(50), []);

  const [draft, setDraft] = useState<Record<string, string>>({});
  const [riskDraft, setRiskDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const entries = useMemo(() => Object.entries(cfg.data ?? {}), [cfg.data]);

  const changed = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(draft)) {
      const original = cfg.data?.[k]?.value;
      const next = parseValue(v, original);
      if (asText(original) !== asText(next)) out[k] = next;
    }
    return out;
  }, [draft, cfg.data]);

  const riskChanged = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(riskDraft)) {
      const original = (risk.data as Record<string, unknown> | null)?.[k];
      if (typeof original !== "object") {
        const next = parseValue(v, original);
        if (asText(original) !== asText(next)) out[k] = next;
      }
    }
    return out;
  }, [riskDraft, risk.data]);

  const saveEngine = async () => {
    if (Object.keys(changed).length === 0) {
      setBanner({ tone: "error", text: "没有检测到改动" });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const res = await systemApi.updateConfig(changed);
      setBanner({ tone: "ok", text: `已热更新 ${res.changed.length} 项：${res.changed.join(", ")}` });
      setDraft({});
      await Promise.all([cfg.reload(), hist.reload()]);
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const saveRisk = async () => {
    if (Object.keys(riskChanged).length === 0) {
      setBanner({ tone: "error", text: "没有检测到改动" });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const res = await systemApi.updateRiskConfig(riskChanged);
      setBanner({ tone: "ok", text: `风控参数已保存：${res.changed.join(", ")}` });
      setRiskDraft({});
      await risk.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const circuit = async (action: "trip" | "reset") => {
    if (action === "trip" && !window.confirm("手动熔断将立即暂停一切买入开仓，确认？")) return;
    setBusy(true);
    setBanner(null);
    try {
      await systemApi.riskCircuit(action);
      setBanner({ tone: "ok", text: action === "trip" ? "已手动熔断" : "已解除熔断" });
      await risk.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const rollback = async (id: number) => {
    setBusy(true);
    setBanner(null);
    try {
      await systemApi.rollbackConfig(id);
      setBanner({ tone: "ok", text: `已回滚到历史记录 #${id}` });
      setDraft({});
      await Promise.all([cfg.reload(), hist.reload()]);
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const daily = risk.data?.daily;

  return (
    <div className={s.page}>
      <Tabs
        items={[
          { key: "engine", label: "引擎参数（热更新）" },
          { key: "risk", label: "风控参数" },
          { key: "ui", label: "界面偏好" },
        ]}
        value={tab}
        onChange={setTab}
      />

      {banner && (
        <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>{banner.text}</div>
      )}

      {tab === "engine" ? (
        <>
          <div className={s.toolbar}>
            <span className={s.muted}>
              改动 {Object.keys(changed).length} 项 —— 保存后**立即生效**，无需重启
            </span>
            <span className={s.spacer} />
            <Button size="sm" disabled={busy || Object.keys(changed).length === 0} onClick={() => void saveEngine()}>
              保存改动
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setDraft({});
                void cfg.reload();
              }}
            >
              放弃
            </Button>
          </div>

          <Panel flush className={s.grow} title={`运行时参数（${entries.length}）`}>
            <div className={s.scroll}>
              {cfg.loading && !cfg.data ? (
                <Spinner label="加载中…" />
              ) : cfg.error ? (
                <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
                  {cfg.error}
                </div>
              ) : entries.length === 0 ? (
                <EmptyState text="无可配置项" />
              ) : (
                <div className={s.cols3} style={{ padding: 8 }}>
                  {entries.map(([k, spec]) => {
                    const cur = draft[k] ?? asText(spec.value);
                    const dirty = k in changed;
                    return (
                      <div key={k} className={s.stat}>
                        <span className={s.statLabel} title={spec.desc ?? k}>
                          {k}
                        </span>
                        <Input
                          value={cur}
                          mono
                          onChange={(e) => setDraft((p) => ({ ...p, [k]: e.target.value }))}
                        />
                        <span className={s.statSub}>
                          默认 {asText(spec.default)}
                          {dirty && <b style={{ color: "var(--warning)" }}> · 已改动</b>}
                        </span>
                        {spec.desc && (
                          <span className={s.statSub} style={{ whiteSpace: "normal", lineHeight: 1.4 }}>
                            {spec.desc}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </Panel>

          <Panel flush title={`变更历史（${hist.data?.rows.length ?? 0}）`}>
            <div className={s.scroll} style={{ maxHeight: 200 }}>
              {(hist.data?.rows.length ?? 0) === 0 ? (
                <div className={s.muted} style={{ padding: 8 }}>
                  暂无变更历史
                </div>
              ) : (
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "var(--font-sm)" }}>
                  <tbody>
                    {hist.data?.rows.map((r) => (
                      <tr key={r.id} style={{ borderBottom: "1px solid var(--border)" }}>
                        <td className={s.mono} style={{ padding: "3px 8px", width: 60 }}>
                          #{r.id}
                        </td>
                        <td className={s.mono} style={{ padding: "3px 8px", width: 170 }}>
                          {r.created_at ?? ""}
                        </td>
                        <td className={s.mono} style={{ padding: "3px 8px", width: 200 }}>
                          {r.key ?? ""}
                        </td>
                        <td className={s.mono} style={{ padding: "3px 8px" }}>
                          {asText(r.old_value)} → {asText(r.new_value)}
                        </td>
                        <td style={{ padding: "3px 8px", width: 70 }}>
                          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void rollback(r.id)}>
                            回滚
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </Panel>
        </>
      ) : tab === "risk" ? (
        <>
          <div className={s.toolbar}>
            <Badge tone={daily?.circuit_tripped ? "danger" : "success"}>
              {daily?.circuit_tripped ? "熔断中" : "正常"}
            </Badge>
            {daily?.circuit_reason && <span className={s.muted}>原因：{daily.circuit_reason}</span>}
            <span className={s.spacer} />
            <Button size="sm" variant="danger" disabled={busy} onClick={() => void circuit("trip")}>
              手动熔断
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => void circuit("reset")}>
              解除熔断
            </Button>
            <Button size="sm" disabled={busy || Object.keys(riskChanged).length === 0} onClick={() => void saveRisk()}>
              保存风控参数
            </Button>
          </div>

          <Panel title="风控参数">
            {risk.loading && !risk.data ? (
              <Spinner label="加载中…" />
            ) : risk.error ? (
              <div className={`${s.note} ${s.noteError}`}>{risk.error}</div>
            ) : (
              <div className={s.cols3}>
                {Object.entries(risk.data ?? {})
                  .filter(([, v]) => typeof v !== "object")
                  .map(([k, v]) => (
                    <div key={k} className={s.stat}>
                      <span className={s.statLabel}>{k}</span>
                      <Input
                        value={riskDraft[k] ?? asText(v)}
                        mono
                        onChange={(e) => setRiskDraft((p) => ({ ...p, [k]: e.target.value }))}
                      />
                      {k in riskChanged && <span className={s.statSub} style={{ color: "var(--warning)" }}>已改动</span>}
                    </div>
                  ))}
              </div>
            )}
          </Panel>

          {daily && (
            <Panel title="日级用量与熔断状态">
              <div className={s.kv}>
                {Object.entries(daily)
                  .filter(([, v]) => typeof v !== "object")
                  .map(([k, v]) => (
                    <div key={k} style={{ display: "contents" }}>
                      <span className={s.kvKey}>{k}</span>
                      <span className={s.kvVal}>{asText(v)}</span>
                    </div>
                  ))}
              </div>
            </Panel>
          )}
        </>
      ) : (
        <UiPrefs />
      )}
    </div>
  );
}

const THEME_OPTIONS: Array<{ key: ThemePref; label: string; hint: string }> = [
  { key: "auto", label: "跟随系统", hint: "随操作系统浅色/深色自动切换" },
  { key: "dark", label: "深色", hint: "专业终端深色，长时间盯盘更省眼" },
  { key: "light", label: "浅色", hint: "明亮环境 / 投影演示" },
];

/**
 * 界面偏好：主题三态 + 涨跌配色。
 *
 * 这里的选择与状态栏右下角的快捷开关是同一份状态（stores/ui.ts），
 * 落盘键 `qmt.ui.v1`，刷新后保持。主题通过 <html data-theme> 驱动设计令牌，
 * 组件本身不感知明暗，因此切换是零成本的。
 */
function UiPrefs() {
  const themePref = useUiStore((st) => st.themePref);
  const setThemePref = useUiStore((st) => st.setThemePref);
  const theme = useUiStore((st) => st.theme);
  const updown = useUiStore((st) => st.updown);
  const setUpdown = useUiStore((st) => st.setUpdown);

  return (
    <>
      <Panel title="主题">
        <div className={s.toolbar}>
          {THEME_OPTIONS.map((o) => (
            <Button
              key={o.key}
              size="sm"
              variant={themePref === o.key ? "primary" : "ghost"}
              onClick={() => setThemePref(o.key)}
              title={o.hint}
            >
              {o.label}
            </Button>
          ))}
          <span className={s.spacer} />
          <span className={s.muted}>
            当前生效：<b>{theme === "dark" ? "深色" : "浅色"}</b>
            {themePref === "auto" && "（跟随系统）"}
          </span>
        </div>
      </Panel>

      <Panel title="涨跌配色">
        <div className={s.toolbar}>
          <Button
            size="sm"
            variant={updown === "red-up" ? "primary" : "ghost"}
            onClick={() => setUpdown("red-up")}
            title="A 股习惯"
          >
            红涨绿跌
          </Button>
          <Button
            size="sm"
            variant={updown === "green-up" ? "primary" : "ghost"}
            onClick={() => setUpdown("green-up")}
            title="国际市场习惯"
          >
            绿涨红跌
          </Button>
          <span className={s.spacer} />
          <span className={s.muted}>独立于明暗主题，仅影响行情涨跌色</span>
        </div>
      </Panel>
    </>
  );
}

export default Settings;
