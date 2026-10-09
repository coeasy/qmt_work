"""Canonical 选主规则的**单一性**护栏（2026-09-15）。

背景：仓库里曾有三份「同一 (code, dt) 多源行选哪一条」的实现，规则各不相同 ——

============  ==========================================================
位置           排序键
============  ==========================================================
``get_bars``  质量状态 → ``(provider_id='') DESC`` → ``provider_id``
``get_bars_batch``  同上（``_canonical_key`` 复刻 SQL）
``reconcile_bars``   ``PROVIDER_QUALITY_RANK``（空 provider = 999 最差）
============  ==========================================================

后果：一旦真有第二个数据源写入同一 (code, dt)，会出现
**「消费方读到的行」≠「对账侧标为 canonical 的行」**。
实测分歧：``akshare`` vs ``broker`` → 读侧选 ``akshare``、对账侧选 ``broker``；
``''`` vs ``broker`` → 读侧选 ``''``、对账侧选 ``broker``。

现统一为 :func:`datasource.quality.canonical_sort_key` 一份实现，读侧与对账侧共用。
本文件钉住：① 规则本身；② 两侧结果一致；③ SQL 的 ``CASE`` 由排名表程序化生成
（不会与 Python 侧漂移）。
"""
import pytest

from datasource.local_store import (
    _CANONICAL_ORDER_SQL,
    _PROVIDER_RANK_CASE_SQL,
    _QUALITY_STATE_CASE_SQL,
    LocalStore,
)
from datasource.models import Bar
from datasource.quality import (
    PROVIDER_QUALITY_RANK,
    QUALITY_STATE_RANK,
    canonical_sort_key,
    provider_rank,
    reconcile_bars,
)


@pytest.fixture()
def store(tmp_path):
    from core.db import DB
    db = DB(tmp_path / "test_canonical.db")
    yield LocalStore(db)
    db._conn.close()


def _bar(t, close):
    return Bar(time=t, open=close, high=close + 1, low=close - 1,
               close=close, volume=100, amount=close * 100)


# ---------------------------------------------------------------------------
# ① 规则本身
# ---------------------------------------------------------------------------

def test_provider_rank_empty_is_worst():
    """空 provider（未标注来源）必须是**最差**档，不得压过任何具名源。"""
    assert provider_rank("") == 999
    assert provider_rank(None) == 999
    assert provider_rank("未登记源") == 999
    # 具名源严格优于空
    for p in PROVIDER_QUALITY_RANK:
        assert provider_rank(p) < provider_rank(""), p


def test_provider_rank_matches_documented_chain():
    """质量序与模块文档写明的数据链 QMT→eltdx→baostock→akshare 一致。"""
    assert provider_rank("broker") < provider_rank("tdx")
    assert provider_rank("tdx") < provider_rank("baostock")
    assert provider_rank("baostock") < provider_rank("akshare")
    assert provider_rank("qmt") == provider_rank("broker")   # 同一档
    assert provider_rank("BROKER") == provider_rank("broker")  # 大小写不敏感


def test_canonical_sort_key_priority_order():
    """三级键：质量状态 > provider 质量序 > provider 名。"""
    # 质量状态优先（即使 provider 更差）
    assert canonical_sort_key("validated", "akshare") < canonical_sort_key("unknown", "broker")
    # 同质量 → provider 质量序（broker 胜 akshare，尽管 akshare 名字典序在前）
    assert canonical_sort_key("unknown", "broker") < canonical_sort_key("unknown", "akshare")
    # 同档位 → provider 名升序（确定性 tie-break）
    assert canonical_sort_key("unknown", "broker") < canonical_sort_key("unknown", "qmt")
    # 空 provider 最差
    assert canonical_sort_key("unknown", "broker") < canonical_sort_key("unknown", "")
    # NULL / 未知质量状态落最后一档
    assert canonical_sort_key(None, "broker") == canonical_sort_key("__x__", "broker")
    assert canonical_sort_key(None, "broker")[0] == 3


