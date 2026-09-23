#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布当前版本到 GitHub Release（幂等，可重复执行）。

背景
----
GitHub 会在推送 **annotated tag** 时自动创建一个 Release，标题 = tag 名、
正文 = tag 注解消息。这种自动 Release **只带 Source code，没有任何二进制资产**，
所以「tag 推上去了」并不等于「版本发布完了」—— 安装包必须另外上传。

本脚本负责补齐这一步：更新 Release 正文（Release Notes）+ 上传安装包资产。

用法
----
1) 准备凭据（二选一，需要 `repo` 权限的 classic token 或 `contents:write` 的 fine-grained token）::

       # cmd
       set GITHUB_TOKEN=ghp_xxxxxxxx
       # PowerShell
       $env:GITHUB_TOKEN="ghp_xxxxxxxx"

2) 干跑：只做本地自检，**不联网**（推荐先跑这一步）::

       backend/runtimes/cp311/python.exe scripts/publish_release.py --dry-run

3) 真跑：更新正文 + 上传资产::

       backend/runtimes/cp311/python.exe scripts/publish_release.py

幂等性
------
* Release 已存在（例如由 annotated tag 自动创建）-> 只 PATCH 正文，不重建；
* Release 不存在 -> POST 创建（`draft=false`）；
* 同名资产已存在 -> 先 DELETE 再上传（避免 422 already_exists）；
* `--dry-run` 全程不写远端。

注意
----
* 资产最大 236 MB 级，上传耗时数分钟；请用后台方式运行，别用前台短超时。
* 本脚本显式**禁用代理**（本机环境有代理会干扰 github.com 直连）。
* 只依赖标准库（urllib），不需要 requests。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

API_ROOT = "https://api.github.com"
UPLOAD_ROOT = "https://uploads.github.com"

# 需要上传的资产：按 electron-updater 的依赖顺序
ASSET_SUFFIXES = (".exe", ".zip", ".yml", ".blockmap")

# Release 正文的来源文件（相对仓库根）；不存在则不改正文
NOTES_DIR = "docs/release-notes"

UPLOAD_TIMEOUT = 3600  # 秒；236MB 上传留足余量
API_TIMEOUT = 60


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> "None":
    log(f"[FAIL] {msg}")
    raise SystemExit(code)


