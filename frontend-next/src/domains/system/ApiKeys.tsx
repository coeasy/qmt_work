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
  Spinner,
  type Column,
} from "@/design/primitives";
import { systemApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import type { ApiKeyItem } from "@/shared/types";
import s from "../domain.module.css";

/**
 * API Key 管理。
 *
 * ★ 契约要点（apikeys.py）：
 *   - 主键字段是 id（int）；列表**只返回 key_prefix**（hash 前 8 位），永不回显明文
 *   - 创建与轮换是**唯一**能看到明文 api_key 的时刻，本页对此做一次性高亮提示
 *   - 批量删除的 body 键是 ids；clean-unused 按「超过 N 天未使用」清理
 *   - 轮换语义：新密钥立即生效、旧密钥立即失效，grace_until 只是宽限标记
 *   - scopes 是逗号分隔字符串（如 market,trade,account）
 */
export function ApiKeys() {
  const keys = useAsync<ApiKeyItem[]>(() => systemApi.apiKeys(), []);

  const [name, setName] = useState("");
  const [scopes, setScopes] = useState("market,trade,account");
  const [rateLimit, setRateLimit] = useState("0");
  const [ipAllow, setIpAllow] = useState("");
  const [expiresAt, setExpiresAt] = useState("");

  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [plaintext, setPlaintext] = useState<{ label: string; key: string } | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  /** 批量删除的模态确认（批量 ⇒ ConfirmModal，见按钮处注释） */
  const [confirmBatch, setConfirmBatch] = useState(false);
  /** 「清理 N 天未使用」会**真删**密钥且没有预演 ⇒ 必须确认 */
  const [confirmClean, setConfirmClean] = useState(false);
  const [cleanDays, setCleanDays] = useState("30");

  const create = async () => {
    setBusy(true);
    setBanner(null);
    setPlaintext(null);
    try {
      const res = await systemApi.createApiKey({
        name,
        scopes,
        rate_limit: Number(rateLimit) || 0,
        ip_allow: ipAllow,
        expires_at: expiresAt,
      });
      setPlaintext({ label: `新建密钥 #${res.id}`, key: res.api_key });
      setBanner({ tone: "ok", text: `密钥 #${res.id} 已创建，请立即复制明文（此后不再显示）` });
      setName("");
      await keys.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const rotate = async (kid: number) => {
    setBusy(true);
    setBanner(null);
    setPlaintext(null);
    try {
      const res = await systemApi.rotateApiKey(kid);
      setPlaintext({ label: `轮换后密钥 #${res.id}`, key: res.api_key });
      setBanner({ tone: "ok", text: `密钥 #${res.id} 已轮换，宽限标记至 ${res.grace_until}` });
      await keys.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (kid: number) => {
    setBusy(true);
    setBanner(null);
    try {
      await systemApi.deleteApiKey(kid);
      setBanner({ tone: "ok", text: `已删除密钥 #${kid}` });
      setSelected((p) => p.filter((x) => x !== kid));
      await keys.reload();
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
      const res = await systemApi.batchDeleteApiKeys(selected);
      setBanner({ tone: "ok", text: `已批量删除 ${res.deleted} 个密钥` });
      setSelected([]);
      await keys.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const cleanUnused = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await systemApi.cleanUnusedApiKeys(Number(cleanDays) || 30);
      setBanner({ tone: "ok", text: `已清理 ${res.deleted} 个超过 ${cleanDays} 天未使用的密钥（cutoff ${res.cutoff}）` });
      await keys.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const toggleEnabled = async (k: ApiKeyItem) => {
    setBusy(true);
    setBanner(null);
    try {
      const next = k.status === "active" ? "disabled" : "active";
      await systemApi.updateApiKey(k.id, { status: next });
      setBanner({ tone: "ok", text: `密钥 #${k.id} 状态已改为 ${next}` });
      await keys.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const cols: Column<ApiKeyItem>[] = [
    { key: "id", header: "ID", width: 56, mono: true, render: (r) => String(r.id) },
    { key: "name", header: "名称", width: 140, render: (r) => r.name || "--" },
    { key: "prefix", header: "前缀", width: 110, mono: true, render: (r) => r.key_prefix || "--" },
    { key: "scopes", header: "权限", width: 200, mono: true, render: (r) => r.scopes || "--" },
    {
      key: "rate",
      header: "限速",
      width: 70,
      align: "right",
      mono: true,
      render: (r) => (r.rate_limit ? String(r.rate_limit) : "不限"),
    },
    {
      key: "status",
      header: "状态",
      width: 76,
      render: (r) => (
        <Badge tone={r.status === "active" ? "success" : r.status === "expired" ? "warning" : "neutral"}>
          {r.status}
        </Badge>
      ),
    },
    {
      key: "used",
      header: "调用次数",
      width: 84,
      align: "right",
      mono: true,
      render: (r) => String(r.use_count ?? 0),
    },
    { key: "lastUsed", header: "最近使用", width: 150, mono: true, render: (r) => r.last_used_at || "从未" },
    { key: "created", header: "创建", width: 150, mono: true, render: (r) => r.created_at },
    { key: "ip", header: "IP 白名单", width: 120, mono: true, render: (r) => r.ip_allow || "不限" },
    {
      key: "act",
      header: "操作",
      width: 200,
      render: (r) => (
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={selected.includes(r.id)}
            onChange={(e) =>
              setSelected((p) => (e.target.checked ? [...p, r.id] : p.filter((x) => x !== r.id)))
            }
            aria-label={`选择密钥 ${r.id}`}
          />
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void toggleEnabled(r)}>
            {r.status === "active" ? "停用" : "启用"}
          </Button>
          {/* 轮换会让旧密钥立即失效（在用调用方立刻中断）⇒ 两段式确认，
              与同行的「删除」同口径，避免一处弹窗一处内联两种交互。 */}
          <ConfirmButton
            disabled={busy}
            confirmText="确认轮换"
            title={`轮换密钥 #${r.id}，旧密钥将立即失效`}
            onConfirm={() => void rotate(r.id)}
          >
            轮换
          </ConfirmButton>
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
        {/* ★ 批量删除走 ConfirmModal（同 Alerts/Webhooks），理由见 ConfirmButton 文档：
            批量删除属高风险，要让人读一遍条数再确认。 */}
        <Button
          size="sm"
          variant="danger"
          disabled={busy || selected.length === 0}
          onClick={() => setConfirmBatch(true)}
        >
          批量删除（{selected.length}）
        </Button>
        <span className={s.spacer} />
        <Input value={cleanDays} onChange={(e) => setCleanDays(e.target.value)} mono style={{ width: 60 }} />
        {/*
          ★ 这个按钮**真删密钥**（后端 DELETE FROM api_keys）且**没有预演**，
            原先点一下就执行 —— 一次误触可能废掉还在用的集成（脚本 / MCP / 定时任务）。
        */}
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirmClean(true)}>
          清理 N 天未使用
        </Button>
        <Button size="sm" variant="ghost" onClick={() => void keys.reload()}>
          刷新
        </Button>
      </div>

      {banner && (
        <div className={`${s.note} ${banner.tone === "ok" ? s.noteOk : s.noteError}`}>{banner.text}</div>
      )}

      {plaintext && (
        <div className={`${s.note} ${s.noteWarn}`}>
          <b>{plaintext.label}</b> 明文密钥（仅此一次显示，请立即复制保存）：
          <div style={{ marginTop: 6, display: "flex", gap: 8, alignItems: "center" }}>
            <code className={s.mono} style={{ wordBreak: "break-all", flex: 1 }}>
              {plaintext.key}
            </code>
            <Button
              size="sm"
              onClick={() => {
                void navigator.clipboard?.writeText(plaintext.key);
              }}
            >
              复制
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setPlaintext(null)}>
              我已保存
            </Button>
          </div>
        </div>
      )}

      <Panel title="新建密钥">
        <div className={s.cols4}>
          <FormRow label="名称">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如 dashboard" />
          </FormRow>
          <FormRow label="权限">
            <Input value={scopes} onChange={(e) => setScopes(e.target.value)} mono placeholder="market,trade,account" />
          </FormRow>
          <FormRow label="限速(次/分)">
            <Input value={rateLimit} onChange={(e) => setRateLimit(e.target.value)} mono placeholder="0=不限" />
          </FormRow>
          <FormRow label="IP 白名单">
            <Input value={ipAllow} onChange={(e) => setIpAllow(e.target.value)} mono placeholder="逗号分隔，空=不限" />
          </FormRow>
          <FormRow label="过期时间">
            <Input
              value={expiresAt}
              onChange={(e) => setExpiresAt(e.target.value)}
              mono
              placeholder="YYYY-MM-DDTHH:MM:SS，空=不过期"
            />
          </FormRow>
        </div>
        <div style={{ marginTop: 8 }}>
          <Button size="sm" disabled={busy} onClick={() => void create()}>
            创建密钥
          </Button>
        </div>
      </Panel>

      <Panel flush className={s.grow} title={`密钥列表（${keys.data?.length ?? 0}）`}>
        <div className={s.tableArea}>
          {keys.loading && !keys.data ? (
            <Spinner label="加载中…" />
          ) : keys.error ? (
            <div className={`${s.note} ${s.noteError}`} style={{ margin: 8 }}>
              {keys.error}
            </div>
          ) : (keys.data?.length ?? 0) === 0 ? (
            <EmptyState text="暂无 API Key —— 在上方「新建密钥」填写名称与权限后创建" />
          ) : (
            <DataTable columns={cols} rows={keys.data ?? []} rowKey={(r) => String(r.id)} rowHeight={24} />
          )}
        </div>
      </Panel>

      <ConfirmModal
        open={confirmBatch}
        title="批量删除 API Key"
        danger
        confirmText={`确认删除 ${selected.length} 条`}
        message={
          <>
            将删除选中的 <b>{selected.length}</b> 个密钥。删除后
            <b>使用这些密钥的客户端（脚本 / MCP / 定时任务）会立即失效</b>
            （后端会同步作废缓存）。
          </>
        }
        warn="删除后不可恢复"
        onConfirm={() => {
          setConfirmBatch(false);
          void batchRemove();
        }}
        onCancel={() => setConfirmBatch(false)}
      />

      <ConfirmModal
        open={confirmClean}
        title="清理长期未使用的 API Key"
        danger
        confirmText="确认清理"
        message={
          <>
            将删除超过 <b>{Number(cleanDays) || 30}</b> 天未使用的密钥；
            「从未使用」的按其<b>创建时间</b>判断，同样需超过该天数才会被删。
            <br />
            ⚠ 这个操作<b>没有预演</b>：点下去直接删，界面只在事后提示删了几个。
            使用中的客户端若正用着这些密钥会立即失效。
          </>
        }
        warn="删除后不可恢复"
        onConfirm={() => {
          setConfirmClean(false);
          void cleanUnused();
        }}
        onCancel={() => setConfirmClean(false)}
      />
    </div>
  );
}

export default ApiKeys;
