"""ABI 运行时选择单元测试（不依赖真实券商/xtquant）。

通过合成目录模拟券商 xtquant .cpXXX .pyd 与随包嵌入式运行时，验证：
- detect_xtquant_abis 扫描 ABI 变体（含子目录 xtquant/）
- discover_bundled_runtimes 枚举 runtimes/cpXXX/python.exe
- select_runtime 进程内直连 / 桥接 择机逻辑
- require_runtime_or_raise 无匹配时给出可操作错误
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from xtquant_client.runtime import (  # noqa: E402
    detect_xtquant_abis, discover_bundled_runtimes, select_runtime,
    require_runtime_or_raise, ABINotSupportedError, host_python_minor,
    discover_system_runtimes, _merge_runtimes)


def _mk_xtquant(root: str, abis: list[int], subdir: bool = False) -> str:
    if subdir:
        d = os.path.join(root, "xtquant")
        os.makedirs(d, exist_ok=True)
    else:
        d = root
        os.makedirs(d, exist_ok=True)
    for m in abis:
        with open(os.path.join(d, f"IPythonApiClient.cp{m}-win_amd64.pyd"), "wb") as f:
            f.write(b"\x00\x00")
    return root


def test_detect_xtquant_abis_direct():
    with tempfile.TemporaryDirectory() as td:
        site = _mk_xtquant(td, [311, 310, 38])
        # 归一化后：cp38 -> 308, cp310 -> 310, cp311 -> 311
        assert detect_xtquant_abis(site) == [308, 310, 311]


def test_detect_xtquant_abis_subdir():
    with tempfile.TemporaryDirectory() as td:
        site = _mk_xtquant(td, [39], subdir=True)
        assert detect_xtquant_abis(site) == [309]


def test_detect_xtquant_abis_missing():
    with tempfile.TemporaryDirectory() as td:
        assert detect_xtquant_abis(td) == []
        assert detect_xtquant_abis(None) == []


def test_discover_bundled_runtimes():
    with tempfile.TemporaryDirectory() as td:
        rt = os.path.join(td, "runtimes")
        os.makedirs(os.path.join(rt, "cp311"))
        with open(os.path.join(rt, "cp311", "python.exe"), "wb") as f:
            f.write(b"MZ")
        found = discover_bundled_runtimes(rt)
        assert found == {311: os.path.join(rt, "cp311", "python.exe")}


def test_select_runtime_in_process():
    host = host_python_minor()
    with tempfile.TemporaryDirectory() as td:
        site = _mk_xtquant(td, [host, 38])
        sel = select_runtime(site)
        assert sel is not None
        assert sel["mode"] == "in_process"
        assert sel["python_exe"] == sys.executable


def test_select_runtime_bridge():
    # 券商仅提供 cp38，主后端（host）不在集合 -> 选 bundled cp38 桥接
    with tempfile.TemporaryDirectory() as td:
        site = _mk_xtquant(td, [38])
        rt = os.path.join(td, "runtimes")
        os.makedirs(os.path.join(rt, "cp38"))
        with open(os.path.join(rt, "cp38", "python.exe"), "wb") as f:
            f.write(b"MZ")
        bundled = discover_bundled_runtimes(rt)
        sel = select_runtime(site, bundled=bundled)
        assert sel is not None
        assert sel["mode"] == "bridge"
        assert sel["abi"] == 308


def test_select_runtime_bridge_picks_highest():
    # bundled 含 cp38/cp311，券商支持 cp38/cp310/cp311 -> 选最高的 cp311
    with tempfile.TemporaryDirectory() as td:
        site = _mk_xtquant(td, [38, 310, 311])
        rt = os.path.join(td, "runtimes")
        for m in (38, 311):
            os.makedirs(os.path.join(rt, f"cp{m}"))
            with open(os.path.join(rt, f"cp{m}", "python.exe"), "wb") as f:
                f.write(b"MZ")
        bundled = discover_bundled_runtimes(rt)
        sel = select_runtime(site, bundled=bundled)
        assert sel["mode"] == "bridge"
        assert sel["abi"] == 311


def test_require_runtime_raises_when_no_match():
    with tempfile.TemporaryDirectory() as td:
        site = _mk_xtquant(td, [38])  # 仅 cp38
        # 主后端 host 不在，且 bundled 为空 -> 无兼容
        try:
            require_runtime_or_raise(site, bundled={})
            assert False, "应当抛出 ABINotSupportedError"
        except ABINotSupportedError as exc:
            assert exc.broker_abis == [308]  # 归一化后 cp38 -> 308
            assert "backend/runtimes" in str(exc)


def test_merge_runtimes_prefers_bundled():
    """bundled 运行时优先于系统运行时（即使二者 ABI 相同）。"""
    with tempfile.TemporaryDirectory() as td:
        bundled = {311: os.path.join(td, "bundled", "python.exe")}
        merged = _merge_runtimes(bundled)
        # 即使系统里也有 cp311，merged[311] 仍应来自 bundled
        assert merged.get(311) == bundled[311]


def test_merge_runtimes_falls_back_to_system():
    """bundled 为空时，系统运行时进入合并集（如系统装了 cp311）。"""
    merged = _merge_runtimes({})
    # 结果应为非负小版本集合，且不包含主后端自身（除非系统检测也找到）
    assert all(isinstance(k, int) and 308 <= k <= 313 for k in merged)


def test_discover_system_runtimes_smoke():
    """discover_system_runtimes 至少返回一个可用版本（主后端自身通常也在 PATH）。"""
    sys_runtimes = discover_system_runtimes()
    assert isinstance(sys_runtimes, dict)
    # 主后端 python 本身应被收录（managed 3.13 在 PATH 或由 py 启动器暴露）
    assert host_python_minor() in sys_runtimes or len(sys_runtimes) >= 1


def test_select_runtime_uses_system_when_bundled_empty():
    """券商 cp311，无 bundled 运行时，但系统检测到 cp311 -> 选系统运行时桥接。"""
    from xtquant_client import runtime as runtime_mod
    with tempfile.TemporaryDirectory() as td:
        site = _mk_xtquant(td, [311])
        # 模拟系统运行时有 cp311（用主后端自身作 stub，避免真实 spawn）
        fake_exe = sys.executable
        runtime_mod._SYSTEM_RUNTIME_CACHE = {311: fake_exe}
        try:
            sel = select_runtime(site, bundled={})
            assert sel is not None
            assert sel["mode"] == "bridge"
            assert sel["abi"] == 311
            assert sel["python_exe"] == fake_exe
        finally:
            runtime_mod._SYSTEM_RUNTIME_CACHE = None


def test_require_runtime_or_raise_actionable_message():
    """无兼容运行时时的错误信息应含「任择其一」及具体 Python 版本与 PATH 指引。"""
    with tempfile.TemporaryDirectory() as td:
        site = _mk_xtquant(td, [311])
        # 系统缓存清空，强制无兼容运行时
        runtime_mod = __import__("xtquant_client.runtime", fromlist=["_SYSTEM_RUNTIME_CACHE"])
        runtime_mod._SYSTEM_RUNTIME_CACHE = {}
        try:
            try:
                require_runtime_or_raise(site, bundled={}, prefer_bridge=True)
                assert False, "应当抛出 ABINotSupportedError"
            except ABINotSupportedError as exc:
                msg = str(exc)
                assert "任选其一" in msg
                assert "Python 3.11" in msg
                assert "PATH" in msg
                assert exc.broker_abis == [311]
        finally:
            runtime_mod._SYSTEM_RUNTIME_CACHE = None


def test_detect_xtquant_abis_empty_when_no_pyd():
    """xtquant 目录存在但无 .pyd 文件时返回空列表（不报异常）。"""
    with tempfile.TemporaryDirectory() as td:
        assert detect_xtquant_abis(td) == []


def test_select_runtime_none_when_no_broker_abis():
    """券商 xtquant 目录不存在 .pyd 时，select_runtime 返回 None（无 ABI 可匹配）。"""
    with tempfile.TemporaryDirectory() as td:
        assert select_runtime(td) is None
