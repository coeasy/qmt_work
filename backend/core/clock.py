"""统一时间戳：**唯一实现**（V11 R8 收敛）。

## 为什么需要它

审计（2026-09-16）发现「取当前时刻并写成字符串」在全仓有 **8 处独立实现**：
``core/db.py`` / ``app/sync/bars.py`` / ``datasource/local_store.py`` /
``datasource/snapshots.py`` / ``gateway/alert_engine.py`` / ``gateway/notifier.py`` /
``tools/strategy_market.py`` 各写了一份**逐字相同**的

    datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

（产出 ``2026-09-16T18:57:49+08:00``，**带偏移**）；另有 ~41 处直接写
``time.strftime("%Y-%m-%dT%H:%M:%S")``（产出 ``2026-09-16T18:57:49``，**无偏移**）；
以及 SQLite 的 ``datetime('now','localtime')``（产出 ``2026-09-16 18:57:49``，**空格分隔**）。
同一个「时刻」因此有 **6 种形状**，且**同一列可能被两种形状各写一次** —— 实测
``schedules.updated_at``：INSERT 走 SQLite DEFAULT → 空格形，UPDATE 走代码 → T 形。

## 契约（唯一）

**写** —— :func:`now_iso` 返回 ``"2026-09-16T18:57:49"``：**ISO 本地时间、秒精度、无偏移**。

选它的理由不是"最好看"，而是**与本项目既有约定一致，且对全部存量消费者零风险**：

1. 项目既有约定就是「ISO 本地时间」—— ``core/db_migrations.py:624`` 注释明写
   「上次触发（ISO 本地时间）」；``schedules.timezone`` 固定 ``Asia/Shanghai``；
   前端 ``frontend-next/src/shared/time.ts`` 一律按**本地时间**构造，并明确禁止
   ``new Date("2026-08-17")``（ECMAScript 会把纯日期串按 UTC 解析，GMT+8 下整体错位一天）。
2. 对存量消费者**零行为变化**：``datetime.timestamp()`` 对「裸值本地」与「``+08:00``」
   得到**同一数值**；前端 ``SEPARATED`` 正则只取到秒、忽略尾部偏移；SQLite 前缀比较不变。
3. 因此只需把**偏离约定的 7 处 aware 生产者**改过来，而不是把 41 处裸值改成 aware，
   即可让「同一语义」只剩**一种**形状 —— 改动面小一个数量级，且彻底消除
   aware/naive 混比风险。

**读** —— :func:`parse_iso` **容忍全部历史形状**（见其 docstring），故存量数据无需
迁移即可读；新旧数据可共存。

**比较** —— 一律用 :func:`local_now` 配 :func:`parse_iso`。**禁止**裸 ``datetime.now()``
直接与解析值比较：``engines/condition_order.py`` 曾写
``datetime.now() < datetime.fromisoformat(nra)``，字段一旦带偏移即抛
``TypeError: can't compare offset-naive and offset-aware datetimes``。

## 与「交易所时区时钟」的区别（不要误统一）

本模块解决的是**格式**统一，用的是**宿主机本地时区**。若业务要的是「交易所所在地
（Asia/Shanghai）的日历时刻」——例如 EOD 触发、交易日归属 —— 那是**另一个问题**，
已有专门实现 ``gateway/market_sync.py::_sh_now()``（显式 ``ZoneInfo("Asia/Shanghai")``，
避免跨时区部署在错误时刻触发收盘同步）。两者不可互相替代：**不要**把 ``_sh_now``
改成 ``local_now``，也**不要**把 ``local_now`` 改成交易所时区。

## 护栏

``backend/tests/test_clock_unity.py`` 锁死四条：① 全仓只有本模块产出**带偏移**的时间戳；
② 只有本模块定义 ``now_iso``；③ :func:`parse_iso` 覆盖全部 6 种历史形状；
④ 不允许「裸 ``datetime.now()`` 与解析值比较」的新代码。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

__all__ = ["FORMAT", "bar_date", "local_now", "now_iso", "parse_iso", "today_str", "to_iso"]

#: 唯一写格式：ISO 本地时间、秒精度、无偏移。
FORMAT = "%Y-%m-%dT%H:%M:%S"

#: :func:`parse_iso` 的兜底格式（3.11 的 ``fromisoformat`` 已能覆盖，此处兼容更早版本）。
_FALLBACK_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d")


def local_now() -> datetime:
    """当前**本地**时间（naive，与 :func:`now_iso` 同口径）。

    ⚠️ 刻意返回 naive：本模块的写格式不带偏移，读回来的也是 naive。若这里返回 aware，
    调用方拿它与 :func:`parse_iso` 的结果比较就会抛 ``TypeError``。
    """
    return datetime.now()


def now_iso() -> str:
    """当前时刻的唯一表示：``"2026-09-16T18:57:49"``（ISO 本地时间，秒精度，无偏移）。"""
    return datetime.now().strftime(FORMAT)


def today_str() -> str:
    """当前本地日期：``"2026-09-16"``。用于「交易日 / 按日聚合」这类**只要日期**的场景。"""
    return datetime.now().strftime("%Y-%m-%d")


def to_iso(dt: Optional[datetime]) -> str:
    """把 ``datetime`` 规范化成 :func:`now_iso` 的形状；``None`` → ``""``。

    ``aware`` 入参会**先换算到本地时区**再格式化 —— 直接 ``strftime`` 一个带偏移的值
    会把偏移丢掉、静默产出一个错误的墙钟时间（例如 UTC 的 ``10:00+00:00`` 会变成
    ``10:00``，而本机此刻是 ``18:00``）。
    """
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.strftime(FORMAT)


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    """宽容解析历史时间戳，返回 **naive 本地时间**（与 :func:`now_iso` 同口径）。

    覆盖全部已知形状（实测 6 种）::

        "2026-09-16T18:57:49"          # 主流：本项目 now_iso()
        "2026-09-16 18:57:49"          # SQLite datetime('now','localtime')
        "2026-09-16T18:57:49+08:00"    # V9 早期 aware 生产者
        "2026-09-16T18:57:49+0800"     # strftime("%z")
        "2026-09-16T18:57:49Z"         # UTC 标记
        "2026-09-16"                   # 仅日期（→ 当日 00:00:00）

    无法解析时返回 ``None`` —— **不抛异常，也不兜底为「现在」**：把坏值当「现在」会让
    过期判断静默反转，比直接暴露问题危险得多。调用方应自行决定兜底策略。
    """
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None

    dt: Optional[datetime]
    try:
        dt = datetime.fromisoformat(t)
    except (TypeError, ValueError):
        dt = None
        for fmt in _FALLBACK_FORMATS:
            try:
                dt = datetime.strptime(t, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None

    # 统一换算成本地 naive：带偏移的按绝对时刻换算，裸值视为**已经是本地时间**。
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def bar_date(value) -> str:
    """把任意形状的行情日期规范化成 **K 线唯一交易日格式** ``"YYYYMMDD"``。

    ★ 唯一入口（V11 R13）：此前各数据源各写各的形状并**直接落库** ——
    QMT/券商给 ``"20260825"``、腾讯在线源给 ``"2026-09-18"``、东财给
    ``datetime``/``"2026/09/18"``，``local_bars.dt`` 因此**混存两种字符串**。
    后果不是「看着不整齐」，而是四处**静默错误**：

    1. 字符串排序错乱：``'2026-09-18' < '20260825'``（``'-'`` 0x2D < ``'0'`` 0x30），
       ``MAX(dt)`` 永远取不到真正的最后一根；
    2. 主键 ``(code, period, adjust, dt, provider_id)`` 对**同一天**产出两行，
       跨源对账把它们当成「两个不同交易日」而永远对不上；
    3. ``WHERE dt BETWEEN start AND end`` 区间过滤对另一种格式整体失效；
    4. 消费方按 ``"%Y%m%d"`` 硬解析时直接抛 ``ValueError``。

    实测（2026-09-18 全市场同步）：163 万根里 3894 根是带横线格式，
    ``MAX(dt)='20260825'`` 由**唯一一只**股票贡献，而 5093 只实际停在 ``20250418``
    —— 「数据看着是新的，实际全是陈的」。

    无法解析时返回 ``""``（**不抛异常、不兜底为今天**）：坏日期应当暴露成
    「这一行不可用」，而不是被改写成「最新」。

    注：``datetime`` 入参走 ``strftime`` 而非 ``parse_iso``，避免
    ``datetime -> str -> datetime`` 的无谓往返；``date`` 同理。
    """
    if value is None:
        return ""
    if hasattr(value, "strftime"):  # datetime / date
        return value.strftime("%Y%m%d")
    t = str(value).strip()
    if not t:
        return ""
    # 快路径：已经是规范形状且是合法日期（拦住 "20261332" 这类数字垃圾）
    if len(t) == 8 and t.isdigit():
        return t if _valid_ymd(t) else ""
    dt = parse_iso(t)
    return dt.strftime("%Y%m%d") if dt is not None else ""


def _valid_ymd(s: str) -> bool:
    """``"YYYYMMDD"`` 是否为真实存在的日期（拦 ``20260231`` / ``20261332``）。"""
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return False
    return True
