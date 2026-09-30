"""准备多运行时桥接所需的嵌入式 Python（cp38 ~ cp312）。

用途：P0「多运行时 IPC 桥接」需要一组与目标券商 xtquant ABI 匹配的极简 Python，
随包放在 backend/runtimes/cpXXX/python.exe，供 ABI 不匹配时拉起桥接子进程。

实现：下载 python.org 的 embed 压缩包（如 python-3.11.9-embed-amd64.zip），
解压到 backend/runtimes/cp311，并修改 pythonXXX._pth 启用 site + 追加 `..\\..`
（开发态=后端根，打包态=_internal），让子进程能 import 本后端 xtquant_client 包。
缺失项跳过（不阻断已有安装），网络不可用时仅告警。

用法：python tools/fetch_runtimes.py            # 准备 cp38~cp312（已存在则跳过）
      python tools/fetch_runtimes.py --only cp311 cp38
      python tools/fetch_runtimes.py --list       # 仅列出将下载的目标
      python tools/fetch_runtimes.py --only cp311 --with-deps --strict   # CI 推荐

--with-deps：给运行时装好**桥接子进程的第三方依赖闭包**。embed 版 Python 只有
  标准库，而 `xtquant_client` / `core` 还 import 了 cryptography / pydantic /
  pydantic-settings / psutil（core/crypto.py 用 AESGCM、core/config.py 用
  BaseSettings）⇒ 裸 embed 运行时起桥接必 ModuleNotFoundError。
  ⚠️ `backend/runtimes/` 不入库（.gitignore），CI checkout 后必须跑本脚本，
  否则打出的安装包「构建全绿但桥接 100% 起不来」（2026-09-28 实测断链）。
--strict：任一目标未就绪即以非零码退出（CI 用；默认只打印告警继续）。
"""
import argparse
import os
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIMES = os.path.join(ROOT, "runtimes")

# 小版本 -> python.org embed 包文件名（amd64）
EMBED = {
    308: "python-3.8.10-embed-amd64.zip",
    309: "python-3.9.13-embed-amd64.zip",
    310: "python-3.10.11-embed-amd64.zip",
    311: "python-3.11.9-embed-amd64.zip",
    312: "python-3.12.7-embed-amd64.zip",
}
# 下载源：python.org 官方优先，失败时依次尝试国内镜像（华为云等，兼容受限网络）
BASE_URLS = [
    "https://www.python.org/ftp/python/",
    "https://mirrors.huaweicloud.com/python/",
    "https://mirrors.aliyun.com/python-release/",
]


def _version_dir(minor: int) -> str:
    major = 3
    patch = {"308": "3.8.10", "309": "3.9.13", "310": "3.10.11",
             "311": "3.11.9", "312": "3.12.7"}[str(minor)]
    return f"{major}.{minor % 100}.{patch.split('.')[-1]}"


def _patch_pth(dest: str) -> None:
    """修改 embed 包 pythonXXX._pth：启用 site + 追加相对路径 `..\\..`。

    背景：embed 版默认不处理 .pth / PYTHONPATH（._pth 存在即忽略）。
    - `import site` 恢复 site 机制；
    - 追加 `..\\..`（相对解释器目录解析）：
      开发态 runtimes/cpXXX/ -> 后端根；打包态 _internal/runtimes/cpXXX/ -> _internal。
      使子进程可 import 后端 xtquant_client 包（纯 Python，无需第三方依赖）。
    """
    try:
        names = [f for f in os.listdir(dest) if f.lower().endswith("._pth")]
        if not names:
            return
        p = os.path.join(dest, names[0])
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
        new = content.replace("#import site", "import site")
        if r"..\.." not in new:
            new = new.rstrip() + "\n..\\..\n"
        if new != content:
            with open(p, "w", encoding="utf-8") as f:
                f.write(new)
        print(f"  [pth] {os.path.basename(p)} 已启用 site + 后端相对路径")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 修改 ._pth 失败（{exc}），子进程可能无法 import 后端包")


# 桥接子进程（`runtimes/cpXXX/python.exe -m xtquant_client.bridge_server`）的第三方
# 依赖闭包。来源：静态扫描 xtquant_client/ + core/ 的顶层 import（bridge_server 走
# 磁盘真实 .py，只依赖这两包 + stdlib）。改动这两包的新依赖时需同步本清单
# —— 打包期 `build_exe._verify_bridge_imports` 会真实 import 兜底验证。
BRIDGE_DEPS = ["cryptography", "pydantic", "pydantic-settings", "psutil"]


