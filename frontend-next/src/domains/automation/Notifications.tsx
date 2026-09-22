import { useMemo, useState } from "react";
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
  type Column,
} from "@/design/primitives";
import { systemApi, type NotificationConfig } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import s from "../domain.module.css";

/**
 * 通知渠道（P1-H 新建页）：告警 / 风控 / 任务失败到底往哪儿发。
 *
 * ## 为什么必须补这个页面
 *
 * 后端 ``/notifications`` 的增删改查、批量删除、测试发送、发送记录**早就齐了**，
 * 但界面上没有入口 —— 于是「告警发出去没有」只能靠翻数据库。
 * 通知是**最后一道可感知性**：任务失败、风控熔断、冷仓切换失败全靠它告知用户，
 * 没有配置入口就等于这条链路整体失效。
 *
 * ## 契约要点（照抄 ``app/routes/notifications.py`` + ``gateway/notifier.py``）
 *
 * - ``POST /notifications`` **带 id 即更新**（后端按 id 走 UPDATE），新建与编辑同一个端点；
 * - 批量删除 body 键是 ``ids``（与 alerts / webhooks / api-keys 一致），**不是** ``nids``；
 * - 通道取值是 ``webhook / dingtalk / wecom / feishu / email``，
 *   写成 ``dingding`` 之类后端会在**发送时**才报 ``unknown channel`` —— 所以这里
 *   用 Select 枚举而不是自由文本，把错误消灭在输入层；
 * - ``events`` 是字符串（``*`` 或逗号分隔），**不是**数组；
 * - 参数按通道不同：机器人只要 ``url``，钉钉另有 ``secret``，邮件要
 *   ``host/port/user/password/to`` —— 前端必须按通道渲染，否则用户填了 URL
 *   却选了 email，保存成功但永远发不出去。
 */

const CHANNELS = [
  { value: "webhook", label: "自定义 Webhook" },
  { value: "dingtalk", label: "钉钉机器人" },
  { value: "wecom", label: "企业微信机器人" },
  { value: "feishu", label: "飞书机器人" },
  { value: "email", label: "邮件（SMTP）" },
];

/** 各通道真正会去读的 params 键（照抄 notifier._send_one 的分支）。 */
const CHANNEL_PARAMS: Record<string, { key: string; label: string; secret?: boolean }[]> = {
  webhook: [{ key: "url", label: "URL" }],
  dingtalk: [
    { key: "url", label: "机器人 Webhook URL" },
    { key: "secret", label: "加签密钥（可选）", secret: true },
  ],
  wecom: [{ key: "url", label: "机器人 Webhook URL" }],
  feishu: [{ key: "url", label: "机器人 Webhook URL" }],
  email: [
    { key: "host", label: "SMTP 主机" },
    { key: "port", label: "端口（默认 587）" },
    { key: "user", label: "账号" },
    { key: "password", label: "密码 / 授权码", secret: true },
    { key: "to", label: "收件人" },
  ],
};

const DEFAULT_TEMPLATE = "{{title}}\n{{body}}";

/**
 * 该渠道**真正会去读的必填参数**里还缺哪几个。
 *
 * ## 为什么必须算这个
 *
 * 后端保存渠道时**不校验参数完整性**：只填了名字就保存，界面显示「启用」，
 * 于是「告警发出去没有」在一个看起来完全正常的配置页面上静默失败 ——
 * 发送记录里只会留一句 `webhook url missing`。本项目实测就撞上了 2 条
 * 这样的僵尸渠道（name 为空、params 为空、enabled=1）。
 * 把「缺什么」摆到列表里，是把这类假成功变成**看得见的红色**。
 */
function missingParams(cfg: NotificationConfig): string[] {
  const need = CHANNEL_PARAMS[String(cfg.channel ?? "")] ?? [];
  const p = (cfg.params ?? {}) as Record<string, unknown>;
  return need.filter((f) => !String(p[f.key] ?? "").trim()).map((f) => f.label);
}

type Banner = { tone: "ok" | "error" | "warn"; text: string } | null;

