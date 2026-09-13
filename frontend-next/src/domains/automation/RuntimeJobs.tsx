import { useState } from "react";
import {
  Badge,
  Button,
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
  "sync",
  "screen",
] as const;

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
 *   - misfire_policy 取值 coalesce / skip / catchup
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

  const createSchedule = () => {
    const body: ScheduleCreate = {
      kind: sKind,
      cron: sCron,
      name: sName || sKind,
      misfire_policy: sMisfire,
      enabled: sEnabled === "1",
      params: {},
    };
    void wrap(() => systemApi.createSchedule(body), `调度已创建（${sCron}）`, schedules.reload);
  };

  const jobCols: Column<RuntimeJob>[] = [
    { key: "id", header: "任务号", width: 120, mono: true, render: (r) => r.id },
    { key: "kind", header: "类型", width: 180, mono: true, render: (r) => r.kind },
    { key: "name", header: "名称", width: 160, render: (r) => r.name ?? "--" },
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
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => void wrap(() => systemApi.cancelJob(r.id), `已请求取消 ${r.id}`, jobs.reload)}
          >
            取消
          </Button>
        ) : null,
    },
  ];

  const schedCols: Column<ScheduleItem>[] = [
    { key: "id", header: "ID", width: 110, mono: true, render: (r) => r.id },
    { key: "name", header: "名称", width: 150, render: (r) => r.name || "--" },
    { key: "kind", header: "类型", width: 180, mono: true, render: (r) => r.kind },
    { key: "cron", header: "Cron", width: 130, mono: true, render: (r) => r.cron },
    { key: "misfire", header: "补跑策略", width: 90, mono: true, render: (r) => r.misfire_policy },
    {
      key: "enabled",
      header: "状态",
      width: 76,
      render: (r) => <Badge tone={r.enabled ? "success" : "neutral"}>{r.enabled ? "启用" : "停用"}</Badge>,
    },
    { key: "next", header: "下次运行", width: 150, mono: true, render: (r) => r.next_run ?? "--" },
    { key: "last", header: "上次运行", width: 150, mono: true, render: (r) => r.last_run ?? "--" },
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
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => void wrap(() => systemApi.deleteSchedule(r.id), `已删除 ${r.id}`, schedules.reload)}
          >
            删除
          </Button>
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
                <EmptyState text="暂无任务" />
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
          <Panel title="新建调度">
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
                    { value: "catchup", label: "catchup 补跑" },
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
              <Button size="sm" disabled={busy} onClick={createSchedule}>
                创建
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void schedules.reload()}>
                刷新
              </Button>
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
                <EmptyState text="暂无调度" />
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
