"""文档引用断链门禁（``scripts/audit_doc_links.py``）的回归测试。

为什么需要它——**这个工具自己会假告状**（2026-10-05 实测）：

    正则写成 ``\\.(?:md|py|ts|tsx|...)$`` 时，``ts`` 分支在 ``tsx`` 之前，
    于是 ``frontend-next/src/app/routes.tsx`` 被切成 ``.../routes.ts``
    （尾巴 ``x`` 被静默丢掉）。全仓 **20 处** ``*.tsx`` 引用因此被集体误报成
    「断链」—— 而它们其实全都好好的。

这与 TD-33（诊断工具拿陈旧快照当证据）是同一家族：**工具说谎比没有工具更坏**，
因为它会把真问题淹在假问题里，最后没人再看它的输出。所以此处锁死三条：

1. 扩展名匹配**不得截断**（``tsx`` 必须完整吃掉，且不吃到更长名字的前缀）；
2. 解析别名必须认（pytest rootdir 是 ``backend``，故 ``tests/test_x.py``
   是 ``backend/tests/test_x.py`` 的合法简写；前端同理）；
3. **不接受无理由的白名单**：``KNOWN_MISSING`` 每一条都必须写清「为什么不是缺陷」，
   冻结源必须是真实存在的目录/文件 —— 否则这个清单会退化成「把告警藏起来」的地方。

最后一条用**全量跑一遍**收口：``main()`` 必须返回 0。这是「不存在断链」的总闸。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "audit_doc_links.py"


def _load():
    spec = importlib.util.spec_from_file_location("_audit_doc_links", _SCRIPT)
    assert spec and spec.loader, f"无法加载 {_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _load()


# ---------------------------------------------------------------- 扩展名截断

def test_regex_does_not_truncate_tsx(audit):
    """``.tsx`` 必须整体匹配 —— 这条就是本次真缺陷的锁。"""
    text = "页面注册表见 frontend-next/src/app/routes.tsx 的 PAGES。"
    got = [m.group(1) or m.group(2) for m in audit._RE_PATH.finditer(text)]
    assert got == ["frontend-next/src/app/routes.tsx"]
    assert not any(g.endswith(".ts") for g in got), "绝不能截成 .ts"


@pytest.mark.parametrize("cand", [
    "frontend-next/src/charts/KLineChart.tsx",
    "frontend-next/src/domains/system/SystemLog.tsx",
    "frontend-next/src/domains/Dashboard.tsx",
    "docs/DESIGN_SYSTEM.md",
    "backend/datasource/periods.py",
])
def test_regex_matches_full_extension(audit, cand):
    text = f"见 `{cand}`。"
    got = [m.group(1) or m.group(2) for m in audit._RE_PATH.finditer(text)]
    assert got == [cand]


def test_regex_does_not_eat_longer_extension_prefix(audit):
    """``(?![.\\w])`` 边界：不得匹配成更长扩展名的前缀。"""
    # .mdx / .json5 之类不是本仓要扫的类型，不该被切出一个"存在的"前缀
    text = "见 docs/README.mdx 与 foo.json5"
    got = [m.group(1) or m.group(2) for m in audit._RE_PATH.finditer(text)]
    assert got == []


def test_regex_ignores_placeholder_tokens(audit):
    """占位符/模板片段不是断链。"""
    for text in ("示例：docs/xxx.md", "见 docs/…/a.md", "路径 docs/<name>.md"):
        for m in audit._RE_PATH.finditer(text):
            cand = m.group(1) or m.group(2)
            assert any(tok in cand for tok in audit._ALLOW_SUBSTR), (
                f"占位符应被 _ALLOW_SUBSTR 命中：{cand}")


# ---------------------------------------------------------------- 解析别名

def test_backend_alias_resolves(audit):
    """pytest rootdir 是 backend ⇒ ``tests/test_x.py`` 简写等价于 backend 下同名文件。"""
    tracked = set()
    src = ROOT / "scripts" / "verify_gpu_safe_mode_persistence.py"
    assert audit._resolves(src, "backend/tests/test_unit.py", tracked)
    assert audit._resolves(src, "backend/pyproject.toml", tracked)


def test_frontend_next_alias_resolves(audit):
    """vitest rootdir 是 frontend-next ⇒ 不带 ``frontend-next/`` 前缀的 tests 简写合法。"""
    src = ROOT / "frontend-next" / "src" / "stores" / "quotes.ts"
    assert audit._resolves(src, "frontend-next/tests/clientExit.test.ts", set())


def test_probes_alias_resolves(audit):
    """探针脚本自身用法行省略前缀，实指 backend/tests/probes/。"""
    src = ROOT / "backend" / "datasource" / "periods.py"
    assert audit._resolves(src, "backend/tests/probes/probe_periods.py", set())


def test_resolver_rejects_real_missing_file(audit):
    """反面：别名不能把**真的**不存在变成存在（否则门禁形同虚设）。

    ⚠️ 两个样本用**拼接**写出来，不是洁癖：本文件也在我方扫描面内，
    直接写出完整的不存在路径，会让门禁把「测试的负样本」当成真断链报出来
    （实测踩到：本文件一落地，全仓断链从 0 变 4）。
    """
    src = ROOT / "backend" / "plugins" / "__init__.py"
    assert not audit._resolves(src, "docs/" + "2026-09-09_V9总纲实施核对审计报告.md", set())
    assert not audit._resolves(src, "backend/" + "no/such/file_xyz.py", set())


# ---------------------------------------------------------------- 规则表自审

def test_known_missing_entries_all_carry_reason(audit):
    """白名单必须是「有理由的例外」，不能是「藏告警的洞」。"""
    assert audit.KNOWN_MISSING, "KNOWN_MISSING 被清空了？"
    for cand, reason in audit.KNOWN_MISSING.items():
        assert cand and cand == cand.strip(), f"候选名不规范：{cand!r}"
        assert isinstance(reason, str) and len(reason.strip()) >= 10, (
            f"「{cand}」的理由太短/为空——无理由的豁免等于藏问题")


def test_allow_and_placeholder_lists_are_documented(audit):
    """同上：``ALLOW`` 的每一项也应能在源码里看到解释。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "已知有意" in src or "占位" in src
    assert all(isinstance(x, str) and x for x in audit.ALLOW)