export function Notifications() {
  const list = useAsync<NotificationConfig[]>(() => systemApi.notifications(), []);
  const logs = useAsync<Record<string, unknown>[]>(
    () => systemApi.notificationLogs(30) as Promise<Record<string, unknown>[]>,
    [],
  );

  const [editingId, setEditingId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [channel, setChannel] = useState("webhook");
  const [events, setEvents] = useState("*");
  const [enabled, setEnabled] = useState("1");
  const [template, setTemplate] = useState(DEFAULT_TEMPLATE);
  const [params, setParams] = useState<Record<string, string>>({});
  /** 高级参数（webhook 的 payload/headers 等），JSON 合并进 params */
  const [extraJson, setExtraJson] = useState("");
  const [selected, setSelected] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [cleanup, setCleanup] = useState(false);
  const [banner, setBanner] = useState<Banner>(null);

  const rows = list.data ?? [];
  const fields = CHANNEL_PARAMS[channel] ?? [];
  /** 缺必填参数的渠道 —— 「启用」着却一次也发不出去的那批。 */
  const incomplete = rows.filter((r) => missingParams(r).length > 0);

  const resetForm = () => {
    setEditingId(null);
    setName("");
    setChannel("webhook");
    setEvents("*");
    setEnabled("1");
    setTemplate(DEFAULT_TEMPLATE);
    setParams({});
    setExtraJson("");
  };

  const startEdit = (r: NotificationConfig) => {
    setEditingId(r.id);
    setName(String(r.name ?? ""));
    setChannel(String(r.channel ?? "webhook"));
    setEvents(String(r.events ?? "*"));
    setEnabled(r.enabled ? "1" : "0");
    setTemplate(String(r.template ?? DEFAULT_TEMPLATE));
    const p = (r.params ?? {}) as Record<string, unknown>;
    setParams(
      Object.fromEntries(
        Object.entries(p).map(([k, v]) => [k, typeof v === "string" ? v : String(v ?? "")]),
      ),
    );
    setExtraJson("");
    setBanner(null);
  };

  const mergedParams = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(params)) {
      if (String(v ?? "").trim() === "") continue;
      // 端口是数字：SMTP 端口写成字符串会被 int() 吃掉吗？不会 —— 后端 int() 兼容。
      out[k] = k === "port" ? Number(v) : v;
    }
    const extra = extraJson.trim();
    if (extra) {
      try {
        Object.assign(out, JSON.parse(extra));
      } catch {
        setBanner({ tone: "error", text: "高级参数不是合法 JSON，已忽略（其余字段仍会保存）" });
      }
    }
    return out;
  }, [params, extraJson]);

  const save = async () => {
    const missing = fields.filter((f) => !String(params[f.key] ?? "").trim());
    if (missing.length) {
      setBanner({
        tone: "error",
        text: `缺少必填参数：${missing.map((f) => f.label).join("、")}`,
      });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const payload: Record<string, unknown> = {
        name: name.trim() || channel,
        channel,
        events,
        enabled: enabled === "1",
        template,
        params: mergedParams,
      };
      // ★ 带 id 即更新 —— 新建与编辑是同一个端点，别再找 PUT
      if (editingId) payload.id = editingId;
      const res = await systemApi.saveNotification(payload);
      setBanner({ tone: "ok", text: `已保存渠道 #${res.id}` });
      resetForm();
      await list.reload();
    } catch (e) {
      setBanner({ tone: "error", text: `保存失败：${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (nid: number) => {
    setBusy(true);
    try {
      await systemApi.deleteNotification(nid);
      setSelected((prev) => prev.filter((x) => x !== nid));
      await list.reload();
    } catch (e) {
      setBanner({ tone: "error", text: `删除失败：${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setBusy(false);
    }
  };

  const batchRemove = async () => {
    if (!selected.length) {
      setBanner({ tone: "warn", text: "请先勾选要删除的渠道" });
      return;
    }
    setBusy(true);
    try {
      const r = await systemApi.batchDeleteNotifications(selected);
      setBanner({ tone: "ok", text: `已删除 ${r.deleted} 个渠道` });
      setSelected([]);
      await list.reload();
    } catch (e) {
      setBanner({
        tone: "error",
        text: `批量删除失败：${e instanceof Error ? e.message : String(e)}`,
      });
    } finally {
      setBusy(false);
    }
  };

  /**
   * 一键清掉「缺必填参数」的渠道。
   *
   * 为什么需要这个动作：这类渠道**保存时后端不校验**，于是能长期以「启用」姿态
   * 躺在库里，每次触发都白跑一次发送并写一条 failed 记录。光把它标红还不够 ——
   * 用户还得自己一条条删；而这个动作是**不可逆**的，所以走 `ConfirmModal`
   * （行内两段式 `ConfirmButton` 只用于单条、低风险的行内删除）。
   */
  const removeIncomplete = async () => {
    setBusy(true);
    try {
      const r = await systemApi.batchDeleteNotifications(incomplete.map((x) => x.id));
      setBanner({ tone: "ok", text: `已清理 ${r.deleted} 个配置不完整的渠道` });
      setSelected((prev) => prev.filter((id) => !incomplete.some((x) => x.id === id)));
      await list.reload();
    } catch (e) {
      setBanner({
        tone: "error",
        text: `清理失败：${e instanceof Error ? e.message : String(e)}`,
      });
    } finally {
      setBusy(false);
      setCleanup(false);
    }
  };

  const sendTest = async (cfg?: NotificationConfig) => {
    const target: Record<string, unknown> = cfg
      ? { name: cfg.name, channel: cfg.channel, params: cfg.params ?? {},
          template: cfg.template ?? DEFAULT_TEMPLATE }
      : { name, channel, params: mergedParams, template };
    setBusy(true);
    setBanner(null);
    try {
      const r = await systemApi.testNotification({
        config: target,
        title: "测试通知",
        body: "来自 qmt_work 的通知测试。",
      });
      setBanner({ tone: "ok", text: `测试已发送（preview：${String(r.preview ?? "")}）` });
    } catch (e) {
      setBanner({
        tone: "error",
        text: `测试发送失败：${e instanceof Error ? e.message : String(e)}`,
      });
    } finally {
      setBusy(false);
    }
  };

  const toggle = (nid: number) =>
    setSelected((prev) =>
      prev.includes(nid) ? prev.filter((x) => x !== nid) : [...prev, nid],
    );

  const cols: Column<NotificationConfig>[] = [
    {
      key: "sel",
      // ⚠️ Column.header 只接受 string，全选框只能放在表头下方的工具条里
      header: "",
      width: 44,
      render: (r) => (
        <input
          type="checkbox"
          aria-label={`选择 ${r.name ?? r.id}`}
          checked={selected.includes(r.id)}
          onChange={() => toggle(r.id)}
        />
      ),
    },
    { key: "id", header: "ID", width: 56, mono: true, render: (r) => String(r.id) },
    {
      key: "name",
      header: "名称",
      width: 140,
      // ★ 空名字直白写出来 —— 表格里一片空白会被当成「还没渲染出来」
      render: (r) => {
        const n = String(r.name ?? "").trim();
        return n ? n : <span className={s.muted}>（未命名）</span>;
      },
    },
    {
      key: "channel",
      header: "渠道",
      width: 130,
      mono: true,
      render: (r) => (
        <span title={CHANNELS.find((c) => c.value === r.channel)?.label ?? ""}>
          {String(r.channel ?? "—")}
        </span>
      ),
    },
    {
      key: "enabled",
      header: "启用",
      width: 76,
      render: (r) => (
        <Badge tone={r.enabled ? "success" : "neutral"}>{r.enabled ? "启用" : "停用"}</Badge>
      ),
    },
    { key: "events", header: "事件", width: 120, mono: true, render: (r) => String(r.events ?? "*") },
    {
      key: "health",
      header: "配置",
      width: 160,
      render: (r) => {
        const miss = missingParams(r);
        return miss.length ? (
          <Badge tone="danger" title={`发送必然失败（notifier 取不到这些参数）`}>
            缺 {miss.join("、")}
          </Badge>
        ) : (
          <Badge tone="success">完整</Badge>
        );
      },
    },
    {
      key: "act",
      header: "操作",
      width: 230,
      render: (r) => (
        <div className={s.actions}>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => startEdit(r)}>
            编辑
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void sendTest(r)}>
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
      {banner ? (
        <div
          className={
            banner.tone === "ok" ? s.noteOk : banner.tone === "warn" ? s.noteWarn : s.noteError
          }
        >
          {banner.text}
        </div>
      ) : null}

      <Panel
        title={editingId ? `编辑通知渠道 #${editingId}` : "新增通知渠道"}
        extra={
          editingId ? (
            <Button size="sm" variant="ghost" onClick={resetForm}>
              取消编辑
            </Button>
          ) : null
        }
      >
        <div className={s.form}>
          <FormRow label="名称">
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="留空则用渠道名"
            />
          </FormRow>
          {/* ★ 用枚举而不是自由文本：通道写错只会在**发送时**报
              unknown channel —— 那时用户以为已经配好了。 */}
          <FormRow label="渠道">
            <Select
              value={channel}
              onChange={(e) => {
                setChannel(e.target.value);
                setParams({});
              }}
              options={CHANNELS}
            />
          </FormRow>
          {fields.map((f) => (
            <FormRow key={f.key} label={f.label}>
              <Input
                type={f.secret ? "password" : f.key === "port" ? "number" : "text"}
                value={params[f.key] ?? ""}
                onChange={(e) => setParams((p) => ({ ...p, [f.key]: e.target.value }))}
                placeholder={f.key === "port" ? "587" : ""}
              />
            </FormRow>
          ))}
          <FormRow label="事件订阅">
            <Input
              value={events}
              onChange={(e) => setEvents(e.target.value)}
              mono
              placeholder="* 或逗号分隔，如 risk.circuit,system.job_failed"
            />
          </FormRow>
          <FormRow label="模板">
            <Input value={template} onChange={(e) => setTemplate(e.target.value)} mono />
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
          <FormRow label="高级参数(JSON)">
            <Input
              value={extraJson}
              onChange={(e) => setExtraJson(e.target.value)}
              mono
              placeholder='如 {"headers":{"X-Token":"abc"}}'
            />
          </FormRow>
          <div className={s.actions}>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void save()}>
              {editingId ? "保存修改" : "创建渠道"}
            </Button>
            <Button size="sm" disabled={busy} onClick={() => void sendTest()}>
              发送测试
            </Button>
          </div>
        </div>
      </Panel>

      <Panel
        title="通知渠道列表"
        extra={
          <div className={s.actions}>
            {/* ★ 顶部再喊一次：只靠行内角标，列表一长就看不见 */}
            {incomplete.length ? (
              <>
                <Badge tone="danger" title="这些渠道发送必然失败">
                  {incomplete.length} 个渠道配置不完整
                </Badge>
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => setCleanup(true)}>
                  清理不完整
                </Button>
              </>
            ) : null}
            <span className={s.muted}>已选 {selected.length} 项</span>
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                setSelected(selected.length === rows.length ? [] : rows.map((r) => r.id))
              }
            >
              {selected.length === rows.length && rows.length > 0 ? "取消全选" : "全选"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void list.reload()}>
              刷新
            </Button>
            {/* ★ 批量删除走两段式确认（ConfirmButton）：一次误触删掉全部通知渠道，
                等于把所有告警静音。 */}
            <ConfirmButton disabled={busy || !selected.length} onConfirm={() => void batchRemove()}>
              批量删除
            </ConfirmButton>
          </div>
        }
      >
        {list.error ? (
          <EmptyState text={`通知渠道加载失败：${list.error}`} />
        ) : rows.length === 0 ? (
          <EmptyState text="尚未配置任何通知渠道。没有通知渠道时，任务失败 / 风控熔断 / 冷仓切换失败都只能靠翻数据库发现 —— 建议至少配置一个。" />
        ) : (
          <div className={s.tableArea}>
            <DataTable columns={cols} rows={rows} rowKey={(r) => String(r.id)} />
          </div>
        )}
      </Panel>

      <ConfirmModal
        open={cleanup}
        title="清理配置不完整的通知渠道"
        message={
          <>
            将删除 {incomplete.length} 个缺必填参数的渠道（
            {incomplete.map((x) => `#${x.id}`).join("、")}
            ）。它们目前一次也发不出去，只会持续产生失败记录。
          </>
        }
        warn="删除后无法恢复；需要的话请重新创建渠道并填全参数。"
        confirmText="确认清理"
        danger
        loading={busy}
        onConfirm={() => void removeIncomplete()}
        onCancel={() => setCleanup(false)}
      />

      <Panel title="最近发送记录">
        {logs.error ? (
          <EmptyState text={`发送记录加载失败：${logs.error}`} />
        ) : (logs.data?.length ?? 0) === 0 ? (
          <EmptyState text="暂无发送记录（发过的通知会记在这里，失败原因也在其中）" />
        ) : (
          <div className={s.logList}>
            {logs.data?.map((r, i) => (
              <div key={i} className={s.logRow}>
                <span className={s.mono}>{String(r.created_at ?? r.sent_at ?? "")}</span>
                <Badge tone={r.status === "ok" ? "success" : "danger"}>
                  {String(r.status ?? "—")}
                </Badge>
                <span>{String(r.title ?? "")}</span>
                {/* ★ 失败原因必须展示：否则「通知没收到」无从排查 */}
                {r.response ? <span className={s.muted}>{String(r.response)}</span> : null}
              </div>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}

export default Notifications;
