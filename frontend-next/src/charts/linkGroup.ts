/**
 * 跨窗联动组（对标主流 A 股终端的多图联动）。
 *
 * 用法：多个图表加入同一 group 名，十字光标移动时互相广播，
 * 使多周期图（如 1d / 60m / 15m）时间轴对齐、光标同步。
 *
 * 设计取舍：只用 klinecharts 原生 onCrosshairChange + scrollToDataIndex 实现，
 * 不引入额外依赖；广播时排除自身，避免回环。
 */

export interface CrosshairPayload {
  /** 数据索引；-1 表示光标离开 */
  dataIndex: number;
  timestamp?: number;
}

type Listener = (p: CrosshairPayload) => void;

const groups = new Map<string, Set<Listener>>();

/** 加入联动组，返回退出函数 */
export function joinLinkGroup(name: string, fn: Listener): () => void {
  let set = groups.get(name);
  if (!set) {
    set = new Set();
    groups.set(name, set);
  }
  set.add(fn);
  const target = set;
  return () => {
    target.delete(fn);
    if (target.size === 0) groups.delete(name);
  };
}

/** 向组内广播（排除来源自身，防回环） */
export function broadcastLink(name: string, payload: CrosshairPayload, source?: Listener): void {
  const set = groups.get(name);
  if (!set) return;
  for (const fn of set) {
    if (fn !== source) fn(payload);
  }
}

/** 供测试使用：查看组内监听者数量 */
export function linkGroupSize(name: string): number {
  return groups.get(name)?.size ?? 0;
}
