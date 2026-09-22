"""``POST /api-keys/clean-unused`` 的「从未使用」保留条件回归测试。

★ 锁定一条会造成**静默数据丢失**的真实缺陷
-------------------------------------------

旧 SQL::

    WHERE status='active' AND (last_used_at < ? OR last_used_at = '' OR last_used_at IS NULL)

只判 ``last_used_at``，**没有判 ``created_at``**。而本端点 docstring 声明的保留条件是
「从未使用**且** created_at < cutoff」。两者相反 ⇒ 一个刚创建、还没来得及配到客户端的
密钥，会在**第一次清理时就被删掉**。

典型后果：新建密钥 → 忘了填进脚本 / MCP 配置 → 跑一次「清理 N 天未使用」→ 密钥消失，
且没有任何痕迹可查（界面只显示「已清理 N 个」）。

为什么值得单独测：这条路径平时没人碰（要等运维点一次清理），一旦踩到就是「密钥凭空
没了」，排查成本极高 —— 而且它属于**删数据**的操作，错了不可逆。

⚠️ 本项目**没装 pytest-asyncio** ⇒ 异步一律写成 sync 函数 + ``asyncio.run(...)``。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from app.routes.apikeys import clean_unused_api_keys
from core.clock import local_now, to_iso


def _new_db(tmp_path):
    from core.db import DB

    return DB(Path(tmp_path) / "apikeys_clean.db")


def _insert(db, name: str, created_days_ago: int, last_used_days_ago: int | None) -> None:
    """插入一条密钥。``last_used_days_ago=None`` 表示**从未使用**（空字符串）。"""
    created = to_iso(local_now() - timedelta(days=created_days_ago))
    used = "" if last_used_days_ago is None else to_iso(local_now() - timedelta(days=last_used_days_ago))
    db.execute(
        "INSERT INTO api_keys (key_hash, name, status, created_at, last_used_at) VALUES (?,?, 'active', ?, ?)",
        (f"hash-{name}", name, created, used),
    )


def _clean(db, days: int = 30) -> dict:
    ctx = SimpleNamespace(db=db, apikey_store=None)
    return asyncio.run(clean_unused_api_keys({"days": days}, ctx))


def test_fresh_never_used_key_is_kept(tmp_path):
    """① 刚创建（1 天前）、从未使用 ⇒ **不得被删**（本轮修复的核心）。"""
    db = _new_db(tmp_path)
    try:
        _insert(db, "fresh", created_days_ago=1, last_used_days_ago=None)
        res = _clean(db, days=30)
        left = db.query("SELECT name FROM api_keys")
        assert res["data"]["deleted"] == 0, f"刚创建的密钥不应被删：{res}"
        assert [r["name"] for r in left] == ["fresh"]
    finally:
        db.close()


def test_old_never_used_key_is_deleted(tmp_path):
    """② 创建很久（60 天前）且从未使用 ⇒ 应被删（保留条件的正确边界）。"""
    db = _new_db(tmp_path)
    try:
        _insert(db, "stale", created_days_ago=60, last_used_days_ago=None)
        res = _clean(db, days=30)
        assert res["data"]["deleted"] == 1, f"陈旧未使用密钥应被删：{res}"
        assert db.query("SELECT id FROM api_keys") == []
    finally:
        db.close()


def test_old_used_key_is_deleted(tmp_path):
    """③ 很久没用（60 天前用过）⇒ 应被删。"""
    db = _new_db(tmp_path)
    try:
        _insert(db, "cold", created_days_ago=200, last_used_days_ago=60)
        res = _clean(db, days=30)
        assert res["data"]["deleted"] == 1, f"久未使用应被删：{res}"
    finally:
        db.close()


def test_recently_used_key_is_kept(tmp_path):
    """④ 最近还在用（2 天前）⇒ 不得被删。"""
    db = _new_db(tmp_path)
    try:
        _insert(db, "active", created_days_ago=200, last_used_days_ago=2)
        res = _clean(db, days=30)
        assert res["data"]["deleted"] == 0, f"在用密钥不应被删：{res}"
    finally:
        db.close()


def test_mixed_batch_only_stale_removed(tmp_path):
    """⑤ 混合场景：只删该删的，新建未使用的那条必须活下来。"""
    db = _new_db(tmp_path)
    try:
        _insert(db, "fresh", created_days_ago=1, last_used_days_ago=None)      # 留
        _insert(db, "active", created_days_ago=200, last_used_days_ago=2)      # 留
        _insert(db, "cold", created_days_ago=200, last_used_days_ago=60)       # 删
        _insert(db, "stale", created_days_ago=60, last_used_days_ago=None)     # 删
        res = _clean(db, days=30)
        names = sorted(r["name"] for r in db.query("SELECT name FROM api_keys"))
        assert res["data"]["deleted"] == 2, f"应删 2 条：{res}"
        assert names == ["active", "fresh"], f"存活集合不对：{names}"
    finally:
        db.close()
