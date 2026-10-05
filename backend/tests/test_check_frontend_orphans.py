"""`scripts/check_frontend_orphans.py` 的回归测试。

这条门禁的价值完全取决于**判据是否可证伪**：
- 把「测试里出现过」也算消费者 ⇒ 不能乱报；
- 真零引用 ⇒ 必须报红；
- 裁定表条目一旦过期（符号没了 / 已有消费者）⇒ 必须报红，否则清单会腐烂成摆设。

故每个「必须报红」的分支都有对应的证伪用例，而不是只测「现在全绿」。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load():
    root = Path(__file__).resolve().parents[2]      # tests/<this file> -> repo root
    script = root / "scripts" / "check_frontend_orphans.py"
    assert script.is_file(), f"门禁脚本不存在：{script}"
    spec = importlib.util.spec_from_file_location("_frontend_orphans", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate():
    return _load()


def _make_tree(tmp_path: Path, decl_files: dict[str, str], *, filler: int = 60):
    """造一个够大的 src/tests 树（脚本有「扫描面过小即失败」的自检，需 ≥50 文件）。

    返回 (src_dir, tests_dir)。
    """
    src = tmp_path / "frontend-next" / "src"
    tests = tmp_path / "frontend-next" / "tests"
    src.mkdir(parents=True)
    tests.mkdir(parents=True)
    for rel, text in decl_files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    # 填充文件：只是把文件数顶过自检阈值。
    # ★ 它们必须**自己不是孤儿**：每个符号都被下面那个不导出的聚合文件引用一次，
    #   否则 fillerN 会以「零引用未裁定」的身份混进问题清单，把「期望全绿」的
    #   用例顶红 —— 夹具本身把被测判据污染了。
    for i in range(filler):
        (src / f"filler{i}.ts").write_text(f"export const filler{i} = {i};\n",
                                          encoding="utf-8")
    imports = "\n".join(
        f'import {{ filler{i} }} from "./filler{i}";' for i in range(filler))
    uses = ", ".join(f"filler{i}" for i in range(filler))
    (src / "_filler_refs.ts").write_text(
        f"{imports}\nvoid [{uses}];\n", encoding="utf-8")
    return src, tests


def _run(gate, src: Path, tests: Path, adjudicated: dict | None, monkeypatch, capsys):
    monkeypatch.setattr(gate, "SRC", src)
    monkeypatch.setattr(gate, "TESTS", tests)
    monkeypatch.setattr(gate, "ADJUDICATED", adjudicated or {})
    rc = gate.main()
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------- 声明识别

def test_all_export_kinds_are_detected(gate, tmp_path, monkeypatch, capsys):
    """function/const/class/interface/type/enum 六种形态都要被识别为候选。"""
    src, tests = _make_tree(tmp_path, {
        "kinds.ts": (
            "export function aFn() {}\n"
            "export const bConst = 1;\n"
            "export class CCls {}\n"
            "export interface DIf { x: number }\n"
            "export type EType = string;\n"
            "export enum FEnum { A }\n"
        ),
    })
    rc, out = _run(gate, src, tests, {}, monkeypatch, capsys)
    assert rc == 1, "六个都零引用且未裁定 ⇒ 必须报红"
    for n in ("aFn", "bConst", "CCls", "DIf", "EType", "FEnum"):
        assert n in out, f"{n} 未被识别为导出候选"


# ---------------------------------------------------------------- 消费者判定

def test_referenced_in_src_is_not_reported(gate, tmp_path, monkeypatch, capsys):
    src, tests = _make_tree(tmp_path, {
        "used.ts": "export const usedOne = 1;\n",
        "consumer.ts": "import { usedOne } from './used';\nconsole.log(usedOne);\n",
    })
    rc, out = _run(gate, src, tests, {}, monkeypatch, capsys)
    assert rc == 0, out
    assert "usedOne" not in out.split("零引用导出清单")[-1]


def test_referenced_only_in_tests_is_not_reported(gate, tmp_path, monkeypatch, capsys):
    """★ 关键：测试也是消费者。不认测试会制造假告警（本仓最忌讳）。"""
    src, tests = _make_tree(tmp_path, {"fmt.ts": "export const onlyTestUses = 1;\n"})
    (tests / "fmt.test.ts").write_text(
        "import { onlyTestUses } from '@/fmt';\nit('x', () => expect(onlyTestUses).toBe(1));\n",
        encoding="utf-8")
    rc, out = _run(gate, src, tests, {}, monkeypatch, capsys)
    assert rc == 0, out
    assert "onlyTestUses" not in out.split("零引用导出清单")[-1]


def test_zero_ref_outside_tests_is_reported(gate, tmp_path, monkeypatch, capsys):
    src, tests = _make_tree(tmp_path, {"dead.ts": "export const nobody = 1;\n"})
    rc, out = _run(gate, src, tests, {}, monkeypatch, capsys)
    assert rc == 1
    assert "nobody" in out


# ---------------------------------------------------------------- 裁定表语义

def test_adjudicated_zero_ref_passes(gate, tmp_path, monkeypatch, capsys):
    src, tests = _make_tree(tmp_path, {"keep.ts": "export const keptOnPurpose = 1;\n"})
    rc, out = _run(gate, src, tests,
                   {"keep.ts::keptOnPurpose": "零调用但合法：为控制台排障保留，刻意不做 UI 入口。"},
                   monkeypatch, capsys)
    assert rc == 0, out
    assert "gate: OK" in out


def test_stale_entry_when_symbol_gone_fails(gate, tmp_path, monkeypatch, capsys):
    """裁定表指向一个已不存在的符号 ⇒ 过期 ⇒ 报红（否则清单会无限期腐烂）。"""
    src, tests = _make_tree(tmp_path, {"keep.ts": "export const stillHere = 1;\n"})
    rc, out = _run(gate, src, tests,
                   {"keep.ts::removedAlready": "这条理由再充分也没用，符号已经没了。"},
                   monkeypatch, capsys)
    assert rc == 1
    assert "已不存在" in out


def test_stale_entry_when_now_consumed_fails(gate, tmp_path, monkeypatch, capsys):
    """裁定表条目对应的符号**已有消费者** ⇒ 必须删条目 ⇒ 报红。"""
    src, tests = _make_tree(tmp_path, {
        "keep.ts": "export const nowUsed = 1;\n",
        "c.ts": "import { nowUsed } from './keep';\nconsole.log(nowUsed);\n",
    })
    rc, out = _run(gate, src, tests,
                   {"keep.ts::nowUsed": "曾经零引用所以登记，现在有消费者了。"},
                   monkeypatch, capsys)
    assert rc == 1
    assert "已有消费者" in out


def test_too_short_reason_fails(gate, tmp_path, monkeypatch, capsys):
    src, tests = _make_tree(tmp_path, {"keep.ts": "export const x1 = 1;\n"})
    rc, out = _run(gate, src, tests, {"keep.ts::x1": "占位"}, monkeypatch, capsys)
    assert rc == 1
    assert "理由过短" in out


def test_tiny_scan_surface_fails_loudly(gate, tmp_path, monkeypatch, capsys):
    """扫描面过小（路径/过滤器写错）⇒ 必须失败，而不是给出「零孤儿」的假结论。"""
    src = tmp_path / "frontend-next" / "src"
    src.mkdir(parents=True)
    (src / "only.ts").write_text("export const a = 1;\n", encoding="utf-8")
    tests = tmp_path / "frontend-next" / "tests"
    tests.mkdir()
    rc, out = _run(gate, src, tests, {}, monkeypatch, capsys)
    assert rc == 1
    assert "扫描面过小" in out


# ---------------------------------------------------------------- 总闸

def test_repo_wide_gate_is_green(gate, monkeypatch, capsys):
    """真实仓库面：门禁必须是绿的（零引用导出全部已裁定，无过期条目）。"""
    monkeypatch.setattr(gate, "SRC", Path(gate.__file__).resolve().parents[1] / "frontend-next" / "src")
    monkeypatch.setattr(gate, "TESTS", Path(gate.__file__).resolve().parents[1] / "frontend-next" / "tests")
    rc = gate.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "gate: OK" in out
    # 扫描面必须是真的（不是 0 文件蒙混过关）
    assert "声明" in out and int(out.split("个文件")[0].split("：")[-1]) > 100
