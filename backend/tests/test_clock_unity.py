"""时间戳统一护栏（V11 R8）。

锁定 ``core/clock.py`` 为时间戳的**唯一实现**，并防止「6 种形状」与
「aware/naive 混比」这两类问题重新长回来。

对应文档：``docs/2026-09-15_项目全景梳理与V11重构方案.md`` §8.14。

六条不变量：

1. **形状**：``now_iso()`` 只产出一种形状 ``YYYY-MM-DDTHH:MM:SS``（无偏移）；
2. **读兼容**：``parse_iso()`` 覆盖全部 6 种历史形状，坏值 → ``None``（不抛、不兜底为现在）；
3. **生产者唯一**：除 ``core/clock.py`` 外，**任何生产代码都不得内联产出当前时刻**
   （``datetime.now().strftime/isoformat``、单参 ``time.strftime``、``date.today()``）；
4. **无偏移生产者**：不得再产出带偏移的时间戳（``astimezone().isoformat()`` /
   ``%z`` / 带 ``timezone`` 的 ``isoformat`` / ``utcoffset()``）；
5. **禁止混比**：不得出现「裸 ``datetime.now()`` 与解析值比较」的写法 ——
   字段一旦带偏移即抛 ``TypeError``，且会被 ``except`` 吞成静默错误结论；
6. **消费侧唯一**：生产代码不得用 ``fromisoformat`` 解析生意时刻（统一走 ``parse_iso``）。

第 3/4 条各有一张**显式例外表**（``ALLOWED_*``）：每条例外都必须写明理由，
且 ``test_allowlist_entries_are_not_stale`` 会校验例外文件仍然命中规则 ——
避免「例外」变成永远不被清理的墓碑。
"""
from __future__ import annotations

import ast
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.clock import FORMAT, local_now, now_iso, parse_iso, today_str, to_iso  # noqa: E402

#: 生产代码目录（排除 tests/ 与第三方 runtimes/、打包产物 dist/、build/）
PROD_DIRS = ("app", "core", "datasource", "engines", "gateway",
             "tools", "sync", "mcp_server", "connectors", "plugins",
             "backtest", "xtquant_client", "scripts")

#: 位于 backend/ 根部的生产入口文件
EXTRA_PROD_FILES = ("run.py",)

#: 唯一允许产出时间戳实现的文件
CLOCK_REL = "core/clock.py"

#: 不参与扫描的路径片段（打包副本/第三方/测试）
_SKIP_PARTS = {"__pycache__", "dist", "build", "runtimes", "data", "logs",
               "output", "static", "tests", "xtp_pkg_tmp"}

#: **例外表（带偏移生产者）**：文件 -> 理由。每项都必须能被 test 校验到仍然命中。
ALLOWED_AWARE = {
    "app/logging_setup.py":
        "日志行 JSON 格式含 %z 偏移（人类可读/便于与系统日志对齐）；不落库、不比较",
    "run.py":
        "启动自检**有意**打印本机 UTC 偏移（TOTP 二次确认依赖本机时钟）；非落库时间戳",
}

#: **例外表（内联当前时刻生产者）**：文件 -> 理由。
ALLOWED_INLINE = {
    "engines/algo.py":
        "算法单进度里的展示用 %H:%M:%S（前端直接展示，非落库生意时刻）",
    "engines/limitup.py":
        "幂等键用行情交易日 %Y%m%d、进度用展示 %H:%M:%S；均非 ISO 生意时刻",
    "gateway/db_backup.py":
        "备份文件名用 %Y%m%d_%H%M%S（文件系统安全格式），非 ISO 生意时刻",
    "tools/strategy_gen.py":
        "生成代码模板里的展示用 %H:%M（写进用户策略源码，非平台生意时刻）",
}


def _iter_prod_py():
    for d in PROD_DIRS:
        root = BACKEND / d
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.py")):
            if _SKIP_PARTS & set(p.parts):
                continue
            yield p
    for name in EXTRA_PROD_FILES:
        p = BACKEND / name
        if p.is_file():
            yield p


def _rel(p: Path) -> str:
    return p.relative_to(BACKEND).as_posix()


