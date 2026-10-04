import { useState } from "react";
import { Button, Input } from "@/design/primitives";
import { normalizeCode } from "@/shared/format";
import type { PageProps } from "@/app/routes";
import { MarketTicksPanel } from "./panels/MarketTicksPanel";
import s from "../domain.module.css";

/**
 * 逐笔成交（独立页）。
 *
 * 列表本体在 `panels/MarketTicksPanel`（行情工作台右栏共用同一份实现）——
 * 本页只负责「输入标的 → 查看」这层外壳。
 *
 * ★ 2026-10-03 语义纠正：本页此前叫「成交明细」，数据来自 WS 的 `deal` 事件 ——
 *   那是**本账户成交回报**（`adapter.get_deals` / `on_trade`），不是市场成交流：
 *   看别人的票（或默认的上证指数）永远为空，且标题会被误读成市场成交。
 *   现改接 `GET /market/ticks`（本地 TDX 当日逐笔），**无需券商**即有真实数据。
 *
 * ★ 「全部」按钮已移除：市场逐笔是**按标的**的，不存在「全部标的的成交流」这个
 *   东西 —— 那个按钮是在账户成交流语境下才有意义的交互（订阅自己所有成交）。
 *   保留它只会让用户按下去看到一片空白。
 *
 * 支持 `params.code` 预填（工作台头部「独立成交」按钮带入当前标的）。
 */
const DEFAULT_TICKS_CODE = "000001.SH";

export default function DealFeed({ params }: PageProps) {
  const initial = normalizeCode((params.code as string) || "") || DEFAULT_TICKS_CODE;
  const [code, setCode] = useState(initial);
  const [viewCode, setViewCode] = useState(initial);

  const handleGo = () => {
    const c = normalizeCode(code.trim());
    if (/^\d{6}(\.(SH|SZ|BJ))?$/.test(c)) {
      setViewCode(c);
      setCode(c);
    }
  };

  return (
    <div className={s.page} style={{ padding: 0 }}>
      <div className={s.toolbar} style={{ padding: "8px 12px", borderBottom: "1px solid var(--border)" }}>
        <Input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleGo();
          }}
          mono
          style={{ width: 160 }}
          placeholder="6位 或 600519.SH"
        />
        <Button size="sm" variant="default" onClick={handleGo}>
          查看
        </Button>
        <span className={s.spacer} />
        <span style={{ color: "var(--text-faint)", fontSize: "var(--font-xs)" }}>
          市场逐笔成交（本地 TDX，无需券商）· 每 3 秒刷新
        </span>
      </div>

      <MarketTicksPanel code={viewCode} />
    </div>
  );
}
