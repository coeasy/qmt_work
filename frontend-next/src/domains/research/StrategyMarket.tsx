import { useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  FormRow,
  Input,
  Panel,
  type Column,
} from "@/design/primitives";
import {
  strategyMarketApi,
  type MarketCatalogItem,
  type MarketStrategy,
} from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import s from "../domain.module.css";

/**
 * 策略市场（P1 新建页）：模板目录 / 发布 / 安装到 QMT 客户端 / 导入导出。
 *
 * ## 为什么必须补这个页面
 *
 * 后端 `/strategy-market/*` 八个端点（catalog / market / 单条 / publish / install /
 * export / import / export-json / import-json）**早就齐了**，但 `src/` 里零调用、
 * `routes.tsx` 里无页面 —— 于是「有没有现成策略可以用、我写的策略怎么复用」
 * 只能靠 curl。这与通知渠道、回测同源：**能力已实现但界面不可达**。
 *
 * ## 契约要点（照抄 `app/routes/strategy_market.py`）
 *
 * - `install` 需要 `client_path`（QMT 客户端安装目录），**空值直接 400** ⇒
 *   界面必须让用户填，且点了没反应时要能看出是缺路径；
 * - `publish` 的 `content` 是策略正文（Python），`strategy_id` 可省略
 *   （后端会生成 `usr_xxx`）；
 * - `import` / `import-json` 收的是**服务端可见的本地文件路径**，且文件必须存在
 *   ⇒ 路径填错后端 400「bundle 不存在」，界面要把这句原因原样显示出来；
 * - 列表行的 `tags` 由后端从 `tags_json` 解开，前端不要自己 parse。
 */

