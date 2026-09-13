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

  const create = async () => {
    if (!url.trim()) {
      setBanner({ tone: "error", text: "URL 不能为空" });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const res = await webhookApi.create({
        name,
        url,
        events,
        enabled: enabled === "1",
      });
      setBanner({ tone: "ok", text: `已创建订阅 #${res.id}` });
      setName("");
      setUrl("");
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
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void test(r.id)}>
            测试
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void remove(r.id)}>
            删除
          </Button>
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
        <Button size="sm" variant="ghost" disabled={busy || selected.length === 0} onClick={() => void batchRemove()}>
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

      <Panel title="新建订阅">
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
          <Button size="sm" disabled={busy} onClick={() => void create()}>
            创建
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
            <EmptyState text="暂无 Webhook 订阅" />
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
            <EmptyState text="暂无投递记录" />
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
    </div>
  );
}

export default Webhooks;
