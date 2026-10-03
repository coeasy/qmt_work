#!/usr/bin/env python3
"""Create and verify a reproducible desktop artifact manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _version() -> str:
    """P2-28：版本单一来源 = 仓库根 VERSION 文件。"""
    version_file = ROOT / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _toolchain() -> dict:
    tc = ROOT / "TOOLCHAIN.json"
    try:
        return json.loads(tc.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pyproject_version() -> str | None:
    """P2-29：读 ``backend/pyproject.toml`` 的 ``[project].version``。

    用正则而不是 ``tomllib``：本脚本要在最小环境里跑（release 校验常在刚
    checkout 的干净机器上执行），且这里只需要一个标量。
    """
    path = ROOT / "backend" / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        match = re.match(r'^\s*version\s*=\s*["\']([^"\']+)["\']', line)
        if match:
            return match.group(1)
    return None


def _json_version(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("version")
    return value if isinstance(value, str) else None


def _lock_version() -> str | None:
    """package-lock.json 的版本在两个位置，两处都算（TD-30 同源）。"""
    lock = ROOT / "frontend-next" / "package-lock.json"
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    root_pkg = data.get("packages", {}).get("", {}).get("version")
    if isinstance(root_pkg, str):
        return root_pkg
    value = data.get("version")
    return value if isinstance(value, str) else None


def _version_drift() -> list[str]:
    """P2-29：版本真源一致性闸门。

    2026-10-03 发现的真缺陷：``backend/pyproject.toml`` 长期停在 ``0.1.0-beta.1``，
    而 VERSION / package.json / package-lock.json 都已是 ``0.4.3``（漂移 4 个版本）。
    全仓没有 ``importlib.metadata.version("qmt-work")`` 读取方，三个 workflow 也
    不引用它，所以**从来没有任何 CI 会报错**——但它是依赖声明的真源，未来一旦
    接入 ``pip install -e`` 或 wheel 打包，就会打出错误版本号的包。

    返回不一致项列表（空 = 全一致）。缺文件不计入漂移（允许部分产物未检出）。
    """
    expected = _version()
    if expected in ("", "unknown"):
        return ["VERSION 文件缺失或不可读，无法作为版本真源"]
    found: dict[str, str | None] = {
        "backend/pyproject.toml": _pyproject_version(),
        "frontend-next/package.json": _json_version(ROOT / "frontend-next" / "package.json"),
        "frontend-next/package-lock.json": _lock_version(),
    }
    return [f"{name} = {value}（期望 {expected}）"
            for name, value in found.items() if value != expected]


def components() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    lock = ROOT / "frontend-next" / "package-lock.json"
    if lock.exists():
        data = json.loads(lock.read_text(encoding="utf-8"))
        deps = data.get("packages", {}).get("", {}).get("dependencies", {})
        result.extend({"ecosystem": "npm", "name": name, "version": str(version)}
                      for name, version in sorted(deps.items()))
    requirements = ROOT / "backend" / "requirements.txt"
    if requirements.exists():
        for line in requirements.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*([^; ]*)", line)
            if match and not line.lstrip().startswith("#"):
                result.append({"ecosystem": "pip", "name": match.group(1),
                               "version": match.group(2)})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-client", action="store_true")
    parser.add_argument("--portable-only", action="store_true",
                        help="只要求当前版本的便携 zip，不要求 NSIS Setup exe"
                             "（build-client.yml 便携包流水线专用；release.yml "
                             "两种产物都打，不加此参数维持 exe+zip 双闸）。")
    args = parser.parse_args()
    # P2-29：版本真源一致性 —— 与产物是否存在无关，任何情况下都必须先过这一关。
    drift = _version_drift()
    if drift:
        print("artifact verification failed: 版本真源不一致\n  " + "\n  ".join(drift),
              file=sys.stderr)
        return 1
    backend = ROOT / "backend" / "dist" / "qmt_work"
    release = ROOT / "frontend-next" / "dist-electron"
    artifact_paths = [p for p in files(backend) + files(release)
                      if p.name != "release-manifest.json"]
    if args.require_client and (not backend.exists() or not release.exists()):
        print("artifact verification failed: build outputs are missing", file=sys.stderr)
        return 1
    # ★ 必须按**当前版本**判定，否则残留的旧版本产物（dist-electron 里可能同时躺着
    #   0.3.5/0.3.6/… 的包）会让「有 exe 有 zip」在**本轮什么都没产出**时也判过。
    #   版本真源：仓库根 VERSION（与 release.yml 的一致性闸门同源）。
    version = _version()
    client_paths = [p for p in artifact_paths if version and version in p.name]
    need_exe = not args.portable_only
    if args.require_client and ((need_exe and not any(p.suffix.lower() == ".exe" for p in client_paths))
                                or not any(p.suffix.lower() == ".zip" for p in client_paths)):
        what = "安装包与 zip 均为必需" if need_exe else "便携 zip 为必需"
        print(f"artifact verification failed: version {version} 的{what}"
              f"（dist-electron 内找到的 {version} 产物：{[p.name for p in client_paths] or '无'}）",
              file=sys.stderr)
        return 1
    manifest = {
        "schema": "qmt_work.artifact-manifest.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": _version(),
        "source_revision": os.environ.get("GITHUB_SHA", "local"),
        "artifacts": [
            {"path": str(path.relative_to(ROOT)).replace("\\", "/"),
             "size": path.stat().st_size, "sha256": sha256(path)}
            for path in artifact_paths
        ],
        "sbom": components(),
        "toolchain": _toolchain(),
    }
    release.mkdir(parents=True, exist_ok=True)
    output = release / "release-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"artifact manifest written: {output} ({len(artifact_paths)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
