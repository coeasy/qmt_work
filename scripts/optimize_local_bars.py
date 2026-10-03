#!/usr/bin/env python3
"""主库瘦身工具：删除 `local_bars` 的冗余二级索引。

背景（实测，见 docs/DATASTORE_SIZE_AND_SPLIT_ANALYSIS.md）
-----------------------------------------------------------
``app.db`` 1.35 GB 中，``local_bars`` 表 + 2 个二级索引共占 87.4%：

    表 local_bars                          798.80 MB
    idx_local_bars_provenance (provider_id,batch_id,dt)  246.34 MB
    idx_local_bars_lookup     (code,period,adjust,dt)    137.36 MB

其中 ``idx_local_bars_lookup`` 的列是**主键的严格前缀**
（PRIMARY KEY (code, period, adjust, dt, provider_id)）。
SQLite 主键本身就是一个包含全部列数据的 B-tree 索引，
优化器在有主键索引时不会选择这个二级索引 —— 它只白占 137 MB，
并且每次写入还要多维护一份。删除后**零性能损失**。

``idx_local_bars_provenance`` 另有 246 MB，但确实服务 2 处批处理查询
（``datasource/snapshots.py`` 的快照构建与覆盖率统计），默认**保留**。

用法
----
    python scripts/optimize_local_bars.py                # dry-run，只报告
    python scripts/optimize_local_bars.py --apply        # 先备份再执行
    python scripts/optimize_local_bars.py --apply --also-provenance
    python scripts/optimize_local_bars.py --restore      # 从备份还原

安全保证
--------
- 只执行 ``DROP INDEX IF EXISTS``，**不删除任何数据行**。
- ``--apply`` 之前用 SQLite 在线备份 API（``Connection.backup``）
  生成一份完整副本，失败则中止，不碰主库。
- ``--restore`` 把备份覆盖回主库路径。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 主库路径：跟随 core.config 的 db_path，与后端运行时同源。
DEFAULT_DB = ROOT / "backend" / "data" / "app.db"

#: 冗余索引：列为主键的严格前缀，删除后零性能损失。
REDUNDANT_INDEX = "idx_local_bars_lookup"

#: 有真实用途但可牺牲的索引：仅服务 datasource/snapshots.py 的 2 处批处理查询。
OPTIONAL_INDEX = "idx_local_bars_provenance"

PK_COLUMNS = ("code", "period", "adjust", "dt", "provider_id")


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _table_bytes(conn: sqlite3.Connection, obj: str) -> int | None:
    """用 dbstat 虚拟表统计对象体积（字节）；未编译 dbstat 时返回 None。"""
    try:
        row = conn.execute(
            "SELECT SUM(pgsize) FROM dbstat WHERE name=?", (obj,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else 0


def _index_columns(conn: sqlite3.Connection, name: str) -> list[str]:
    # PRAGMA 不支持参数绑定，索引名来自 sqlite_master（可信），仅做标识符白名单校验。
    assert name.replace("_", "").replace('"', "").isalnum(), f"非索引名：{name!r}"
    info = conn.execute(f'PRAGMA index_info("{name}")').fetchall()
    return [str(r[2]) for r in info]


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    pk = [r for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if r[5] > 0]
    pk.sort(key=lambda r: r[5])
    return [str(r[1]) for r in pk]


def profile(db: Path) -> dict:
    """采集瘦身所需的画像数据（只读）。"""
    out: dict = {
        "db": str(db),
        "file_mb": round(db.stat().st_size / 1048576, 2) if db.exists() else None,
        "freelist_pages": None,
        "pk_columns": None,
        "indexes": {},
    }
    if not db.exists():
        return out
    conn = _open_readonly(db)
    try:
        out["freelist_pages"] = conn.execute("PRAGMA freelist_count").fetchone()[0]
        out["pk_columns"] = _pk_columns(conn, "local_bars")
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='local_bars' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for name in names:
            cols = _index_columns(conn, name)
            out["indexes"][name] = {
                "columns": cols,
                "size_mb": round(_table_bytes(conn, name) / 1048576, 2)
                if _table_bytes(conn, name) is not None else None,
                "is_pk_prefix": bool(cols) and out["pk_columns"][:len(cols)] == cols,
            }
    finally:
        conn.close()
    return out


def _backup(dest_dir: Path, db: Path) -> Path:
    """用 SQLite 在线备份 API 生成完整副本（不锁库、支持 WAL）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"app.db.before-optimize-{stamp}"
    src, dst = sqlite3.connect(f"file:{db}?mode=ro", uri=True), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def apply_optimize(db: Path, also_provenance: bool, backup: bool) -> int:
    if not db.exists():
        print(f"✗ 主库不存在：{db}", file=sys.stderr)
        return 2

    targets = [REDUNDANT_INDEX] + ([OPTIONAL_INDEX] if also_provenance else [])
    # 白名单校验：DROP 语句无法参数绑定，只允许删除我们明确认识的索引名。
    assert set(targets) <= {REDUNDANT_INDEX, OPTIONAL_INDEX}, targets
    before = db.stat().st_size

    if backup:
        print(f"  备份中（SQLite 在线备份，不锁库）…")
        t0 = time.time()
        bpath = _backup(db.parent / "backups", db)
        print(f"  备份完成 {bpath.name} "
              f"({bpath.stat().st_size / 1048576:.1f} MB, {time.time() - t0:.1f}s)")
        print(f"  回滚：python {Path(__file__).name} --restore --backup {bpath}")

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        for name in targets:
            exists = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='index' AND name=?", (name,)
            ).fetchone()[0]
            if not exists:
                print(f"  · {name} 不存在，跳过")
                continue
            t0 = time.time()
            conn.execute("DROP INDEX IF EXISTS \"%s\"" % name)
            conn.commit()
            print(f"  ✓ DROP {name}  ({time.time() - t0:.1f}s)")
    finally:
        conn.close()

    after = db.stat().st_size
    freed = (before - after) / 1048576
    print()
    print(f"  文件：{before / 1048576:.1f} MB → {after / 1048576:.1f} MB")
    print(f"  实际回收 {freed:.1f} MB")
    print()
    print("  注意：DROP INDEX 释放的页进入 freelist，文件不会立刻变小。")
    print("  这些空间会被后续写入自动复用；若要文件物理变小需 VACUUM")
    print("  （会锁库并整库重写，主库 1.35 GB 建议只在停机窗口做）。")
    return 0


