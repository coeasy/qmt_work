# docs/archive/ · 归档说明

本目录存放**已完成使命、但仍被活代码/活文档引用为「设计理由」**的历史方案。

## 为什么不是删掉

这些文档的价值不在「要做什么」，而在**「为什么这么定」**：

- `UNIFIED_TRADING_ABSTRACTION.md` —— 六方案加权对比（选型论证）。
  仍被 `backend/connectors/ports.py`、`backend/connectors/canonicalize.py`、
  `backend/tests/test_connector_canonicalize.py` 的注释按 §5.2 引用。
- `UNIVERSAL_BROKER_PLATFORM_FINAL_PLAN_V4.md` —— 统一券商平台纲领。
  仍被 `backend/agent_bigqmt/DEPLOY.md` 按 §9 / §6.2 引用。
- `BIG_QMT_COMPAT_PLAN.md` —— 大 QMT 兼容方案（含路径 B 的可行性论证）。
  仍被 `backend/agent_bigqmt/DEPLOY.md`、`docs/QMT_大小版本使用说明.md`、
  `frontend-next/src/services/api/broker.ts` 引用。
- `BIG_QMT_IMPLEMENTATION_PLAN.md` —— 上者的实施细化（M0~M4）。

**删掉它们 = 把活引用变成断链**，读者会以为文件丢了。所以归档而非删除。

判据（与「该删」的区别）：**论证/决策类文档保留，一次性执行计划删除。**
2026-10-05 已按此判据删除了 `PROJECT_OPTIMIZATION_PLAN_2026-10-03.md`
（基线 0.4.3 的清理任务清单，条目全部闭环、全仓零活引用）。

## ⚠️ 目录内路径用归档前布局

本目录文档写于归档之前，内部引用的是**当时的路径**，例如：

```text
docs/BIG_QMT_COMPAT_PLAN.md      ← 当时在 docs/ 根，现在在 docs/archive/
docs/UNIFIED_TRADING_ABSTRACTION.md
docs/BIG_QMT_DEPLOY.md           ← 该文件已彻底删除
```

**这不是断链**，是当时布局的真实记录。归档件按「冻结快照」对待，不回改；
跨目录找文件请以 [`../README.md`](../README.md) 索引为准。

同理，`docs/release-notes/` 也是冻结历史源，其中的路径同样按当时布局书写。

## 门禁

`python scripts/audit_doc_links.py` 判定仓库内路径引用是否可解析。
`docs/archive/` 与 `docs/release-notes/` 作为冻结历史源不参与判定；
其余产品面（docs / backend / frontend-next / scripts / .github）断链即非零退出。
