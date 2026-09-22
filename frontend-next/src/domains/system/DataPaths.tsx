import { useCallback, useEffect, useState } from "react";
import { Badge, Button, ConfirmModal, EmptyState, Input, Panel, type BadgeTone } from "@/design/primitives";
import {
  pathsApi,
  type BackupStats,
  type PathDirInfo,
  type PathKind,
  type PathsResponse,
  type PathValidateResult,
} from "@/services/api";
import s from "./offline.module.css";

/**
 * 备份占用的**文案**（导出以便单测：这三句是本块的灵魂，错了会出真事）。
 *
 * 三条纪律：
 *
 * 1. **「主库没变、已跳过」不是失败**：`last.action === "skipped"` 表示已有备份精确
 *    覆盖当前状态，把它说成「备份失败」就是错误归因；
 * 2. **超上限 ≠ 一定能回收**：预算比单份备份还小时，`min_keep` 地板会让占用停在
 *    超限状态（`reclaimable_bytes === 0`）。此时既不能说「可回收 X」，
 *    也不能说「一切正常」—— 要如实讲清「受最少 N 份保护，需手工处理」；
 * 3. **未纳管副本必须点名**：它们不在保留策略内，不显示就永远查不出「磁盘去哪了」。
 */
export function backupOccupancyText(bk: BackupStats): string {
  const cap = bk.max_total_size
    ? `上限 ${bk.max_total_size}、最多 ${bk.keep} 份、至少保留 ${bk.min_keep} 份`
    : `最多 ${bk.keep} 份（未设体积上限）`;
  const newest = bk.newest ? `；最新 ${bk.newest.name}（${bk.newest.mtime}）` : "";
  return `主库备份 ${bk.count} 份，共 ${bk.total_size}（${cap}）${newest}`;
}

export function backupLastText(bk: BackupStats): string {
  const act = bk.last?.action || "idle";
  const detail = bk.last?.detail || "";
  if (act === "created") return `最近一次备份：已完成。${detail}`;
  if (act === "skipped") return `最近一次备份：未产生新文件（主库未变化，已有备份覆盖当前状态）。${detail}`;
  if (act === "failed") return `最近一次备份：失败。${detail}`;
  return "最近一次备份：本次进程内尚未执行。";
}

/** 返回**必须显式提示**的异常文案；一切正常时返回空串。 */
export function backupWarn(bk: BackupStats): string {
  if (bk.over_budget && bk.reclaimable_bytes === 0) {
    return `备份占用 ${bk.total_size} 已超出体积上限，但为保证可还原性至少保留 ${bk.min_keep} 份，` +
      `无法再自动清理。如需释放空间，请关闭客户端后手工删除 ${bk.dir} 下的旧备份。`;
  }
  if (bk.reclaimable_bytes > 0) {
    return `按当前保留策略可回收 ${bk.reclaimable_size}` +
      `（${Math.max(0, bk.count - bk.retain_count)} 份旧备份）。`;
  }
  return "";
}

export function backupStrayText(bk: BackupStats): string {
  if (!bk.stray_count) return "";
  const names = bk.strays.map((x) => `${x.name}（${x.size_str}）`).join("、");
  return `主库目录里还有 ${bk.stray_count} 个「不受保留策略管理」的库副本，共 ${bk.stray_total_size}：` +
    `${names}。这些文件不会被自动清理，也不会被本页删除 —— 是否保留请自行判断。`;
}

export function canPruneBackups(bk: BackupStats): boolean {
  return bk.reclaimable_bytes > 0;
}

