"""本地诊断工具：检查 QMT / MiniQMT 客户端目录与 xtquant 可用性（无需启动平台）。

用法（在本仓库 backend 目录下执行）：
    python tools/diag_qmt.py "C:\\国金证券QMT交易端\\userdata_mini"
    python tools/diag_qmt.py                          # 不填参数则尝试常见安装位置

输出内容：
- client_path 是否存在、候选根、bin.x64/userdata_mini 标记
- xtquant 是否定位、能否导入（含 ABI 错误原文）
- 关键目录一览（根下前两层），便于人工核对结构差异
- 修复建议（hint）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xtquant_client.xtp import (  # noqa: E402
    _is_likely_root,
    probe_environment,
)

# 常见迅投系客户端安装位置（探测时自动尝试）
COMMON_ROOTS = [
    r"C:\国金证券QMT交易端",
    r"C:\华鑫证券\奇点QMT交易端",
    r"C:\银河证券QMT交易端",
    r"C:\中信建投QMT交易端",
    r"C:\兴业证券QMT交易端",
    r"C:\广发证券QMT交易端",
    r"C:\QMT",
    r"C:\迅投QMT",
    r"D:\国金证券QMT交易端",
    r"D:\QMT",
]


def _find_auto_client() -> str | None:
    """自动扫描常见安装位置（取第一个含 userdata_mini 或 bin.x64 的根）。"""
    for root in COMMON_ROOTS:
        if os.path.isdir(root) and (_is_likely_root(root)
                                    or os.path.isdir(os.path.join(root, "userdata_mini"))):
            return os.path.join(root, "userdata_mini")
    # 兜底：扫描 C:\ 与 D:\ 下形如 *QMT* 的目录（仅一层）
    for drive in ("C:", "D:"):
        if not os.path.isdir(drive + "\\"):
            continue
        try:
            for name in os.listdir(drive + "\\"):
                if "qmt" in name.lower():
                    p = os.path.join(drive + "\\", name)
                    if os.path.isdir(p) and (_is_likely_root(p)
                                             or os.path.isdir(os.path.join(p, "userdata_mini"))):
                        return os.path.join(p, "userdata_mini")
        except OSError:
            continue
    return None


def _dump_tree(root: str, max_depth: int = 2, max_items: int = 12) -> list[str]:
    """列出根下前两层目录（排除 __pycache__ 等），便于核对结构。"""
    lines: list[str] = []
    if not root or not os.path.isdir(root):
        return lines
    skip = {"__pycache__", ".git", "$recycle.bin", "system volume information"}
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return lines
    for name in entries[:max_items]:
        if name in skip:
            continue
        p = os.path.join(root, name)
        if os.path.isdir(p):
            lines.append(f"  {name}\\")
            if max_depth > 1:
                try:
                    sub = sorted(os.listdir(p))[:6]
                except OSError:
                    sub = []
                for s in sub:
                    if s not in skip:
                        lines.append(f"    {s}\\")
        else:
            lines.append(f"  {name}")
    if len(entries) > max_items:
        lines.append(f"  … 共 {len(entries)} 项")
    return lines


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    client_path = args[0] if args else None
    print("=" * 68)
    print("qmt_work · QMT/MiniQMT 客户端诊断（无需启动平台）")
    print(f"Python 版本：{sys.version.split()[0]}（{sys.executable}）")
    print("=" * 68)

    if not client_path:
        client_path = _find_auto_client()
        if client_path:
            print(f"[自动扫描] 未指定 client_path，发现：{client_path}")
        else:
            print("[自动扫描] 常见安装位置未发现客户端，请显式传入 client_path")

    diag = probe_environment(client_path)
    print(f"\n■ client_path    : {diag['client_path'] or '（空）'}")
    print(f"■ 目录存在        : {'是' if diag['client_exists'] else '否'}")
    print(f"■ 候选根          : {diag['candidate_roots'][:4]}")
    print(f"■ 含 userdata_mini: {'是' if diag['has_userdata_mini'] else '否'}")
    print(f"■ 含 bin.x64      : {'是' if diag['has_bin_x64'] else '否'}")
    print(f"■ xtquant 定位    : {diag['xtquant_site'] or '未找到'}")
    print(f"■ xtquant 可导入  : {'是' if diag['xtquant_importable'] else '否'}")
    if diag.get("import_error"):
        print(f"■ 导入错误        : {diag['import_error'][:300]}")
    print(f"▶ 结论            : {diag['hint'] or '（无）'}")

    # 目录树：候选根中第一个存在的
    shown = False
    for r in diag.get("candidate_roots", []):
        if os.path.isdir(r) and not _is_system_dir_name(r):
            print(f"\n■ {r}\\ 目录结构（前两层）：")
            for line in _dump_tree(r):
                print(line)
            shown = True
            break
    if not shown and client_path and os.path.isdir(client_path):
        print(f"\n■ {client_path}\\ 目录结构（前两层）：")
        for line in _dump_tree(client_path):
            print(line)

    # ABI 提示
    if not diag["xtquant_importable"] and diag["xtquant_found"]:
        print("\n⚠ 若报 DLL load failed / ImportError：xtquant 扩展与当前 Python ABI 不兼容。")
        print("  请用 ≤3.12 的 Python 运行平台，或 pip install xtquant 到当前环境，")
        print("  或把平台放到客户端自带 python（bin.x64\\Python\\python.exe）下运行。")
    return 0 if diag["xtquant_importable"] else 1


def _is_system_dir_name(p: str) -> bool:
    base = os.path.basename(p).strip().lower()
    return base in {"appdata", "programdata", "temp", "tmp", "windows", "users",
                    "program files", "program files (x86)"}


if __name__ == "__main__":
    sys.exit(main())
