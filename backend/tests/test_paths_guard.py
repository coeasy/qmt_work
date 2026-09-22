"""数据目录校验与解析的护栏（`core/paths.py` + `core.config` 路径解析 + 冷仓迁移）。

为什么需要它
------------
目录一旦开放给用户设置，就变成了**外部输入面**。而「写错地方」的代价极不对称：

- 写成盘符根目录（``C:\\``）⇒ 备份/清理/迁移全部失去边界；
- 写成系统目录（``C:\\Windows``）⇒ 轻则被 UAC 拦成「保存失败」，重则污染系统；
- 冷库目录改了但**没搬迁数据** ⇒ 图表历史**静默变短**（不报错、不提示）；
- ``override`` 被当成文件路径解释 ⇒ 用户选了个文件夹，得到一个名叫「我的数据」
  的**文件**（不带 .db 后缀）。

锁定的不变量：

1. 四类目录（空 / 盘根 / 系统目录 / 非目录）**全部被拒**，且拒绝原因要能读；
2. ``describe_dir`` **永不抛异常**（界面「边输边提示」要求半截路径也返回数据）；
3. 冷库 ``override`` 按**目录**解释，历史 ``bars_cold_path`` 按**文件**解释（向后兼容）；
4. 冷仓文件迁移**只复制不删源**。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.config as cfg
from core.paths import (PathError, describe_dir, inside_install_dir,
                        is_drive_root, is_system_dir, system_dir_roots,
                        validate_dir)


# ------------------------------------------------------------------ 拒绝四类

def test_empty_is_rejected():
    """``Path("")`` 会退化成当前工作目录 —— 最隐蔽的一种「写错地方」。"""
    for raw in ("", "   ", None):
        with pytest.raises(PathError):
            validate_dir(raw)          # type: ignore[arg-type]


def test_drive_root_is_rejected():
    anchor = Path(__file__).resolve().anchor      # Windows: "C:\\"；POSIX: "/"
    assert is_drive_root(anchor) is True
    with pytest.raises(PathError) as ei:
        validate_dir(anchor)
    assert "盘符根目录" in str(ei.value)


def test_system_dir_is_rejected():
    roots = system_dir_roots()
    if not roots:
        pytest.skip("当前环境取不到系统目录（环境变量缺失），跳过平台相关断言")
    root = str(roots[0])
    assert is_system_dir(root) is True
    with pytest.raises(PathError) as ei:
        validate_dir(root)
    assert "系统目录" in str(ei.value)

    # 子目录同样被拒（只挡顶层的话 ``C:\Windows\System32`` 就漏了）
    with pytest.raises(PathError):
        validate_dir(os.path.join(root, "System32"))


def test_existing_file_is_rejected(tmp_path):
    f = tmp_path / "iam_a_file.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(PathError) as ei:
        validate_dir(str(f))
    assert "不是目录" in str(ei.value)


def test_windows_bad_chars_are_rejected():
    if os.name != "nt":
        pytest.skip("仅在 Windows 上校验非法文件名字符")
    with pytest.raises(PathError):
        validate_dir(str(Path("C:/tmp/a<b")))


# ------------------------------------------------------------------ 接受

def test_normal_dir_is_accepted_and_created(tmp_path):
    target = tmp_path / "a" / "b"
    p = validate_dir(str(target), create=True)
    assert p.is_dir(), "create=True 必须真的把目录建出来"
    assert describe_dir(str(target))["ok"] is True


def test_inside_install_dir_is_a_hint_not_a_rejection(tmp_path):
    """默认导出目录就在运行目录里 ⇒ 只能是提示，绝不能拒绝。"""
    assert inside_install_dir(cfg.exe_dir()) is True
    # 反证：它仍然「可用」（不是被拒的四类之一）
    assert describe_dir(str(cfg.exe_dir()))["ok"] is True


def test_describe_dir_never_raises():
    """界面边输边提示：用户路径打了一半是常态，不能因此 500。"""
    for raw in ("", "   ", "C:/", "C:/Windows", "/etc", "半截路径", "\x00bad", None):
        out = describe_dir(raw)        # type: ignore[arg-type]
        assert isinstance(out, dict)
        assert "ok" in out and "reason" in out
        if out["ok"] is False:
            assert out["reason"], f"被拒却没有原因（无法自查）：{raw!r}"


# ------------------------------------------------------------------ 路径解析

def test_cold_override_is_a_directory(tmp_path):
    """``offline.cold_dir`` 是**文件夹**；按文件解释会产出没有后缀的怪文件。"""
    p = cfg.cold_bars_path(str(tmp_path))
    assert p.name == "bars_cold.db"
    assert p.parent == tmp_path.resolve()


def test_legacy_bars_cold_path_stays_a_file(monkeypatch, tmp_path):
    """历史 ``bars_cold_path`` 语义是**文件路径**，必须向后兼容（不得再加一层）。"""
    f = tmp_path / "my_cold.db"
    monkeypatch.setattr(cfg.settings, "bars_cold_path", str(f), raising=False)
    assert cfg.cold_bars_path("") == Path(f)


def test_cold_follows_db_dir_when_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg.settings, "bars_cold_path", "", raising=False)
    monkeypatch.setattr(cfg.settings, "db_path", tmp_path / "app.db", raising=False)
    assert cfg.cold_bars_path("") == tmp_path / "bars_cold.db"


def test_export_dir_default_is_under_run_dir():
    p = cfg.export_dir("")
    assert p.name == "export"
    assert p.is_absolute()
    assert cfg.export_dir("D:/my/export").name == "export"


def test_update_config_file_merges_not_overwrites(tmp_path, monkeypatch):
    """整体覆盖会删掉用户手改的 api_key / 风控阈值 —— 必须合并。"""
    import json

    f = tmp_path / "qmt_work_config.json"
    f.write_text(json.dumps({"api_key": "keep-me", "_readme": "note"}),
                 encoding="utf-8")
    monkeypatch.setattr(cfg, "config_file", lambda: f)
    cfg.update_config_file({"db_path": str(tmp_path / "app.db")})

    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["api_key"] == "keep-me", "合并写入却丢了既有配置"
    assert data["_readme"] == "note", "注释键必须原样保留"
    assert data["db_path"] == str(tmp_path / "app.db")


# ------------------------------------------------------------------ 冷仓迁移

def test_cold_copy_from_file_keeps_source(tmp_path):
    """★ 只复制、不删源：删错一个文件比多占一份磁盘严重得多。"""
    from datasource.cold_store import ColdStore

    cols = ("code", "period", "dt", "open", "high", "low", "close",
            "volume", "amount", "fetched_at", "adjust")
    ph = ",".join("?" * len(cols))
    src = ColdStore(tmp_path / "old" / "bars_cold.db")
    src.executemany_in_txn(
        f"INSERT OR REPLACE INTO kline_archive ({','.join(cols)}) VALUES ({ph})",
        [("000001.SZ", "1d", "2024-01-02", 1.0, 1.1, 0.9, 1.05, 100, 105, 0.0, "qfq"),
         ("000001.SZ", "1d", "2024-01-03", 1.05, 1.2, 1.0, 1.1, 120, 130, 0.0, "qfq")])

    dst = ColdStore(tmp_path / "new" / "bars_cold.db")
    res = dst.copy_from_file(src.path)

    assert res["moved"] == 2, res
    assert res["error"] == ""
    assert dst.count() == 2
    assert src.count() == 2, "源文件被删了 —— 迁移必须是复制，不是移动"
    src.close()
    dst.close()


def test_cold_copy_reports_missing_source(tmp_path):
    from datasource.cold_store import ColdStore

    dst = ColdStore(tmp_path / "new" / "bars_cold.db")
    res = dst.copy_from_file(tmp_path / "nope.db")
    assert res["moved"] == 0
    assert res["error"], "源不存在必须给出原因，而不是静默返回 0"
    dst.close()


def test_cold_copy_refuses_same_file(tmp_path):
    from datasource.cold_store import ColdStore

    st = ColdStore(tmp_path / "same.db")
    res = st.copy_from_file(st.path)
    assert res["moved"] == 0 and "相同" in res["error"]
    st.close()
