"""行业 / 题材（F10）能力（P1-1 自 ``eltdx_source.py`` 拆出，2026-10-05 二次拆分）。

``eltdx_source.py`` 第二次越过 Gate 4 的 50KB 上限
（``scripts/check_execution_architecture.py``）。它当时承载四族关注点：

1. **连接与限流**（客户端复用、TTL、信号量、节流）；
2. **名称表 / 搜索索引**；
3. **板块·指数·ETF** —— 已先一步拆到 ``eltdx_boards.py``；
4. **行业 / 题材**（F10 网关 + 本地缓存 + 假空重试窗口）—— 即本模块。

第 4 族与其余各族的唯一耦合是 ``self._use_client`` 这一个取数入口；
它自己的状态（``_industry_map`` / ``_industry_ts`` / ``_industry_loaded`` /
``_industry_lock``）**刻意留在 ``EltdxSource``**：它们与方法是
``self.__class__.`` 的访问关系，留在原类上语义不变、改动面最小
（与 ``eltdx_boards.py`` 的 ``_BOARD_TTL`` 处理一致）。

★ 为什么是「拆文件」而不是「把上限调高」：Gate 4 的意义正是阻止再膨胀；
抬高阈值只会让下一次膨胀更晚被发现 —— 那是「改判据而不改代码」的假绿灯。

拆成 mixin 后 ``EltdxSource`` 的 MRO 多一层基类，对外行为零变化。

★ ``_INDUSTRY_RETRY_TTL`` 随之迁到本模块（它只被本模块的两条方法使用）。
``tests/test_tdx_data_completeness.py`` 的导入已同步指向本模块 ——
定义处与使用处同址，避免「常量留在 A、消费者在 B」的第二真源。
"""
from __future__ import annotations

import asyncio
import logging
import time

from core.clock import now_iso
from datasource.eltdx_utils import (
    _industry_cache_path,
    _load_json_cache,
    _save_json_cache,
    _to_eltdx,
)

#: 与拆分前**同名**的 logger —— 日志行为逐字不变。
log = logging.getLogger("qmt_work.datasource.eltdx")

#: 行业/题材为空时的重试窗口（秒）：空值可能是网络抖动或协议缺口导致的**假空**，
#: 不能像昨收那样按日缓存——否则一次失败会永久掩盖真实数据（R1 实测：
#: 修复深市市场判定后，300750/688981 的行业仍显示 --，因旧的空值已被落盘）。
_INDUSTRY_RETRY_TTL = 6 * 3600.0


class EltdxIndustryMixin:
    """见模块 docstring。"""

    async def _ensure_industry_loaded(self):
        """加载行业概念表到类级 _industry_map（所有实例共享）。"""
        if self.__class__._industry_loaded:
            return
        async with self.__class__._get_aio_lock():
            if self.__class__._industry_loaded:
                return
            cached = await asyncio.to_thread(_load_json_cache, _industry_cache_path())
            if cached:
                self.__class__._industry_map.update(cached)
            self.__class__._industry_loaded = True
            if cached:
                log.info("eltdx 行业概念表已从本地缓存加载：%d 只", len(cached))

    async def _get_industry(self, code: str) -> dict:
        """返回 {industry, concepts}；优先本地缓存，缺失则经 F10 网关拉取并落盘。

        并发安全：网络拉取与本地落盘均经 _industry_lock 串行；且仅在确有
        新记录时才重写整份缓存文件，避免高频请求下的写放大与丢更新。
        """
        await self._ensure_industry_loaded()
        cls_imap = self.__class__._industry_map
        hit = cls_imap.get(code)   # 读不加锁：dict 取键在 CPython 下原子，避免占死事件循环
        if hit is not None:
            # 空值不永久缓存：网络抖动或接口缺口产生的假空必须可被重试。
            # 非空结果照常命中（行业分类极少变动，无需 TTL）。
            _empty = not (hit.get("industry") or hit.get("concepts"))
            if not _empty or (time.time() - self.__class__._industry_ts.get(code, 0)) < _INDUSTRY_RETRY_TTL:
                return hit
        # 2026-10-04 R1 根因修复：此前传裸 6 位代码（_num），门面对无前缀代码
        # 一律加 'sh' → 所有深市 000xxx/300xxx 个股被当成沪市查询，行业与题材
        # **恒空**（平安银行 stock-info 面板显示"行业 --"）。现在传完整 TDX
        # 形态（_to_eltdx 保留交易所），沪深均可正确归属。
        ec = _to_eltdx(code)
        industry, concepts = "", []
        try:
            def _run():
                def _inner(cl):
                    ind = ""
                    try:
                        sc = cl.f10.stock_score(ec, section="pf")
                        rows = getattr(sc, "rows", None) or []
                        if rows:
                            ind = (rows[0].get("N012") or "").strip()
                    except Exception as e:  # noqa: BLE001
                        log.warning("eltdx 行业(F10)拉取失败 %s: %s", code, e)
                    cons = []
                    try:
                        tp = cl.helpers.stock_topics(ec)
                        for t in (getattr(tp, "topics", []) or []):
                            nm = getattr(t, "topic_name", None)
                            if nm is None and isinstance(t, dict):
                                nm = t.get("topic_name")
                            if nm:
                                cons.append(nm)
                    except Exception as e:  # noqa: BLE001
                        log.warning("eltdx 题材拉取失败 %s: %s", code, e)
                    return ind, cons[:8]
                return self._use_client(_inner)
            industry, concepts = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("eltdx 行业/题材获取失败 %s: %s", code, exc)

        rec = {"industry": industry, "concepts": concepts,
               "ts": now_iso()}
        _fetched_at = time.time()

        def _commit() -> None:
            # 锁 + 文件写入整体放线程池：既保并发下不重复落盘，
            # 也不让事件循环线程持 threading.Lock 或同步写磁盘。
            with self.__class__._industry_lock:
                cls_imap[code] = rec
                self.__class__._industry_ts[code] = _fetched_at
                # 空值只留在内存（带 _INDUSTRY_RETRY_TTL 重试窗口），**不落盘**：
                # 否则一次网络抖动就会把磁盘缓存永久污染成"行业 --"，重启后
                # 照样读到假空（R1 实测 300750/688981 即此路径）。
                if industry or concepts:
                    _save_json_cache(_industry_cache_path(), cls_imap)
        await asyncio.to_thread(_commit)
        return rec
