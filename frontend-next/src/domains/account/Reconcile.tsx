import { useState } from "react";
import { Button, Panel, Spinner } from "@/design/primitives";
import { reconcileApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import type { WalStats } from "@/shared/types";
import s from "../domain.module.css";

function sizeText(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(2)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(2)} KB`;
  return `${n} B`;
}

/** 把任意结果对象渲染为键值列表；嵌套对象降级为 JSON 串，避免 [object Object] */
function renderValue(v: unknown): string {
  if (v === null || v === undefined) return "--";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/**
 * 对账核销与 WAL。
 *
 * ★ 契约要点（reconcile.py）：
 *   - POST /reconcile 立即执行；返回对账结果对象（字段随实现，页面通用渲染）
 *   - GET /reconcile/last 无历史时返回 {checked: 0}
 *   - GET /reconcile/wal/stats 返回 {path,size,snapshot_size,checkpoint_threshold,records,by_entity}
 *   - 端点同时注册了 /wal/* 兼容路径（旧调用方），前端统一走 /reconcile/wal/*
 *
 * 这是交易正确性的关键页：WAL 是「先写日志再下单」的凭证，
 * 对账负责把 WAL 未核销委托与券商当日委托/成交比对后核销。
 */
export function Reconcile() {
  const last = useAsync<Record<string, unknown>>(() => reconcileApi.last(), []);
  const wal = useAsync<WalStats>(() => reconcileApi.walStats(), []);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error" | "warn"; text: string } | null>(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  const runReconcile = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await reconcileApi.run();
      setResult(res);
      setBanner({ tone: "ok", text: "对账已执行完成" });
      await Promise.all([last.reload(), wal.reload()]);
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const doCheckpoint = async () => {
    setBusy(true);
    setBanner(null);
    try {
      await reconcileApi.walCheckpoint();
      setBanner({ tone: "ok", text: "WAL 归档轮转已触发" });
      await wal.reload();
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const walData = wal.data;
  const byEntity = walData?.by_entity ?? {};

  return (
    <div className={s.page}>
      <div className={s.toolbar}>
        <Button size="sm" variant="primary" disabled={busy} onClick={() => void runReconcile()}>
          {busy ? "执行中…" : "立即对账"}
        </Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void doCheckpoint()}>
          手动 WAL 归档
        </Button>
        <span className={s.spacer} />
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            void last.reload();
            void wal.reload();
          }}
        >
          刷新
        </Button>
      </div>

      {banner && (
        <div
          className={`${s.note} ${
            banner.tone === "ok" ? s.noteOk : banner.tone === "warn" ? s.noteWarn : s.noteError
          }`}
        >
          {banner.text}
        </div>
      )}

      <div className={s.split}>
        <div className={s.grow} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <Panel title="WAL 状态">
            {wal.loading && !walData ? (
              <Spinner label="读取中…" />
            ) : wal.error ? (
              <div className={`${s.note} ${s.noteError}`}>{wal.error}</div>
            ) : walData ? (
              <div className={s.kv}>
                <span className={s.kvKey}>文件路径</span>
                <span className={s.kvVal} title={walData.path}>
                  {walData.path}
                </span>
                <span className={s.kvKey}>主文件大小</span>
                <span className={s.kvVal}>{sizeText(walData.size)}</span>
                <span className={s.kvKey}>快照大小</span>
                <span className={s.kvVal}>{sizeText(walData.snapshot_size)}</span>
                <span className={s.kvKey}>记录条数</span>
                <span className={s.kvVal}>{walData.records}</span>
                <span className={s.kvKey}>归档阈值</span>
                <span className={s.kvVal}>{walData.checkpoint_threshold}</span>
              </div>
            ) : null}

            {Object.keys(byEntity).length > 0 && (
              <>
                <div className={s.note} style={{ marginTop: 8 }}>
                  按实体分布（WAL 记录分类）
                </div>
                <div className={s.kv} style={{ marginTop: 6 }}>
                  {Object.entries(byEntity).map(([k, v]) => (
                    <div key={k} style={{ display: "contents" }}>
                      <span className={s.kvKey}>{k}</span>
                      <span className={s.kvVal}>{v}</span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </Panel>

          <Panel title="最近一次对账结果">
            {last.loading && !last.data ? (
              <Spinner label="读取中…" />
            ) : last.error ? (
              <div className={`${s.note} ${s.noteError}`}>{last.error}</div>
            ) : last.data ? (
              <div className={s.kv}>
                {Object.entries(last.data).map(([k, v]) => (
                  <div key={k} style={{ display: "contents" }}>
                    <span className={s.kvKey}>{k}</span>
                    <span className={s.kvVal}>{renderValue(v)}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </Panel>
        </div>

        <Panel title="本次对账明细">
          {!result ? (
            <div className={s.note}>
              尚未执行对账。点击「立即对账」比对 WAL 未核销委托与券商当日委托/成交，
              标记最终状态并写核销记录。
            </div>
          ) : (
            <div className={s.kv}>
              {Object.entries(result).map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}>
                  <span className={s.kvKey}>{k}</span>
                  <span className={s.kvVal}>{renderValue(v)}</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}

export default Reconcile;
