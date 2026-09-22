import { useState } from "react";
import {
  Badge,
  Button,
  ConfirmButton,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  Spinner,
  Tabs,
  type Column,
} from "@/design/primitives";
import { systemApi, type ScheduleCreate } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import type { RuntimeJob, ScheduleItem } from "@/shared/types";
import s from "../domain.module.css";

/** 后端已注册的 runner kind（app/runtime/system_jobs.py:SYSTEM_JOB_KINDS + 内置 sync/screen） */
const JOB_KINDS = [
  "system.eod",
  "system.sync_bars",
  "system.sync_fundamentals",
  "system.refresh_universe",
  "system.reconcile_bars",
  "system.rolling_repair",
  "system.coverage_report",
  "system.publish_snapshot",
  "system.classic_screen",
  "sync",
  "screen",
] as const;

/**
 * cron 预设。
 *
 * ★ 为什么需要：cron 是给机器读的，让用户手填「30 15 * * 1-5」出错率极高
 * （顺序是「分 时 日 月 周」，写反一点任务就永远不跑或跑错时间）。
 * 这几个覆盖了「定时更新日线 / 定时选股」的真实需求，与后端播种的默认调度
 * （system_jobs.DEFAULT_SCHEDULES）同一套口径。
 */
const CRON_PRESETS: Array<{ cron: string; label: string; hint: string }> = [
  { cron: "30 15 * * 1-5", label: "每交易日 15:30", hint: "收盘后更新日线（默认调度同款）" },
  { cron: "0 16 * * 1-5", label: "每交易日 16:00", hint: "收盘后经典策略选股（默认调度同款）" },
  { cron: "0 9 * * 1-5", label: "每交易日 09:00", hint: "开盘前准备" },
  { cron: "0 18 * * 5", label: "每周五 18:00", hint: "周末前的对账/报表" },
  { cron: "0 2 * * *", label: "每天 02:00", hint: "夜间批量（避开交易时段）" },
  { cron: "0 * * * *", label: "每小时整点", hint: "高频巡检" },
];

/** 默认调度的固定 id（backend/app/runtime/system_jobs.py:DEFAULT_SCHEDULES） */
const DEFAULT_SCHEDULE_IDS = new Set(["sch-default-sync-bars", "sch-default-classic-screen"]);

/** kind 的中文说明 —— 「system.publish_snapshot」这类名字对界面毫无解释力。 */
const KIND_HINT: Record<string, string> = {
  "system.eod": "收盘后全流程管线（同步→对账→快照）",
  "system.sync_bars": "全市场日线同步",
  "system.sync_fundamentals": "基本面因子预取（加速选股）",
  "system.refresh_universe": "股票池刷新",
  "system.reconcile_bars": "跨源对账",
  "system.rolling_repair": "缺失区间滚动修复",
  "system.coverage_report": "覆盖率与来源占比报表",
  "system.publish_snapshot": "发布数据集快照",
  "system.classic_screen": "经典策略选股（海龟/均线放量/RPS…）",
  sync: "通用同步",
  screen: "通用选股",
};

const JOB_STATUS_TONE: Record<RuntimeJob["status"], "success" | "danger" | "neutral" | "info" | "warning"> = {
  queued: "info",
  running: "warning",
  done: "success",
  failed: "danger",
  canceled: "neutral",
};

/**
 * 定时任务与调度。
 *
 * ★ 契约要点（runtime.py）：
 *   - GET /runtime/jobs 与 /runtime/schedules 都返回 {items, count}，**不是**裸数组
 *   - 调度的创建字段是 kind（不是 job_kind）；cron 由后端 CronExpr.parse 校验，非法即 400
 *   - misfire_policy 取值 coalesce / skip / catch_up（**下划线**，不是 catchup）
 *   - /runtime/schedules/{id}/trigger 立即触发一次，返回 {schedule_id, job_id}
 *   - kind 必须能取到 runner，否则 400「kind 无对应 runner」——本页只列已注册 kind
 */