def _code_lines(p: Path):
    """产出 (行号, 行内容)，**跳过注释行与 docstring 内的行**。

    护栏必须只看**代码**：本文件与 ``core/clock.py`` 的 docstring 里大量引用了
    「反面写法」（如 ``astimezone().isoformat()``），若把 docstring 也算进去会自我误报
    （R7 在 ``_body_source`` 上踩过同一个坑）。
    """
    src = p.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    # 收集所有 docstring 的行号区间
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc = body[0]
                for ln in range(doc.lineno, (doc.end_lineno or doc.lineno) + 1):
                    skip.add(ln)
    for i, line in enumerate(src.splitlines(), 1):
        if i in skip:
            continue
        if line.strip().startswith("#"):
            continue
        yield i, line


# ────────────────────────── 1. 形状 ──────────────────────────

def test_now_iso_shape_is_canonical():
    """``now_iso()`` 只产出 ``YYYY-MM-DDTHH:MM:SS``：无偏移、无微秒、T 分隔。"""
    v = now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", v), v
    assert "+" not in v and " " not in v and "." not in v


def test_format_constant_matches_now_iso():
    assert now_iso() == datetime.now().strftime(FORMAT)


def test_today_str_shape():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", today_str())


def test_local_now_is_naive():
    """``local_now()`` 必须是 naive —— 否则与 ``parse_iso`` 的结果比较会抛 TypeError。"""
    assert local_now().tzinfo is None


def test_to_iso_shape_and_none():
    assert to_iso(None) == ""
    assert to_iso(datetime(2026, 9, 16, 18, 57, 49)) == "2026-09-16T18:57:49"


def test_to_iso_converts_aware_to_local_wall_clock():
    """aware 入参必须先换算到本地时区 —— 直接 strftime 会静默丢掉偏移、产出错误的墙钟时间。"""
    aware = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
    expect = aware.astimezone().replace(tzinfo=None).strftime(FORMAT)
    assert to_iso(aware) == expect
    # 若实现是「直接 strftime」，会得到 10:00:00；本机为 +08，正确值是 18:00:00
    assert to_iso(aware).endswith(":00:00")
    assert to_iso(aware).split("T")[1] != "10:00:00" or \
        datetime.now().astimezone().utcoffset() == timedelta(0)


# ────────────────────────── 2. 读兼容 ──────────────────────────

HISTORICAL_SHAPES = [
    ("2026-09-16T18:57:49", datetime(2026, 9, 16, 18, 57, 49)),          # 主流（now_iso）
    ("2026-09-16 18:57:49", datetime(2026, 9, 16, 18, 57, 49)),          # SQLite DEFAULT
    ("2026-09-16T18:57:49+08:00", datetime(2026, 9, 16, 18, 57, 49)),    # V9 早期 aware
    ("2026-09-16T18:57:49+0800", datetime(2026, 9, 16, 18, 57, 49)),     # %z
    ("2026-09-16", datetime(2026, 9, 16, 0, 0, 0)),                      # 仅日期
]


@pytest.mark.parametrize("raw,expect", HISTORICAL_SHAPES)
def test_parse_iso_covers_historical_shapes(raw, expect):
    got = parse_iso(raw)
    assert got == expect, f"{raw!r} -> {got!r}"
    assert got.tzinfo is None, "parse_iso 必须返回 naive（与 now_iso 同口径）"


def test_parse_iso_handles_z_suffix_as_utc():
    """``Z`` 表示 UTC，须换算到本地（不是当作裸值）。"""
    got = parse_iso("2026-09-16T18:57:49Z")
    assert got == datetime(2026, 9, 16, 18, 57, 49, tzinfo=timezone.utc) \
        .astimezone().replace(tzinfo=None)


@pytest.mark.parametrize("bad", ["", "   ", None, "garbage", "2026-13-45", "abcTdef"])
def test_parse_iso_bad_input_returns_none(bad):
    """坏值 → ``None``：**不抛异常、也不兜底为「现在」**（兜底会让过期判断静默反转）。"""
    assert parse_iso(bad) is None


def test_parse_iso_roundtrip():
    assert to_iso(parse_iso(now_iso())) == now_iso()
    assert abs((parse_iso(now_iso()) - local_now()).total_seconds()) < 2


# ────────── 3. 生产者唯一（内联当前时刻 / 带偏移者清零） ──────────

