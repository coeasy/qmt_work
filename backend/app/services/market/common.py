"""行情域共享设施：服务层错误 / TTL 缓存 / 代码归一与搜索富化 / 指数清单。

自 routes/market.py 原样下沉（参数与行为零变更），路由层只保留端点签名与信封包装。
"""
import asyncio
import logging
import time

from datasource.instrument import classify_instrument
from datasource.pinyin import matches_initials, pinyin_initials

log = logging.getLogger("qmt_work.market")


class ServiceError(Exception):
    """服务层可预期失败：路由层捕获后按 (code, message) 返回统一 err 信封。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def quote_error(code: str, source: str) -> tuple:
    """按 source 返回清晰的行情不可用错误参数（取代静默 null）。"""
    if source == "eltdx":
        return 503, f"TDX 行情源不可用：{code}（请检查网络，或连接券商获取更稳定行情）"
    if source == "broker":
        return 503, f"行情获取失败：券商连接异常或未连接，无法取到 {code} 行情。"
    return 503, f"行情获取失败：{code} 券商不可用且 TDX 行情源亦无数据，请连接券商或检查网络。"


class TTLCache:
    """轻量 TTL 缓存：多窗口同时打开指数条/板块榜时不重复打源。

    只缓存"清单类/快照类"高频且可接受秒级延迟的数据；个股 quote/K线 走既有链路不缓存。
    R1 容量防护：达到上限先回收超过 hard_ttl 的死键，仍超限按最旧写入时间淘汰至 keep 水位。
    """

    def __init__(self, max_entries: int = 512, keep: int = 480, hard_ttl: float = 3600):
        self._data: dict = {}
        self._max = max_entries
        self._keep = keep
        self._hard_ttl = hard_ttl

    def get(self, key: str, ttl: float):
        hit = self._data.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        return None

    def set(self, key: str, val) -> None:
        now = time.time()
        if len(self._data) >= self._max:
            # 先清理全部过期键（get 只查单个 key，过期残留靠这里回收）
            for k in [k for k, (ts, _) in self._data.items() if now - ts >= self._hard_ttl]:
                self._data.pop(k, None)
            # 仍超限则按「最旧写入时间」淘汰至 keep 水位
            if len(self._data) >= self._max:
                oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])
                for k, _ in oldest[: len(self._data) - self._keep]:
                    self._data.pop(k, None)
        self._data[key] = (now, val)


# 多窗口同时打开指数条/板块榜时不重复打源的全局缓存（原 routes/market.py `_TTL_CACHE`）。
TTL = TTLCache()


def enrich_search_row(row: dict, q: str) -> dict:
    """搜索结果富化：补 type/exchange/board/match/pinyin（缺数据不伪造）。"""
    code = str(row.get("code", "") or "")
    name = str(row.get("name", "") or "")
    cls = classify_instrument(code, name)
    ql = (q or "").lower()
    if row.get("match"):
        match = row["match"]
    elif code.lower() == ql or (name and name.lower() == ql):
        match = "exact"
    elif code.lower().startswith(ql):
        match = "code"
    elif name and (ql in name.lower() or code.lower() in ql):
        match = "name"
    elif name and ql.isalpha() and matches_initials(name, ql):
        match = "pinyin"
    else:
        match = "name"
    out = dict(row)
    out.update({"type": cls["type"], "exchange": cls["exchange"],
                "board": cls["board"], "label": cls["label"], "match": match})
    if name:
        out["pinyin"] = pinyin_initials(name)
    return out


def normalize_code(q: str) -> list:
    """把用户输入归一为候选 QMT 代码列表（代码段 × 交易所推断）。

    支持：600519 / 600519.SH / sh600519 / 513090 / 000001（歧义码双候选）。
    歧义码（000 开头：沪=指数 / 深=股票）返回双候选，由上层让用户确认。
    非代码输入返回空列表。
    """
    s = (q or "").strip().upper()
    if not s:
        return []
    num = "".join(ch for ch in s if ch.isdigit())
    # 已带交易所后缀：直接归一
    if s.endswith((".SH", ".SZ", ".BJ")) and num:
        return [f"{num}{s[-3:]}"]
    # sh600519 / SZ000001 前缀式
    low = s.lower()
    for pref, suf in (("sh", ".SH"), ("sz", ".SZ"), ("bj", ".BJ")):
        if low.startswith(pref) and num:
            return [f"{num}{suf}"]
    if not num or not num.isdigit() or not (4 <= len(num) <= 8):
        return []
    # 纯数字：按代码段推断（歧义码双候选；注意 899/920 北交所段须在 9 沪 B 段之前）
    if num.startswith("899") or num.startswith("920"):
        return [f"{num}.BJ"]
    if num.startswith("399"):
        return [f"{num}.SZ"]
    if num.startswith(("60", "68", "9", "51", "56", "58", "11", "88")):
        return [f"{num}.SH"]
    # 000 开头歧义：沪=指数、深=股票 → 双候选（调用方据名称表定夺）；
    # 须在 "00" 深市段之前判断，否则被 "00" 提前命中、指数候选永远丢失。
    if num.startswith("000"):
        return [f"{num}.SH", f"{num}.SZ"]
    if num.startswith(("00", "30", "2", "15", "16", "12")):
        return [f"{num}.SZ"]
    if num.startswith(("83", "87", "92", "43")):
        return [f"{num}.BJ"]
    return [f"{num}.SH", f"{num}.SZ"]


DEFAULT_INDICES = [
    "000001.SH",  # 上证指数
    "399001.SZ",  # 深证成指
    "399006.SZ",  # 创业板指
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000016.SH",  # 上证50
    "000688.SH",  # 科创50
    "899050.BJ",  # 北证50
]


def configured_indices(state) -> list:
    """R8/B4：指数清单 runtime_config 化。

    读 `market.indices.list`（逗号分隔 QMT 代码），留空/非法回退内置 DEFAULT_INDICES，
    改清单经 PUT /config/runtime 即可热生效，无需改代码重发版。
    state 由调用方注入（app.state），服务层不直接依赖单例容器。
    """
    try:
        rc = state.runtime_config
        raw = (rc.get("market.indices.list") or "") if rc else ""
    except Exception:  # noqa: BLE001
        raw = ""
    conf = [c.strip() for c in str(raw).replace("，", ",").split(",") if c.strip()]
    return conf or DEFAULT_INDICES


# R3：/market/quotes 缓存补齐打源并发上限（报价牌大清单全 miss 时不打爆 TDX 侧）。
QUOTES_FILL_SEM = asyncio.Semaphore(8)
# 指数快照 / spark 并发上限（避免连接风暴）
INDICES_SEM = asyncio.Semaphore(6)
# ETF 快照并发上限：无并发控制时 800 只逐只打 TDX 快照约 95s（实测 60 只 7.15s），
# 远超前端 15s 超时，页面必然失败（P0-1）。限流到 8 路后仍可控，且首屏已不依赖全量快照。
ETF_QUOTE_SEM = asyncio.Semaphore(8)
ETF_QUOTE_CAP = 150      # with_quote 单批最多补齐的只数
ETF_LIST_TTL = 300       # ETF 清单 5 分钟缓存（清单基本不日内变化）
# 成分股资金流并发上限（避免连接风暴）
BOARD_MF_SEM = asyncio.Semaphore(10)
# 板块轮动 K 线并发上限
ROTATION_SEM = asyncio.Semaphore(6)
