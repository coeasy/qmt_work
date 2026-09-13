import { useState } from "react";
import { Button, Input, Panel } from "@/design/primitives";
import { QuoteBoard } from "./QuoteBoard";
import { useWatchlistStore } from "@/stores/watchlist";
import { normalizeCode } from "@/shared/format";
import s from "./watchlist.module.css";

/**
 * 自选股（全页）。
 * 复用报价牌的虚拟滚动表格，顶部提供代码快速添加。
 */
export function Watchlist() {
  const [text, setText] = useState("");
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
              onClick={() => {
                if (window.confirm("确认清空自选股？")) setAll([]);
              }}
              disabled={codes.length === 0}
            >
              清空
            </Button>
          </div>
        }
      >
        <div className={s.tableArea}>
          <QuoteBoard />
        </div>
      </Panel>
    </div>
  );
}

export default Watchlist;
