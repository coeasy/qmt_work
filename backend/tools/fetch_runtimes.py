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
"""
import argparse
import os
import sys
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


def _download(url: str, dest: str) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": "qmt_work-fetch-runtimes/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        with open(dest, "wb") as f:
            f.write(r.read())
    return os.path.getsize(dest) > 1_000_000  # embed 包应 >1MB，防错误页


def prepare(minor: int, dry: bool = False) -> str:
    """下载并解压指定小版本的嵌入 Python；返回状态描述。"""
    if minor not in EMBED:
        return f"cp{minor}: 未配置下载源"
    dest = os.path.join(RUNTIMES, f"cp{minor}")
    exe = os.path.join(dest, "python.exe")
    if os.path.isfile(exe):
        _patch_pth(dest)
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
    return f"cp{minor}: 已准备 -> {exe}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="仅准备指定 cpXXX（如 --only cp311 cp38）")
    ap.add_argument("--list", action="store_true", help="仅列出目标，不下载")
    args = ap.parse_args()

    targets = [int(k[2:]) for k in (args.only or [])] if args.only else list(EMBED)
    if args.list:
        for m in targets:
            print(prepare(m, dry=True))
        return
    print(f"目标运行时目录: {RUNTIMES}")
    for m in targets:
        print(prepare(m))
    print("完成。缺失项请检查网络或手动放置 python.org embed 包。")


if __name__ == "__main__":
    main()
