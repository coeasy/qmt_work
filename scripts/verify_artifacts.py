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


def components() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    lock = ROOT / "frontend" / "package-lock.json"
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
    args = parser.parse_args()
    backend = ROOT / "backend" / "dist" / "qmt_work"
    release = ROOT / "frontend" / "release"
    artifact_paths = [p for p in files(backend) + files(release)
                      if p.name != "release-manifest.json"]
    if args.require_client and (not backend.exists() or not release.exists()):
        print("artifact verification failed: build outputs are missing", file=sys.stderr)
        return 1
    if args.require_client and (not any(p.suffix.lower() == ".exe" for p in artifact_paths)
                                or not any(p.suffix.lower() == ".zip" for p in artifact_paths)):
        print("artifact verification failed: executable and zip are both required", file=sys.stderr)
        return 1
    manifest = {
        "schema": "qmt_work.artifact-manifest.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_revision": os.environ.get("GITHUB_SHA", "local"),
        "artifacts": [
            {"path": str(path.relative_to(ROOT)).replace("\\", "/"),
             "size": path.stat().st_size, "sha256": sha256(path)}
            for path in artifact_paths
        ],
        "sbom": components(),
    }
    release.mkdir(parents=True, exist_ok=True)
    output = release / "release-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"artifact manifest written: {output} ({len(artifact_paths)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
