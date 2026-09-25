"""批量盘口快照（``DataSourceManager.get_quotes``）—— 以 mixin 形式拆出（P1-1）。

★ 为什么用 mixin 而不是抽成模块级函数：本方法深度依赖 ``self._plugins`` /
``self._source_allowed`` / ``self._call_source`` / ``self._broker`` 等实例状态。
改成自由函数就要把这 4 个依赖全部当参数传进去 —— 那是**接口改造**，不是拆文件。
mixin 让 ``self`` 语义逐字不变，``DataSourceManager`` 的 MRO 里多一层基类，
对外行为零变化。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from datasource.board import classify_board
from datasource.instrument import with_exchange_suffix

class QuotesMixin:
    """见模块 docstring。"""

    # ---------- 行情快照（批量） ----------
    async def get_quotes(self, codes, source: str = "auto",
                         conn_id: Optional[str] = None) -> dict:
        """批量盘口快照 —— ``{code: dict | None}``。

        ★ 为什么必须有这个方法（2026-09-20 实测）：``/market/quotes`` 的自选股 /
        报价牌冷启动要一次拉 4~N 只。此前它逐只调本类的 ``get_quote``，而公开源
        有 **全局 0.3s 节流锁**（``_PublicSource._MIN_INTERVAL``，跨请求共享），
        N 只 = N 次 HTTP + N 次锁等待；同步任务在前面占着锁时，冷启动 4 只
        **一只都拿不到**（items 恒空）。批量接口让 N 只收敛成 1 次 HTTP。

        与 ``get_quote`` 的语义差异（重要，勿混淆）：
        - ``get_quote`` 的 auto 是「逐个源尝试，第一个成功的整体返回」；
          批量版是「**同一源内**批量拉取，未拿到的 code **带着缺口继续走下一个源**」，
          即降级粒度是 **code 级** 而不是整批级 —— 一批里 3 只成功 1 只失败时，
          失败的 1 只仍有机会被下一个源补上，成功的 3 只不会被重来一遍。
        - 券商没有批量接口（QMT 盘口是逐只订阅的），故 broker 分支仍是并发单只，
          与 ``get_quote(broker)`` 语义完全一致。

        ⚠️ 返回值**总包含全部入参 code**（拿不到的显式置 None），调用方据此决定
        是否走兜底；绝不静默丢键。
        """
        source = self._validate_source(source)
        # 规范化 + 去重（保序）：券商只认 600519.SH，名称表也以带后缀代码为键
        ordered: list = []
        seen: set = set()
        for raw_code in codes or []:
            full = with_exchange_suffix(raw_code)
            if full and full not in seen:
                seen.add(full)
                ordered.append(full)
        if not ordered:
            return {}
        boards = {c: classify_board(c) for c in ordered}
        out: dict = {c: None for c in ordered}

        async def _plugin_batch(name: str, todo: list) -> dict:
            src = self._plugins.get(name)
            if src is None:
                return {c: None for c in todo}
            raws = await self._call_source(name, src.get_quotes(todo))
            if not isinstance(raws, dict):
                return {c: None for c in todo}
            hit = [c for c in todo if raws.get(c)]
            dets: dict = {}
            # ① 画像**先从已取到的快照派生**（公开源零额外 HTTP —— 见
            #    ``_PublicSource.derive_detail``）；不能派生的才回源批量取。
            #    这样一次批量 = 1 次 HTTP；否则画像会再打一次，收益砍半。
            todo_det: list = []
            for c in hit:
                try:
                    d = src.derive_detail(raws[c])
                except Exception:  # noqa: BLE001  派生是增强项，失败不该拖垮行情
                    d = None
                if d:
                    dets[c] = d
                else:
                    todo_det.append(c)
            if todo_det:
                got = await self._call_source(name, src.get_details(todo_det))
                if isinstance(got, dict):
                    dets.update({c: v for c, v in got.items() if v})
            res: dict = {}
            for c in todo:
                raw = raws.get(c)
                if not raw:
                    res[c] = None
                    continue
                det = dets.get(c) or {}
                # 与 _from_plugin 同一条规则：详情名缺失或等于代码时回退源层名称，
                # 否则指数会被详情层的「名称 == 代码」覆盖成 000001.SH。
                det_name = str(det.get("name") or "").strip()
                if not det_name or det_name.upper() == c.upper():
                    if raw.get("name"):
                        det = {**det, "name": raw["name"]}
                res[c] = self._merge_quote(
                    raw, c, boards[c], det, name,
                    industry=det.get("industry") or "",
                    concepts=det.get("concepts") or [])
            return res

        async def _broker_batch(todo: list) -> dict:
            # ⚠️ 这里是「一批标的」的**假并发**：`asyncio.gather` 只并发了等待，
            #    每次 `self.get_quote(...)` 都会各发一条 `get_quote` RPC
            #    （`bridge_client.py::BridgeAdapter.get_quote` → `_rpc("get_quote", [code])`）
            #    ⇒ **N 只标的 = N 次跨进程 RPC**。取 8 个指数就是 8 次。
            #
            # ★ 桥接侧其实**已有批量接口**：`BridgeAdapter.get_full_tick(codes)`
            #   （`_rpc("get_full_tick", [list(codes)])`）一次就能拿全。
            #
            # ⚠️ 但**不能**把这里直接换成批量 RPC：`self.get_quote()` 除取价之外还做了
            #    名称兜底 / 板块分类 / 详情合并（见上面 `_merge_quote` 那段），
            #    直接批量取原始 tick 会**静默丢掉这些字段**（指数名会退化成代码）。
            #    正确做法是「批量只用于取原始 tick，逐只归一化照旧」。
            #
            # ⚠️ 归属**尚未定论**，别当结论用：隔离实例实测 `source=broker` 冷 8.140s、
            #    `source=tencent` 冷 0.688s（同一后端进程，8 只指数）。8 次 RPC 是不是
            #    那 8 秒的**主因**，需要真券商环境再测一次才能确认。
            results = await asyncio.gather(
                *[self.get_quote(c, "broker", conn_id) for c in todo],
                return_exceptions=True)
            return {c: (None if isinstance(r, Exception) else r)
                    for c, r in zip(todo, results)}

        if source == "broker":
            return await _broker_batch(ordered)
        if source in self._plugins:
            return await _plugin_batch(source, ordered)
        # auto：code 级降级 —— 缺口交给链上的下一个源
        for name in self._resolve_sources(source, "quote"):
            todo = [c for c in ordered if out.get(c) is None]
            if not todo:
                break
            part = await (_broker_batch(todo) if name == "broker"
                          else _plugin_batch(name, todo))
            for c, q in (part or {}).items():
                if q is not None:
                    out[c] = q
        return out
