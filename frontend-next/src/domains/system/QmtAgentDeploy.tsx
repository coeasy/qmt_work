import { useCallback, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  Input,
  Panel,
} from "@/design/primitives";
import { ConfirmModal } from "@/design/primitives/Modal";
import {
  qmtAgentApi,
  type QmtAgentBundle,
  type QmtAgentCapability,
  type QmtAgentConfigResponse,
  type QmtAgentDeployResult,
  type QmtAgentDiagnoseResult,
  type QmtAgentDistributeCheckResult,
  type QmtAgentDistributePullResult,
  type QmtAgentStatus,
  type QmtAgentToolsResponse,
} from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import s from "../domain.module.css";

/**
 * 大 QMT 策略桥 agent —— 一键部署 / 诊断。
 *
 * 设计要点（2026-10-08 R27）
 * --------------------------
 * 1. **状态 → 部署 → 诊断** 三步闭环：所有写操作都必须能立即验证结果，
 *    避免「点了部署但不知道落哪」的假成功。
 * 2. **预览必须走 dry_run**：预览按钮不写盘，只是让后端生成 bundle
 *    计算一次并返回路径 + 大小。
 * 3. **诊断 problems[] 必须显式红色呈现**：后端 problems 出现即视为失败；
 *    静默吞掉 = 让下次事故无从追溯。
 * 4. **工具链自检**：`agent_tools_available=false` 时部署按钮禁用，明确
 *    提示「客户端安装包缺少工具链，请重装最新版」。
 * 5. **零 mock**：所有数据来自后端 `/qmt-agent/*`，本页不生成任何 bundle
 *    文本、不模拟任何判据。
 * 6. **能力真相必须显式呈现**（2026-10-09 真机教训）：诊断面板此前只列
 *    bundle/config/心跳，于是「agent 自检报了 `quote_call` 异常、
 *    且本进程根本拿不到下单函数」时界面照样显示「一切正常」——
 *    用户只能盯着永远空白的行情面板猜。现在把 `capabilities.trading` /
 *    `capabilities.quote` 与自检结论直接摊开，并区分
 *    「运行模式使然」（灰/黄）与「真故障」（红）。
 */

/** 能力一行：「可用 / 不可用（运行模式使然）/ 不可用」。 */
function CapabilityRow({ label, cap }: { label: string; cap?: QmtAgentCapability }) {
  if (!cap) {
    return (
      <>
        <dt>{label}</dt>
        <dd>
          <Badge tone="neutral">未上报</Badge>
        </dd>
      </>
    );
  }
  const tone = cap.available ? "success" : cap.expected_in_mode ? "warning" : "danger";
  const text = cap.available ? "可用" : cap.expected_in_mode ? "不可用（运行模式使然）" : "不可用";
  return (
    <>
      <dt>{label}</dt>
      <dd>
        <Badge tone={tone}>{text}</Badge>
        {/* 只报「不可用」等于没说：必须把 agent 给的真因带上，用户才知道下一步点哪 */}
        {cap.available ? null : <span style={{ marginLeft: 6 }}>{cap.reason}</span>}
      </dd>
    </>
  );
}