# ---------------------------------------------------------------------------
# ② 读侧与对账侧必须选出**同一行**
# ---------------------------------------------------------------------------

def _seed_multi_source(store, code):
    """三个 dt，每个都构造「读侧旧规则会选错」的多源数据。

    组内价差刻意 < 0.5%（``reconcile_bars`` 的 ``price_diff_pct`` 阈值）——
    否则整组被判 ``conflict``、不产出 canonical，就测不到「两侧选同一行」。
    """
    # dt1：空 provider vs 具名 → 具名 broker 必须胜（旧读侧选 ''）
    store.upsert_bars(code, [_bar("20260901", 10.00)], adjust="qfq",
                      provider_id="", quality_state="unknown")
    store.upsert_bars(code, [_bar("20260901", 10.02)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")
    # dt2：名字典序在前但质量档更差 → broker 必须胜（旧读侧选 akshare）
    store.upsert_bars(code, [_bar("20260902", 20.00)], adjust="qfq",
                      provider_id="akshare", quality_state="unknown")
    store.upsert_bars(code, [_bar("20260902", 20.02)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")
    # dt3：质量状态优先于 provider 质量序
    store.upsert_bars(code, [_bar("20260903", 30.00)], adjust="qfq",
                      provider_id="akshare", quality_state="validated")
    store.upsert_bars(code, [_bar("20260903", 30.02)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")


def _canonical_row(db, code, dt):
    r = db.query_one("SELECT provider_id, close FROM local_bars "
                     "WHERE code=? AND dt=? AND quality_state='final'", (code, dt))
    return (r["provider_id"], r["close"]) if r else None


def test_read_side_never_prefers_empty_provider(store):
    """回归：空 provider 不得胜具名源（旧规则 `(provider_id='') DESC` 会胜）。"""
    store.upsert_bars("600001.SH", [_bar("20260901", 10.0)], adjust="qfq",
                      provider_id="", quality_state="unknown")
    store.upsert_bars("600001.SH", [_bar("20260901", 11.0)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")
    per = store.get_bars("600001.SH", period="1d", adjust="qfq")
    batch = store.get_bars_batch(["600001.SH"], period="1d", adjust="qfq")
    assert [b.close for b in per] == [11.0]
    assert [b.close for b in batch["600001.SH"]] == [11.0]


def test_read_side_uses_provider_quality_order_not_name_order(store):
    """回归：同质量下按 provider **质量序**，而非名字典序。

    ``akshare`` 名字典序在 ``broker`` 之前，旧规则会选它；新规则必须选 ``broker``。
    """
    store.upsert_bars("600002.SH", [_bar("20260901", 20.0)], adjust="qfq",
                      provider_id="akshare", quality_state="unknown")
    store.upsert_bars("600002.SH", [_bar("20260901", 21.0)], adjust="qfq",
                      provider_id="broker", quality_state="unknown")
    per = store.get_bars("600002.SH", period="1d", adjust="qfq")
    assert [b.close for b in per] == [21.0]


def test_read_side_matches_reconcile_on_multi_source(store):
    """**核心护栏**：``get_bars`` 读到的行 == ``reconcile_bars`` 标 canonical 的行。

    这条是本次统一的目的所在。回退任一侧的排序键（或让空 provider 优先）即红。
    """
    code = "600003.SH"
    _seed_multi_source(store, code)

    # 读侧（逐只 + 批量，两者必须一致）
    per = {b.time: b.close for b in store.get_bars(code, period="1d", adjust="qfq")}
    batch = {b.time: b.close
             for b in store.get_bars_batch([code], period="1d", adjust="qfq")[code]}
    assert per == batch, f"逐只与批量取数结果不一致：{per} vs {batch}"

    # 对账侧
    stats = reconcile_bars(store._db, period="1d", adjust="qfq", lookback_days=100000)
    assert stats["canonical_set"] == 3, stats

    for dt, close in per.items():
        canon = _canonical_row(store._db, code, dt)
        assert canon is not None, f"{dt} 没有 canonical 行"
        assert canon[1] == close, (
            f"{dt}：读侧读到 close={close}，对账侧 canonical close={canon[1]}"
            f"（provider={canon[0]}）—— 两侧选主规则不一致")


def test_batch_selection_equals_per_code_selection(store):
    """① get_bars 与 ② get_bars_batch 必须逐行一致（SQL 与 Python 两套实现等价）。"""
    code = "600004.SH"
    _seed_multi_source(store, code)
    per = [(b.time, b.close) for b in store.get_bars(code, period="1d", adjust="qfq")]
    bat = [(b.time, b.close)
           for b in store.get_bars_batch([code], period="1d", adjust="qfq")[code]]
    assert per == bat


# ---------------------------------------------------------------------------
# ③ SQL 侧不得与 Python 侧漂移
# ---------------------------------------------------------------------------

def test_sql_case_generated_from_rank_tables():
    """SQL 的 ``CASE`` 必须由排名表**程序化生成** —— 新增 provider 时不会漏改 SQL。

    若有人把 ``CASE`` 手写成字面量副本，这里会红。
    """
    for p, r in PROVIDER_QUALITY_RANK.items():
        assert f"WHEN '{p}' THEN {r}" in _PROVIDER_RANK_CASE_SQL, p
    for q, r in QUALITY_STATE_RANK.items():
        assert f"WHEN '{q}' THEN {r}" in _QUALITY_STATE_CASE_SQL, q
    assert "ELSE 999 END" in _PROVIDER_RANK_CASE_SQL
    assert "ELSE 3 END" in _QUALITY_STATE_CASE_SQL
    # 完整 ORDER BY 片段 = 质量状态 CASE + provider 质量序 CASE + provider 名
    assert _CANONICAL_ORDER_SQL == (
        f"{_QUALITY_STATE_CASE_SQL}, {_PROVIDER_RANK_CASE_SQL}, provider_id")


def test_sql_order_matches_python_key_on_multi_source(store):
    """SQL 的 ``ORDER BY`` 与 Python 的 ``canonical_sort_key`` 选出同一行。

    直接对同一批行跑两种排序，比对结果 —— 覆盖「新增一个 provider 但只改了一半」。
    """
    code = "600005.SH"
    for pid, close in (("", 10.0), ("akshare", 20.0), ("broker", 30.0),
                       ("tdx", 40.0), ("baostock", 50.0)):
        store.upsert_bars(code, [_bar("20260901", close)], adjust="qfq",
                          provider_id=pid, quality_state="unknown")

    sql_pick = store._db.query_one(
        "SELECT provider_id, close FROM local_bars WHERE code=? AND period='1d' "
        f"AND adjust='qfq' ORDER BY {_CANONICAL_ORDER_SQL} LIMIT 1", (code,))
    rows = store._db.query(
        "SELECT provider_id, quality_state, close FROM local_bars WHERE code=? "
        "AND period='1d' AND adjust='qfq'", (code,))
    py_pick = min(rows, key=lambda r: canonical_sort_key(r["quality_state"],
                                                         r["provider_id"]))
    assert sql_pick["provider_id"] == py_pick["provider_id"], (
        f"SQL 选 {sql_pick['provider_id']}，Python 选 {py_pick['provider_id']}")
    assert sql_pick["close"] == 30.0  # broker 质量序最优


# =====================================================================
# 2026-10-03：质量状态「表与实现错位」护栏
#
# 背景：QUALITY_STATE_RANK 曾只登记 validated/complete/match，而生产代码实际写的是
# raw/unknown/final/conflict —— 四个真实取值**全部**落在 .get(..., 3) 的 ELSE 档，
# 与彼此同档。后果：reconcile_bars 把胜出者从 raw 升为 final 后，读侧排序键
# (final=3) 与 (raw=3) 完全并列，晋级**存在但无效**，99.98% 的 K 线永卡 raw。
#
# 这类错位不会报任何错（.get 默认值静默兜底），所以只能靠下面的源码扫描钉住。
# =====================================================================
import ast
import pathlib


#: 同形但**写的不是** ``local_bars`` 的模块 —— 它们也定义 ``upsert_bars``，
#: 但落的是另一张表：
#:   - ``datasource/snapshots.py``      -> ``dataset_snapshots``（发布终态词汇）
#:   - ``datasource/intraday_store.py`` -> ``local_bars_intraday``（分钟仓；
#:     ``quality_state`` 同签名接收但**刻意不落库**，见该函数 docstring）
#: 不排除就会把另一套词汇混进 ``local_bars`` 的档位表 —— 测试自己变成误报源。
#: ★ 这份风险本测试 docstring 早已写明，2026-10-09 分钟仓落地时第 1 次真的发生
#:   （``-- ''`` 被当成未登记档位而报红）。
#: ★ 反腐烂：``test_other_table_modules_never_write_local_bars`` 断言名单里的模块
#:   确实不含任何 ``local_bars`` 写语句 —— 排除名单不可能变成藏污点。
_OTHER_TABLE_MODULES = frozenset({
    "datasource/snapshots.py",
    "datasource/intraday_store.py",
})


def _written_quality_states() -> set[str]:
    """AST 扫全后端源码，收集所有写入 **``local_bars.quality_state``** 的字符串字面量。

    ⚠️ 必须限定表：仓库里 **三张表共用 ``quality_state`` 列名但语义不同**——
    ``local_bars`` 走「数据质量档位」词汇（raw/unknown/final/conflict），
    ``dataset_snapshots`` 走「发布终态」词汇（provisional/final/revised/invalid/empty），
    ``local_bars_intraday`` 走「同签名接收但不落库」（分钟仓刻意保持精瘦）。
    不区分表就把几套词汇混在一起，测试自己会变成误报源（见 ``_OTHER_TABLE_MODULES``）。

    覆盖 ``local_bars`` 的四种真实写法：
    - ``upsert_bars(..., quality_state="raw")`` —— 关键字实参（upsert_bars 只写 local_bars）
    - ``def upsert_bars(..., quality_state: str = "unknown")`` —— 带默认值形参
      （**必须按形参名取默认值**，取错就是本测试自己变成误报源）
    - ``UPDATE local_bars SET quality_state='conflict'`` —— SQL 字面量
    - ``quality_state = "x"`` —— 局部赋值（仅在上游已被判定为 local_bars 语境时计入）
    """
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    exclude = {"dist", "build", "runtimes", ".venv", "node_modules", "__pycache__"}
    found: set[str] = set()
    for py in root.rglob("*.py"):
        if any(part in exclude for part in py.parts):
            continue
        if "tests" in py.parts:
            continue
        # ★ 必须 as_posix()：Windows 上 str(Path) 用反斜杠，与 _OTHER_TABLE_MODULES
        #   里的 posix 写法比较会**静默失配**（排除名单看似生效、实际一个都没排掉）。
        rel = py.relative_to(root).as_posix()
        src = py.read_text(encoding="utf-8", errors="replace")

        # SQL 字面量：只认显式指向 local_bars 的 UPDATE
        for m in re.finditer(
                r"UPDATE\s+local_bars\s+SET\s+quality_state\s*=\s*'([A-Za-z_][A-Za-z0-9_]*)'",
                src):
            found.add(m.group(1))

        # 非 local_bars 语境的赋值不采（另两张表各有自己的词汇，见 _OTHER_TABLE_MODULES）
        if rel in _OTHER_TABLE_MODULES:
            continue

        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # 归口判断：直接调 upsert_bars，或 asyncio.to_thread(upsert_bars, ...)
                # 后者是 EOD 同步的实际写法（落库移出事件循环），漏了就漏采 'raw'。
                fn = node.func
                target = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else "")
                is_upsert = target == "upsert_bars"
                if target == "to_thread" and node.args:
                    first = node.args[0]
                    inner = (first.id if isinstance(first, ast.Name) else
                             (first.attr if isinstance(first, ast.Attribute) else ""))
                    is_upsert = inner == "upsert_bars"
                if not is_upsert:
                    continue
                for kw in node.keywords:
                    if kw.arg == "quality_state" and isinstance(kw.value, ast.Constant):
                        found.add(str(kw.value.value))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name != "upsert_bars":
                    continue
                args = node.args
                # args.defaults 右对齐到 posonlyargs+args 的尾部
                pos = list(args.posonlyargs) + list(args.args)
                pos_defaults = {pos[len(pos) - len(args.defaults) + i].arg: d
                                for i, d in enumerate(args.defaults) if d is not None}
                kw_defaults = {k.arg: d for k, d in zip(args.kwonlyargs, args.kw_defaults)
                               if d is not None and k.arg is not None}
                for argname, d in {**pos_defaults, **kw_defaults}.items():
                    if argname == "quality_state" and isinstance(d, ast.Constant):
                        found.add(str(d.value))
    return found


def _written_snapshot_states() -> set[str]:
    """``dataset_snapshots.quality_state`` 的取值（发布终态词汇）。"""
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    py = root / "datasource" / "snapshots.py"
    src = py.read_text(encoding="utf-8", errors="replace")
    found: set[str] = set(re.findall(r"quality_state\s*=\s*'([A-Za-z_][A-Za-z0-9_]*)'", src))
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "quality_state":
                    if isinstance(node.value, ast.Constant):
                        found.add(str(node.value.value))
        elif isinstance(node, ast.Compare):
            # quality_state in ("complete", "match") —— 输入态判定
            if isinstance(node.left, ast.Name) and node.left.id == "quality_state":
                for comp in node.comparators:
                    if isinstance(comp, (ast.Tuple, ast.List)):
                        for e in comp.elts:
                            if isinstance(e, ast.Constant):
                                found.add(str(e.value))
    return found


def test_rank_table_covers_every_quality_state_written_in_source():
    """每一个被生产代码写出的 quality_state 都必须显式登记在排名表内。

    未登记的值会静默落到 ``.get(..., 3)`` 兜底档——正是「晋级无效」的原始缺陷形态。
    这里**禁止兜底掩盖**：宁可让测试变红，也不许新状态悄悄同档。
    """
    written = _written_quality_states()
    registered = set(QUALITY_STATE_RANK)
    missing = written - registered
    assert not missing, (
        f"以下 quality_state 被生产代码写出但未登记 QUALITY_STATE_RANK：{sorted(missing)}\n"
        f"它们会静默落到 ELSE 兜底档，导致 canonical 选主与晋级失效。"
        f"已登记：{sorted(registered)}"
    )


def test_final_outranks_raw_so_promotion_matters():
    """reconcile_bars 的晋级结果必须真的改变排序 —— 否则晋级是空转。"""
    # 同一 provider 下，晋级行必须压过未晋级行
    assert canonical_sort_key("final", "tdx") < canonical_sort_key("raw", "tdx")
    assert canonical_sort_key("final", "tdx") < canonical_sort_key("unknown", "tdx")
    assert QUALITY_STATE_RANK["final"] < QUALITY_STATE_RANK["raw"]


def test_conflict_never_wins_canonical_even_from_best_provider():
    """冲突行给最差档：即使来自质量序最好的 broker，也不该赢得 canonical。"""
    assert QUALITY_STATE_RANK["conflict"] > QUALITY_STATE_RANK["raw"]
    assert canonical_sort_key("raw", "tdx") < canonical_sort_key("conflict", "broker")


def test_sql_case_covers_every_quality_state_written_in_source():
    """SQL 的 ``CASE`` 由排名表程序化生成，须覆盖全部真实取值。

    未命中的值落 ``ELSE 3``，与 Python 侧 ``.get(..., 3)`` 一致——但那是兜底，
    不是设计意图，故不允许出现。
    """
    written = _written_quality_states()
    for state in sorted(written):
        assert f"WHEN '{state}'" in _QUALITY_STATE_CASE_SQL, (
            f"quality_state={state!r} 未出现在 SQL CASE 中，将落到 ELSE 兜底档")


def test_other_table_modules_never_write_local_bars():
    """``_OTHER_TABLE_MODULES`` 里的模块必须**真的不写** ``local_bars`` —— 反腐烂。

    没有这一条，排除名单就会变成藏污点：将来有人在 ``intraday_store.py`` 里
    顺手补一句 ``UPDATE local_bars SET quality_state='...'``，本门禁会因为
    「该模块已被排除」而**放过**它，而那句写法的档位从没被 ``QUALITY_STATE_RANK``
    校验过 —— 正是 TD 里那类「静默同档、晋级无效」的复发形态。
    """
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    writers = re.compile(
        r"(INSERT\s+(?:OR\s+\w+\s+)?INTO\s+local_bars\b"
        r"|UPDATE\s+local_bars\b"
        r"|DELETE\s+FROM\s+local_bars\b"
        r"|REPLACE\s+INTO\s+local_bars\b)", re.I)
    for rel in sorted(_OTHER_TABLE_MODULES):
        path = root / rel
        assert path.exists(), f"_OTHER_TABLE_MODULES 登记了不存在的文件：{rel}"
        src = path.read_text(encoding="utf-8", errors="replace")
        assert not writers.search(src), (
            f"{rel} 被登记为「不写 local_bars」，但源码里出现了对 local_bars 的写语句。"
            f" 请改 _OTHER_TABLE_MODULES，或在 _written_quality_states 中按表细分。")


def test_two_tables_share_quality_state_column_but_not_vocabulary():
    """``quality_state`` 列被两张表共用，但词汇**完全不同**——必须钉住这个事实。

    这是同名异构的经典陷阱：新代码很容易把 ``dataset_snapshots`` 的终态词汇
    （provisional/final/revised/invalid/empty）误当成 ``local_bars`` 的档位词汇写，
    或反之，且**不会有任何报错**（两表各自独立校验）。

    断言两张表的实际取值集**不相交于危险语义**——两者都有 ``final``，但含义不同：
    ``local_bars.final`` = 跨源对账胜出；``dataset_snapshots.final`` = 快照可对外承诺。
    """
    bars_states = _written_quality_states()
    snap_states = _written_snapshot_states()
    # local_bars 侧必须是纯档位词汇
    assert "provisional" not in bars_states and "revised" not in bars_states
    assert "invalid" not in bars_states and "empty" not in bars_states
    # 两套词汇都必须非空（空集说明扫描器坏了，等于测试自废）
    assert bars_states, "扫描器未收集到任何 local_bars 质量状态，扫描逻辑可能失效"
    assert snap_states, "扫描器未收集到任何 dataset_snapshots 终态，扫描逻辑可能失效"
    assert "raw" in bars_states, "local_bars 的默认写入态 raw 未被采集"


def test_finality_states_declares_every_state_snapshots_can_write():
    """``FINALITY_STATES`` 必须覆盖 dataset_snapshots 实际写出的全部终态。

    2026-10-03 发现：``snapshots.py`` 会把空批次降级为 ``quality_state="empty"``
    （绕开 :func:`apply_finality` 的直接写入），但 ``FINALITY_STATES`` 只声明了
    provisional/final/revised/invalid 四档 ⇒ 声明与实现不一致。任何经
    ``apply_finality`` 写入 ``empty`` 的路径会直接 ``ValueError``，而同一个值
    经 ``snapshots.py`` 却能写进去——同一个字段两条路径两套规则。
    """
    from datasource.quality import FINALITY_STATES

    written = _written_snapshot_states()
    # 输入态（会被降级，不作为终态对外承诺）单独放行
    input_states = {"complete", "match"}
    missing = {s for s in written if s not in FINALITY_STATES and s not in input_states}
    assert not missing, (
        f"dataset_snapshots 写出了以下终态但未登记 FINALITY_STATES：{sorted(missing)}\n"
        f"已声明：{FINALITY_STATES}；输入态（可降级，非终态）：{sorted(input_states)}\n"
        "后果：同一字段经 apply_finality 会 ValueError，经 snapshots.py 却能写入。")
