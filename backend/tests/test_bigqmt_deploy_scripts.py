# -*- coding: utf-8 -*-
"""P1/P2/P3/P4 交付回归锁。

锁定「大 QMT 一键部署 / 一键诊断」的易用性改进不被后续改动悄悄破坏：

  * `deploy_qmt_work_agent.bat` / `diag_qmt_work_agent.bat` 存在且非空
  * `.bat` 必须含 Python 探测、QMT 目录探测、下一步指引（缺一就退化为「半截脚本」）
  * `gen_qmt_agent_bundle.py` 与 `qmt_agent_deploy.py` 的 `--txt` 参数被 argparse 识别
    （走 AST 静态检查，不真的执行 deploy —— 避免污染 QMT 目录）
  * `docs/QMT_大小版本使用说明.md` §5.2.0 含「快速上手」、§5.6 表含 `.bat`、§5.7 常见错误表存在

历史教训（R15，TD-34 ⑤）：接口加参数看似无害，实际是全局契约变更。
本文件锁的是「参数一旦被移除就红」，防止以后删参数时漏改文档/脚本。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

DEPLOY_BAT = ROOT / "deploy_qmt_work_agent.bat"
DIAG_BAT = ROOT / "diag_qmt_work_agent.bat"
GEN_BUNDLE = ROOT / "scripts" / "gen_qmt_agent_bundle.py"
DEPLOY_SCRIPT = ROOT / "scripts" / "qmt_agent_deploy.py"
USAGE_MD = ROOT / "docs" / "QMT_大小版本使用说明.md"


# --------------------------------------------------------------------------
# .bat 存在性与内容锁
# --------------------------------------------------------------------------

def _read_utf8(path: pathlib.Path) -> str:
    # Windows bat 保存为 UTF-8 without BOM + `chcp 65001`；若被工具转成 GBK 则读回乱码
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize(
    "path, must_contain",
    [
        (
            DEPLOY_BAT,
            [
                "chcp 65001",  # UTF-8 控制台
                "python.exe",  # Python 解释器探测
                "qmt_agent_deploy.py",  # 部署脚本名
                "deploy",  # 部署子命令
                "--txt",  # 生成 .txt 副本
                "qmt_work_agent.py",  # 打开资源管理器选中
                "导入本地策略",  # 下一步指引
                "explorer /select",  # Windows 打开资源管理器选中文件
            ],
        ),
        (
            DIAG_BAT,
            [
                "chcp 65001",
                "qmt_strategy_list_probe.py",
                "qmt_agent_verify.py",
                "qmt_agent_deploy.py",
                "inspect",
                "已注册",  # 判读要点
                "心跳",
                "probe_stale",
            ],
        ),
    ],
)
def test_bat_scripts_exist_and_contain_key_elements(path, must_contain):
    """一键脚本必须存在且非空，且包含所有关键路径与指引。"""
    assert path.exists(), f"{path.name} 缺失（{path}）"
    size = path.stat().st_size
    assert size > 500, f"{path.name} 只有 {size} 字节，疑似被清空"
    text = _read_utf8(path)
    for token in must_contain:
        assert token in text, f"{path.name} 缺少关键元素 {token!r}（脚本退化为半截）"


def test_bat_find_qmt_dirs_covers_common_paths():
    """一键脚本必须覆盖常见的 QMT 安装路径，否则自动探测形同虚设。"""
    for bat_name in ("deploy_qmt_work_agent.bat", "diag_qmt_work_agent.bat"):
        text = _read_utf8(ROOT / bat_name)
        # 至少要探测 3 个以上的常见 QMT 目录
        # bat 里写的是单反斜杠 `P:\stock\gd_qmt\python`；正则 `\\python` 匹配字面 `\python`
        probes = re.findall(r'if\s+exist\s+"[^"]*\\python"', text)
        assert len(probes) >= 3, f"{bat_name} 只探测了 {len(probes)} 个 QMT 路径，太窄"


def test_bat_degrade_when_qmt_not_found():
    """找不到 QMT 目录时，脚本必须支持手动指定（否则用户只能放弃）。"""
    for bat_name in ("deploy_qmt_work_agent.bat", "diag_qmt_work_agent.bat"):
        text = _read_utf8(ROOT / bat_name)
        assert "set /p QMT_DIR" in text, f"{bat_name} 缺少手动指定 QMT 目录的降级路径"
        assert "%~1" in text, f"{bat_name} 缺少命令行传参支持"


# --------------------------------------------------------------------------
# --txt 参数锁（AST 静态检查，不真跑 deploy）
# --------------------------------------------------------------------------

def _argparse_options(path: pathlib.Path) -> set[str]:
    """从 argparse 定义的 add_argument 提取所有 opt 字符串（含 --x / -x / 位置参）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    opts: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
            continue
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                opts.add(a.value)
    return opts


def test_gen_bundle_has_txt_option():
    """gen_qmt_agent_bundle.py 必须暴露 --txt 参数（供路径 B 粘贴场景）。"""
    opts = _argparse_options(GEN_BUNDLE)
    assert "--txt" in opts, f"gen_qmt_agent_bundle.py 缺 --txt 参数，现有: {sorted(opts)}"
    # 且 --txt 必须是 action=store_true（不能要用户额外传值）
    text = GEN_BUNDLE.read_text(encoding="utf-8")
    assert "--txt" in text and "store_true" in text, (
        "--txt 未配置为 action=store_true，用户被迫多传值，UX 退化"
    )