export default function QmtAgentDeploy() {
  const status = useAsync(() => qmtAgentApi.status(), []);
  const [qmtDir, setQmtDir] = useState("");
  const [filename, setFilename] = useState("qmt_work_agent.py");
  const [strategy, setStrategy] = useState("qmt_work_agent");
  const [dryRun, setDryRun] = useState(true);
  const [txtCopy, setTxtCopy] = useState(true);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [confirm, setConfirm] = useState<{
    title: string;
    message: string;
    warn?: string;
    run: () => Promise<void>;
  } | null>(null);
  const [lastDeploy, setLastDeploy] = useState<QmtAgentDeployResult | null>(null);
  const [lastDiagnose, setLastDiagnose] = useState<QmtAgentDiagnoseResult | null>(null);

  // ---- 远程下发（bundle 分发）----
  const distStatus = useAsync(() => qmtAgentApi.distributeStatus(), []);
  const [distUrl, setDistUrl] = useState("");
  const [distDryRun, setDistDryRun] = useState(true);
  const [distBusy, setDistBusy] = useState(false);
  const [lastCheck, setLastCheck] = useState<QmtAgentDistributeCheckResult | null>(null);
  const [lastPull, setLastPull] = useState<QmtAgentDistributePullResult | null>(null);

  // ---- bundle 源码预览 / agent_config 编辑 / 工具链清单 ----
  const [bundle, setBundle] = useState<QmtAgentBundle | null>(null);
  const [bundleBusy, setBundleBusy] = useState(false);
  const [cfg, setCfg] = useState<QmtAgentConfigResponse | null>(null);
  const [cfgText, setCfgText] = useState("");
  const [cfgBusy, setCfgBusy] = useState(false);
  const [tools, setTools] = useState<QmtAgentToolsResponse | null>(null);
  const [toolsBusy, setToolsBusy] = useState(false);

  const runBundle = useCallback(async () => {
    setBundleBusy(true);
    setBanner(null);
    try {
      const res = await qmtAgentApi.bundle();
      setBundle(res);
      setBanner({
        tone: res.problems.length ? "error" : "ok",
        text: res.problems.length
          ? `bundle 生成有问题：${res.problems.join("; ")}`
          : `bundle 已生成：${res.size_bytes} B（${res.encoding}，未写盘）`,
      });
    } catch (e: any) {
      setBanner({ tone: "error", text: e.message || "bundle 生成失败" });
    } finally {
      setBundleBusy(false);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    setCfgBusy(true);
    setBanner(null);
    try {
      const res = await qmtAgentApi.getConfig(qmtDir || undefined);
      setCfg(res);
      setCfgText(JSON.stringify(res.data ?? {}, null, 2));
      setBanner({
        tone: "ok",
        text: res.exists
          ? `已读取：${res.path}`
          : `目标未存在，显示的是模板${res.template_path ? `（${res.template_path}）` : ""}`,
      });
    } catch (e: any) {
      setBanner({ tone: "error", text: e.message || "读取配置失败" });
    } finally {
      setCfgBusy(false);
    }
  }, [qmtDir]);

  const runSaveConfig = useCallback(async () => {
    setCfgBusy(true);
    setBanner(null);
    try {
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(cfgText || "{}");
      } catch {
        setBanner({ tone: "error", text: "JSON 解析失败，未写盘" });
        return;
      }
      if (typeof parsed !== "object" || Array.isArray(parsed) || parsed === null) {
        setBanner({ tone: "error", text: "agent_config.json 必须是 JSON 对象，未写盘" });
        return;
      }
      const res = await qmtAgentApi.saveConfig(parsed, qmtDir || undefined);
      setBanner({ tone: "ok", text: `已写入 ${res.path}（${res.written} B，已备份旧版）` });
      status.reload();
    } catch (e: any) {
      setBanner({ tone: "error", text: e.message || "保存配置失败" });
    } finally {
      setCfgBusy(false);
    }
  }, [cfgText, qmtDir, status]);

  const runTools = useCallback(async () => {
    setToolsBusy(true);
    try {
      setTools(await qmtAgentApi.tools());
    } catch (e: any) {
      setBanner({ tone: "error", text: `工具链清单加载失败：${e.message}` });
    } finally {
      setToolsBusy(false);
    }
  }, []);

  const runDeploy = useCallback(async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await qmtAgentApi.deploy({
        qmt_dir: qmtDir || undefined,
        filename,
        strategy,
        dry_run: dryRun,
        txt_copy: txtCopy,
      });
      setLastDeploy(res);
      const summary = dryRun
        ? `预览完成：${res.path}（${res.size_bytes} B，未写盘）`
        : `已部署：${res.path}（${res.size_bytes} B，编码 ${res.encoding}${res.backup ? "，已备份旧版" : ""}）`;
      setBanner({
        tone: res.problems.length ? "error" : "ok",
        text: summary + (res.problems.length ? ` ⚠ ${res.problems.join("; ")}` : ""),
      });
      status.reload();
    } catch (e: any) {
      setBanner({ tone: "error", text: e.message || "部署失败" });
    } finally {
      setBusy(false);
    }
  }, [qmtDir, filename, strategy, dryRun, txtCopy, status]);

  const runDiagnose = useCallback(async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await qmtAgentApi.diagnose({
        qmt_dir: qmtDir || undefined,
        strategy,
      });
      setLastDiagnose(res);
      setBanner({
        tone: res.ok ? "ok" : "error",
        text: res.ok ? "诊断通过，agent 一切正常" : `诊断发现 ${res.problems.length} 个问题`,
      });
    } catch (e: any) {
      setBanner({ tone: "error", text: e.message || "诊断失败" });
    } finally {
      setBusy(false);
    }
  }, [qmtDir, strategy]);

  const runDistCheck = useCallback(async () => {
    setDistBusy(true);
    try {
      const res = await qmtAgentApi.distributeCheck(distUrl || undefined);
      setLastCheck(res);
      if (res.has_update) {
        setBanner({ tone: "ok", text: `发现更新：本地 v${res.local_version} → 远程 v${res.remote_version}` });
      } else {
        setBanner({ tone: "ok", text: `当前已是最新（v${res.local_version}）` });
      }
    } catch (e: any) {
      setBanner({ tone: "error", text: `检测失败：${e.message}` });
    } finally {
      setDistBusy(false);
    }
  }, [distUrl]);

  const runDistPull = useCallback(async () => {
    setDistBusy(true);
    try {
      const res = await qmtAgentApi.distributePull({
        url: distUrl || undefined,
        qmt_dir: qmtDir || undefined,
        dry_run: distDryRun,
      });
      setLastPull(res);
      setBanner({
        tone: "ok",
        text: distDryRun
          ? `拉取预览：${res.path}（${res.size_bytes} B，未写盘）`
          : `已拉取部署：${res.path}（${res.size_bytes} B${res.backup ? "，已备份旧版" : ""}）`,
      });
      status.reload();
    } catch (e: any) {
      setBanner({ tone: "error", text: `拉取失败：${e.message}` });
    } finally {
      setDistBusy(false);
    }
  }, [distUrl, qmtDir, distDryRun, status]);

  const d: QmtAgentStatus | null = status.data;
  const toolsReady = !!(d && d.agent_tools_available && d.agent_bigqmt_available);

  return (
    <div className={s.page}>
      <Panel title="当前状态">
        {status.error ? (
          <EmptyState text={`状态加载失败：${status.error}`} />
        ) : !d ? (
          <span className={s.muted}>加载中…</span>
        ) : (
          <dl className={s.grid2}>
            <dt>QMT 安装目录</dt>
            <dd>
              {d.qmt_dir ? <code>{d.qmt_dir}</code> : <Badge tone="warning">未探测到</Badge>}
            </dd>
            <dt>QMT 进程</dt>
            <dd>
              {d.qmt_running.length ? (
                <Badge tone="success">{d.qmt_running.join("、")}</Badge>
              ) : (
                <Badge tone="neutral">未运行</Badge>
              )}
            </dd>
            <dt>已部署 bundle</dt>
            <dd>
              {d.deployed.exists ? (
                <span>
                  <code>{d.deployed.path}</code> · {d.deployed.size_bytes} B · {d.deployed.mtime}
                </span>
              ) : (
                <Badge tone="warning">未部署</Badge>
              )}
            </dd>
            <dt>Bundle 版本</dt>
            <dd>
              <Badge tone="neutral">{d.bundle_version || "未知"}</Badge> · {d.bundle_chars_estimate} 字符
            </dd>
            <dt>agent_config.json</dt>
            <dd>
              <code>{d.config.path}</code>{" "}
              {d.config.exists ? <Badge tone="success">存在</Badge> : <Badge tone="warning">未存在</Badge>}
              {d.config.bridge_dir ? <> · bridge_dir=<code>{d.config.bridge_dir}</code></> : null}
            </dd>
            <dt>工具链</dt>
            <dd>
              {toolsReady ? (
                <Badge tone="success">可用（{d.internal_dir}）</Badge>
              ) : (
                <Badge tone="danger">
                  {d.agent_tools_available ? "分片真源缺失" : "工具链缺失"}
                </Badge>
              )}
            </dd>
          </dl>
        )}
      </Panel>

      <Panel
        title="一键部署"
        extra={
          <label style={{ fontSize: "0.85em" }}>
            <input
              type="checkbox"
              checked={dryRun}
              onChange={(e) => setDryRun(e.target.checked)}
              disabled={busy}
            />{" "}
            预览模式（不写盘）
          </label>
        }
      >
        <dl className={s.grid2}>
          <dt>QMT 目录</dt>
          <dd>
            <Input
              placeholder={d?.qmt_dir || "留空则使用探测结果"}
              value={qmtDir}
              onChange={(e) => setQmtDir(e.target.value)}
            />
          </dd>
          <dt>文件名</dt>
          <dd>
            <Input value={filename} onChange={(e) => setFilename(e.target.value)} />
          </dd>
          <dt>策略显示名</dt>
          <dd>
            <Input value={strategy} onChange={(e) => setStrategy(e.target.value)} />
          </dd>
          <dt>附 .txt 副本</dt>
          <dd>
            <input
              type="checkbox"
              checked={txtCopy}
              onChange={(e) => setTxtCopy(e.target.checked)}
              disabled={busy}
            />
            <span style={{ marginLeft: 8, fontSize: "0.85em" }}>供「新建 + 粘贴」路径 B 用</span>
          </dd>
        </dl>

        {banner && (
          <div style={{
            padding: "6px 10px",
            borderRadius: 4,
            background: banner.tone === "ok" ? "var(--success-dim)" : "var(--danger-dim)",
            color: banner.tone === "ok" ? "var(--success)" : "var(--danger)",
            margin: "8px 0",
          }}>
            {banner.text}
          </div>
        )}

        <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
          <Button
            variant="primary"
            onClick={() =>
              setConfirm({
                title: dryRun ? "预览部署" : "确认部署",
                message: dryRun
                  ? `将生成 bundle 并显示目标路径（不写盘）：\n${filename}`
                  : `将把 bundle 写到 QMT 策略目录：\n${filename}\n\n目标文件若已存在将先备份为 .bak.<epoch>。`,
                warn: dryRun ? undefined : "写盘操作不可回滚，请先确认 QMT 目录路径正确。",
                run: runDeploy,
              })
            }
            disabled={busy || !toolsReady}
          >
            {busy ? "处理中…" : dryRun ? "预览部署" : "确认部署"}
          </Button>
          <Button variant="ghost" onClick={runDiagnose} disabled={busy}>
            运行诊断
          </Button>
          <Button
            variant="ghost"
            onClick={() => setDryRun((v) => !v)}
            disabled={busy}
          >
            {dryRun ? "切换到写盘" : "切换到预览"}
          </Button>
        </div>

        {lastDeploy && !lastDeploy.ok && (
          <div style={{
            marginTop: 8,
            padding: "6px 10px",
            borderRadius: 4,
            background: "var(--danger-dim)",
            color: "var(--danger)",
          }}>
            <strong>部署未成功：</strong>
            {lastDeploy.problems.join("; ") || "未知原因"}
          </div>
        )}

        {lastDeploy?.registered_hint && (
          <div className={s.muted} style={{ marginTop: 8, fontSize: "0.88em", lineHeight: 1.5 }}>
            {lastDeploy.registered_hint}
          </div>
        )}
      </Panel>

      <Panel
        title="Bundle 源码预览"
        extra={
          <Button variant="ghost" onClick={runBundle} disabled={bundleBusy || !toolsReady}>
            {bundleBusy ? "生成中…" : "生成并预览"}
          </Button>
        }
      >
        <div className={s.muted} style={{ marginBottom: 6, fontSize: "0.88em" }}>
          直接调后端生成器取回 bundle 全文（<strong>不写盘</strong>）。用于确认「要写进去的
          到底是什么」—— 部署前先看一眼，比事后对着乱码猜强。
        </div>
        {!bundle ? (
          <span className={s.muted}>尚未生成。</span>
        ) : (
          <>
            <dl className={s.grid2}>
              <dt>大小 / 编码</dt>
              <dd>
                {bundle.size_bytes} B · <Badge tone="neutral">{bundle.encoding}</Badge>
              </dd>
              <dt>分片真源</dt>
              <dd><code>{bundle.source_agent_dir}</code></dd>
              <dt>工具链目录</dt>
              <dd><code>{bundle.source_tools_dir}</code></dd>
            </dl>
            {bundle.problems.length > 0 && (
              <ol style={{ paddingLeft: 20, marginTop: 8 }}>
                {bundle.problems.map((p, i) => (
                  <li key={i} style={{ color: "var(--danger)" }}>{p}</li>
                ))}
              </ol>
            )}
            <pre
              style={{
                marginTop: 8,
                maxHeight: 320,
                overflow: "auto",
                padding: 10,
                borderRadius: 4,
                background: "var(--bg-2)",
                fontSize: "0.8em",
                lineHeight: 1.45,
              }}
            >
              {bundle.text}
            </pre>
          </>
        )}
      </Panel>

      <Panel
        title="agent_config.json"
        extra={
          <div style={{ display: "flex", gap: 6 }}>
            <Button variant="ghost" onClick={loadConfig} disabled={cfgBusy}>
              {cfgBusy ? "读取中…" : "读取"}
            </Button>
            <Button
              variant="primary"
              onClick={() =>
                setConfirm({
                  title: "写入 agent_config.json",
                  message: `将把编辑框内容覆盖写入：\n${cfg?.path || "（读取后可知具体路径）"}`,
                  warn: "原文件会先备份为 .bak.<epoch> 再覆盖。写错会让 agent 连不上 bridge。",
                  run: runSaveConfig,
                })
              }
              disabled={cfgBusy || !cfg}
            >
              保存
            </Button>
          </div>
        }
      >
        <div className={s.muted} style={{ marginBottom: 6, fontSize: "0.88em" }}>
          agent 运行时配置（bridge_dir 等）。目标文件不存在时读取返回的是模板，
          保存即创建。
        </div>
        {cfg && (
          <div className={s.muted} style={{ marginBottom: 6 }}>
            <code>{cfg.path}</code>{" "}
            {cfg.exists ? (
              <Badge tone="success">已存在</Badge>
            ) : (
              <Badge tone="warning">未存在（模板）</Badge>
            )}
          </div>
        )}
        <textarea
          value={cfgText}
          onChange={(e) => setCfgText(e.target.value)}
          spellCheck={false}
          rows={12}
          placeholder='{ "bridge_dir": "P:/stock/gd_qmt/..." }'
          style={{
            width: "100%",
            boxSizing: "border-box",
            fontFamily: "var(--font-mono, ui-monospace, Consolas, monospace)",
            fontSize: "0.82em",
            lineHeight: 1.45,
            padding: 10,
            borderRadius: 4,
            border: "1px solid var(--border)",
            background: "var(--bg-2)",
            resize: "vertical",
          }}
        />
      </Panel>

      <Panel
        title="工具链自检"
        extra={
          <Button variant="ghost" onClick={runTools} disabled={toolsBusy}>
            {toolsBusy ? "加载中…" : "刷新清单"}
          </Button>
        }
      >
        <div className={s.muted} style={{ marginBottom: 6, fontSize: "0.88em" }}>
          列出随客户端打包的工具链与分片真源文件。部署按钮在工具链缺失时会被禁用 ——
          这里能看到「缺的到底是哪一个」。
        </div>
        {!tools ? (
          <span className={s.muted}>尚未加载。</span>
        ) : (
          <>
            <dl className={s.grid2}>
              <dt>运行形态</dt>
              <dd>
                <Badge tone={tools.frozen ? "success" : "neutral"}>
                  {tools.frozen ? "打包运行（frozen）" : "源码运行（dev）"}
                </Badge>
              </dd>
              <dt>internal 目录</dt>
              <dd><code>{tools.internal_dir}</code></dd>
              <dt>分片真源</dt>
              <dd><code>{tools.agent_bigqmt_dir}</code></dd>
              <dt>工具目录</dt>
              <dd><code>{tools.qmt_tools_dir}</code></dd>
            </dl>
            <div style={{ marginTop: 8, display: "flex", gap: 16, flexWrap: "wrap" }}>
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>工具链文件</div>
                {tools.tools.length ? (
                  <ul style={{ paddingLeft: 18 }}>
                    {tools.tools.map((t) => (
                      <li key={t.name}>
                        <code>{t.name}</code> · {t.size} B
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Badge tone="danger">无（安装包缺少工具链）</Badge>
                )}
              </div>
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>分片真源文件</div>
                {tools.sources.length ? (
                  <ul style={{ paddingLeft: 18 }}>
                    {tools.sources.map((t) => (
                      <li key={t.name}>
                        <code>{t.name}</code> · {t.size} B
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Badge tone="danger">无（分片真源缺失）</Badge>
                )}
              </div>
            </div>
          </>
        )}
      </Panel>

      <Panel
        title="远程下发（bundle 分发）"
        extra={
          <label style={{ fontSize: "0.85em" }}>
            <input
              type="checkbox"
              checked={distDryRun}
              onChange={(e) => setDistDryRun(e.target.checked)}
              disabled={distBusy}
            />{" "}
            拉取预览（不写盘）
          </label>
        }
      >
        {distStatus.error ? (
          <EmptyState text={`下发状态加载失败：${distStatus.error}`} />
        ) : !distStatus.data ? (
          <span className={s.muted}>加载中…</span>
        ) : (
          <dl className={s.grid2}>
            <dt>下发开关</dt>
            <dd>
              {distStatus.data.enabled ? (
                <Badge tone="success">已启用</Badge>
              ) : (
                <Badge tone="neutral">未启用</Badge>
              )}
            </dd>
            <dt>已配置 URL</dt>
            <dd>
              {distStatus.data.url ? (
                <code>{distStatus.data.url}</code>
              ) : (
                <Badge tone="warning">未配置</Badge>
              )}
            </dd>
            <dt>本地版本</dt>
            <dd><Badge tone="neutral">{distStatus.data.local_version || "未知"}</Badge></dd>
          </dl>
        )}

        <div style={{ marginTop: 8 }}>
          <Input
            placeholder="覆盖下发 URL（留空则使用配置）"
            value={distUrl}
            onChange={(e) => setDistUrl(e.target.value)}
            disabled={distBusy}
          />
        </div>

        <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
          <Button variant="primary" onClick={runDistCheck} disabled={distBusy}>
            {distBusy ? "处理中…" : "检测更新"}
          </Button>
          <Button
            variant="ghost"
            onClick={() =>
              setConfirm({
                title: distDryRun ? "预览拉取" : "确认拉取",
                message: distDryRun
                  ? "将从下发 URL 拉取 bundle 并显示目标路径（不写盘）。"
                  : "将从下发 URL 拉取 bundle 并通过体检后写入 QMT 策略目录。",
                warn: distDryRun
                  ? undefined
                  : "写盘操作不可回滚。bundle 会先做 UTF-8 + 污染 + 语法体检，未通过则拒绝落盘。",
                run: runDistPull,
              })
            }
            disabled={distBusy}
          >
            {distDryRun ? "预览拉取" : "确认拉取部署"}
          </Button>
          <Button
            variant="ghost"
            onClick={() => setDistDryRun((v) => !v)}
            disabled={distBusy}
          >
            {distDryRun ? "切换到写盘" : "切换到预览"}
          </Button>
        </div>

        {lastCheck && (
          <div style={{ marginTop: 8, padding: "6px 10px", borderRadius: 4, background: "var(--bg-2)" }}>
            <div style={{ marginBottom: 4 }}>
              <Badge tone={lastCheck.has_update ? "warning" : "success"}>
                {lastCheck.has_update ? "发现更新" : "已是最新"}
              </Badge>{" "}
              本地 v{lastCheck.local_version}
              {lastCheck.remote_version ? (
                <span> · 远程 v{lastCheck.remote_version}</span>
              ) : null}
            </div>
            {lastCheck.bundle_url && <div className={s.muted}><code>{lastCheck.bundle_url}</code></div>}
            {lastCheck.size_bytes != null && (
              <div className={s.muted}>{lastCheck.size_bytes} B</div>
            )}
          </div>
        )}

        {lastPull && (
          <div style={{
            marginTop: 8,
            padding: "6px 10px",
            borderRadius: 4,
            background: "var(--success-dim)",
            color: "var(--success)",
          }}>
            {distDryRun ? "预览：" : "已拉取部署："}
            <code>{lastPull.path}</code> · {lastPull.size_bytes} B
            {lastPull.backup ? <> · 旧版备份：<code>{lastPull.backup}</code></> : null}
          </div>
        )}
      </Panel>

      {lastDiagnose && (
        <Panel
          title="诊断结果"
          extra={
            <Badge tone={lastDiagnose.ok ? "success" : "danger"}>
              {lastDiagnose.ok ? "一切正常" : `${lastDiagnose.problems.length} 个问题`}
            </Badge>
          }
        >
          <dl className={s.grid2}>
            <dt>Bundle</dt>
            <dd>
              <code>{lastDiagnose.bundle.path}</code> · {lastDiagnose.bundle.size_bytes} B{" "}
              {lastDiagnose.encoding_ok ? (
                <Badge tone="success">编码 OK</Badge>
              ) : (
                <Badge tone="danger">编码异常：{lastDiagnose.encoding_note}</Badge>
              )}
            </dd>
            <dt>Config</dt>
            <dd>
              <code>{lastDiagnose.config.path}</code>{" "}
              {lastDiagnose.config.exists ? <Badge tone="success">存在</Badge> : <Badge tone="warning">未存在</Badge>}
              {lastDiagnose.config.bridge_dir ? <> · bridge_dir=<code>{lastDiagnose.config.bridge_dir}</code></> : null}
            </dd>
            <dt>心跳</dt>
            <dd>
              {lastDiagnose.heartbeat.alive ? (
                <Badge tone="success">agent 在跑（v{lastDiagnose.heartbeat.agent_ver || "?"}）</Badge>
              ) : (
                <Badge tone="warning">agent 未运行</Badge>
              )}
              {lastDiagnose.heartbeat.runtime_mode ? <> · {lastDiagnose.heartbeat.runtime_mode}</> : null}
            </dd>
            <dt>自检</dt>
            <dd>
              {lastDiagnose.heartbeat.probe_ok === undefined ? (
                <Badge tone="neutral">未上报</Badge>
              ) : lastDiagnose.heartbeat.probe_ok ? (
                <Badge tone="success">通过</Badge>
              ) : (
                <>
                  <Badge tone="danger">未通过</Badge>
                  {(lastDiagnose.heartbeat.probe_bad_steps || []).length > 0 && (
                    <span style={{ marginLeft: 6 }}>
                      异常项：<code>{(lastDiagnose.heartbeat.probe_bad_steps || []).join("、")}</code>
                    </span>
                  )}
                </>
              )}
            </dd>
            <CapabilityRow label="下单能力" cap={lastDiagnose.capabilities?.trading} />
            {/* 结论之上补「依据」：只说「不可用」用户不知道缺哪个入口 */}
            {(() => {
              const ts = lastDiagnose.trade_surface;
              if (!ts || (!ts.present?.length && !ts.missing?.length)) {
                return null;
              }
              return (
                <>
                  <dt>下单接口面</dt>
                  <dd>
                    <Badge tone={ts.can_submit ? "success" : "warning"}>
                      {ts.can_submit ? "就绪" : "不完整"}
                    </Badge>
                    <span style={{ marginLeft: 6 }}>
                      已注入 {(ts.present || []).join("、") || "（无）"}；缺失{" "}
                      {(ts.missing || []).join("、") || "（无）"}
                    </span>
                  </dd>
                </>
              );
            })()}
            <CapabilityRow label="行情能力" cap={lastDiagnose.capabilities?.quote} />
            <dt>QMT 客户端</dt>
            <dd>
              {lastDiagnose.qmt_running.length ? (
                <Badge tone="success">{lastDiagnose.qmt_running.join("、")}</Badge>
              ) : (
                <Badge tone="neutral">未运行</Badge>
              )}
            </dd>
          </dl>

          {lastDiagnose.problems.length > 0 && (
            <ol style={{ paddingLeft: 20, marginTop: 8 }}>
              {lastDiagnose.problems.map((p, i) => (
                <li key={i} style={{ marginBottom: 4 }}>
                  <Badge tone="danger">{p.source}</Badge>{" "}
                  <span style={{ color: "var(--danger)" }}>{p.msg}</span>
                </li>
              ))}
            </ol>
          )}
        </Panel>
      )}

      {confirm && (
        <ConfirmModal
          open
          title={confirm.title}
          message={confirm.message}
          warn={confirm.warn}
          onConfirm={confirm.run}
          onCancel={() => setConfirm(null)}
        />
      )}
    </div>
  );
}