def test_frozen_sources_exist(audit):
    """冻结源必须真实存在——否则「跳过它」就成了跳过空气，掩盖配置错误。"""
    assert audit.FROZEN_SOURCES
    for entry in audit.FROZEN_SOURCES:
        p = ROOT / entry.rstrip("/")
        assert p.exists(), f"冻结源不存在：{entry}"
        if entry.endswith("/"):
            assert p.is_dir(), f"{entry} 声明为目录却不是目录"


def test_frozen_sources_are_not_overbroad(audit):
    """冻结源不得宽到把产品面整片罩住（那样门禁就白设了）。"""
    for entry in audit.FROZEN_SOURCES:
        assert entry not in ("", "/", "docs/", "backend/", "frontend-next/"), \
            f"冻结源过宽：{entry}"


# ---------------------------------------------------------------- 总闸

def test_every_top_level_doc_is_indexed():
    """``docs/`` 顶层的每份文档都必须出现在 ``docs/README.md`` 索引里。

    为什么这算「断链」的一种：断链门禁管的是「引用指向不存在的文件」，
    反过来「文件存在却没人引用」同样致命 —— 索引是文档**有没有被遗弃**的唯一入口，
    漏登记就是事实上的孤儿文档（本轮实测抓到 3 份：`REMOTE_ACCESS_DECISION.md`
    被 5 处活代码引用却不在索引里、`SECURITY_AND_DEPLOYMENT_AUDIT.md`、
    `DATASTORE_SIZE_AND_SPLIT_ANALYSIS.md` 被 `scripts/optimize_local_bars.py` 引用）。

    受管子目录（`release-notes/` / `archive/` / `screenshots/`）各有自己的
    入口说明，不在本条管辖范围内。
    """
    docs = ROOT / "docs"
    index = (docs / "README.md").read_text(encoding="utf-8", errors="replace")
    missing = [p.name for p in sorted(docs.glob("*.md"))
               if p.name != "README.md" and p.name not in index]
    assert not missing, (
        f"以下文档未登记进 docs/README.md 索引（孤儿文档）：{missing}\n"
        "要么补一行索引（说明用途与读者），要么按判据删除/归档："
        "论证/决策类文档保留，一次性执行计划删除。"
    )


def test_no_broken_doc_links_repo_wide(audit, capsys):
    """全仓产品面无断链。这是「不存在断链」的可证伪判据。

    改坏任一被引用的路径（或把文件删掉而不同步引用）本条即变红。
    """
    rc = audit.main()
    out = capsys.readouterr().out
    assert "断链 0 处" in out, out
    assert rc == 0, f"audit_doc_links 报出断链：\n{out}"