def restore(db: Path, backup: Path) -> int:
    if not backup.exists():
        print(f"✗ 备份不存在：{backup}", file=sys.stderr)
        return 2
    # 关键：先把现有主库挪走而不是直接覆盖，避免半截写入留下损坏文件。
    if db.exists():
        tmp = db.with_suffix(".db.optimize-bak")
        if tmp.exists():
            tmp.unlink()
        shutil.move(str(db), str(tmp))
        print(f"  原主库已移开 → {tmp.name}（如需回退再回退用此文件）")
    shutil.copy2(str(backup), str(db))
    print(f"  ✓ 已从 {backup.name} 还原主库（{db.stat().st_size / 1048576:.1f} MB）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="删除 local_bars 的冗余二级索引以瘦身主库")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="主库路径")
    ap.add_argument("--apply", action="store_true", help="真正执行 DROP（默认只报告）")
    ap.add_argument("--also-provenance", action="store_true",
                    help="同时删除 idx_local_bars_provenance（约 246 MB，"
                         "会使 datasource/snapshots.py 的批处理查询退化为全表扫描）")
    ap.add_argument("--no-backup", action="store_true",
                    help="跳过 --apply 前的在线备份（不推荐）")
    ap.add_argument("--restore", action="store_true", help="从备份还原主库")
    ap.add_argument("--backup", default="", help="还原用的备份文件路径")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出画像（供脚本消费）")
    args = ap.parse_args()

    db = Path(args.db).resolve()

    if args.restore:
        if not args.backup:
            print("✗ --restore 需要 --backup <备份路径>", file=sys.stderr)
            return 2
        return restore(db, Path(args.backup).resolve())

    p = profile(db)
    if args.json:
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return 0

    print(f"主库：{p['db']}")
    print(f"  文件体积      {p['file_mb']} MB")
    print(f"  freelist 页   {p['freelist_pages']}  "
          f"(≈ {0 if p['freelist_pages'] is None else p['freelist_pages'] * 4096 / 1048576:.1f} MB 空闲，"
          f"占比极小说明不是碎片化膨胀)")
    print(f"  主键列        {p['pk_columns']}")
    print()
    print("  local_bars 二级索引：")
    print(f"  {'索引':<34}{'体积 MB':>10}  {'列':<40}{'判定'}")
    print("  " + "-" * 96)
    total = 0.0
    for name, info in sorted(p["indexes"].items(), key=lambda kv: -(kv[1]["size_mb"] or 0)):
        size = info["size_mb"]
        if size:
            total += size
        tag = "← 主键前缀，冗余" if info["is_pk_prefix"] else (
            "← 有真实用途" if name == OPTIONAL_INDEX else "")
        print(f"  {name:<34}{size if size is not None else '?':>10}  "
              f"{','.join(info['columns']):<40}{tag}")
    print(f"  {'合计':<34}{total:>10}")
    print()

    if not p["indexes"]:
        print("  未找到 local_bars 上的二级索引。")
        return 0

    print(f"  可回收：{REDUNDANT_INDEX}（主键前缀，零性能损失）")
    if args.also_provenance:
        print(f"  同时回收：{OPTIONAL_INDEX}（会使 2 处批处理查询退化为全表扫描）")
    print()
    if args.apply:
        return apply_optimize(db, args.also_provenance, not args.no_backup)
    print("  dry-run：未修改任何文件。加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
