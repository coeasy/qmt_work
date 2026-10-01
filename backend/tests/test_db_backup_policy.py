"""DB 自动备份的「占用闸门」与「跳过重复备份」契约（2026-09-20 实测缺陷修复）。

## 为什么要有这个文件

原实现只有 ``keep=10`` 这一个上限，而备份是**整库全量复制**：

    1.14 GB 主库 × 10 份 = 11.4 GB 常驻磁盘

实测用户数据目录 ``backups/`` 就是 **11 GB**（C: 321 GB 仅剩 47 GB / 已用 86%），
而 ``list_backups()`` 一个调用方都没有 ⇒ 界面上完全不可见，用户只能等磁盘满。
磁盘满在本项目不是理论风险：它会让同步写库失败、测试假失败。

本文件锁定的不变量：

1. **体积上限必须真的生效**（不能只算份数）；
2. **``min_keep`` 是地板**：预算再小也不能把备份删光 —— 那等于静默关掉保护；
3. **没变化就跳过**，且跳过**跨进程有效**（客户端反复启停不该重复整库复制）；
4. **跳过 ≠ 失败**：两者返回值都是 ``None``，必须靠 ``last_status["action"]`` 区分
   （把「跳过」渲染成「备份失败」就是错误归因）；
5. **未纳管的库副本只上报、绝不删**（``app.db.bak_*`` 可能是用户自己留的）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

from gateway.db_backup import DBBackup, human_size, stray_db_files


# --------------------------------------------------------------- 工具

def _mk(tmp_path, keep=10, max_total_mb=0.0, min_keep=2):
    """建一个「主库 + 备份器」。``max_total_mb=0`` = 只按份数（旧行为）。"""
    dbp = tmp_path / "app.db"
    dbp.write_bytes(b"x" * 1000)
    return dbp, DBBackup(dbp, keep=keep, max_total_mb=max_total_mb, min_keep=min_keep)


def _backup_files(tmp_path):
    return sorted((tmp_path / "backups").glob("app.*.db"))


def _sizes(tmp_path):
    return sorted(p.stat().st_size for p in _backup_files(tmp_path))


def _grow(dbp, n):
    """改主库（大小必变 ⇒ 指纹必变）。"""
    dbp.write_bytes(b"y" * n)


# --------------------------------------------------------------- 基本行为

def test_backup_creates_file_and_records_status(tmp_path):
    dbp, b = _mk(tmp_path)
    path = b.backup_once("t")
    assert path and Path(path).exists()
    assert Path(path).parent == tmp_path / "backups"
    assert b.last_status["action"] == "created"
    assert b.last_status["reason"] == "t"
    assert len(_backup_files(tmp_path)) == 1


def test_missing_db_reports_failure_not_skip(tmp_path):
    """★ 主库不存在 = **失败**，不是「跳过」。两者返回值都是 None，只能靠 action 区分。"""
    b = DBBackup(tmp_path / "nope.db", max_total_mb=0.0)
    assert b.backup_once("t") is None
    assert b.last_status["action"] == "failed"
    assert "不存在" in b.last_status["detail"]


def test_second_resolution_names_do_not_overwrite(tmp_path):
    """★ 同一秒内多次备份必须落到**不同**文件。

    文件名只到「秒」（``app.YYYYMMDD_HHMMSS.db``），而备份可以在同一秒里被触发
    多次（启动 + 手动 + 关闭）。直接写同名文件 = 静默覆盖上一份备份，
    等于悄悄少一个还原点，且因为跳过判断只认最新那份，连异常都看不出来。
    """
    dbp, b = _mk(tmp_path)
    for i in range(3):
        _grow(dbp, 1000 + i * 100)
        assert b.backup_once(f"t{i}") is not None
    names = [p.name for p in _backup_files(tmp_path)]
    assert len(names) == 3, names
    assert len(set(names)) == 3, f"备份文件名重名 ⇒ 互相覆盖：{names}"


# --------------------------------------------------------------- 跳过重复备份

def test_unchanged_source_skips(tmp_path):
    """主库自上次备份以来没变 ⇒ 跳过（不产生第二份 1GB 复制）。"""
    dbp, b = _mk(tmp_path)
    assert b.backup_once("t1") is not None
    assert b.backup_once("t2") is None
    assert b.last_status["action"] == "skipped"
    assert len(_backup_files(tmp_path)) == 1
    # 跳过的文案必须说清「为什么不用备份」，而不是含糊的失败
    assert "未发生变化" in b.last_status["detail"]


def test_skip_is_cross_process(tmp_path):
    """★ 跳过必须**跨进程**有效：客户端反复启停不该每次都整库复制一遍。

    指纹落盘在 ``backups/.last_backup.json``；新实例（模拟重启）读它。
    """
    dbp, b1 = _mk(tmp_path)
    assert b1.backup_once("startup") is not None
    b2 = DBBackup(dbp, keep=10, max_total_mb=0.0)
    assert b2.backup_once("startup") is None
    assert b2.last_status["action"] == "skipped"
    assert len(_backup_files(tmp_path)) == 1


def test_skip_lapses_when_covering_backup_deleted(tmp_path):
    """★ 被记录的那份备份若已被清理，覆盖关系失效 ⇒ 必须重新备份。

    否则会出现「指纹说已有备份、磁盘上其实没有」—— 用户以为有还原点，实际没有。
    """
    dbp, b = _mk(tmp_path)
    assert b.backup_once("t1") is not None
    for f in _backup_files(tmp_path):
        f.unlink()
    assert b.backup_once("t2") is not None
    assert b.last_status["action"] == "created"
    assert len(_backup_files(tmp_path)) == 1


def test_changed_source_always_backs_up(tmp_path):
    dbp, b = _mk(tmp_path)
    assert b.backup_once("t1") is not None
    _grow(dbp, 5000)
    assert b.backup_once("t2") is not None
    assert len(_backup_files(tmp_path)) == 2


def test_sidecar_is_not_counted_as_a_backup(tmp_path):
    """指纹旁挂文件不能污染备份清单（否则「保留 N 份」会凭空少一份）。"""
    dbp, b = _mk(tmp_path)
    b.backup_once("t1")
    assert (tmp_path / "backups" / ".last_backup.json").exists()
    listed = [x["name"] for x in b.list_backups()]
    assert len(listed) == 1, listed
    assert listed[0].startswith("app.") and listed[0].endswith(".db"), listed
    assert ".last_backup" not in listed[0]


def test_broken_sidecar_degrades_to_backup(tmp_path):
    """指纹文件损坏 ⇒ 当作「没有覆盖」重新备份，绝不抛。"""
    dbp, b = _mk(tmp_path)
    b.backup_once("t1")
    (tmp_path / "backups" / ".last_backup.json").write_text("{not json", encoding="utf-8")
    assert b.backup_once("t2") is not None


def test_after_create_side_effect_does_not_break_skip(tmp_path):
    """★ 调用方的副作用（写审计行等）必须在**采指纹之前**执行。

    审计行本身就是写库。若指纹在回调之前采，下一次调用必然认为「主库变了」，
    于是「没变化就跳过」永远不成立 —— 实测用户连点两次「立即备份」会白复制
    两份整库（1.14GB × 2）。这个顺序是本文件里最容易被改回去的一条。
    """
    dbp, b = _mk(tmp_path)

    def hook(dst):  # 模拟写一条审计行
        with dbp.open("ab") as f:
            f.write(b"audit-row")

    assert b.backup_once("t1", hook) is not None
    assert b.last_status["action"] == "created"
    # 回调写进去的内容已被这次备份覆盖 ⇒ 再调必须跳过
    assert b.backup_once("t2", hook) is None
    assert b.last_status["action"] == "skipped"


def test_after_create_hook_failure_never_breaks_backup(tmp_path):
    """回调抛异常只记日志：备份已经落盘，不能被审计失败连坐。"""
    dbp, b = _mk(tmp_path)

    def boom(dst):
        raise RuntimeError("audit down")

    assert b.backup_once("t1", boom) is not None
    assert b.last_status["action"] == "created"


# ------------------------------------------------- A5：WAL 检查点 vs 跳过判断

def _wal_db(tmp_path, rows=2000):
    """建一个**真实**的 WAL 模式 SQLite 主库 + 备份器。

    ★ 为什么不能用 ``_mk()``：它写的是随机字节，不是 SQLite 库，
    ``PRAGMA wal_checkpoint`` 对它毫无意义 ⇒ 用假文件根本复现不出 A5。
    """
    dbp = tmp_path / "app.db"
    conn = sqlite3.connect(str(dbp))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"v{i}",) for i in range(rows)])
    conn.commit()
    b = DBBackup(dbp, keep=10, max_total_mb=0.0, min_keep=2)
    return dbp, conn, b


def test_pure_wal_checkpoint_does_not_defeat_skip(tmp_path):
    """★★ A5：一次**纯 WAL 检查点**不改变任何逻辑数据，不该让跳过失效。

    缺陷现场（2026-09-21 实测）：指纹按**文件形态**取 (size, mtime_ns)，而 WAL 模式下
    同一个逻辑状态有多种形态 ——

        备份时      主库  4096B / -wal 45352B
        检查点后    主库 36864B / -wal     0B      ← 逻辑数据一字未改

    两者不等 ⇒ ``_covered_by()`` 判定「源已变化」⇒ 白复制一份**整库**。
    用户侧的表现是「明明什么都没改，磁盘又少了一份整库的空间」。
    """
    dbp, conn, b = _wal_db(tmp_path)
    try:
        assert b.backup_once("t1") is not None
        assert b.last_status["action"] == "created"
        before = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        assert before == after, "前置不成立：检查点本身不该改逻辑数据"
        assert b.backup_once("t2") is None
        assert b.last_status["action"] == "skipped", b.last_status
        assert len(_backup_files(tmp_path)) == 1, _backup_files(tmp_path)
    finally:
        conn.close()


def test_repeated_noop_checkpoints_do_not_defeat_skip(tmp_path):
    """★★ A5 的反向护栏：**空操作检查点**也不能让跳过失效。

    这条专门挡住一个看似显然、实测**错误**的修法 ——「采指纹前先跑检查点」但
    **仍把 ``-wal`` 的 mtime 记进指纹**。实测：一次无内容可合并的
    ``wal_checkpoint(TRUNCATE)`` 会把 ``-wal`` 的 mtime 更新掉（size 恒为 0），
    于是每次判断都算「变了」⇒ **永远跳过不了** ⇒ 每次点击都白复制一份整库。
    那比原缺陷更糟：原缺陷只在真的发生检查点时才重复备份。

    ⇒ 归一化成功后必须**剔除 ``-wal`` 的 mtime**，只认主库。
    """
    dbp, conn, b = _wal_db(tmp_path)
    try:
        assert b.backup_once("t1") is not None
        for i in range(3):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            assert b.backup_once(f"noop{i}") is None, (
                f"第 {i + 1} 次空操作检查点后仍应跳过，"
                f"实得 {b.last_status['action']}：{b.last_status.get('detail')}")
            assert b.last_status["action"] == "skipped"
        assert len(_backup_files(tmp_path)) == 1, _backup_files(tmp_path)
    finally:
        conn.close()


def test_real_write_after_checkpoint_still_backs_up(tmp_path):
    """★ A5 反向验证：归一化**不能**把真变化吃掉 —— 那是**静默丢备份**，更危险。

    三种写法都要检出（含「页数不变」的原地 UPDATE，它连主库 size 都不变，
    只有 mtime 会动 ⇒ 正好验证 mtime 精度这一环）。
    """
    dbp, conn, b = _wal_db(tmp_path)
    try:
        assert b.backup_once("t1") is not None
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert b.backup_once("skip") is None
        assert b.last_status["action"] == "skipped"
        for label, act in (
            ("原地 UPDATE", lambda: conn.execute("UPDATE t SET v='x' WHERE id=1")),
            ("小 INSERT", lambda: conn.execute("INSERT INTO t (v) VALUES ('n')")),
            ("删一行", lambda: conn.execute("DELETE FROM t WHERE id=2")),
        ):
            act()
            conn.commit()
            assert b.backup_once(label) is not None, f"{label} 必须触发备份"
            assert b.last_status["action"] == "created", (label, b.last_status)
            # 备份之后立刻再调必须能跳过（跳过链不能断）
            assert b.backup_once("again") is None, (label, b.last_status)
    finally:
        conn.close()


# --------------------------------------------------------------- A11（同 tick 盲区）
def test_pending_wal_frames_force_backup_even_if_fingerprint_unchanged(tmp_path,
                                                                       monkeypatch):
    """★★ A11：即使主库 ``(size, mtime_ns)`` **完全没变**，只要归一化**前** ``-wal``
    里有帧，就必须备份。

    为什么单独立这条：``test_real_write_after_checkpoint_still_backs_up`` 依赖
    「写与上次备份恰好落在同一文件时间 tick」这个**时序巧合**（实测 6 轮里 4 轮触发，
    所以它时红时绿）。本用例断言的是**机制**：把 ``_source_fingerprint()`` 打成
    恒定值（等价于「size 与 mtime 都没变」），再让 ``-wal`` 里存在真实帧 ——
    只看指纹的旧实现会跳过，新实现必须备份。

    ⚠️ 风险方向：这里漏掉 = **静默丢备份（不可恢复）**，所以宁可多备份。
    """
    dbp, conn, b = _wal_db(tmp_path)
    try:
        assert b.backup_once("t1") is not None
        frozen = b._read_fp()[0]
        assert frozen, "前置不成立：应当已记录指纹"

        # 真写入 ⇒ -wal 里出现帧（但主库 size 不变）
        conn.execute("UPDATE t SET v='changed' WHERE id=1")
        conn.commit()
        assert b._wal_size() > 0, "前置不成立：写入后 -wal 应当有帧"

        # 把指纹钉死成「与上次备份完全相同」= 模拟同 tick 盲区
        monkeypatch.setattr(b, "_source_fingerprint", lambda: dict(frozen))

        assert b.backup_once("同 tick 盲区") is not None, (
            "归一化前 -wal 有帧却跳过了备份 ⇒ 静默丢备份（A11）")
        assert b.last_status["action"] == "created", b.last_status
    finally:
        conn.close()


def test_noop_checkpoint_leaves_no_frames_so_pending_signal_does_not_misfire(tmp_path):
    """★ A11 的反向护栏：空操作检查点**不留帧** ⇒ 不得因「pending」而误报。

    这是 A5 的跳过语义必须完好的一面：补上 pending 信号**不能**把
    「什么都没改」重新判成「变了」（否则每次点击又白复制一份整库）。
    """
    dbp, conn, b = _wal_db(tmp_path)
    try:
        assert b.backup_once("t1") is not None
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert b._wal_size() == 0, "前置不成立：纯检查点后 -wal 应当归零"
        assert b.backup_once("t2") is None, "空操作检查点不该触发备份"
        assert b.last_status["action"] == "skipped", b.last_status
    finally:
        conn.close()


def test_wal_size_helper_does_not_normalize(tmp_path):
    """``_wal_size()`` 必须是**纯读**：它一旦顺手做了归一化，读到的永远是 0。"""
    dbp, conn, b = _wal_db(tmp_path)
    try:
        assert b.backup_once("t1") is not None
        conn.execute("INSERT INTO t (v) VALUES ('probe')")
        conn.commit()
        before = b._wal_size()
        assert before > 0, "写入后 -wal 应有帧"
        # 纯读：连续读两次结果不变（若它内部归一化了，第二次会变 0）
        assert b._wal_size() == before, "_wal_size() 不该有副作用（不得归一化）"
        # 显式归一化之后才归零
        b._normalize_wal()
        assert b._wal_size() == 0
    finally:
        conn.close()


def test_degraded_path_still_detects_real_writes(tmp_path):
    """★ A5 降级路径：检查点被活跃读者挡住时，指纹退回文件形态口径 ——
    但**仍必须**检出真写入。

    「降级」只允许意味着**多备份**，绝不允许意味着**漏备份**。
    """
    dbp, conn, b = _wal_db(tmp_path)
    holder = sqlite3.connect(str(dbp))
    try:
        assert b.backup_once("t1") is not None
        holder.execute("BEGIN")
        holder.execute("SELECT * FROM t").fetchall()   # 持住读快照，卡住 WAL 归零
        conn.execute("INSERT INTO t (v) VALUES ('under-reader')")
        conn.commit()
        fp1 = b._source_fingerprint()
        if fp1.get("wal") != [0]:
            # 已进入降级路径：此时 -wal 必须按 (size, mtime) 全量记，不能只留 size
            assert isinstance(fp1["wal"], list) and len(fp1["wal"]) == 2, (
                f"降级路径的 -wal 必须记 (size, mtime_ns)，否则可能漏掉真写入：{fp1}")
        conn.execute("INSERT INTO t (v) VALUES ('more')")
        conn.commit()
        fp2 = b._source_fingerprint()
        assert fp1 != fp2, (
            "降级路径下真写入仍必须改变指纹（否则会静默丢备份）\n"
            f"  fp1={fp1}\n  fp2={fp2}")
    finally:
        holder.rollback()
        holder.close()
        conn.close()


# ------------------------------------ 跨「折叠成功 / 失败」两种口径的指纹比较（A12）

def _stub_wal(b, monkeypatch, *, normalized: bool, pre_wal: int):
    """把 ``_normalize_wal`` / ``_wal_size`` 钉死，**确定性地**造出指定口径的指纹。

    ★ 为什么要打桩，而不是「真的让检查点 busy」：能否拿到 busy 取决于平台与时序
    （必须恰好有活跃读者），拿不到就只能把断言跳过 —— 那个分支于是**永远不会被
    覆盖**，正是本项目反复踩到的「绿灯是另一个 bug 遮出来的」。这里直接钉死两个
    输入，让「旧指纹=降级 / 新指纹=归一化」这个**组合**必然出现。
    """
    monkeypatch.setattr(b, "_normalize_wal", lambda: normalized)
    monkeypatch.setattr(b, "_wal_size", lambda: pre_wal)


def test_degraded_then_normalized_fingerprint_still_skips(tmp_path, monkeypatch):
    """★★ A12：旧指纹是**降级**口径、新指纹是**归一化**口径时，仍须认出「同一状态」。

    缺陷现场（2026-10-01 实测）：备份时 ``_normalize_wal()`` 因活跃读者 busy 而失败
    ⇒ 指纹记成 ``{'db': [...], 'wal': [11021032, ...]}``（文件形态）；
    下一次判定时折叠成功 ⇒ 指纹是 ``{'db': [...], 'wal': [0]}``。
    两者**形状不同** ⇒ 旧代码的 ``cur != old`` **恒为不等** ⇒ 判定「源已变化」
    ⇒ 每次「立即备份」都白复制一份整库（实测主库 1.3 GB）。

    这类缺陷之所以危险：它**没有红**。跳过失效只表现为「多写一次盘」，日志/健康
    灯全绿，只有磁盘占用翻倍时才被察觉。
    """
    _, b = _mk(tmp_path)

    _stub_wal(b, monkeypatch, normalized=False, pre_wal=45352)
    old = b._source_fingerprint()
    assert b._is_normalized(old) is False, old
    assert old["pre_wal"] == 45352, old

    _stub_wal(b, monkeypatch, normalized=True, pre_wal=45352)
    cur = b._source_fingerprint()
    assert b._is_normalized(cur) is True, cur
    assert cur["wal"] == [0], cur

    assert b._same_logical_state(old, cur, 45352) is True, (
        "同一批帧（字节数未变）跨折叠形态必须判为同一逻辑状态，"
        f"否则每次点击白复制整库：old={old} cur={cur}")


def test_degraded_then_normalized_but_new_write_still_backs_up(tmp_path, monkeypatch):
    """★ 上一条的**反向护栏**：跨形态**不能**退化成「无条件跳过」。

    降级口径下 ``-wal`` 大小**单调递增**（追加写只变长，唯一的归零动作是我们的
    截断、而那会体现在 ``pre_wal`` 上），所以归一化前的字节数变了 ⇒ 一定有新提交
    ⇒ **必须**备份。这条写错的方向是「静默丢备份」，不可恢复。
    """
    _, b = _mk(tmp_path)
    _stub_wal(b, monkeypatch, normalized=False, pre_wal=45352)
    old = b._source_fingerprint()
    _stub_wal(b, monkeypatch, normalized=True, pre_wal=60000)
    cur = b._source_fingerprint()
    assert b._same_logical_state(old, cur, 60000) is False, (
        f"-wal 从 45352 长到 60000 = 有新提交，必须备份：old={old} cur={cur}")


def test_normalized_then_degraded_is_undecidable_so_backs_up(tmp_path, monkeypatch):
    """★ 旧指纹=归一化、新指纹=降级 ⇒ **无法判定**，保守备份。

    此时折叠失败意味着 ``-wal`` 里可能混着「上次之后的新提交」，而我们没有任何
    与折叠无关的信号能证伪它。风险不对称：多备份只浪费磁盘，漏备份不可恢复。
    """
    _, b = _mk(tmp_path)
    _stub_wal(b, monkeypatch, normalized=True, pre_wal=0)
    old = b._source_fingerprint()
    _stub_wal(b, monkeypatch, normalized=False, pre_wal=45352)
    cur = b._source_fingerprint()
    assert b._same_logical_state(old, cur, 45352) is False


def test_normalized_pair_with_pending_wal_is_a_new_commit(tmp_path, monkeypatch):
    """★ 守住 A11：同口径（都归一化）下，折叠前有帧 = 上次之后确有提交 ⇒ 必须备份。

    这条防的是「为了让跨形态比对能过，顺手把 A11 一起阉掉」—— 那会直接退回静默丢备份。
    """
    _, b = _mk(tmp_path)
    _stub_wal(b, monkeypatch, normalized=True, pre_wal=0)
    old = b._source_fingerprint()
    cur = b._source_fingerprint()
    assert b._same_logical_state(old, cur, 0) is True, "无任何变化必须跳过"
    assert b._same_logical_state(old, cur, 8192) is False, (
        "折叠前 -wal 有帧 = 有新提交 ⇒ 必须备份（A11）")


def test_same_shape_comparison_ignores_the_pending_marker(tmp_path, monkeypatch):
    """★★ 同形态比较**不能**把 ``pre_wal`` 计进去（A12 的自身回退护栏）。

    这是修 A12 时**自己踩的坑**：把 ``pre_wal`` 一起塞进指纹 dict 之后，同形态分支的
    ``cur == old`` 会去比「折叠前 ``-wal`` 有没有帧」—— 而这两次本来就可以不同
    （备份那一刻 ``-wal`` 里有帧、判定时已经空，逻辑数据一字未改）⇒ **每次判断都算
    「变了」**，跳过彻底失效，每次点击白复制一份整库。比原缺陷更糟。

    ⇒ ``pre_wal`` / ``norm`` 是**标记**不是**形态**，只允许跨形态比较时使用。
    """
    _, b = _mk(tmp_path)
    _stub_wal(b, monkeypatch, normalized=True, pre_wal=45352)
    old = b._source_fingerprint()      # 备份时：折叠前 -wal 里有帧
    _stub_wal(b, monkeypatch, normalized=True, pre_wal=0)
    cur = b._source_fingerprint()      # 判定时：-wal 已空（同一逻辑状态）
    assert old["pre_wal"] == 45352 and cur["pre_wal"] == 0, "前置不成立：标记应当不同"
    assert b._shape(old) == b._shape(cur), "前置不成立：文件形态应当相同"
    assert b._same_logical_state(old, cur, 0) is True, (
        "pre_wal 只是标记、不是文件形态，不得参与同形态比较（否则跳过永远失效）")


def test_is_normalized_understands_legacy_fingerprints():
    """★ 旧指纹（``norm`` 键引入之前落盘的）只能靠 ``wal == [0]`` 的形状判。

    那一形状**只可能**由归一化成功产生（代码里唯一写 ``[0]`` 的地方）⇒ 判 True。
    兼容它，升级后才不会把老用户的历史指纹一律判成「降级」而白白多备份一次。
    """
    assert DBBackup._is_normalized({"db": [4096, 1], "wal": [0]}) is True
    assert DBBackup._is_normalized({"db": [4096, 1], "wal": [45352, 2]}) is False
    assert DBBackup._is_normalized({}) is False
    assert DBBackup._is_normalized(None) is False
    assert DBBackup._is_normalized({"wal": None}) is False


def test_skip_chain_survives_a_real_checkpoint(tmp_path):
    """★★ 端到端（**不打桩**）：一次真检查点之后的重复备份仍须跳过。

    与上面打桩用例互补：打桩保证**分支**被覆盖，这条保证**真实文件形态**下也成立。
    折叠成功与否取决于平台是否给了 busy 检查点，两种走向都必须满足「不重复备份」。
    """
    dbp, conn, b = _wal_db(tmp_path)
    holder = sqlite3.connect(str(dbp))
    try:
        # 先提高「降级口径」的出现概率：用活跃读者挡住 TRUNCATE 检查点
        holder.execute("BEGIN")
        holder.execute("SELECT count(*) FROM t").fetchall()
        assert b.backup_once("t1") is not None
        fp1, name1 = b._read_fp()
        assert name1, fp1
        holder.rollback()
        holder.close()
        # 释放读者后折叠大概率成功 ⇒ 形成跨形态比较
        assert b.backup_once("t2") is None, (
            "无新写入时，跨折叠形态也必须跳过；"
            f"fp1={fp1}\n  cur={b._source_fingerprint()}")
        assert b.last_status["action"] == "skipped", b.last_status
        assert len(_backup_files(tmp_path)) == 1
        if not DBBackup._is_normalized(fp1):
            # 真走到了跨形态分支：显式记下来，否则本用例会悄悄退回成 A5 的同形态版本
            assert DBBackup._is_normalized(b._source_fingerprint()), (
                "降级之后的判定应当已归一化，本用例没有覆盖跨形态分支")
    finally:
        holder.close()
        conn.close()


# --------------------------------------------------------------- 保留策略

def test_count_cap_applies_without_budget(tmp_path):
    """``max_total_mb=0`` = 只按份数（旧行为仍可用）。"""
    dbp, b = _mk(tmp_path, keep=3, max_total_mb=0.0)
    for i in range(5):
        _grow(dbp, 1000 + i * 100)
        b.backup_once(f"t{i}")
    assert len(_backup_files(tmp_path)) == 3


def test_budget_prunes_oldest(tmp_path):
    """★ 体积上限必须真的删东西 —— 这是本缺陷的核心。"""
    dbp, b = _mk(tmp_path, keep=10, max_total_mb=0.002)  # 预算 ≈ 2097 字节
    for i, sz in enumerate([1000, 2000, 3000]):
        _grow(dbp, sz)
        b.backup_once(f"t{i}")
    # 3000+2000=5000 > 2097，但 min_keep=2 ⇒ 保住最新两份，删掉 1000 那份
    assert _sizes(tmp_path) == [2000, 3000], _sizes(tmp_path)
    assert b.last_status["action"] == "created"


def test_prune_orders_by_name_not_mtime(tmp_path):
    """★ 剪枝的「新 → 旧」必须按**文件名**，不能按 mtime —— 否则会删错备份。

    ## 实测（2026-09-20）：这条不是测试 flake，是会**删掉最新还原点**的真缺陷

    - 回退复制路径用 ``shutil.copy2``，它把**主库的 mtime 复制给备份**；
    - Windows/NTFS 下短间隔连续写同一文件，mtime 可能**完全相同**
      （实测三个备份 ``mtime_ns`` 都是 ``1789918599694760700``）。

    ``sort`` 在键相等时是稳定排序 ⇒ 顺序退化为 ``glob`` 的目录顺序 ⇒
    「哪份最旧」变成随机。而 ``_plan`` 正是按这个顺序决定删谁：
    实测同一脚本里，一轮删掉了 2000（该删 1000），另一轮**删掉了最新那份 3000**。

    ## 怎么让这条**可证伪**
    把 mtime 设成**反向**（最新备份的 mtime 最旧）：按 mtime 排必然把 3000 判成
    最旧而删掉 ⇒ ``[1000, 2000]`` ≠ ``[2000, 3000]``，修复前必失败。
    """
    # 先不设预算（创建期不剪枝），好让三份都在场上
    dbp, b = _mk(tmp_path, keep=10, max_total_mb=0.0)
    for i, sz in enumerate([1000, 2000, 3000]):
        _grow(dbp, sz)
        b.backup_once(f"t{i}")
    files = _backup_files(tmp_path)  # 名称升序 ⇒ 1000, 2000, 3000
    assert [f.stat().st_size for f in files] == [1000, 2000, 3000], files

    base = 1_700_000_000
    for i, f in enumerate(files):
        os.utime(f, (base - i * 100, base - i * 100))  # 越新 ⇒ mtime 越旧

    b.max_total_bytes = 2097  # 现在才施加预算，触发一次剪枝
    b._prune()
    assert _sizes(tmp_path) == [2000, 3000], _sizes(tmp_path)


def test_backup_mtime_is_creation_time_not_source(tmp_path):
    """备份文件的 mtime 必须是「备份生成时刻」，不能是主库 mtime。

    ``shutil.copy2`` 会连元数据一起复制 ⇒ 备份文件的 mtime 变成**主库最后写入时间**，
    于是 ``list_backups()`` / ``stats()`` 展示给用户的「备份时间」是错的。
    这里把主库 mtime 设成一个古老值：修复前备份会继承它。
    """
    dbp, b = _mk(tmp_path)
    ancient = 1_600_000_000
    os.utime(dbp, (ancient, ancient))

    path = b.backup_once("t1")
    assert path is not None
    assert Path(path).stat().st_mtime > ancient, "备份 mtime 继承了主库（copy2 的元数据复制）"


def test_min_keep_is_a_floor_over_budget(tmp_path):
    """★ 预算比「一份备份」还小 ⇒ 也必须留下 ``min_keep`` 份。

    否则「1.14GB 主库 + 1GB 预算」会把备份删光 —— 等于静默关掉保护，
    比不设上限更危险。
    """
    dbp, b = _mk(tmp_path, keep=10, max_total_mb=0.0001, min_keep=2)  # 预算 ≈ 104 字节
    for i, sz in enumerate([1000, 2000, 3000, 4000]):
        _grow(dbp, sz)
        b.backup_once(f"t{i}")
    assert len(_backup_files(tmp_path)) == 2, _sizes(tmp_path)


def test_min_keep_clamped_to_keep(tmp_path):
    """``min_keep`` 不许超过 ``keep``（否则「份数上限」形同虚设）。"""
    _, b = _mk(tmp_path, keep=2, min_keep=9)
    assert b.min_keep == 2


def test_zero_or_negative_budget_means_count_only(tmp_path):
    for v in (0.0, -1.0):
        _, b = _mk(tmp_path, max_total_mb=v)
        assert b.max_total_bytes == 0, v


def test_prune_now_is_idempotent_and_honest(tmp_path):
    """★ 已符合策略时必须如实说「没删东西」，不能假装清理成功。"""
    dbp, b = _mk(tmp_path, keep=10, max_total_mb=0.002)
    for i, sz in enumerate([1000, 2000, 3000]):
        _grow(dbp, sz)
        b.backup_once(f"t{i}")
    first = b.prune_now()
    assert first["removed_count"] == 0 and first["freed_bytes"] == 0
    assert "至少保留" in first["note"]


# --------------------------------------------------------------- 占用快照

def test_stats_reports_occupancy_and_reclaimable(tmp_path):
    """★ 占用必须可读 —— 这是「11GB 不可见」的直接修复。"""
    dbp, b = _mk(tmp_path, keep=2, max_total_mb=0.0)
    for i in range(4):
        _grow(dbp, 1000 + i * 100)
        b.backup_once(f"t{i}")
    st = b.stats()
    assert st["count"] == 2
    assert st["total_bytes"] == sum(p.stat().st_size for p in _backup_files(tmp_path))
    assert st["total_size"].endswith("KB") or st["total_size"].endswith("B")
    # 备份时会就地裁剪，故「份数已合规」是常态（不是 bug）
    assert st["over_count"] is False
    assert st["retain_count"] == 2
    assert st["newest"] and st["oldest"]
    assert st["dir"].endswith("backups")
    assert isinstance(st["strays"], list)
    assert st["last"]["action"] == "created"


def test_stats_marks_reclaimable_when_budget_tightened(tmp_path):
    """★ 用户调小体积上限后，界面必须能算出「现在能回收多少」。

    这才是 ``/config/paths/db-backups/prune`` 的真实用途：策略收紧后立刻回收，
    而不是干等下一次备份触发裁剪（下一次可能是几小时以后）。
    """
    dbp, b = _mk(tmp_path, keep=10, max_total_mb=0.0)  # 先不限体积
    for i, sz in enumerate([1000, 2000, 3000]):
        _grow(dbp, sz)
        b.backup_once(f"t{i}")
    assert len(_backup_files(tmp_path)) == 3

    tight = DBBackup(dbp, keep=10, max_total_mb=0.002, min_keep=2)
    st = tight.stats()
    assert st["over_budget"] is True
    assert st["reclaimable_bytes"] >= 1000, st
    res = tight.prune_now()
    assert res["freed_bytes"] >= 1000, res
    assert res["removed_count"] == 1
    assert len(_backup_files(tmp_path)) == 2


def test_over_budget_with_nothing_reclaimable_is_honest(tmp_path):
    """★ 「超预算但无可回收」是**合法**状态，必须如实上报。

    预算比单份备份还小时，``min_keep`` 地板会让占用停在超限状态。界面若把它渲染成
    「有空间可回收」就是假承诺；渲染成「一切正常」又掩盖了磁盘正在被吃。
    正确做法是把这个状态原样报出来（超限 + 可回收 0 + 保留了几份）。
    """
    dbp, b = _mk(tmp_path, keep=10, max_total_mb=0.0001, min_keep=2)  # 预算 < 单份
    for i, sz in enumerate([1000, 2000, 3000]):
        _grow(dbp, sz)
        b.backup_once(f"t{i}")
    st = b.stats()
    assert st["over_budget"] is True
    assert st["reclaimable_bytes"] == 0
    assert st["retain_count"] == 2
    assert st["min_keep"] == 2


# --------------------------------------------------------------- 未纳管的库副本

def test_strays_reported_but_never_deleted(tmp_path):
    """★ 主库目录里的手工副本只上报、绝不删。

    实测真实数据目录里有一个 ``app.db.bak_bardate``（603 MB），既不被备份策略
    统计也不被清理，界面上同样一处都不显示 —— 必须让它可见；但删不删由用户定
    （可能在别处仍有用途）。
    """
    dbp, b = _mk(tmp_path)
    stray = tmp_path / "app.db.bak_bardate"
    stray.write_bytes(b"z" * 500)
    b.backup_once("t1")
    st = b.stats()
    names = [x["name"] for x in st["strays"]]
    assert names == ["app.db.bak_bardate"], names
    assert st["strays"][0]["size"] == 500
    b.prune_now()
    assert stray.exists(), "未纳管的库副本绝不能被清理动作删掉"


def test_managed_and_unrelated_files_are_not_strays(tmp_path):
    """主库自身（app.db/-wal/-shm）与非同名文件都不算「未纳管副本」。"""
    dbp, b = _mk(tmp_path)
    (tmp_path / "app.db-wal").write_bytes(b"w")
    (tmp_path / "app.db-shm").write_bytes(b"s")
    (tmp_path / "stock_names.json").write_bytes(b"{}")
    assert stray_db_files(dbp) == []


def test_stray_scan_survives_missing_dir(tmp_path):
    assert stray_db_files(tmp_path / "no" / "such" / "app.db") == []


# --------------------------------------------------------------- 人类可读口径

def test_human_size():
    assert human_size(0) == "0B"
    assert human_size(1536) == "1.5KB"
    assert human_size(1146806272) == "1.1GB"
    assert human_size("oops") == "?"


# --------------------------------------------------------------- 生命周期

def test_stop_takes_shutdown_backup(tmp_path):
    """停机时补一次备份（无变化则如实跳过）。"""
    dbp, b = _mk(tmp_path)
    asyncio.run(b.start())
    asyncio.run(b.stop())
    assert b.last_status["action"] in ("created", "skipped")
    assert len(_backup_files(tmp_path)) == 1


# --------------------------------------------------------------- 路由契约

def _get(client, path):
    r = client.get("/api/v1" + path)
    assert r.status_code == 200, r.text[:200]
    return r.json()


def _post(client, path, payload=None):
    r = client.post("/api/v1" + path, json=payload or {})
    assert r.status_code == 200, r.text[:200]
    return r.json()


@pytest.fixture
def _isolated_backup(tmp_path, monkeypatch):
    """把 `/config/paths` 看到的备份器指向临时目录（不碰真实数据目录）。"""
    from app.routes import config as cfg_routes

    dbp = tmp_path / "app.db"
    dbp.write_bytes(b"x" * 1000)
    inst = DBBackup(dbp, keep=10, max_total_mb=0.002, min_keep=2)
    monkeypatch.setattr(cfg_routes, "_backup_view", lambda ctx: inst)
    return inst, tmp_path


@pytest.fixture
def _live_db_backup(tmp_path, monkeypatch, app_client):
    """备份器盯**应用正在写的那个主库** —— 但把「主库」整体换到 tmp 里的小库。

    ★ 为什么必须让两边盯**同一个库**：端点在备份成功后要写一条**审计行**，而审计行
    写的是 ``ctx.db`` 那个库。若备份器盯的是另一个库（``_isolated_backup`` 那种做法），
    审计行就影响不到指纹 ⇒ 测试**假通过**，而真实环境里连点两次「立即备份」会重复
    复制整库（实测踩过）。

    ★★ 为什么不能直接用 ``settings.db_path``（2026-10-01，A14）：本机主库实测
    **1.4 GB**，而备份是**整库全量复制**，两个用例各复制 2 次 ⇒ 本文件**一轮**就往
    pytest 临时目录写 **约 5.6 GB**。实测它把 C: 顶到 100%，随后同文件里**毫不相干**
    的 4 个 WAL 用例集体以 ``[WinError 112] 磁盘空间不足`` 变红 —— 又一次
    「磁盘满让测试假失败」，失败原因还与被测逻辑毫无关系（排查时会一路怀疑到指纹
    逻辑上去）。

    ⇒ 把 ``ctx.db`` 与备份器**一起**换到 tmp 里的小库：保真度不变（仍是同一个库、
      审计行仍会写进被备份的那个库），占用从 GB 级降到 KB 级。
    """
    from app.routes import config as cfg_routes
    from core.context import get_ctx
    from core.db import DB

    live_path = tmp_path / "live.db"
    live_db = DB(live_path)          # 真 SQLite，跑一遍迁移，schema 与生产同源
    ctx = get_ctx()
    monkeypatch.setattr(ctx, "db", live_db)   # 应用「正在写的库」= 被备份的库
    inst = DBBackup(live_path, keep=10, max_total_mb=0.0, min_keep=2,
                    backups_dir=tmp_path / "backups", db=live_db)
    monkeypatch.setattr(cfg_routes, "_backup_view", lambda ctx: inst)
    try:
        yield inst, tmp_path
    finally:
        try:
            live_db.close()
        except Exception:  # noqa: BLE001  关不掉只是临时目录残留，不该影响用例结论
            pass


def test_paths_db_exposes_backups_block(app_client):
    """★ 备份占用必须随主库一起出现在 `/config/paths`。

    此前 ``list_backups()`` 零调用方 ⇒ 11GB 占用在界面上完全不可见。
    """
    data = _get(app_client, "/config/paths")["data"]
    bk = data["db"].get("backups")
    assert isinstance(bk, dict), data["db"]
    for field in ("dir", "count", "total_bytes", "total_size", "keep", "min_keep",
                  "max_total_bytes", "over_budget", "reclaimable_bytes",
                  "retain_count", "strays", "last"):
        assert field in bk, f"db.backups 缺字段 {field}"
    assert isinstance(bk["count"], int)
    assert isinstance(bk["strays"], list)


def test_prune_endpoint_returns_snapshot_and_keeps_strays(app_client, _isolated_backup):
    inst, tmp = _isolated_backup
    for i, sz in enumerate([1000, 2000, 3000]):
        (tmp / "app.db").write_bytes(b"y" * sz)
        inst.backup_once(f"t{i}")
    stray = tmp / "app.db.bak_bardate"
    stray.write_bytes(b"z" * 500)

    res = _post(app_client, "/config/paths/db-backups/prune")
    assert res["code"] == 0, res
    assert "removed" in res["data"] and "freed_bytes" in res["data"]
    # 清理后必须**回带一份最新快照**（界面一次请求就能刷新占用）
    assert res["data"]["db"]["backups"]["count"] == len(_backup_files(tmp))
    assert stray.exists(), "清理端点绝不能删未纳管的库副本"


def test_run_endpoint_distinguishes_skipped_from_created(app_client, _isolated_backup):
    """★ 「跳过」与「已备份」必须是**不同**的 action，界面才能正确归因。"""
    res1 = _post(app_client, "/config/paths/db-backups/run")
    assert res1["data"]["action"] == "created", res1["data"]
    res2 = _post(app_client, "/config/paths/db-backups/run")
    assert res2["data"]["action"] == "skipped", res2["data"]
    assert "未发生变化" in res2["data"]["message"]


def test_run_endpoint_skip_chain_survives_its_own_audit_row(app_client, _live_db_backup):
    """★★ 端点的审计行**不能**破坏跳过链（审计写在采指纹之前）。

    这是真实环境里唯一能暴露该缺陷的写法：备份器必须盯**应用正在写的那个主库**，
    否则「审计行影响不到指纹」⇒ 测试假通过。
    """
    r1 = _post(app_client, "/config/paths/db-backups/run")
    assert r1["data"]["action"] == "created", r1["data"]
    inst, _ = _live_db_backup
    recorded_fp, recorded_name = inst._read_fp()   # 备份那一刻记下的指纹
    r2 = _post(app_client, "/config/paths/db-backups/run")
    # ★ 诊断增强（2026-10-01 修订）：本用例曾**偶发**为红（同一次改动下 3 跑 1 红）。
    #   当时的注释把原因归给「纯 WAL 检查点也会改变 (size, mtime_ns)」，并建议
    #   「把这种差异当成良性」—— **写反了**。真因是：备份那次折叠**失败**（有读者
    #   持快照 ⇒ TRUNCATE 拿不到独占），指纹记成了**文件形态**；判定那次折叠成功，
    #   指纹是**归一化形态**。两种形态形状不同，旧代码的 ``cur != old`` 恒为不等
    #   ⇒ 误判「源已变化」。纯检查点造成的形态差异由 ``_same_logical_state`` 处理，
    #   **不再**使跳过失效；若这里仍看到两份指纹不等，说明确实发生了新写入。
    diag = (f"记下的指纹(备份时) = {recorded_fp}"
            f"  [归一化={DBBackup._is_normalized(recorded_fp)}]"
            f"  备份名={recorded_name}\n"
            f"  当前指纹(判定时)   = {inst._source_fingerprint()}\n"
            "  两者不等即说明两次调用之间主库 / -wal 被**真写入**过"
            "（纯检查点的形态差异已由 _same_logical_state 兜住，不再算变化）")
    assert r2["data"]["action"] == "skipped", (
        "连点两次「立即备份」必须跳过第二次 —— 否则每次点击白复制一份整库；"
        f"实测得到 {r2['data']['action']}：{r2['data'].get('message')}\n  {diag}")


def test_run_endpoint_skipped_call_writes_no_audit_row(app_client, _live_db_backup):
    """跳过是**无副作用**的：不写审计行（写了反而让下一次跳过失效）。

    失败则必须留痕 —— 不能因为「没备份成」就静默。
    """
    inst, tmp = _live_db_backup

    def _rows() -> list:
        env = app_client.get(
            "/api/v1/audit?action=paths.run_db_backup&limit=50").json()
        assert env["code"] == 0, env
        data = env["data"]
        return data if isinstance(data, list) else list(data.get("items") or [])

    _post(app_client, "/config/paths/db-backups/run")          # created，写 1 行
    before = len(_rows())
    assert before >= 1, "成功备份必须留审计"
    _post(app_client, "/config/paths/db-backups/run")          # skipped，不写
    assert len(_rows()) == before, f"跳过不该产生审计行：{before} -> {len(_rows())}"


def test_paths_backup_block_reports_strays_from_real_dir(app_client, _isolated_backup):
    _, tmp = _isolated_backup
    (tmp / "app.db.bak_bardate").write_bytes(b"z" * 700)
    bk = _get(app_client, "/config/paths")["data"]["db"]["backups"]
    assert [x["name"] for x in bk["strays"]] == ["app.db.bak_bardate"]
    assert bk["strays"][0]["size_str"] == "700B"


def test_sidecar_written_is_valid_json(tmp_path):
    dbp, b = _mk(tmp_path)
    b.backup_once("t1")
    raw = json.loads((tmp_path / "backups" / ".last_backup.json").read_text(encoding="utf-8"))
    assert raw["backup"].startswith("app.")
    assert isinstance(raw["fingerprint"], dict)
    assert raw["fingerprint"]["db"][0] == dbp.stat().st_size


# --------------------------------------------------------------- 装配位置护栏

_BACKEND = Path(__file__).resolve().parent.parent


def _code_only(src: str) -> str:
    """只留**可执行代码**：剥掉注释与三引号字符串。

    护栏必须扫代码、不能扫说明文字 —— 否则「注释里引用被禁写法」会让护栏自己
    误报（本文件就踩过：新交代的 why 里逐字写了旧写法）。
    """
    text = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    return re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", text)


def test_db_backup_is_assembled_in_the_last_startup_phase():
    """★★ 启动备份必须装配在**最后一个**启动阶段，不能放回 watchdogs。

    启动备份会记录「源库指纹」用于「主库没变化就跳过」。指纹必须在**所有写库阶段
    都跑完、库已静止**之后才采 —— 否则记下来当场就过期，「客户端反复启停不重复
    整库复制」永远不成立。

    实测（2026-09-20）：放在 `phase_watchdogs`（启动阶段第 4/6 个）时，
    它后面还有 `replay` / `misc` 在写库，指纹当场过期 ⇒ 每次启动都白复制一份
    1GB+ 的主库；迁到最后一个阶段 `phase_misc` 后，第二次启动实测**不再复制**。

    这是一条「放错位置就静默失效」的约束（没有任何报错，只是白干活），
    所以用源码扫描把它钉住。
    """
    misc = (_BACKEND / "app" / "bootstrap" / "phase_misc.py").read_text(encoding="utf-8")
    watch = (_BACKEND / "app" / "bootstrap" / "phase_watchdogs.py").read_text(encoding="utf-8")
    assert "DBBackup(" in misc, "启动备份应装配在 phase_misc（最后一个启动阶段）"
    # ★ 2026-09-28（R31）：调用形态由 `backup_once` 改为 `spawn_startup_backup` ——
    #   必须**后台线程、不等结果**。见下方 test_startup_backup_never_blocks_readiness。
    assert "spawn_startup_backup" in misc, "启动备份调用应在 phase_misc"
    assert "DBBackup(" not in watch, (
        "phase_watchdogs 不是最后一个启动阶段：它之后还有 replay / misc 写库，"
        "在此采指纹会当场过期 ⇒ 每次启动都白复制一份整库")
    assert "state.db_backup" in misc, "db_backup 必须挂到 AppContext 供停机与占用展示读取"


def test_startup_backup_never_blocks_readiness():
    """★★ 回归护栏（2026-09-28 R31 实测「应用永远起不来」）：

    启动阶段**绝不能 await 备份结果**。旧写法
    ``await asyncio.to_thread(db_backup.backup_once, "startup")`` 虽然不占用事件循环，
    但 lifespan 仍在等它返回；而 CPython 的 ``Connection.backup()`` 在源库被别的连接
    持锁时按 ``SQLITE_BUSY`` 每 0.25 s **无限重试、不回调 progress** —— 调用方拿不回
    控制权。于是「一次卡住的备份」= lifespan 永不完成 = 就绪广播与前端首屏永远等不到。

    实测现场（py-spy dump 真实进程）：7 个线程里 6 个排队在 ``_rw.write()``，
    其中就有 lifespan 主协程（经 jobs reaper 的 ``upsert``）；
    ``TestClient.__enter__`` 永不返回，逐文件回归**卡在同一个文件上 > 10 分钟**。

    修法就是「丢 daemon 线程 + 不等」，所以这里静态钉住「不许再 await 它」。
    """
    misc = (_BACKEND / "app" / "bootstrap" / "phase_misc.py").read_text(encoding="utf-8")
    code = _code_only(misc)
    assert "await asyncio.to_thread(db_backup.backup_once" not in code, (
        "启动阶段又 await 备份了：备份卡住会让应用永远起不来（TECH_DEBT TD-25）")
    assert "spawn_startup_backup" in code, (
        "启动备份必须走 spawn_startup_backup（后台 daemon 线程、不等结果）")
    svc = _code_only((_BACKEND / "gateway" / "db_backup.py").read_text(encoding="utf-8"))
    assert "def spawn_startup_backup" in svc and "daemon=True" in svc, (
        "spawn_startup_backup 必须是 daemon 线程，否则进程退出会被它拖住")
    assert "asyncio.to_thread(self.backup_once" not in svc, (
        "备份不得走 asyncio.to_thread：默认 ThreadPoolExecutor 的线程是**非 daemon** "
        "的，concurrent.futures 在解释器退出时会 atexit join 它们 ⇒ 备份卡死时"
        "`asyncio.wait_for` 能超时返回、但**进程仍然退不出去**（实测被钉死到强杀）。"
        "必须用自建 daemon 线程（_run_backup_bounded）")


def test_backup_to_never_holds_the_process_write_lock():
    """★★ 回归护栏（2026-09-28 R31 冻机根因）：``DB.backup_to`` 不得把
    ``Connection.backup()`` 包在 ``_rw.write()`` 里。

    为什么必须**静态**钉住：这条耦合在动态测试里极难稳定复现（要恰好撞上「备份卡住
    + 其它线程想写库」），但代价是**全进程冻死**。SQLite/CPython 的
    ``Connection.backup()`` 在源库被任何其它连接持锁时按 ``SQLITE_BUSY`` 每 0.25 s
    无限重试且**不回调 progress** —— 调用方拿不回控制权，于是「持锁 + 无界等待」
    就等于「整个应用停摆」。一致性由 SQLite 备份 API 自身保证，进程级锁并非必需。
    """
    src = (_BACKEND / "core" / "db.py").read_text(encoding="utf-8")
    assert "def backup_to(" in src, "core/db.py 里找不到 backup_to"
    body = _code_only(src.split("def backup_to(", 1)[1].split("\n    def ", 1)[0])
    assert "_rw.write()" not in body, (
        "backup_to 又持有了进程级写锁 ⇒ 备份卡住会冻死全进程（TECH_DEBT TD-25）")
    assert "progress=" in body and "deadline" in body, (
        "backup_to 必须有墙钟上限（分块 pages + progress 回调）")
    assert "shutil.disk_usage" in body, (
        "backup_to 必须有磁盘可用空间预检：实测 585 MB 主库 × keep=10 把 98% 满的盘"
        "又写进去 4.4 GB")


def test_last_phase_really_is_last():
    """上一条护栏的前提：`phase_misc` 确实是启动阶段列表里的最后一个。

    若有人往后追加阶段，这条会失败 —— 提醒把备份再往后挪（否则指纹又会过期）。
    """
    src = (_BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    m = re.search(r"phases\s*=\s*\[(.*?)\]", src, re.S)
    assert m, "main.py 里找不到 phases 列表"
    names = re.findall(r'\(\s*"([a-z_]+)"\s*,', m.group(1))
    assert names, m.group(1)[:200]
    assert names[-1] == "misc", f"启动阶段最后一个不再是 misc：{names}"


def test_occupied_capability_is_reachable_from_routes():
    """★ 占用类能力必须能从路由取到。

    此前 DBBackup 只挂在 `app.state._db_backup`（AppContext 之外的私有属性），
    路由**没有任何途径**拿到它 ⇒ 11GB 备份占用在界面上完全不可见。
    """
    ctx = (_BACKEND / "core" / "context.py").read_text(encoding="utf-8")
    assert "db_backup: Any = None" in ctx, "AppContext 必须有 db_backup 槽位"
    route = (_BACKEND / "app" / "routes" / "config.py").read_text(encoding="utf-8")
    assert '"backups": _backup_view(ctx).stats()' in route, (
        "/config/paths 的 db 条目必须带上备份占用快照")
    assert "db-backups/prune" in route and "db-backups/run" in route
