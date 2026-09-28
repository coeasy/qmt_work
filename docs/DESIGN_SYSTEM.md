# qmt_work 前端设计系统（DESIGN_SYSTEM.md）

> **适用范围：仅 `frontend-next/`。**
>
> ⚠️ 历史提示：旧 `frontend/` 已退役。本文曾描述 `frontend/src/styles.css` 的令牌
> 体系与 `lib/*.js` 工具层 —— 那套路径与令牌值**已全部不存在**，2026-09-29 按
> 真实代码重写。若在别处看到 `frontend/…`（无 `-next`）的路径描述，一律视为过时。
>
> 样式真源（三层，缺一不可）：
>
> | 层 | 文件 | 职责 |
> |---|---|---|
> | 令牌 | `frontend-next/src/design/tokens.css` | 颜色/尺寸语义变量（主题切换只改这里） |
> | 基础 | `frontend-next/src/design/base.css` | 全局重置、排版、历史类名别名兼容 |
> | 皮肤 | `frontend-next/src/design/skins.css` + `skins.ts` | 主题（dark/light）与涨跌色方案的可选项 |
>
> 组件样式一律用 CSS Module（`*.module.css`），与组件同目录。

## 1. 设计令牌（`:root` 语义层）

命名统一为 **`--bg-N`**（由深到浅），不要写成 `--bg3`。

| 令牌 | 深色值 | 语义 |
|---|---|---|
| `--bg-0` | `#0d1117` | 页面底色（最深） |
| `--bg-1` | `#131a24` | 侧栏 / 抽屉 |
| `--bg-2` | `#1a2230` | 面板 / 卡片底 |
| `--bg-3` | `#212b3b` | 悬浮 / 激活块 |
| `--bg-hover` / `--bg-active` | `#263244` / `#2d3b50` | 悬停 / 按下 |
| `--bg-overlay` | `rgba(6,10,16,.72)` | 遮罩层 |
| `--border` / `--border-strong` | `#2a3648` / `#3a4a61` | 边框 / 强调分隔 |
| `--text` / `--text-dim` / `--text-faint` | `#e6edf6` / `#9aa8bd` / `#6b7a91` | 三级文本 |
| `--accent` / `--accent-hover` / `--accent-dim` | `#3b82f6` / `#60a5fa` / `rgba(59,130,246,.16)` | 主强调（链接/选中/代码） |
| `--success` / `--warning` / `--danger` / `--info` | `#22c55e` / `#f59e0b` / `#ef4444` / `#38bdf8` | 语义色（各带 `-dim` 弱化版） |
| `--up` / `--down` / `--flat` | `#ef4444` / `#22c55e` / `#9aa8bd` | **涨/跌/平**（默认 A 股口径，见 §2） |
| `--chart-bg` `--chart-grid` `--chart-axis` `--chart-crosshair` | — | 图表底/网格/轴/十字线 |
| `--chart-ma5` `--chart-ma10` `--chart-ma20` `--chart-ma60` | `#f59e0b` `#38bdf8` `#a78bfa` `#22c55e` | 均线配色 |

浅色主题在 `[data-theme="light"]` 下覆盖同名令牌；**组件不得出现硬编码色值**
（由 `check_frontend_classnames.py` 与令牌门禁共同校验）。

## 2. 涨跌色约定（A 股口径）

- 涨/升 → `var(--up)`（**红**）；跌/降 → `var(--down)`（**绿**）。
- 无涨跌/缺失一律显示 `—`，**绝不估算、绝不用 0 冒充**。
  （`AssetSummary.tsx` / `FundamentalsPanel.tsx` 有统一占位实现，勿各自造轮子。）
- ⚠️ 这是中国股市惯例，与欧美相反；**禁止**在金融场景使用绿涨红跌。
- 涨跌色可**独立于主题**切换（`design/skins.ts`），A 股与境外市场两套口径并存。

## 3. 金额/数量格式化

- 单点实现：`frontend-next/src/shared/format.ts`（`fmtMoney` / `fmtPrice` / `fmtPct` /
  `toneColor` 等）。**禁止**在组件内自行 `toFixed` —— 历史 Bug：万档 1 位 vs 2 位
  不一致（G11-1 已收敛）。
- 成交量单位契约：后端统一「**股**」（部分源返回「手」，由**后端**归一化）。
  前端**不再换算**，也不得假设「手」。

## 4. 组件与页面规范

- **可复用 UI**：`frontend-next/src/design/primitives/`（`Panel` / `Button` / `Badge` /
  `Modal` / `Tabs` / `DataTable` / `Tooltip` / `Input` / `ConfirmButton` /
  `TradingDateBadge`）。新页面**优先复用**；确需新增才写自定义类，并同步追加到
  对应 `*.module.css`。
- **样式隔离**：组件样式走 CSS Module，**不写全局类**；全局只有 `design/*.css`。
- **指标/图表**：
  - 前端指标实现单点：`frontend-next/src/shared/indicators.ts`；图表在
    `frontend-next/src/charts/KLineChart.tsx`。
  - 后端另有 **API/MCP-only** 的指标与图表契约端点
    （`/market/indicators`、`/market/indicators/calc`、`/market/chart-spec`），
    供脚本 / Agent / MCP 调用，**当前前端不消费**。若要接回，须先评估与前端
    本地实现的口径一致性，避免「双真源」。
- **数据获取**：行情走 `frontend-next/src/hooks/`
  （`useLiveQuotes` / `useQuoteSubscription` / `useAsync` / `useOpenWorkbench`）
  ＋ WebSocket（`services/ws.ts`），**零轮询**；
  后端不可用显示「—」，**绝不**显示占位假数据。

## 5. 防回归门禁

- `backend/scripts/check_frontend_classnames.py`（G11-6）：**已随旧前端退役而失效** ——
  它扫的是 `frontend/src/styles.css` 与全局类名差集，而 `frontend-next` 改用
  CSS Modules（类名作用域由构建器保证）。该脚本现直接打印 `[SKIP]` 并 exit 0，
  **不再提供任何保护**；如需等价的「样式类名缺失」防护，应针对
  `*.module.css` 重新实现（当前未做）。
- 构建：`cd frontend-next && npm run build`（产物 → `backend/static`，已 gitignore）。
- 类型检查：`cd frontend-next && npm run typecheck`（`tsc --noEmit`，CI 必跑）。
- 单测：`cd frontend-next && npm run test:serial`
  （**必须 serial**：并行 worker 在受限临时目录下 `EPERM` 会静默丢用例，见 TD-24）。
- 契约：`python scripts/ci_reconcile.py`（组件数 / 页数 / API 契约 / README 计数 / 行尾）。
