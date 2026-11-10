"""P0 探针（73 项 / 8 组）：**工具本身必须可证伪**。

为什么值得给工具写测试：
探针是用来判断「这台机器封了什么」的，如果探针自己数错项、生成物在 py3.6 跑不起来、
或者解析器把最后一条结果读错，结论就是错的 —— 而错误结论会直接导致选错架构。

盯四件事：
* 73 项 / 8 组与声明表**逐组相等**（改检查项必须同步改声明）；
* 生成物必须是 ``gbk + CRLF`` 且无 f-string（``gbk+LF`` 在 QMT 内置 py3.6 必 SyntaxError）；
* 真跑一遍能产出 73 条结果、状态只在四个合法值里；
* 解析器取**最后一条**记录（客户端日志可能有多轮），没有记录时抛错而不是返回空壳。
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import qmt_sandbox_probe as probe  # noqa: E402

STATUSES = {"PASS", "BLOCK", "WARN", "SKIP"}


def test_group_sizes_match_checks():
    counted = Counter(group for group, _, _ in probe.CHECKS)
    assert len(probe.CHECKS) == 68
    assert dict(counted) == probe.GROUP_SIZES
    assert sum(probe.GROUP_SIZES.values()) == 68


def test_check_names_unique():
    names = [name for _, name, _ in probe.CHECKS]
    assert len(names) == len(set(names))


def test_db_group_excluded_by_default():
    """数据库组实测全不可用，默认不生成 —— 否则每跑一次白等 redis 的 30 秒重试。"""
    assert all(group != "D" for group, _, _ in probe.CHECKS)
    assert "D" not in probe.GROUP_SIZES
    with_db = probe.checks_for(with_db=True)
    assert len(with_db) == 68 + len(probe.DB_CHECKS)
    assert probe.group_sizes_for(True)["D"] == len(probe.DB_CHECKS)


def test_every_check_has_expression():
    for group, name, expr in probe.CHECKS:
        assert group in probe.GROUP_SIZES, group
        assert expr.strip(), name


def test_emit_gbk_crlf_without_fstring(tmp_path):
    target = tmp_path / "probe.py"
    probe.emit(str(target), encoding="gbk")
    raw = target.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") > 0
    text = raw.decode("gbk")
    assert "f'" not in text and 'f"' not in text
    compile(text, "probe", "exec")  # 语法自洽


def test_emit_utf8_is_lf(tmp_path):
    target = tmp_path / "probe_utf8.py"
    probe.emit(str(target), encoding="utf-8")
    raw = target.read_bytes()
    assert b"\r\n" not in raw


def test_network_group_off_by_default(tmp_path):
    target = tmp_path / "probe.py"
    probe.emit(str(target), encoding="utf-8")
    text = target.read_text(encoding="utf-8")
    assert "RUN_NETWORK = False" in text
    assert "RUN_NETWORK=False" in text  # 生成的 SKIP 原因


def test_emitted_checks_list_has_68(tmp_path):
    """静态核对：生成物里真的带着 68 项（不是只有 Python 侧常量是 68）。"""
    import ast
    target = tmp_path / "probe.py"
    probe.emit(str(target), encoding="utf-8")
    tree = ast.parse(target.read_text(encoding="utf-8"))
    checks = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CHECKS":
            checks = ast.literal_eval(node.value)
    assert checks is not None
    assert len(checks) == 68
    assert dict(Counter(item[0] for item in checks)) == probe.GROUP_SIZES


def test_probe_runs_end_to_end(tmp_path, monkeypatch, capsys):
    """真跑一遍生成物（只取轻量子集，避免在 CI 里 import 一堆重库）。"""
    monkeypatch.chdir(tmp_path)
    light = tuple(item for item in probe.CHECKS if item[0] in ("H", "F", "K", "B"))
    monkeypatch.setattr(probe, "CHECKS", light)

    target = tmp_path / "probe.py"
    probe.emit(str(target), encoding="utf-8")
    source = target.read_text(encoding="utf-8")

    namespace = {"__name__": "probe_under_test"}
    exec(compile(source, "probe", "exec"), namespace)

    class Ctx:
        def get_market_data_ex(self, *args, **kwargs):
            return [[1.0], [2.0]]

    namespace["init"](Ctx())
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith(probe.PROBE_PREFIX)]
    assert len(lines) == 1

    payload = json.loads(lines[0][len(probe.PROBE_PREFIX):])
    assert payload["n"] == len(light)
    assert {row["st"] for row in payload["res"]} <= STATUSES
    # 这台机器上文件读写是通的 ⇒ 至少有一条 PASS（证明探针真的执行了表达式）
    assert any(row["st"] == "PASS" for row in payload["res"])
    # 未注入 save_py_key ⇒ K 组必须是 SKIP 而不是 BLOCK
    k_rows = [row for row in payload["res"] if row["g"] == "K"]
    assert k_rows and all(row["st"] == "SKIP" for row in k_rows)


def test_probe_marks_network_skipped_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    net = tuple(item for item in probe.CHECKS if item[0] == "N")
    monkeypatch.setattr(probe, "CHECKS", net)
    target = tmp_path / "probe_net.py"
    probe.emit(str(target), encoding="utf-8")
    namespace = {"__name__": "probe_net"}
    exec(compile(target.read_text(encoding="utf-8"), "probe", "exec"), namespace)
    namespace["init"](object())
    out = capsys.readouterr().out
    payload = json.loads(out.splitlines()[-1][len(probe.PROBE_PREFIX):])
    assert payload["n"] == 13
    assert all(row["st"] == "SKIP" and "RUN_NETWORK" in row["e"] for row in payload["res"])


def test_parse_log_takes_last_record(tmp_path):
    log = tmp_path / "XtClient.log"
    first = probe.PROBE_PREFIX + json.dumps({"py": "3.6", "n": 1, "res": []})
    second = probe.PROBE_PREFIX + json.dumps({"py": "3.12", "n": 2, "res": [
        {"g": "F", "k": "read_abs", "st": "BLOCK", "e": "PermissionError"}]})
    log.write_text("噪音\n" + first + "\n噪音\n" + second + "\n", encoding="utf-8")
    payload = probe.parse_log(str(log))
    assert payload["py"] == "3.12"
    assert payload["res"][0]["k"] == "read_abs"


def test_parse_log_rejects_missing_record(tmp_path):
    log = tmp_path / "empty.log"
    log.write_text("只有终端噪音\n", encoding="utf-8")
    with pytest.raises(ValueError):
        probe.parse_log(str(log))


def test_summarize_and_verdict_partially_blocked():
    payload = {"res": (
        [{"g": "F", "k": "f%d" % i, "st": "BLOCK"} for i in range(8)] +
        [{"g": "F", "k": "p%d" % i, "st": "PASS"} for i in range(9)] +
        [{"g": "N", "k": "n%d" % i, "st": "SKIP"} for i in range(13)] +
        [{"g": "K", "k": "save_key", "st": "PASS"}, {"g": "K", "k": "get_key", "st": "PASS"},
         {"g": "K", "k": "get_missing", "st": "PASS"},
         {"g": "K", "k": "save_oversize", "st": "BLOCK"}]
    )}
    groups = probe.summarize(payload)
    assert groups["F"]["BLOCK"] == 8
    text = probe.verdict(groups)
    assert "文件部分受限" in text
    assert "网络组未运行" in text
    assert "KV 可用" in text


def test_verdict_fully_closed():
    groups = {"F": {"BLOCK": 17}, "N": {"BLOCK": 13}, "K": {"SKIP": 4}}
    text = probe.verdict(groups)
    assert "文件全封" in text and "网络被拦" in text
    assert "KV 也不可用" in text


def test_report_writes_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = {"py": "3.12", "n": 2, "res": [
        {"g": "F", "k": "read_abs", "st": "BLOCK", "e": "PermissionError"},
        {"g": "K", "k": "save_key", "st": "PASS"}]}
    out = tmp_path / "report.json"
    doc = probe.report(payload, out_path=str(out))
    assert doc["total"] == 2
    assert doc["blocked"] == ["F/read_abs"]
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["verdict"] == doc["verdict"]
    assert os.path.exists(doc["report_path"])