/**
 * 数据目录设置（V11 §5.3 P0-3 II）。
 *
 * ## 为什么要有这块
 *
 * 在此之前三类目录**全部写死**，用户想把几十 GB 的历史 K 线放到 D 盘只能手工改
 * `qmt_work_config.json`。而这三类的可改性**根本不一样**，界面必须如实区分：
 *
 * | 类别 | 运行期可改 | 说明 |
 * | --- | --- | --- |
 * | 导出目录 | ✅ | 改完立即生效 |
 * | 冷库目录 | ✅ | 改完立即生效，但**已有冷数据不会自动搬迁**（要手动「迁移冷库」） |
 * | 主库目录 | ❌ | 路径在进程启动时固化，写配置后**必须重启客户端** |
 *
 * ★ 把「主库」渲染成「已保存」是最恶劣的一种假成功：用户以为搬好了，重启后
 * 发现是个空库（数据还在旧位置）。所以 `requires_restart` 必须原样呈现。
 *
 * ## 纪律
 *
 * 1. **目录选择框是可选能力**：浏览器 / 单测里没有 `window.electronAPI`，
 *    必须能降级到手输路径，而不是整块灰掉；
 * 2. **校验走后端唯一入口**：前端不自己判断「这是不是系统目录」，
 *    一律 `POST /config/paths/validate`（两处各写一套规则迟早不一致）；
 * 3. **取消选择 ≠ 失败**：`selectDirectory()` 返回 `null` 表示用户点了取消，不弹错。
 */
