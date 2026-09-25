"""东财对标能力：板块 / 指数 / ETF（P1-1 自 ``eltdx_source.py`` 拆出）。

``eltdx_source.py`` 曾达 55.3KB / 1107 行，把三族关注点挤在一个类里：

1. **连接与限流**（客户端复用、TTL、信号量、节流）；
2. **基础行情**（quote / kline / minutes / instrument_detail）；
3. **板块·指数·ETF**（榜单、成分、指数名、ETF 清单）—— 即本模块。

第 3 族与第 1、2 族没有共享状态：它们只用 ``self._use_client`` 这一个入口去取数，
其余全是自己的缓存与命名判定。拆成 mixin 后 ``EltdxSource`` 的 MRO 多一层基类，
对外行为零变化。

★ 类属性（``_BOARD_TTL`` / ``_STAT_BOARD_GENERIC_PAT`` 等）**刻意留在 ``EltdxSource``**：
它们与方法是 ``self.``/``cls.`` 访问关系，留在原类上语义不变、改动面最小。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from datasource.eltdx_utils import (
    _INDEX_FALLBACK_NAMES,
    _f,
    _to_eltdx,
    _to_qmt,
)
from datasource.periods import to_eltdx_period

#: 与拆分前**同名**的 logger —— 日志行为逐字不变。
log = logging.getLogger("qmt_work.datasource.eltdx")

class EltdxBoardMixin:
    """见模块 docstring。"""

    def _is_stat_name(self, nm: str) -> bool:
        """统计类板块判定：精确模式 + 通用模式（B3 强化）任一命中即 stat。"""
        return (any(k in nm for k in self._STAT_BOARD_PAT)
                or any(k in nm for k in self._STAT_BOARD_GENERIC_PAT))

    @classmethod
    def _board_cache_get(cls, key: str, ttl: float):
        item = cls._BOARDS_CACHE.get(key)
        if item and (time.time() - item[0]) < ttl:
            return item[1]
        return None

    @classmethod
    def _board_cache_set(cls, key: str, val) -> None:
        cls._BOARDS_CACHE[key] = (time.time(), val)

    def _load_index_names(self, cl) -> dict:
        """交易所指数/板块代码 -> 名称（来自 security profile，本地解析无额外网络往返风险）。"""
        names: dict = {}
        for mk in ("sh", "sz", "bj"):
            try:
                for p in cl.codes.all(mk):
                    if getattr(p, "category", None) == "index":
                        names[p.full_code] = p.name
            except Exception as exc:  # noqa: BLE001
                log.debug("eltdx 指数名称表 %s 加载失败：%s", mk, exc)
        return names

    async def _get_index_name(self, code: str) -> Optional[str]:
        """指数 / 板块名称（股票名称表不覆盖指数代码，单独维护）。

        结果按 _BOARD_TTL 缓存；仅在确为指数/板块代码段时才触发网络加载，
        普通个股直接返回 None，避免给每笔 quote 增加无谓往返。
        """
        ec = _to_eltdx(code)
        c6 = ec[2:]
        is_index = c6.startswith(("000", "999")) or ec[:2] == "bj" \
            or c6.startswith(("399", "880", "881", "899"))
        if not is_index:
            return None
        cache_key = "index_names"
        names = self._board_cache_get(cache_key, self._BOARD_TTL)
        if names is None:
            def _run():
                def _inner(cl):
                    return self._load_index_names(cl)
                return self._use_client(_inner)
            try:
                names = await asyncio.to_thread(_run)
            except Exception as exc:  # noqa: BLE001
                log.debug("eltdx 指数名称表加载失败：%s", exc)
                names = {}
            if names:
                self._board_cache_set(cache_key, names)
        if names and names.get(ec):
            return names[ec]
        # 主要指数兜底：部分交易所清单不含常用宽基指数名
        return _INDEX_FALLBACK_NAMES.get(code.upper())

    def _load_board_codes(self, cl) -> tuple[list, dict]:
        """返回 (板块代码列表, 代码->名称)。缓存 1 小时。"""
        cached = self._board_cache_get("boards", self._BOARD_TTL)
        if cached:
            return cached
        codes = []
        for c in cl.codes.all_indices():
            seg = c[2:5]
            if seg in (self._BOARD_SEG_INDUSTRY, self._BOARD_SEG_CONCEPT):
                codes.append(c)
        names = self._load_index_names(cl)
        out = (codes, names)
        self._board_cache_set("boards", out)
        return out

    async def get_boards(self, kind: str = "industry", sort_by: str = "pct",
                         limit: int = 50) -> list:
        """板块指数榜单（真实板块指数快照，非自聚合）。

        kind: industry(881xxx) / concept(880xxx 剔除统计类) / stat(仅统计类)
        sort_by: pct(涨跌幅) / amount(成交额)
        返回元素含 code/name/last/change_pct/amount/kind/source，按 sort_by 降序。
        """
        def _run():
            def _inner(cl):
                codes, names = self._load_board_codes(cl)
                seg = (self._BOARD_SEG_INDUSTRY if kind == "industry"
                       else self._BOARD_SEG_CONCEPT)
                target = [c for c in codes if c[2:5] == seg]
                rows = []
                for i in range(0, len(target), 80):
                    batch = target[i:i + 80]
                    try:
                        snaps = cl.quotes.get_snapshots(batch)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("eltdx 板块快照批次失败：%s", exc)
                        continue
                    for s in (snaps if isinstance(snaps, list) else []):
                        last = getattr(s, "last_price", None) or 0
                        if not last:
                            continue
                        nm = names.get(s.full_code, "")
                        is_stat = self._is_stat_name(nm)
                        if kind == "concept" and is_stat:
                            continue
                        if kind == "stat" and not is_stat:
                            continue
                        rows.append({
                            "code": _to_qmt(s.full_code),
                            "name": nm,
                            "last": last,
                            "lastClose": getattr(s, "pre_close_price", None),
                            "change_pct": (round(s.change_pct, 2)
                                           if getattr(s, "change_pct", None) is not None else None),
                            "amount": getattr(s, "amount", None),
                            "volume": getattr(s, "total_hand", None),
                            "kind": "stat" if is_stat else kind,
                            # 口径标注（P1-5）：统计类板块的 last 是「家数」而非指数点位，
                            # 前端据此显示「家」而不是按涨跌幅染色误读为「北证涨了 27%」。
                            "metric": "count" if is_stat else "point",
                            "unit": "家" if is_stat else "",
                        })
                rows.sort(key=lambda r: (r.get("amount") if sort_by == "amount"
                                         else (r.get("change_pct") if r.get("change_pct") is not None
                                               else -999)) or 0, reverse=True)
                return rows[:limit]
            return self._use_client(_inner)
        return await asyncio.to_thread(_run)

    async def search_boards(self, name: str, limit: int = 8) -> list:
        """板块名称 → 代码精确匹配（P1-8 深链稳化）。

        个股页只拿到行业/概念「名称」，若前端靠 name.includes 模糊匹配板块榜，
        同名/近名板块（如「半导体」vs「半导体概念」）会命中错项，榜单未加载时
        还会静默失败（点击无反应）。此处在服务端做精确 + 前缀 + 包含三级匹配，
        返回 [{code,name,kind,metric}]，前端据 code 精确选中。
        """
        name = (name or "").strip()
        if not name:
            return []

        def _run():
            def _inner(cl):
                codes, names = self._load_board_codes(cl)
                seg2kind = {self._BOARD_SEG_INDUSTRY: "industry",
                            self._BOARD_SEG_CONCEPT: "concept"}
                scored = []
                for c in codes:
                    nm = names.get(c, "")
                    if not nm:
                        continue
                    if nm == name:
                        score = 0                      # 完全一致
                    elif nm.startswith(name) or name.startswith(nm):
                        score = 1                      # 前缀/包含
                    elif name in nm:
                        score = 2                      # 名称包含
                    else:
                        continue
                    is_stat = self._is_stat_name(nm)
                    scored.append((score, {
                        "code": _to_qmt(c),
                        "name": nm,
                        "kind": "stat" if is_stat else seg2kind.get(c[2:5], "concept"),
                        "metric": "count" if is_stat else "point",
                        "unit": "家" if is_stat else "",
                    }))
                scored.sort(key=lambda x: (x[0], -len(x[1]["name"])))
                return [r for _, r in scored[:limit]]
            return self._use_client(_inner)
        res = await asyncio.to_thread(_run)
        return res or []

    async def get_board_constituents(self, code: str, limit: int = 50, page: int = 0) -> dict:
        """板块成分股（f10.theme_market req_id=200744）。

        实测列：N001=市场(0深/1沪/2北) N002=代码 N003=名称 N004=涨跌幅 N005=价格；
        成分总数在 table[0] 的 total_num 字段。
        page/limit 直接透传 f10 分页（page_size=limit），大板块（电子 548 只）
        可翻页取全量，不再只显示前 50 只（P0-3）。
        """
        ec = _to_eltdx(code)
        page = max(0, int(page or 0))

        def _run():
            def _inner(cl):
                resp = cl.f10.theme_market(ec, req_id="200744", page=page, page_size=limit)
                tables = list(getattr(resp, "tables", ()) or ())
                total = None
                for cell in (tables[0].rows if tables else ()):
                    if isinstance(cell, dict) and "total_num" in cell:
                        try:
                            total = int(cell["total_num"])
                        except (TypeError, ValueError):
                            total = None
                mkt = {"0": "SZ", "1": "SH", "2": "BJ"}
                out = []
                if len(tables) > 1:
                    for row in tables[1].rows:
                        if not isinstance(row, dict):
                            continue
                        c6 = str(row.get("N002") or "")
                        if not c6:
                            continue
                        out.append({
                            "code": f"{c6}.{mkt.get(str(row.get('N001') or ''), 'SH')}",
                            "name": row.get("N003") or "",
                            "change_pct": _f(row.get("N004")),
                            "last": _f(row.get("N005")),
                        })
                has_more = bool(total is not None and (page + 1) * limit < total)
                return {"code": code, "total": total, "items": out[:limit],
                        "page": page, "page_size": limit, "has_more": has_more,
                        "page_count": (int((total + limit - 1) / limit)
                                       if total else (1 if out else 0))}
            return self._use_client(_inner)
        return await asyncio.to_thread(_run)

    async def get_board_kline(self, code: str, period: str = "1d", count: int = 60) -> list:
        """板块 / 指数 K 线（必须 kind="index"，否则 ProtocolError）。"""
        ec = _to_eltdx(code)
        p = to_eltdx_period(period)

        def _run():
            def _inner(cl):
                bars = cl.bars.get(ec, period=p, count=count, kind="index")
                bl = getattr(bars, "bars", bars) or []
                out = []
                for b in bl:
                    t = getattr(b, "time", None)
                    ts = t.strftime("%Y%m%d") if hasattr(t, "strftime") else str(t)
                    raw_vol = getattr(b, "volume_lots", None)
                    out.append({
                        "time": ts,
                        "open": getattr(b, "open", None),
                        "high": getattr(b, "high", None),
                        "low": getattr(b, "low", None),
                        "close": getattr(b, "close", None),
                        # T6 单位归一：手 → 股（契约单一真源，见 Bar.volume_unit）
                        "volume": (raw_vol * 100) if raw_vol is not None else None,
                        "volume_unit": "shares",
                    })
                return out
            return self._use_client(_inner)
        return await asyncio.to_thread(_run)

    async def get_etf_list(self, limit: int = 0) -> list:
        """ETF 清单（代码段 51/56/58/15/16 过滤 + 名称）。

        ⚠️ P0-2 截断缺陷：清单按 code 升序排列，若直接取前 N 条（旧行为
        `cached[:limit]`），会把 56 段（沪行业/主题）、58 段（沪跨境/商品）整段
        切掉 —— 前端这两个分组 tab 点开永远是空表。因此默认返回全量，
        仅当调用方显式给出小于全量的 limit 时才截断。
        """
        cached = self._board_cache_get("etfs", self._ETF_TTL)
        if cached:
            # 并入名称表（幂等）：旧缓存升级后首次访问即让 ETF 全局可搜
            self._merge_into_name_map(cached)
            return self._cap_etfs(cached, limit)

        def _run():
            def _inner(cl):
                out = []
                # codes.all_etfs() 只返回代码字符串；codes.etfs(mk) 才带 .name，
                # 否则 ETF 清单会全行空名称（界面上不可辨识）。
                for mk in ("sh", "sz", "bj"):
                    try:
                        items = cl.codes.etfs(mk)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("eltdx ETF 清单 %s 加载失败：%s", mk, exc)
                        continue
                    for it in items:
                        fc = getattr(it, "full_code", "") or ""
                        c6 = fc[2:]
                        if c6[:2] not in ("51", "56", "58", "15", "16"):
                            continue
                        out.append({"code": _to_qmt(fc), "name": getattr(it, "name", "") or ""})
                return out
            return self._use_client(_inner)
        etfs = await asyncio.to_thread(_run)
        # 名称兜底：个别市场清单缺失时，用已加载的通用名称表补全
        if any(not e["name"] for e in etfs):
            await self._ensure_name_map()
            nm = self.__class__._name_map
            for it in etfs:
                if not it["name"]:
                    it["name"] = nm.get(it["code"], "")
        etfs.sort(key=lambda e: e["code"])
        self._board_cache_set("etfs", etfs)
        # 并入名称表（幂等）并持久化：此后 ETF 搜索/详情/解析全链路离线可用
        self._merge_into_name_map(etfs)
        return self._cap_etfs(etfs, limit)

    @staticmethod
    def _cap_etfs(items: list, limit: int) -> list:
        """limit<=0 或 ≥ 全量时返回全量，否则截断（避免整段丢失，见 get_etf_list 说明）。"""
        try:
            n = int(limit or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0 or n >= len(items):
            return items
        return items[:n]
