"""当日逐笔成交（``DataSourceManager.get_ticks``）—— 以 mixin 形式拆出。

★ 为什么单独一条能力，而不是复用 ``/market/l2``：
  - ``/market/l2`` 走**券商** ``gateway.get_l2_transactions``，未连接恒 503；
  - 本能力走**公开源**（eltdx 的 ``0x0FC5`` 当日成交明细），**无需券商**。
  看盘界面的「成交流」必须在前者不可用时仍然有真实数据 —— 否则「成交流」
  就只是「券商在线时才有」的装饰，而市场逐笔本身并不依赖券商。

★ 与 ``get_quotes`` 的 mixin 同构（P1-1）：深度依赖 ``self._plugins`` /
``self._call_source`` / ``self._resolve_sources`` 等实例状态，改成自由函数
就要把这些依赖全部当参数传进去 —— 那是接口改造，不是拆文件。

⚠️ 空列表是**合法结果**（如盘前尚无成交、非交易日外的空窗），必须原样返回并
由调用方如实呈现；绝不用假数据填充（项目零 mock 铁律）。
"""
from __future__ import annotations

from typing import Optional

from datasource.instrument import with_exchange_suffix

#: 单次请求的逐笔条数上限 —— 与 eltdx 侧 ``get_ticks`` 的 clamp 保持一致，
#: 防止调用方传 ``count=100000`` 把一页 wire 记录撑爆。
MAX_TICKS = 500


class TicksMixin:
    """见模块 docstring。"""

    async def get_ticks(self, code: str, count: int = 60, source: str = "auto",
                        conn_id: Optional[str] = None) -> Optional[dict]:
        """当日逐笔成交 —— ``{code, items, count, trading_date, source}`` 或 None。

        返回 None 表示**所有候选源都不可用**（未安装 eltdx / 网络不可达 / 熔断中），
        调用方据此给 503 + 引导；返回 ``items == []`` 则表示「源可用但当前没有成交」，
        **两者语义不同，前端文案必须分开**（同 ``OrderBookPanel`` 的空态分叉口径）。
        """
        source = self._validate_source(source)
        # 代码规范化：eltdx 只认带交易所前缀的代码（内部再转 SH/SZ 前缀）
        code = with_exchange_suffix(code)
        try:
            n = max(1, min(int(count or 60), MAX_TICKS))
        except (TypeError, ValueError):
            n = 60

        async def _from_plugin(name: str):
            src = self._plugins.get(name)
            if src is None:
                return None
            fn = getattr(src, "get_ticks", None)
            # 能力声明与实现必须一致（护栏 test_capability_chain_unity 锁死），
            # 这里再守一道：没有该方法的源直接跳过，而不是抛 AttributeError。
            if fn is None:
                return None
            res = await self._call_source(name, fn(code, n))
            if not isinstance(res, dict):
                return None
            items = res.get("items")
            if not isinstance(items, list):
                return None
            return {
                **res,
                "code": code,
                "items": items,
                "count": len(items),
                "source": name,
            }

        if source in self._plugins:
            return await _from_plugin(source)
        # auto：按能力链尝试。ticks 链上只有补充源（见 providers.py 的注释：
        # 券商 L2 逐笔另走 /market/l2，刻意不并入本条链）。
        for name in self._resolve_sources(source, "ticks"):
            if name == "broker":
                continue
            r = await _from_plugin(name)
            if r is not None:
                return r
        return None
