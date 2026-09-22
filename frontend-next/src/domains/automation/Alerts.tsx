import { useState } from "react";
import {
  Badge,
  Button,
  ConfirmButton,
  ConfirmModal,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  Select,
  Spinner,
  type Column,
} from "@/design/primitives";
import { alertApi, type AlertRulePayload } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import type { AlertHistoryRow, AlertRule } from "@/shared/types";
import s from "../domain.module.css";

const OPS = [">", ">=", "<", "<=", "==", "!="];

/**
 * 告警规则。
 *
 * ★ 契约要点（alerts.py）：
 *   - 主键字段是 id（int）；enabled 在 DB 里是 0/1，不是布尔
 *   - 保存用 POST /alerts/rules，带 id 即为更新（后端按 id 走 UPDATE）
 *   - 批量删除的 body 键是 ids（不是 rids）
 *   - /alerts/test 只做「评估事件」不落库，用于验证规则是否命中
 *   - 字段 event/metric/op/threshold/channel 构成规则主体；cooldown_seconds 防重复轰炸
 */
export function Alerts() {
  const rules = useAsync<AlertRule[]>(() => alertApi.rules(), []);
  const history = useAsync<AlertHistoryRow[]>(() => alertApi.history(50), []);

  const [editing, setEditing] = useState<AlertRulePayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  /** 批量删除的勾选；删除后必须清理，否则会残留指向已删 id 的勾选态 */
  const [selected, setSelected] = useState<number[]>([]);
  /** 批量删除的模态确认（批量 ⇒ 走 ConfirmModal，见下方按钮处注释） */
  const [confirmBatch, setConfirmBatch] = useState(false);

  const blank: AlertRulePayload = {
    name: "",
    enabled: true,
    event: "*",
    metric: "",
    op: ">",
    threshold: 0,
    channel: "*",
    cooldown_seconds: 300,
  };

  const save = async () => {
    if (!editing) return;
    setBusy(true);
    setBanner(null);
    try {
      const res = await alertApi.saveRule(editing);
      setBanner({ tone: "ok", text: `已保存规则 #${res.id}` });
      setEditing(null);
      await rules.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: number) => {
    setBusy(true);
    setBanner(null);
    try {
      await alertApi.deleteRule(id);
      setBanner({ tone: "ok", text: `已删除规则 #${id}` });
      await rules.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (r: AlertRule) => {
    setBusy(true);
    try {
      await alertApi.saveRule({
        id: r.id,
        name: r.name,
        enabled: r.enabled !== 1,
        event: r.event,
        metric: r.metric,
        op: r.op,
        threshold: r.threshold,
        channel: r.channel,
        cooldown_seconds: r.cooldown_seconds,
      });
      await rules.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const batchRemove = async () => {
    if (!selected.length) return;
    setBusy(true);
    setBanner(null);
    try {
      const res = await alertApi.batchDelete(selected);
      setBanner({ tone: "ok", text: `已批量删除 ${res.deleted} 条规则` });
      setSelected([]);
      await rules.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const testRule = async (r: AlertRule) => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await alertApi.test(r.event || "system.test", { metric: r.metric, value: r.threshold });
      setBanner({ tone: "ok", text: `已投递测试事件 ${res.event}（告警引擎评估完成）` });
      await history.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const all = rules.data ?? [];

  const cols: Column<AlertRule>[] = [
    {
      key: "sel",
      // ⚠️ Column.header 只接受 string（DataTable 的表头不是 ReactNode），
      // 传 Element 会直接编译不过 —— 全选框放进表头单元格的写法在这里不可用。
      header: "",
      width: 44,
      render: (r) => (
        <input
          type="checkbox"
          aria-label={`选择规则 ${r.id}`}
          checked={selected.includes(r.id)}
          onChange={() =>
            setSelected((prev) =>
              prev.includes(r.id) ? prev.filter((x) => x !== r.id) : [...prev, r.id],
            )
          }
        />
      ),
    },
    { key: "id", header: "ID", width: 56, mono: true, render: (r) => String(r.id) },
    { key: "name", header: "名称", width: 150, render: (r) => r.name || "--" },
    { key: "event", header: "事件", width: 130, mono: true, render: (r) => r.event || "*" },
    { key: "metric", header: "指标", width: 120, mono: true, render: (r) => r.metric || "--" },
    {
      key: "cond",
      header: "条件",
      width: 130,
      mono: true,
      render: (r) => `${r.op} ${r.threshold}`,
    },
    { key: "channel", header: "渠道", width: 100, mono: true, render: (r) => r.channel || "*" },
    {
      key: "cool",
      header: "冷却(秒)",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => String(r.cooldown_seconds),
    },
    {
      key: "enabled",
      header: "启用",
      width: 70,
      render: (r) => (
        <Badge tone={r.enabled === 1 ? "success" : "neutral"}>{r.enabled === 1 ? "已启用" : "已停用"}</Badge>
      ),
    },
    { key: "created", header: "创建", width: 150, mono: true, render: (r) => r.created_at },
    {
      key: "act",
      header: "操作",
      width: 190,
      render: (r) => (
        <div style={{ display: "flex", gap: 4 }}>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void toggle(r)}>
            {r.enabled === 1 ? "停用" : "启用"}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing({ ...r, enabled: r.enabled === 1 })}>
            编辑
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void testRule(r)}>
            测试
          </Button>
          <ConfirmButton disabled={busy} onConfirm={() => void remove(r.id)}>
            删除
          </ConfirmButton>
        </div>
      ),
    },
  ];

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Button size="sm" onClick={() => setEditing({ ...blank })}>
          新建规则
        </Button>
        <span className={s.muted}>已选 {selected.length} 条</span>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setSelected(selected.length === all.length ? [] : all.map((r) => r.id))}
        >
          {selected.length === all.length && all.length > 0 ? "取消全选" : "全选"}
        </Button>
        {/*
          ★ 批量删除走 **ConfirmModal**，不是 ConfirmButton。
          判据（ConfirmButton 文档）：行内单条删除用两段式按钮；**批量删除**属高风险，
          要让人读一遍「将删除 N 条」再确认 —— 一次误触删掉全部告警规则 = 把监控静音。
        */}
        <Button
          size="sm"
          variant="danger"
          disabled={busy || !selected.length}
          onClick={() => setConfirmBatch(true)}
        >
          批量删除
        </Button>
        <span className={s.spacer} />
        <Button size="sm" variant="ghost" onClick={() => void rules.reload()}>
          刷新
        </Button>
      </div>

      {banner && (
        <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>{banner.text}</div>
      )}

      {editing && (
        <Panel
          title={editing.id ? `编辑规则 #${editing.id}` : "新建规则"}
          extra={
            <div style={{ display: "flex", gap: 6 }}>
              <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>
                取消
              </Button>
              <Button size="sm" disabled={busy} onClick={() => void save()}>
                保存
              </Button>
            </div>
          }
        >
          <div className={s.cols3}>
            <FormRow label="名称">
              <Input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            </FormRow>
            <FormRow label="事件类型">
              <Input
                value={editing.event ?? ""}
                onChange={(e) => setEditing({ ...editing, event: e.target.value })}
                mono
                placeholder="* 或具体事件名"
              />
            </FormRow>
            <FormRow label="指标">
              <Input
                value={editing.metric ?? ""}
                onChange={(e) => setEditing({ ...editing, metric: e.target.value })}
                mono
                placeholder="如 price / drawdown"
              />
            </FormRow>
            <FormRow label="比较">
              <Select
                value={editing.op ?? ">"}
                onChange={(e) => setEditing({ ...editing, op: e.target.value })}
                options={OPS.map((o) => ({ value: o, label: o }))}
              />
            </FormRow>
            <FormRow label="阈值">
              <Input
                value={String(editing.threshold ?? 0)}
                onChange={(e) => setEditing({ ...editing, threshold: Number(e.target.value) || 0 })}
                mono
              />
            </FormRow>
            <FormRow label="渠道">
              <Input
                value={editing.channel ?? ""}
                onChange={(e) => setEditing({ ...editing, channel: e.target.value })}
                mono
                placeholder="* 或具体渠道"
              />
            </FormRow>
            <FormRow label="冷却(秒)">
              <Input
                value={String(editing.cooldown_seconds ?? 300)}
                onChange={(e) => setEditing({ ...editing, cooldown_seconds: Number(e.target.value) || 300 })}
                mono
              />
            </FormRow>
            <FormRow label="启用">
              <Select
                value={editing.enabled ? "1" : "0"}
                onChange={(e) => setEditing({ ...editing, enabled: e.target.value === "1" })}
                options={[
                  { value: "1", label: "启用" },
                  { value: "0", label: "停用" },
                ]}
              />
            </FormRow>
          </div>
        </Panel>
      )}

      <Panel flush className={s.grow} title={`告警规则（${rules.data?.length ?? 0}）`}>
        <div className={s.tableArea}>
          {rules.loading && !rules.data ? (
            <Spinner label="加载中…" />
          ) : rules.error ? (
            <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
              {rules.error}
            </div>
          ) : (rules.data?.length ?? 0) === 0 ? (
            <EmptyState text="暂无告警规则" actionText="新建规则" onAction={() => setEditing({ ...blank })} />
          ) : (
            <DataTable columns={cols} rows={rules.data ?? []} rowKey={(r) => String(r.id)} rowHeight={24} />
          )}
        </div>
      </Panel>

      <Panel flush title={`告警历史（${history.data?.length ?? 0}）`}>
        <div className={s.scroll} style={{ maxHeight: 220 }}>
          {history.error ? (
            <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
              {history.error}
            </div>
          ) : (history.data?.length ?? 0) === 0 ? (
            <div className={s.muted} style={{ padding: 8 }}>
              暂无告警历史
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "var(--font-sm)" }}>
              <tbody>
                {(history.data ?? []).map((h, i) => (
                  <tr key={String(h.id ?? i)} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td className={s.mono} style={{ padding: "3px 8px", width: 70 }}>
                      {String(h.id ?? "")}
                    </td>
                    <td className={s.mono} style={{ padding: "3px 8px", width: 160 }}>
                      {String(h.created_at ?? h.ts ?? "")}
                    </td>
                    <td style={{ padding: "3px 8px" }}>{String(h.event ?? h.rule_name ?? "")}</td>
                    <td className={s.mono} style={{ padding: "3px 8px" }}>
                      {String(h.message ?? h.detail ?? "")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Panel>

      <ConfirmModal
        open={confirmBatch}
        title="批量删除告警规则"
        danger
        confirmText={`确认删除 ${selected.length} 条`}
        message={
          <>
            将删除选中的 <b>{selected.length}</b> 条告警规则，删除后这些规则不再触发，
            <b>已发出的历史通知不会被清除</b>。
            <br />
            若只是想临时停掉，用「启用/停用」更安全。
          </>
        }
        warn="删除后不可恢复"
        onConfirm={() => {
          setConfirmBatch(false);
          void batchRemove();
        }}
        onCancel={() => setConfirmBatch(false)}
      />
    </div>
  );
}

export default Alerts;