export function RuntimeJobs() {
  const [tab, setTab] = useState<"jobs" | "schedules">("jobs");
  const jobs = useAsync(() => systemApi.jobs(), []);
  const schedules = useAsync(() => systemApi.schedules(), []);

  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  // 提交任务表单
  const [jobKind, setJobKind] = useState<string>("system.sync_bars");
  const [jobName, setJobName] = useState("");
  const [jobParams, setJobParams] = useState("{}");
  const [priority, setPriority] = useState("5");

  // 创建调度表单
  const [sKind, setSKind] = useState<string>("system.eod");
  const [sCron, setSCron] = useState("0 15 * * 1-5");
  const [sName, setSName] = useState("");
  const [sMisfire, setSMisfire] = useState("coalesce");
  const [sEnabled, setSEnabled] = useState("1");
  const [sParams, setSParams] = useState("{}");
  /** 正在编辑的调度 id；null = 当前在「新建」模式。 */
  const [editingId, setEditingId] = useState<string | null>(null);
  // ★ 新建调度的默认值（取消编辑时还原；显式保存一份，避免后面再加新字段时漏改）。
  const SCHEDULE_DEFAULTS = {
    kind: "system.eod",
    cron: "0 15 * * 1-5",
    name: "",
    misfire: "coalesce",
    enabled: "1",
    params: "{}",
  };

  const wrap = async (fn: () => Promise<unknown>, okText: string, reload: () => Promise<void>) => {
    setBusy(true);
    setBanner(null);
    try {
      await fn();
      setBanner({ tone: "ok", text: okText });
      await reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const submitJob = () => {
    let params: Record<string, unknown> = {};
    try {
      params = JSON.parse(jobParams || "{}") as Record<string, unknown>;
    } catch {
      setBanner({ tone: "error", text: "params 不是合法 JSON" });
      return;
    }
    void wrap(
      () => systemApi.submitJob({ kind: jobKind, name: jobName || jobKind, params, priority: Number(priority) || 5 }),
      `任务已入队（${jobKind}）`,
      jobs.reload,
    );
  };

  const enterEdit = (r: ScheduleItem) => {
    setSKind(r.kind);
    setSCron(r.cron);
    setSName(r.name || "");
    setSMisfire(r.misfire_policy || "coalesce");
    setSEnabled(r.enabled ? "1" : "0");
    setSParams(JSON.stringify(r.params ?? {}));
    setEditingId(r.id);
  };

  const cancelEdit = () => {
    setSKind(SCHEDULE_DEFAULTS.kind);
    setSCron(SCHEDULE_DEFAULTS.cron);
    setSName(SCHEDULE_DEFAULTS.name);
    setSMisfire(SCHEDULE_DEFAULTS.misfire);
    setSEnabled(SCHEDULE_DEFAULTS.enabled);
    setSParams(SCHEDULE_DEFAULTS.params);
    setEditingId(null);
  };

  const submitSchedule = () => {
    let params: Record<string, unknown> = {};
    try {
      params = JSON.parse(sParams || "{}") as Record<string, unknown>;
    } catch {
      setBanner({ tone: "error", text: "调度 params 不是合法 JSON" });
      return;
    }
    const base: ScheduleCreate = {
      kind: sKind,
      cron: sCron,
      name: sName || sKind,
      misfire_policy: sMisfire,
      enabled: sEnabled === "1",
      params,
    };
    if (editingId) {
      void wrap(
        () => systemApi.updateSchedule(editingId, base),
        `调度已保存（${editingId}）`,
        async () => { await schedules.reload(); cancelEdit(); },
      );
    } else {
      void wrap(
        () => systemApi.createSchedule(base),
        `调度已创建（${sCron}）`,
        async () => { await schedules.reload(); cancelEdit(); },
      );
    }
  };

  const jobCols: Column<RuntimeJob>[] = [
    { key: "id", header: "任务号", width: 120, mono: true, render: (r) => r.id },
    { key: "kind", header: "类型", width: 180, mono: true, render: (r) => r.kind },
    { key: "name", header: "名称", width: 160, render: (r) => r.name || "--" },
    {
      key: "status",
      header: "状态",
      width: 80,
      render: (r) => <Badge tone={JOB_STATUS_TONE[r.status] ?? "neutral"}>{r.status}</Badge>,
    },
    {
      key: "progress",
      header: "进度",
      width: 80,
      align: "right",
      mono: true,
      render: (r) => (r.progress === undefined ? "--" : `${r.progress}%`),
    },
    { key: "prio", header: "优先级", width: 74, align: "right", mono: true, render: (r) => String(r.priority ?? "--") },
    { key: "started", header: "开始", width: 150, mono: true, render: (r) => r.started_at ?? "--" },
    { key: "finished", header: "结束", width: 150, mono: true, render: (r) => r.finished_at ?? "--" },
    {
      key: "msg",
      header: "信息 / 错误",
      render: (r) =>
        r.error ? (
          <span style={{ color: "var(--danger)" }}>{r.error}</span>
        ) : (
          <span className={s.muted}>{r.message ?? ""}</span>
        ),
    },
    {
      key: "act",
      header: "操作",
      width: 70,
      render: (r) =>
        r.status === "queued" || r.status === "running" ? (
          // 取消正在跑的任务不可逆（跑了一半的结果不会回滚）⇒ 两段式确认
          <ConfirmButton
            disabled={busy}
            confirmText="确认取消"
            title={`取消任务 ${r.id}`}
            onConfirm={() => void wrap(() => systemApi.cancelJob(r.id), `已请求取消 ${r.id}`, jobs.reload)}
          >
            取消
          </ConfirmButton>
        ) : null,
    },
  ];

  const schedCols: Column<ScheduleItem>[] = [
    {
      key: "id",
      header: "ID",
      width: 140,
      mono: true,
      render: (r) => (
        <span title={DEFAULT_SCHEDULE_IDS.has(r.id) ? "内置默认调度：可停用/改时间，删除后下次启动会重建" : r.id}>
          {r.id}
          {DEFAULT_SCHEDULE_IDS.has(r.id) && (
            <Badge tone="info" title="内置默认调度">内置</Badge>
          )}
        </span>
      ),
    },
    { key: "name", header: "名称", width: 150, render: (r) => r.name || "--" },
    {
      key: "kind",
      header: "类型",
      width: 180,
      mono: true,
      render: (r) => <span title={KIND_HINT[r.kind] ?? r.kind}>{r.kind}</span>,
    },
    { key: "cron", header: "Cron", width: 130, mono: true, render: (r) => r.cron },
    { key: "misfire", header: "补跑策略", width: 90, mono: true, render: (r) => r.misfire_policy },
    {
      key: "enabled",
      header: "状态",
      width: 76,
      render: (r) => <Badge tone={r.enabled ? "success" : "neutral"}>{r.enabled ? "启用" : "停用"}</Badge>,
    },
    // ⚠️ 字段名是 next_run_at / last_run_at（后端连列名一起返回）。
    // 写成 next_run/last_run 会永远读到 undefined ⇒ 两列恒 `--`，且不报错。
    { key: "next", header: "下次运行", width: 150, mono: true, render: (r) => r.next_run_at ?? "--" },
    { key: "last", header: "上次运行", width: 150, mono: true, render: (r) => r.last_run_at ?? "--" },
    {
      key: "act",
      header: "操作",
      width: 190,
      render: (r) => (
        <div style={{ display: "flex", gap: 4 }}>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => enterEdit(r)}
            title="编辑此调度（cron / 补跑策略 / 启用 等）"
          >
            编辑
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => void wrap(() => systemApi.triggerSchedule(r.id), `已触发 ${r.id}`, schedules.reload)}
          >
            立即运行
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() =>
              void wrap(
                () => systemApi.updateSchedule(r.id, { enabled: !r.enabled }),
                `${r.enabled ? "已停用" : "已启用"} ${r.id}`,
                schedules.reload,
              )
            }
          >
            {r.enabled ? "停用" : "启用"}
          </Button>
          <ConfirmButton
            disabled={busy}
            onConfirm={() =>
              void wrap(() => systemApi.deleteSchedule(r.id), `已删除 ${r.id}`, schedules.reload)
            }
          >
            删除
          </ConfirmButton>
        </div>
      ),
    },
  ];

  return (
    <div className={s.page}>
      <Tabs
        items={[
          { key: "jobs", label: `任务（${jobs.data?.count ?? 0}）` },
          { key: "schedules", label: `调度（${schedules.data?.count ?? 0}）` },
        ]}
        value={tab}
        onChange={setTab}
      />

      {banner && (
        <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>{banner.text}</div>
      )}

      {tab === "jobs" ? (
        <>
          <Panel title="提交任务">
            <div className={s.cols4}>
              <FormRow label="类型">
                <Select
                  value={jobKind}
                  onChange={(e) => setJobKind(e.target.value)}
                  options={JOB_KINDS.map((k) => ({ value: k, label: k }))}
                />
              </FormRow>
              <FormRow label="名称">
                <Input value={jobName} onChange={(e) => setJobName(e.target.value)} placeholder="可选" />
              </FormRow>
              <FormRow label="优先级">
                <Input value={priority} onChange={(e) => setPriority(e.target.value)} mono />
              </FormRow>
              <FormRow label="参数(JSON)">
                <Input value={jobParams} onChange={(e) => setJobParams(e.target.value)} mono />
              </FormRow>
            </div>
            <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
              <Button size="sm" disabled={busy} onClick={submitJob}>
                提交
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void jobs.reload()}>
                刷新
              </Button>
            </div>
          </Panel>

          <Panel flush className={s.grow} title="任务列表">
            <div className={s.tableArea}>
              {jobs.loading && !jobs.data ? (
                <Spinner label="加载中…" />
              ) : jobs.error ? (
                <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
                  {jobs.error}
                </div>
              ) : (jobs.data?.items.length ?? 0) === 0 ? (
                <EmptyState text="暂无任务 —— 在上方「提交任务」选择类型后提交；调度自动触发的任务也会出现在这里" />
              ) : (
                <DataTable
                  columns={jobCols}
                  rows={jobs.data?.items ?? []}
                  rowKey={(r) => r.id}
                  rowHeight={24}
                />
              )}
            </div>
          </Panel>
        </>
      ) : (
        <>
          <Panel title={editingId ? `编辑调度（${editingId}）` : "新建调度"}>
            <div className={s.cols4}>
              <FormRow label="类型">
                <Select
                  value={sKind}
                  onChange={(e) => setSKind(e.target.value)}
                  options={JOB_KINDS.map((k) => ({ value: k, label: k }))}
                />
              </FormRow>
              <FormRow label="Cron">
                <Input value={sCron} onChange={(e) => setSCron(e.target.value)} mono placeholder="0 15 * * 1-5" />
              </FormRow>
              <FormRow label="常用时间">
                <Select
                  value=""
                  onChange={(e) => {
                    const p = CRON_PRESETS.find((x) => x.cron === e.target.value);
                    if (p) setSCron(p.cron);
                  }}
                  options={[
                    { value: "", label: "选择预设时间…" },
                    ...CRON_PRESETS.map((p) => ({ value: p.cron, label: `${p.label} — ${p.hint}` })),
                  ]}
                />
              </FormRow>
              <FormRow label="参数(JSON)">
                <Input value={sParams} onChange={(e) => setSParams(e.target.value)} mono />
              </FormRow>
              <FormRow label="名称">
                <Input value={sName} onChange={(e) => setSName(e.target.value)} placeholder="可选" />
              </FormRow>
              <FormRow label="补跑策略">
                <Select
                  value={sMisfire}
                  onChange={(e) => setSMisfire(e.target.value)}
                  options={[
                    { value: "coalesce", label: "coalesce 合并" },
                    { value: "skip", label: "skip 跳过" },
                    // ⚠️ 后端只认 **catch_up**（schedules.py 白名单：
                    // catch_up / coalesce / skip）。写成 catchup 会必 400，
                    // 而界面上看不出是哪个字段错了。
                    { value: "catch_up", label: "catch_up 补跑" },
                  ]}
                />
              </FormRow>
              <FormRow label="启用">
                <Select
                  value={sEnabled}
                  onChange={(e) => setSEnabled(e.target.value)}
                  options={[
                    { value: "1", label: "启用" },
                    { value: "0", label: "停用" },
                  ]}
                />
              </FormRow>
            </div>
            <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
              <Button size="sm" disabled={busy} onClick={submitSchedule}>
                创建
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void schedules.reload()}>
                刷新
              </Button>
            </div>
            <div className={s.note} style={{ marginTop: 8 }}>
              {KIND_HINT[sKind] ?? sKind} · cron 为「分 时 日 月 周」五段（如
              <code> 30 15 * * 1-5 </code>= 每交易日 15:30）。
              {sKind === "system.classic_screen" && (
                <>
                  {" "}经典策略调度可用参数示例：
                  <code> {"{"}"strategies":["turtle_trade","ma_volume"],"limit":50{"}"} </code>
                  —— 留空则跑全部策略。
                </>
              )}
            </div>
          </Panel>

          <Panel flush className={s.grow} title="调度列表">
            <div className={s.tableArea}>
              {schedules.loading && !schedules.data ? (
                <Spinner label="加载中…" />
              ) : schedules.error ? (
                <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
                  {schedules.error}
                </div>
              ) : (schedules.data?.items.length ?? 0) === 0 ? (
                <EmptyState text="暂无调度 —— 在上方「新建调度」选择类型并填写 cron 后提交" />
              ) : (
                <DataTable
                  columns={schedCols}
                  rows={schedules.data?.items ?? []}
                  rowKey={(r) => r.id}
                  rowHeight={24}
                />
              )}
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}

export default RuntimeJobs;
