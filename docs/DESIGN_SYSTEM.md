# qmt_work 前端设计系统（DESIGN_SYSTEM.md）

> G11-2 交付物。把 `styles.css` 的语义令牌与跨页约定固化为规范，新页面/新组件
> 一律继承，避免「每页一套样式」的漂移。唯一样式表：`frontend/src/styles.css`。

## 1. 设计令牌（:root 语义层）

| 令牌 | 值 | 语义 |
|---|---|---|
| `--bg` | `#0f1420` | 页面底色（深色金融终端） |
| `--bg-2` | `#161c2c` | 次级底（侧栏/抽屉） |
| `--panel` | `#1b2333` | 面板/卡片底 |
| `--panel-2` | `#222c40` | 悬浮/激活面板底 |
| `--border` | `#2c3850` | 边框/分隔线 |
| `--text` | `#e6ecf5` | 主文本 |
| `--text-dim` | `#8a97ad` | 次级文本 |
| `--accent` | `#4f8cff` | 主强调（链接/选中/代码） |
| `--accent-2` | `#2bd4a4` | 次强调（成功/健康） |
| `--danger` | `#ff5c6c` | 危险/错误 |
| `--warn` | `#ffb020` | 警告 |
| `--up` | `#ff4d4f` | **涨 红**（A 股约定，勿改） |
| `--down` | `#19c37d` | **跌 绿**（A 股约定，勿改） |

## 2. 涨跌色约定（A 股口径）

- 涨/升 → `var(--up)`（红）；跌/降 → `var(--down)`（绿）。
- 表格涨跌幅单元格用 `.up` / `.down` 类（已定义）；无涨跌/缺失显示 `—`，**绝不估算**。
- ⚠️ 这是中国股市惯例，与欧美相反；**禁止**在金融场景使用绿涨红跌。

## 3. 金额/数量格式化

- 单点实现：`frontend/src/lib/format.js`（`fmtAmount` 等）。**禁止**在组件内
  自行 `toFixed`（历史 Bug：万档 1 位 vs 2 位不一致，G11-1 已收敛）。
- 成交量单位契约：后端统一「股」（部分源为「手」，由后端归一化），前端不再换算。

## 4. 组件与页面规范

- **组件**：面板 `.panel`、按钮 `.ib-btn`（行内）/`.btn-primary`（主）/`.btn-sm`/
  `.btn-danger-sm`、表单行 `.form-row`、输入 `.mp-code-input`、表格 `.table`、
  工具栏 `.bd-toolbar`、复选 `.checkbox`。新页面**优先复用**，确需新增才加
  `sc-`/`mp-` 等前缀类，并同步追加到 `styles.css`。
- **指标/图表**：指标计算**只存在于后端**（`app/indicators`，G2），前端经
  `lib/indicators.js` 消费 `/market/indicators/calc`；**禁止**在组件内实现指标
  算法（防双份实现回归，R2）。
- **数据获取**：行情类优先 `hooks/useMarket.js` / `lib/quoteHub.jsx`；零轮询
  （`useActiveInterval` 非活跃 Pane 暂停）；后端不可用显示「—」而非占位假数据。

## 5. 防回归门禁

- `backend/scripts/check_frontend_classnames.py`（G11-6）：组件 className 与
  `styles.css` 差集，**新增缺失类阻断 CI**；历史遗留走基线豁免。改样式/新页面
  后必跑：`python ../backend/scripts/check_frontend_classnames.py`（repo 根）。
- 构建：`cd frontend && npm run build`（产物 `backend/static`，已 gitignore）。
- 冒烟：`frontend/tests/render_smoke_market.mjs`（Playwright 逐页派发 nav，
  捕获 console/pageerror）。