#: 带偏移的产出形态
_AWARE_PATTERNS = (
    re.compile(r"astimezone\s*\(\s*\)\s*\.\s*isoformat"),      # ...astimezone().isoformat(
    re.compile(r"isoformat\s*\([^)]*timezone"),                # isoformat(...timezone...)
    re.compile(r"strftime\s*\([^)]*%z"),                       # strftime("%...%z")
    re.compile(r"['\"][^'\"]*%z[^'\"]*['\"]"),                 # 任何含 %z 的字符串字面量
    re.compile(r"\.utcoffset\s*\(\s*\)"),                      # 显式偏移运算
)

#: 内联「当前时刻」的产出形态（形状对也不行 —— 必须是唯一实现）
#: 注：``(?<![\w.])`` 用于排除 ``p.time.strftime(...)`` / ``_time.strftime(...)``
#: 这类「属性名叫 time」的假阳性。
_INLINE_PRODUCER_PATTERNS = (
    re.compile(r"datetime\s*\.\s*now\s*\([^)]*\)\s*\.\s*(?:strftime|isoformat)\s*\("),
    re.compile(r"datetime\s*\.\s*datetime\s*\.\s*now\s*\([^)]*\)"
               r"\s*\.\s*(?:strftime|isoformat)\s*\("),
    re.compile(r"(?<![\w.])date\s*\.\s*today\s*\("),
    re.compile(r"(?<![\w.])time\s*\.\s*strftime\s*\("),
)

#: 消费侧：解析生意时刻只允许走 ``parse_iso``
_PARSE_PATTERNS = (re.compile(r"fromisoformat\s*\("),)


def _scan(patterns, allowed: dict) -> list:
    """在全部生产代码里扫描 ``patterns``，返回 ``文件:行: 内容`` 列表（跳过例外文件）。"""
    offenders: list[str] = []
    for p in _iter_prod_py():
        rel = _rel(p)
        if rel == CLOCK_REL or rel in allowed:
            continue
        for ln, line in _code_lines(p):
            for rx in patterns:
                if rx.search(line):
                    offenders.append(f"{rel}:{ln}: {line.strip()}")
                    break
    return offenders


def test_no_aware_timestamp_producer_outside_clock():
    """除 ``core/clock.py`` 外，生产代码不得产出带偏移的时间戳。

    R8 前实测有 7 处**逐字相同**的
    ``datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")``
    （core/db、app/sync/bars、datasource/local_store、datasource/snapshots、
    gateway/alert_engine、gateway/notifier、tools/strategy_market），
    与 ~41 处裸值生产者形状不一致。本用例防止其复活。
    """
    offenders = _scan(_AWARE_PATTERNS, ALLOWED_AWARE)
    assert not offenders, (
        "检测到 core/clock.py 之外的带偏移时间戳生产者（请改用 core.clock.now_iso()）：\n  "
        + "\n  ".join(offenders)
    )


def test_no_inline_current_time_producer_outside_clock():
    """**形状一致也不许**：当前时刻只能由 ``core.clock`` 产出。

    这是比「带偏移者清零」更强的约束 —— R8 收敛前有 **56 处**内联生产者
    （``time.strftime("%Y-%m-%dT%H:%M:%S")`` 26 处、``datetime.now().isoformat(
    timespec="seconds")`` 11 处、``datetime.now().strftime("%Y-%m-%d")`` 等）。
    形状虽与 canonical 相同，但一旦有人改 canonical（加毫秒/换时区/改精度），
    这些副本不会跟着变 —— 这正是「6 种形状」的成因。统一后它们全部改为
    ``now_iso()`` / ``today_str()`` / ``to_iso(...)``。
    """
    offenders = _scan(_INLINE_PRODUCER_PATTERNS, ALLOWED_INLINE)
    assert not offenders, (
        "检测到 core/clock.py 之外的内联当前时刻生产者（请改用 now_iso()/"
        "today_str()/to_iso()/local_now()）：\n  " + "\n  ".join(offenders)
    )


def test_no_business_fromisoformat():
    """生产代码不得用 ``fromisoformat`` 解析生意时刻（统一走 ``parse_iso``）。

    ``fromisoformat`` 对 ``+0800`` 这类非严格 ISO 形状会抛 ``ValueError``，
    而历史库里 6 种形状都存在 —— 用它的地方会「静默降级」成异常分支。
    """
    offenders = _scan(_PARSE_PATTERNS, {})
    assert not offenders, (
        "检测到 core/clock.py 之外的 fromisoformat 消费点（请改用 core.clock.parse_iso）：\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("allowed,patterns",
                         [(ALLOWED_AWARE, _AWARE_PATTERNS),
                          (ALLOWED_INLINE, _INLINE_PRODUCER_PATTERNS)])
