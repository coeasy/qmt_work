"""版本 / 许可证一致性门禁（P0-4）。

单一真源是仓库根 ``VERSION``；其余位置只能**生成或校验**，不允许手工漂移。

检查项：
1. ``backend/pyproject.toml``：version 为动态（读 VERSION）或与 VERSION 相同；
2. ``frontend-next/package.json``：version 与 VERSION 相同；
3. ``backend/pyproject.toml`` 的 license 与根 ``LICENSE`` 首部一致；
4. 产物中的 ``VERSION``（若存在 dist 目录）与源一致。

用法：``python scripts/check_version_consistency.py``，失败返回 1。
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
PYPROJECT = ROOT / "backend" / "pyproject.toml"
PACKAGE_JSON = ROOT / "frontend-next" / "package.json"
LICENSE = ROOT / "LICENSE"

#: license 字段 → LICENSE 文本必须包含的关键词
_LICENSE_HINTS = {
    "Apache-2.0": ("Apache License", "Version 2.0"),
    "MIT": ("MIT License",),
    "BSD-3-Clause": ("BSD 3-Clause",),
}


def _read_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _check_pyproject(want: str, errors: list[str]) -> None:
    try:
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"无法解析 {PYPROJECT.relative_to(ROOT)}：{exc}")
        return
    project = data.get("project") or {}
    version = str(project.get("version") or "")
    dynamic = [str(x) for x in (project.get("dynamic") or [])]
    if version and version != want:
        errors.append(
            f"backend/pyproject.toml version={version} != VERSION={want}"
            "（请改为 dynamic = [\"version\"]，不要在 pyproject 手写版本）")
    if not version and "version" not in dynamic:
        errors.append("backend/pyproject.toml 既无 version 也未声明 dynamic=[\"version\"]")


def _check_package(want: str, errors: list[str]) -> None:
    try:
        data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"无法解析 frontend-next/package.json：{exc}")
        return
    got = str(data.get("version") or "")
    if got != want:
        errors.append(f"frontend-next/package.json version={got} != VERSION={want}")


def _check_license(errors: list[str]) -> None:
    try:
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        declared = (data.get("project") or {}).get("license")
    except (OSError, tomllib.TOMLDecodeError):
        return
    text = ""
    if isinstance(declared, dict):
        text = str(declared.get("text") or "")
    elif isinstance(declared, str):
        text = declared
    if not text:
        errors.append("backend/pyproject.toml 未声明 license（必须与根 LICENSE 一致）")
        return
    try:
        head = "\n".join(LICENSE.read_text(encoding="utf-8", errors="replace").splitlines()[:40])
    except OSError as exc:
        errors.append(f"无法读取 LICENSE：{exc}")
        return
    hints = _LICENSE_HINTS.get(text)
    if hints and not all(h in head for h in hints):
        errors.append(
            f"pyproject license={text}，但根 LICENSE 首部不是该许可证"
            "（法务口径必须一致，不能一处 Apache 一处 Proprietary）")


def _check_bundled(want: str, errors: list[str]) -> None:
    """产物内 VERSION（存在才查），避免“包是旧的、源是新的”。"""
    for candidate in (ROOT / "dist" / "VERSION", ROOT / "backend" / "dist" / "VERSION"):
        if not candidate.is_file():
            continue
        got = candidate.read_text(encoding="utf-8").strip()
        if got != want:
            errors.append(f"{candidate.relative_to(ROOT)}={got} != VERSION={want}（产物未重新构建）")


def main() -> int:
    errors: list[str] = []
    want = _read_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", want):
        errors.append(f"VERSION 格式非法：{want!r}")
    _check_pyproject(want, errors)
    _check_package(want, errors)
    _check_license(errors)
    _check_bundled(want, errors)

    if errors:
        print("版本/许可证一致性检查失败：")
        for e in errors:
            print("  -", e)
        return 1
    print(f"版本/许可证一致性检查通过：{want}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
