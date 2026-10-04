"""孤儿模块扫描（``backend/scripts/audit_orphan_modules.py``）的回归测试。

为什么需要它 —— **这个工具曾经长期假告状**（2026-10-05 实测）：

    旧判据只认 ``.py`` 里的 ``import`` 语句。于是两类**纯命令行入口**被永久
    报成「零引用模块」：

    * ``tools/fetch_runtimes.py`` —— **构建关键路径**，被
      ``.github/workflows/release.yml`` / ``build-client.yml`` 以
      ``python tools/fetch_runtimes.py --only cp311 --with-deps --strict`` 调用；
      不跑它就会打出「没有桥接运行时」的包（被 ``QMT_BUILD_REQUIRE_RUNTIMES=1``
      硬闸门兜底，见 docs/TECH_DEBT.md）。
    * ``tools/diag_qmt.py`` —— 人工诊断入口，被 ``xtquant_client/xtp/env.py`` /
      ``adapter.py`` 的活代码 docstring 点名。

    一份「永远有 2 条假红」的报告，唯一作用是教会使用者忽略这个脚本 ——
    与 TD-33（诊断工具拿陈旧快照当证据）同族：**工具说谎比没有工具更坏**。

因此本条测试锁死四件事：

1. 「按路径点名」的判据必须真的覆盖 CI（``.yml``）与 shell/bat 这些**非 import**
   的调用方式，且**不得把更长标识符切出假匹配**（``fooapp/x.py`` 不算）；
2. 两类已知活模块必须能被判据覆盖 —— 且**不是靠把名字塞进 ALLOW** 掩盖；
3. ``main()`` 在真有孤儿时必须**非零退出**（否则它只是报告，不是门禁）；
4. 全仓实测 ``零引用模块：0``（这是「不存在孤儿逻辑」的总闸）。

★ 本文件自身位于扫描面内（``backend/tests/**`` 会被 ``_cmdline_refs`` 读到），
  所以断言里出现的路径字面量一律**用字符串拼接构造**，避免测试文件自己
  给被测对象"喂"出一条引用 —— 那样测试就变成了自我实现的空转。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "backend" / "scripts" / "audit_orphan_modules.py"

#: 两个「必须被判为活」的已知入口（拼接构造，避免自我喂料）。
_LIVE_TOOLS = ("fetch_" + "runtimes", "diag_" + "qmt")


def _load():
    spec = importlib.util.spec_from_file_location("_audit_orphan_modules", _SCRIPT)
    assert spec and spec.loader, f"无法加载 {_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _load()


# ---------------------------------------------------------------- 正则判据

def test_invoke_regex_matches_command_line_call(audit):
    m = audit._RE_INVOKE.search("run: python tools/" + "fetch_runtimes.py --with-deps")
    assert m, "命令行调用形式必须能匹配"
    assert m.group(1) == "tools/" + "fetch_runtimes"


def test_invoke_regex_matches_repo_prefixed_path(audit):
    """``backend/app/routes/market.py`` 这种带仓库前缀的写法也要认。"""
    m = audit._RE_INVOKE.search("见 backend/app/routes/market.py 的挂载块")
    assert m and m.group(1) == "app/routes/market"


def test_invoke_regex_does_not_cut_longer_identifier(audit):
    """前导边界：``fooapp/x.py`` 不得被切出 ``app/x``。"""
    assert audit._RE_INVOKE.search("fooapp/bar.py") is None
    assert audit._RE_INVOKE.search("mytools/baz.py") is None


def test_invoke_regex_ignores_non_scanned_dirs(audit):
    """非产品目录（tests/scripts/...）不是被扫描模块，不该产生引用。"""
    assert audit._RE_INVOKE.search("tests/probes/probe_periods.py") is None
    assert audit._RE_INVOKE.search("scripts/audit_doc_links.py") is None


def test_invoke_names_yields_dotted_and_leaf(audit):
    names = audit._invoke_names("python tools/" + "fetch_runtimes.py")
    assert "tools." + "fetch_runtimes" in names
    assert "fetch_" + "runtimes" in names


def test_text_suffixes_cover_ci_and_shell(audit):
    """CI（yml）与批处理（bat/sh）必须在内，否则又回到"只看 .py"的老毛病。"""
    for suffix in (".yml", ".yaml", ".bat", ".sh", ".py", ".md"):
        assert suffix in audit.TEXT_SUFFIXES, f"{suffix} 必须参与命令行引用扫描"


def test_scan_dirs_exclude_vendored_runtimes(audit):
    """``runtimes/`` 是捆绑 CPython 依赖产物，扫描面必须排除它。"""
    assert "runtimes" not in audit.SCAN_DIRS
    assert "runtimes" in audit.SKIP_PARTS


# ------------------------------------------------- 已知活模块必须被覆盖

@pytest.mark.parametrize("tool", _LIVE_TOOLS)
def test_live_entry_points_are_not_whitelisted(audit, tool):
    """★ 修的是判据，不是白名单 —— ALLOW 里不许出现这两个名字。"""
    assert tool not in audit.ALLOW, (
        f"{tool} 是「被命令行调用」的活模块，属于判据缺口；"
        "塞进 ALLOW 会让它真死掉那天也没人发现"
    )


def test_missing_orphan_modules_are_not_whitelisted(audit):
    """ALLOW 只放真正无静态线索的项，且必须是一条能自解释的原因。"""
    assert audit.ALLOW == {"__main__", "__init__", "conftest", "setup"}


def test_ci_workflow_still_invokes_bridge_runtime_provisioning(audit):
    """直接对 CI 文件复核：bridge 运行时现备步骤还在，且判据认得它。

    这一条**故意从真实文件读文本**（而不是全仓 refs），这样即便测试文件本身
    也写了这个路径，断言仍然指向「CI 里真的有这一步」。
    """
    rel = ROOT / ".github" / "workflows" / "release.yml"
    text = rel.read_text(encoding="utf-8", errors="replace")
    names = audit._invoke_names(text)
    assert "tools." + "fetch_runtimes" in names, (
        "release.yml 必须仍以 python tools/fetch_runtimes.py 现备桥接运行时；"
        "若已改由别处准备，请同步更新本测试与 fetch_runtimes 的存废判断"
    )


def test_live_docstring_reference_is_recognized(audit):
    """活代码 docstring 里点名 ``tools/diag_qmt.py`` 也要算引用。"""
    rel = ROOT / "backend" / "xtquant_client" / "xtp" / "env.py"
    text = rel.read_text(encoding="utf-8", errors="replace")
    names = audit._invoke_names(text)
    assert "tools." + "diag_qmt" in names


# ----------------------------------------------------- import 判据（不回归）

def test_referenced_names_expands_from_import_list(audit):
    """``from X import a, b`` 必须把 a、b 都算上（否则会误报子模块为孤儿）。"""
    refs = audit._referenced_names("from app.services import account_store, order_store\n")
    assert "account_store" in refs
    assert "order_store" in refs
    assert "app.services.account_store" in refs


def test_referenced_names_handles_plain_import(audit):
    refs = audit._referenced_names("import datasource.registry\n")
    assert "datasource" in refs
    assert "datasource.registry" in refs


# ------------------------------------------------------------- 门禁语义

def test_main_fails_when_orphan_present(audit, monkeypatch, capsys):
    """★ 真有孤儿时必须非零退出 —— 只打印的报告拦不住任何人。"""
    monkeypatch.setattr(audit, "_module_names",
                        lambda: {"tools.__dead_probe__": Path("x.py")})
    monkeypatch.setattr(audit, "_all_sources", lambda: [])
    monkeypatch.setattr(audit, "_text_files", lambda: [])
    rc = audit.main()
    out = capsys.readouterr().out
    assert rc == 1, "有零引用模块时 main() 必须返回 1"
    assert "tools.__dead_probe__" in out
    assert "[FAIL]" in out


def test_main_passes_when_only_allowlisted(audit, monkeypatch):
    """ALLOW 内的入口（如 __main__）不算孤儿。"""
    monkeypatch.setattr(audit, "_module_names",
                        lambda: {"app.__main__": Path("x.py")})
    monkeypatch.setattr(audit, "_all_sources", lambda: [])
    monkeypatch.setattr(audit, "_text_files", lambda: [])
    assert audit.main() == 0


# --------------------------------------------------------------- 全仓总闸

def test_no_orphan_modules_repo_wide(audit, capsys):
    """全仓实测：零引用模块必须为 0（「不存在孤儿逻辑」的总闸）。"""
    rc = audit.main()
    out = capsys.readouterr().out
    assert "零引用模块：0" in out, f"扫描结果非零孤儿：\n{out}"
    assert rc == 0