export function StrategyMarket() {
  const catalog = useAsync<MarketCatalogItem[]>(() => strategyMarketApi.catalog(), []);
  const [tag, setTag] = useState("");
  const [appliedTag, setAppliedTag] = useState("");
  const list = useAsync<MarketStrategy[]>(
    () => strategyMarketApi.list(appliedTag || undefined),
    [appliedTag],
  );
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);

  /* ---------- 安装 ---------- */
  const [clientPath, setClientPath] = useState("");

  /* ---------- 发布 ---------- */
  const [pId, setPId] = useState("");
  const [pTitle, setPTitle] = useState("");
  const [pAuthor, setPAuthor] = useState("");
  const [pType, setPType] = useState("custom");
  const [pTags, setPTags] = useState("");
  const [pContent, setPContent] = useState("");

  /* ---------- 导入导出 ---------- */
  const [ioPath, setIoPath] = useState("");
  const [selected, setSelected] = useState<string[]>([]);

  const rows = list.data ?? [];

  const fail = (what: string, e: unknown) =>
    setBanner({ tone: "error", text: `${what}失败：${e instanceof Error ? e.message : String(e)}` });

  const install = async (id: string) => {
    if (!clientPath.trim()) {
      // ★ 后端对空 client_path 直接 400 —— 这里先拦下并说清楚缺什么
      setBanner({ tone: "warn", text: "请先填写 QMT 客户端安装目录（安装会写入其 mpython 目录）" });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const r = await strategyMarketApi.install({ id, client_path: clientPath.trim() });
      setBanner({ tone: "ok", text: `已安装 #${id} → ${String(r?.path ?? r?.file ?? "客户端目录")}` });
      await list.reload();
    } catch (e) {
      fail("安装", e);
    } finally {
      setBusy(false);
    }
  };

  const publish = async () => {
    if (!pContent.trim()) {
      setBanner({ tone: "warn", text: "策略正文不能为空" });
      return;
    }
    setBusy(true);
    setBanner(null);
    try {
      const rec = await strategyMarketApi.publish({
        strategy_id: pId.trim() || undefined,
        title: pTitle.trim() || pId.trim() || "未命名策略",
        author: pAuthor.trim() || undefined,
        type: pType.trim() || "custom",
        tags: pTags.split(",").map((x) => x.trim()).filter(Boolean),
        content: pContent,
      });
      setBanner({ tone: "ok", text: `已发布 #${rec.id}` });
      setPContent("");
      setPTitle("");
      await list.reload();
    } catch (e) {
      fail("发布", e);
    } finally {
      setBusy(false);
    }
  };

  const exportBundle = async () => {
    if (!selected.length) {
      setBanner({ tone: "warn", text: "请先勾选要导出的策略" });
      return;
    }
    setBusy(true);
    try {
      const r = await strategyMarketApi.exportBundle({ ids: selected });
      setBanner({ tone: "ok", text: `已导出 ${selected.length} 条 → ${String(r?.path ?? "（服务端临时目录）")}` });
    } catch (e) {
      fail("导出", e);
    } finally {
      setBusy(false);
    }
  };

  const doImport = async (kind: "zip" | "json") => {
    if (!ioPath.trim()) {
      setBanner({ tone: "warn", text: "请填写服务端可见的文件路径（文件必须存在）" });
      return;
    }
    setBusy(true);
    try {
      const r = kind === "zip"
        ? await strategyMarketApi.importBundle({ path: ioPath.trim() })
        : await strategyMarketApi.importJson({ path: ioPath.trim() });
      setBanner({ tone: "ok", text: `已导入：${String(r?.imported ?? r?.id ?? "完成")}` });
      await list.reload();
    } catch (e) {
      fail("导入", e);
    } finally {
      setBusy(false);
    }
  };

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const catCols: Column<MarketCatalogItem>[] = [
    { key: "id", header: "ID", width: 130, mono: true, render: (r) => String(r.id) },
    { key: "name", header: "名称", width: 140, render: (r) => String(r.name ?? "—") },
    { key: "type", header: "类型", width: 110, mono: true, render: (r) => String(r.type ?? "—") },
    {
      key: "params", header: "参数", width: 180, mono: true,
      render: (r) => String((r.params_schema ?? []).length),
    },
    {
      key: "desc", header: "说明", render: (r) => String(r.description ?? "—"),
    },
  ];

  const cols: Column<MarketStrategy>[] = [
    {
      key: "sel", header: "", width: 44,
      render: (r) => (
        <input
          type="checkbox"
          aria-label={`选择 ${r.id}`}
          checked={selected.includes(r.id)}
          onChange={() => toggle(r.id)}
        />
      ),
    },
    { key: "id", header: "ID", width: 140, mono: true, render: (r) => String(r.id) },
    { key: "title", header: "标题", width: 160, render: (r) => String(r.title ?? "—") },
    { key: "author", header: "作者", width: 100, render: (r) => String(r.author ?? "—") },
    {
      key: "type", header: "类型", width: 100, mono: true,
      render: (r) => <Badge tone="info">{String(r.type ?? "—")}</Badge>,
    },
    {
      key: "tags", header: "标签", width: 140,
      render: (r) => (r.tags?.length ? String(r.tags.join(" / ")) : "—"),
    },
    {
      key: "dl", header: "安装次数", width: 90, mono: true,
      render: (r) => String(r.downloads ?? 0),
    },
    {
      key: "created", header: "创建", width: 150, mono: true,
      render: (r) => String(r.created_at ?? "—").replace("T", " ").slice(0, 19),
    },
    {
      key: "act", header: "操作", width: 150,
      render: (r) => (
        <div className={s.actions}>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void install(r.id)}>
            安装
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className={s.page}>
      {banner ? (
        <div className={
          banner.tone === "ok" ? s.noteOk : banner.tone === "warn" ? s.noteWarn : s.noteError
        }>
          {banner.text}
        </div>
      ) : null}

      <Panel title="内置模板目录">
        {catalog.error ? (
          <EmptyState text={`模板目录加载失败：${catalog.error}`} />
        ) : (catalog.data?.length ?? 0) === 0 ? (
          <EmptyState text="暂无内置模板" />
        ) : (
          <div className={s.tableArea} style={{ maxHeight: 220 }}>
            <DataTable columns={catCols} rows={catalog.data ?? []} rowKey={(r) => String(r.id)} />
          </div>
        )}
      </Panel>

      <Panel
        title="市场策略"
        extra={
          <div className={s.actions}>
            <span className={s.muted}>已选 {selected.length} 项</span>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => void list.reload()}>
              刷新
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => void exportBundle()}>
              导出所选
            </Button>
          </div>
        }
      >
        <div className={s.form}>
          <FormRow label="QMT 客户端目录">
            <Input
              value={clientPath}
              onChange={(e) => setClientPath(e.target.value)}
              placeholder="安装到该目录的 mpython 下，如 D:/gjzq_qmt"
            />
          </FormRow>
          <FormRow label="按标签过滤">
            <div className={s.actions}>
              <Input value={tag} onChange={(e) => setTag(e.target.value)} placeholder="如 均线" />
              <Button size="sm" variant="ghost" onClick={() => setAppliedTag(tag.trim())}>
                应用
              </Button>
              {appliedTag ? (
                <Button size="sm" variant="ghost" onClick={() => { setTag(""); setAppliedTag(""); }}>
                  清除
                </Button>
              ) : null}
            </div>
          </FormRow>
        </div>
        {list.error ? (
          <EmptyState text={`市场策略加载失败：${list.error}`} />
        ) : rows.length === 0 ? (
          <EmptyState text={
            appliedTag
              ? `没有带标签「${appliedTag}」的策略`
              : "市场里还没有策略。可以在下方「发布策略」把你的策略提交进来。"
          } />
        ) : (
          <div className={s.tableArea}>
            <DataTable columns={cols} rows={rows} rowKey={(r) => String(r.id)} />
          </div>
        )}
      </Panel>

      <Panel title="发布策略">
        <div className={s.form}>
          <FormRow label="策略 ID（可留空）">
            <Input value={pId} onChange={(e) => setPId(e.target.value)} placeholder="留空则由后端生成 usr_xxx" />
          </FormRow>
          <FormRow label="标题">
            <Input value={pTitle} onChange={(e) => setPTitle(e.target.value)} />
          </FormRow>
          <FormRow label="作者">
            <Input value={pAuthor} onChange={(e) => setPAuthor(e.target.value)} placeholder="anonymous" />
          </FormRow>
          <FormRow label="类型">
            <Input value={pType} onChange={(e) => setPType(e.target.value)} placeholder="custom" />
          </FormRow>
          <FormRow label="标签（逗号分隔）">
            <Input value={pTags} onChange={(e) => setPTags(e.target.value)} />
          </FormRow>
          <FormRow label="策略正文">
            {/* 没有 TextArea 基础组件 ⇒ 用原生 textarea；正文是 Python，必须多行 */}
            <textarea
              value={pContent}
              onChange={(e) => setPContent(e.target.value)}
              rows={8}
              aria-label="策略正文"
              style={{
                width: "100%", minHeight: 140, resize: "vertical",
                fontFamily: "ui-monospace, monospace", fontSize: 12,
              }}
            />
          </FormRow>
          <div className={s.actions}>
            <Button variant="primary" size="sm" disabled={busy} onClick={() => void publish()}>
              发布
            </Button>
          </div>
        </div>
      </Panel>

      <Panel title="导入 / 导出（服务端路径）">
        <div className={s.form}>
          <FormRow label="文件路径">
            <Input
              value={ioPath}
              onChange={(e) => setIoPath(e.target.value)}
              placeholder="服务端可见的 .zip / .json 路径，文件必须存在"
            />
          </FormRow>
          <div className={s.actions}>
            <Button size="sm" disabled={busy} onClick={() => void doImport("zip")}>
              导入 bundle
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => void doImport("json")}>
              导入 JSON
            </Button>
            <span className={s.muted}>导入的文件必须存在于运行后端这台机器上</span>
          </div>
        </div>
      </Panel>
    </div>
  );
}

export default StrategyMarket;