def test_allowlist_entries_are_not_stale(allowed, patterns):
    """例外表不许变成「墓碑」：每条被豁免的文件都必须**仍然命中**规则。

    否则说明该文件已经改好了、例外条目该删掉了 —— 留着会掩盖将来新引入的违规。
    """
    stale = []
    for rel in allowed:
        p = BACKEND / rel
        if not p.is_file():
            stale.append(f"{rel}: 文件不存在")
            continue
        hit = any(rx.search(line)
                  for _, line in _code_lines(p) for rx in patterns)
        if not hit:
            stale.append(f"{rel}: 已不再命中任何规则，请从例外表移除")
    assert not stale, "过期例外条目：\n  " + "\n  ".join(stale)


def test_only_clock_defines_now_iso():
    """只有 ``core/clock.py`` 可以**定义** ``now_iso``（其余只允许 re-export/别名导入）。"""
    offenders: list[str] = []
    for p in _iter_prod_py():
        if _rel(p) == CLOCK_REL:
            continue
        for ln, line in _code_lines(p):
            if re.match(r"\s*def\s+now_iso\s*\(", line):
                offenders.append(f"{_rel(p)}:{ln}")
    assert not offenders, "除 core/clock.py 外不得定义 now_iso：" + ", ".join(offenders)


def test_legacy_reexport_shims_still_delegate():
    """存量 ``from core.db import now_iso`` 等 re-export 必须仍指向唯一实现。"""
    from core.db import now_iso as db_now_iso
    from app.sync.bars import now_iso as bars_now_iso
    from datasource.local_store import _now as store_now
    from datasource.snapshots import _now as snap_now
    from gateway.alert_engine import _now_iso as alert_now
    from gateway.notifier import _now_iso as notify_now
    from tools.strategy_market import _now as market_now

    for fn in (db_now_iso, bars_now_iso, store_now, snap_now,
               alert_now, notify_now, market_now):
        assert fn is now_iso, f"{fn!r} 未委托到 core.clock.now_iso"


# ─────────────────── 4. 禁止 aware/naive 混比 ───────────────────

#: 同一行同时出现「裸 now()」与「解析」→ 典型混比写法
_MIXED_COMPARE = re.compile(r"datetime\.now\s*\(\s*\)[^\n]*fromisoformat\s*\(")
#: 更宽松：同一行出现 datetime.now() 与 fromisoformat（顺序无关）
_MIXED_COMPARE_ALT = re.compile(r"fromisoformat\s*\([^\n]*datetime\.now\s*\(\s*\)")


def test_no_naive_aware_mixed_comparison():
    """不得再出现「裸 ``datetime.now()`` 与 ``fromisoformat(...)`` 同行」的写法。

    ``engines/condition_order.py`` 曾写 ``datetime.now() < datetime.fromisoformat(nra)``：
    一旦字段带偏移即抛 ``TypeError``，且被 ``except`` 吞掉 → 条件单**永不到期**、
    永不清算（R8 实测：旧 ``_is_expired`` 对带偏移值返回 ``False``）。
    """
    offenders: list[str] = []
    for p in _iter_prod_py():
        for ln, line in _code_lines(p):
            if _MIXED_COMPARE.search(line) or _MIXED_COMPARE_ALT.search(line):
                offenders.append(f"{_rel(p)}:{ln}: {line.strip()}")
    assert not offenders, (
        "检测到 aware/naive 混比写法（请用 core.clock.parse_iso + local_now）：\n  "
        + "\n  ".join(offenders)
    )


# ────────── 5. 消费侧回归：真缺陷（条件单永不到期） ──────────

#: 全部 6 种历史形状（过去 / 未来各一套）—— 消费点必须对每一种都给出正确结论
_PAST_SHAPES = (
    "2020-01-01T23:59:59",          # 裸值（canonical，now_iso）
    "2020-01-01 23:59:59",          # SQLite DEFAULT（空格）
    "2020-01-01T23:59:59+08:00",    # V9 早期 aware  ← 旧实现的漏网形状
    "2020-01-01T23:59:59+0800",     # strftime("%z")
    "2020-01-01",                   # 仅日期
)
_FUTURE_SHAPES = tuple(s.replace("2020-01-01", "2099-01-01")
                       for s in _PAST_SHAPES)


