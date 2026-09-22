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
import { webhookApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import type { WebhookDelivery, WebhookSub } from "@/shared/types";
import s from "../domain.module.css";

/**
 * 出站 Webhook 订阅。
 *
 * ★ 契约要点（webhooks.py）：
 *   - 主键字段是 id（int），路径参数 /webhooks/{sid} 用 int
 *   - 批量删除的 body 键是 ids（不是 sids）
 *   - events 是字符串（逗号分隔或 "*"），不是数组
 *   - /webhooks/{sid}/test 真实投递一次，返回结果对象
 *   - /webhooks/deliveries 支持 sid=0（全部）+ limit
 */
export function Webhooks() {
  const subs = useAsync<WebhookSub[]>(() => webhookApi.list(), []);
  const deliveries = useAsync<WebhookDelivery[]>(() => webhookApi.deliveries(0, 50), []);

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState("*");
  const [enabled, setEnabled] = useState("1");
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  /** 批量删除的模态确认（批量 ⇒ ConfirmModal，见按钮处注释） */
  const [confirmBatch, setConfirmBatch] = useState(false);
  /** 非空 = 正在编辑该订阅（★ 新建与编辑共用 POST，靠这个 id 区分） */
  const [editingId, setEditingId] = useState<number | null>(null);

  const resetForm = () => {
    setEditingId(null);
    setName("");
    setUrl("");
    setEvents("*");
    setEnabled("1");
  };

  const startEdit = (r: WebhookSub) => {
    setEditingId(r.id);
    setName(r.name ?? "");
    setUrl(r.url ?? "");
    setEvents(r.events ?? "*");
    setEnabled(r.enabled === 0 ? "0" : "1");
    setBanner(null);
  };

  const create = async () => {
    if (!url.trim()) {
      setBanner({ tone: "error", text: "URL 不能为空" });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const res = await webhookApi.create({
        // ★ 带 id 即更新 —— 后端 save_sub 按 id 走 UPDATE，不要去找 PUT
        ...(editingId ? { id: editingId } : {}),
        name,
        url,
        events,
        enabled: enabled === "1",
      });
      setBanner({
        tone: "ok",
        text: editingId ? `已保存订阅 #${res.id}` : `已创建订阅 #${res.id}`,
      });
      resetForm();
      await subs.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const test = async (sid: number) => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await webhookApi.test(sid);
      setBanner({ tone: "ok", text: `已投递测试：${JSON.stringify(res)}` });
      await deliveries.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (sid: number) => {
    setBusy(true);
    setBanner(null);
    try {
      await webhookApi.remove(sid);
      setBanner({ tone: "ok", text: `已删除订阅 #${sid}` });
      setSelected((prev) => prev.filter((x) => x !== sid));
      await subs.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const batchRemove = async () => {
    if (selected.length === 0) return;
    setBusy(true);
    setBanner(null);
    try {
      const res = await webhookApi.batchDelete(selected);
      setBanner({ tone: "ok", text: `已批量删除 ${res.deleted} 条订阅` });
      setSelected([]);
      await subs.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const subCols: Column<WebhookSub>[] = [
    { key: "id", header: "ID", width: 56, mono: true, render: (r) => String(r.id) },
    { key: "name", header: "名称", width: 130, render: (r) => r.name || "--" },
    { key: "url", header: "URL", render: (r) => <span className={s.mono}>{r.url}</span> },
    { key: "events", header: "事件", width: 160, mono: true, render: (r) => r.events || "*" },
    {
      key: "enabled",
      header: "状态",
      width: 76,
      render: (r) => (
        <Badge tone={r.enabled === 0 ? "neutral" : "success"}>{r.enabled === 0 ? "停用" : "启用"}</Badge>
      ),
    },
    {
      key: "act",
      header: "操作",
      width: 140,
      render: (r) => (
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={selected.includes(r.id)}
            onChange={(e) =>
              setSelected((prev) => (e.target.checked ? [...prev, r.id] : prev.filter((x) => x !== r.id)))
            }
            aria-label={`选择订阅 ${r.id}`}
          />
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => startEdit(r)}>
            编辑
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void test(r.id)}>
            测试
          </Button>
          <ConfirmButton disabled={busy} onConfirm={() => void remove(r.id)}>
            删除
          </ConfirmButton>
        </div>
      ),
    },
  ];

  const deliveryCols: Column<WebhookDelivery>[] = [
    { key: "id", header: "ID", width: 60, mono: true, render: (r) => String(r.id ?? "--") },
    { key: "sid", header: "订阅", width: 60, mono: true, render: (r) => String(r.sid ?? "--") },
    { key: "event", header: "事件", width: 140, mono: true, render: (r) => r.event ?? "--" },
    { key: "url", header: "URL", render: (r) => <span className={s.mono}>{r.url ?? "--"}</span> },
    {
      key: "status",
      header: "HTTP",
      width: 70,
      align: "right",
      mono: true,
      render: (r) =>
        r.status === undefined ? (
          "--"
        ) : (
          <span style={{ color: r.status >= 200 && r.status < 300 ? "var(--success)" : "var(--danger)" }}>
            {r.status}
          </span>
        ),
    },
    {
      key: "err",
      header: "错误",
      width: 180,
      render: (r) => (r.error ? <span style={{ color: "var(--danger)" }}>{r.error}</span> : null),
    },
    { key: "ts", header: "时间", width: 150, mono: true, render: (r) => r.ts ?? "--" },
  ];

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        {/* ★ 批量删除走 ConfirmModal（判据同 Alerts.tsx）：高风险、低频，
            要让人读一遍「将删除 N 条」再确认，而不是靠第二次点击形成肌肉记忆。 */}
        <Button
          size="sm"
          variant="danger"
          disabled={busy || selected.length === 0}
          onClick={() => setConfirmBatch(true)}
        >
          批量删除（{selected.length}）
        </Button>
        <span className={s.spacer} />
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            void subs.reload();
            void deliveries.reload();
          }}
        >
          刷新
        </Button>
      </div>

      {banner && (
        <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>{banner.text}</div>
      )}

      <Panel
        title={editingId ? `编辑订阅 #${editingId}` : "新建订阅"}
        extra={
          editingId ? (
            <Button size="sm" variant="ghost" onClick={resetForm}>
              取消编辑
            </Button>
          ) : null
        }
      >
        <div className={s.cols4}>
          <FormRow label="名称">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="可选" />
          </FormRow>
          <FormRow label="URL">
            <Input value={url} onChange={(e) => setUrl(e.target.value)} mono placeholder="https://..." />
          </FormRow>
          <FormRow label="事件">
            <Input value={events} onChange={(e) => setEvents(e.target.value)} mono placeholder="* 或 a,b" />
          </FormRow>
          <FormRow label="启用">
            <Select
              value={enabled}
              onChange={(e) => setEnabled(e.target.value)}
              options={[
                { value: "1", label: "启用" },
                { value: "0", label: "停用" },
              ]}
            />
          </FormRow>
        </div>
        <div style={{ marginTop: 8 }}>
          <Button size="sm" variant="primary" disabled={busy} onClick={() => void create()}>
            {editingId ? "保存修改" : "创建"}
          </Button>
        </div>
      </Panel>

      <Panel flush className={s.grow} title={`订阅列表（${subs.data?.length ?? 0}）`}>
        <div className={s.tableArea}>
          {subs.loading && !subs.data ? (
            <Spinner label="加载中…" />
          ) : subs.error ? (
            <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
              {subs.error}
            </div>
          ) : (subs.data?.length ?? 0) === 0 ? (
            <EmptyState text="暂无 Webhook 订阅 —— 在上方填写回调 URL 与订阅事件后点「创建」" />
          ) : (
            <DataTable columns={subCols} rows={subs.data ?? []} rowKey={(r) => String(r.id)} rowHeight={24} />
          )}
        </div>
      </Panel>

      <Panel flush title={`投递记录（${deliveries.data?.length ?? 0}）`}>
        <div className={s.tableArea} style={{ maxHeight: 240 }}>
          {deliveries.error ? (
            <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
              {deliveries.error}
            </div>
          ) : (deliveries.data?.length ?? 0) === 0 ? (
            <EmptyState text="暂无投递记录 —— 订阅被触发后，每次投递的结果与失败原因会记在这里" />
          ) : (
            <DataTable
              columns={deliveryCols}
              rows={deliveries.data ?? []}
              rowKey={(r, i) => String(r.id ?? i)}
              rowHeight={22}
            />
          )}
        </div>
      </Panel>

      <ConfirmModal
        open={confirmBatch}
        title="批量删除 Webhook 订阅"
        danger
        confirmText={`确认删除 ${selected.length} 条`}
        message={
          <>
            将删除选中的 <b>{selected.length}</b> 条订阅，删除后不再向这些地址推送事件。
            <br />
            若只是想暂停推送，把订阅改成「停用」更安全。
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

export default Webhooks;
