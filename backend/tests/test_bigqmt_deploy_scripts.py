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
    """读 .bat 文本，**按真实编码**解码。

    cmd 的批处理文件编码必须与文件里声明的 ``chcp`` 一致，否则中文行会被按错码页
    解析成乱码（实测：UTF-8 无 BOM 的中文 ``REM`` 行在 cp936 控制台下直接报
    「不是内部或外部命令」）。仓库里两种存法都出现过：``diag_*.bat`` 是 UTF-8 +
    ``chcp 65001``，``deploy_*.bat`` 是 GBK + ``chcp 936``。测试只关心**文本内容**，
    不该把某一种编码钉死 —— 这里按 utf-8 → gbk 依次尝试。

    :func:`test_bat_chcp_matches_file_encoding` 单独负责「编码 == 声明的代码页」。
    """
    raw = path.read_bytes()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _bat_encoding(path: pathlib.Path) -> str:
    """返回 .bat 的实际字节编码名（``utf-8`` / ``gbk``）。"""
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gbk"


@pytest.mark.parametrize(
    "path, must_contain",
    [
        (
            DEPLOY_BAT,
            [
                "python.exe",  # Python 解释器探测
                "qmt_agent_deploy.py",  # 部署脚本名
                "deploy",  # 部署子命令
                "--txt",  # 生成 .txt 副本
                "AGENT_FILE",  # 落盘文件名变量（= 注册树条目指向的文件名）
                "--filename",  # 显式把文件名传给 deploy（不再写死小写）
                "导入本地策略",  # 下一步指引
                "explorer /select",  # Windows 打开资源管理器选中文件
            ],
        ),
        (
            DIAG_BAT,
            [
                "chcp 936",  # 控制台码页（必须与文件字节编码一致，见 test_bat_chcp_*）
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


def test_bat_chcp_matches_file_encoding():
    """``chcp`` 声明的代码页必须与文件实际字节编码一致。

    ★ 取代旧断言「必须是 chcp 65001」。旧断言把编码**当成了目的**，真实目的只有
    一个：**中文行不被 cmd 按错码页解析**。实测（2026-10-09）：
      * **GBK + chcp 936** —— 解析期零码页转换，中文行/多行块全对（本仓采用）；
      * **UTF-8 + chcp 65001** —— 小文件能跑，但本仓这两支脚本中文较多、文件较大，
        cmd 的分块读取会把 UTF-8 多字节序列**从中间切断**，表现为随机行被当成命令
        执行（``'...' 不是内部或外部命令``）。原因：`chcp 65001` 生效之前，
        cmd 已按默认 cp936 缓冲解析了文件开头（含大量中文注释块）。
    所以中文 .bat 一律用 GBK + chcp 936，由本断言防回归。
    """
    for bat_name in ("deploy_qmt_work_agent.bat", "diag_qmt_work_agent.bat"):
        p = ROOT / bat_name
        text = _read_utf8(p)
        m = re.search(r"chcp\s+(\d+)", text)
        assert m, f"{bat_name} 未声明 chcp（中文输出会随宿主码页乱码）"
        cp = m.group(1)
        enc = _bat_encoding(p)
        want = {"65001": "utf-8", "936": "gbk"}.get(cp)
        assert want == enc, (
            f"{bat_name} 编码自相矛盾：文件字节是 {enc}，却声明 chcp {cp}"
            f"（应改成 chcp {65001 if enc == 'utf-8' else 936} 或按对应编码重存）")


def test_bat_is_portable_no_hardcoded_broker_paths():
    """一键脚本必须是**通行脚本**：不许写死任何券商专属安装路径。

    ★ 这条断言取代了旧的 ``test_bat_find_qmt_dirs_covers_common_paths`` —— 旧断言
    要求 bat 里**至少硬编码 3 个** ``if exist "...\\python"`` 探测点（含
    ``P:\\stock\\gd_qmt`` / ``C:\\光大证券金阳光远航版`` 这类本机路径），等于用测试
    把「换个券商或换台机器就失效」这个坑**锁死**。这与产品要求正好相反。

    通行做法：把目录发现交给 ``qmt_agent_deploy.py`` 的通用探测（逐盘符扫描 +
    ``python`` 策略目录 + 次级标记），bat 只负责「显式参数 → 环境变量 → 自动探测 →
    人工输入」四级降级。因此这里断言**没有**写死的绝对盘符路径，且四级降级都在。
    """
    for bat_name in ("deploy_qmt_work_agent.bat", "diag_qmt_work_agent.bat"):
        text = _read_utf8(ROOT / bat_name)
        # 不许出现 `X:\...\python` 形式的写死绝对路径探测
        baked = re.findall(r'if\s+exist\s+"[A-Za-z]:\\[^"]*\\python"', text)
        assert not baked, (
            f"{bat_name} 仍写死了券商专属路径 {baked} —— 换个安装环境就探测失败")
        # 也不许写死本机/券商标识串
        for bad in ("gd_qmt", "光大证券", "金阳光"):
            assert bad not in text, (
                f"{bat_name} 残留本机专属标识 {bad!r}（通行脚本不得绑定单一客户端）")
        # 四级降级链必须在：命令行参数 / 环境变量 / 自动探测 / 人工输入
        assert "%~1" in text, f"{bat_name} 缺少命令行传参支持"
        assert "QMT_DIR" in text, f"{bat_name} 缺少 QMT_DIR 环境变量/变量支持"
        assert "discover" in text, f"{bat_name} 未调用通用探测（qmt_agent_deploy.py discover）"
        assert "set /p QMT_DIR" in text, f"{bat_name} 缺少手动指定 QMT 目录的降级路径"


def test_bats_use_crlf_eol():
    """两支 .bat 必须是 **CRLF** 行尾，且已在 ``.gitattributes`` 登记 ``eol=crlf``。

    ★ 这是本文件最重要的回归锁。同一个坑项目已记录三次（TD-16 / TD-17 / TD-21），
    而 **2026-10-09 又踩了第四次**：``diag_qmt_work_agent.bat`` 长期以 **LF-only
    入库**（当时 ``.gitattributes`` 只登记了 ``build_all.bat``），交付出去后用户
    双击报「不是内部或外部命令」——中文行被 cmd 按整行拼接解析。

    cmd.exe 是**逐字**读 .bat 的：LF-only 会让多行并成一条逻辑行，括号块与中文行
    全部错位。仅在工作区修好不够（TD-17：仓库 blob 仍是 LF，新克隆照坏），
    必须在 ``.gitattributes`` 声明 ``<file>.bat text eol=crlf`` 才能让 git 在检出时
    强制还原 CRLF。
    """
    ga = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for bat_name in ("build_all.bat", "deploy_qmt_work_agent.bat",
                     "diag_qmt_work_agent.bat"):
        raw = (ROOT / bat_name).read_bytes()
        crlf = raw.count(b"\r\n")
        lf_only = raw.count(b"\n") - crlf
        assert lf_only == 0, (
            f"{bat_name} 含 {lf_only} 处 LF-only 行尾 —— cmd 会解析错位，"
            "必须改回 CRLF")
        assert crlf > 0, f"{bat_name} 没有 CRLF（文件疑似被整体重写为 LF）"
        assert re.search(rf"^{re.escape(bat_name)}\s+text\s+eol=crlf", ga, re.M), (
            f".gitattributes 未登记 `{bat_name} text eol=crlf` —— "
            "工作区修好也没用，新克隆 / CI / 源码包仍会拿到 LF 坏版本（TD-17）")


def test_deploy_bat_agent_filename_matches_registration_tree():
    """落盘文件名必须 = 注册树里那条策略指向的文件名（本机实测 QMT_WORK_AGENT.py）。

    R27/TD-37 教训：以前 bat 把 `qmt_work_agent.py`（小写）写死进三处（deploy 不传
    `--filename`、打印的「选文件」路径、`explorer /select`），而注册树里的条目指向的是
    大写 `QMT_WORK_AGENT.py`。结果「部署成功 + 列表里有 + 点了跑不起来」，
    排查方向被彻底带偏。现在三处必须由同一个变量驱动。
    """
    text = _read_utf8(DEPLOY_BAT)
    m = re.search(r'set\s+"AGENT_FILE=([^"]+)"', text)
    assert m, "deploy bat 未定义 AGENT_FILE 变量（文件名会在多处写死后再次漂移）"
    assert m.group(1).strip() == "QMT_WORK_AGENT.py", (
        "AGENT_FILE 必须与注册树条目一致（本机实测 QMT_WORK_AGENT.py），"
        f"当前为 {m.group(1)!r}")
    # ★ 三条消费路径都必须走**同一个变量**。分隔符无关紧要：脚本里用了
    #   `setlocal enabledelayedexpansion`，所以 `!AGENT_FILE!`（延迟展开）与
    #   `%AGENT_FILE%`（解析期展开）都是合法引用 —— 断言只认变量名。
    assert re.search(r'--filename\s+"[%!]AGENT_FILE[%!]"', text), (
        "deploy 调用未显式传 --filename（或未用 AGENT_FILE 变量）")
    assert re.search(r'explorer\s+/select,"[^"]*[%!]AGENT_FILE[%!]"', text), (
        "explorer 选中路径未使用 AGENT_FILE")
    assert re.search(r'选文件[^\r\n]*[%!]AGENT_FILE[%!]', text), (
        "打印的「选文件」路径未使用 AGENT_FILE")
    # ★ 除定义行与注释/echo 外，**命令**里不许再出现写死的字面文件名（大小写不敏感）。
    #   注释与 echo 里提到默认值是文档，不影响行为，故豁免。
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.upper().startswith("REM") or s.lower().startswith("echo"):
            continue
        if "AGENT_FILE=" in s.upper():
            continue
        assert "qmt_work_agent.py" not in s.lower(), (
            f"命令里残留写死的小写 qmt_work_agent.py —— 会被 QMT 当成另一个不存在的策略: {s}")


def test_deploy_script_check_and_register_respect_filename():
    """`check` / `register` 不许把 bundle 文件名写死（否则巡检对着不存在的路径报缺失）。"""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "def cmd_check(args)" in text and "def cmd_register(args)" in text
    for fn, sig in (("cmd_check", "args.strategy_dir or strategy_dir(qmt)"),
                    ("cmd_register", "args.strategy_dir or strategy_dir(qmt)")):
        body = re.search(
            r"def %s\(args\):(?P<body>.*?)(?:\n\ndef |\Z)" % fn, text,
            flags=re.DOTALL)
        assert body, f"{fn} 未找到"
        assert sig in body.group("body"), f"{fn} 未复用统一策略目录解析"
        assert "getattr(args, \"filename\"" in body.group("body"), (
            f"{fn} 未读取 args.filename（文件名会写死）")


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


# --------------------------------------------------------------------------
# B8（2026-10-09）：拉起 QMT 内置解释器时必须剥掉宿主的 PYTHON* 注入
# --------------------------------------------------------------------------

LOCAL_RUN = ROOT / "scripts" / "qmt_agent_local_run.py"


def _load_local_run():
    """把 scripts/qmt_agent_local_run.py 当模块加载（它不在 sys.path 上）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_qmt_local_run_under_test", LOCAL_RUN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_child_env_strips_python_injection(monkeypatch):
    """★ B8：宿主 `PYTHONPATH` 会被 QMT 的 py3.6 **一并导入**，必须剥掉。

    真机实证：本机 shell 的 `PYTHONPATH` 指向一个为 py3.7+ 写的 shim，
    QMT 的 Python 3.6.8 启动策略后立刻
    `TypeError: __init__() got an unexpected keyword argument 'capture_output'`
    → `return code:1`（在客户端里就是「点运行 → 立刻停止」）。
    """
    mod = _load_local_run()
    monkeypatch.setenv("PYTHONPATH", r"C:/some/shim")
    monkeypatch.setenv("PYTHONHOME", r"C:/py")
    monkeypatch.setenv("PYTHONSTARTUP", r"C:/x.py")
    monkeypatch.setenv("PYTHONEXECUTABLE", r"C:/x.exe")
    env = mod._child_env()
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        assert key not in env, key


def test_child_env_keeps_path_and_systemroot(monkeypatch):
    """反向锚：**绝不能整体丢弃 os.environ** —— 丢 PATH 连 pythonw.exe 都起不来。

    注意 Windows 下 ``os.environ`` 的键被规范化为**大写**，故断言用 ``SYSTEMROOT``
    （写 ``SystemRoot`` 会取不到 —— 这本身就是环境键大小写的一个真实坑）。
    """
    mod = _load_local_run()
    monkeypatch.setenv("PATH", r"C:/Windows/System32")
    monkeypatch.setenv("SystemRoot", r"C:/Windows")
    env = mod._child_env()
    assert env.get("PATH") == r"C:/Windows/System32"
    assert env.get("SYSTEMROOT") == r"C:/Windows"
    # 反向锚：被剥的键**只有** PYTHON*，其余键一律原样保留
    assert env.get("PYTHONPATH") is None


def test_run_process_uses_child_env(monkeypatch, tmp_path):
    """`run_process` 必须走 `_child_env()`（回归：别退回 `dict(os.environ)`）。"""
    mod = _load_local_run()
    captured: dict[str, dict] = {}

    class _FakeProc:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return 0            # 立刻「已退出」⇒ 后续判定短路，测试只需看 env

        def communicate(self, timeout=None):  # pragma: no cover - 兜底
            return b"", b""

        def kill(self):         # pragma: no cover
            pass

    def _fake_popen(argv, **kw):
        captured["env"] = kw.get("env") or {}
        return _FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", _fake_popen)
    monkeypatch.setenv("PYTHONPATH", r"C:/bad/shim")
    bundle = tmp_path / "QMT_WORK_AGENT.py"
    bundle.write_text("x = 1\n", encoding="utf-8")
    mod.run_process(str(bundle), str(tmp_path / "wd"), "pythonw.exe", "", max_seconds=1.0)
    assert "PYTHONPATH" not in captured.get("env", {}), captured


# --------------------------------------------------------------------------
# B7（2026-10-09）：inspect --force 必须在文件被独占锁时**优雅降级**
# --------------------------------------------------------------------------

def test_inspect_force_degrades_on_locked_file(monkeypatch, tmp_path, capsys):
    """★ B7：QMT 运行期注册树被整文件锁 —— `inspect --force` 必须降级而不是崩溃。

    旧实现 `raw = open(src,'rb').read()` 没有 try 包裹，直接 PermissionError
    以 traceback 退出：另一个文件的信息拿不到，用户也看不出「是锁、不是损坏」。
    同时**必须保留 `PermissionError` 字样**（qmt_diag_report.py 依赖它汇总问题）。
    """
    import importlib.util
    import types

    spec = importlib.util.spec_from_file_location("_deploy_mod_under_test", DEPLOY_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    qmt = tmp_path / "gd_qmt"
    root = qmt / "config" / "user" / "root"
    root.mkdir(parents=True)
    (root / "configFormula").write_bytes(b"XTF1")
    (root / "UiSettingConfig").write_bytes(b"XTF1")

    monkeypatch.setattr(mod, "find_qmt_dir", lambda d: str(qmt))
    monkeypatch.setattr(mod, "qmt_running", lambda: ["XtItClient.exe"])

    def _boom(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(mod.shutil, "copy2", _boom)
    monkeypatch.setattr(mod, "_ROOT", str(tmp_path))

    args = types.SimpleNamespace(qmt_dir=str(qmt), force=True)
    rc = mod.cmd_inspect(args)
    out = capsys.readouterr().out
    assert rc == 0, "锁住的注册树属预期限制，不该让整条命令失败"
    assert "PermissionError" in out, "必须保留关键字供 diag_report 汇总"
    assert "configFormula" in out and "UiSettingConfig" in out, out
    # 反向锚：不得把异常漏出去变成 traceback
    assert "Traceback" not in out
