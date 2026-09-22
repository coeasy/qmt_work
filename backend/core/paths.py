"""数据目录校验的唯一入口（P0-3 II 数据目录可配置）。

## 为什么必须收敛成一个模块

目录来自**用户输入**（设置界面文本框 / Electron 目录选择框），是典型的外部输入面。
校验如果散落在各调用点，迟早出现「某一条路径绕过了检查」——而**一条绕过就等于没有检查**：
用户随手填了 `C:\\Windows\\System32`，主库或冷库就被写到系统目录里去了。
所以这里把「什么目录能写」收敛成一个函数，端点 / 服务 / 启动阶段都只调它。

## 被拒绝的四类

1. **空 / 纯空白**：`Path("")` 会退化成当前目录，是最隐蔽的一种「写错地方」；
2. **盘符根目录**（`C:\\`、`D:\\`、`/`）：允许写盘根 = 允许把整个盘当数据目录，
   备份、清理、迁移全部失去边界；
3. **系统目录**（Windows / Program Files / POSIX 的 /etc /usr /bin …）：
   轻则被 UAC 拦成「保存失败」，重则损坏系统；
4. **已存在但不是目录**（是个文件），或**写不进去**（父目录不可写 / 只读介质）。

## 与「安装目录」的关系：**提示，不是拒绝**

默认导出目录就在 exe 同目录（`<exe>/export`），所以「在安装目录里」**不能**拒绝，
只能提示「重装/卸载可能连带删除」。:func:`inside_install_dir` 只负责回答这个问题，
由调用方决定怎么展示。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "system_dir_roots", "is_system_dir", "is_drive_root", "inside_install_dir",
    "validate_dir", "describe_dir", "PathError",
]

#: Windows 上不允许作为数据目录的目录（由环境变量推导，取不到就退回字面量）。
_WIN_ENV_DIRS = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)",
                 "ProgramData", "CommonProgramFiles")

#: POSIX 上的系统目录前缀（前缀匹配，故 /usr 覆盖 /usr/local 等）。
_POSIX_SYSTEM_PREFIXES = ("/bin", "/sbin", "/etc", "/usr", "/boot", "/dev",
                          "/proc", "/sys", "/lib", "/lib64", "/opt", "/var/lib",
                          "/System", "/Library", "/Applications", "/private",
                          "/cores", "/srv")

#: Windows 文件名里非法、但 `Path()` 不会替你拦的字符（用户可能从别处粘贴进来）。
_WIN_BAD_CHARS = re.compile(r'[<>"|?*\x00-\x1f]')


class PathError(ValueError):
    """目录不可用。**继承 ValueError**：路由层已有的 `except ValueError` 会自动接住，
    不必为了这个新错误再改一遍所有调用点。"""


def _norm(p: Path | str) -> Path:
    """归一化：展开 `~`、转绝对路径、消掉 `..`。

    `strict=False`：目录**还不存在**是正常情况（正要创建它），不能因此报错。
    `resolve()` 在 Windows 上还会统一盘符大小写与 8.3 短名，
    否则 `c:\\windows` 与 `C:\\Windows` 会被判成两个不同目录，绕过系统目录检查。
    """
    return Path(os.path.expanduser(str(p))).resolve(strict=False)


def system_dir_roots() -> list[Path]:
    """当前平台上的系统目录集合（归一化）。取不到环境变量的条目自动跳过。"""
    roots: list[Path] = []
    if os.name == "nt":
        for name in _WIN_ENV_DIRS:
            raw = os.environ.get(name, "").strip()
            if raw:
                roots.append(_norm(raw))
    else:
        for prefix in _POSIX_SYSTEM_PREFIXES:
            p = Path(prefix)
            if p.exists():
                roots.append(_norm(p))
    return roots


def is_drive_root(p: Path | str) -> bool:
    """是不是盘符根目录（`C:\\` / `/`）。

    判据用 `parent == self`：只有根目录的父目录是它自己。
    比 `p == Path(p.anchor)` 稳，因为后者对 UNC 路径（`\\\\server\\share`）行为不一致。
    """
    n = _norm(p)
    return n.parent == n


def is_system_dir(p: Path | str) -> bool:
    """是不是（或位于）系统目录之下。"""
    n = _norm(p)
    for root in system_dir_roots():
        if n == root or root in n.parents:
            return True
    return False


def inside_install_dir(p: Path | str) -> bool:
    """是否位于程序安装目录之下（**只用于提示**「重装可能丢数据」，不作为拒绝理由）。

    安装目录 = `core.config.exe_dir()`：打包时是 exe 所在目录，开发时是 backend 根目录。
    这里用局部导入避免 `core.paths` ←→ `core.config` 的模块级循环（config 不 import paths，
    但保持单向依赖更清晰）。
    """
    from core.config import exe_dir
    n = _norm(p)
    root = _norm(exe_dir())
    return n == root or root in n.parents


def _nearest_existing(p: Path) -> Optional[Path]:
    """向上找第一个真实存在的祖先（用于判断「这个目录将来能不能被创建出来」）。"""
    for cand in (p, *p.parents):
        if cand.exists():
            return cand
    return None


def validate_dir(raw: Any, *, create: bool = False) -> Path:
    """校验并归一化一个**数据目录**；不可用则抛 :class:`PathError`（中文原因）。

    ``create=True`` 时会真的 `mkdir -p` —— 这是唯一可靠的「可写」证明：
    `os.access(W_OK)` 在 Windows 上对 ACL/只读介质经常给出乐观答案，
    与其事后在写文件时炸，不如在这里就把失败暴露给用户。

    ⚠️ 刻意**不**在这里判断「是否在安装目录里」：那是提示不是拒绝，
    调用方（界面）自己决定怎么展示。
    """
    s = str(raw or "").strip()
    if not s:
        raise PathError("目录不能为空")
    if os.name == "nt" and _WIN_BAD_CHARS.search(s):
        raise PathError("目录名含有 Windows 不允许的字符（< > \" | ? *）")

    p = _norm(s)

    if p.exists() and not p.is_dir():
        raise PathError(f"该路径已存在且不是目录：{p}")
    if is_drive_root(p):
        raise PathError(f"不允许把盘符根目录（{p}）作为数据目录，请选择其下的具体文件夹")
    if is_system_dir(p):
        raise PathError(f"不允许写入系统目录：{p}")

    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PathError(f"目录不可创建：{exc}") from exc
        if not os.access(p, os.W_OK):
            raise PathError(f"目录不可写（权限不足或只读介质）：{p}")
    elif not p.exists():
        base = _nearest_existing(p)
        if base is None or not os.access(base, os.W_OK):
            raise PathError(f"目录不存在且其上级不可写：{p}")
    elif not os.access(p, os.W_OK):
        raise PathError(f"目录不可写（权限不足或只读介质）：{p}")

    return p


def describe_dir(raw: Any, *, create: bool = False) -> dict:
    """给界面用的**不抛异常**版本：永远返回结构化结果。

    界面要能「边输边提示」，不能因为用户还在打字（半截路径）就收到 500。
    所以这里把 :func:`validate_dir` 的失败也变成数据的一部分。
    """
    s = str(raw or "").strip()
    out: dict[str, Any] = {
        "input": s,
        "ok": False,
        "reason": "",
        "path": "",
        "exists": False,
        "writable": False,
        "inside_install": False,
        "is_system": False,
        "is_drive_root": False,
    }
    try:
        p = validate_dir(s, create=create)
    except PathError as exc:
        out["reason"] = str(exc)
        # 即使被拒也尽量把「用户到底指了哪儿」回显出来，便于自查
        if s:
            try:
                cand = _norm(s)
                out["path"] = str(cand)
                out["exists"] = cand.exists()
                out["is_system"] = is_system_dir(cand)
                out["is_drive_root"] = is_drive_root(cand)
                out["inside_install"] = inside_install_dir(cand)
            except (OSError, ValueError):
                pass
        return out
    out.update({
        "ok": True,
        "path": str(p),
        "exists": p.exists(),
        "writable": os.access(p, os.W_OK),
        "inside_install": inside_install_dir(p),
        "is_system": False,
        "is_drive_root": False,
    })
    return out


def is_frozen() -> bool:
    """PyInstaller 打包运行时为 True（透传，便于界面区分「安装目录」的含义）。"""
    return bool(getattr(sys, "frozen", False))
