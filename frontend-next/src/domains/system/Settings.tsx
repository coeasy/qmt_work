import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  ConfirmModal,
  EmptyState,
  Input,
  Panel,
  Spinner,
  Tabs,
} from "@/design/primitives";
import { systemApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import { CUSTOM_SKIN_ID, DEFAULT_SKIN_ID, PRESETS, presetById } from "@/design/skins";
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
  /**
   * 危险操作的统一确认（熔断 / 配置回滚）。
   *
   * ★ 为什么替换掉 `window.confirm`：原生弹窗不受应用主题与皮肤控制（深色界面里
   * 弹出一个白框）、文案无法承载「影响范围」说明、且在项目规范里被明确列为
   * 资金/不可逆动作的禁用写法（应走 ConfirmModal / ConfirmButton）。
   * 回滚此前更是**零确认**直接生效 —— 运行时配置被改错只能靠重新改回来。
   */
  const [confirm, setConfirm] = useState<{
    title: string;
    message: string;
    warn?: string;
    run: () => Promise<void>;
  } | null>(null);

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

  /** 熔断的真实执行体（确认逻辑在 circuit 里，分开写避免确认后递归再弹一次） */
  const doCircuit = async (action: "trip" | "reset") => {
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

  const circuit = (action: "trip" | "reset") => {
    if (action === "trip") {
      setConfirm({
        title: "手动熔断",
        message: "熔断将立即暂停一切买入开仓，直到手动解除。",
        warn: "影响全部策略与手动下单",
        run: () => doCircuit("trip"),
      });
      return;
    }
    void doCircuit("reset");
  };

  /** 配置回滚的真实执行体 */
  const doRollback = async (id: number) => {
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

  /** 回滚会**直接改写运行时配置**（此前零确认即生效），必须两段式 */
  const rollback = (id: number) => {
    setConfirm({
      title: "回滚运行时配置",
      message: `将把运行时配置整体回滚到历史记录 #${id}，当前未保存的改动会丢失。`,
      warn: "立即生效，不可撤销",
      run: () => doRollback(id),
    });
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
              改动 {Object.keys(changed).length} 项 —— 保存后立即生效，无需重启
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

      <ConfirmModal
        open={confirm !== null}
        title={confirm?.title ?? ""}
        danger
        confirmText="确认执行"
        message={confirm?.message ?? null}
        warn={confirm?.warn}
        onConfirm={() => {
          const run = confirm?.run;
          setConfirm(null);
          if (run) void run();
        }}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}

const THEME_OPTIONS: Array<{ key: ThemePref; label: string; hint: string }> = [
  { key: "auto", label: "跟随系统", hint: "随操作系统浅色/深色自动切换" },
  { key: "dark", label: "深色", hint: "专业终端深色，长时间盯盘更省眼" },
  { key: "light", label: "浅色", hint: "明亮环境 / 投影演示" },
];

/**
 * 界面偏好：主题三态 + 背景配色（皮肤） + 涨跌配色。
 *
 * 这里的选择与状态栏右下角的快捷开关是同一份状态（stores/ui.ts），
 * 落盘键 `qmt.ui.v1`，刷新后保持。主题通过 <html data-theme> 驱动设计令牌，
 * 皮肤通过 <html data-skin>（预设）或内联变量（自定义）驱动，组件本身不感知明暗，
 * 因此切换是零成本的。
 */
function UiPrefs() {
  const themePref = useUiStore((st) => st.themePref);
  const setThemePref = useUiStore((st) => st.setThemePref);
  const theme = useUiStore((st) => st.theme);
  const updown = useUiStore((st) => st.updown);
  const setUpdown = useUiStore((st) => st.setUpdown);
  const activeSkin = useUiStore((st) => st.activeSkin);
  const customBg = useUiStore((st) => st.customBg);
  const setSkin = useUiStore((st) => st.setSkin);
  const setCustomBg = useUiStore((st) => st.setCustomBg);

  // hex 输入框的草稿：只有合法值才即时应用，非法值在失焦时回滚，避免半截输入把界面刷黑
  const [bgDraft, setBgDraft] = useState(customBg);
  const applyBg = (raw: string) => {
    const v = raw.trim();
    if (/^#[0-9a-f]{6}$/i.test(v)) setCustomBg(v.toLowerCase());
    else setBgDraft(customBg);
  };

  const activeLabel =
    activeSkin === CUSTOM_SKIN_ID
      ? `自定义 ${customBg}`
      : (presetById(activeSkin)?.label ?? "默认（随主题）");

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

      <Panel title="背景配色">
        <div className={s.skinGrid}>
          {PRESETS.map((p) => {
            const on = activeSkin === p.id;
            return (
              <button
                key={p.id}
                type="button"
                className={[s.skinCard, on ? s.skinCardActive : ""].filter(Boolean).join(" ")}
                onClick={() => setSkin(p.id)}
                title={p.hint}
                aria-pressed={on}
              >
                <span className={s.skinSwatch} style={{ background: p.swatch }} aria-hidden />
                <span className={s.skinName}>{p.label}{p.id === DEFAULT_SKIN_ID ? "（默认）" : ""}</span>
              </button>
            );
          })}
        </div>

        <div className={s.toolbar} style={{ marginTop: 8 }}>
          <span className={s.muted}>自定义背景色</span>
          <input
            type="color"
            className={s.colorInput}
            value={customBg}
            aria-label="自定义背景色"
            onChange={(e) => {
              setBgDraft(e.target.value);
              setCustomBg(e.target.value.toLowerCase());
            }}
          />
          <Input
            value={bgDraft}
            mono
            style={{ width: 96 }}
            aria-label="自定义背景色 HEX"
            onChange={(e) => setBgDraft(e.target.value)}
            onBlur={() => applyBg(bgDraft)}
            onKeyDown={(e) => {
              if (e.key === "Enter") applyBg(bgDraft);
            }}
          />
          <span className={s.spacer} />
          <span className={s.muted}>
            当前生效：<b>{activeLabel}</b>
          </span>
        </div>

        <div className={s.muted} style={{ marginTop: 6, whiteSpace: "normal", lineHeight: 1.5 }}>
          预设配色提供五套深色（极夜黑、石墨黑、曜石黑、石板蓝、墨绿）与一套浅色（晨曦白，默认），
          命名与任何第三方软件无关。
          自定义背景色时会按背景明暗<b>自动配套</b>文字与边框色，不会出现「白底白字」；
          涨跌色与强调色<b>不受</b>背景色影响，仍是独立设置。
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
          <span className={s.muted}>独立于明暗主题与背景配色，仅影响行情涨跌色</span>
        </div>
      </Panel>

      <UiCloudSync />
    </>
  );
}

/**
 * 外观的**云端同步 + 强调色可调**（P1-G）。
 *
 * 缺口背景：accent 此前写死在 ``design/tokens.css``、界面完全不能调；外观又只存在
 * localStorage，换台机器或清一次缓存就回到默认皮肤 —— 用户会以为「软件把我的设置丢了」。
 *
 * 设计取舍：
 * - **服务器是权威**，本机 localStorage 是离线缓存，两者冲突时以服务器为准；
 * - **不自动覆盖本机**：从服务器拉到值后要用户点「应用到本机」才生效，
 *   否则一进设置页界面就自己变色，比不生效更吓人；
 * - accent 非法值**清空**而不是保留旧值（保留会让用户以为保存成功但没生效）。
 */
function UiCloudSync() {
  const accent = useUiStore((st) => st.accent);
  const setAccent = useUiStore((st) => st.setAccent);
  const skin = useUiStore((st) => st.skin);
  const customBg = useUiStore((st) => st.customBg);

  const remote = useAsync(() => systemApi.uiAppearance(), []);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);
  const [draft, setDraft] = useState(accent);

  const say = (tone: "ok" | "error" | "warn", text: string) => setNote({ tone, text });

  const save = async () => {
    setBusy(true);
    try {
      const r = await systemApi.updateUiAppearance({
        skin_id: skin || DEFAULT_SKIN_ID,
        // skin=custom 时把自定义背景色一并带上，否则服务器上的皮肤 id 无法还原颜色
        accent: accent || "",
        density: remote.data?.appearance.density || "comfortable",
      });
      say("ok", `已保存到服务器（改动字段：${r.changed.join("、") || "无"}）`);
      await remote.reload();
    } catch (e) {
      say("error", `保存失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const restore = async () => {
    const ap = remote.data?.appearance;
    if (!ap) return;
    setAccent(ap.accent || "");
    setDraft(ap.accent || "");
    say("ok", `已应用服务器上的外观（皮肤 ${ap.skin_id || "默认"}）`);
  };

  const exportJson = async () => {
    try {
      const blobData = await systemApi.exportUiAppearance();
      const blob = new Blob([JSON.stringify(blobData, null, 2)], {
        type: "application/json",
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "qmt-ui-appearance.json";
      a.click();
      URL.revokeObjectURL(a.href);
      say("ok", "已导出 qmt-ui-appearance.json");
    } catch (e) {
      say("error", `导出失败：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const importJson = async (file: File) => {
    setBusy(true);
    try {
      const parsed = JSON.parse(await file.text()) as Record<string, unknown>;
      const r = await systemApi.importUiAppearance(parsed);
      setAccent(r.appearance.accent || "");
      setDraft(r.appearance.accent || "");
      say("ok", `已导入并应用（字段：${r.changed.join("、")}）`);
      await remote.reload();
    } catch (e) {
      say("error", `导入失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="强调色与云端同步">
      <div className={s.toolbar}>
        <span className={s.muted}>强调色</span>
        <input
          type="color"
          className={s.colorInput}
          aria-label="强调色"
          value={accent || "#3b82f6"}
          onChange={(e) => {
            setDraft(e.target.value);
            setAccent(e.target.value);
          }}
        />
        <Input
          value={draft}
          mono
          style={{ width: 96 }}
          aria-label="强调色 HEX"
          placeholder="#3b82f6"
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => setAccent(draft)}
          onKeyDown={(e) => {
            if (e.key === "Enter") setAccent(draft);
          }}
        />
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => setAccent("")}>
          用默认色
        </Button>
        <span className={s.spacer} />
        <span className={s.muted}>当前：{accent || "主题默认"}</span>
      </div>

      <div className={s.toolbar} style={{ marginTop: 8 }}>
        <Button size="sm" variant="primary" disabled={busy} onClick={() => void save()}>
          保存到服务器
        </Button>
        <Button size="sm" disabled={busy || !remote.data} onClick={() => void restore()}>
          从服务器恢复
        </Button>
        <Button size="sm" variant="ghost" onClick={() => void exportJson()}>
          导出 JSON
        </Button>
        <label className={s.muted} style={{ cursor: "pointer" }}>
          导入 JSON
          <input
            type="file"
            accept="application/json,.json"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void importJson(f);
              e.target.value = "";
            }}
          />
        </label>
        <span className={s.spacer} />
        <span className={s.muted}>
          {remote.error
            ? `服务器外观读取失败：${remote.error}（仍可本地使用）`
            : remote.data
              ? `服务器：皮肤 ${remote.data.appearance.skin_id || "默认"} · 强调色 ${
                  remote.data.appearance.accent || "默认"
                }`
              : "读取中…"}
        </span>
      </div>

      {note ? (
        <div
          className={
            note.tone === "ok" ? s.noteOk : note.tone === "warn" ? s.noteWarn : s.noteError
          }
          style={{ marginTop: 6 }}
        >
          {note.text}
        </div>
      ) : null}

      <div className={s.muted} style={{ marginTop: 6, whiteSpace: "normal", lineHeight: 1.5 }}>
        外观此前只存在浏览器本地，换机器 / 清缓存即丢。现在服务器保存一份权威配置，
        可在另一台机器上「导入 JSON」还原；自定义背景色（
        {skin === "custom" ? customBg : "当前未使用自定义"}）请连同皮肤一并保存，
        否则只还原皮肤 id 无法还原颜色。
      </div>
    </Panel>
  );
}

export default Settings;