def _bootstrap_pip(exe: str) -> bool:
    """embed 版 Python 不带 pip；先 ensurepip，失败则回退 get-pip.py。"""
    import subprocess  # 局部导入：保持本脚本零第三方依赖

    def _ok() -> bool:
        r = subprocess.run([exe, "-m", "pip", "--version"],  # noqa: S603
                           check=False, capture_output=True, text=True)
        return r.returncode == 0

    if _ok():
        return True
    print("  [pip] 引导 pip（ensurepip）...")
    r = subprocess.run([exe, "-m", "ensurepip", "--upgrade", "--default-pip"],  # noqa: S603
                       check=False, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [pip] ensurepip 失败，回退 get-pip.py（{r.stderr.strip()[:200]}）")
        try:
            import tempfile
            tmp = os.path.join(tempfile.gettempdir(), "get-pip.py")
            for base in ("https://bootstrap.pypa.io/pip/",
                         "https://mirrors.huaweicloud.com/pypi/web/packages/"
                         "source/g/get-pip/"):
                try:
                    _download(base + "get-pip.py", tmp)
                    break
                except Exception:  # noqa: BLE001
                    continue
            r2 = subprocess.run([exe, tmp], check=False,  # noqa: S603
                                capture_output=True, text=True)
            if r2.returncode != 0:
                print(f"  [warn] get-pip 也失败：{r2.stderr.strip()[:200]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] get-pip 下载失败：{exc}")
    return _ok()


def _install_bridge_deps(exe: str) -> bool:
    """把桥接依赖装进该运行时；失败返回 False（CI 配合 --strict 会直接失败）。"""
    import subprocess  # 局部导入

    if not _bootstrap_pip(exe):
        print("  [warn] 运行时无 pip，跳过依赖安装（桥接可能 import 失败）")
        return False
    cmd = [exe, "-m", "pip", "install", "--no-warn-script-location",
           "--disable-pip-version-check", *BRIDGE_DEPS]
    print(f"  [deps] 安装桥接依赖：{' '.join(BRIDGE_DEPS)}")
    r = subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603
    if r.returncode != 0:
        print(f"  [warn] 依赖安装失败：{(r.stderr or '').strip()[-400:]}")
        return False
    # 真实 import 兜底验证：装了不代表能 import（._pth / 路径问题）
    probe = "import cryptography, pydantic, pydantic_settings, psutil; print('deps-ok')"
    v = subprocess.run([exe, "-c", probe], check=False,  # noqa: S603
                       capture_output=True, text=True)
    if v.returncode != 0 or "deps-ok" not in (v.stdout or ""):
        print(f"  [warn] 依赖 import 验证失败：{(v.stderr or '').strip()[-400:]}")
        return False
    print("  [deps] 桥接依赖 import 验证通过")
    return True


def _download(url: str, dest: str) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": "qmt_work-fetch-runtimes/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        with open(dest, "wb") as f:
            f.write(r.read())
    return os.path.getsize(dest) > 1_000_000  # embed 包应 >1MB，防错误页


def prepare(minor: int, dry: bool = False, with_deps: bool = False) -> str:
    """下载并解压指定小版本的嵌入 Python；返回状态描述。"""
    if minor not in EMBED:
        return f"cp{minor}: 未配置下载源"
    dest = os.path.join(RUNTIMES, f"cp{minor}")
    exe = os.path.join(dest, "python.exe")
    if os.path.isfile(exe):
        _patch_pth(dest)
        if with_deps:
            ok = _install_bridge_deps(exe)
            return f"cp{minor}: 已存在 -> {exe}；依赖{'OK' if ok else '缺失'}"
        return f"cp{minor}: 已存在 -> {exe}"
    if dry:
        return f"cp{minor}: 将下载 {EMBED[minor]}"
    ver = _version_dir(minor)
    os.makedirs(dest, exist_ok=True)
    zip_path = os.path.join(dest, EMBED[minor])
    last_err = ""
    ok = False
    for base in BASE_URLS:
        url = f"{base}{ver}/{EMBED[minor]}"
        print(f">> 下载 {url}")
        try:
            if _download(url, zip_path):
                ok = True
                break
            raise RuntimeError(f"文件过小（{os.path.getsize(zip_path)}B）")
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            print(f"   ! 失败：{last_err}，切换下一镜像...")
            try:
                os.remove(zip_path)
            except OSError:
                pass
    if not ok:
        return (f"cp{minor}: 下载失败（{last_err}）— 可手动放置 embed 包到 {dest}"
                f"，或使用代理后重试")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    os.remove(zip_path)
    _patch_pth(dest)
    if with_deps:
        ok = _install_bridge_deps(exe)
        return f"cp{minor}: 已准备 -> {exe}；依赖{'OK' if ok else '缺失'}"
    return f"cp{minor}: 已准备 -> {exe}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="仅准备指定 cpXXX（如 --only cp311 cp38）")
    ap.add_argument("--list", action="store_true", help="仅列出目标，不下载")
    ap.add_argument("--with-deps", action="store_true",
                    help="同时安装桥接子进程的第三方依赖闭包")
    ap.add_argument("--strict", action="store_true",
                    help="任一目标未就绪即非零退出（CI 用）")
    args = ap.parse_args()

    targets = [int(k[2:]) for k in (args.only or [])] if args.only else list(EMBED)
    if args.list:
        for m in targets:
            print(prepare(m, dry=True))
        return
    print(f"目标运行时目录: {RUNTIMES}")
    failed = []
    for m in targets:
        status = prepare(m, with_deps=args.with_deps)
        print(status)
        if not os.path.isfile(os.path.join(RUNTIMES, f"cp{m}", "python.exe")):
            failed.append(f"cp{m}")
        elif args.with_deps and "依赖缺失" in status:
            failed.append(f"cp{m}(deps)")
    if failed:
        msg = ("运行时未就绪：" + ", ".join(failed)
               + "。桥接将不可用；请检查网络或手动放置 python.org embed 包。")
        if args.strict:
            raise SystemExit("[FATAL] " + msg)
        print("[warn] " + msg)
    print("完成。缺失项请检查网络或手动放置 python.org embed 包。")


if __name__ == "__main__":
    main()
