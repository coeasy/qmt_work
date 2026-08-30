# G8 自然语言选股使用指南（NL → 条件树）

> 交付物：`backend/app/agent/nl_screen.py` + `POST /market/screen/nl` +
> 前端 Screen.jsx 顶部 NL 输入条（T5 接入）。
> 用自然语言生成**可编辑**的条件树（G8-1 不黑箱）：结果回显到条件构建器，
> 用户确认/微调后再执行。

## 支持语义（规则式离线，零外部 LLM 依赖）

| 描述示例 | 规则 | 产出的条件树 |
|---|---|---|
| 放量上涨 | 放量（V > V-MA(20)）+ N 日上涨（ROC(n)>0） | and 树 |
| 近5日下跌 / 近20日上涨 | N 日涨跌（ROC(n)） | indicator roc |
| RSI(14) 超卖 / 超买 | RSI < 30 / > 70 | indicator rsi |
| 5日均线金叉 / 死叉 | MA5 vs MA10 双序列 | compare 叶子 |
| 站上20日均线 / 跌破60日均线 | C vs MA(n) | compare 叶子 |

## 诚实提示（不伪造）

市值 / 国资 / 社保 / 估值 / 资金流等**基本面语义** → `unsupported` 数组返回，
**绝不静默忽略或编造条件**。前端提示「暂不支持：市值、国资（可改用条件构建器）」。

## 接口

```bash
curl -X POST http://127.0.0.1:21118/api/v1/market/screen/nl \
  -H "Content-Type: application/json" -d '{"text":"放量上涨"}'
# → {"text":"放量上涨",
#     "conditions":{"and":[{"compare":{...V>VMA...}},{"indicator":{...roc>0...}}]},
#     "rules":["放量（量 > 1× 量均线）","N 日上涨（ROC(n) > 0）"],
#     "unsupported":[]}
```

`conditions` 与 `/market/screen` 的 conditions 参数**完全兼容**：
前端拿到后可直接回显/执行。

## 前端接入（已落地）

Screen.jsx 顶部「自然语言选股」输入条：输入描述 → `api.screenNL({text})` →
`rowFromLeaf` 把条件树叶子映射为构建器行（可编辑）→ 运行选股。
规则表位于 `nl_screen.py` 顶部 `RULES`，可扩展（新增正则 + 条件树构建函数）。