def test_deploy_script_has_txt_option():
    """qmt_agent_deploy.py 必须暴露 --txt 参数（deploy 子命令透传）。"""
    opts = _argparse_options(DEPLOY_SCRIPT)
    assert "--txt" in opts, f"qmt_agent_deploy.py 缺 --txt 参数，现有: {sorted(opts)}"


def test_deploy_command_writes_txt_copy_when_requested():
    """cmd_deploy 里，--txt 触发时必须真的写 .txt 副本（不是仅打印一句）。"""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    # 定位 cmd_deploy 函数体
    m = re.search(
        r"def cmd_deploy\(args\):(?P<body>.*?)(?:\n\ndef |\Z)",
        text,
        flags=re.DOTALL,
    )
    assert m, "cmd_deploy 函数未找到"
    body = m.group("body")
    assert "getattr(args, \"txt\"" in body or "getattr(args, 'txt'" in body, (
        "cmd_deploy 未读取 args.txt"
    )
    # 必须真的写文件（不是 print 假装写了）
    assert '"w", encoding=enc' in body, (
        "cmd_deploy 未以文本模式重新写 .txt 副本"
    )
    assert '+ ".txt"' in body, (
        "cmd_deploy 未生成 .txt 后缀的文件（路径 B 粘贴靠此）"
    )


def test_gen_bundle_writes_txt_copy_when_requested():
    """gen_qmt_agent_bundle.py 里，--txt 触发时必须真的写 .txt 副本。"""
    text = GEN_BUNDLE.read_text(encoding="utf-8")
    assert "if args.txt" in text, "gen_qmt_agent_bundle.py main() 未读取 args.txt"
    assert '+ ".txt"' in text, "gen_qmt_agent_bundle.py 未生成 .txt 后缀文件"


# --------------------------------------------------------------------------
# 文档锁（USAGE §5.2.0 / §5.6 / §5.7）
# --------------------------------------------------------------------------

def test_usage_has_quickstart_section():
    """USAGE §5.2.0 必须存在「快速上手」入口，否则新用户看不到 5 步路径（含前端连桥）。"""
    text = USAGE_MD.read_text(encoding="utf-8")
    assert "5.2.0 快速上手" in text, "USAGE 缺 5.2.0 快速上手小节"
    assert "deploy_qmt_work_agent.bat" in text, "USAGE 未提及 deploy_qmt_work_agent.bat"
    assert "diag_qmt_work_agent.bat" in text, "USAGE 未提及 diag_qmt_work_agent.bat"
    # 快速上手必须是 5 步（不是 4 步）—— 缺第 5 步「前端连桥」会让新用户部署完就以为能用
    assert "5.2.1 详细展开版" in text or "5.2.1" in text, "USAGE 缺 §5.2.1 详细展开版"
    quickstart = text.split("5.2.1", 1)[0]
    for step in ["1. **双击", "2. **在 QMT", "3. **关闭并重启", "4. **双击", "5. **在前端"]:
        assert step in quickstart, f"快速上手缺步骤: {step}"
    # 第 5 步必须提到「大 QMT 桥接（推荐）」模板按钮 —— 与前端 Brokers.tsx 里的模板一一对应
    assert "大 QMT 桥接（推荐）" in quickstart, (
        "快速上手第 5 步未提「大 QMT 桥接（推荐）」模板 —— 用户不知道点哪个按钮")


def test_usage_tool_table_lists_bat_scripts():
    """USAGE §5.6 工具表必须列出两个 .bat 脚本（否则用户不知道有双击入口）。"""
    text = USAGE_MD.read_text(encoding="utf-8")
    m = re.search(r"### 5\.6 工具链速查(?P<tbl>.*?)(?:\n### |\n## |\Z)", text, flags=re.DOTALL)
    assert m, "USAGE 缺 §5.6 工具链速查"
    tbl = m.group("tbl")
    assert "deploy_qmt_work_agent.bat" in tbl, "§5.6 表未列 deploy_qmt_work_agent.bat"
    assert "diag_qmt_work_agent.bat" in tbl, "§5.6 表未列 diag_qmt_work_agent.bat"
    assert "--txt" in tbl, "§5.6 表未说明 --txt 参数"


def test_usage_has_common_errors_table():
    """USAGE §5.7 常见错误表必须存在且非空。"""
    text = USAGE_MD.read_text(encoding="utf-8")
    m = re.search(r"### 5\.7 常见部署错误(?P<body>.*?)(?:\n### |\n## |\Z)", text, flags=re.DOTALL)
    assert m, "USAGE 缺 §5.7 常见部署错误 → 解决方案"
    body = m.group("body")
    # 至少覆盖 5 条常见错误
    rows = [l for l in body.splitlines() if l.startswith("| ") and "|" in l[2:]]
    assert len(rows) >= 6, f"§5.7 只列了 {len(rows)} 行，覆盖不足"
    # 必须点出「日常排障第一名」
    assert "bridge_dir" in body, "§5.7 未覆盖「bridge_dir 路径不一致」这条头号排障"
    assert "注册树" in body, "§5.7 未覆盖「注册树未登记」这条头号症状"
