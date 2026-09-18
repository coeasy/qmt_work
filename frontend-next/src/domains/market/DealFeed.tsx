import { useState } from "react";
import { Button, Input } from "@/design/primitives";
import { normalizeCode } from "@/shared/format";
import type { PageProps } from "@/app/routes";
import { DealFeedPanel } from "./panels/DealFeedPanel";
import s from "../domain.module.css";

/**
 * 成交明细（独立页）。
 *
 * 列表本体在 `panels/DealFeedPanel`（行情工作台右栏共用同一份实现）——
 * 本页只负责「输入标的 / 订阅 / 全部」这层外壳。
 *
 * 契约（与旧 frontend/features/market/DealFeed.jsx 一致，未做行为变更）：
 * - WS 复用全局单例 quoteSocket（App 启动时已 connect），不自建系统 WS
 * - 后端成交事件形态：{ type:"deal", data:{ type:"deal_event", data:<realDeal> } }
 *   为兼容历史/未来形变，extractDeal 同时容忍 data.data 与 data 两种形态
 * - 零 mock：离线未连接券商时 WS 状态非 open，页面显式提示「实时通道尚未连接」
 *
 * 支持 `params.code` 预填（工作台「独立成交」按钮带入当前标的）。
 */
export default function DealFeed({ params }: PageProps) {
  const initial = (params.code as string) || "";
  const [code, setCode] = useState(initial);
  const [subscribedCode, setSubscribedCode] = useState(initial ? normalizeCode(initial) : "");

  const handleSubscribe = () => {
    const raw = code.trim();
    const c = normalizeCode(raw);
    if (/^\d{6}(\.(SH|SZ|BJ))?$/.test(c)) {
      setSubscribedCode(c);
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
            if (e.key === "Enter") handleSubscribe();
          }}
          mono
          style={{ width: 160 }}
          placeholder="6位 或 600519.SH"
        />
        <Button size="sm" variant="default" onClick={handleSubscribe}>
          订阅
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            setSubscribedCode("");
            setCode("");
          }}
        >
          全部
        </Button>
      </div>

      <DealFeedPanel code={subscribedCode} />
    </div>
  );
}