def test_condition_order_is_expired_handles_all_historical_shapes():
    """``_is_expired`` 必须对**全部历史形状**都给出正确结论。

    R8 前它写 ``datetime.now() > datetime.fromisoformat(expire_at)``：
    ``expire_at`` 一旦带偏移即抛
    ``TypeError: can't compare offset-naive and offset-aware datetimes``，
    且被调用方的 ``except`` 吞掉 → **恒返回「未过期」** → 条件单永不到期、
    永不通知、永不清算。旧实现实测对 ``'2020-01-01T23:59:59+08:00'`` 返回 ``False``。
    """
    from engines.condition_order import _is_expired

    for raw in _PAST_SHAPES:
        assert _is_expired(raw) is True, f"{raw!r} 应判为已过期"
    for raw in _FUTURE_SHAPES:
        assert _is_expired(raw) is False, f"{raw!r} 不应判为已过期"
    # 坏值维持原语义：不视为过期（不引入「坏值即过期」的行为变化）
    for bad in ("", "   ", "garbage", None):
        assert _is_expired(bad) is False, f"{bad!r} 应视为「无法判断 → 不过期」"


def test_condition_order_end_of_day_accepts_all_shapes():
    """``_end_of_day`` 对全部历史形状都要落到**同一自然日**的 23:59:59。"""
    from engines.condition_order import _end_of_day

    for raw in ("2026-09-16T08:00:00", "2026-09-16 08:00:00",
                "2026-09-16T08:00:00+08:00", "2026-09-16T08:00:00+0800",
                "2026-09-16"):
        got = _end_of_day(raw)
        assert got.endswith("T23:59:59"), (raw, got)
        assert got.startswith(parse_iso(raw).date().isoformat()), (raw, got)
    # 空值 → 今天
    assert _end_of_day("").endswith("T23:59:59")
    assert _end_of_day("").startswith(today_str())


def test_condition_order_expire_and_tomorrow_are_canonical_shape():
    """``_compute_expire`` / ``_today`` / ``_tomorrow`` 只产出 canonical 形状。"""
    from engines.condition_order import _compute_expire, _today, _tomorrow

    for v in (_compute_expire(0), _compute_expire(3)):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", v), v
        assert v.endswith("T23:59:59"), v
    assert _compute_expire(3) > _compute_expire(0)          # 3 天后更晚（同形可比）
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", _today())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", _tomorrow())


def test_alert_engine_cooldown_accepts_offset_shape():
    """告警冷却：带偏移形状必须能正确判冷却，坏值维持「放行」原语义。"""
    from gateway.alert_engine import AlertEngine

    class _Dummy:                      # _cooldown_ok 只读 rule，不用实例状态
        pass

    def ok(rule):
        return AlertEngine._cooldown_ok(_Dummy(), rule)

    for raw in _PAST_SHAPES:
        assert ok({"last_triggered": raw, "cooldown_seconds": 300}) is True, raw
    for raw in _FUTURE_SHAPES:
        assert ok({"last_triggered": raw, "cooldown_seconds": 300}) is False, raw
    assert ok({"last_triggered": "garbage", "cooldown_seconds": 300}) is True
    assert ok({"last_triggered": "", "cooldown_seconds": 300}) is True
    assert ok({"last_triggered": now_iso(), "cooldown_seconds": 0}) is True


def test_apikey_expiry_fail_closed_and_accepts_offset_shape():
    """密钥过期判断：坏值 **fail-closed**；带偏移形状必须能正确判定。"""
    from gateway.apikey import ApiKeyStore

    f = ApiKeyStore._is_expired
    for raw in _PAST_SHAPES:
        assert f({"expires_at": raw}) is True, raw
    for raw in _FUTURE_SHAPES:
        assert f({"expires_at": raw}) is False, raw
    assert f({"expires_at": "garbage"}) is True        # 坏值 → 视为已过期（fail-closed）
    assert f({"expires_at": ""}) is False              # 空 → 永不过期
    # 轮换宽限期：宽限期内仍可用
    assert f({"expires_at": "2020-01-01T00:00:00+08:00",
              "grace_until": "2099-01-01T00:00:00"}) is False
