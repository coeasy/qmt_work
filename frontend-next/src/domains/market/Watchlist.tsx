import { useState } from "react";
import { Button, ConfirmModal, Input, Panel } from "@/design/primitives";
import { QuoteBoard } from "./QuoteBoard";
import { useWatchlistStore } from "@/stores/watchlist";
import { normalizeCode } from "@/shared/format";
import s from "./watchlist.module.css";

/**
 * 自选股（全页）。
 * 复用报价牌的虚拟滚动表格，顶部提供代码快速添加。
 *
 * ★ 增删对称性（修复前是**残缺**的）：
 *   - 增：顶部输入框批量添加（支持逗号 / 空格 / 分号分隔）；
 *   - 删：表格「移除」列逐条删（两段式确认）+ 顶部「清空」整体删（模态确认）。
 * 修复前这里**只有清空、没有逐条删除**，管理自选股只能全删重加。
 */
export function Watchlist() {
  const [text, setText] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const add = useWatchlistStore((st) => st.add);
  const setAll = useWatchlistStore((st) => st.setAll);
  const codes = useWatchlistStore((st) => st.codes);

  const submit = () => {
    const raw = text.trim();
    if (!raw) return;
    // 支持逗号 / 空格 / 换行批量添加
    const parts = raw.split(/[\s,，;；]+/).filter(Boolean);
    for (const p of parts) add(normalizeCode(p));
    setText("");
  };

  return (
    <div className={s.wrap}>
      <Panel
        title="自选股"
        extra={
          <div className={s.addBar}>
            <Input
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
              placeholder="输入代码，支持批量（逗号分隔）"
              style={{ width: 220 }}
            />
            <Button size="sm" onClick={submit}>
              添加
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setConfirmClear(true)}
              disabled={codes.length === 0}
            >
              清空
            </Button>
          </div>
        }
      >
        <div className={s.tableArea}>
          {/* removable 只在**管理页**开：报价牌同时被行情工作台左栏复用，
              那里点行是「切换标的」，多一个删除按钮就是误删入口。 */}
          <QuoteBoard removable />
        </div>
      </Panel>

      <ConfirmModal
        open={confirmClear}
        title="清空自选股"
        message={
          <>
            将移除全部 <b>{codes.length}</b> 只自选股。此操作不可撤销，需要恢复请重新添加。
          </>
        }
        confirmText="确认清空"
        danger
        onConfirm={() => {
          setAll([]);
          setConfirmClear(false);
        }}
        onCancel={() => setConfirmClear(false)}
      />
    </div>
  );
}

export default Watchlist;
