"""G2-2 指标引擎 REST 暴露（GET 只读）。

- ``GET /market/indicators``：指标目录（元数据单一真源，供前端/Agent 发现）。
- ``GET /market/indicators/calc``：按 code 取真实 K 线后**服务端计算**指标——
  前端 K 线不再自带 7 个本地指标实现，改为消费本端点（R2 断崖收敛）。

**G3 联动**：两端点均为 GET 且签名只含简单类型命名参数（无 Request/**kwargs），
经能力自描述自动暴露为 MCP tool（auto_safe），Agent 可直接调用。

参数约定：``period`` 为 K 线周期（与 /market/kline 一致）；指标窗口参数用
``win``（ma/ema/rsi 的窗口，映射到指标参数 period）、``n``（kdj/wr/boll 窗口）、
``m``（boll 标准差倍数）——避免与 K 线 period 冲突，且保持签名 auto_safe。
"""
from typing import Optional

from fastapi import APIRouter

from app.indicators import calc, get_indicator, list_indicators
from app.routes._common import err, ok

router = APIRouter()


@router.get("/market/indicators")
async def market_indicators():
    """指标目录：返回全部已注册指标（名称/分类/参数/输出/公式说明）。"""
    items = list_indicators()
    return ok({"items": items, "count": len(items)})


@router.get("/market/indicators/calc")
async def market_indicators_calc(
    name: str,
    code: str = "",
    period: str = "1d",
    count: int = 250,
    adj: str = "",
    source: str = "auto",
    win: Optional[int] = None,      # ma/ema/rsi 窗口（映射到指标参数 period）
    n: Optional[int] = None,        # kdj/wr/boll 窗口
    m: Optional[float] = None,      # boll 标准差倍数
):
    """服务端计算指标：按 code 取真实 K 线（fetch_kline_cached，缓存优先），
    经统一指标引擎计算，返回 {code, period, adjust, count, source, cached_at,
    name, params, outputs}。outputs 各列与 K 线对齐，窗口不足处为 null。
    """
    try:
        spec = get_indicator(name)
    except KeyError as exc:
        return err(400, str(exc))
    if not code:
        return err(400, "缺少股票代码 code")

    from tools import fetch_kline_cached
    try:
        res = await fetch_kline_cached(code, period, count, source=source,
                                       adjust=adj or None)
    except Exception as exc:  # noqa: BLE001
        return err(503, f"K 线获取失败：{exc}")
    bars = res.get("bars") or []
    if not bars:
        return err(503, "K 线获取失败：行情源无数据（请连接券商或检查网络）。")

    params: dict = {}
    if win is not None:
        params["period"] = win      # ma/ema/rsi 的窗口参数名为 period
    if n is not None:
        params["n"] = n
    if m is not None:
        params["m"] = m
    try:
        out = calc(name, bars, **params)
    except ValueError as exc:
        return err(400, str(exc))

    return ok({
        "code": code, "period": period, "adjust": adj or "",
        "count": len(bars), "source": res.get("source"),
        "cached_at": res.get("cached_at"),
        "name": out["name"], "params": out["params"], "outputs": out["outputs"],
        "meta": {k: spec.to_dict()[k] for k in ("label", "category", "formula")},
    })