export function DataPaths() {
  const [paths, setPaths] = useState<PathsResponse | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  /** 正在改哪一类；null = 都没在编辑 */
  const [editing, setEditing] = useState<PathKind | null>(null);
  const [draft, setDraft] = useState("");
  const [check, setCheck] = useState<PathValidateResult | null>(null);
  /** 保存后仍需重启的提示（主库专用；不随下次刷新自动消失） */
  const [restartNote, setRestartNote] = useState("");
  /** 清理旧备份的二次确认（不可逆） */
  const [pruneOpen, setPruneOpen] = useState(false);

  const load = useCallback(() => {
    void pathsApi
      .get()
      .then((r) => {
        setPaths(r);
        setErr("");
      })
      .catch((e: unknown) => {
        setPaths(null);
        setErr(e instanceof Error ? e.message : String(e));
      });
  }, []);

  useEffect(load, [load]);

  /** 目录选择框：拿不到（浏览器/单测）就什么都不做，界面仍可手输。 */
  const pickDir = (kind: PathKind) => {
    const api = typeof window !== "undefined" ? window.electronAPI : undefined;
    if (!api?.selectDirectory) return;
    const cur = paths?.[kind];
    void api
      .selectDirectory({ title: cur?.label || "选择数据目录", defaultPath: cur?.path })
      .then((picked) => {
        // null = 用户取消，不是失败
        if (picked) setDraft(picked);
        if (picked) void verify(picked);
      })
      .catch(() => undefined);
  };

  const verify = (raw: string) => {
    return pathsApi
      .validate(raw)
      .then(setCheck)
      .catch((e: unknown) =>
        setCheck({
          input: raw, ok: false, reason: e instanceof Error ? e.message : String(e),
          path: "", exists: false, writable: false, inside_install: false,
          is_system: false, is_drive_root: false,
        }),
      );
  };

  const startEdit = (kind: PathKind) => {
    setEditing(kind);
    setDraft(paths?.[kind]?.path ?? "");
    setCheck(null);
    setMsg("");
  };

  const save = (kind: PathKind) => {
    setBusy(true);
    setMsg("");
    void pathsApi
      .set(kind, draft)
      .then((r) => {
        setPaths(r);
        setEditing(null);
        setBusy(false);
        // ★ 主库改完不是「完成」，是「待重启」。这条提示必须留着，
        //   否则用户以为搬好了，重启后对着空库一脸茫然。
        setRestartNote(
          r.requires_restart
            ? `主库目录已写入配置，但当前进程仍在用旧库 ${r.current_file || ""}。` +
              `重启客户端才会生效；重启前请先把 app.db（含 -wal/-shm）复制到新目录，` +
              `否则新目录会新建一个空库。`
            : "",
        );
        setMsg(r.requires_restart ? "已写入配置，需重启客户端生效" : "已保存，立即生效");
      })
      .catch((e: unknown) => {
        setMsg(`保存失败：${e instanceof Error ? e.message : String(e)}`);
        setBusy(false);
      });
  };

  const migrate = (source = "") => {
    setBusy(true);
    setMsg("");
    void pathsApi
      .migrateCold(source)
      .then((r) => {
        setPaths(r);
        setBusy(false);
        setMsg(
          `已迁移 ${r.moved} 行（来自 ${r.source}）。源文件保留未删，确认无误后可自行清理。`,
        );
      })
      .catch((e: unknown) => {
        setMsg(`迁移失败：${e instanceof Error ? e.message : String(e)}`);
        setBusy(false);
      });
  };

  /** 立刻备份。★ 后端用 action 区分「已备份 / 未变化跳过 / 失败」，前端照抄它的文案。 */
  const runBackup = () => {
    setBusy(true);
    setMsg("");
    void pathsApi
      .runBackup()
      .then((r) => {
        setPaths(r);
        setBusy(false);
        setMsg(r.message || "备份完成");
      })
      .catch((e: unknown) => {
        setMsg(`备份失败：${e instanceof Error ? e.message : String(e)}`);
        setBusy(false);
      });
  };

  /** 按保留策略清理旧备份（不可逆）。 */
  const doPrune = () => {
    setBusy(true);
    setMsg("");
    void pathsApi
      .pruneBackups()
      .then((r) => {
        setPaths(r);
        setPruneOpen(false);
        setBusy(false);
        setMsg(
          r.removed_count > 0
            ? `已清理 ${r.removed_count} 份旧备份，释放 ${r.freed_size}。`
            : `未清理任何文件（${r.note}）。`,
        );
      })
      .catch((e: unknown) => {
        setMsg(`清理失败：${e instanceof Error ? e.message : String(e)}`);
        setPruneOpen(false);
        setBusy(false);
      });
  };

  if (err) {
    return (
      <Panel title="数据目录">
        <div className={s.body}>
          <EmptyState text={`数据目录加载失败：${err}`} />
        </div>
      </Panel>
    );
  }  if (paths === null) {
    return (
      <Panel title="数据目录">
        <div className={s.body}>
          <EmptyState text="正在读取数据目录…" />
        </div>
      </Panel>
    );
  }

  const kinds: PathKind[] = ["export", "cold", "db"];
  const info = (k: PathKind): PathDirInfo => paths[k];
  /** 主库备份占用（后端算好，前端只展示）；老后端不返回时为 undefined */
  const dbBk: BackupStats | undefined = paths.db?.backups;

  const rowTone = (d: PathDirInfo): BadgeTone =>
    d.requires_restart ? "warning" : d.configured ? "success" : "neutral";

  return (
    <Panel
      title="数据目录"
      extra={<span className={s.hint}>运行目录 {paths.install_dir}</span>}
    >
      <div className={s.body}>
        {restartNote ? <div className={s.noteError}>{restartNote}</div> : null}

        {kinds.map((k) => {
          const d = info(k);
          return (
            <div key={k}>
              <div className={s.kv}>
                <span>{d.label}</span>
                <span className={s.row}>
                  <span className={s.mono}>{d.path || "—"}</span>
                  <Badge tone={rowTone(d)}>
                    {d.requires_restart ? "改后需重启" : d.configured ? "已自定义" : "默认"}
                  </Badge>
                  {d.inside_install ? <Badge tone="info">在安装目录内</Badge> : null}
                  {d.usable === false ? <Badge tone="danger">不可用</Badge> : null}
                </span>
                <span>
                  <Button size="sm" onClick={() => startEdit(k)}>
                    更改
                  </Button>
                </span>
              </div>

              {k === "cold" ? (
                <>
                  <div className={s.note}>
                    当前冷仓 {d.rows ?? 0} 行
                    {d.file ? `（${d.file}）` : ""}
                  </div>
                  {/* ★ 配置改了但冷仓没换成功 —— 必须显式否定，
                      否则界面显示的新路径会让用户以为历史已经在新位置（然后删掉旧的）。 */}
                  {d.in_sync === false ? (
                    <div className={s.noteError}>
                      配置指向 {d.path}，但实际装配的冷仓仍是{" "}
                      {d.attached_path || "（无）"} —— 切换未生效，请重试或查看后端日志。
                    </div>
                  ) : null}
                  {(d.candidates?.length ?? 0) > 0 ? (
                    <div className={s.actions}>
                      <span className={s.hint}>发现旧冷仓，可迁入：</span>
                      {d.candidates?.map((c) => (
                        <Button
                          key={c.path}
                          size="sm"
                          disabled={busy}
                          onClick={() => migrate(c.path)}
                        >
                          迁移 {c.rows >= 0 ? `${c.rows} 行` : "?"}：{c.path}
                        </Button>
                      ))}
                    </div>
                  ) : null}
                </>
              ) : null}

              {d.note ? <div className={s.hint}>{d.note}</div> : null}

              {/* ★ 主库备份占用（2026-09-20 修复）：备份是**整库全量复制**，
                  1.14GB 主库按「保留 10 份」就是 11GB 常驻磁盘。此前这段占用
                  在界面上**一处都没有**（list_backups 零调用方），用户只能等磁盘满。 */}
              {k === "db" && d.backups ? (
                <>
                  <div className={s.note}>{backupOccupancyText(d.backups)}</div>
                  {backupWarn(d.backups) ? (
                    <div className={s.noteError}>{backupWarn(d.backups)}</div>
                  ) : null}
                  <div className={s.hint}>{backupLastText(d.backups)}</div>
                  {backupStrayText(d.backups) ? (
                    <div className={s.noteError}>{backupStrayText(d.backups)}</div>
                  ) : null}
                  <div className={s.actions}>
                    <Button size="sm" disabled={busy} onClick={runBackup}>
                      立即备份
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={busy || !canPruneBackups(d.backups)}
                      onClick={() => setPruneOpen(true)}
                    >
                      {canPruneBackups(d.backups)
                        ? `清理旧备份（可回收 ${d.backups.reclaimable_size}）`
                        : "清理旧备份（无可回收）"}
                    </Button>
                    <span className={s.hint}>
                      备份目录 {d.backups.dir}
                    </span>
                  </div>
                </>
              ) : null}

              {editing === k ? (
                <div className={s.actions}>
                  <Input
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    placeholder="输入目录路径，或点「浏览」选择"
                    style={{ minWidth: 320 }}
                  />
                  <Button size="sm" onClick={() => pickDir(k)}>
                    浏览
                  </Button>
                  <Button size="sm" onClick={() => void verify(draft)}>
                    校验
                  </Button>
                  <Button
                    size="sm"
                    variant="primary"
                    disabled={busy || !draft.trim()}
                    onClick={() => save(k)}
                  >
                    {busy ? "保存中…" : "保存"}
                  </Button>
                  <Button size="sm" onClick={() => setEditing(null)}>
                    取消
                  </Button>
                </div>
              ) : null}
            </div>
          );
        })}

        {check ? (
          <div className={check.ok ? s.note : s.noteError}>
            {check.ok
              ? `可用：${check.path}${check.inside_install ? "（在安装目录内，重装可能连带删除）" : ""}`
              : `不可用：${check.reason}`}
          </div>
        ) : null}

        {msg ? <div className={s.note}>{msg}</div> : null}

        <div className={s.hint}>
          配置文件 {paths.config_file}。主库目录写入后需重启客户端；导出目录与冷库目录
          改完立即生效，但冷库里的历史数据不会自动搬迁，请用上方「迁移」。
        </div>
      </div>

      {/* ★ 清理备份是**不可逆**的（直接删文件、不进回收站）⇒ 必须走 ConfirmModal
          让人读一遍条数与后果，禁止 window.confirm，也禁止自写内联二次确认。 */}
      <ConfirmModal
        open={pruneOpen && !!dbBk}
        title="清理旧备份"
        danger
        loading={busy}
        confirmText={dbBk ? `删除 ${Math.max(0, dbBk.count - dbBk.retain_count)} 份旧备份` : "确认"}
        message={
          dbBk ? (
            <>
              将按保留策略删除最旧的 {Math.max(0, dbBk.count - dbBk.retain_count)} 份备份，
              释放约 {dbBk.reclaimable_size}；保留最新 {dbBk.retain_count} 份
              （目录 {dbBk.dir}）。
            </>
          ) : null
        }
        warn="删除不可恢复（不进回收站）。备份是主库损坏时唯一的还原点，请先确认最新几份已覆盖你要保留的状态。"
        onConfirm={doPrune}
        onCancel={() => setPruneOpen(false)}
      />
    </Panel>
  );
}

export default DataPaths;
