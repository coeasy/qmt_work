"""载荷孤儿字段对账（``backend/scripts/audit_payload_orphans.py``）的回归测试。

这个脚本是 TD-31（后端多算、前端零消费）的**可执行近似**。它自己踩过两个坑，
两条都属于「工具在说谎」，因此都必须有测试兜住：

1. **裁定表各持一份**（报告腐烂）
   ``sum_buy_vol`` / ``sum_sell_vol`` 早已被 ``audit_api_payloads.KNOWN`` 判定为
   「协议限制：MAC 无五档盘口」，但本脚本不读那张表 ⇒ 同一批字段**每轮都重新
   被列成"孤儿候选"**。读者第一次会去判、第二次会皱眉、第三次就不看了。修法是
   共用同一张表（``_verdict``），并让「已判定」与「待处理」在输出里分开。

2. **一条永不执行的死分支**（工具自己藏着孤儿逻辑）
   原码注释写「也把前缀段算进来（如 ``breadth_summary.xxx`` 的 ``breadth_summary``）」，
   实现却是 ``leaf.startswith(seg + ".")``，而 ``seg`` 是**末段** ⇒ 恒为 False。
   一个专找孤儿逻辑的脚本里藏着孤儿逻辑，报出来的清单自然缺项
  （``execution`` / ``reasons`` 这类首段字段长期不出现在结果里）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "backend" / "scripts" / "audit_payload_orphans.py"


def _load():
    spec = importlib.util.spec_from_file_location("_audit_payload_orphans", _SCRIPT)
    assert spec and spec.loader, f"无法加载 {_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture()
def frontend(tmp_path: Path) -> Path:
    """最小"前端源码"：只认得的字段会命中词边界。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("const x = resp.known_field;\n", encoding="utf-8")
    return src


def _payload(tmp_path: Path, issues) -> Path:
    p = tmp_path / "payload.json"
    p.write_text(json.dumps({"issues": issues}, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------- 判据 1：裁定表必须共用

def test_verdict_table_is_shared(mod):
    """必须成功从 audit_api_payloads 借用裁定表（借用失败要在输出里明说）。"""
    assert mod._VERDICT_OK is True, f"裁定表不可用：{getattr(mod, '_VERDICT_ERR', '?')}"


def test_known_protocol_limits_are_recognized(mod):
    """已知协议限制字段必须被判为已判定，而不是待处理。"""
    assert mod._verdict("/market/quote", "sum_buy_vol") == "协议限制：MAC 无五档盘口"
    assert mod._verdict("/market/indices", "sum_sell_vol") == "协议限制：MAC 无五档盘口"


# ------------------------------------------- 判据 2：首段分组不得是死分支

def test_first_segment_grouping_is_reachable(mod, tmp_path, frontend, capsys):
    """``reasons.screening`` 这类嵌套叶子必须能让首段 ``reasons`` 进入统计。

    这正是原实现 ``leaf.startswith(seg + ".")`` 永远做不到的那一步。
    """
    pl = _payload(tmp_path, [
        {"endpoint": "/platform/status", "leaf": "reasons.screening"},
    ])
    rc = mod.main(["--payload", str(pl), "--frontend", str(frontend)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "reasons" in out, f"首段分组未生效（死分支未修）：\n{out}"


def test_nested_leaf_under_generic_first_segment_is_not_noise(mod, tmp_path, frontend, capsys):
    """首段若本身是通用名（如 ``items``）仍要被排除，避免清单变噪声。

    ``items.code`` ⇒ 末段 ``code`` 与首段 ``items`` 都在 ``_TOO_GENERIC`` 里，
    于是这个字段一个名字都不该进统计（共 0 个）。
    """
    pl = _payload(tmp_path, [
        {"endpoint": "/market/indices", "leaf": "items.code"},
    ])
    mod.main(["--payload", str(pl), "--frontend", str(frontend)])
    out = capsys.readouterr().out
    assert "共 0 个" in out, out
    assert "items" not in out, out


# ------------------------------------------- 已判定 / 待处理 必须分开呈现

def test_adjudicated_field_is_not_pending(mod, tmp_path, frontend, capsys):
    pl = _payload(tmp_path, [
        {"endpoint": "/platform/status", "leaf": "execution.active_conn_id"},
    ])
    rc = mod.main(["--payload", str(pl), "--frontend", str(frontend)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "待处理 0" in out, out
    # 末段 `active_conn_id` 与首段 `execution` 都会各自成一条 —— 这正是首段
    # 分组生效的证据（死分支修好之前，`execution` 永远不会出现）。
    assert "已判定 2" in out, out
    assert "active_conn_id" in out and "execution" in out, out
    assert "[OK] 待处理零引用字段：0" in out


def test_unknown_field_is_pending(mod, tmp_path, frontend, capsys):
    """裁定表里没有的零引用字段必须进「待处理」，不能被悄悄算作已判定。"""
    pl = _payload(tmp_path, [
        {"endpoint": "/whatever/endpoint", "leaf": "brand_new_field"},
    ])
    rc = mod.main(["--payload", str(pl), "--frontend", str(frontend)])
    out = capsys.readouterr().out
    assert rc == 0, "默认是报告不是门禁"
    assert "待处理 1" in out, out
    assert "brand_new_field" in out


def test_strict_turns_pending_into_failure(mod, tmp_path, frontend, capsys):
    pl = _payload(tmp_path, [
        {"endpoint": "/whatever/endpoint", "leaf": "brand_new_field"},
    ])
    rc = mod.main(["--payload", str(pl), "--frontend", str(frontend), "--strict"])
    out = capsys.readouterr().out
    assert rc == 1, "--strict 下待处理非空必须非零退出"
    assert "[FAIL]" in out


def test_strict_is_green_when_all_adjudicated(mod, tmp_path, frontend, capsys):
    pl = _payload(tmp_path, [
        {"endpoint": "/platform/status", "leaf": "execution.active_conn_id"},
    ])
    rc = mod.main(["--payload", str(pl), "--frontend", str(frontend), "--strict"])
    assert rc == 0, "--strict 下全部已判定应当为绿"


# ------------------------------------------------------------- 用法守卫

def test_empty_frontend_tree_fails_loudly(mod, tmp_path, capsys):
    empty = tmp_path / "empty_src"
    empty.mkdir()
    pl = _payload(tmp_path, [{"endpoint": "/a/b", "leaf": "field_x"}])
    rc = mod.main(["--payload", str(pl), "--frontend", str(empty)])
    out = capsys.readouterr().out
    assert rc == 1, "前端源码为空时必须报错，不能给出「零引用」的假结论"
    assert "前端源码为空" in out