def repo_root() -> Path:
    """从本脚本位置向上找带 VERSION 的仓库根。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "VERSION").is_file():
            return parent
    die("找不到仓库根（向上未发现 VERSION 文件）")


def git_slug(root: Path) -> str:
    """从 origin remote 解析 owner/repo（兼容 ssh 与 https 两种写法）。"""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(root), capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        die(f"读取 origin remote 失败：{exc}")

    # git@github.com:owner/repo.git  |  https://github.com/owner/repo.git
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?$", out)
    if not m:
        die(f"无法从 origin 解析 owner/repo：{out}")
    return f"{m.group(1)}/{m.group(2)}"


def sha512_b64(path: Path) -> str:
    """electron-builder 用的 base64(sha512)。"""
    import base64
    h = hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode("ascii")


def opener() -> urllib.request.OpenerDirector:
    """显式禁用代理（本机环境代理会干扰 github.com）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def api_call(
    method: str,
    url: str,
    token: str,
    payload: "dict | None" = None,
) -> "tuple[int, dict]":
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "qmt-work-release",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener().open(req, timeout=API_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:  # noqa: BLE001
            parsed = {"message": body[:400]}
        return exc.code, parsed


def upload_asset(upload_url: str, token: str, path: Path) -> "tuple[int, dict]":
    """流式上传单个资产（不把 236MB 读进内存）。"""
    base = upload_url.split("{")[0]
    url = f"{base}?{urllib.parse.urlencode({'name': path.name})}"
    size = path.stat().st_size
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "qmt-work-release",
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
    }
    with path.open("rb") as fh:
        req = urllib.request.Request(url, data=fh, headers=headers, method="POST")
        try:
            with opener().open(req, timeout=UPLOAD_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:  # noqa: BLE001
                parsed = {"message": body[:400]}
            return exc.code, parsed


# ---------------------------------------------------------------------------
# 本地自检
# ---------------------------------------------------------------------------

def parse_latest_yml(text: str) -> "dict":
    """把 latest.yml 归一化成扁平 dict，字段缺失时**显式置 None**。

    ⚠️ electron-builder 的 latest.yml 结构是::

        version: 0.3.3
        files:
          - url: qmt_work-Setup-0.3.3.exe
            sha512: <base64>
            size: 236154106
        path: qmt_work-Setup-0.3.3.exe
        sha512: <base64>        # 与 files[0] 相同
        releaseDate: '...'

    也就是说 **`size` 只在 `files[0]` 里，顶层没有** —— 直接读顶层会让 size 校验
    **静默跳过**（本项目明令禁止的失效模式）。所以这里必须把 files[0] 提升上来，
    并且归一化后**必须包含** version / path / sha512 / size 四个键，缺谁由调用方报错。
    """
    data: "dict" = {}
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(text) or {}
        if isinstance(loaded, dict):
            data = dict(loaded)
    except Exception:  # noqa: BLE001
        data = {}

    if not data:  # 无 PyYAML 或解析失败 -> 正则回退
        for key, pat in (
            ("version", r"^version:\s*(\S+)"),
            ("path", r"^path:\s*(\S+)"),
            ("sha512", r"^\s*sha512:\s*(\S+)"),
            ("size", r"^\s*size:\s*(\d+)"),
            ("url", r"^\s*-\s*url:\s*(\S+)"),
        ):
            m = re.search(pat, text, re.M)
            if m:
                data[key] = m.group(1)
        if "size" in data:
            data["size"] = int(data["size"])
        return data

    # 把 files[0] 的 url / sha512 / size 提升到顶层（files[0] 才是权威来源）
    files = data.get("files")
    if isinstance(files, list) and files and isinstance(files[0], dict):
        first = files[0]
        for key in ("url", "sha512", "size"):
            if first.get(key) is not None:
                data[key] = first[key]
    return data


def local_selfcheck(root: Path, version: str, tag: str) -> "tuple[list[Path], list[str]]":
    """返回 (资产列表, 问题列表)。问题非空则不应继续。"""
    problems: "list[str]" = []

    if tag != f"v{version}":
        problems.append(f"tag 与 VERSION 不一致：tag={tag} VERSION={version}")

    dist = root / "frontend-next" / "dist-electron"
    if not dist.is_dir():
        die(f"产物目录不存在：{dist}")

    # 收集资产：只认本版本号
    assets: "list[Path]" = []
    for suffix in ASSET_SUFFIXES:
        # .blockmap 的真实文件名形如 xxx.exe.blockmap
        for cand in sorted(dist.glob(f"*{version}*{suffix}")):
            if cand.is_file() and cand not in assets:
                assets.append(cand)
    # latest.yml 文件名不含版本号，单独补
    latest = dist / "latest.yml"
    if latest.is_file() and latest not in assets:
        assets.append(latest)

    for need in ("qmt_work-Setup-{v}.exe".format(v=version),
                 "qmt_work-{v}.zip".format(v=version),
                 "latest.yml"):
        if not any(a.name == need for a in assets):
            problems.append(f"缺少必需资产：{need}")

    # latest.yml 与 exe 自洽（★ 四个必需字段缺一不可，缺失即报错，绝不静默跳过）
    if not latest.is_file():
        problems.append("缺少 latest.yml（electron-updater 自动更新必需）")
    else:
        meta = parse_latest_yml(latest.read_text(encoding="utf-8"))
        exe = dist / f"qmt_work-Setup-{version}.exe"

        for key in ("version", "path", "sha512", "size"):
            if meta.get(key) is None:
                problems.append(
                    f"latest.yml 缺少必需字段 {key!r} -> 该项校验会被静默跳过，视为失败")

        if not exe.is_file():
            problems.append(f"找不到 exe，无法校验 latest.yml：{exe.name}")
        else:
            real_size = exe.stat().st_size
            real_sha = sha512_b64(exe)
            if meta.get("version") is not None and str(meta["version"]) != version:
                problems.append(f"latest.yml version={meta['version']!r} != {version}")
            if meta.get("path") is not None and str(meta["path"]) != exe.name:
                problems.append(f"latest.yml path={meta['path']!r} != {exe.name}")
            if meta.get("url") is not None and str(meta["url"]) != exe.name:
                problems.append(f"latest.yml files[0].url={meta['url']!r} != {exe.name}")
            if meta.get("size") is not None and int(meta["size"]) != real_size:
                problems.append(f"latest.yml size={meta['size']} != exe 实测 {real_size}")
            if meta.get("sha512") is not None and meta["sha512"] != real_sha:
                problems.append("latest.yml sha512 与 exe 实测不一致")

    return assets, problems


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="发布当前版本到 GitHub Release")
    ap.add_argument("--dry-run", action="store_true", help="只做本地自检，不联网")
    ap.add_argument("--tag", default=None, help="覆盖 tag（默认由 VERSION 推导）")
    ap.add_argument("--notes", default=None, help="Release 正文文件（默认 docs/release-notes/<tag>.md）")
    args = ap.parse_args()

    root = repo_root()
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    tag = args.tag or f"v{version}"
    slug = git_slug(root)

    log("=" * 66)
    log(f"仓库      : {root}")
    log(f"slug      : {slug}")
    log(f"版本      : {version}   tag: {tag}")
    log("=" * 66)

    assets, problems = local_selfcheck(root, version, tag)

    log(f"\n资产清单（{len(assets)} 个）：")
    for a in assets:
        log(f"  - {a.name:<42} {a.stat().st_size:>13,} B")
    if problems:
        log("\n本地自检问题：")
        for p in problems:
            log(f"  ! {p}")

    if args.dry_run:
        log("\n[DRY-RUN] 本地自检完成，未联网。")
        return 0 if not problems else 2

    if problems:
        die("本地自检未通过，拒绝上传（先修上面的问题，或用 --dry-run 复核）")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        die("未设置 GITHUB_TOKEN / GH_TOKEN —— 创建 Release 与上传资产都必须认证。\n"
            "       生成方式：GitHub -> Settings -> Developer settings -> Personal access tokens\n"
            "       classic 需勾选 repo；fine-grained 需 Contents: Read and write。")

    # 正文
    notes_path = Path(args.notes) if args.notes else (root / NOTES_DIR / f"{tag}.md")
    body = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else None
    if body is None:
        log(f"\n[WARN] 未找到正文文件 {notes_path}，将保留 Release 现有正文。")

    # 1) 查 Release
    status, rel = api_call("GET", f"{API_ROOT}/repos/{slug}/releases/tags/{tag}", token)
    if status == 200:
        rel_id = rel["id"]
        log(f"\n[1/3] Release 已存在（id={rel_id}，自动创建于 annotated tag）-> 复用")
        if body is not None:
            st, _ = api_call("PATCH", f"{API_ROOT}/repos/{slug}/releases/{rel_id}",
                             token, {"body": body, "name": tag})
            log(f"      更新正文: HTTP {st}")
    elif status == 404:
        log("\n[1/3] Release 不存在 -> 创建")
        payload = {"tag_name": tag, "name": tag, "draft": False, "prerelease": False}
        if body is not None:
            payload["body"] = body
        st, rel = api_call("POST", f"{API_ROOT}/repos/{slug}/releases", token, payload)
        if st not in (200, 201):
            die(f"创建 Release 失败：HTTP {st} {rel}")
        rel_id = rel["id"]
        log(f"      已创建 id={rel_id}")
    else:
        die(f"查询 Release 失败：HTTP {status} {rel}")

    upload_url = rel.get("upload_url") or f"{UPLOAD_ROOT}/repos/{slug}/releases/{rel_id}/assets"

    # 2) 已存在的同名资产先删
    st, existing = api_call("GET", f"{API_ROOT}/repos/{slug}/releases/{rel_id}/assets", token)
    have = {a["name"]: a["id"] for a in existing} if st == 200 and isinstance(existing, list) else {}
    for a in assets:
        if a.name in have:
            st2, _ = api_call("DELETE",
                              f"{API_ROOT}/repos/{slug}/releases/assets/{have[a.name]}", token)
            log(f"[2/3] 删除同名旧资产 {a.name} -> HTTP {st2}")
    if not any(a.name in have for a in assets):
        log("[2/3] 无同名旧资产，跳过清理")

    # 3) 上传
    log("\n[3/3] 上传资产（大文件，请耐心等待）")
    failures = 0
    for i, a in enumerate(assets, 1):
        t0 = time.time()
        st, res = upload_asset(upload_url, token, a)
        dt = time.time() - t0
        if st in (200, 201):
            log(f"  [{i}/{len(assets)}] OK   {a.name}  {a.stat().st_size:,} B  "
                f"{dt:.1f}s  -> {res.get('browser_download_url', '')}")
        else:
            failures += 1
            log(f"  [{i}/{len(assets)}] FAIL {a.name}  HTTP {st}  {res.get('message', res)}")

    log("\n" + "=" * 66)
    if failures:
        log(f"结果：{len(assets) - failures}/{len(assets)} 上传成功，{failures} 失败")
        return 1
    log(f"结果：{len(assets)}/{len(assets)} 上传成功")
    log(f"Release 页：https://github.com/{slug}/releases/tag/{tag}")
    log("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
